"""
MFD Portfolio Intelligence — Streamlit App
"""
import csv
import logging
import re
import threading
import warnings
from contextlib import contextmanager
from datetime import datetime, timedelta
from io import StringIO

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
DB_PATH = "DataBack.db"
AMFI_TEXT_URL = "https://portal.amfiindia.com/spages/NAVAll.txt"
MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
CANCELLED_KEYWORDS = frozenset(
    ["CXL","AUTOCXL","AUTO CXL","CX","CANCEL","CLOSED","REDEEM","STOPPED","FAILED"]
)
AMC_SUFFIXES = [" MUTUAL FUND"," MF"," FUND"," AMC"," INDIA"," MANAGEMENT"," LTD"," LIMITED"]
_WHITESPACE_RE = re.compile(r"\s+")
PAGE_SIZE = 20

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
                    client_code TEXT PRIMARY KEY, name TEXT, pan TEXT,
                    mobile TEXT, email TEXT, kyc_status TEXT,
                    start_date TEXT, notes TEXT DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS holdings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_code TEXT, folio_no TEXT, scheme_code TEXT,
                    scheme_name TEXT, amc TEXT, investment_type TEXT,
                    sip_amount REAL, sip_day INTEGER DEFAULT 1,
                    start_date TEXT, end_date TEXT,
                    status TEXT DEFAULT 'Active', first_order TEXT DEFAULT 'N'
                );
                CREATE TABLE IF NOT EXISTS amc_schemes (
                    scheme_code TEXT PRIMARY KEY, amc TEXT,
                    scheme_name TEXT, category TEXT, last_nav REAL, nav_date TEXT
                );
                CREATE TABLE IF NOT EXISTS amc_config (
                    amc TEXT PRIMARY KEY, is_enabled INTEGER DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS monthly_brokerage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    amc TEXT, month TEXT, year INTEGER,
                    amount REAL, notes TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                );
            """)
        st.session_state["db_initialized"] = True

    with get_conn() as conn:
        try:
            conn.execute("ALTER TABLE holdings ADD COLUMN first_order TEXT DEFAULT 'N'")
        except sqlite3.OperationalError:
            pass

        conn.executescript("""
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
            CREATE TABLE IF NOT EXISTS cams_amc_map (
                amc_code     TEXT PRIMARY KEY,
                display_name TEXT DEFAULT ''
            );
        """)

        try:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "ux_monthly_brokerage ON monthly_brokerage (amc, month, year)"
            )
        except sqlite3.OperationalError:
            conn.execute("""
                DELETE FROM monthly_brokerage WHERE id NOT IN (
                    SELECT MAX(id) FROM monthly_brokerage GROUP BY amc, month, year
                )
            """)
            try:
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS "
                    "ux_monthly_brokerage ON monthly_brokerage (amc, month, year)"
                )
            except sqlite3.OperationalError:
                pass


# ==================== PURE HELPERS ====================
def clean_str(val) -> str:
    if val is None:
        return ""
    try:
        if pd.isna(val):
            return ""
    except (TypeError, ValueError):
        pass
    s = str(val).strip()
    return "" if s.lower() in {"nan","none","null","na",""} else s


def format_currency(val, decimals: int = 0) -> str:
    try:
        return f"Rs {float(val):,.{decimals}f}"
    except (TypeError, ValueError):
        return "Rs -"


def format_brokerage(val) -> str:
    try:
        return f"Rs {float(val):,.4f}"
    except (TypeError, ValueError):
        return "Rs -"

# 🔧 NEW: Dedicated formatter for manual brokerage entries (2 decimal places)
def format_manual_brokerage(val, decimals: int = 2) -> str:
    """Format manual brokerage amounts with configurable decimals (default: 2 for paise)."""
    try:
        return f"Rs {float(val):,.{decimals}f}"
    except (TypeError, ValueError):
        return "Rs -"

@st.cache_data(show_spinner=False)
def normalize_amc(name: str) -> str:
    if not name:
        return ""
    n = str(name).strip().upper()
    for suffix in AMC_SUFFIXES:
        n = n.replace(suffix, "")
    return _WHITESPACE_RE.sub(" ", n).strip()


def normalize_name(name: str) -> str:
    return _WHITESPACE_RE.sub(" ", str(name).strip().lower())


def folio_base(folio: str) -> str:
    """Strip sub-account suffix: '8687996/09' -> '8687996'."""
    s = str(folio).strip()
    return s.split("/")[0].strip() if "/" in s else s


def is_active_status(raw_status) -> bool:
    if pd.isna(raw_status):
        return True
    return not any(kw in str(raw_status).strip().upper() for kw in CANCELLED_KEYWORDS)


def parse_date_safe(val) -> str:
    try:
        if pd.isna(val):
            return ""
    except (TypeError, ValueError):
        pass
    if str(val).strip() in {"","None","NaN"}:
        return ""
    try:
        dt = pd.to_datetime(val, dayfirst=True, errors="coerce")
        return dt.strftime("%Y-%m-%d") if pd.notna(dt) else ""
    except Exception:
        return ""


def calc_invested_series(start_dates, sip_amts, first_orders, today) -> pd.Series:
    fo_bonus = sip_amts.where(first_orders.str.upper() == "Y", 0.0)
    sd = pd.to_datetime(start_dates, format="%Y-%m-%d", errors="coerce")
    months = (today.year - sd.dt.year) * 12 + (today.month - sd.dt.month)
    months = months.where(today.day >= sd.dt.day, months - 1).clip(lower=0)
    invested = fo_bonus + (months * sip_amts)
    invested = invested.where(sd.notna() & (sd <= today), fo_bonus)
    return invested.fillna(0.0)


def get_next_sip_date(day) -> str:
    if pd.isna(day) or not day:
        return "N/A"
    try:
        day = int(day)
        today = datetime.now()
        try:
            candidate = today.replace(day=day)
            if candidate.date() >= today.date():
                return candidate.strftime("%d %b %Y")
        except ValueError:
            pass
        nxt = (today.replace(day=28) + timedelta(days=4)).replace(day=1)
        try:
            return nxt.replace(day=day).strftime("%d %b %Y")
        except ValueError:
            last = (nxt.replace(month=nxt.month % 12 + 1, day=1) - timedelta(days=1))
            return last.strftime("%d %b %Y")
    except Exception:
        return "N/A"


# ==================== AMFI SYNC ====================
def _amfi_sync_worker(result_bucket: list) -> None:
    try:
        res = requests.get(AMFI_TEXT_URL, timeout=20)
        res.raise_for_status()
        schemes, curr_amc = [], None
        for line in res.text.splitlines():
            line = line.strip()
            if not line or ";" not in line:
                if "Mutual Fund" in line:
                    curr_amc = line.strip()
                continue
            parts = line.split(";")
            if len(parts) < 6 or parts[0] == "Scheme Code":
                continue
            name = parts[3].strip()
            nu = name.upper()
            if ("REGULAR" in nu or "RETAIL" in nu) and "DIRECT" not in nu:
                try:
                    nav = float(parts[4]) if parts[4] not in ("N.A.","") else 0.0
                except ValueError:
                    nav = 0.0
                schemes.append((parts[0].strip(), curr_amc, name, "Auto", nav, parts[5].strip()))
        if schemes:
            with get_conn() as conn:
                conn.executemany("INSERT OR REPLACE INTO amc_schemes VALUES (?,?,?,?,?,?)", schemes)
                conn.execute(
                    "INSERT OR IGNORE INTO amc_config (amc, is_enabled) "
                    "SELECT DISTINCT amc, 1 FROM amc_schemes WHERE amc IS NOT NULL"
                )
            result_bucket.append(("ok", f"Synced {len(schemes):,} schemes"))
        else:
            result_bucket.append(("error", "Parsed 0 schemes"))
    except Exception as exc:
        log.exception("AMFI sync failed")
        result_bucket.append(("error", str(exc)))


def start_amfi_sync() -> None:
    if st.session_state.get("amfi_sync_started"):
        return
    st.session_state["amfi_sync_started"] = True
    bucket: list = []
    st.session_state["amfi_result"] = bucket
    t = threading.Thread(target=_amfi_sync_worker, args=(bucket,), daemon=True)
    add_script_run_ctx(t)
    t.start()


# ==================== COLUMN NORMALISATION ====================
def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    df.columns = (
        df.columns.str.strip().str.lower()
        .str.replace(r"[\s.\-_]+", "_", regex=True)
    )
    return df


# ==================== IMPORT: CLIENT MASTER ====================
def parse_client_master(file, replace: bool) -> tuple[bool, str]:
    file.seek(0)
    try:
        df = pd.read_excel(file)
    except Exception as exc:
        return False, f"File read error: {exc}"
    df = normalize_columns(df)

    def _col(*cands):
        return next((c for c in cands if c in df.columns), None)

    code_col  = _col("client_code", "member_code")
    fname_col = _col("primary_holder_first_name")
    lname_col = _col("primary_holder_last_name")
    pan_col   = _col("primary_holder_pan")
    mob_col   = _col("indian_mobile_no_")
    email_col = _col("email", "primary_holder_email")
    date_col  = _col("created_at")

    if not code_col or not fname_col:
        return False, "Missing: client_code / primary_holder_first_name"

    rows = []
    for _, row in df.iterrows():
        code = clean_str(row.get(code_col))
        if not code:
            continue
        fn   = clean_str(row.get(fname_col, ""))
        ln   = clean_str(row.get(lname_col, "")) if lname_col else ""
        name = f"{fn} {ln}".strip() or "Unknown"
        rows.append((
            code, name,
            clean_str(row.get(pan_col, "")).upper(),
            clean_str(row.get(mob_col, "")),
            clean_str(row.get(email_col, "")).lower(),
            "Verified",
            parse_date_safe(row.get(date_col)) or datetime.now().strftime("%Y-%m-%d"),
        ))

    if not rows:
        return False, "No valid rows found"
    with get_conn() as conn:
        if replace:
            conn.execute("DELETE FROM clients")
        conn.executemany(
            "INSERT OR REPLACE INTO clients "
            "(client_code, name, pan, mobile, email, kyc_status, start_date) "
            "VALUES (?,?,?,?,?,?,?)", rows
        )
    return True, f"Imported {len(rows)} clients"


# ==================== IMPORT: SIP REPORT ====================
def parse_sip_report(file, replace: bool) -> tuple[bool, str, dict]:
    file.seek(0)
    try:
        df = pd.read_excel(file)
    except Exception as exc:
        return False, f"File read error: {exc}", {}
    df = normalize_columns(df)
    missing = [c for c in ["client_code","folio_no","scheme_name","amc_name"] if c not in df.columns]
    if missing:
        return False, f"Missing columns: {missing}", {}

    preview = {}
    if "status" in df.columns:
        a = int(df["status"].apply(is_active_status).sum())
        preview["active"] = a
        preview["cancelled"] = len(df) - a
    if "first_order" in df.columns:
        preview["first_order"] = int((df["first_order"].astype(str).str.upper() == "Y").sum())

    rows, skipped = [], 0
    for _, row in df.iterrows():
        if not is_active_status(row.get("status", "ACTIVE")):
            skipped += 1
            continue
        try:
            fo = clean_str(row.get("first_order","N")).upper()
            if fo not in ("Y","N"): fo = "N"
            sd = parse_date_safe(row.get("start_date"))
            sip_day = pd.to_datetime(sd).day if sd else 1
            rows.append((
                clean_str(row["client_code"]), clean_str(row["folio_no"]),
                clean_str(row.get("rta_scheme_code","")), clean_str(row["scheme_name"]),
                clean_str(row["amc_name"]), clean_str(row.get("frequency_type","")),
                float(row.get("installments_amt") or 0), sip_day, sd,
                parse_date_safe(row.get("end_date")), "Active", fo,
            ))
        except Exception as exc:
            log.warning("Skipped SIP row: %s", exc)
            skipped += 1

    if not rows:
        return False, "0 SIPs imported", preview
    with get_conn() as conn:
        if replace:
            conn.execute("DELETE FROM holdings")
        conn.executemany(
            "INSERT INTO holdings (client_code, folio_no, scheme_code, scheme_name, amc, "
            "investment_type, sip_amount, sip_day, start_date, end_date, status, first_order) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows
        )
    msg = f"Imported {len(rows)} Active SIPs"
    if skipped: msg += f" | Skipped {skipped}"
    return True, msg, preview


# ==================== IMPORT: CAMS BROKERAGE ====================
def _read_cams_dataframe(file) -> tuple["pd.DataFrame | None", str]:
    """
    Reads CAMS brokerage CSV using Python csv.reader with quotechar="'".
    Handles comma-separated files with mixed quoted strings and bare numbers.
    pandas quotechar fails on mixed rows — this approach does not.
    """
    file.seek(0)
    try:
        raw = file.read().decode("utf-8", errors="replace")
    except Exception as exc:
        return None, f"File read error: {exc}"

    first_line = next((ln for ln in raw.splitlines() if ln.strip()), "")
    delimiter = "," if first_line.count(",") >= first_line.count("\t") else "\t"

    try:
        reader = csv.reader(StringIO(raw), delimiter=delimiter,
                            quotechar="'", skipinitialspace=True)
        all_rows = [row for row in reader if any(c.strip() for c in row)]
    except Exception as exc:
        return None, f"CSV parse error: {exc}"

    if not all_rows:
        return None, "File is empty."

    headers = [h.strip().strip("'").upper() for h in all_rows[0]]
    n_cols  = len(headers)
    records = []
    for row in all_rows[1:]:
        cleaned = [c.strip().strip("'").strip() for c in row]
        if len(cleaned) < n_cols:
            cleaned += [""] * (n_cols - len(cleaned))
        records.append(cleaned[:n_cols])

    if not records:
        return None, "No data rows found."

    return pd.DataFrame(records, columns=headers), ""


def parse_cams_brokerage(file, replace: bool) -> tuple[bool, str, dict]:
    df, err = _read_cams_dataframe(file)
    if df is None:
        return False, err, {}

    required = ["FOLIO_NO","BRKAGE_AMT","AMC_CODE"]
    missing  = [c for c in required if c not in df.columns]
    if missing:
        return False, (
            f"Missing columns: {missing}\n"
            f"Found ({len(df.columns)}): {sorted(df.columns.tolist())}"
        ), {"found_columns": sorted(df.columns.tolist())}

    batch_id = f"{getattr(file,'name','upload')}_{datetime.now().strftime('%Y%m%d%H%M%S')}"

    def _accrual(val: str) -> str:
        try:
            dt = pd.to_datetime(val, errors="coerce")
            return dt.strftime("%Y-%m") if pd.notna(dt) else ""
        except Exception:
            return ""

    def _f(val) -> float:
        try:
            v = str(val).replace(",","").strip().strip("'")
            return float(v) if v not in ("","N.A.","NA","-") else 0.0
        except (ValueError, TypeError):
            return 0.0

    rows, skipped = [], 0
    for _, row in df.iterrows():
        try:
            rows.append((
                clean_str(row.get("AMC_CODE","")),
                clean_str(row.get("FOLIO_NO","")),
                clean_str(row.get("SCHEME_CODE", row.get("TXN_SCH_CODE",""))),
                clean_str(row.get("TRXN_NO","")),
                clean_str(row.get("TRXN_TYPE","")),
                _f(row.get("BRKAGE_AMT", 0)),
                clean_str(row.get("BRKAGE_TYPE","")),
                _f(row.get("BRKAGE_RATE", 0)),
                clean_str(row.get("INV_NAME","")).strip(),
                parse_date_safe(row.get("PROC_DATE","")),
                _accrual(row.get("BROKERAGE_ACRUAL_MONTH","")),
                _f(row.get("PLOT_AMOUNT", 0)),
                _f(row.get("AVG_ASSETS", 0)),
                _f(row.get("IGST_VALUE", 0)),
                _f(row.get("CGST_VALUE", 0)),
                _f(row.get("SGST_VALUE", 0)),
                batch_id,
            ))
        except Exception as exc:
            log.warning("Skipped CAMS row: %s", exc)
            skipped += 1

    if not rows:
        return False, "0 rows imported", {}

    with get_conn() as conn:
        if replace:
            conn.execute("DELETE FROM cams_brokerage")
        conn.executemany(
            "INSERT OR IGNORE INTO cams_brokerage "
            "(amc_code, folio_no, scheme_code, trxn_no, trxn_type, brkage_amt, "
            " brkage_type, brkage_rate, inv_name, proc_date, accrual_month, "
            " plot_amount, avg_assets, igst_value, cgst_value, sgst_value, upload_batch) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows
        )
        conn.execute(
            "INSERT OR IGNORE INTO cams_amc_map (amc_code, display_name) "
            "SELECT DISTINCT amc_code, '' FROM cams_brokerage WHERE amc_code != ''"
        )

    total = sum(r[5] for r in rows)
    preview = {
        "rows": len(rows), "skipped": skipped, "total_brokerage": total,
        "months": sorted({r[10] for r in rows if r[10]}),
    }
    msg = f"Imported {len(rows)} CAMS rows | {format_brokerage(total)}"
    if skipped: msg += f" | Skipped {skipped}"
    return True, msg, preview


# ==================== CAMS DATA LOADERS ====================
@st.cache_data(ttl=120, show_spinner=False)
def load_cams_by_client(client_code: str) -> pd.DataFrame:
    """
    Finds all CAMS brokerage rows for a client.

    Strategy 1 — Folio base match:
      Strip '/XX' suffix from both sides then compare.
      Covers SIP clients (folio in holdings) and any lumpsum folio stored there.

    Strategy 2 — Investor name word overlap (>=2 words):
      Handles middle names and lumpsum clients with no holdings row.
      e.g. 'Kishore Goyal' matches 'Kishore Roopchand Goyal' (2 words overlap).
    """
    with get_conn() as conn:
        client_row = conn.execute(
            "SELECT name FROM clients WHERE client_code = ?", (client_code,)
        ).fetchone()
        if not client_row:
            return pd.DataFrame()

        client_name  = normalize_name(client_row["name"])
        client_words = set(client_name.split()) - {"","mr","mrs","ms","dr"}

        folio_rows = conn.execute(
            "SELECT DISTINCT folio_no FROM holdings WHERE client_code = ?",
            (client_code,)
        ).fetchall()
        client_folio_bases = {folio_base(r["folio_no"]) for r in folio_rows}

        all_cams = pd.read_sql(
            "SELECT amc_code, folio_no, scheme_code, trxn_no, trxn_type, "
            "brkage_amt, brkage_rate, inv_name, proc_date, accrual_month, "
            "plot_amount, avg_assets FROM cams_brokerage ORDER BY accrual_month DESC",
            conn
        )
        amc_map = dict(conn.execute(
            "SELECT amc_code, display_name FROM cams_amc_map"
        ).fetchall())

    if all_cams.empty:
        return pd.DataFrame()

    def _matches(row) -> bool:
        if folio_base(str(row["folio_no"])) in client_folio_bases:
            return True
        cams_words = set(normalize_name(str(row["inv_name"])).split()) - {"","mr","mrs","ms","dr"}
        return len(client_words & cams_words) >= 2

    matched = all_cams[all_cams.apply(_matches, axis=1)].copy()
    matched["amc_name"] = matched["amc_code"].map(lambda c: amc_map.get(c,"") or c)
    return matched.reset_index(drop=True)


@st.cache_data(ttl=60, show_spinner=False)
def load_cams_by_amc(month_str: str) -> pd.DataFrame:
    """
    Brokerage by amc_code for a given accrual month.
    No join with holdings — amc_code is the CAMS short code (PP, G, T...).
    Uses cams_amc_map display_name where set by the user.
    """
    with get_conn() as conn:
        return pd.read_sql(
            """
            SELECT
                cb.amc_code,
                COALESCE(NULLIF(m.display_name,''), cb.amc_code) AS display_name,
                SUM(cb.brkage_amt)          AS cams_brokerage,
                COUNT(DISTINCT cb.folio_no) AS folio_count,
                COUNT(*)                    AS txn_count
            FROM cams_brokerage cb
            LEFT JOIN cams_amc_map m ON m.amc_code = cb.amc_code
            WHERE cb.accrual_month = ?
            GROUP BY cb.amc_code
            ORDER BY cams_brokerage DESC
            """,
            conn, params=(month_str,),
        )


@st.cache_data(ttl=60, show_spinner=False)
def load_cams_months() -> list[str]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT accrual_month FROM cams_brokerage "
            "WHERE accrual_month != '' ORDER BY accrual_month DESC"
        ).fetchall()
    return [r[0] for r in rows]


@st.cache_data(ttl=60, show_spinner=False)
def load_amc_map() -> dict[str, str]:
    with get_conn() as conn:
        return dict(conn.execute(
            "SELECT amc_code, display_name FROM cams_amc_map ORDER BY amc_code"
        ).fetchall())


@st.cache_data(ttl=60, show_spinner=False)
def load_active_amcs() -> list[str]:
    with get_conn() as conn:
        return [r[0] for r in conn.execute(
            "SELECT amc FROM amc_config WHERE is_enabled = 1"
        ).fetchall() if r[0]]


@st.cache_data(ttl=60, show_spinner=False)
def load_active_holdings() -> pd.DataFrame:
    enabled = load_active_amcs()
    if not enabled:
        return pd.DataFrame()
    enabled_norm = {normalize_amc(a) for a in enabled}
    with get_conn() as conn:
        df = pd.read_sql("SELECT * FROM holdings WHERE status='Active'", conn)
    if df.empty:
        return df
    df["norm_amc"] = df["amc"].apply(normalize_amc)
    return df[df["norm_amc"].isin(enabled_norm)].drop(columns=["norm_amc"]).reset_index(drop=True)


@st.cache_data(ttl=300, show_spinner=False)
def load_clients() -> pd.DataFrame:
    with get_conn() as conn:
        return pd.read_sql("SELECT client_code, name FROM clients ORDER BY name", conn)


# ==================== APP INIT ====================
st.set_page_config(page_title="MFD Portfolio Intelligence", layout="wide", page_icon="📊")
st.markdown("""
<style>
    .stMarkdown,.stMetric,.stDataFrame,[data-testid="stMetricLabel"],
    [data-testid="stMetricValue"],.streamlit-expanderContent,.stAlert
        { color: var(--text-color) !important; }
    .dataframe { color: var(--text-color) !important;
                 background: var(--background-color) !important; }
    .dataframe th { background: var(--secondary-background-color) !important; }
</style>
""", unsafe_allow_html=True)

init_db()
start_amfi_sync()

result_bucket = st.session_state.get("amfi_result", [])
if result_bucket:
    status, msg = result_bucket[0]
    st.toast(f"{'OK' if status=='ok' else 'WARN'} AMFI: {msg}")
    result_bucket.clear()

# ==================== SIDEBAR ====================
with st.sidebar:
    st.title("MFD Intelligence")
    mode = st.radio("Navigate", [
        "Dashboard", "Client View", "Earnings", "Admin Panel"
    ], label_visibility="collapsed")
    if st.button("Refresh Data", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

df_active       = load_active_holdings()
active_amcs_raw = load_active_amcs()

# ==================== DASHBOARD ====================
if mode == "Dashboard":
    st.header("Portfolio Overview")

    with get_conn() as conn:
        total_brokerage = conn.execute(
            "SELECT COALESCE(SUM(amount),0) FROM monthly_brokerage"
        ).fetchone()[0]
        total_clients = conn.execute("SELECT COUNT(*) FROM clients").fetchone()[0]

    sip_total = df_active["sip_amount"].sum() if not df_active.empty else 0

    m1,m2,m3,m4,m5 = st.columns(5)
    m1.metric("Clients", total_clients)
    m2.metric("Active SIPs", len(df_active))
    m3.metric("Monthly SIP", format_currency(sip_total))
    m4.metric("Enabled AMCs", len(active_amcs_raw))
    m5.metric("Brokerage Rcvd.", format_currency(total_brokerage))
    st.divider()

    if df_active.empty:
        st.warning("No Active Holdings Found")
    else:
        amc_grp = df_active.groupby("amc")["sip_amount"].sum().reset_index()
        c1, c2 = st.columns(2)
        with c1:
            st.plotly_chart(px.pie(amc_grp, values="sip_amount", names="amc",
                hole=0.4, title="SIP Distribution by AMC"), use_container_width=True)
        with c2:
            st.plotly_chart(px.bar(amc_grp, x="amc", y="sip_amount",
                labels={"amc":"AMC","sip_amount":"Monthly SIP (Rs)"},
                title="Monthly SIP by AMC"), use_container_width=True)
        st.download_button("Export AMC Summary (CSV)",
            amc_grp.to_csv(index=False).encode(), "amc_summary.csv", "text/csv")


# ==================== CLIENT VIEW ====================
elif mode == "Client View":
    st.header("Client Portfolio")
    clients = load_clients()

    if clients.empty:
        st.warning("No clients imported.")
    else:
        sel = st.selectbox("Select Client", clients["name"].tolist(),
                           index=None, placeholder="Search...")
        if sel:
            code = clients.loc[clients["name"]==sel, "client_code"].iloc[0]
            c_hold = df_active[df_active["client_code"]==code].copy()

            # SIP Holdings
            st.subheader("Active SIPs")
            if c_hold.empty:
                st.info(f"No active SIPs for {sel}.")
            else:
                today = pd.Timestamp.today()
                c_hold["invested_val"] = calc_invested_series(
                    c_hold["start_date"], c_hold["sip_amount"],
                    c_hold["first_order"].fillna("N"), today
                )
                c_hold["next_sip"] = c_hold["sip_day"].apply(get_next_sip_date)
                total_inv  = c_hold["invested_val"].sum()
                next_dates = pd.to_datetime(c_hold["next_sip"], format="%d %b %Y", errors="coerce")

                m1,m2,m3 = st.columns(3)
                m1.metric("Total Invested", format_currency(total_inv))
                m2.metric("Active SIPs", len(c_hold))
                m3.metric("Next SIP", next_dates.min().strftime("%d %b %Y")
                          if pd.notna(next_dates.min()) else "N/A")

                disp = c_hold[["scheme_name","amc","folio_no","sip_amount",
                               "start_date","invested_val","next_sip","first_order"]].copy()
                disp["Start"]    = pd.to_datetime(disp["start_date"], errors="coerce").dt.strftime("%d %b %Y")
                disp["SIP Amt"]  = disp["sip_amount"].apply(format_currency)
                disp["Invested"] = disp["invested_val"].apply(format_currency)
                disp["FO"]       = disp["first_order"].apply(lambda x: "Yes" if str(x).upper()=="Y" else "No")

                total_rows = len(disp)
                page = st.number_input("Page", 1, max(1,-(-total_rows//PAGE_SIZE)), 1)
                s, e = (page-1)*PAGE_SIZE, page*PAGE_SIZE
                st.caption(f"Showing {s+1}-{min(e,total_rows)} of {total_rows}")
                st.dataframe(
                    disp.iloc[s:e][["scheme_name","amc","folio_no","SIP Amt","Start","Invested","next_sip","FO"]]
                    .rename(columns={"scheme_name":"Scheme","amc":"AMC",
                                     "folio_no":"Folio","next_sip":"Next SIP","FO":"First Order"}),
                    use_container_width=True, hide_index=True
                )
                st.download_button("Export Holdings (CSV)",
                    c_hold.to_csv(index=False).encode(), f"{sel}_holdings.csv", "text/csv")

            # CAMS Brokerage
            st.divider()
            st.subheader("Brokerage Generated (CAMS)")
            cams_client = load_cams_by_client(code)

            if cams_client.empty:
                st.info(
                    "No CAMS brokerage found for this client.\n\n"
                    "Possible reasons: CAMS file not uploaded, or client name / "
                    "folio does not match any CAMS entry."
                )
            else:
                total_cams   = cams_client["brkage_amt"].sum()
                months_avail = sorted(cams_client["accrual_month"].unique(), reverse=True)

                bm1,bm2,bm3 = st.columns(3)
                bm1.metric("Total Brokerage", format_brokerage(total_cams))
                bm2.metric("Months", len(months_avail))
                bm3.metric("Transactions", len(cams_client))

                sel_month = st.selectbox("Filter by Month", ["All"]+months_avail,
                                         key="cams_client_month")
                vdf = cams_client if sel_month=="All" else \
                      cams_client[cams_client["accrual_month"]==sel_month]

                disp2 = vdf[["accrual_month","amc_name","folio_no","trxn_type",
                              "plot_amount","brkage_amt","brkage_rate","inv_name"]].copy()
                disp2["plot_amount"] = disp2["plot_amount"].apply(
                    lambda x: format_currency(float(x or 0)))
                disp2["brkage_amt"]  = disp2["brkage_amt"].apply(
                    lambda x: format_brokerage(float(x or 0)))
                disp2["brkage_rate"] = disp2["brkage_rate"].apply(
                    lambda x: f"{float(x or 0):.4f}%")
                disp2.columns = ["Month","AMC","Folio","Txn Type",
                                  "Txn Amount","Brokerage","Rate","Investor Name"]
                st.dataframe(disp2, use_container_width=True, hide_index=True)
                st.download_button("Export Client Brokerage (CSV)",
                    vdf.to_csv(index=False).encode(), f"{sel}_brokerage.csv", "text/csv")


# ==================== EARNINGS ====================
elif mode == "Earnings":
    st.header("Brokerage Earnings")

    client_amcs   = sorted(df_active["amc"].unique().tolist()) if not df_active.empty else []
    current_month = datetime.now().strftime("%b")
    current_year  = datetime.now().year
    cams_month_str = f"{current_year}-{datetime.now().month:02d}"

    # Manual entry form
    if not client_amcs:
        st.info("No active SIPs to record brokerage for.")
    else:
        st.subheader("Record / Update Brokerage")
        c1,c2,c3 = st.columns(3)
        sel_amc = c1.selectbox("AMC", client_amcs, index=None, placeholder="Choose AMC...")
        month   = c2.selectbox("Month", MONTHS, index=MONTHS.index(current_month))
        year    = c3.number_input("Year", 2024, 2030, current_year)
        amt = st.number_input(
            "Brokerage Amount (Rs)",
            min_value=0.0,
            step=0.01,  # Allow paise-level precision
            format="%.2f",  # Display 2 decimal places in UI
            help="Enter exact amount (e.g., 1234.56)"
        )
        notes   = st.text_input("Notes (optional)")

        col_save, col_del = st.columns(2)
        if col_save.button("Save / Update"):
            if not sel_amc:
                st.error("Select an AMC first.")
            else:
                with get_conn() as conn:
                    conn.execute(
                        "INSERT INTO monthly_brokerage (amc, month, year, amount, notes) "
                        "VALUES (?,?,?,?,?) "
                        "ON CONFLICT(amc, month, year) DO UPDATE "
                        "SET amount=excluded.amount, notes=excluded.notes",
                        (sel_amc, month, year, float(amt), notes)  # ← Ensure float(amt)
                    )
                st.success(f"Saved {format_manual_brokerage(amt)} for {sel_amc} - {month} {year}")
                st.cache_data.clear(); st.rerun()

        if col_del.button("Delete Entry"):
            if not sel_amc:
                st.error("Select an AMC first.")
            else:
                with get_conn() as conn:
                    conn.execute(
                        "DELETE FROM monthly_brokerage WHERE amc=? AND month=? AND year=?",
                        (sel_amc, month, year)
                    )
                st.success(f"Deleted {sel_amc} - {month} {year}")
                st.cache_data.clear(); st.rerun()

    st.divider()
    st.subheader(f"Reconciliation - {current_month} {current_year}")

    with get_conn() as conn:
        brok_df = pd.read_sql(
            "SELECT amc, amount FROM monthly_brokerage WHERE month=? AND year=?",
            conn, params=(current_month, current_year)
        )
    manual_map = dict(zip(brok_df["amc"], brok_df["amount"])) if not brok_df.empty else {}

    # CAMS totals for this month — keyed by display_name (or amc_code fallback)
    cams_df = load_cams_by_amc(cams_month_str)
    cams_by_display: dict[str,float] = {}
    cams_code_for_display: dict[str,str] = {}
    if not cams_df.empty:
        for _, r in cams_df.iterrows():
            dn = str(r["display_name"])
            cams_by_display[dn]      = cams_by_display.get(dn,0.0) + float(r["cams_brokerage"] or 0)
            cams_code_for_display[dn] = str(r["amc_code"])

    # Match each SIP AMC to a CAMS display_name by normalized name
    table_rows = []
    total_manual = total_cams_sum = 0.0
    seen_display = set()

    for amc in client_amcs:
        sip_ct  = int((df_active["amc"]==amc).sum())
        sip_vol = float(df_active.loc[df_active["amc"]==amc,"sip_amount"].sum())
        manual  = manual_map.get(amc)

        norm_amc = normalize_amc(amc)
        cams_val = 0.0
        matched_dn = ""
        for dn, cv in cams_by_display.items():
            if normalize_amc(dn) == norm_amc:
                cams_val   = cv
                matched_dn = dn
                seen_display.add(dn)
                break

        total_cams_sum += cams_val
        if manual: total_manual += manual
        diff = (manual or 0.0) - cams_val
        match_icon = (
            "Match" if cams_val and abs(diff) < 1.0
            else ("Check" if cams_val else "-")
        )

        table_rows.append({
            "AMC":            amc,
            "SIPs":           sip_ct,
            "Monthly Vol":    format_currency(sip_vol),
            "Manual Entry":   format_manual_brokerage(manual or 0),
            "CAMS Code":      cams_code_for_display.get(matched_dn, "-"),
            "CAMS Amount":    format_brokerage(cams_val) if cams_val else "-",
            "Difference":     format_manual_brokerage(diff) if cams_val else "-",
            "Status":         match_icon,
            "Entry Status":   "Entered" if manual is not None else "Pending",
        })

    # Orphan CAMS codes not matched to any SIP AMC
    for dn, cv in cams_by_display.items():
        if dn not in seen_display:
            total_cams_sum += cv
            table_rows.append({
                "AMC":          f"[CAMS only] {dn}",
                "SIPs":         "-", "Monthly Vol": "-", "Manual Entry": "-",
                "CAMS Code":    cams_code_for_display.get(dn,"-"),
                "CAMS Amount":  format_brokerage(cv),
                "Difference":   "-", "Status": "No SIP AMC",
                "Entry Status": "-",
            })

    if table_rows:
        st.dataframe(pd.DataFrame(table_rows), use_container_width=True, hide_index=True)
        tm1,tm2,tm3 = st.columns(3)
        tm1.metric("Total Manual", format_manual_brokerage(total_manual))
        tm2.metric("Total CAMS", format_brokerage(total_cams_sum))  # CAMS keeps 4 decimals
        tm3.metric("Difference", format_manual_brokerage(total_manual - total_cams_sum))
        st.download_button("Export Reconciliation (CSV)",
            pd.DataFrame(table_rows).to_csv(index=False).encode(),
            f"brokerage_{current_month}_{current_year}.csv", "text/csv")
    else:
        st.info("No data yet for this month.")

    # Browse all CAMS months
    all_months = load_cams_months()
    if all_months:
        st.divider()
        st.subheader("CAMS Brokerage by Accrual Month")
        sel_cm = st.selectbox("Accrual Month", all_months, key="earn_cams_month")
        mdf = load_cams_by_amc(sel_cm)
        if not mdf.empty:
            mdf2 = mdf.copy()
            mdf2["cams_brokerage"] = mdf2["cams_brokerage"].apply(format_brokerage)
            st.dataframe(mdf2.rename(columns={
                "amc_code":"Code","display_name":"AMC",
                "cams_brokerage":"Brokerage","folio_count":"Folios","txn_count":"Txns"
            }), use_container_width=True, hide_index=True)
            st.metric("Total", format_brokerage(mdf["cams_brokerage"].sum()))


# ==================== ADMIN PANEL ====================
elif mode == "Admin Panel":
    st.header("Admin Panel")
    tab1,tab2,tab3,tab4,tab5 = st.tabs([
        "Import Data", "AMC Config", "CAMS AMC Map", "Brokerage Upload", "DB Info"
    ])

    with tab1:
        st.subheader("Import Client Master")
        f1 = st.file_uploader("Client Master Excel", type=["xlsx"], key="client_file")
        r1 = st.checkbox("Replace existing clients", key="replace_clients")
        if st.button("Import Clients") and f1:
            with st.spinner("Importing..."):
                ok, msg = parse_client_master(f1, r1)
            (st.success if ok else st.error)(msg)
            if ok: st.cache_data.clear()

        st.divider()
        st.subheader("Import SIP Report")
        f2 = st.file_uploader("SIP Report Excel", type=["xlsx"], key="sip_file")
        r2 = st.checkbox("Replace existing holdings", key="replace_holdings")
        if st.button("Import SIPs") and f2:
            with st.spinner("Importing..."):
                ok, msg, preview = parse_sip_report(f2, r2)
            (st.success if ok else st.error)(msg)
            if preview:
                cols = st.columns(3)
                if "active"      in preview: cols[0].metric("Active", preview["active"])
                if "cancelled"   in preview: cols[1].metric("Cancelled", preview["cancelled"])
                if "first_order" in preview: cols[2].metric("First Order=Y", preview["first_order"])
            if ok: st.cache_data.clear()

    with tab2:
        st.subheader("Enable / Disable AMCs")
        with get_conn() as conn:
            amc_cfg = pd.read_sql("SELECT amc, is_enabled FROM amc_config ORDER BY amc", conn)
        if amc_cfg.empty:
            st.info("No AMCs found - run AMFI sync first (restart app).")
        else:
            updated = {}
            for _, row in amc_cfg.iterrows():
                updated[row["amc"]] = st.checkbox(
                    row["amc"], value=bool(row["is_enabled"]), key=f"amc_{row['amc']}")
            if st.button("Save AMC Config"):
                with get_conn() as conn:
                    for amc, en in updated.items():
                        conn.execute("UPDATE amc_config SET is_enabled=? WHERE amc=?",
                                     (1 if en else 0, amc))
                st.success("Saved."); st.cache_data.clear(); st.rerun()

    with tab3:
        st.subheader("CAMS AMC Code -> Display Name")
        st.caption(
            "CAMS uses short codes (PP, G, T...). Set the full AMC name here so "
            "the Earnings tab can match CAMS amounts to your manual entries.\n\n"
            "Example: PP -> PPFAS Mutual Fund, G -> Mirae Asset Mutual Fund"
        )
        amc_map = load_amc_map()
        if not amc_map:
            st.info("No CAMS codes found. Upload a CAMS file first (Brokerage Upload tab).")
        else:
            updated_map = {}
            for code, current_name in amc_map.items():
                updated_map[code] = st.text_input(
                    f"Code: {code}", value=current_name,
                    placeholder="e.g. PPFAS Mutual Fund", key=f"map_{code}"
                )
            if st.button("Save Mapping"):
                with get_conn() as conn:
                    for code, name in updated_map.items():
                        conn.execute(
                            "UPDATE cams_amc_map SET display_name=? WHERE amc_code=?",
                            (name.strip(), code)
                        )
                st.success("Mapping saved.")
                st.cache_data.clear(); st.rerun()

    with tab4:
        st.subheader("CAMS Brokerage File Upload")
        st.caption(
            "Upload the CAMS brokerage CSV. Comma-separated, single-quoted strings "
            "are handled automatically. Duplicates (same TRXN_NO + FOLIO + Month) are skipped."
        )
        cams_file   = st.file_uploader("CAMS Brokerage CSV", type=["csv","txt","tsv"], key="cams_file")
        replace_cams = st.checkbox("Replace ALL existing CAMS data", key="replace_cams")

        if st.button("Upload & Process") and cams_file:
            with st.spinner("Parsing..."):
                ok, msg, preview = parse_cams_brokerage(cams_file, replace_cams)
            (st.success if ok else st.error)(msg)
            if not ok and preview.get("found_columns"):
                with st.expander("Columns found in file"):
                    st.write(preview["found_columns"])
            if ok and preview:
                p1,p2,p3 = st.columns(3)
                p1.metric("Rows Imported", preview.get("rows",0))
                p2.metric("Total Brokerage", format_brokerage(preview.get("total_brokerage",0)))
                p3.metric("Skipped", preview.get("skipped",0))
                if preview.get("months"):
                    st.info(f"Accrual months: {', '.join(preview['months'])}")
                st.info("Go to CAMS AMC Map tab to assign full names to the short codes.")
                st.cache_data.clear()

        st.divider()
        with get_conn() as conn:
            cams_summary = pd.read_sql(
                "SELECT accrual_month AS Month, COUNT(*) AS Rows, "
                "COUNT(DISTINCT folio_no) AS Folios, "
                "ROUND(SUM(brkage_amt),4) AS 'Total Brokerage', "
                "upload_batch AS Batch "
                "FROM cams_brokerage GROUP BY accrual_month, upload_batch "
                "ORDER BY accrual_month DESC", conn
            )
        if cams_summary.empty:
            st.info("No CAMS data uploaded yet.")
        else:
            st.dataframe(cams_summary, use_container_width=True, hide_index=True)
        if st.button("Clear All CAMS Data"):
            with get_conn() as conn:
                conn.execute("DELETE FROM cams_brokerage")
            st.warning("All CAMS data deleted.")
            st.cache_data.clear(); st.rerun()

    with tab5:
        st.subheader("Database Stats")
        with get_conn() as conn:
            for table, label in [
                ("clients","Clients"), ("holdings","Holdings"),
                ("amc_schemes","Schemes"), ("monthly_brokerage","Manual Brokerage"),
                ("cams_brokerage","CAMS Rows"), ("cams_amc_map","CAMS AMC Codes"),
            ]:
                n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                st.metric(label, n)

        if st.button("Clear All Holdings"):
            with get_conn() as conn:
                conn.execute("DELETE FROM holdings")
            st.warning("All holdings deleted.")
            st.cache_data.clear(); st.rerun()