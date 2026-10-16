"""
compat.py — legacy raw-sqlite3 connection shim.

Use ONLY in services/data_manager.py and services/mailback.py where the
200-column parser INSERTs would be pure churn to rewrite as SQLAlchemy
text() statements. All new code should use app.core.db.SessionLocal.
"""
from contextlib import contextmanager
import sqlite3

from app.core.config import settings


@contextmanager
def get_conn():
    conn = sqlite3.connect(settings.db_path, check_same_thread=False, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()