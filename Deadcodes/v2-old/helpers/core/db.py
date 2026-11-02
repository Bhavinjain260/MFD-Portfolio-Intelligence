"""
core/db.py
==========
Single source of truth for DB path, connection, and cross-process data version.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

# ── Absolute path (cwd-independent) ──
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = os.environ.get(
    "MFD_DB_PATH",
    str(_PROJECT_ROOT / "mfd_local.db"),
)

_VERSION_FILE = Path(os.environ.get(
    "DATA_VERSION_FILE",
    str(_PROJECT_ROOT / ".data_version"),
))
_lock = threading.Lock()


def set_db_path(path: str) -> None:
    """Override DB path at runtime (tests, alternate envs)."""
    global DB_PATH
    DB_PATH = path


@contextmanager
def get_conn():
    """Write-capable connection. Commits on success, rolls back on error."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_shared_read_conn():
    """
    Long-lived read-only connection (WAL). Callers must not write.
    Not cached here — ui layer can wrap with @st.cache_resource if desired.
    """
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def bump() -> int:
    """Increment the persisted version counter. Returns the new value.
    Safe across processes (UI, worker, cron)."""
    with _lock:
        try:
            current_val = int(_VERSION_FILE.read_text().strip() or "0")
        except Exception:
            current_val = 0
        new_val = current_val + 1
        try:
            _VERSION_FILE.write_text(str(new_val))
        except Exception:
            pass
        return new_val


def current() -> int:
    """Read the persisted version. Returns 0 if the file is missing."""
    try:
        return int(_VERSION_FILE.read_text().strip() or "0")
    except Exception:
        return 0


def ensure_schema() -> None:
    """
    Idempotent schema setup for app-owned tables that live outside init_db.py
    (overrides, families, monthly brokerage, admin settings, etc.).
    Call once at startup from the UI or worker.
    """
    with get_conn() as conn:
        # Admin settings
        conn.execute("""
            CREATE TABLE IF NOT EXISTS admin_settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Family grouping
        conn.execute("""
            CREATE TABLE IF NOT EXISTS client_family (
                family_id INTEGER PRIMARY KEY AUTOINCREMENT,
                family_name TEXT NOT NULL,
                head_client_code TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS client_family_member (
                family_id INTEGER NOT NULL,
                client_code TEXT NOT NULL,
                is_head INTEGER DEFAULT 0,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (family_id, client_code)
            )
        """)

        # Monthly brokerage manual entry
        conn.execute("""
            CREATE TABLE IF NOT EXISTS monthly_brokerage (
                amc        TEXT NOT NULL,
                month      TEXT NOT NULL,
                year       INTEGER NOT NULL,
                amount     REAL NOT NULL DEFAULT 0,
                notes      TEXT,
                timestamp  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS ux_monthly_brokerage_amy
            ON monthly_brokerage(amc, month, year)
        """)

        # Sync credentials (mailback, etc.)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sync_credentials (
                key         TEXT PRIMARY KEY,
                value       TEXT NOT NULL,
                updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # AUM recon overrides + active views
        conn.execute("""
            CREATE TABLE IF NOT EXISTS txn_overrides (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                rta        TEXT NOT NULL,
                folio      TEXT NOT NULL,
                txn_no     TEXT NOT NULL,
                fund       TEXT NOT NULL DEFAULT '',
                status     TEXT NOT NULL DEFAULT 'inactive',
                reason     TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (rta, folio, txn_no, fund)
            )
        """)
        conn.execute("""
            CREATE VIEW IF NOT EXISTS v_cams_txn_active AS
            SELECT t.* FROM cams_wbr2_transaction t
            WHERE NOT EXISTS (
                SELECT 1 FROM txn_overrides o
                WHERE o.rta = 'CAMS' AND o.status = 'inactive'
                  AND o.folio  = CAST(t.folio_no AS TEXT)
                  AND o.txn_no = CAST(t.trxnno  AS TEXT)
            )
        """)
        conn.execute("""
            CREATE VIEW IF NOT EXISTS v_kfin_txn_active AS
            SELECT t.* FROM kfin_mfsd201_transaction t
            WHERE NOT EXISTS (
                SELECT 1 FROM txn_overrides o
                WHERE o.rta = 'KFinTech' AND o.status = 'inactive'
                  AND o.folio  = CAST(t.td_acno AS TEXT)
                  AND o.txn_no = CAST(t.td_trno AS TEXT)
                  AND o.fund   = COALESCE(CAST(t.td_fund AS TEXT), '')
            )
        """)

        # Sub-broker tables
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sub_brokers (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                name         TEXT NOT NULL,
                email        TEXT,
                phone        TEXT,
                role         TEXT NOT NULL DEFAULT 'Sub-Broker',
                sharing_pct  REAL NOT NULL DEFAULT 0,
                notes        TEXT,
                active       INTEGER NOT NULL DEFAULT 1,
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sub_broker_clients (
                sub_broker_id INTEGER NOT NULL,
                client_code   TEXT NOT NULL,
                assigned_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (sub_broker_id, client_code)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sub_broker_payouts (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                sub_broker_id      INTEGER NOT NULL,
                period_type        TEXT NOT NULL,
                period_key         TEXT NOT NULL,
                total_brokerage    REAL,
                sub_broker_share   REAL,
                paid_amount        REAL,
                paid_on            TEXT,
                notes              TEXT,
                created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(sub_broker_id, period_type, period_key)
            )
        """)

