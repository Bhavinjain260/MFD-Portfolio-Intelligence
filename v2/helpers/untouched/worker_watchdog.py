#!/usr/bin/env python3
"""
worker_watchdog.py
==================
Detects missed cron runs and spawns a catch-up worker.

Cron schedule (must match your crontab):
    0 11 * * *  ~/mfd_nav_cron.sh
    0 15 * * *  ~/mfd_nav_cron.sh

Instead of a naive "last run within N hours" check (which fires
unnecessarily overnight), we compare the stamp against the most
recent *expected* cron slot. A catch-up fires only when the stamp
predates that slot.

Called from app.py on startup and from the 60s poller fragment.
The worker's own flock() guard makes duplicate launches a no-op.
"""

import logging
import os
from datetime import datetime, timedelta, time as _dtime
from pathlib import Path

log = logging.getLogger("worker_watchdog")

PROJECT_DIR = Path(__file__).resolve().parent

STAMP_FILE = os.environ.get(
    "WORKER_STAMP_FILE",
    str(PROJECT_DIR / ".worker_run_stamp"),
)

DEBOUNCE_FILE = os.environ.get(
    "WORKER_DEBOUNCE_FILE",
    str(PROJECT_DIR / ".worker_catchup_debounce"),
)

# ══════════════════════════════════════════════════════════════════════
# SCHEDULE CONFIG — keep in sync with crontab
# ══════════════════════════════════════════════════════════════════════
# Hours (24h, local TZ) at which cron fires the worker.
CRON_HOURS = [11, 15]

# Don't check a slot until it's at least this many minutes old, so a
# legitimately-running cron job isn't flagged as "missed" the second it
# starts. 15 min is safely longer than a typical worker run (30-120s).
GRACE_MINUTES = 15

# How far back to search for the most recent slot if today has none.
# 2 days is ample for a 2-slot-per-day schedule.
LOOKBACK_DAYS = 2

DEBOUNCE_MINUTES = float(os.environ.get("WORKER_DEBOUNCE_MINUTES", "15"))
LAUNCH_TIMEOUT_S = int(os.environ.get("WORKER_LAUNCH_TIMEOUT_S", "300"))


# ══════════════════════════════════════════════════════════════════════
# STAMP READING
# ══════════════════════════════════════════════════════════════════════
def _read_stamp() -> tuple[datetime | None, str | None, int | None]:
    """
    Parse the worker stamp file written by background_worker.run_all().

    Expected format:
        2026-10-12T15:00:10 trigger=cron pid=1673
    """
    try:
        p = Path(STAMP_FILE)
        if not p.exists():
            return None, None, None

        parts = p.read_text().strip().split()
        if not parts:
            return None, None, None

        ts = datetime.fromisoformat(parts[0])
        trigger, pid = None, None
        for token in parts[1:]:
            if token.startswith("trigger="):
                trigger = token.split("=", 1)[1]
            elif token.startswith("pid="):
                try:
                    pid = int(token.split("=", 1)[1])
                except ValueError:
                    pass
        return ts, trigger, pid
    except Exception as e:
        log.warning("[WATCHDOG] Could not read worker stamp: %s", e)
        return None, None, None


# ══════════════════════════════════════════════════════════════════════
# SCHEDULE-AWARE STALENESS
# ══════════════════════════════════════════════════════════════════════
def _last_expected_slot(now: datetime) -> datetime | None:
    """
    Most recent cron slot that has already passed AND cleared the grace
    period. Returns None only if nothing in the lookback window qualifies
    (essentially never happens with LOOKBACK_DAYS=2).
    """
    candidates = []
    for day_offset in range(LOOKBACK_DAYS + 1):
        d = (now - timedelta(days=day_offset)).date()
        for h in CRON_HOURS:
            slot = datetime.combine(d, _dtime(h, 0))
            if slot + timedelta(minutes=GRACE_MINUTES) <= now:
                candidates.append(slot)
    return max(candidates) if candidates else None


def _evaluate_staleness(now: datetime | None = None) -> dict:
    """Core decision. Read-only — never launches."""
    now = now or datetime.now()
    last_slot = _last_expected_slot(now)

    if last_slot is None:
        # Should be unreachable with a 2-day lookback.
        return {
            "last_run": None,
            "last_slot": None,
            "age_hours": None,
            "stale": True,
            "reason": "no prior cron slot in lookback window",
        }

    ts, trigger, pid = _read_stamp()
    age_h = (now - ts).total_seconds() / 3600 if ts else None

    base = {
        "last_run": ts.isoformat(timespec="seconds") if ts else None,
        "last_trigger": trigger,
        "last_pid": pid,
        "last_slot": last_slot.isoformat(timespec="seconds"),
        "age_hours": round(age_h, 2) if age_h is not None else None,
        "now": now.isoformat(timespec="seconds"),
    }

    if ts is None:
        return {
            **base,
            "stale": True,
            "reason": f"no stamp — expected a run since {last_slot:%Y-%m-%d %H:%M}",
        }

    if ts >= last_slot:
        return {
            **base,
            "stale": False,
            "reason": (
                f"ran at {ts:%Y-%m-%d %H:%M} "
                f"(> last slot {last_slot:%Y-%m-%d %H:%M})"
            ),
        }

    return {
        **base,
        "stale": True,
        "reason": (
            f"missed {last_slot:%Y-%m-%d %H:%M} slot "
            f"(last run {ts:%Y-%m-%d %H:%M})"
        ),
    }


def get_staleness() -> dict:
    """Public read-only diagnostic. Safe to call from anywhere."""
    return _evaluate_staleness()


# ══════════════════════════════════════════════════════════════════════
# DEBOUNCE
# ══════════════════════════════════════════════════════════════════════
def _debounced() -> bool:
    p = Path(DEBOUNCE_FILE)
    if not p.exists():
        return False
    try:
        last_attempt = datetime.fromisoformat(p.read_text().strip())
        elapsed_min = (datetime.now() - last_attempt).total_seconds() / 60
        if elapsed_min < DEBOUNCE_MINUTES:
            log.debug(
                "[WATCHDOG] Debounced — %s min since last catch-up attempt",
                round(elapsed_min, 1),
            )
            return True
    except Exception:
        pass
    return False


def _stamp_debounce() -> None:
    try:
        Path(DEBOUNCE_FILE).write_text(
            datetime.now().isoformat(timespec="seconds")
        )
    except Exception as e:
        log.warning("[WATCHDOG] Could not write debounce marker: %s", e)


# ══════════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ══════════════════════════════════════════════════════════════════════
def check_and_launch_if_stale() -> dict:
    """
    Public API. Call from app.py startup and from the 60s poller.
    Never raises — Streamlit should not die because of a watchdog hiccup.
    """
    try:
        from worker_launcher import launch_worker, is_worker_running
    except Exception as e:
        return {
            "triggered": False,
            "reason": f"worker_launcher unavailable: {e}",
        }

    # 1. Something already running? Stand down.
    try:
        if is_worker_running():
            return {
                "triggered": False,
                "reason": "worker already running (flock held or subprocess alive)",
            }
    except Exception as e:
        log.warning("[WATCHDOG] is_worker_running() failed: %s", e)

    # 2. Has a run succeeded since the last expected cron slot?
    stale_info = _evaluate_staleness()
    if not stale_info["stale"]:
        return {"triggered": False, **stale_info}

    # 3. Debounce — don't retry more than once per window.
    if _debounced():
        return {
            "triggered": False,
            "reason": f"debounced (< {DEBOUNCE_MINUTES}min since last attempt)",
            **stale_info,
        }

    # 4. Fire.
    _stamp_debounce()   # write marker BEFORE launching (concurrency guard)

    log.warning(
        "[WATCHDOG] Missed cron slot — %s. Launching catch-up worker.",
        stale_info["reason"],
    )

    try:
        result = launch_worker(timeout_seconds=LAUNCH_TIMEOUT_S)
        return {
            "triggered": True,
            "reason": stale_info["reason"],
            "launch_result": result,
            "stale_info": stale_info,
        }
    except Exception as e:
        log.exception("[WATCHDOG] Catch-up launch failed")
        return {
            "triggered": False,
            "reason": f"launch_worker raised: {e}",
            **stale_info,
        }