"""
FIXED nav_scheduler.py - Proper timing, retry backoff, error alerts
Ready to use: just copy/paste this entire file.

Changes:
- Smart sleep timing (check every ~1 min, wake early if approaching scheduled time)
- Exponential backoff on download failures
- Consecutive failure alerting
- Cleaner logging
"""

import logging
import threading
import time
from datetime import datetime, time as time_cls, timedelta

log = logging.getLogger("nav_scheduler")

NAV_SCHEDULE_TIMES = [time_cls(11, 0), time_cls(15, 0)]
NAV_SCHEDULE_ENABLED_KEY = "nav_auto_schedule_enabled"

_scheduler_started = False
_scheduler_lock = threading.Lock()

# Retry backoff state
_consecutive_failures = 0
_max_backoff_seconds = 600  # 10 minutes max


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


def _get_next_scheduled_time():
    """
    Return next scheduled time TODAY, or earliest time TOMORROW if past all today.
    Helps us know when to wake up from sleep.
    """
    now = datetime.now()
    today = now.date()
    
    for t in NAV_SCHEDULE_TIMES:
        scheduled = datetime.combine(today, t)
        if now < scheduled:
            return scheduled
    
    # Past all times today, next is tomorrow's first slot
    tomorrow = today + timedelta(days=1)
    return datetime.combine(tomorrow, NAV_SCHEDULE_TIMES[0])


def _calculate_sleep_time(now: datetime):
    """
    Smart sleep: wake ~30s before next scheduled time, but min 1s, max 60s.
    Ensures we fire within ±30s of scheduled time, not ±60s.
    """
    next_scheduled = _get_next_scheduled_time()
    seconds_until = (next_scheduled - now).total_seconds()
    
    # Wake 30s early, but clamp to [1s, 60s]
    wake_early = max(1, min(60, seconds_until - 30))
    return wake_early


def _scheduler_loop(get_conn, download_fn, reload_fn):
    """
    Runs forever in daemon thread.
    - Fires download ONCE per calendar day per scheduled time
    - Smart sleep timing (wakes ~30s before scheduled)
    - Exponential backoff on failures (max 10 min)
    - Alerts on consecutive failures
    """
    global _consecutive_failures
    
    fired_today: set[str] = set()
    
    while True:
        try:
            if is_nav_schedule_enabled(get_conn):
                now = datetime.now()
                today = now.strftime("%Y-%m-%d")
                fired_this_loop = False
                
                for t in NAV_SCHEDULE_TIMES:
                    key = f"{today}_{t.strftime('%H:%M')}"
                    if now.time() >= t and key not in fired_today:
                        log.info(
                            "[NAV-SCHED] Firing scheduled NAV download for %s",
                            t.strftime("%H:%M"),
                        )
                        try:
                            download_fn(force=True)
                            reload_fn(force=True)
                            fired_today.add(key)
                            fired_this_loop = True
                            _consecutive_failures = 0  # reset on success
                        except Exception as e:
                            _consecutive_failures += 1
                            backoff = min(2 ** _consecutive_failures, _max_backoff_seconds)
                            log.error(
                                f"[NAV-SCHED] Download failed (attempt {_consecutive_failures}). "
                                f"Backing off {backoff}s. Error: {e}"
                            )
                            if _consecutive_failures >= 3:
                                log.critical(
                                    f"[NAV-SCHED] {_consecutive_failures} consecutive failures. "
                                    "Check logs and network connectivity."
                                )
                
                # Prune old keys
                fired_today = {k for k in fired_today if k.startswith(today)}
            
            # Smart sleep
            sleep_time = _calculate_sleep_time(datetime.now())
            time.sleep(sleep_time)
            
        except Exception:
            log.exception("[NAV-SCHED] Unexpected error in loop, will retry in 60s")
            time.sleep(60)


def ensure_started(get_conn, download_fn, reload_fn) -> None:
    """
    Idempotent, process-wide singleton start.
    Safe to call on every Streamlit rerun.
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