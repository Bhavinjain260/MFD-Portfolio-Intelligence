"""
Database layer: connection handling + schema creation.
All tables from legacy app preserved. Run init_db() once at app startup.
"""
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = str(Path(__file__).resolve().parent.parent / "data" / "mfd_local.db")


@contextmanager
def get_conn():
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


SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    client_code TEXT PRIMARY KEY,
    name        TEXT,
    pan         TEXT,
    mobile      TEXT,
    email       TEXT,
    kyc_status  TEXT,
    start_date  TEXT,
    notes       TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS holdings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    client_code     TEXT,
    folio_no        TEXT,
    scheme_code     TEXT,
    scheme_name     TEXT,
    amc             TEXT,
    rta             TEXT DEFAULT 'Unknown',
    investment_type TEXT,
    sip_amount      REAL,
    sip_day         INTEGER DEFAULT 1,
    start_date      TEXT,
    end_date        TEXT,
    status          TEXT DEFAULT 'Active',
    first_order     TEXT DEFAULT 'N'
);
CREATE TABLE IF NOT EXISTS amc_schemes (
    scheme_code TEXT PRIMARY KEY,
    amc         TEXT,
    rta         TEXT DEFAULT 'Unknown',
    scheme_name TEXT,
    category    TEXT,
    last_nav    REAL,
    nav_date    TEXT
);
CREATE TABLE IF NOT EXISTS amc_config (
    amc        TEXT PRIMARY KEY,
    rta        TEXT DEFAULT 'Unknown',
    is_enabled INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS monthly_brokerage (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    amc       TEXT,
    month     TEXT,
    year      INTEGER,
    amount    REAL,
    notes     TEXT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS amc_code_map (
    amc_code TEXT PRIMARY KEY,
    amc_name TEXT NOT NULL
);

-- ===== CAMS =====
CREATE TABLE IF NOT EXISTS cams_aum (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    folio_no        TEXT,
    inv_name        TEXT,
    scheme_name     TEXT,
    amc_code        TEXT,
    pan_no          TEXT,
    email           TEXT,
    rep_date        TEXT,
    units           REAL,
    rupee_bal       REAL,
    upload_batch    TEXT,
    UNIQUE(folio_no, scheme_name, rep_date)
);
CREATE TABLE IF NOT EXISTS cams_transactions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    amc_code        TEXT,
    folio_no        TEXT,
    scheme_code     TEXT,
    scheme_name     TEXT,
    inv_name        TEXT,
    trxn_type       TEXT,
    trxn_no         TEXT,
    trxn_mode       TEXT,
    trxn_status     TEXT,
    trade_date      TEXT,
    post_date       TEXT,
    nav             REAL,
    units           REAL,
    amount          REAL,
    pan             TEXT,
    remarks         TEXT,
    sip_trxn_no     TEXT,
    igst_amount     REAL DEFAULT 0,
    cgst_amount     REAL DEFAULT 0,
    sgst_amount     REAL DEFAULT 0,
    rep_date        TEXT,
    upload_batch    TEXT,
    UNIQUE(trxn_no, folio_no)
);
CREATE TABLE IF NOT EXISTS cams_folio_master (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    folio_no         TEXT,
    inv_name         TEXT,
    address1         TEXT,
    address2         TEXT,
    address3         TEXT,
    city             TEXT,
    pincode          TEXT,
    scheme_code      TEXT,
    scheme_name      TEXT,
    amc_code         TEXT,
    rep_date         TEXT,
    units            REAL,
    rupee_bal        REAL,
    email            TEXT,
    mobile           TEXT,
    pan_no           TEXT,
    joint1_pan       TEXT,
    joint2_pan       TEXT,
    tax_status       TEXT,
    holding_nature   TEXT,
    bank_name        TEXT,
    branch           TEXT,
    ac_type          TEXT,
    ac_no            TEXT,
    ifsc_code        TEXT,
    inv_dob          TEXT,
    nominee_name     TEXT,
    nominee_relation TEXT,
    folio_date       TEXT,
    upload_batch     TEXT,
    UNIQUE(folio_no, scheme_name)
);
CREATE TABLE IF NOT EXISTS cams_sip_master (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    amc_code        TEXT,
    scheme_code     TEXT,
    scheme_name     TEXT,
    folio_no        TEXT,
    inv_name        TEXT,
    sip_reg_no      TEXT,
    sip_amount      REAL,
    from_date       TEXT,
    to_date         TEXT,
    cease_date      TEXT,
    periodicity     TEXT,
    sip_day         INTEGER,
    pan             TEXT,
    payment_mode    TEXT,
    bank_name       TEXT,
    reg_date        TEXT,
    remarks         TEXT,
    status          TEXT,
    upload_batch    TEXT,
    UNIQUE(sip_reg_no, folio_no)
);
CREATE TABLE IF NOT EXISTS cams_brokerage (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    amc_code      TEXT,
    folio_no      TEXT,
    scheme_code   TEXT,
    trxn_no       TEXT,
    trxn_type     TEXT,
    brkage_amt    REAL,
    brkage_type   TEXT,
    brkage_rate   REAL,
    inv_name      TEXT,
    proc_date     TEXT,
    accrual_month TEXT,
    plot_amount   REAL,
    avg_assets    REAL,
    igst_value    REAL,
    cgst_value    REAL,
    sgst_value    REAL,
    upload_batch  TEXT,
    UNIQUE (trxn_no, folio_no, accrual_month)
);

-- ===== KFINTECH =====
CREATE TABLE IF NOT EXISTS kfintech_aum (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    folio_no        TEXT,
    inv_name        TEXT,
    scheme_name     TEXT,
    amc_code        TEXT,
    product_code    TEXT,
    scheme_code     TEXT,
    dividend_opt    TEXT,
    email           TEXT,
    rep_date        TEXT,
    units           REAL,
    rupee_bal       REAL,
    nav             REAL,
    aum             REAL,
    upload_batch    TEXT,
    UNIQUE(folio_no, scheme_name, rep_date)
);
CREATE TABLE IF NOT EXISTS kfintech_brokerage (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    amc_code      TEXT,
    folio_no      TEXT,
    scheme_code   TEXT,
    trxn_no       TEXT,
    trxn_type     TEXT,
    brkage_amt    REAL,
    brkage_type   TEXT,
    brkage_rate   REAL,
    inv_name      TEXT,
    proc_date     TEXT,
    accrual_month TEXT,
    plot_amount   REAL,
    avg_assets    REAL,
    igst_value    REAL DEFAULT 0,
    cgst_value    REAL DEFAULT 0,
    sgst_value    REAL DEFAULT 0,
    upload_batch  TEXT,
    UNIQUE (trxn_no, folio_no, accrual_month)
);
"""


def init_db() -> None:
    """Idempotent. Safe to call on every page load (checked via session_state by caller)."""
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        # Defensive column adds for older DBs
        for tbl, col, default in [
            ("holdings", "first_order", "'N'"),
            ("holdings", "rta", "'Unknown'"),
            ("amc_config", "rta", "'Unknown'"),
            ("amc_schemes", "rta", "'Unknown'"),
        ]:
            try:
                conn.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} TEXT DEFAULT {default}")
            except sqlite3.OperationalError:
                pass
