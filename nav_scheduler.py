"""
NAV background scheduler — lives in its own module (not app.py) so its
module-level state survives Streamlit reruns. app.py itself is re-executed
top-to-bottom on every rerun, which would reset any "already started" flag
defined directly in app.py and spawn a new thread every single time.
"""

import logging
import threading
import time
from datetime import datetime, time as time_cls

log = logging.getLogger("nav_scheduler")

NAV_SCHEDULE_TIMES = [time_cls(11, 0), time_cls(15, 0)]
NAV_SCHEDULE_ENABLED_KEY = "nav_auto_schedule_enabled"

_scheduler_started = False
_scheduler_lock = threading.Lock()


def _ensure_settings_table(get_conn):
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)


def is_nav_schedule_enabled(get_conn) -> bool:
    _ensure_settings_table(get_conn)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = ?", (NAV_SCHEDULE_ENABLED_KEY,)
        ).fetchone()
    return (row[0] if row else "1") == "1"


def set_nav_schedule_enabled(get_conn, enabled: bool) -> None:
    _ensure_settings_table(get_conn)
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO app_settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """, (NAV_SCHEDULE_ENABLED_KEY, "1" if enabled else "0"))


def _scheduler_loop(get_conn, download_fn, reload_fn):
    """
    Runs forever in a daemon thread. Only fires a download for a scheduled
    time slot ONCE per calendar day, tracked in `fired_today` (local to this
    single thread — safe since only one thread ever runs, guarded by
    ensure_started()).
    """
    fired_today: set[str] = set()
    while True:
        try:
            if is_nav_schedule_enabled(get_conn):
                now = datetime.now()
                today = now.strftime("%Y-%m-%d")
                for t in NAV_SCHEDULE_TIMES:
                    key = f"{today}_{t.strftime('%H:%M')}"
                    if now.time() >= t and key not in fired_today:
                        log.info("[NAV-SCHED] Firing scheduled NAV download for %s", t)
                        download_fn(force=True)
                        reload_fn(force=True)
                        fired_today.add(key)
                # prune keys from previous days so the set doesn't grow forever
                fired_today = {k for k in fired_today if k.startswith(today)}
        except Exception:
            log.exception("[NAV-SCHED] Scheduled download failed")
        time.sleep(60)  # check once a minute


def ensure_started(get_conn, download_fn, reload_fn) -> None:
    """
    Idempotent, process-wide singleton start. Safe to call on every
    Streamlit rerun — the thread only actually spawns once per process,
    because this module (unlike app.py) is imported once and its globals
    persist for the life of the process.
    """
    global _scheduler_started
    with _scheduler_lock:
        if _scheduler_started:
            return
        t = threading.Thread(
            target=_scheduler_loop,
            args=(get_conn, download_fn, reload_fn),
            daemon=True,
        )
        t.start()
        _scheduler_started = True
        log.info("[NAV-SCHED] Background NAV scheduler started (11:00, 15:00)")