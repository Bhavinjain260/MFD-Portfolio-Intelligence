
"""
MFD Portfolio Intelligence — Streamlit App
Updated: Folio Normalization + Full Precision Brokerage + Month Filters + RTA Bifurcation + AMC Breakdown
+ CAMS AUM Report Upload + Total AUM on Dashboard + Client Invested Amount
+ KFINTECH AUM Report Upload + KFinTech AUM Dashboard Integration
+ KFINTECH BROKERAGE UPLOAD + RECONCILIATION + CLIENT VIEW INTEGRATION
+ Admin Panel Revamp: Import BSE Data tab, merged RTA Data Upload tab
+ Smart Upsert: skip existing records, Replace = delete+reinsert only that dataset
"""
import logging
import re
import threading
import warnings
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Optional
import pandas as pd
import plotly.express as px
import requests
import sqlite3
import streamlit as st
from streamlit.runtime.scriptrunner import add_script_run_ctx

warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.WARNING, format="%(levelname)s | %(message)s")
log = logging.getLogger(__name__)

# ==================== CONSTANTS ====================
DB_PATH = "mfd_local.db"
AMFI_TEXT_URL = "https://portal.amfiindia.com/spages/NAVAll.txt"
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
CANCELLED_KEYWORDS = frozenset(
    ["CXL", "AUTOCXL", "AUTO CXL", "CX", "CANCEL", "CLOSED", "REDEEM", "STOPPED", "FAILED"]
)
AMC_SUFFIXES = [
    " MUTUAL FUND", " MF", " FUND", " AMC",
    " INDIA", " MANAGEMENT", " LTD", " LIMITED",
]
_WHITESPACE_RE = re.compile(r"\s+")
PAGE_SIZE = 20

# Normalized RTA Lists (auto-matched against AMC names)
CAMS_AMCS = frozenset([
    "360 ONE", "ADITYA BIRLA SUN LIFE", "ANGEL ONE", "BANDHAN", "DSP",
    "FRANKLIN TEMPLETON", "HDFC", "HELIOS", "HSBC", "ICICI PRUDENTIAL",
    "JIO BLACKROCK", "KOTAK", "MAHINDRA MANULIFE", "NAVI", "PPFAS",
    "SBI", "SHRIRAM", "TATA", "UNIFI", "UNION", "WHITEOAK", "ZERODHA"
])
KFIN_AMCS = frozenset([
    "AXIS", "BARODA BNP PARIBAS", "BANK OF INDIA", "BAJAJ FINSERV",
    "CANARA ROBECO", "CAPITALMIND", "EDELWEISS", "GROWW", "INVESTECO",
    "ITI", "JM FINANCIAL", "LIC", "MIRAE ASSET", "MOTILAL OSWAL",
    "NIPPON INDIA", "OLD BRIDGE", "NJ", "PGIM", "QUANTUM", "QUANT",
    "SAMCO", "SUNDARAM", "TRUST", "TAURUS", "UTI"
])


# ==================== DB HELPERS ====================
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


def init_db() -> None:
    if not st.session_state.get("db_initialized"):
        with get_conn() as conn:
            conn.executescript("""
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
            """)
        st.session_state["db_initialized"] = True

        with get_conn() as conn:
            for tbl, col, default in [("holdings", "first_order", "'N'"), ("holdings", "rta", "'Unknown'"),
                                      ("amc_config", "rta", "'Unknown'"), ("amc_schemes", "rta", "'Unknown'")]:
                try:
                    conn.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} TEXT DEFAULT {default}")
                except sqlite3.OperationalError:
                    pass

            conn.executescript("""
                CREATE TABLE IF NOT EXISTS amc_code_map (
                    amc_code TEXT PRIMARY KEY,
                    amc_name TEXT NOT NULL
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
                    upload_batch    TEXT
                );
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
                    upload_batch    TEXT
                );
            """)

            try:
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS ux_cams_aum ON cams_aum (folio_no, scheme_name, rep_date)")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS ux_kfintech_aum ON kfintech_aum (folio_no, scheme_name, rep_date)")
            except sqlite3.OperationalError:
                pass

            try:
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS ux_monthly_brokerage ON monthly_brokerage (amc, month, year)")
            except sqlite3.OperationalError:
                conn.execute(
                    """DELETE FROM monthly_brokerage WHERE id NOT IN (SELECT MAX(id) FROM monthly_brokerage GROUP BY amc, month, year)""")
                try:
                    conn.execute(
                        "CREATE UNIQUE INDEX IF NOT EXISTS ux_monthly_brokerage ON monthly_brokerage (amc, month, year)")
                except sqlite3.OperationalError:
                    pass


# ==================== PURE HELPERS ====================
def clean_str(val) -> str:
    if val is None: return ""
    try:
        if pd.isna(val): return ""
    except (TypeError, ValueError):
        pass
    s = str(val).strip()
    return "" if s.lower() in {"nan", "none", "null", "na", ""} else s


def format_currency(val, decimals: int = 2) -> str:
    try:
        return f"Rs {float(val):,.{decimals}f}"
    except (TypeError, ValueError):
        return "Rs -"


def format_brokerage(val) -> str:
    try:
        amount = float(val)
        formatted = f"{amount:.8f}".rstrip('0').rstrip('.')
        return f"Rs {formatted}"
    except (TypeError, ValueError):
        return "Rs -"


def format_aum(val) -> str:
    try:
        amount = float(val)
        if amount >= 1_00_00_000:
            return f"Rs {amount / 1_00_00_000:.2f} Cr"
        elif amount >= 1_00_000:
            return f"Rs {amount / 1_00_000:.2f} L"
        else:
            return f"Rs {amount:,.0f}"
    except (TypeError, ValueError):
        return "Rs -"


@st.cache_data(show_spinner=False)
def normalize_amc(name: str) -> str:
    if not name: return ""
    n = str(name).strip().upper()
    for suffix in AMC_SUFFIXES: n = n.replace(suffix, "")
    return _WHITESPACE_RE.sub(" ", n).strip()


def get_rta(amc_name: str) -> str:
    norm = normalize_amc(amc_name)
    if norm in CAMS_AMCS: return "CAMS"
    if norm in KFIN_AMCS: return "KFinTech"
    return "Unknown"


def normalize_folio(folio: str) -> str:
    if not folio: return ""
    try:
        if pd.isna(folio): return ""
    except Exception:
        pass
    return str(folio).strip().split("/")[0].strip().lower()


def is_active_status(raw_status) -> bool:
    if pd.isna(raw_status): return True
    status = str(raw_status).strip().upper()
    return not any(kw in status for kw in CANCELLED_KEYWORDS)


def parse_date_safe(val) -> str:
    if pd.isna(val) if isinstance(val, float) else str(val).strip() in {"", "None", "NaN"}: return ""
    try:
        dt = pd.to_datetime(val, dayfirst=True, errors="coerce")
        return dt.strftime("%Y-%m-%d") if pd.notna(dt) else ""
    except Exception:
        return ""


def get_next_sip_date(day) -> str:
    if pd.isna(day) or not day: return "N/A"
    try:
        day = int(day)
        today = datetime.now()
        try:
            candidate = today.replace(day=day)
            if candidate.date() >= today.date(): return candidate.strftime("%d %b %Y")
        except ValueError:
            pass
        next_month_first = (today.replace(day=28) + timedelta(days=4)).replace(day=1)
        try:
            return next_month_first.replace(day=day).strftime("%d %b %Y")
        except ValueError:
            last_day = (next_month_first.replace(month=next_month_first.month % 12 + 1, day=1) - timedelta(days=1))
            return last_day.strftime("%d %b %Y")
    except Exception:
        return "N/A"


# ==================== AMFI SYNC ====================
def _amfi_sync_worker(result_bucket: list) -> None:
    try:
        res = requests.get(AMFI_TEXT_URL, timeout=20)
        res.raise_for_status()
        lines = res.text.splitlines()
        schemes, curr_amc = [], None
        for line in lines:
            line = line.strip()
            if not line or ";" not in line:
                if "Mutual Fund" in line:
                    curr_amc = line.strip()
                    continue
                parts = line.split(";")
                if len(parts) < 6 or parts[0] == "Scheme Code": continue
                name = parts[3].strip()
                name_upper = name.upper()
                if ("REGULAR" in name_upper or "RETAIL" in name_upper) and "DIRECT" not in name_upper:
                    try:
                        nav = float(parts[4]) if parts[4] not in ("N.A.", "") else 0.0
                    except ValueError:
                        nav = 0.0
                    schemes.append((parts[0].strip(), curr_amc, get_rta(curr_amc), name, "Auto", nav, parts[5].strip()))
        if schemes:
            with get_conn() as conn:
                conn.executemany("INSERT OR REPLACE INTO amc_schemes VALUES (?,?,?,?,?,?,?)", schemes)
                conn.execute(
                    "INSERT OR IGNORE INTO amc_config (amc, rta, is_enabled) SELECT DISTINCT amc, rta, 1 FROM amc_schemes WHERE amc IS NOT NULL")
            result_bucket.append(("ok", f"Synced {len(schemes):,} schemes"))
        else:
            result_bucket.append(("error", "Parsed 0 schemes — check AMFI URL"))
    except Exception as exc:
        log.exception("AMFI sync failed")
        result_bucket.append(("error", str(exc)))


def start_amfi_sync() -> None:
    if st.session_state.get("amfi_sync_started"): return
    st.session_state["amfi_sync_started"] = True
    st.session_state["amfi_result"] = []
    bucket = st.session_state["amfi_result"]
    t = threading.Thread(target=_amfi_sync_worker, args=(bucket,), daemon=True)
    add_script_run_ctx(t)
    t.start()


# ==================== COLUMN NORMALISATION ====================
def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    df.columns = (df.columns.str.strip().str.lower().str.replace(r"[\s.\-_]+", "_", regex=True))
    return df


# ==================== IMPORT FUNCTIONS ====================
def parse_client_master(file, replace: bool) -> tuple[bool, str]:
    file.seek(0)
    try:
        df = pd.read_excel(file)
    except Exception as exc:
        return False, f"File read error: {exc}"
    df = normalize_columns(df)

    def _col(*candidates):
        return next((c for c in candidates if c in df.columns), None)

    code_col, fname_col = _col("client_code", "member_code"), _col("primary_holder_first_name")
    lname_col, pan_col = _col("primary_holder_last_name"), _col("primary_holder_pan")
    mobile_col, email_col = _col("indian_mobile_no_"), _col("email", "primary_holder_email")
    date_col = _col("created_at")
    if not code_col or not fname_col: return False, "Missing critical columns: client_code / primary_holder_first_name"
    rows = []
    for _, row in df.iterrows():
        code = clean_str(row.get(code_col))
        if not code: continue
        fn, ln = clean_str(row.get(fname_col, "")), clean_str(row.get(lname_col, "")) if lname_col else ""
        name = f"{fn} {ln}".strip() or "Unknown"
        pan = clean_str(row.get(pan_col, "")).upper()
        mob, mail = clean_str(row.get(mobile_col, "")), clean_str(row.get(email_col, "")).lower()
        dt = parse_date_safe(row.get(date_col)) or datetime.now().strftime("%Y-%m-%d")
        rows.append((code, name, pan, mob, mail, "Verified", dt))
    if not rows: return False, "No valid rows found"
    inserted = skipped = 0
    with get_conn() as conn:
        if replace:
            conn.execute("DELETE FROM clients")
            conn.executemany(
                "INSERT INTO clients (client_code, name, pan, mobile, email, kyc_status, start_date) VALUES (?,?,?,?,?,?,?)",
                rows)
            inserted = len(rows)
        else:
            existing = {r[0] for r in conn.execute("SELECT client_code FROM clients").fetchall()}
            new_rows = [r for r in rows if r[0] not in existing]
            skipped = len(rows) - len(new_rows)
            if new_rows:
                conn.executemany(
                    "INSERT OR IGNORE INTO clients (client_code, name, pan, mobile, email, kyc_status, start_date) VALUES (?,?,?,?,?,?,?)",
                    new_rows)
            inserted = len(new_rows)
    msg = f"Imported {inserted} clients"
    if skipped: msg += f" | Skipped {skipped} already existing"
    return True, msg


def parse_sip_report(file, replace: bool) -> tuple[bool, str, dict]:
    file.seek(0)
    try:
        df = pd.read_excel(file)
    except Exception as exc:
        return False, f"File read error: {exc}", {}
    df = normalize_columns(df)
    required = ["client_code", "folio_no", "scheme_name", "amc_name"]
    missing = [c for c in required if c not in df.columns]
    if missing: return False, f"Missing columns: {missing}", {}
    preview = {}
    if "status" in df.columns:
        active_count = df["status"].apply(is_active_status).sum()
        preview["active"], preview["cancelled"] = int(active_count), len(df) - int(active_count)
    if "first_order" in df.columns: preview["first_order"] = int(
        (df["first_order"].astype(str).str.upper() == "Y").sum())
    rows, skipped = [], 0
    for _, row in df.iterrows():
        if not is_active_status(row.get("status", "ACTIVE")):
            skipped += 1;
            continue
        try:
            fo = clean_str(row.get("first_order", "N")).upper()
            if fo not in ("Y", "N"): fo = "N"
            sip_day = 1
            sd = parse_date_safe(row.get("start_date"))
            if sd:
                try:
                    sip_day = pd.to_datetime(sd).day
                except Exception:
                    pass
            amc_raw = clean_str(row["amc_name"])
            rows.append((
                clean_str(row["client_code"]), clean_str(row["folio_no"]), clean_str(row.get("rta_scheme_code", "")),
                clean_str(row["scheme_name"]), amc_raw, get_rta(amc_raw), clean_str(row.get("frequency_type", "")),
                float(row.get("installments_amt") or 0), sip_day, sd, parse_date_safe(row.get("end_date")), "Active",
                fo,
            ))
        except Exception as exc:
            log.warning("Skipped SIP row: %s", exc);
            skipped += 1
    if not rows: return False, "0 SIPs imported (all skipped or invalid)", preview
    inserted = duplicate_skipped = 0
    with get_conn() as conn:
        if replace:
            conn.execute("DELETE FROM holdings")
            conn.executemany(
                "INSERT INTO holdings (client_code, folio_no, scheme_code, scheme_name, amc, rta, investment_type, sip_amount, sip_day, start_date, end_date, status, first_order) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows)
            inserted = len(rows)
        else:
            existing = set(conn.execute(
                "SELECT LOWER(TRIM(folio_no)), LOWER(TRIM(scheme_code)), start_date FROM holdings").fetchall())
            new_rows = []
            for r in rows:
                key = (normalize_folio(r[1]), str(r[2]).strip().lower(), r[9])
                if key in existing:
                    duplicate_skipped += 1
                else:
                    new_rows.append(r);
                    existing.add(key)
            if new_rows:
                conn.executemany(
                    "INSERT INTO holdings (client_code, folio_no, scheme_code, scheme_name, amc, rta, investment_type, sip_amount, sip_day, start_date, end_date, status, first_order) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    new_rows)
            inserted = len(new_rows)
    msg = f"Imported {inserted} Active SIPs"
    if skipped: msg += f" | Skipped {skipped} cancelled/invalid"
    if duplicate_skipped: msg += f" | Skipped {duplicate_skipped} already existing"
    return True, msg, preview


def parse_cams_brokerage(file, replace: bool) -> tuple[bool, str, dict]:
    df = None;
    last_err = ""
    for sep in ("\t", ","):
        try:
            file.seek(0)
            _df = pd.read_csv(file, sep=sep, quotechar="'", dtype=str, encoding="utf-8", encoding_errors="replace")
            if len(_df.columns) > 5: df = _df; break
        except Exception as exc:
            last_err = str(exc); continue
    if df is None: return False, f"Could not parse file — {last_err}", {}
    df.columns = [col.strip().replace('\u200b', '').replace('\ufeff', '').replace('\u00a0', '').strip('\'"').upper() for
                  col in df.columns]
    required = ["FOLIO_NO", "BRKAGE_AMT", "AMC_CODE"]
    missing = [c for c in required if c not in df.columns]
    if missing: return False, f"Missing columns: {missing}. Found: {list(df.columns)[:15]}", {}
    batch_id = f"{file.name}_{datetime.now().strftime('%Y%m%d%H%M%S')}"

    def _parse_accrual_month(val) -> str:
        try:
            dt = pd.to_datetime(val, errors="coerce"); return dt.strftime("%Y-%m") if pd.notna(dt) else ""
        except Exception:
            return ""

    def _safe_float(val) -> float:
        try:
            return float(str(val).replace(",", "").strip()) if val not in ("", None) else 0.0
        except (ValueError, TypeError):
            return 0.0

    rows, skipped = [], 0
    for _, row in df.iterrows():
        try:
            rows.append((
                clean_str(row.get("AMC_CODE", "")), clean_str(row.get("FOLIO_NO", "")),
                clean_str(row.get("SCHEME_CODE", row.get("TXN_SCH_CODE", ""))),
                clean_str(row.get("TRXN_NO", "")), clean_str(row.get("TRXN_TYPE", "")),
                _safe_float(row.get("BRKAGE_AMT", 0)),
                clean_str(row.get("BRKAGE_TYPE", "")), _safe_float(row.get("BRKAGE_RATE", 0)),
                clean_str(row.get("INV_NAME", "")).strip(),
                parse_date_safe(row.get("PROC_DATE", "")), _parse_accrual_month(row.get("BROKERAGE_ACRUAL_MONTH", "")),
                _safe_float(row.get("PLOT_AMOUNT", 0)), _safe_float(row.get("AVG_ASSETS", 0)),
                _safe_float(row.get("IGST_VALUE", 0)), _safe_float(row.get("CGST_VALUE", 0)),
                _safe_float(row.get("SGST_VALUE", 0)), batch_id,
            ))
        except Exception as exc:
            log.warning("Skipped CAMS row: %s", exc); skipped += 1
    if not rows: return False, "0 rows imported (all skipped or invalid)", {}
    inserted = duplicate_skipped = 0
    with get_conn() as conn:
        if replace:
            conn.execute("DELETE FROM cams_brokerage")
            conn.executemany(
                "INSERT OR IGNORE INTO cams_brokerage (amc_code, folio_no, scheme_code, trxn_no, trxn_type, brkage_amt, brkage_type, brkage_rate, inv_name, proc_date, accrual_month, plot_amount, avg_assets, igst_value, cgst_value, sgst_value, upload_batch) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows)
            inserted = len(rows)
        else:
            before = conn.execute("SELECT COUNT(*) FROM cams_brokerage").fetchone()[0]
            conn.executemany(
                "INSERT OR IGNORE INTO cams_brokerage (amc_code, folio_no, scheme_code, trxn_no, trxn_type, brkage_amt, brkage_type, brkage_rate, inv_name, proc_date, accrual_month, plot_amount, avg_assets, igst_value, cgst_value, sgst_value, upload_batch) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows)
            after = conn.execute("SELECT COUNT(*) FROM cams_brokerage").fetchone()[0]
            inserted, duplicate_skipped = after - before, len(rows) - inserted
    total_amount = sum(r[5] for r in rows)
    preview = {"rows": inserted, "skipped": skipped, "duplicates": duplicate_skipped, "total_brokerage": total_amount,
               "months": sorted({r[10] for r in rows if r[10]})}
    msg = f"Imported {inserted} CAMS rows | Rs {total_amount:,.2f} brokerage"
    if skipped: msg += f" | Skipped {skipped} invalid"
    if duplicate_skipped: msg += f" | Skipped {duplicate_skipped} duplicates"
    return True, msg, preview


def parse_kfintech_brokerage(file, replace: bool) -> tuple[bool, str, dict]:
    df = None;
    last_err = ""
    for sep in ("\t", ","):
        try:
            file.seek(0)
            _df = pd.read_csv(file, sep=sep, quotechar="'", dtype=str, encoding="utf-8", encoding_errors="replace")
            if len(_df.columns) > 5: df = _df; break
        except Exception as exc:
            last_err = str(exc); continue
    if df is None: return False, f"Could not parse file — {last_err}", {}
    df.columns = [col.strip().replace('\u200b', '').replace('\ufeff', '').replace('\u00a0', '').strip('\'"').upper() for
                  col in df.columns]

    col_mappings = {
        'amc_code': ['FUND', 'AMC CODE', 'AMC_CODE'],
        'folio_no': ['ACCOUNT NUMBER', 'FOLIO NUMBER', 'FOLIO_NO', 'FOLIO'],
        'scheme_code': ['SCHEME CODE', 'SCHEME_CODE', 'SCH CODE'],
        'trxn_no': ['TRANSACTION NUMBER', 'TRXN_NO', 'TXN NO'],
        'trxn_type': ['TRANSACTION DESCRIPTION', 'TRXN_TYPE', 'TRANTYPECODE'],
        'brkage_amt': ['BROKERAGE (IN RS.)', 'BROKERAGE', 'BRKAGE_AMT', 'GROSSBROKERAGE'],
        'brkage_type': ['BROKERAGE TYPE', 'BRKAGE_TYPE'],
        'brkage_rate': ['PERCENTAGE (%)', 'PERCENTAGE', 'BRKAGE_RATE'],
        'inv_name': ['INVESTOR NAME', 'INV_NAME', 'NAME'],
        'proc_date': ['PROCESS DATE', 'PROC_DATE'],
        'accrual_month': ['FROM DATE', 'TO DATE', 'STARTING DATE', 'ENDING DATE'],
        'plot_amount': ['AMOUNT (IN RS.)', 'AMOUNT', 'PLOT_AMOUNT'],
        'avg_assets': ['AVERAGE ASSETS', 'AVG_ASSETS'],
    }

    def _find_col(candidates):
        for c in candidates:
            matches = [col for col in df.columns if col.upper().replace(' ', '_') == c.upper().replace(' ', '_')]
            if matches: return matches[0]
            for col in df.columns:
                if c.upper() in col.upper() or col.upper() in c.upper(): return col
        return None

    resolved = {key: _find_col(candidates) for key, candidates in col_mappings.items()}
    required = ['folio_no', 'brkage_amt', 'amc_code']
    missing = [c for c in required if resolved[c] is None]
    if missing: return False, f"Missing columns: {missing}. Found: {list(df.columns)[:15]}", {}

    batch_id = f"KF_{file.name}_{datetime.now().strftime('%Y%m%d%H%M%S')}"

    def _parse_accrual_month(val) -> str:
        try:
            dt = pd.to_datetime(val, errors="coerce", dayfirst=True); return dt.strftime("%Y-%m") if pd.notna(
                dt) else ""
        except Exception:
            return ""

    def _safe_float(val) -> float:
        try:
            return float(str(val).replace(",", "").strip()) if val not in ("", None) else 0.0
        except (ValueError, TypeError):
            return 0.0

    rows, skipped = [], 0
    for _, row in df.iterrows():
        try:
            rows.append((
                clean_str(row.get(resolved['amc_code'], "")), clean_str(row.get(resolved['folio_no'], "")),
                clean_str(row.get(resolved['scheme_code'], "")), clean_str(row.get(resolved['trxn_no'], "")),
                clean_str(row.get(resolved['trxn_type'], "")), _safe_float(row.get(resolved['brkage_amt'], 0)),
                clean_str(row.get(resolved['brkage_type'], "")), _safe_float(row.get(resolved['brkage_rate'], 0)),
                clean_str(row.get(resolved['inv_name'], "")).strip(),
                parse_date_safe(row.get(resolved['proc_date'], "")),
                _parse_accrual_month(row.get(resolved['accrual_month'], "")),
                _safe_float(row.get(resolved['plot_amount'], 0)), _safe_float(row.get(resolved['avg_assets'], 0)),
                0.0, 0.0, 0.0, batch_id,
            ))
        except Exception as exc:
            log.warning("Skipped KFinTech row: %s", exc); skipped += 1
    if not rows: return False, "0 rows imported (all skipped or invalid)", {}

    inserted = duplicate_skipped = 0
    with get_conn() as conn:
        if replace:
            conn.execute("DELETE FROM kfintech_brokerage")
            conn.executemany(
                "INSERT OR IGNORE INTO kfintech_brokerage (amc_code, folio_no, scheme_code, trxn_no, trxn_type, brkage_amt, brkage_type, brkage_rate, inv_name, proc_date, accrual_month, plot_amount, avg_assets, igst_value, cgst_value, sgst_value, upload_batch) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows)
            inserted = len(rows)
        else:
            before = conn.execute("SELECT COUNT(*) FROM kfintech_brokerage").fetchone()[0]
            conn.executemany(
                "INSERT OR IGNORE INTO kfintech_brokerage (amc_code, folio_no, scheme_code, trxn_no, trxn_type, brkage_amt, brkage_type, brkage_rate, inv_name, proc_date, accrual_month, plot_amount, avg_assets, igst_value, cgst_value, sgst_value, upload_batch) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows)
            after = conn.execute("SELECT COUNT(*) FROM kfintech_brokerage").fetchone()[0]
            inserted, duplicate_skipped = after - before, len(rows) - inserted

    total_amount = sum(r[5] for r in rows)
    preview = {"rows": inserted, "skipped": skipped, "duplicates": duplicate_skipped, "total_brokerage": total_amount,
               "months": sorted({r[10] for r in rows if r[10]})}
    msg = f"Imported {inserted} KFinTech rows | Rs {total_amount:,.2f} brokerage"
    if skipped: msg += f" | Skipped {skipped} invalid"
    if duplicate_skipped: msg += f" | Skipped {duplicate_skipped} duplicates"
    return True, msg, preview


def parse_cams_aum(file, replace: bool) -> tuple[bool, str, dict]:
    df = None;
    last_err = ""
    for sep in ("\t", ","):
        try:
            file.seek(0)
            _df = pd.read_csv(file, sep=sep, quotechar="'", dtype=str, encoding="utf-8", encoding_errors="replace")
            if len(_df.columns) > 5: df = _df; break
        except Exception as exc:
            last_err = str(exc); continue
    if df is None: return False, f"Could not parse AUM file — {last_err}", {}
    df.columns = [col.strip().replace('\u200b', '').replace('\ufeff', '').replace('\u00a0', '').strip('\'"').upper() for
                  col in df.columns]
    required = ["FOLIOCHK", "RUPEE_BAL"]
    missing = [c for c in required if c not in df.columns]
    if missing: return False, f"Missing AUM columns: {missing}. Found: {list(df.columns)[:15]}", {}
    batch_id = f"{file.name}_{datetime.now().strftime('%Y%m%d%H%M%S')}"

    def _safe_float(val) -> float:
        try:
            return float(str(val).replace(",", "").strip()) if val not in ("", None) else 0.0
        except (ValueError, TypeError):
            return 0.0

    rows, skipped = [], 0
    for _, row in df.iterrows():
        folio = clean_str(row.get("FOLIOCHK", ""))
        if not folio: skipped += 1; continue
        try:
            rows.append((
                folio, clean_str(row.get("INV_NAME", "")).strip(), clean_str(row.get("SCH_NAME", "")),
                clean_str(row.get("AMC_CODE", "")), clean_str(row.get("PAN_NO", "")).upper(),
                clean_str(row.get("EMAIL", "")).lower(), parse_date_safe(row.get("REP_DATE", "")),
                _safe_float(row.get("CLOS_BAL", 0)), _safe_float(row.get("RUPEE_BAL", 0)), batch_id,
            ))
        except Exception as exc:
            log.warning("Skipped AUM row: %s", exc); skipped += 1
    if not rows: return False, "0 AUM rows imported", {}
    inserted = duplicate_skipped = 0
    with get_conn() as conn:
        if replace:
            conn.execute("DELETE FROM cams_aum")
            conn.executemany(
                "INSERT OR IGNORE INTO cams_aum (folio_no, inv_name, scheme_name, amc_code, pan_no, email, rep_date, units, rupee_bal, upload_batch) VALUES (?,?,?,?,?,?,?,?,?,?)",
                rows)
            inserted = len(rows)
        else:
            before = conn.execute("SELECT COUNT(*) FROM cams_aum").fetchone()[0]
            conn.executemany(
                "INSERT OR IGNORE INTO cams_aum (folio_no, inv_name, scheme_name, amc_code, pan_no, email, rep_date, units, rupee_bal, upload_batch) VALUES (?,?,?,?,?,?,?,?,?,?)",
                rows)
            after = conn.execute("SELECT COUNT(*) FROM cams_aum").fetchone()[0]
            inserted, duplicate_skipped = after - before, len(rows) - inserted
    total_aum = sum(r[8] for r in rows)
    preview = {"rows": inserted, "skipped": skipped, "duplicates": duplicate_skipped, "total_aum": total_aum}
    msg = f"Imported {inserted} AUM rows | Total AUM: {format_aum(total_aum)}"
    if skipped: msg += f" | Skipped {skipped}"
    if duplicate_skipped: msg += f" | Skipped {duplicate_skipped} duplicates"
    return True, msg, preview


def parse_kfintech_aum(file, replace: bool) -> tuple[bool, str, dict]:
    df = None;
    last_err = ""
    for sep in ("\t", ","):
        try:
            file.seek(0)
            _df = pd.read_csv(file, sep=sep, dtype=str, encoding="utf-8", encoding_errors="replace")
            if len(_df.columns) > 5: df = _df; break
        except Exception as exc:
            last_err = str(exc); continue
    if df is None: return False, f"Could not parse KFinTech AUM file — {last_err}", {}
    df.columns = [
        col.strip().replace('\u200b', '').replace('\ufeff', '').replace('\u00a0', '').strip('\'"').upper().replace('#',
                                                                                                                   '').replace(
            '  ', ' ').strip() for col in df.columns]
    col_mappings = {
        'folio_no': ['FOLIO NUMBER', 'FOLIO_NO', 'FOLIO', 'ACCOUNT NUMBER'],
        'inv_name': ['INVESTOR NAME', 'INV_NAME', 'CLIENT NAME', 'NAME'],
        'scheme_name': ['FUND DESCRIPTION', 'SCHEME NAME', 'DESCRIPTION'],
        'amc_code': ['FUND', 'AMC CODE', 'AMC_CODE', 'FUND CODE'],
        'product_code': ['PRODUCT CODE', 'PRODUCT_CODE', 'PROD CODE'],
        'scheme_code': ['SCHEME CODE', 'SCHEME_CODE', 'SCH CODE'],
        'dividend_opt': ['DIVIDEND OPTION', 'DIVIDEND_OPTION', 'DIV OPT', 'OPTION'],
        'email': ['EMAIL', 'E-MAIL', 'EMAIL ID'],
        'rep_date': ['REPORT DATE', 'REPORT_DATE', 'AS ON DATE', 'DATE'],
        'units': ['BALANCE', 'UNITS', 'CLOS_BAL', 'CLOSING BALANCE'],
        'rupee_bal': ['AUM', 'RUPEE_BAL', 'CURRENT VALUE', 'MARKET VALUE', 'VALUE'],
        'nav': ['NAV', 'NET ASSET VALUE'],
    }

    def _find_col(candidates):
        for c in candidates:
            matches = [col for col in df.columns if col.upper().replace(' ', '_') == c.upper().replace(' ', '_')]
            if matches: return matches[0]
            for col in df.columns:
                if c.upper() in col.upper() or col.upper() in c.upper(): return col
        return None

    resolved = {key: _find_col(candidates) for key, candidates in col_mappings.items()}
    required = ['folio_no', 'rupee_bal']
    missing = [c for c in required if resolved[c] is None]
    if missing: return False, f"Missing required columns: {missing}. Found: {list(df.columns)[:20]}. Resolved mapping: {resolved}", {}
    batch_id = f"KF_{file.name}_{datetime.now().strftime('%Y%m%d%H%M%S')}"

    def _safe_float(val) -> float:
        try:
            if val is None or pd.isna(val): return 0.0
            return float(str(val).replace(",", "").strip()) if str(val).strip() not in (
            "", "None", "NaN", "NULL") else 0.0
        except (ValueError, TypeError):
            return 0.0

    rows, skipped = [], 0
    for _, row in df.iterrows():
        folio = clean_str(row.get(resolved['folio_no'], ""))
        if not folio: skipped += 1; continue
        try:
            aum_val = _safe_float(row.get(resolved['rupee_bal'], 0)) if resolved['rupee_bal'] else 0.0
            if aum_val == 0 and resolved['nav'] and resolved['units']:
                nav_val, units_val = _safe_float(row.get(resolved['nav'], 0)), _safe_float(
                    row.get(resolved['units'], 0))
                aum_val = nav_val * units_val
            rows.append((
                folio, clean_str(row.get(resolved['inv_name'], "")).strip(),
                clean_str(row.get(resolved['scheme_name'], "")),
                clean_str(row.get(resolved['amc_code'], "")), clean_str(row.get(resolved['product_code'], "")),
                clean_str(row.get(resolved['scheme_code'], "")), clean_str(row.get(resolved['dividend_opt'], "")),
                clean_str(row.get(resolved['email'], "")).lower(), parse_date_safe(row.get(resolved['rep_date'], "")),
                _safe_float(row.get(resolved['units'], 0)) if resolved['units'] else 0.0, aum_val,
                _safe_float(row.get(resolved['nav'], 0)) if resolved['nav'] else 0.0, aum_val, batch_id,
            ))
        except Exception as exc:
            log.warning("Skipped KFinTech AUM row: %s", exc); skipped += 1
    if not rows: return False, "0 KFinTech AUM rows imported (all skipped or invalid)", {}
    inserted = duplicate_skipped = 0
    with get_conn() as conn:
        if replace:
            conn.execute("DELETE FROM kfintech_aum")
            conn.executemany(
                "INSERT OR IGNORE INTO kfintech_aum (folio_no, inv_name, scheme_name, amc_code, product_code, scheme_code, dividend_opt, email, rep_date, units, rupee_bal, nav, aum, upload_batch) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows)
            inserted = len(rows)
        else:
            before = conn.execute("SELECT COUNT(*) FROM kfintech_aum").fetchone()[0]
            conn.executemany(
                "INSERT OR IGNORE INTO kfintech_aum (folio_no, inv_name, scheme_name, amc_code, product_code, scheme_code, dividend_opt, email, rep_date, units, rupee_bal, nav, aum, upload_batch) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows)
            after = conn.execute("SELECT COUNT(*) FROM kfintech_aum").fetchone()[0]
            inserted, duplicate_skipped = after - before, len(rows) - inserted
    total_aum = sum(r[10] for r in rows)
    preview = {"rows": inserted, "skipped": skipped, "duplicates": duplicate_skipped, "total_aum": total_aum}
    msg = f"Imported {inserted} KFinTech AUM rows | Total AUM: {format_aum(total_aum)}"
    if skipped: msg += f" | Skipped {skipped}"
    if duplicate_skipped: msg += f" | Skipped {duplicate_skipped} duplicates"
    return True, msg, preview


# ==================== AUM DATA LOADERS ====================
@st.cache_data(ttl=60, show_spinner=False)
def load_total_aum() -> float:
    with get_conn() as conn:
        result = conn.execute("SELECT COALESCE(SUM(rupee_bal), 0) FROM cams_aum").fetchone()
        return float(result[0]) if result else 0.0


@st.cache_data(ttl=60, show_spinner=False)
def load_total_kfintech_aum() -> float:
    with get_conn() as conn:
        result = conn.execute("SELECT COALESCE(SUM(rupee_bal), 0) FROM kfintech_aum").fetchone()
        return float(result[0]) if result else 0.0


@st.cache_data(ttl=60, show_spinner=False)
def load_combined_total_aum() -> dict:
    cams, kfin = load_total_aum(), load_total_kfintech_aum()
    return {"cams": cams, "kfintech": kfin, "total": cams + kfin}


@st.cache_data(ttl=60, show_spinner=False)
def load_client_aum(client_code: str) -> pd.DataFrame:
    with get_conn() as conn:
        client_folios = conn.execute("SELECT folio_no FROM holdings WHERE client_code = ?", (client_code,)).fetchall()
        pan_row = conn.execute("SELECT pan FROM clients WHERE client_code = ?", (client_code,)).fetchone()
        client_pan = pan_row[0].strip().upper() if pan_row and pan_row[0] else ""
        aum_df = pd.read_sql(
            "SELECT folio_no, inv_name, scheme_name, amc_code, rupee_bal, units, rep_date, pan_no FROM cams_aum", conn)
        if aum_df.empty: return pd.DataFrame()
        aum_df["folio_base"] = aum_df["folio_no"].apply(normalize_folio)
        aum_df["pan_clean"] = aum_df["pan_no"].str.strip().str.upper().fillna("")
        folio_bases = {normalize_folio(f[0]) for f in client_folios if f[0]}
        folio_matched = aum_df[aum_df["folio_base"].isin(folio_bases)].copy()
        folio_matched["match_type"] = "SIP Folio"
        pan_matched = pd.DataFrame()
        if client_pan:
            already_folios = set(folio_matched["folio_no"].str.strip().str.lower())
            pan_rows = aum_df[(aum_df["pan_clean"] == client_pan) & (
                ~aum_df["folio_no"].str.strip().str.lower().isin(already_folios))].copy()
            pan_rows["match_type"] = "Lumpsum (PAN)"
            pan_matched = pan_rows
        result = pd.concat([folio_matched, pan_matched], ignore_index=True)
        return result.drop(columns=["folio_base", "pan_clean", "pan_no"], errors="ignore")


@st.cache_data(ttl=60, show_spinner=False)
def load_client_kfintech_aum(client_code: str) -> pd.DataFrame:
    with get_conn() as conn:
        client_folios = conn.execute("SELECT folio_no FROM holdings WHERE client_code = ?", (client_code,)).fetchall()
        client_row = conn.execute("SELECT pan, email FROM clients WHERE client_code = ?", (client_code,)).fetchone()
        client_email = client_row[1].strip().lower() if client_row and client_row[1] else ""
        aum_df = pd.read_sql(
            "SELECT folio_no, inv_name, scheme_name, amc_code, product_code, scheme_code, dividend_opt, rupee_bal, units, nav, rep_date, email FROM kfintech_aum",
            conn)
        if aum_df.empty: return pd.DataFrame()
        aum_df["folio_base"] = aum_df["folio_no"].apply(normalize_folio)
        aum_df["email_clean"] = aum_df["email"].str.strip().str.lower().fillna("")
        folio_bases = {normalize_folio(f[0]) for f in client_folios if f[0]}
        folio_matched = aum_df[aum_df["folio_base"].isin(folio_bases)].copy()
        folio_matched["match_type"] = "SIP Folio (KFinTech)"
        email_matched = pd.DataFrame()
        if client_email:
            already_folios = set(folio_matched["folio_no"].str.strip().str.lower())
            email_rows = aum_df[(aum_df["email_clean"] == client_email) & (
                ~aum_df["folio_no"].str.strip().str.lower().isin(already_folios))].copy()
            if not email_rows.empty: email_rows["match_type"] = "Lumpsum (Email) (KFinTech)"; email_matched = email_rows
        result = pd.concat([folio_matched, email_matched], ignore_index=True)
        return result.drop(columns=["folio_base", "email_clean"], errors="ignore")


@st.cache_data(ttl=60, show_spinner=False)
def load_combined_client_aum(client_code: str) -> pd.DataFrame:
    cams_df, kfin_df = load_client_aum(client_code), load_client_kfintech_aum(client_code)
    if not cams_df.empty: cams_df["rta_source"] = "CAMS"
    if not kfin_df.empty: kfin_df["rta_source"] = "KFinTech"
    all_cols = list(set(list(cams_df.columns) + list(kfin_df.columns)))
    for col in all_cols:
        if col not in cams_df.columns: cams_df[col] = None
        if col not in kfin_df.columns: kfin_df[col] = None
    return pd.concat([cams_df, kfin_df], ignore_index=True) if not (cams_df.empty and kfin_df.empty) else pd.DataFrame()


# ==================== BROKERAGE DATA LOADERS ====================
@st.cache_data(ttl=60, show_spinner=False)
def load_cams_by_amc(month_str: str) -> pd.DataFrame:
    with get_conn() as conn:
        return pd.read_sql("""
            SELECT cb.amc_code, COALESCE(m.amc_name, h.amc) AS amc_name, cb.accrual_month,
            SUM(cb.brkage_amt) AS cams_brokerage, COUNT(DISTINCT cb.folio_no) AS folio_count
            FROM cams_brokerage cb
            LEFT JOIN amc_code_map m ON UPPER(TRIM(cb.amc_code)) = UPPER(TRIM(m.amc_code))
            LEFT JOIN holdings h ON TRIM(LOWER(cb.folio_no)) = TRIM(LOWER(h.folio_no))
            WHERE cb.accrual_month = ?
            GROUP BY cb.amc_code, COALESCE(m.amc_name, h.amc), cb.accrual_month
        """, conn, params=(month_str,))


@st.cache_data(ttl=60, show_spinner=False)
def load_kfintech_by_amc(month_str: str) -> pd.DataFrame:
    with get_conn() as conn:
        return pd.read_sql("""
            SELECT kb.amc_code, COALESCE(m.amc_name, h.amc) AS amc_name, kb.accrual_month,
            SUM(kb.brkage_amt) AS kfintech_brokerage, COUNT(DISTINCT kb.folio_no) AS folio_count
            FROM kfintech_brokerage kb
            LEFT JOIN amc_code_map m ON UPPER(TRIM(kb.amc_code)) = UPPER(TRIM(m.amc_code))
            LEFT JOIN holdings h ON TRIM(LOWER(kb.folio_no)) = TRIM(LOWER(h.folio_no))
            WHERE kb.accrual_month = ?
            GROUP BY kb.amc_code, COALESCE(m.amc_name, h.amc), kb.accrual_month
        """, conn, params=(month_str,))


@st.cache_data(ttl=60, show_spinner=False)
def load_cams_by_client(client_code: str) -> pd.DataFrame:
    with get_conn() as conn:
        client_folios = conn.execute("SELECT folio_no FROM holdings WHERE client_code = ?", (client_code,)).fetchall()
        client_folio_bases = {normalize_folio(f[0]) for f in client_folios if f[0]}
        if not client_folio_bases: return pd.DataFrame()
        cams_df = pd.read_sql(
            "SELECT accrual_month, amc_code, folio_no, trxn_type, plot_amount, brkage_amt, brkage_rate, inv_name FROM cams_brokerage",
            conn)
        if cams_df.empty: return pd.DataFrame()
        cams_df["folio_base"] = cams_df["folio_no"].apply(normalize_folio)
        cams_matched = cams_df[cams_df["folio_base"].isin(client_folio_bases)].copy()
        if cams_matched.empty: return pd.DataFrame()
        holdings_df = pd.read_sql("SELECT folio_no, scheme_name, amc, rta FROM holdings", conn)
        holdings_df["folio_base"] = holdings_df["folio_no"].apply(normalize_folio)
        merged = cams_matched.merge(holdings_df[["folio_base", "scheme_name", "amc", "rta"]], on="folio_base",
                                    how="left")
        amc_map = load_amc_code_map()
        merged["amc_name"] = merged["amc_code"].map(amc_map).fillna(merged["amc"])
        result = merged.rename(
            columns={"brkage_amt": "brokerage", "plot_amount": "transaction_amount", "brkage_rate": "rate_pct"})
        expected_cols = ["accrual_month", "amc_name", "scheme_name", "folio_no", "trxn_type", "transaction_amount",
                         "brokerage", "rate_pct", "rta"]
        for col in expected_cols:
            if col not in result.columns: result[col] = None
        return result[expected_cols]


@st.cache_data(ttl=60, show_spinner=False)
def load_kfintech_by_client(client_code: str) -> pd.DataFrame:
    with get_conn() as conn:
        client_folios = conn.execute("SELECT folio_no FROM holdings WHERE client_code = ?", (client_code,)).fetchall()
        client_folio_bases = {normalize_folio(f[0]) for f in client_folios if f[0]}
        if not client_folio_bases: return pd.DataFrame()
        kf_df = pd.read_sql(
            "SELECT accrual_month, amc_code, folio_no, trxn_type, plot_amount, brkage_amt, brkage_rate, inv_name FROM kfintech_brokerage",
            conn)
        if kf_df.empty: return pd.DataFrame()
        kf_df["folio_base"] = kf_df["folio_no"].apply(normalize_folio)
        kf_matched = kf_df[kf_df["folio_base"].isin(client_folio_bases)].copy()
        if kf_matched.empty: return pd.DataFrame()
        holdings_df = pd.read_sql("SELECT folio_no, scheme_name, amc, rta FROM holdings", conn)
        holdings_df["folio_base"] = holdings_df["folio_no"].apply(normalize_folio)
        merged = kf_matched.merge(holdings_df[["folio_base", "scheme_name", "amc", "rta"]], on="folio_base", how="left")
        amc_map = load_amc_code_map()
        merged["amc_name"] = merged["amc_code"].map(amc_map).fillna(merged["amc"])
        result = merged.rename(
            columns={"brkage_amt": "brokerage", "plot_amount": "transaction_amount", "brkage_rate": "rate_pct"})
        expected_cols = ["accrual_month", "amc_name", "scheme_name", "folio_no", "trxn_type", "transaction_amount",
                         "brokerage", "rate_pct", "rta"]
        for col in expected_cols:
            if col not in result.columns: result[col] = None
        return result[expected_cols]


@st.cache_data(ttl=60, show_spinner=False)
def load_cams_months() -> list[str]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT accrual_month FROM cams_brokerage WHERE accrual_month != '' ORDER BY accrual_month DESC").fetchall()
        return [r[0] for r in rows]


@st.cache_data(ttl=60, show_spinner=False)
def load_kfintech_months() -> list[str]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT accrual_month FROM kfintech_brokerage WHERE accrual_month != '' ORDER BY accrual_month DESC").fetchall()
        return [r[0] for r in rows]


@st.cache_data(ttl=60, show_spinner=False)
def load_amc_code_map() -> dict[str, str]:
    with get_conn() as conn:
        rows = conn.execute("SELECT amc_code, amc_name FROM amc_code_map").fetchall()
        return {r[0]: r[1] for r in rows}


@st.cache_data(ttl=60, show_spinner=False)
def load_distinct_cams_amc_codes() -> list[str]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT amc_code FROM cams_brokerage WHERE amc_code != '' ORDER BY amc_code").fetchall()
        kf_rows = conn.execute(
            "SELECT DISTINCT amc_code FROM kfintech_brokerage WHERE amc_code != '' ORDER BY amc_code").fetchall()
        return sorted(list(set([r[0] for r in rows] + [r[0] for r in kf_rows])))


@st.cache_data(ttl=60, show_spinner=False)
def load_active_amcs() -> list[str]:
    with get_conn() as conn:
        return [r[0] for r in conn.execute("SELECT amc FROM amc_config WHERE is_enabled = 1").fetchall() if r[0]]


@st.cache_data(ttl=60, show_spinner=False)
def load_active_holdings() -> pd.DataFrame:
    enabled = load_active_amcs()
    if not enabled: return pd.DataFrame()
    enabled_norm = {normalize_amc(a) for a in enabled}
    with get_conn() as conn:
        df = pd.read_sql("SELECT * FROM holdings WHERE status='Active'", conn)
        if df.empty: return df
        df["norm_amc"] = df["amc"].apply(normalize_amc)
        df = df[df["norm_amc"].isin(enabled_norm)].drop(columns=["norm_amc"]).reset_index(drop=True)
        df["rta"] = df["amc"].apply(get_rta)
        return df


@st.cache_data(ttl=300, show_spinner=False)
def load_clients() -> pd.DataFrame:
    with get_conn() as conn: return pd.read_sql("SELECT client_code, name FROM clients ORDER BY name", conn)


@st.cache_data(ttl=300, show_spinner=False)
def load_clients_full() -> pd.DataFrame:
    with get_conn() as conn: return pd.read_sql(
        "SELECT client_code, name, pan, mobile, email FROM clients ORDER BY name", conn)


@st.cache_data(ttl=60, show_spinner=False)
def load_notifications() -> dict:
    today = datetime.now().date()
    week_ahead = today + timedelta(days=7)
    notes = {"sips_due": [], "unmatched_aum": 0, "pending_brokerage": []}
    with get_conn() as conn:
        holdings = pd.read_sql(
            "SELECT h.client_code, c.name, h.scheme_name, h.sip_day FROM holdings h JOIN clients c ON h.client_code = c.client_code WHERE h.status='Active'",
            conn)
        for _, row in holdings.iterrows():
            nxt = get_next_sip_date(row["sip_day"])
            if nxt == "N/A": continue
            try:
                dt = datetime.strptime(nxt, "%d %b %Y").date()
                if today <= dt <= week_ahead: notes["sips_due"].append(
                    {"client": row["name"], "scheme": row["scheme_name"], "date": nxt})
            except Exception:
                pass
        aum_folios = conn.execute(
            "SELECT COUNT(DISTINCT folio_no) FROM cams_aum WHERE folio_no NOT IN (SELECT folio_no FROM holdings) AND (pan_no IS NULL OR pan_no = '' OR pan_no NOT IN (SELECT pan FROM clients WHERE pan != ''))").fetchone()[
            0]
        notes["unmatched_aum"] = int(aum_folios or 0)
        current_month, current_year = datetime.now().strftime("%b"), datetime.now().year
        active_amc_list = [r[0] for r in
                           conn.execute("SELECT DISTINCT amc FROM holdings WHERE status='Active'").fetchall()]
        entered = {r[0] for r in conn.execute("SELECT amc FROM monthly_brokerage WHERE month=? AND year=?",
                                              (current_month, current_year)).fetchall()}
        notes["pending_brokerage"] = [a for a in active_amc_list if a not in entered]
    return notes


@st.cache_data(ttl=60, show_spinner=False)
def global_search(query: str) -> dict:
    q = query.strip().lower()
    if len(q) < 2: return {}
    results = {}
    with get_conn() as conn:
        clients_df = pd.read_sql(
            "SELECT client_code, name, pan FROM clients WHERE LOWER(name) LIKE ? OR UPPER(pan) LIKE ?", conn,
            params=(f"%{q}%", f"%{query.upper()}%"))
        if not clients_df.empty: results["clients"] = clients_df.to_dict("records")
        folios_df = pd.read_sql(
            "SELECT DISTINCT h.folio_no, h.scheme_name, h.amc, c.name as client_name FROM holdings h LEFT JOIN clients c ON h.client_code = c.client_code WHERE LOWER(h.folio_no) LIKE ? OR LOWER(h.scheme_name) LIKE ?",
            conn, params=(f"%{q}%", f"%{q}%"))
        if not folios_df.empty: results["folios"] = folios_df.to_dict("records")
        aum_df = pd.read_sql(
            "SELECT DISTINCT folio_no, inv_name, scheme_name, pan_no FROM cams_aum WHERE LOWER(folio_no) LIKE ? OR LOWER(scheme_name) LIKE ? OR LOWER(inv_name) LIKE ?",
            conn, params=(f"%{q}%", f"%{q}%", f"%{q}%"))
        if not aum_df.empty: results["aum"] = aum_df.to_dict("records")
    return results


# ==================== APP INIT ====================
st.set_page_config(page_title="MFD Portfolio Intelligence", layout="wide", page_icon="📊")
st.markdown("""
<style>
.stMarkdown, .stMetric, .stDataFrame, .stPlotlyChart, [data-testid="stMetricLabel"], [data-testid="stMetricValue"], .streamlit-expanderContent, .stAlert { color: var(--text-color) !important; }
.dataframe { color: var(--text-color) !important; background: var(--background-color) !important; }
.dataframe th { color: var(--text-color) !important; background: var(--secondary-background-color) !important; }
.aum-card { background: linear-gradient(135deg, #1a472a 0%, #2d6a4f 100%); border-radius: 12px; padding: 20px 24px; margin-bottom: 8px; border: 1px solid rgba(255,255,255,0.1); }
.aum-card .label { font-size: 0.85rem; color: rgba(255,255,255,0.75); margin-bottom: 4px; }
.aum-card .value { font-size: 1.6rem; font-weight: 700; color: #fff; }
.aum-card-kfin { background: linear-gradient(135deg, #1a3a5c 0%, #2d6494 100%); border-radius: 12px; padding: 20px 24px; margin-bottom: 8px; border: 1px solid rgba(255,255,255,0.1); }
.aum-card-kfin .label { font-size: 0.85rem; color: rgba(255,255,255,0.75); margin-bottom: 4px; }
.aum-card-kfin .value { font-size: 1.6rem; font-weight: 700; color: #fff; }
.notif-badge { display:inline-block; background:#ef4444; color:#fff; border-radius:999px; font-size:0.7rem; font-weight:700; padding:1px 7px; margin-left:6px; vertical-align:middle; }
.search-card { background: var(--secondary-background-color); border:1px solid var(--border-color); border-radius:8px; padding:10px 14px; margin-bottom:6px; }
.search-tag { display:inline-block; font-size:0.7rem; font-weight:600; background: var(--accent-color); color: var(--accent-text); border-radius:4px; padding:1px 6px; margin-right:6px; }
</style>
""", unsafe_allow_html=True)

init_db()
start_amfi_sync()
result_bucket = st.session_state.get("amfi_result", [])
if result_bucket:
    status, msg = result_bucket[0]
    st.toast(f"{'✅' if status == 'ok' else '⚠️'} AMFI: {msg}")
    result_bucket.clear()

# ==================== SIDEBAR / NAV ====================
if "dark_mode" not in st.session_state: st.session_state["dark_mode"] = False
dark = st.session_state["dark_mode"]
_bg = "#0e1117" if dark else "#ffffff"
_sbg = "#1a1f2e" if dark else "#f0f2f6"
_text = "#fafafa" if dark else "#1a1a2e"
_muted = "#9aa0b0" if dark else "#6b7280"
_border = "#2d3748" if dark else "#e2e8f0"
_accent = "#4f8ef7" if dark else "#2563eb"

st.markdown(f"""
<style>
.stApp {{ background-color: {_bg} !important; }}
section[data-testid="stSidebar"] {{ background-color: {_sbg} !important; }}
.stMarkdown, p, span, div, label {{ color: {_text} !important; }}
[data-testid="stMetricLabel"], [data-testid="stMetricValue"] {{ color: {_text} !important; }}
.dataframe {{ color: {_text} !important; background: {_sbg} !important; }}
.dataframe th {{ color: {_text} !important; background: {_border} !important; }}
.stAlert {{ color: {_text} !important; }}
.aum-card {{ background: linear-gradient(135deg, #1a472a 0%, #2d6a4f 100%); border-radius: 12px; padding: 20px 24px; margin-bottom: 8px; border: 1px solid rgba(255,255,255,0.1); }}
.aum-card .label {{ font-size:0.85rem; color:rgba(255,255,255,0.75); margin-bottom:4px; }}
.aum-card .value {{ font-size:1.6rem; font-weight:700; color:#fff; }}
.aum-card-kfin {{ background: linear-gradient(135deg, #1a3a5c 0%, #2d6494 100%); border-radius: 12px; padding: 20px 24px; margin-bottom: 8px; border: 1px solid rgba(255,255,255,0.1); }}
.aum-card-kfin .label {{ font-size:0.85rem; color:rgba(255,255,255,0.75); margin-bottom:4px; }}
.aum-card-kfin .value {{ font-size:1.6rem; font-weight:700; color:#fff; }}
.notif-badge {{ display:inline-block; background:#ef4444; color:#fff; border-radius:999px; font-size:0.7rem; font-weight:700; padding:1px 7px; margin-left:6px; vertical-align:middle; }}
.search-card {{ background:{_sbg}; border:1px solid {_border}; border-radius:8px; padding:10px 14px; margin-bottom:6px; }}
.search-tag {{ display:inline-block; font-size:0.7rem; font-weight:600; background:{_accent}22; color:{_accent}; border-radius:4px; padding:1px 6px; margin-right:6px; }}
</style>
""", unsafe_allow_html=True)

with st.sidebar:
    t_col, d_col = st.columns([3, 1])
    t_col.markdown(f"### MFD Intelligence")
    if d_col.button("🌙" if not dark else "☀️", help="Toggle dark/light mode"):
        st.session_state["dark_mode"] = not st.session_state["dark_mode"]
        st.rerun()
    st.divider()
    search_q = st.text_input("🔍 Global Search", placeholder="Client, folio, scheme…", key="global_search_q")
    if search_q and len(search_q.strip()) >= 2:
        with st.spinner("Searching…"):
            sr = global_search(search_q)
            if not sr:
                st.caption("No results found.")
            else:
                if "clients" in sr:
                    st.markdown(f"**👤 Clients** ({len(sr['clients'])})")
                    for r in sr["clients"][:5]: st.markdown(
                        f'<div class="search-card"><span class="search-tag">Client</span><b>{r["name"]}</b><br><small style="color:{_muted}">{r["pan"] or "—"}</small></div>',
                        unsafe_allow_html=True)
                if "folios" in sr:
                    st.markdown(f"**📂 Folios / Schemes** ({len(sr['folios'])})")
                    for r in sr["folios"][:5]: st.markdown(
                        f'<div class="search-card"><span class="search-tag">Folio</span><b>{r["folio_no"]}</b><br><small style="color:{_muted}">{r["scheme_name"][:40]} — {r["client_name"] or "?"}</small></div>',
                        unsafe_allow_html=True)
                if "aum" in sr:
                    st.markdown(f"**📦 AUM Records** ({len(sr['aum'])})")
                    for r in sr["aum"][:4]: st.markdown(
                        f'<div class="search-card"><span class="search-tag">AUM</span><b>{r["inv_name"]}</b><br><small style="color:{_muted}">{r["scheme_name"][:40]}</small></div>',
                        unsafe_allow_html=True)
    st.divider()
    mode = st.radio("Navigate", ["📊 Dashboard", "👤 Client View", "💰 Earnings", "⚙️ Admin Panel"],
                    label_visibility="collapsed")
    st.divider()
    notifs = load_notifications()
    n_sips, n_unmatched, n_pending = len(notifs["sips_due"]), notifs["unmatched_aum"], len(notifs["pending_brokerage"])
    total_n = n_sips + (1 if n_unmatched > 0 else 0) + (1 if n_pending > 0 else 0)
    notif_label = "🔔 Notifications"
    if total_n: notif_label += f' <span class="notif-badge">{total_n}</span>'
    st.markdown(notif_label, unsafe_allow_html=True)
    with st.expander("View Alerts", expanded=(total_n > 0)):
        if n_sips:
            st.markdown(f"**📅 SIPs due in 7 days** ({n_sips})")
            for s in notifs["sips_due"][:10]: st.caption(f"• {s['client']} — {s['date']} — {s['scheme'][:35]}")
        else:
            st.caption("✅ No SIPs due in next 7 days.")
        if n_unmatched:
            st.markdown(f"**⚠️ Unmatched AUM folios:** {n_unmatched}")
            st.caption("Folios in AUM file with no client/holding match.")
        else:
            st.caption("✅ All AUM folios matched.")
        if n_pending:
            st.markdown(f"**⏳ Brokerage not entered this month** ({n_pending})")
            for a in notifs["pending_brokerage"][:8]: st.caption(f"• {a}")
        else:
            st.caption("✅ All AMC brokerage entered for this month.")
    st.divider()
    if st.button("🔄 Refresh Data", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

df_active = load_active_holdings()
active_amcs_raw = load_active_amcs()

# ==================== 📊 DASHBOARD ====================
if mode == "📊 Dashboard":
    st.header("📊 Portfolio Overview")
    rta_filter = st.selectbox("🔍 RTA Filter", ["All", "CAMS", "KFinTech", "Unknown"], key="dash_rta")
    df_filtered = df_active[df_active["rta"] == rta_filter] if rta_filter != "All" else df_active
    with get_conn() as conn:
        total_brokerage = conn.execute("SELECT COALESCE(SUM(amount), 0) FROM monthly_brokerage").fetchone()[0]
        total_clients = conn.execute("SELECT COUNT(*) FROM clients").fetchone()[0]
    aum_data = load_combined_total_aum()
    total_aum, cams_aum, kfin_aum = aum_data["total"], aum_data["cams"], aum_data["kfintech"]
    sip_count = len(df_filtered)
    sip_total = df_filtered["sip_amount"].sum() if not df_filtered.empty else 0
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("👥 Clients", total_clients)
    m2.metric("📈 Active SIPs", sip_count)
    m3.metric("💰 Monthly SIP", format_currency(sip_total, decimals=0))
    m4.metric("🏢 Enabled AMCs", len(active_amcs_raw))
    m5.metric("💵 Brokerage Rcvd.", format_currency(total_brokerage, decimals=0))
    if total_aum > 0:
        aum_col1, aum_col2, aum_col3, aum_col4 = st.columns([1, 1, 1, 1])
        with aum_col1:
            st.markdown(
                f"""<div class="aum-card"><div class="label">📦 Total AUM (All RTAs)</div><div class="value">{format_aum(total_aum)}</div></div>""",
                unsafe_allow_html=True)
        with aum_col2:
            st.markdown(
                f"""<div class="aum-card" style="background: linear-gradient(135deg, #1a3a5c 0%, #2d6494 100%);"><div class="label">📦 CAMS AUM</div><div class="value">{format_aum(cams_aum)}</div></div>""",
                unsafe_allow_html=True)
        with aum_col3:
            st.markdown(
                f"""<div class="aum-card-kfin"><div class="label">📦 KFinTech AUM</div><div class="value">{format_aum(kfin_aum)}</div></div>""",
                unsafe_allow_html=True)
        with aum_col4:
            with get_conn() as conn:
                aum_schemes = conn.execute(
                    "SELECT COUNT(DISTINCT scheme_name) FROM (SELECT scheme_name FROM cams_aum UNION SELECT scheme_name FROM kfintech_aum)").fetchone()[
                    0]
                aum_folios = conn.execute(
                    "SELECT COUNT(DISTINCT folio_no) FROM (SELECT folio_no FROM cams_aum UNION SELECT folio_no FROM kfintech_aum)").fetchone()[
                    0]
            st.markdown(
                f"""<div class="aum-card" style="background: linear-gradient(135deg, #5c1a3a 0%, #942d64 100%);"><div class="label">📂 Schemes × Folios</div><div class="value">{aum_schemes} × {aum_folios}</div></div>""",
                unsafe_allow_html=True)
    else:
        st.info("📦 Upload CAMS or KFinTech AUM Report in Admin Panel → Import RTA Data to see Total AUM.")
    st.divider()
    if df_filtered.empty:
        st.warning(f"📭 No Active Holdings Found for RTA: {rta_filter}")
    else:
        c1, c2 = st.columns(2)
        amc_grp = df_filtered.groupby("amc")["sip_amount"].sum().reset_index()
        with c1:
            fig = px.pie(amc_grp, values="sip_amount", names="amc", hole=0.4,
                         title=f"SIP Distribution by AMC ({rta_filter})")
            st.plotly_chart(fig, use_container_width=True)
        with c2:
            fig = px.bar(amc_grp, x="amc", y="sip_amount", labels={"amc": "AMC", "sip_amount": "Monthly SIP (Rs)"},
                         title=f"Monthly SIP by AMC ({rta_filter})")
            st.plotly_chart(fig, use_container_width=True)
        if total_aum > 0:
            st.subheader("📦 AUM by AMC Code")
            with get_conn() as conn:
                cams_aum_by_amc = pd.read_sql("SELECT amc_code, SUM(rupee_bal) as aum FROM cams_aum GROUP BY amc_code",
                                              conn)
                kfin_aum_by_amc = pd.read_sql(
                    "SELECT amc_code, SUM(rupee_bal) as aum FROM kfintech_aum GROUP BY amc_code", conn)
            cams_aum_by_amc["source"], kfin_aum_by_amc["source"] = "CAMS", "KFinTech"
            combined_aum = pd.concat([cams_aum_by_amc, kfin_aum_by_amc], ignore_index=True)
            if not combined_aum.empty:
                fig_aum = px.bar(combined_aum, x="amc_code", y="aum", color="source",
                                 labels={"amc_code": "AMC Code", "aum": "AUM (Rs)", "source": "RTA"},
                                 title="Total AUM Distribution by AMC & RTA",
                                 color_discrete_map={"CAMS": "#2d6a4f", "KFinTech": "#2d6494"})
                st.plotly_chart(fig_aum, use_container_width=True)
        csv = amc_grp.to_csv(index=False).encode()
        st.download_button("⬇️ Export AMC Summary (CSV)", csv, f"amc_summary_{rta_filter}.csv", "text/csv")

# ==================== 👤 CLIENT VIEW ====================
elif mode == "👤 Client View":
    st.header("👤 Client Portfolio")
    rta_filter = st.selectbox("🔍 RTA Filter", ["All", "CAMS", "KFinTech", "Unknown"], key="client_rta")
    clients = load_clients()
    if clients.empty:
        st.warning("No clients imported.")
    else:
        sel = st.selectbox("🔍 Select Client", clients["name"].tolist(), index=None, placeholder="Search...")
        if sel:
            code = clients.loc[clients["name"] == sel, "client_code"].iloc[0]
            c_holdings = df_active[df_active["client_code"] == code].copy()
            if rta_filter != "All": c_holdings = c_holdings[c_holdings["rta"] == rta_filter]
            client_aum_df = load_combined_client_aum(code)
            total_invested = client_aum_df["rupee_bal"].sum() if not client_aum_df.empty else 0.0
            cams_aum_df, kfin_aum_df = load_client_aum(code), load_client_kfintech_aum(code)
            cams_invested = cams_aum_df["rupee_bal"].sum() if not cams_aum_df.empty else 0.0
            kfin_invested = kfin_aum_df["rupee_bal"].sum() if not kfin_aum_df.empty else 0.0
            has_sips, has_aum = not c_holdings.empty, not client_aum_df.empty
            has_cams, has_kfin = not cams_aum_df.empty, not kfin_aum_df.empty
            has_lumpsum = has_aum and "match_type" in client_aum_df.columns and (
                client_aum_df["match_type"].str.contains("Lumpsum")).any()
            if has_sips:
                c_holdings["next_sip"] = c_holdings["sip_day"].apply(get_next_sip_date)
                next_dates = pd.to_datetime(c_holdings["next_sip"], format="%d %b %Y", errors="coerce")
                next_sip_dt = next_dates.min()
                sip_total_amt = c_holdings["sip_amount"].sum()
                next_sip_str = next_sip_dt.strftime("%d %b %Y") if pd.notna(next_sip_dt) else "N/A"
            else:
                sip_total_amt, next_sip_str = 0.0, "—"
            m1, m2, m3, m4, m5 = st.columns(5)
            m1.metric("📈 Active SIPs", len(c_holdings) if has_sips else 0)
            m2.metric("💰 Monthly SIP", format_currency(sip_total_amt, decimals=0))
            m3.metric("📅 Next SIP", next_sip_str)
            m4.metric("📦 Total Invested", format_aum(total_invested) if total_invested > 0 else "—")
            m5.metric("📦 RTAs", (
                "CAMS+KFin" if (has_cams and has_kfin) else ("CAMS" if has_cams else ("KFin" if has_kfin else "—"))))
            if not has_sips and not has_aum: st.info(f"No active SIPs or AUM data found for **{sel}**.")
            if has_lumpsum and not has_sips:
                st.warning(
                    "⚠️ **Lumpsum-only client** — no active SIPs found. AUM below is matched via PAN/Email from AUM reports.",
                    icon=None)
            elif has_lumpsum:
                st.info(
                    "ℹ️ This client also has lumpsum holdings matched via PAN/Email (shown with **Lumpsum** tag in AUM breakdown).")
            if has_aum:
                with st.expander("📦 AUM Holdings Breakdown", expanded=(not has_sips)):
                    display_cols = ["scheme_name", "amc_code", "folio_no", "units", "rupee_bal", "rep_date",
                                    "match_type", "rta_source"]
                    available_cols = [c for c in display_cols if c in client_aum_df.columns]
                    aum_display = client_aum_df[available_cols].copy()
                    if "rupee_bal" in aum_display.columns: aum_display["rupee_bal"] = aum_display["rupee_bal"].apply(
                        lambda x: format_currency(x, decimals=2))
                    if "units" in aum_display.columns: aum_display["units"] = aum_display["units"].apply(
                        lambda x: f"{float(x):.3f}" if x else "—")
                    col_rename = {"scheme_name": "Scheme", "amc_code": "AMC Code", "folio_no": "Folio",
                                  "units": "Units", "rupee_bal": "Current Value", "rep_date": "As of Date",
                                  "match_type": "Source", "rta_source": "RTA"}
                    aum_display = aum_display.rename(
                        columns={k: v for k, v in col_rename.items() if k in aum_display.columns})
                    st.dataframe(aum_display, use_container_width=True, hide_index=True)
                    if len(client_aum_df) > 1:
                        fig_pie = px.pie(client_aum_df, values="rupee_bal", names="scheme_name", hole=0.4,
                                         title="Portfolio Allocation",
                                         color_discrete_sequence=px.colors.qualitative.Set3)
                        fig_pie.update_layout(height=350)
                        st.plotly_chart(fig_pie, use_container_width=True)
            if has_sips:
                st.subheader("📋 Active SIPs")
                display_df = c_holdings[
                    ["scheme_name", "amc", "rta", "folio_no", "sip_amount", "start_date", "next_sip",
                     "first_order"]].copy()
                display_df["Start Date"] = pd.to_datetime(display_df["start_date"], errors="coerce").dt.strftime(
                    "%d %b %Y")
                display_df["SIP Amt"] = display_df["sip_amount"].apply(lambda x: format_currency(x, decimals=0))
                display_df["First Order"] = display_df["first_order"].apply(
                    lambda x: "Yes" if str(x).upper() == "Y" else "No")
                total_rows = len(display_df)
                page = st.number_input("Page", 1, max(1, -(-total_rows // PAGE_SIZE)), 1)
                start, end = (page - 1) * PAGE_SIZE, page * PAGE_SIZE
                st.caption(f"Showing {start + 1}–{min(end, total_rows)} of {total_rows} SIPs")
                st.dataframe(display_df.iloc[start:end][
                                 ["scheme_name", "amc", "rta", "folio_no", "SIP Amt", "Start Date", "next_sip",
                                  "First Order"]].rename(
                    columns={"scheme_name": "Scheme", "amc": "AMC", "rta": "RTA", "folio_no": "Folio",
                             "next_sip": "Next SIP"}), use_container_width=True, hide_index=True)
                csv = c_holdings.to_csv(index=False).encode()
                st.download_button("⬇️ Export Client Holdings (CSV)", csv, f"{sel}_holdings.csv", "text/csv")

            st.divider()
            st.subheader("💹 Brokerage Generated (from RTA Files)")
            cams_client = load_cams_by_client(code)
            kf_client = load_kfintech_by_client(code)
            all_client_brok = pd.concat([cams_client, kf_client], ignore_index=True) if not (
                        cams_client.empty and kf_client.empty) else pd.DataFrame()

            if all_client_brok.empty:
                st.info("No RTA brokerage data found for this client's folios.")
            else:
                if rta_filter != "All": all_client_brok = all_client_brok[all_client_brok["rta"] == rta_filter]
                if all_client_brok.empty:
                    st.info(f"No {rta_filter} brokerage data found for this client's folios.")
                else:
                    months_avail = sorted(all_client_brok["accrual_month"].unique(), reverse=True)
                    sel_month = st.selectbox("📅 Filter by Accrual Month", ["All"] + months_avail,
                                             key=f"brok_filter_{code}")
                    view_df = all_client_brok if sel_month == "All" else all_client_brok[
                        all_client_brok["accrual_month"] == sel_month]
                    total_brok = view_df["brokerage"].sum()
                    month_grp = view_df.groupby("accrual_month")["brokerage"].sum().reset_index()
                    bm1, bm2 = st.columns(2)
                    bm1.metric("💰 Total Brokerage", format_brokerage(total_brok))
                    bm2.metric("📅 Months Shown", len(month_grp) if sel_month == "All" else 1)
                    disp = view_df[
                        ["accrual_month", "amc_name", "scheme_name", "folio_no", "trxn_type", "transaction_amount",
                         "brokerage", "rate_pct", "rta"]].copy()
                    disp["transaction_amount"] = disp["transaction_amount"].apply(
                        lambda x: format_currency(x, decimals=0))
                    disp["brokerage"] = disp["brokerage"].apply(format_brokerage)
                    disp["rate_pct"] = disp["rate_pct"].apply(lambda x: f"{float(x):.4f}%" if x else "-")
                    disp.columns = ["Month", "AMC", "Scheme", "Folio", "Txn Type", "Txn Amount", "Brokerage", "Rate",
                                    "RTA"]
                    st.dataframe(disp, use_container_width=True, hide_index=True)
                    brok_csv = view_df.to_csv(index=False).encode()
                    st.download_button("⬇️ Export Client Brokerage (CSV)", brok_csv, f"{sel}_brokerage.csv", "text/csv")

# ==================== 💰 EARNINGS ====================
elif mode == "💰 Earnings":
    st.header("💰 Brokerage Earnings")
    rta_filter = st.selectbox("🔍 RTA Filter", ["All", "CAMS", "KFinTech", "Unknown"], key="earnings_rta")
    client_amcs = sorted(df_active["amc"].unique().tolist()) if not df_active.empty else []
    if rta_filter != "All": client_amcs = [amc for amc in client_amcs if
                                           df_active[df_active["amc"] == amc]["rta"].iloc[0] == rta_filter]
    if not client_amcs:
        st.info(f"No active SIPs to record brokerage for ({rta_filter} RTA).")
    else:
        current_month, current_year = datetime.now().strftime("%b"), datetime.now().year
        st.subheader("📝 Record / Update Brokerage")
        with st.container():
            c1, c2, c3 = st.columns(3)
            sel_amc = c1.selectbox("AMC", client_amcs, index=None, placeholder="Choose AMC...")
            month = c2.selectbox("Month", MONTHS, index=MONTHS.index(current_month))
            year = c3.number_input("Year", 2024, 2030, current_year)
            amt = st.number_input("Brokerage Amount (Rs)", min_value=0.0, step=0.01)
            notes = st.text_input("Notes (optional)")
            col_save, col_del = st.columns([1, 1])
            if col_save.button("💾 Save / Update"):
                if not sel_amc:
                    st.error("Select an AMC first.")
                else:
                    with get_conn() as conn:
                        conn.execute(
                            "INSERT INTO monthly_brokerage (amc, month, year, amount, notes) VALUES (?,?,?,?,?) ON CONFLICT(amc, month, year) DO UPDATE SET amount=excluded.amount, notes=excluded.notes",
                            (sel_amc, month, year, amt, notes))
                    st.success(f"Rs {amt:,.2f} saved for {sel_amc} — {month} {year}")
                    st.cache_data.clear();
                    st.rerun()
            if col_del.button("🗑️ Delete Entry"):
                if not sel_amc:
                    st.error("Select an AMC first.")
                else:
                    with get_conn() as conn:
                        conn.execute("DELETE FROM monthly_brokerage WHERE amc=? AND month=? AND year=?",
                                     (sel_amc, month, year))
                    st.success(f"Deleted entry for {sel_amc} — {month} {year}")
                    st.cache_data.clear();
                    st.rerun()
        st.divider()
        st.subheader("📊 Brokerage Reconciliation")
        all_rta_months = sorted(list(set(load_cams_months() + load_kfintech_months())), reverse=True)
        curr_ym = f"{current_year}-{datetime.now().month:02d}"
        if curr_ym not in all_rta_months: all_rta_months.append(curr_ym)
        all_rta_months = sorted(all_rta_months, reverse=True)
        sel_ym = st.selectbox("📅 Select Reconciliation Month", all_rta_months,
                              format_func=lambda x: datetime(*map(int, x.split("-")), 1).strftime(
                                  "%B %Y") if "-" in x else x,
                              index=all_rta_months.index(curr_ym) if curr_ym in all_rta_months else 0)
        sel_year, sel_month_num = map(int, sel_ym.split("-"))
        sel_month_str = datetime(sel_year, sel_month_num, 1).strftime("%b")
        with get_conn() as conn:
            brok_df = pd.read_sql("SELECT amc, amount FROM monthly_brokerage WHERE month=? AND year=?", conn,
                                  params=(sel_month_str, sel_year))
            brok_map = dict(zip(brok_df["amc"], brok_df["amount"])) if not brok_df.empty else {}
        cams_amc_df = load_cams_by_amc(sel_ym)
        cams_map = {}
        if not cams_amc_df.empty:
            for _, r in cams_amc_df.iterrows():
                key = r["amc_name"] or r["amc_code"] or ""
                if key: cams_map[key] = cams_map.get(key, 0.0) + float(r["cams_brokerage"] or 0)
        kfintech_amc_df = load_kfintech_by_amc(sel_ym)
        kfintech_map = {}
        if not kfintech_amc_df.empty:
            for _, r in kfintech_amc_df.iterrows():
                key = r["amc_name"] or r["amc_code"] or ""
                if key: kfintech_map[key] = kfintech_map.get(key, 0.0) + float(r["kfintech_brokerage"] or 0)
        table_data, total_manual, total_cams_sum, total_kf_sum = [], 0.0, 0.0, 0.0
        for amc in client_amcs:
            sip_ct = int((df_active["amc"] == amc).sum())
            sip_vol = float(df_active.loc[df_active["amc"] == amc, "sip_amount"].sum())
            manual = brok_map.get(amc)
            cams_val, kfintech_val = cams_map.get(amc, 0.0), kfintech_map.get(amc, 0.0)
            total_file_val = cams_val + kfintech_val
            if manual is not None:
                status = "✅ Entered"; total_manual += manual
            else:
                manual = 0.0; status = "⏳ Pending"
            total_cams_sum += cams_val;
            total_kf_sum += kfintech_val
            diff = manual - total_file_val if (manual and total_file_val) else None
            diff_str = format_brokerage(diff) if diff is not None else "—"
            match_icon = ("✅" if abs(diff) < 10 else "⚠️") if diff is not None else "—"
            table_data.append({
                "AMC": amc, "SIP Count": sip_ct, "Monthly Volume": format_currency(sip_vol, decimals=0),
                "Manual Entry": format_currency(manual, decimals=2),
                "CAMS File": format_brokerage(cams_val) if cams_val else "—",
                "KFinTech File": format_brokerage(kfintech_val) if kfintech_val else "—",
                "Total File": format_brokerage(total_file_val) if total_file_val else "—",
                "Difference": diff_str, "Match": match_icon, "Status": status,
            })
        st.dataframe(pd.DataFrame(table_data), use_container_width=True, hide_index=True)
        tm1, tm2, tm3 = st.columns(3)
        tm1.metric("💵 Total Manual", format_currency(total_manual, decimals=2))
        tm2.metric("📄 Total RTA File", format_brokerage(total_cams_sum + total_kf_sum))
        tm3.metric("↔️ Difference", format_brokerage(total_manual - (total_cams_sum + total_kf_sum)))

        if all_rta_months:
            st.divider()
            st.subheader("📅 RTA Brokerage — By Accrual Month")
            sel_rta_month = st.selectbox("Accrual Month", all_rta_months, key="earnings_rta_month")
            month_df_cams = load_cams_by_amc(sel_rta_month)
            month_df_kf = load_kfintech_by_amc(sel_rta_month)
            if not month_df_cams.empty and not month_df_kf.empty:
                month_df = pd.merge(month_df_cams, month_df_kf, on=["amc_code", "amc_name", "accrual_month"],
                                    how="outer").fillna(0)
                month_df["total_brokerage"] = month_df["cams_brokerage"] + month_df["kfintech_brokerage"]
            elif not month_df_cams.empty:
                month_df = month_df_cams;
                month_df["kfintech_brokerage"] = 0;
                month_df["total_brokerage"] = month_df["cams_brokerage"]
            elif not month_df_kf.empty:
                month_df = month_df_kf;
                month_df["cams_brokerage"] = 0;
                month_df["total_brokerage"] = month_df["kfintech_brokerage"]
            else:
                month_df = pd.DataFrame()
            if not month_df.empty:
                month_df["cams_brokerage"] = month_df["cams_brokerage"].apply(format_brokerage)
                month_df["kfintech_brokerage"] = month_df["kfintech_brokerage"].apply(format_brokerage)
                month_df["total_brokerage"] = month_df["total_brokerage"].apply(format_brokerage)
                st.dataframe(month_df.rename(
                    columns={"amc_code": "AMC Code", "amc_name": "AMC Name", "cams_brokerage": "CAMS Brokerage",
                             "kfintech_brokerage": "KFinTech Brokerage", "total_brokerage": "Total Brokerage",
                             "folio_count": "Folios", "accrual_month": "Month"}), use_container_width=True,
                             hide_index=True)
        csv = pd.DataFrame(table_data).to_csv(index=False).encode()
        st.download_button("⬇️ Export Earnings (CSV)", csv, f"brokerage_{sel_month_str}_{sel_year}.csv", "text/csv")

        st.divider()
        st.subheader("📈 AMC-wise Brokerage Breakdown (All Time)")
        st.caption("View total brokerage earned per AMC from manual entries AND uploaded files")
        col1, col2, col3 = st.columns(3)
        with col1:
            breakdown_rta = st.selectbox("RTA", ["All", "CAMS", "KFinTech", "Unknown"], key="breakdown_rta")
        with col2:
            breakdown_sort = st.selectbox("Sort By", ["AMC Name", "Manual Total", "File Total", "Difference"],
                                          key="breakdown_sort")
        with col3:
            breakdown_search = st.text_input("🔍 Search AMC", "", key="breakdown_search")
        with get_conn() as conn:
            manual_all = pd.read_sql("SELECT amc, SUM(amount) as total_manual FROM monthly_brokerage GROUP BY amc",
                                     conn)
            manual_map = dict(zip(manual_all["amc"], manual_all["total_manual"])) if not manual_all.empty else {}
            cams_file_all = pd.read_sql("""
                SELECT COALESCE(m.amc_name, h.amc) as amc_name, cb.amc_code, SUM(cb.brkage_amt) as total_file
                FROM cams_brokerage cb LEFT JOIN amc_code_map m ON UPPER(TRIM(cb.amc_code)) = UPPER(TRIM(m.amc_code))
                LEFT JOIN holdings h ON TRIM(LOWER(cb.folio_no)) = TRIM(LOWER(h.folio_no))
                GROUP BY COALESCE(m.amc_name, h.amc), cb.amc_code
            """, conn)
            kf_file_all = pd.read_sql("""
                SELECT COALESCE(m.amc_name, h.amc) as amc_name, kb.amc_code, SUM(kb.brkage_amt) as total_file
                FROM kfintech_brokerage kb LEFT JOIN amc_code_map m ON UPPER(TRIM(kb.amc_code)) = UPPER(TRIM(m.amc_code))
                LEFT JOIN holdings h ON TRIM(LOWER(kb.folio_no)) = TRIM(LOWER(h.folio_no))
                GROUP BY COALESCE(m.amc_name, h.amc), kb.amc_code
            """, conn)
        file_map = {}
        for df_file in [cams_file_all, kf_file_all]:
            if not df_file.empty:
                for _, r in df_file.iterrows():
                    key = r["amc_name"] or r["amc_code"] or ""
                    if key: file_map[key] = file_map.get(key, 0.0) + float(r["total_file"] or 0)
        breakdown_data = []
        for amc in sorted(set(list(manual_map.keys()) + list(file_map.keys()) + client_amcs)):
            if breakdown_rta != "All":
                amc_rta = df_active[df_active["amc"] == amc]["rta"].iloc[0] if amc in df_active[
                    "amc"].values else "Unknown"
                if amc_rta != breakdown_rta: continue
            if breakdown_search and breakdown_search.lower() not in amc.lower(): continue
            manual_val, file_val = manual_map.get(amc, 0.0), file_map.get(amc, 0.0)
            diff = manual_val - file_val
            match_status = "✅ Match" if abs(diff) < 10 else ("⚠️ Gap" if diff != 0 else "—")
            breakdown_data.append({
                "AMC": amc, "RTA": df_active[df_active["amc"] == amc]["rta"].iloc[0] if amc in df_active[
                    "amc"].values else "Unknown",
                "Manual Total (Rs)": manual_val, "File Total (Rs)": file_val, "Difference (Rs)": diff,
                "Match Status": match_status,
                "Months with Manual": manual_all[manual_all["amc"] == amc].shape[0] if not manual_all.empty else 0,
                "Months with File": len([m for m in all_rta_months if amc in (
                    file_all[file_all["amc_name"] == amc]["amc_name"].values if not file_all.empty else [])]),
            })
        breakdown_df = pd.DataFrame(breakdown_data)
        if breakdown_df.empty:
            st.info("No data matches the current filters.")
        else:
            if breakdown_sort == "AMC Name":
                breakdown_df = breakdown_df.sort_values("AMC")
            elif breakdown_sort == "Manual Total":
                breakdown_df = breakdown_df.sort_values("Manual Total (Rs)", ascending=False)
            elif breakdown_sort == "File Total":
                breakdown_df = breakdown_df.sort_values("File Total (Rs)", ascending=False)
            elif breakdown_sort == "Difference":
                breakdown_df = breakdown_df.sort_values("Difference (Rs)", key=abs, ascending=False)
            display_df = breakdown_df.copy()
            display_df["Manual Total (Rs)"] = display_df["Manual Total (Rs)"].apply(format_brokerage)
            display_df["File Total (Rs)"] = display_df["File Total (Rs)"].apply(format_brokerage)
            display_df["Difference (Rs)"] = display_df["Difference (Rs)"].apply(
                lambda x: format_brokerage(x) if pd.notna(x) and x != 0 else "—")
            display_df = display_df[
                ["AMC", "RTA", "Manual Total (Rs)", "File Total (Rs)", "Difference (Rs)", "Match Status"]]
            st.dataframe(display_df, use_container_width=True, hide_index=True)
            total_manual_all = breakdown_df["Manual Total (Rs)"].sum()
            total_file_all = breakdown_df["File Total (Rs)"].sum()
            total_diff_all = total_manual_all - total_file_all
            sm1, sm2, sm3 = st.columns(3)
            sm1.metric("💵 Total Manual (All AMCs)", format_brokerage(total_manual_all))
            sm2.metric("📄 Total File (All AMCs)", format_brokerage(total_file_all))
            sm3.metric("↔️ Overall Difference", format_brokerage(total_diff_all))
            export_df = breakdown_df.copy()
            export_df["Manual Total (Rs)"] = export_df["Manual Total (Rs)"].apply(lambda x: f"{x:.8f}")
            export_df["File Total (Rs)"] = export_df["File Total (Rs)"].apply(lambda x: f"{x:.8f}")
            export_df["Difference (Rs)"] = export_df["Difference (Rs)"].apply(
                lambda x: f"{x:.8f}" if pd.notna(x) else "")
            csv = export_df.to_csv(index=False).encode()
            st.download_button("⬇️ Export AMC Breakdown (CSV)", csv,
                               f"amc_breakdown_{datetime.now().strftime('%Y%m%d')}.csv", "text/csv")

# ==================== ⚙️ ADMIN PANEL ====================
elif mode == "⚙️ Admin Panel":
    st.header("⚙️ Admin Panel")
    tab_bse, tab_rta, tab_amc, tab_map, tab_db, tab_rta_map = st.tabs(
        ["📥 Import BSE Data", "🗂️ Import RTA Data", "🏢 AMC Config", "🔗 AMC Code Map", "🗄️ DB Info", "📦 RTA Mapping"])

    with tab_bse:
        st.subheader("📋 Client Master")
        f1 = st.file_uploader("Client Master Excel", type=["xlsx"], key="client_file")
        replace1 = st.checkbox("Replace existing clients", key="replace_clients",
                               help="Checked: deletes ALL existing clients, then reinserts from file.\nUnchecked: only inserts new client codes, existing ones are skipped.")
        if replace1:
            st.warning("⚠️ Replace mode: ALL existing clients will be deleted and reimported.", icon="⚠️")
        else:
            st.info("Append mode: only new client codes will be inserted; existing ones skipped.", icon="ℹ️")
        if st.button("📤 Import Clients") and f1:
            with st.spinner("Importing…"):
                ok, msg = parse_client_master(f1, replace1)
                (st.success if ok else st.error)(msg)
                if ok: st.cache_data.clear()
        st.divider()
        st.subheader("📋 SIP Report")
        f2 = st.file_uploader("SIP Report Excel", type=["xlsx"], key="sip_file")
        replace2 = st.checkbox("Replace existing holdings", key="replace_holdings",
                               help="Checked: deletes ALL existing holdings, then reinserts active SIPs from file.\nUnchecked: only inserts records with a new (folio + scheme_code + start_date) combo; duplicates are skipped.")
        if replace2:
            st.warning("⚠️ Replace mode: ALL existing holdings will be deleted and reimported.", icon="⚠️")
        else:
            st.info("Append mode: new SIPs are inserted; any (folio + scheme + start_date) already in DB is skipped.",
                    icon="ℹ️")
        if st.button("📤 Import SIPs") and f2:
            with st.spinner("Importing…"):
                ok, msg, preview = parse_sip_report(f2, replace2)
                (st.success if ok else st.error)(msg)
                if preview:
                    cols = st.columns(3)
                    if "active" in preview: cols[0].metric("✅ Active in file", preview["active"])
                    if "cancelled" in preview: cols[1].metric("❌ Cancelled (skipped)", preview["cancelled"])
                    if "first_order" in preview: cols[2].metric("🎁 First Order = Y", preview["first_order"])
                if ok: st.cache_data.clear()

    with tab_rta:
        st.subheader("🗂️ RTA Data Upload")
        st.caption("Upload brokerage and AUM reports from CAMS and KFinTech (Karvy).")
        rta_section = st.radio("Select data type", ["📂 Brokerage Data", "📦 AUM Data"], horizontal=True,
                               key="rta_section_toggle")
        st.divider()
        if rta_section == "📂 Brokerage Data":
            st.markdown("#### CAMS Brokerage File")
            st.caption(
                "Upload tab-separated CAMS brokerage report. Required columns: `FOLIO_NO`, `BRKAGE_AMT`, `AMC_CODE`.")
            cams_file = st.file_uploader("CAMS Brokerage CSV / TSV", type=["csv", "txt", "tsv"], key="cams_file")
            replace_cams = st.checkbox("Replace ALL existing CAMS brokerage data", key="replace_cams",
                                       help="Checked: deletes all existing CAMS brokerage rows, then reinserts.\nUnchecked: only inserts transactions not already present (matched on trxn_no + folio + accrual_month).")
            if replace_cams:
                st.warning("⚠️ Replace mode: ALL existing CAMS brokerage data will be deleted and reimported.",
                           icon="⚠️")
            else:
                st.info("ℹ️ Append mode: duplicate transactions (same trxn_no + folio + month) are silently skipped.",
                        icon="ℹ️")
            if st.button("📤 Upload CAMS Brokerage") and cams_file:
                with st.spinner("Parsing brokerage…"):
                    ok, msg, preview = parse_cams_brokerage(cams_file, replace_cams)
                    (st.success if ok else st.error)(msg)
                    if ok and preview:
                        pm1, pm2, pm3, pm4 = st.columns(4)
                        pm1.metric("📄 Inserted", preview.get("rows", 0))
                        pm2.metric("💰 Total Brokerage", format_brokerage(preview.get("total_brokerage", 0)))
                        pm3.metric("🔁 Duplicates skipped", preview.get("duplicates", 0))
                        pm4.metric("⏭️ Invalid skipped", preview.get("skipped", 0))
                        if preview.get("months"): st.info(f"Accrual months in file: **{', '.join(preview['months'])}**")
                    st.cache_data.clear()
            st.divider()
            st.markdown("#### Existing CAMS Brokerage Summary")
            with get_conn() as conn:
                cams_summary = pd.read_sql(
                    """SELECT accrual_month AS "Month", COUNT(*) AS "Rows", COUNT(DISTINCT folio_no) AS "Folios", ROUND(SUM(brkage_amt), 2) AS "Total Brokerage (Rs)", upload_batch AS "Batch" FROM cams_brokerage GROUP BY accrual_month, upload_batch ORDER BY accrual_month DESC""",
                    conn)
            if cams_summary.empty:
                st.info("No CAMS brokerage data uploaded yet.")
            else:
                st.dataframe(cams_summary, use_container_width=True, hide_index=True)
            if st.button("⚠️ Clear All CAMS Brokerage Data"):
                with get_conn() as conn: conn.execute("DELETE FROM cams_brokerage")
                st.warning("All CAMS brokerage data deleted.");
                st.cache_data.clear();
                st.rerun()

            st.divider()
            st.markdown("#### KFinTech (Karvy) Brokerage File")
            st.caption(
                "Upload tab-separated KFinTech brokerage report. Required columns: `Account Number`, `Brokerage (in Rs.)`, `Fund`.")
            kf_brok_file = st.file_uploader("KFinTech Brokerage CSV / TSV", type=["csv", "txt", "tsv"],
                                            key="kf_brok_file")
            replace_kf_brok = st.checkbox("Replace ALL existing KFinTech brokerage data", key="replace_kf_brok",
                                          help="Checked: deletes all existing KFinTech brokerage rows, then reinserts.\nUnchecked: only inserts transactions not already present (matched on trxn_no + folio + accrual_month).")
            if replace_kf_brok:
                st.warning("⚠️ Replace mode: ALL existing KFinTech brokerage data will be deleted and reimported.",
                           icon="⚠️")
            else:
                st.info("ℹ️ Append mode: duplicate transactions (same trxn_no + folio + month) are silently skipped.",
                        icon="ℹ️")
            if st.button("📤 Upload KFinTech Brokerage") and kf_brok_file:
                with st.spinner("Parsing KFinTech brokerage…"):
                    ok, msg, preview = parse_kfintech_brokerage(kf_brok_file, replace_kf_brok)
                    (st.success if ok else st.error)(msg)
                    if ok and preview:
                        pm1, pm2, pm3, pm4 = st.columns(4)
                        pm1.metric("📄 Inserted", preview.get("rows", 0))
                        pm2.metric("💰 Total Brokerage", format_brokerage(preview.get("total_brokerage", 0)))
                        pm3.metric("🔁 Duplicates skipped", preview.get("duplicates", 0))
                        pm4.metric("⏭️ Invalid skipped", preview.get("skipped", 0))
                        if preview.get("months"): st.info(f"Accrual months in file: **{', '.join(preview['months'])}**")
                    st.cache_data.clear()
            st.divider()
            st.markdown("#### Existing KFinTech Brokerage Summary")
            with get_conn() as conn:
                kf_summary = pd.read_sql(
                    """SELECT accrual_month AS "Month", COUNT(*) AS "Rows", COUNT(DISTINCT folio_no) AS "Folios", ROUND(SUM(brkage_amt), 2) AS "Total Brokerage (Rs)", upload_batch AS "Batch" FROM kfintech_brokerage GROUP BY accrual_month, upload_batch ORDER BY accrual_month DESC""",
                    conn)
            if kf_summary.empty:
                st.info("No KFinTech brokerage data uploaded yet.")
            else:
                st.dataframe(kf_summary, use_container_width=True, hide_index=True)
            if st.button("⚠️ Clear All KFinTech Brokerage Data"):
                with get_conn() as conn: conn.execute("DELETE FROM kfintech_brokerage")
                st.warning("All KFinTech brokerage data deleted.");
                st.cache_data.clear();
                st.rerun()
        else:
            col_cams, col_kfin = st.columns(2)
            with col_cams:
                st.markdown("#### CAMS AUM Report")
                st.caption("Required columns: `FOLIOCHK`, `RUPEE_BAL`, `SCH_NAME`, `AMC_CODE`, `CLOS_BAL`.")
                aum_file = st.file_uploader("CAMS AUM CSV / TSV", type=["csv", "txt", "tsv"], key="aum_file")
                replace_aum = st.checkbox("Replace existing CAMS AUM", key="replace_aum",
                                          help="Checked: deletes all CAMS AUM rows, reinserts.\nUnchecked: skips rows already present (folio + scheme + rep_date).")
                if replace_aum:
                    st.warning("⚠️ Replace mode: ALL existing CAMS AUM data will be deleted.", icon="⚠️")
                else:
                    st.info("ℹ️ Append mode: duplicate (folio + scheme + date) rows skipped.", icon="ℹ️")
                if st.button("📤 Upload CAMS AUM") and aum_file:
                    with st.spinner("Parsing CAMS AUM…"):
                        ok, msg, preview = parse_cams_aum(aum_file, replace_aum)
                        (st.success if ok else st.error)(msg)
                        if ok and preview:
                            am1, am2, am3, am4 = st.columns(4)
                            am1.metric("📄 Inserted", preview.get("rows", 0))
                            am2.metric("📦 Total AUM", format_aum(preview.get("total_aum", 0)))
                            am3.metric("🔁 Duplicates", preview.get("duplicates", 0))
                            am4.metric("⏭️ Skipped", preview.get("skipped", 0))
                        st.cache_data.clear()
            with col_kfin:
                st.markdown("#### KFinTech AUM Report")
                st.caption(
                    "Expected columns: `Folio Number`, `Fund Description`, `AUM`, `Balance`, `NAV`, `Report Date`.")
                kfin_file = st.file_uploader("KFinTech AUM CSV / TSV", type=["csv", "txt", "tsv"], key="kfin_aum_file")
                replace_kfin = st.checkbox("Replace existing KFinTech AUM", key="replace_kfin",
                                           help="Checked: deletes all KFinTech AUM rows, reinserts.\nUnchecked: skips duplicate (folio + scheme + date) rows.")
                if replace_kfin:
                    st.warning("⚠️ Replace mode: ALL existing KFinTech AUM data will be deleted.", icon="⚠️")
                else:
                    st.info("ℹ️ Append mode: duplicate (folio + scheme + date) rows skipped.", icon="ℹ️")
                if st.button("📤 Upload KFinTech AUM") and kfin_file:
                    with st.spinner("Parsing KFinTech AUM…"):
                        ok, msg, preview = parse_kfintech_aum(kfin_file, replace_kfin)
                        (st.success if ok else st.error)(msg)
                        if ok and preview:
                            km1, km2, km3, km4 = st.columns(4)
                            km1.metric("📄 Inserted", preview.get("rows", 0))
                            km2.metric("📦 Total AUM", format_aum(preview.get("total_aum", 0)))
                            km3.metric("🔁 Duplicates", preview.get("duplicates", 0))
                            km4.metric("⏭️ Skipped", preview.get("skipped", 0))
                        st.cache_data.clear()
            st.divider()
            st.markdown("#### Existing AUM Summary")
            c1, c2 = st.columns(2)
            with c1:
                st.markdown("**CAMS AUM**")
                with get_conn() as conn:
                    aum_summary = pd.read_sql(
                        """SELECT rep_date AS "Report Date", amc_code AS "AMC Code", COUNT(*) AS "Folios", ROUND(SUM(rupee_bal), 2) AS "Total AUM (Rs)" FROM cams_aum GROUP BY rep_date, amc_code ORDER BY rep_date DESC, 4 DESC""",
                        conn)
                if aum_summary.empty:
                    st.info("No CAMS AUM data uploaded yet.")
                else:
                    aum_summary["Total AUM (Rs)"] = aum_summary["Total AUM (Rs)"].apply(format_aum)
                    st.dataframe(aum_summary, use_container_width=True, hide_index=True)
                st.metric("CAMS Grand Total", format_aum(load_total_aum()))
                if st.button("⚠️ Clear CAMS AUM"):
                    with get_conn() as conn: conn.execute("DELETE FROM cams_aum")
                    st.warning("CAMS AUM data deleted.");
                    st.cache_data.clear();
                    st.rerun()
            with c2:
                st.markdown("**KFinTech AUM**")
                with get_conn() as conn:
                    kfin_summary = pd.read_sql(
                        """SELECT rep_date AS "Report Date", amc_code AS "AMC Code", COUNT(*) AS "Folios", ROUND(SUM(rupee_bal), 2) AS "Total AUM (Rs)" FROM kfintech_aum GROUP BY rep_date, amc_code ORDER BY rep_date DESC, 4 DESC""",
                        conn)
                if kfin_summary.empty:
                    st.info("No KFinTech AUM data uploaded yet.")
                else:
                    kfin_summary["Total AUM (Rs)"] = kfin_summary["Total AUM (Rs)"].apply(format_aum)
                    st.dataframe(kfin_summary, use_container_width=True, hide_index=True)
                st.metric("KFinTech Grand Total", format_aum(load_total_kfintech_aum()))
                if st.button("⚠️ Clear KFinTech AUM"):
                    with get_conn() as conn: conn.execute("DELETE FROM kfintech_aum")
                    st.warning("KFinTech AUM data deleted.");
                    st.cache_data.clear();
                    st.rerun()
            st.divider()
            combined = load_combined_total_aum()
            st.metric("📦 Combined Total AUM (CAMS + KFinTech)", format_aum(combined["total"]))
            if st.button("⚠️ Clear ALL AUM Data"):
                with get_conn() as conn:
                    conn.execute("DELETE FROM cams_aum");
                    conn.execute("DELETE FROM kfintech_aum")
                st.warning("All AUM data (CAMS + KFinTech) deleted.");
                st.cache_data.clear();
                st.rerun()

    with tab_amc:
        st.subheader("Enable / Disable AMCs")
        with get_conn() as conn:
            amc_cfg = pd.read_sql("SELECT amc, rta, is_enabled FROM amc_config ORDER BY amc", conn)
        if amc_cfg.empty:
            st.info("No AMCs found. Run AMFI sync first.")
        else:
            updated_enabled = {}
            for _, row in amc_cfg.iterrows(): updated_enabled[row["amc"]] = st.checkbox(row["amc"],
                                                                                        value=bool(row["is_enabled"]),
                                                                                        key=f"amc_{row['amc']}")
            if st.button("💾 Save AMC Config"):
                with get_conn() as conn:
                    for amc, enabled in updated_enabled.items(): conn.execute(
                        "UPDATE amc_config SET is_enabled=? WHERE amc=?", (1 if enabled else 0, amc))
                st.success("AMC config saved.");
                st.cache_data.clear();
                st.rerun()

    with tab_map:
        st.subheader("🔗 AMC Code → AMC Name Mapping")
        st.caption("Map short CAMS/KFin codes to full AMC names.")
        cams_codes = load_distinct_cams_amc_codes()
        current_map = load_amc_code_map()
        with get_conn() as conn:
            known_amcs = sorted({r[0] for r in conn.execute(
                "SELECT DISTINCT amc FROM holdings WHERE amc IS NOT NULL AND amc != ''").fetchall()})
        if not cams_codes:
            st.info("No RTA data uploaded yet.")
        elif not known_amcs:
            st.info("No holdings imported yet.")
        else:
            st.markdown(f"**{len(cams_codes)} AMC code(s) found**")
            unmapped = [c for c in cams_codes if c not in current_map]
            if unmapped: st.warning(f"{len(unmapped)} code(s) not yet mapped: {', '.join(unmapped)}")
            updated_map: dict[str, str] = {}
            # Split into two separate lines so 'cols_per_row' is defined before it is used
            cols_per_row = 2
            rows_needed = -(-len(cams_codes) // cols_per_row)
            for row_i in range(rows_needed):
                cols = st.columns(cols_per_row)
                for col_i in range(cols_per_row):
                    idx = row_i * cols_per_row + col_i
                    if idx >= len(cams_codes): break
                    code = cams_codes[idx]
                    existing = current_map.get(code, "")
                    options = [""] + known_amcs
                    default_idx = options.index(existing) if existing in options else 0
                    with cols[col_i]:
                        sel = st.selectbox(f"AMC Code: **{code}**", options, index=default_idx, key=f"amc_map_{code}",
                                           placeholder="Select AMC name…")
                        updated_map[code] = sel
            if st.button("💾 Save Mappings", type="primary"):
                with get_conn() as conn:
                    for code, name in updated_map.items():
                        if name:
                            conn.execute(
                                "INSERT INTO amc_code_map (amc_code, amc_name) VALUES (?,?) ON CONFLICT(amc_code) DO UPDATE SET amc_name=excluded.amc_name",
                                (code.strip(), name))
                        else:
                            conn.execute("DELETE FROM amc_code_map WHERE amc_code=?", (code.strip(),))
                st.success("Mappings saved.");
                st.cache_data.clear();
                st.rerun()

    with tab_db:
        st.subheader("Database Stats")
        with get_conn() as conn:
            stats = {
                "Clients": conn.execute("SELECT COUNT(*) FROM clients").fetchone()[0],
                "Holdings": conn.execute("SELECT COUNT(*) FROM holdings").fetchone()[0],
                "Schemes": conn.execute("SELECT COUNT(*) FROM amc_schemes").fetchone()[0],
                "Brokerage": conn.execute("SELECT COUNT(*) FROM monthly_brokerage").fetchone()[0],
                "CAMS Brokerage rows": conn.execute("SELECT COUNT(*) FROM cams_brokerage").fetchone()[0],
                "KFinTech Brokerage rows": conn.execute("SELECT COUNT(*) FROM kfintech_brokerage").fetchone()[0],
                "CAMS AUM rows": conn.execute("SELECT COUNT(*) FROM cams_aum").fetchone()[0],
                "KFinTech AUM rows": conn.execute("SELECT COUNT(*) FROM kfintech_aum").fetchone()[0],
            }
        for k, v in stats.items(): st.metric(k, v)
        if st.button("⚠️ Clear All Holdings"):
            with get_conn() as conn: conn.execute("DELETE FROM holdings")
            st.warning("All holdings deleted.");
            st.cache_data.clear();
            st.rerun()

    with tab_rta_map:
        st.subheader("📦 RTA Mapping Manager")
        st.caption("Override auto-detected RTA assignments for specific AMCs.")
        with get_conn() as conn:
            distinct_amcs = conn.execute("SELECT DISTINCT amc FROM holdings WHERE amc!='' ORDER BY amc").fetchall()
            amc_rta_map = {r[0]: get_rta(r[0]) for r in distinct_amcs}
            db_overrides = conn.execute(
                "SELECT amc, rta FROM amc_config WHERE amc IN (SELECT DISTINCT amc FROM holdings)").fetchall()
            for amc, rta in db_overrides: amc_rta_map[amc] = rta
            updated_rtas = {}
            # Split into two separate lines
            cols_per_row = 2
            rows_needed = -(-len(amc_rta_map) // cols_per_row)
            for row_i in range(rows_needed):
                cols = st.columns(cols_per_row)
                for col_i in range(cols_per_row):
                    idx = row_i * cols_per_row + col_i
                    if idx >= len(amc_rta_map): break
                    amc = list(amc_rta_map.keys())[idx]
                    current = amc_rta_map[amc]
                    with cols[col_i]:
                        sel = st.selectbox(amc, ["CAMS", "KFinTech", "Unknown"],
                                           index=["CAMS", "KFinTech", "Unknown"].index(current), key=f"rta_{amc}")
                        updated_rtas[amc] = sel
            if st.button("💾 Save RTA Overrides", type="primary"):
                with get_conn() as conn:
                    for amc, rta in updated_rtas.items(): conn.execute(
                        "INSERT INTO amc_config (amc, rta) VALUES (?,?) ON CONFLICT(amc) DO UPDATE SET rta=excluded.rta",
                        (amc, rta))
                    conn.execute(
                        "UPDATE holdings SET rta = (SELECT rta FROM amc_config WHERE amc = holdings.amc) WHERE amc IN (SELECT amc FROM amc_config)")
                st.success("RTA mappings updated & synced to holdings.");
                st.cache_data.clear();
                st.rerun()
