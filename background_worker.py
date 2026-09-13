#!/usr/bin/env python3
"""
background_worker.py
====================
Standalone background worker — runs OUTSIDE Streamlit.

Handles all three scheduled tasks:
  1. Mailback (Gmail IMAP poll for CAMS/KFinTech report ZIPs)
  2. NAV auto-download (AMFI live + previous business day)
  3. BSE scheme master (Selenium download, once per day)

Run manually:   python background_worker.py
Cron example:   0 */2 * * *  cd /path && python background_worker.py >> worker.log 2>&1
Systemd:        see bottom of this file for a unit file template

IMPORTANT: Never call any of these from inside app.py, or you'll get
duplicate background threads running in two processes.
"""

import logging
import os
import sys
from datetime import datetime
import time as _time
from pathlib import Path
import fcntl

LOCK_FILE = os.environ.get("WORKER_LOCK", "/tmp/background_worker.lock")
os.environ.setdefault("TZ", "Asia/Kolkata")
_time.tzset()

PROJECT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_DIR))

LOG_FILE = os.environ.get("WORKER_LOG", "background_worker.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s | %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("background_worker")


# ══════════════════════════════════════════════════════════════
# STREAMLIT SHIM
# ══════════════════════════════════════════════════════════════
# nav_service.py, xirr.py, and a few others use @st.cache_data and
# st.session_state. Outside a Streamlit runtime those raise. This shim
# replaces them with no-ops so imported modules work in plain Python.

def _install_streamlit_shim():
    try:
        import streamlit as st
    except ImportError:
        log.warning("streamlit not importable — running without shim")
        return

    if getattr(st, "_worker_shim_installed", False):
        return

    # cache_data / cache_resource -> pass-through decorators
    def _noop_cache(*dargs, **dkwargs):
        def wrap(fn):
            fn.clear = lambda *a, **k: None
            return fn
        if len(dargs) == 1 and callable(dargs[0]) and not dkwargs:
            return dargs[0]
        return wrap

    st.cache_data = _noop_cache
    st.cache_resource = _noop_cache

    # session_state -> dict that silently accepts everything
    class _FakeSessionState(dict):
        def get(self, k, default=None):
            return super().get(k, default)

    st.session_state = _FakeSessionState()
    st._worker_shim_installed = True


_install_streamlit_shim()


# ══════════════════════════════════════════════════════════════
# DB INIT
# ══════════════════════════════════════════════════════════════
def _ensure_db():
    from init_db import init_db, get_conn
    init_db()
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS admin_settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)


def _setting_enabled(key: str, default: bool = True) -> bool:
    from init_db import get_conn
    with get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM admin_settings WHERE key = ?", (key,)
        ).fetchone()
    if not row:
        return default
    return str(row[0]) == "1"


# ══════════════════════════════════════════════════════════════
# TASKS
# ══════════════════════════════════════════════════════════════
def task_mailback() -> dict:
    """Poll Gmail for CAMS/KFinTech mailback ZIPs, import to DB."""
    log.info("[MAILBACK] Starting")
    try:
        import cams_mailback_sync as cms

        if not _setting_enabled("mailback_poll_enabled", default=True):
            log.info("[MAILBACK] Disabled via admin_settings — skipping")
            return {"ok": True, "msg": "disabled"}

        if not cms.credentials_configured():
            log.info("[MAILBACK] No credentials configured — skipping")
            return {"ok": True, "msg": "no credentials"}

        result = cms.sync_once()
        msg = (
            f"checked={result['checked']} "
            f"downloaded={len(result['downloaded'])} "
            f"parsed={len(result['parsed'])} "
            f"failed={len(result['parse_failed'])} "
            f"no_data={len(result['no_data'])} "
            f"errors={len(result['errors'])}"
        )
        log.info("[MAILBACK] %s", msg)
        return {"ok": not result.get("errors"), "msg": msg, "result": result}

    except Exception as e:
        log.exception("[MAILBACK] Failed")
        return {"ok": False, "msg": str(e)}


def task_nav_live() -> dict:
    """Download AMFI's live NAVAll.txt if the current publish cycle is stale."""
    log.info("[NAV-LIVE] Starting")
    try:
        from nav_service import download_and_save_nav_if_needed
        result = download_and_save_nav_if_needed(force=False)
        log.info("[NAV-LIVE] ok=%s ran=%s reason=%s",
                 result.get("ok"), result.get("ran"), result.get("reason"))
        return result
    except Exception as e:
        log.exception("[NAV-LIVE] Failed")
        return {"ok": False, "reason": str(e)}


def task_nav_previous() -> dict:
    """Fetch the previous business day's NAV snapshot (for 1-day diff)."""
    log.info("[NAV-PREV] Starting")
    try:
        from nav_service import sync_previous_business_day_nav
        result = sync_previous_business_day_nav(force=False)
        log.info("[NAV-PREV] ok=%s ran=%s reason=%s",
                 result.get("ok"), result.get("ran"), result.get("reason"))
        return result
    except Exception as e:
        log.exception("[NAV-PREV] Failed")
        return {"ok": False, "reason": str(e)}


def task_bse() -> dict:
    """Download today's BSE scheme master via Selenium, parse, import."""
    log.info("[BSE] Starting")
    try:
        if not _setting_enabled("bse_auto_enabled", default=True):
            log.info("[BSE] Disabled via admin_settings — skipping")
            return {"ok": True, "msg": "disabled"}

        from bse_auto import (
            has_todays_file, _reset_status, _set_status,
            _do_download, _today_done_filename,
        )
        from pathlib import Path as _P
        import data_manager as dm

        if has_todays_file():
            log.info("[BSE] Today's file already present — skipping")
            return {"ok": True, "msg": "already have today's file"}

        _reset_status()
        _set_status(running=True, done=False, started_at=datetime.now().isoformat())

        result = _do_download()
        final_msg = result["msg"]
        final_path = result.get("path")

        if result["ok"] and final_path:
            path = _P(final_path)
            if path.exists() and not path.name.endswith("_done.txt"):
                with open(path, "rb") as f:
                    db_ok, db_msg, _preview = dm.parse_bse_scheme_master(f, replace=False)
                if db_ok:
                    done_path = path.with_name(_today_done_filename())
                    path.rename(done_path)
                    final_path = str(done_path)
                    final_msg = f"{result['msg']} | DB: {db_msg}"
                else:
                    final_msg = f"{result['msg']} | DB import failed: {db_msg}"

        _set_status(
            running=False, done=True, ok=result["ok"],
            path=final_path, msg=final_msg,
            finished_at=datetime.now().isoformat(),
        )

        log.info("[BSE] ok=%s msg=%s", result["ok"], final_msg)
        return {"ok": result["ok"], "msg": final_msg, "path": final_path}

    except Exception as e:
        log.exception("[BSE] Failed")
        return {"ok": False, "msg": str(e)}


# ══════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════


def run_all():
    log.info("=" * 70)
    log.info("BACKGROUND WORKER RUN  @ %s", datetime.now().isoformat(timespec="seconds"))
    log.info("=" * 70)

    # ── Single-instance guard ──
    # If another instance is already running, exit immediately. This protects
    # against: duplicate cron entries, manual runs overlapping cron, a fork
    # left over from a previous crash, etc.
    lock_fp = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.warning("[WORKER] Another instance is already running — exiting")
        lock_fp.close()
        return

    try:
        lock_fp.write(str(os.getpid()))
        lock_fp.flush()

        try:
            _ensure_db()
        except Exception:
            log.exception("DB init failed — aborting")
            return

        for name, fn in [
            ("mailback", task_mailback),
            ("nav_live", task_nav_live),
            ("nav_previous", task_nav_previous),
        ]:
            try:
                res = fn()
                log.info("[%s] -> %s", name, "OK" if res.get("ok") else "FAIL")
            except Exception:
                log.exception("[%s] crashed", name)

        log.info("=" * 70)
    finally:
        fcntl.flock(lock_fp, fcntl.LOCK_UN)
        lock_fp.close()


if __name__ == "__main__":
    run_all()