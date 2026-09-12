"""
runtime_status.py
=================
Cross-process status file. Streamlit reads it; background_worker writes it.
Atomic writes via temp file + rename — safe under concurrent access.
"""

import json
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

STATUS_PATH = Path(os.environ.get("RUNTIME_STATUS_PATH", "runtime_status.json"))


def _read_raw() -> dict:
    if not STATUS_PATH.exists():
        return {}
    try:
        return json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except Exception:
        log.exception("[STATUS] Failed to read %s", STATUS_PATH)
        return {}


def _write_atomic(data: dict) -> None:
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=str(STATUS_PATH.parent), prefix=".runtime_status_", suffix=".tmp"
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp_path, STATUS_PATH)
    except Exception:
        log.exception("[STATUS] Failed to write %s", STATUS_PATH)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def get_task(task: str) -> dict:
    return _read_raw().get(task, {})


def get_all() -> dict:
    return _read_raw()


def update_task(task: str, **kwargs) -> dict:
    data = _read_raw()
    block = data.get(task, {})
    kwargs.setdefault("last_attempt", datetime.now().isoformat(timespec="seconds"))
    block.update(kwargs)
    data[task] = block
    _write_atomic(data)
    return block


def record_success(task: str, msg: str, **extra) -> dict:
    return update_task(
        task,
        last_success=datetime.now().isoformat(timespec="seconds"),
        last_ok=True,
        last_msg=msg,
        **extra,
    )


def record_failure(task: str, msg: str, **extra) -> dict:
    return update_task(task, last_ok=False, last_msg=msg, **extra)