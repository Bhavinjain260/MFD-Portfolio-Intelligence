from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import text

from app.core.db import SessionLocal
from app.services.cache import bump
from app.services import bse as bse_svc
from app.services import data_manager as dm
from app.services import mailback as mb
from app.services import nav_service as ns

log = logging.getLogger("worker.tasks")


# ── job_runs bookkeeping ────────────────────────────────────────────────
def _start_run(task: str, trigger: str) -> int:
    with SessionLocal() as s:
        cur = s.execute(text("""
            INSERT INTO job_runs (task, trigger, started_at)
            VALUES (:t, :tr, CURRENT_TIMESTAMP)
        """), {"t": task, "tr": trigger})
        job_id = cur.lastrowid
        s.commit()
    return job_id


def _adopt_run(job_id: int, task: str, trigger: str) -> None:
    """Update an existing queued row to 'running' instead of inserting a duplicate."""
    with SessionLocal() as s:
        s.execute(text("""
            UPDATE job_runs
            SET task = :t, trigger = :tr, started_at = CURRENT_TIMESTAMP, msg = 'running'
            WHERE id = :i
        """), {"t": task, "tr": trigger, "i": job_id})
        s.commit()


def _finish_run(job_id: int, ok: bool, msg: str) -> None:
    with SessionLocal() as s:
        s.execute(text("""
            UPDATE job_runs
            SET finished_at = CURRENT_TIMESTAMP, ok = :ok, msg = :m
            WHERE id = :i
        """), {"ok": 1 if ok else 0, "m": msg[:2000], "i": job_id})
        s.commit()


def _setting_enabled(key: str, default: bool = True) -> bool:
    with SessionLocal() as s:
        row = s.execute(text(
            "SELECT value FROM admin_settings WHERE key = :k"
        ), {"k": key}).fetchone()
    if not row:
        return default
    return str(row[0]) == "1"


# ── task runner ─────────────────────────────────────────────────────────
def _run(task: str, trigger: str, fn, job_id: int | None = None) -> dict:
    """
    Execute `fn()` under job_runs bookkeeping.

    If `job_id` is provided, we adopt the existing row (from a manual
    queue request) instead of inserting a fresh one — otherwise the API
    would insert one row and this would insert a second, leaving the
    first stuck at 'running' forever.
    """
    if job_id is None:
        job_id = _start_run(task, trigger)
    else:
        _adopt_run(job_id, task, trigger)

    try:
        result = fn()
        ok = bool(result.get("ok", True))
        msg = str(result.get("msg") or result.get("reason") or "")
        _finish_run(job_id, ok, msg)
        return result
    except Exception as e:
        log.exception("[%s] crashed", task)
        _finish_run(job_id, False, str(e))
        return {"ok": False, "msg": str(e)}


# ══════════════════════════════════════════════════════════════
# Tasks
# ══════════════════════════════════════════════════════════════
def task_mailback(trigger: str = "cron", job_id: int | None = None) -> dict:
    def _do():
        if not _setting_enabled("mailback_poll_enabled", default=True):
            return {"ok": True, "msg": "disabled via admin_settings"}
        if not mb.credentials_configured():
            return {"ok": True, "msg": "no credentials configured"}
        res = mb.sync_once()
        msg = (f"checked={res['checked']} "
               f"downloaded={len(res['downloaded'])} "
               f"parsed={len(res['parsed'])} "
               f"failed={len(res['parse_failed'])} "
               f"no_data={len(res['no_data'])} "
               f"errors={len(res['errors'])}")
        return {"ok": not res["errors"], "msg": msg}
    return _run("mailback", trigger, _do, job_id=job_id)


def task_nav_live(trigger: str = "cron", job_id: int | None = None) -> dict:
    return _run("nav_live", trigger,
                lambda: ns.download_and_save_nav_if_needed(force=False),
                job_id=job_id)


def task_nav_previous(trigger: str = "cron", job_id: int | None = None) -> dict:
    return _run("nav_previous", trigger,
                lambda: ns.sync_previous_business_day_nav(force=False),
                job_id=job_id)


def task_bse(trigger: str = "cron", job_id: int | None = None) -> dict:
    def _do():
        if not _setting_enabled("bse_auto_enabled", default=True):
            return {"ok": True, "msg": "disabled via admin_settings"}
        if bse_svc.has_todays_file():
            return {"ok": True, "msg": "already have today's file"}
        return bse_svc.sync_now(parse_func=dm.parse_bse_scheme_master)
    return _run("bse", trigger, _do, job_id=job_id)


# ══════════════════════════════════════════════════════════════
# Manual job queue poller
# ══════════════════════════════════════════════════════════════
_MANUAL_DISPATCH = {
    "mailback":     task_mailback,
    "nav_live":     task_nav_live,
    "nav_previous": task_nav_previous,
    "bse":          task_bse,
}


def poll_and_run_manual_jobs() -> int:
    """Fetch queued rows and execute them, reusing the existing job_id."""
    with SessionLocal() as s:
        rows = s.execute(text("""
            SELECT id, task FROM job_runs
            WHERE trigger='manual' AND ok IS NULL AND msg='queued'
            ORDER BY id ASC LIMIT 20
        """)).fetchall()

    count = 0
    for job_id, task in rows:
        fn = _MANUAL_DISPATCH.get(task)
        if not fn:
            with SessionLocal() as s:
                s.execute(text("""
                    UPDATE job_runs
                    SET finished_at=CURRENT_TIMESTAMP, ok=0, msg='unknown task'
                    WHERE id=:i
                """), {"i": job_id})
                s.commit()
            continue

        # Mark in-progress first so a slow job isn't picked twice.
        with SessionLocal() as s:
            s.execute(text(
                "UPDATE job_runs SET msg='running' WHERE id=:i"
            ), {"i": job_id})
            s.commit()

        try:
            fn(trigger="manual", job_id=job_id)
        except Exception:
            log.exception("[%s] manual run failed", task)
        count += 1

    return count


# ══════════════════════════════════════════════════════════════
# run_all — composite used by cron slots
# ══════════════════════════════════════════════════════════════
def run_all(trigger: str = "cron") -> None:
    log.info("=" * 60)
    log.info("WORKER RUN  trigger=%s  @ %s", trigger,
             datetime.now().isoformat(timespec="seconds"))

    for task_fn in (task_mailback, task_nav_live, task_nav_previous):
        result = task_fn(trigger=trigger)
        log.info("  → %s: %s", task_fn.__name__,
                 "OK" if result.get("ok") else "FAIL")

    try:
        bump()
        log.info("[WORKER] data_version bumped")
    except Exception:
        log.exception("[WORKER] bump failed")

    log.info("WORKER RUN COMPLETE  @ %s",
             datetime.now().isoformat(timespec="seconds"))
    log.info("=" * 60)