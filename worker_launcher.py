"""
worker_launcher.py
==================
Spawns background_worker.py as a separate subprocess.

Used by the Admin Panel's "Run Background Worker Now" button so that:
  • The worker runs in its own process (isolated crashes, memory, logging).
  • The worker's own fcntl.flock guard prevents double-runs when cron is
    already mid-cycle.
  • Output goes to the same background_worker.log the cron runs use.

Never raises. Always returns a dict the UI can render.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).resolve().parent
WORKER_SCRIPT = PROJECT_DIR / "background_worker.py"


def launch_worker(timeout_seconds: int = 300) -> dict:
    """
    Run `python background_worker.py` as a child process and wait for it.

    Blocks the calling Streamlit script until the worker finishes (or the
    timeout expires). Typical run is 30–120s depending on whether the
    mailback poller has new mail.

    Returns:
        {
            "ok": bool,
            "msg": str,
            "returncode": int | None,
            "duration_s": float,
            "log_tail": str,       # last 60 lines of background_worker.log
            "timed_out": bool,
        }
    """
    started = datetime.now()

    if not WORKER_SCRIPT.exists():
        return {
            "ok": False,
            "msg": f"Worker script not found at {WORKER_SCRIPT}",
            "returncode": None,
            "duration_s": 0.0,
            "log_tail": "",
            "timed_out": False,
        }

    env = os.environ.copy()
    env.setdefault("TZ", "Asia/Kolkata")
    # Ensure the worker finds the same DB and log files the app uses
    env.setdefault("WORKER_LOG", str(PROJECT_DIR / "background_worker.log"))
    env.setdefault("WORKER_LOCK", "/tmp/background_worker.lock")

    log.info("[LAUNCHER] Spawning %s", WORKER_SCRIPT)
    try:
        proc = subprocess.run(
            [sys.executable, str(WORKER_SCRIPT)],
            cwd=str(PROJECT_DIR),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as e:
        duration = (datetime.now() - started).total_seconds()
        log.warning("[LAUNCHER] Worker timed out after %ss", timeout_seconds)
        return {
            "ok": False,
            "msg": f"Worker timed out after {timeout_seconds}s (still running in background)",
            "returncode": None,
            "duration_s": duration,
            "log_tail": _tail_worker_log(),
            "timed_out": True,
        }
    except Exception as e:
        duration = (datetime.now() - started).total_seconds()
        log.exception("[LAUNCHER] Failed to spawn worker")
        return {
            "ok": False,
            "msg": f"Failed to spawn worker: {e}",
            "returncode": None,
            "duration_s": duration,
            "log_tail": "",
            "timed_out": False,
        }

    duration = (datetime.now() - started).total_seconds()
    ok = proc.returncode == 0

    if ok:
        msg = f"✅ Worker finished cleanly in {duration:.1f}s"
    else:
        msg = f"❌ Worker exited with code {proc.returncode} after {duration:.1f}s"

    # If stderr has anything, surface the last few lines (worker logs there too)
    stderr_tail = ""
    if proc.stderr and proc.stderr.strip():
        stderr_lines = proc.stderr.strip().splitlines()
        stderr_tail = "\n".join(stderr_lines[-10:])

    return {
        "ok": ok,
        "msg": msg,
        "returncode": proc.returncode,
        "duration_s": duration,
        "log_tail": _tail_worker_log(),
        "timed_out": False,
        "stderr_tail": stderr_tail,
    }


def _tail_worker_log(max_lines: int = 60) -> str:
    """Read the last N lines of background_worker.log for UI display."""
    log_path = Path(os.environ.get("WORKER_LOG", "background_worker.log"))
    if not log_path.exists():
        return "(no worker log file yet)"
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return "".join(lines[-max_lines:])
    except Exception as e:
        return f"(could not read worker log: {e})"


def is_worker_running() -> bool:
    """
    Best-effort check: is another worker holding the flock right now?
    Uses the same lock file path the worker uses.
    """
    import fcntl
    lock_path = os.environ.get("WORKER_LOCK", "/tmp/background_worker.lock")
    try:
        fp = open(lock_path, "w")
    except OSError:
        return False
    try:
        fcntl.flock(fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # We got the lock — no one else is holding it
        fcntl.flock(fp, fcntl.LOCK_UN)
        fp.close()
        return False
    except BlockingIOError:
        fp.close()
        return True