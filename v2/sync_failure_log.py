"""
sync_failure_log.py
====================
Persistent, append-only log for data sync failures (mailback, NAV, BSE).
Writes to a JSON-lines file so it's both human-readable and machine-parseable.

Log file location:  logs/sync_failures.log

Each line is a JSON object:
    {"ts": "2026-09-16T14:23:11", "source": "mailback", "rta": "CAMS",
     "report": "WBR2", "file": "wbr2_20260916.txt", "stage": "parse",
     "msg": "...", "context": {...}}

Never raises — logging failure must never break a sync run.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

# ── Configurable location ──
LOG_DIR = Path(os.environ.get("SYNC_LOG_DIR", "logs"))
LOG_FILE = LOG_DIR / "sync_failures.log"

# ── Rotation: keep the file under ~5 MB ──
MAX_BYTES = 5 * 1024 * 1024
BACKUP_SUFFIX = ".1"

_lock = threading.Lock()


def _ensure_dir() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def _rotate_if_needed() -> None:
    """Rename current log to .1 if it exceeds MAX_BYTES. Single backup kept."""
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > MAX_BYTES:
            backup = LOG_FILE.with_suffix(LOG_FILE.suffix + BACKUP_SUFFIX)
            if backup.exists():
                backup.unlink()
            LOG_FILE.rename(backup)
    except Exception:
        # Rotation is a nice-to-have; never let it break logging
        pass


def record_failure(
    source: str,          # "mailback" | "nav" | "bse" | "manual_upload"
    stage: str,           # "fetch" | "extract" | "parse" | "db_insert" | "unknown"
    msg: str,
    rta: str = "",
    report: str = "",
    file: str = "",
    context: dict | None = None,
) -> None:
    """
    Append one failure record. Safe to call from any thread.
    Never raises — if logging fails, it's swallowed (we log locally).
    """
    try:
        _ensure_dir()
        entry = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "source": source,
            "stage": stage,
            "msg": str(msg)[:2000],  # truncate runaway error strings
            "rta": rta,
            "report": report,
            "file": file,
            "context": context or {},
        }
        line = json.dumps(entry, default=str)

        with _lock:
            _rotate_if_needed()
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")

    except Exception:
        # Last-resort: log to stderr via the module logger, never re-raise
        try:
            log.exception("[SYNC-LOG] Failed to write failure record")
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════
# READER (used by Admin Panel UI)
# ══════════════════════════════════════════════════════════════
def read_failures(limit: int = 200, source_filter: str = "") -> list[dict]:
    """
    Return the most recent `limit` failures (newest first).
    Optionally filter by source ("mailback", "nav", "bse").
    """
    if not LOG_FILE.exists():
        return []

    try:
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return []

    records: list[dict] = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if source_filter and rec.get("source") != source_filter:
            continue
        records.append(rec)
        if len(records) >= limit:
            break

    return records


def clear_log() -> bool:
    """Truncate the log file. Returns True on success."""
    try:
        with _lock:
            if LOG_FILE.exists():
                LOG_FILE.unlink()
        return True
    except Exception:
        log.exception("[SYNC-LOG] Failed to clear log")
        return False


def get_log_path() -> str:
    return str(LOG_FILE.resolve())