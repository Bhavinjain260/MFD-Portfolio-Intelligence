from __future__ import annotations

import functools
import hashlib
import pickle
import threading

from cachetools import TTLCache
from sqlalchemy import text

from app.core.db import SessionLocal

_cache: TTLCache = TTLCache(maxsize=512, ttl=300)
_lock = threading.Lock()

_VERSION_KEY = "data_version"


def data_version() -> int:
    with SessionLocal() as s:
        row = s.execute(
            text("SELECT value FROM admin_settings WHERE key = :k"),
            {"k": _VERSION_KEY},
        ).fetchone()
    try:
        return int(row[0]) if row and row[0] is not None else 0
    except (TypeError, ValueError):
        return 0


def bump() -> int:
    with SessionLocal() as s:
        row = s.execute(
            text("SELECT value FROM admin_settings WHERE key = :k"),
            {"k": _VERSION_KEY},
        ).fetchone()
        current = int(row[0]) if row and row[0] is not None else 0
        new_val = current + 1
        s.execute(
            text("""
                INSERT INTO admin_settings (key, value, updated_at)
                VALUES (:k, :v, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = CURRENT_TIMESTAMP
            """),
            {"k": _VERSION_KEY, "v": str(new_val)},
        )
        s.commit()
        return new_val


def _make_key(fn, args, kwargs, version: int) -> str:
    try:
        payload = (
            fn.__module__,
            fn.__qualname__,
            version,
            args,
            tuple(sorted(kwargs.items())),
        )
        return hashlib.sha1(pickle.dumps(payload, protocol=4)).hexdigest()
    except Exception:
        return f"{fn.__module__}.{fn.__qualname__}:{version}:{id(args)}"


def cached(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        v = data_version()
        key = _make_key(fn, args, kwargs, v)
        with _lock:
            hit = _cache.get(key)
        if hit is not None:
            return hit
        result = fn(*args, **kwargs)
        with _lock:
            _cache[key] = result
        return result

    wrapper.cache_clear = lambda: _cache.clear()
    return wrapper


def invalidate() -> None:
    with _lock:
        _cache.clear()