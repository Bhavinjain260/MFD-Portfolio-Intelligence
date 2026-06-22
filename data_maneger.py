"""
data_manager.py
All Upload / Parse / Delete logic for BSE, CAMS, and KFinTech data.
Import this module in app.py and call render_data_manager() inside the Admin Panel tab.
"""

import logging
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime

import pandas as pd
import streamlit as st

log = logging.getLogger(__name__)

# ── re-exported from main app (pass DB_PATH at init) ──────────────────────────
_DB_PATH = "mfd_local.db"


def set_db_path(path: str):
    global _DB_PATH
    _DB_PATH = path


@contextmanager
def get_conn():
    conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _sync_rta(amc_codes: list[str], rta: str) -> None:
    """After a successful upload, upsert RTA into amc_config for every AMC code seen."""
    if not amc_codes:
        return
    with get_conn() as conn:
        conn.executemany(
            "INSERT INTO amc_config (amc, rta, is_enabled) VALUES (?,?,1) "
            "ON CONFLICT(amc) DO UPDATE SET rta=excluded.rta",
            [(code, rta) for code in amc_codes if code],
        )



# ══════════════════════════════════════════════════════════════════════════════
# PURE HELPERS
# ══════════════════════════════════════════════════════════════════════════════

CANCELLED_KEYWORDS = frozenset(
    ["CXL", "AUTOCXL", "AUTO CXL", "CX", "CANCEL", "CLOSED", "REDEEM", "STOPPED", "FAILED"]
)
AMC_SUFFIXES = [" MUTUAL FUND", " MF", " FUND", " AMC", " INDIA", " MANAGEMENT", " LTD", " LIMITED"]
_WS = re.compile(r"\s+")

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


def clean_str(val) -> str:
    if val is None:
        return ""
    try:
        if pd.isna(val):
            return ""
    except (TypeError, ValueError):
        pass
    s = str(val).strip()
    return "" if s.lower() in {"nan", "none", "null", "na", ""} else s


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


def format_currency(val, decimals: int = 2) -> str:
    try:
        return f"Rs {float(val):,.{decimals}f}"
    except (TypeError, ValueError):
        return "Rs -"


def format_brokerage(val) -> str:
    try:
        amount = float(val)
        formatted = f"{amount:.8f}".rstrip("0").rstrip(".")
        return f"Rs {formatted}"
    except (TypeError, ValueError):
        return "Rs -"


@st.cache_data(show_spinner=False)
def normalize_amc(name: str) -> str:
    if not name:
        return ""
    n = str(name).strip().upper()
    for s in AMC_SUFFIXES:
        n = n.replace(s, "")
    return _WS.sub(" ", n).strip()


def get_rta(amc_name: str) -> str:
    norm = normalize_amc(amc_name)
    if norm in CAMS_AMCS:
        return "CAMS"
    if norm in KFIN_AMCS:
        return "KFinTech"
    return "Unknown"


def normalize_folio(folio: str) -> str:
    if not folio:
        return ""
    try:
        if pd.isna(folio):
            return ""
    except Exception:
        pass
    return str(folio).strip().split("/")[0].strip().lower()


def is_active_status(raw_status) -> bool:
    if pd.isna(raw_status):
        return True
    status = str(raw_status).strip().upper()
    return not any(kw in status for kw in CANCELLED_KEYWORDS)


def parse_date_safe(val) -> str:
    if isinstance(val, float) and pd.isna(val):
        return ""
    if str(val).strip() in {"", "None", "NaN"}:
        return ""
    try:
        dt = pd.to_datetime(val, dayfirst=True, errors="coerce")
        return dt.strftime("%Y-%m-%d") if pd.notna(dt) else ""
    except Exception:
        return ""


def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    df.columns = df.columns.str.strip().str.lower().str.replace(r"[\s.\-_]+", "_", regex=True)
    return df


def _clean_cols(df: pd.DataFrame) -> pd.DataFrame:
    df.columns = [
        c.strip().replace("\u200b", "").replace("\ufeff", "").replace("\u00a0", "").strip("'\"").upper()
        for c in df.columns
    ]
    return df


def _read_csv_auto(file) -> pd.DataFrame | None:
    for sep in ("\t", ","):
        try:
            file.seek(0)
            df = pd.read_csv(file, sep=sep, quotechar="'", dtype=str, encoding="utf-8", encoding_errors="replace")
            if len(df.columns) > 5:
                return df
        except Exception:
            continue
    return None


def _sf(val) -> float:
    try:
        return float(str(val).replace(",", "").strip()) if str(val).strip() not in ("", "None", "NaN", "nan") else 0.0
    except Exception:
        return 0.0


# ══════════════════════════════════════════════════════════════════════════════
# BSE PARSERS
# ══════════════════════════════════════════════════════════════════════════════

def parse_bse_client_master(file, replace: bool) -> tuple[bool, str]:
    file.seek(0)
    try:
        df = pd.read_excel(file)
    except Exception as exc:
        return False, f"File read error: {exc}"
    df = normalize_columns(df)

    def _col(*candidates):
        return next((c for c in candidates if c in df.columns), None)

    code_col = _col("client_code", "member_code")
    fname_col = _col("primary_holder_first_name")
    lname_col = _col("primary_holder_last_name")
    pan_col = _col("primary_holder_pan")
    mobile_col = _col("indian_mobile_no_")
    email_col = _col("email", "primary_holder_email")
    date_col = _col("created_at")

    if not code_col or not fname_col:
        return False, "Missing critical columns: client_code / primary_holder_first_name"

    rows = []
    for _, row in df.iterrows():
        code = clean_str(row.get(code_col))
        if not code:
            continue
        fn = clean_str(row.get(fname_col, ""))
        ln = clean_str(row.get(lname_col, "")) if lname_col else ""
        name = f"{fn} {ln}".strip() or "Unknown"
        pan = clean_str(row.get(pan_col, "")).upper()
        mob = clean_str(row.get(mobile_col, ""))
        mail = clean_str(row.get(email_col, "")).lower()
        dt = parse_date_safe(row.get(date_col)) or datetime.now().strftime("%Y-%m-%d")
        rows.append((code, name, pan, mob, mail, "Verified", dt))

    if not rows:
        return False, "No valid rows found"

    inserted = skipped = 0
    with get_conn() as conn:
        if replace:
            conn.execute("DELETE FROM clients")
            conn.executemany(
                "INSERT INTO clients (client_code,name,pan,mobile,email,kyc_status,start_date) VALUES (?,?,?,?,?,?,?)",
                rows)
            inserted = len(rows)
        else:
            existing = {r[0] for r in conn.execute("SELECT client_code FROM clients").fetchall()}
            new_rows = [r for r in rows if r[0] not in existing]
            skipped = len(rows) - len(new_rows)
            if new_rows:
                conn.executemany(
                    "INSERT OR IGNORE INTO clients (client_code,name,pan,mobile,email,kyc_status,start_date) VALUES (?,?,?,?,?,?,?)",
                    new_rows)
            inserted = len(new_rows)

    msg = f"Imported {inserted} clients"
    if skipped:
        msg += f" | Skipped {skipped} already existing"
    return True, msg


def parse_bse_sip_report(file, replace: bool) -> tuple[bool, str, dict]:
    file.seek(0)
    try:
        df = pd.read_excel(file)
    except Exception as exc:
        return False, f"File read error: {exc}", {}
    df = normalize_columns(df)
    required = ["client_code", "folio_no", "scheme_name", "amc_name"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        return False, f"Missing columns: {missing}", {}

    preview = {}
    if "status" in df.columns:
        active_count = df["status"].apply(is_active_status).sum()
        preview["active"] = int(active_count)
        preview["cancelled"] = len(df) - int(active_count)
    if "first_order" in df.columns:
        preview["first_order"] = int((df["first_order"].astype(str).str.upper() == "Y").sum())

    rows, skipped = [], 0
    for _, row in df.iterrows():
        if not is_active_status(row.get("status", "ACTIVE")):
            skipped += 1
            continue
        try:
            fo = clean_str(row.get("first_order", "N")).upper()
            if fo not in ("Y", "N"):
                fo = "N"
            sip_day = 1
            sd = parse_date_safe(row.get("start_date"))
            if sd:
                try:
                    sip_day = pd.to_datetime(sd).day
                except Exception:
                    pass
            amc_raw = clean_str(row["amc_name"])
            rows.append((
                clean_str(row["client_code"]), clean_str(row["folio_no"]),
                clean_str(row.get("rta_scheme_code", "")), clean_str(row["scheme_name"]),
                amc_raw, get_rta(amc_raw), clean_str(row.get("frequency_type", "")),
                float(row.get("installments_amt") or 0), sip_day, sd,
                parse_date_safe(row.get("end_date")), "Active", fo,
            ))
        except Exception as exc:
            log.warning("Skipped SIP row: %s", exc)
            skipped += 1

    if not rows:
        return False, "0 SIPs imported (all skipped or invalid)", preview

    inserted = duplicate_skipped = 0
    with get_conn() as conn:
        if replace:
            conn.execute("DELETE FROM holdings")
            conn.executemany(
                "INSERT INTO holdings (client_code,folio_no,scheme_code,scheme_name,amc,rta,investment_type,sip_amount,sip_day,start_date,end_date,status,first_order) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                    new_rows.append(r)
                    existing.add(key)
            if new_rows:
                conn.executemany(
                    "INSERT INTO holdings (client_code,folio_no,scheme_code,scheme_name,amc,rta,investment_type,sip_amount,sip_day,start_date,end_date,status,first_order) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    new_rows)
            inserted = len(new_rows)

    msg = f"Imported {inserted} Active SIPs"
    if skipped:
        msg += f" | Skipped {skipped} cancelled/invalid"
    if duplicate_skipped:
        msg += f" | Skipped {duplicate_skipped} already existing"
    return True, msg, preview


# ══════════════════════════════════════════════════════════════════════════════
# CAMS PARSERS
# ══════════════════════════════════════════════════════════════════════════════

def parse_cams_brokerage(file, replace: bool) -> tuple[bool, str, dict]:
    df = _read_csv_auto(file)
    if df is None:
        return False, "Could not parse file", {}
    df = _clean_cols(df)
    required = ["FOLIO_NO", "BRKAGE_AMT", "AMC_CODE"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        return False, f"Missing columns: {missing}. Found: {list(df.columns)[:15]}", {}

    batch_id = f"{file.name}_{datetime.now().strftime('%Y%m%d%H%M%S')}"

    def _accrual(val) -> str:
        try:
            dt = pd.to_datetime(val, errors="coerce")
            return dt.strftime("%Y-%m") if pd.notna(dt) else ""
        except Exception:
            return ""

    rows, skipped = [], 0
    for _, row in df.iterrows():
        try:
            rows.append((
                clean_str(row.get("AMC_CODE", "")), clean_str(row.get("FOLIO_NO", "")),
                clean_str(row.get("SCHEME_CODE", row.get("TXN_SCH_CODE", ""))),
                clean_str(row.get("TRXN_NO", "")), clean_str(row.get("TRXN_TYPE", "")),
                _sf(row.get("BRKAGE_AMT", 0)), clean_str(row.get("BRKAGE_TYPE", "")),
                _sf(row.get("BRKAGE_RATE", 0)), clean_str(row.get("INV_NAME", "")).strip(),
                parse_date_safe(row.get("PROC_DATE", "")),
                _accrual(row.get("BROKERAGE_ACRUAL_MONTH", "")),
                _sf(row.get("PLOT_AMOUNT", 0)), _sf(row.get("AVG_ASSETS", 0)),
                _sf(row.get("IGST_VALUE", 0)), _sf(row.get("CGST_VALUE", 0)),
                _sf(row.get("SGST_VALUE", 0)), batch_id,
            ))
        except Exception as exc:
            log.warning("Skipped CAMS brokerage row: %s", exc)
            skipped += 1

    if not rows:
        return False, "0 rows imported", {}

    inserted = duplicate_skipped = 0
    with get_conn() as conn:
        if replace:
            conn.execute("DELETE FROM cams_brokerage")
            conn.executemany(
                "INSERT OR IGNORE INTO cams_brokerage (amc_code,folio_no,scheme_code,trxn_no,trxn_type,brkage_amt,brkage_type,brkage_rate,inv_name,proc_date,accrual_month,plot_amount,avg_assets,igst_value,cgst_value,sgst_value,upload_batch) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows)
            inserted = len(rows)
        else:
            before = conn.execute("SELECT COUNT(*) FROM cams_brokerage").fetchone()[0]
            conn.executemany(
                "INSERT OR IGNORE INTO cams_brokerage (amc_code,folio_no,scheme_code,trxn_no,trxn_type,brkage_amt,brkage_type,brkage_rate,inv_name,proc_date,accrual_month,plot_amount,avg_assets,igst_value,cgst_value,sgst_value,upload_batch) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows)
            after = conn.execute("SELECT COUNT(*) FROM cams_brokerage").fetchone()[0]
            inserted = after - before
            duplicate_skipped = len(rows) - inserted

    total = sum(r[5] for r in rows)
    preview = {"rows": inserted, "skipped": skipped, "duplicates": duplicate_skipped, "total_brokerage": total,
               "months": sorted({r[10] for r in rows if r[10]})}
    msg = f"Imported {inserted} CAMS rows | Rs {total:,.2f} brokerage"
    if skipped: msg += f" | Skipped {skipped} invalid"
    if duplicate_skipped: msg += f" | Skipped {duplicate_skipped} duplicates"
    return True, msg, preview


def parse_cams_aum(file, replace: bool) -> tuple[bool, str, dict]:
    df = _read_csv_auto(file)
    if df is None:
        return False, "Could not parse AUM file", {}
    df = _clean_cols(df)
    required = ["FOLIOCHK", "RUPEE_BAL"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        return False, f"Missing AUM columns: {missing}. Found: {list(df.columns)[:15]}", {}

    batch_id = f"{file.name}_{datetime.now().strftime('%Y%m%d%H%M%S')}"
    rows, skipped = [], 0
    for _, row in df.iterrows():
        folio = clean_str(row.get("FOLIOCHK", ""))
        if not folio:
            skipped += 1
            continue
        try:
            rows.append((
                folio, clean_str(row.get("INV_NAME", "")).strip(),
                clean_str(row.get("SCH_NAME", "")), clean_str(row.get("AMC_CODE", "")),
                clean_str(row.get("PAN_NO", "")).upper(), clean_str(row.get("EMAIL", "")).lower(),
                parse_date_safe(row.get("REP_DATE", "")),
                _sf(row.get("CLOS_BAL", 0)), _sf(row.get("RUPEE_BAL", 0)), batch_id,
            ))
        except Exception as exc:
            log.warning("Skipped CAMS AUM row: %s", exc)
            skipped += 1

    if not rows:
        return False, "0 AUM rows imported", {}

    inserted = duplicate_skipped = 0
    with get_conn() as conn:
        if replace:
            conn.execute("DELETE FROM cams_aum")
            conn.executemany(
                "INSERT OR IGNORE INTO cams_aum (folio_no,inv_name,scheme_name,amc_code,pan_no,email,rep_date,units,rupee_bal,upload_batch) VALUES (?,?,?,?,?,?,?,?,?,?)",
                rows)
            inserted = len(rows)
        else:
            before = conn.execute("SELECT COUNT(*) FROM cams_aum").fetchone()[0]
            conn.executemany(
                "INSERT OR IGNORE INTO cams_aum (folio_no,inv_name,scheme_name,amc_code,pan_no,email,rep_date,units,rupee_bal,upload_batch) VALUES (?,?,?,?,?,?,?,?,?,?)",
                rows)
            after = conn.execute("SELECT COUNT(*) FROM cams_aum").fetchone()[0]
            inserted = after - before
            duplicate_skipped = len(rows) - inserted

    total_aum = sum(r[8] for r in rows)
    preview = {"rows": inserted, "skipped": skipped, "duplicates": duplicate_skipped, "total_aum": total_aum}
    msg = f"Imported {inserted} AUM rows | Total AUM: {format_aum(total_aum)}"
    if skipped: msg += f" | Skipped {skipped}"
    if duplicate_skipped: msg += f" | Skipped {duplicate_skipped} duplicates"
    return True, msg, preview


def parse_cams_transactions(file, replace: bool) -> tuple[bool, str, dict]:
    df = _read_csv_auto(file)
    if df is None:
        return False, "Could not parse R2 transaction file", {}
    df = _clean_cols(df)
    required = ["FOLIO_NO", "TRXNNO", "AMOUNT"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        return False, f"Missing columns: {missing}. Found: {list(df.columns)[:20]}", {}

    batch_id = f"R2_{datetime.now().strftime('%Y%m%d%H%M%S')}"
    rows, skipped = [], 0
    for _, row in df.iterrows():
        try:
            rows.append((
                clean_str(row.get("AMC_CODE", "")), clean_str(row.get("FOLIO_NO", "")),
                clean_str(row.get("PRODCODE", "")), clean_str(row.get("SCHEME", "")),
                clean_str(row.get("INV_NAME", "")).strip(), clean_str(row.get("TRXNTYPE", "")),
                clean_str(row.get("TRXNNO", "")), clean_str(row.get("TRXNMODE", "")),
                clean_str(row.get("TRXNSTAT", "")),
                parse_date_safe(row.get("TRADDATE", "")), parse_date_safe(row.get("POSTDATE", "")),
                _sf(row.get("PURPRICE", 0)), _sf(row.get("UNITS", 0)), _sf(row.get("AMOUNT", 0)),
                clean_str(row.get("PAN", "")), clean_str(row.get("REMARKS", "")),
                clean_str(row.get("SIPTRXNNO", "")),
                _sf(row.get("IGST_AMOUNT", 0)), _sf(row.get("CGST_AMOUNT", 0)), _sf(row.get("SGST_AMOUNT", 0)),
                parse_date_safe(row.get("REP_DATE", "")), batch_id,
            ))
        except Exception as exc:
            log.warning("Skipped R2 row: %s", exc)
            skipped += 1

    if not rows:
        return False, "0 transaction rows parsed", {}

    inserted = dupes = 0
    with get_conn() as conn:
        if replace:
            conn.execute("DELETE FROM cams_transactions")
            conn.executemany(
                "INSERT OR IGNORE INTO cams_transactions "
                "(amc_code,folio_no,scheme_code,scheme_name,inv_name,trxn_type,trxn_no,trxn_mode,trxn_status,"
                "trade_date,post_date,nav,units,amount,pan,remarks,sip_trxn_no,"
                "igst_amount,cgst_amount,sgst_amount,rep_date,upload_batch) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows)
            inserted = len(rows)
        else:
            before = conn.execute("SELECT COUNT(*) FROM cams_transactions").fetchone()[0]
            conn.executemany(
                "INSERT OR IGNORE INTO cams_transactions "
                "(amc_code,folio_no,scheme_code,scheme_name,inv_name,trxn_type,trxn_no,trxn_mode,trxn_status,"
                "trade_date,post_date,nav,units,amount,pan,remarks,sip_trxn_no,"
                "igst_amount,cgst_amount,sgst_amount,rep_date,upload_batch) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows)
            after = conn.execute("SELECT COUNT(*) FROM cams_transactions").fetchone()[0]
            inserted = after - before
            dupes = len(rows) - inserted

    total_amt = sum(r[13] for r in rows)
    preview = {"rows": inserted, "skipped": skipped, "duplicates": dupes, "total_amount": total_amt,
               "schemes": len({r[3] for r in rows}), "folios": len({r[1] for r in rows})}
    msg = f"Imported {inserted} transactions | Rs {total_amt:,.2f} total"
    if skipped: msg += f" | Skipped {skipped}"
    if dupes: msg += f" | {dupes} duplicates"
    return True, msg, preview


def parse_cams_folio_master(file, replace: bool) -> tuple[bool, str, dict]:
    df = _read_csv_auto(file)
    if df is None:
        return False, "Could not parse R9 folio master file", {}
    df = _clean_cols(df)
    required = ["FOLIOCHK", "INV_NAME"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        return False, f"Missing columns: {missing}. Found: {list(df.columns)[:20]}", {}

    batch_id = f"R9_{datetime.now().strftime('%Y%m%d%H%M%S')}"
    rows, skipped = [], 0
    for _, row in df.iterrows():
        folio = clean_str(row.get("FOLIOCHK", ""))
        if not folio:
            skipped += 1
            continue
        try:
            rows.append((
                folio, clean_str(row.get("INV_NAME", "")).strip(),
                clean_str(row.get("ADDRESS1", "")), clean_str(row.get("ADDRESS2", "")),
                clean_str(row.get("ADDRESS3", "")), clean_str(row.get("CITY", "")),
                clean_str(row.get("PINCODE", "")), clean_str(row.get("PRODUCT", "")),
                clean_str(row.get("SCH_NAME", "")), clean_str(row.get("AMC_CODE", "")),
                parse_date_safe(row.get("REP_DATE", "")),
                _sf(row.get("CLOS_BAL", 0)), _sf(row.get("RUPEE_BAL", 0)),
                clean_str(row.get("EMAIL", "")).lower(), clean_str(row.get("MOBILE_NO", "")),
                clean_str(row.get("PAN_NO", "")).upper(),
                clean_str(row.get("JOINT1_PAN", "")).upper(), clean_str(row.get("JOINT2_PAN", "")).upper(),
                clean_str(row.get("TAX_STATUS", "")), clean_str(row.get("HOLDING_NATURE", "")),
                clean_str(row.get("BANK_NAME", "")), clean_str(row.get("BRANCH", "")),
                clean_str(row.get("AC_TYPE", "")), clean_str(row.get("AC_NO", "")),
                clean_str(row.get("IFSC_CODE", "")), parse_date_safe(row.get("INV_DOB", "")),
                clean_str(row.get("NOM_NAME", "")), clean_str(row.get("RELATION", "")),
                parse_date_safe(row.get("FOLIO_DATE", "")), batch_id,
            ))
        except Exception as exc:
            log.warning("Skipped R9 row: %s", exc)
            skipped += 1

    if not rows:
        return False, "0 folio master rows parsed", {}

    inserted = dupes = 0
    with get_conn() as conn:
        if replace:
            conn.execute("DELETE FROM cams_folio_master")
            conn.executemany(
                "INSERT OR IGNORE INTO cams_folio_master "
                "(folio_no,inv_name,address1,address2,address3,city,pincode,scheme_code,scheme_name,amc_code,"
                "rep_date,units,rupee_bal,email,mobile,pan_no,joint1_pan,joint2_pan,tax_status,holding_nature,"
                "bank_name,branch,ac_type,ac_no,ifsc_code,inv_dob,nominee_name,nominee_relation,folio_date,upload_batch) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows)
            inserted = len(rows)
        else:
            before = conn.execute("SELECT COUNT(*) FROM cams_folio_master").fetchone()[0]
            conn.executemany(
                "INSERT OR IGNORE INTO cams_folio_master "
                "(folio_no,inv_name,address1,address2,address3,city,pincode,scheme_code,scheme_name,amc_code,"
                "rep_date,units,rupee_bal,email,mobile,pan_no,joint1_pan,joint2_pan,tax_status,holding_nature,"
                "bank_name,branch,ac_type,ac_no,ifsc_code,inv_dob,nominee_name,nominee_relation,folio_date,upload_batch) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows)
            after = conn.execute("SELECT COUNT(*) FROM cams_folio_master").fetchone()[0]
            inserted = after - before
            dupes = len(rows) - inserted

    total_aum = sum(r[12] for r in rows)
    preview = {"rows": inserted, "skipped": skipped, "duplicates": dupes, "total_aum": total_aum,
               "unique_folios": len({r[0] for r in rows}), "unique_investors": len({r[1] for r in rows})}
    msg = f"Imported {inserted} folio records | AUM: {format_aum(total_aum)}"
    if skipped: msg += f" | Skipped {skipped}"
    if dupes: msg += f" | {dupes} duplicates"
    return True, msg, preview


def parse_cams_sip_master(file, replace: bool) -> tuple[bool, str, dict]:
    df = _read_csv_auto(file)
    if df is None:
        return False, "Could not parse R49 SIP master file", {}
    df = _clean_cols(df)
    required = ["FOLIO_NO", "AUTO_TRNO", "AUTO_AMOUNT"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        return False, f"Missing columns: {missing}. Found: {list(df.columns)[:20]}", {}

    batch_id = f"R49_{datetime.now().strftime('%Y%m%d%H%M%S')}"

    def _sip_status(cease_str: str, to_str: str) -> str:
        if cease_str and cease_str.strip() not in ("", "None", "NaN"):
            return "Ceased"
        try:
            end = pd.to_datetime(to_str, dayfirst=True, errors="coerce")
            if pd.notna(end) and end.date() < datetime.now().date():
                return "Completed"
        except Exception:
            pass
        return "Active"

    rows, skipped = [], 0
    for _, row in df.iterrows():
        folio = clean_str(row.get("FOLIO_NO", ""))
        sip_no = clean_str(row.get("AUTO_TRNO", ""))
        if not folio or not sip_no:
            skipped += 1
            continue
        try:
            cease = clean_str(row.get("CEASE_DATE", ""))
            to_dt = clean_str(row.get("TO_DATE", ""))
            status = _sip_status(cease, to_dt)
            try:
                period_day = int(float(clean_str(row.get("PERIOD_DAY", "1")) or 1))
            except Exception:
                period_day = 1
            rows.append((
                clean_str(row.get("AMC_CODE", "")), clean_str(row.get("PRODUCT", "")),
                clean_str(row.get("SCHEME", "")), folio,
                clean_str(row.get("INV_NAME", "")).strip(), sip_no,
                _sf(row.get("AUTO_AMOUNT", 0)),
                parse_date_safe(row.get("FROM_DATE", "")), parse_date_safe(row.get("TO_DATE", "")),
                parse_date_safe(row.get("CEASE_DATE", "")), clean_str(row.get("PERIODICITY", "")),
                period_day, clean_str(row.get("PAN", "")).upper(),
                clean_str(row.get("PAYMENT_MODE", "")), clean_str(row.get("BANK", "")),
                parse_date_safe(row.get("REG_DATE", "")), clean_str(row.get("REMARKS", "")),
                status, batch_id,
            ))
        except Exception as exc:
            log.warning("Skipped R49 row: %s", exc)
            skipped += 1

    if not rows:
        return False, "0 SIP master rows parsed", {}

    inserted = dupes = 0
    with get_conn() as conn:
        if replace:
            conn.execute("DELETE FROM cams_sip_master")
            conn.executemany(
                "INSERT OR IGNORE INTO cams_sip_master "
                "(amc_code,scheme_code,scheme_name,folio_no,inv_name,sip_reg_no,sip_amount,"
                "from_date,to_date,cease_date,periodicity,sip_day,pan,payment_mode,bank_name,"
                "reg_date,remarks,status,upload_batch) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows)
            inserted = len(rows)
        else:
            before = conn.execute("SELECT COUNT(*) FROM cams_sip_master").fetchone()[0]
            conn.executemany(
                "INSERT OR IGNORE INTO cams_sip_master "
                "(amc_code,scheme_code,scheme_name,folio_no,inv_name,sip_reg_no,sip_amount,"
                "from_date,to_date,cease_date,periodicity,sip_day,pan,payment_mode,bank_name,"
                "reg_date,remarks,status,upload_batch) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows)
            after = conn.execute("SELECT COUNT(*) FROM cams_sip_master").fetchone()[0]
            inserted = after - before
            dupes = len(rows) - inserted

    sc = {}
    for r in rows:
        sc[r[17]] = sc.get(r[17], 0) + 1
    preview = {"rows": inserted, "skipped": skipped, "duplicates": dupes,
               "active": sc.get("Active", 0), "ceased": sc.get("Ceased", 0), "completed": sc.get("Completed", 0)}
    msg = f"Imported {inserted} SIPs | Active: {sc.get('Active', 0)} | Ceased: {sc.get('Ceased', 0)} | Completed: {sc.get('Completed', 0)}"
    if skipped: msg += f" | Skipped {skipped}"
    if dupes: msg += f" | {dupes} duplicates"
    return True, msg, preview


# ══════════════════════════════════════════════════════════════════════════════
# KFINTECH PARSERS
# ══════════════════════════════════════════════════════════════════════════════

def _kf_find_col(df, candidates):
    for c in candidates:
        matches = [col for col in df.columns if col.upper().replace(" ", "_") == c.upper().replace(" ", "_")]
        if matches:
            return matches[0]
        for col in df.columns:
            if c.upper() in col.upper() or col.upper() in c.upper():
                return col
    return None


def parse_kfintech_brokerage(file, replace: bool) -> tuple[bool, str, dict]:
    df = _read_csv_auto(file)
    if df is None:
        return False, "Could not parse KFinTech brokerage file", {}
    df = _clean_cols(df)

    col_mappings = {
        "amc_code": ["FUND", "AMC CODE", "AMC_CODE"],
        "folio_no": ["ACCOUNT NUMBER", "FOLIO NUMBER", "FOLIO_NO", "FOLIO"],
        "scheme_code": ["SCHEME CODE", "SCHEME_CODE", "SCH CODE"],
        "trxn_no": ["TRANSACTION NUMBER", "TRXN_NO", "TXN NO"],
        "trxn_type": ["TRANSACTION DESCRIPTION", "TRXN_TYPE", "TRANTYPECODE"],
        "brkage_amt": ["BROKERAGE (IN RS.)", "BROKERAGE", "BRKAGE_AMT", "GROSSBROKERAGE"],
        "brkage_type": ["BROKERAGE TYPE", "BRKAGE_TYPE"],
        "brkage_rate": ["PERCENTAGE (%)", "PERCENTAGE", "BRKAGE_RATE"],
        "inv_name": ["INVESTOR NAME", "INV_NAME", "NAME"],
        "proc_date": ["PROCESS DATE", "PROC_DATE"],
        "accrual_month": ["FROM DATE", "TO DATE", "STARTING DATE", "ENDING DATE"],
        "plot_amount": ["AMOUNT (IN RS.)", "AMOUNT", "PLOT_AMOUNT"],
        "avg_assets": ["AVERAGE ASSETS", "AVG_ASSETS"],
    }
    resolved = {k: _kf_find_col(df, v) for k, v in col_mappings.items()}
    missing = [c for c in ["folio_no", "brkage_amt", "amc_code"] if resolved[c] is None]
    if missing:
        return False, f"Missing columns: {missing}. Found: {list(df.columns)[:15]}", {}

    batch_id = f"KF_{file.name}_{datetime.now().strftime('%Y%m%d%H%M%S')}"

    def _accrual(val) -> str:
        try:
            dt = pd.to_datetime(val, errors="coerce", dayfirst=True)
            return dt.strftime("%Y-%m") if pd.notna(dt) else ""
        except Exception:
            return ""

    rows, skipped = [], 0
    for _, row in df.iterrows():
        try:
            rows.append((
                clean_str(row.get(resolved["amc_code"], "")), clean_str(row.get(resolved["folio_no"], "")),
                clean_str(row.get(resolved["scheme_code"], "")), clean_str(row.get(resolved["trxn_no"], "")),
                clean_str(row.get(resolved["trxn_type"], "")), _sf(row.get(resolved["brkage_amt"], 0)),
                clean_str(row.get(resolved["brkage_type"], "")), _sf(row.get(resolved["brkage_rate"], 0)),
                clean_str(row.get(resolved["inv_name"], "")).strip(),
                parse_date_safe(row.get(resolved["proc_date"], "")),
                _accrual(row.get(resolved["accrual_month"], "")),
                _sf(row.get(resolved["plot_amount"], 0)), _sf(row.get(resolved["avg_assets"], 0)),
                0.0, 0.0, 0.0, batch_id,
            ))
        except Exception as exc:
            log.warning("Skipped KFinTech brokerage row: %s", exc)
            skipped += 1

    if not rows:
        return False, "0 rows imported", {}

    inserted = duplicate_skipped = 0
    with get_conn() as conn:
        if replace:
            conn.execute("DELETE FROM kfintech_brokerage")
            conn.executemany(
                "INSERT OR IGNORE INTO kfintech_brokerage (amc_code,folio_no,scheme_code,trxn_no,trxn_type,brkage_amt,brkage_type,brkage_rate,inv_name,proc_date,accrual_month,plot_amount,avg_assets,igst_value,cgst_value,sgst_value,upload_batch) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows)
            inserted = len(rows)
        else:
            before = conn.execute("SELECT COUNT(*) FROM kfintech_brokerage").fetchone()[0]
            conn.executemany(
                "INSERT OR IGNORE INTO kfintech_brokerage (amc_code,folio_no,scheme_code,trxn_no,trxn_type,brkage_amt,brkage_type,brkage_rate,inv_name,proc_date,accrual_month,plot_amount,avg_assets,igst_value,cgst_value,sgst_value,upload_batch) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows)
            after = conn.execute("SELECT COUNT(*) FROM kfintech_brokerage").fetchone()[0]
            inserted = after - before
            duplicate_skipped = len(rows) - inserted

    total = sum(r[5] for r in rows)
    preview = {"rows": inserted, "skipped": skipped, "duplicates": duplicate_skipped, "total_brokerage": total,
               "months": sorted({r[10] for r in rows if r[10]})}
    msg = f"Imported {inserted} KFinTech rows | Rs {total:,.2f} brokerage"
    if skipped: msg += f" | Skipped {skipped} invalid"
    if duplicate_skipped: msg += f" | Skipped {duplicate_skipped} duplicates"
    return True, msg, preview


def parse_kfintech_aum(file, replace: bool) -> tuple[bool, str, dict]:
    df = _read_csv_auto(file)
    if df is None:
        return False, "Could not parse KFinTech AUM file", {}
    df.columns = [
        c.strip().replace("\u200b", "").replace("\ufeff", "").replace("\u00a0", "").strip("'\"").upper().replace("#",
                                                                                                                 "").replace(
            "  ", " ").strip()
        for c in df.columns
    ]
    col_mappings = {
        "folio_no": ["FOLIO NUMBER", "FOLIO_NO", "FOLIO", "ACCOUNT NUMBER"],
        "inv_name": ["INVESTOR NAME", "INV_NAME", "CLIENT NAME", "NAME"],
        "scheme_name": ["FUND DESCRIPTION", "SCHEME NAME", "DESCRIPTION"],
        "amc_code": ["FUND", "AMC CODE", "AMC_CODE", "FUND CODE"],
        "product_code": ["PRODUCT CODE", "PRODUCT_CODE", "PROD CODE"],
        "scheme_code": ["SCHEME CODE", "SCHEME_CODE", "SCH CODE"],
        "dividend_opt": ["DIVIDEND OPTION", "DIVIDEND_OPTION", "DIV OPT", "OPTION"],
        "email": ["EMAIL", "E-MAIL", "EMAIL ID"],
        "rep_date": ["REPORT DATE", "REPORT_DATE", "AS ON DATE", "DATE"],
        "units": ["BALANCE", "UNITS", "CLOS_BAL", "CLOSING BALANCE"],
        "rupee_bal": ["AUM", "RUPEE_BAL", "CURRENT VALUE", "MARKET VALUE", "VALUE"],
        "nav": ["NAV", "NET ASSET VALUE"],
    }
    resolved = {k: _kf_find_col(df, v) for k, v in col_mappings.items()}
    missing = [c for c in ["folio_no", "rupee_bal"] if resolved[c] is None]
    if missing:
        return False, f"Missing required columns: {missing}. Found: {list(df.columns)[:20]}", {}

    batch_id = f"KF_{file.name}_{datetime.now().strftime('%Y%m%d%H%M%S')}"
    rows, skipped = [], 0
    for _, row in df.iterrows():
        folio = clean_str(row.get(resolved["folio_no"], ""))
        if not folio:
            skipped += 1
            continue
        try:
            aum_val = _sf(row.get(resolved["rupee_bal"], 0)) if resolved["rupee_bal"] else 0.0
            if aum_val == 0 and resolved["nav"] and resolved["units"]:
                aum_val = _sf(row.get(resolved["nav"], 0)) * _sf(row.get(resolved["units"], 0))
            rows.append((
                folio, clean_str(row.get(resolved["inv_name"], "")).strip(),
                clean_str(row.get(resolved["scheme_name"], "")), clean_str(row.get(resolved["amc_code"], "")),
                clean_str(row.get(resolved["product_code"], "")), clean_str(row.get(resolved["scheme_code"], "")),
                clean_str(row.get(resolved["dividend_opt"], "")), clean_str(row.get(resolved["email"], "")).lower(),
                parse_date_safe(row.get(resolved["rep_date"], "")),
                _sf(row.get(resolved["units"], 0)) if resolved["units"] else 0.0,
                aum_val, _sf(row.get(resolved["nav"], 0)) if resolved["nav"] else 0.0,
                aum_val, batch_id,
            ))
        except Exception as exc:
            log.warning("Skipped KFinTech AUM row: %s", exc)
            skipped += 1

    if not rows:
        return False, "0 KFinTech AUM rows imported", {}

    inserted = duplicate_skipped = 0
    with get_conn() as conn:
        if replace:
            conn.execute("DELETE FROM kfintech_aum")
            conn.executemany(
                "INSERT OR IGNORE INTO kfintech_aum (folio_no,inv_name,scheme_name,amc_code,product_code,scheme_code,dividend_opt,email,rep_date,units,rupee_bal,nav,aum,upload_batch) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows)
            inserted = len(rows)
        else:
            before = conn.execute("SELECT COUNT(*) FROM kfintech_aum").fetchone()[0]
            conn.executemany(
                "INSERT OR IGNORE INTO kfintech_aum (folio_no,inv_name,scheme_name,amc_code,product_code,scheme_code,dividend_opt,email,rep_date,units,rupee_bal,nav,aum,upload_batch) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows)
            after = conn.execute("SELECT COUNT(*) FROM kfintech_aum").fetchone()[0]
            inserted = after - before
            duplicate_skipped = len(rows) - inserted

    total_aum = sum(r[10] for r in rows)
    preview = {"rows": inserted, "skipped": skipped, "duplicates": duplicate_skipped, "total_aum": total_aum}
    msg = f"Imported {inserted} KFinTech AUM rows | Total AUM: {format_aum(total_aum)}"
    if skipped: msg += f" | Skipped {skipped}"
    if duplicate_skipped: msg += f" | Skipped {duplicate_skipped} duplicates"
    return True, msg, preview


def parse_kfintech_transactions(file, replace: bool) -> tuple[bool, str, dict]:
    df = _read_csv_auto(file)
    if df is None:
        return False, "Could not parse KFinTech transaction file (MFSD201)", {}
    df = _clean_cols(df)
    required = ["TD_ACNO", "TD_TRNO", "TD_AMT"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        return False, f"Missing columns: {missing}. Found: {list(df.columns)[:20]}", {}

    batch_id = f"KF_R2_{datetime.now().strftime('%Y%m%d%H%M%S')}"
    rows, skipped = [], 0
    for _, row in df.iterrows():
        try:
            rows.append((
                clean_str(row.get("FMCODE", "")), clean_str(row.get("TD_FUND", "")),
                clean_str(row.get("TD_ACNO", "")), clean_str(row.get("SCHPLN", "")),
                clean_str(row.get("DIVOPT", "")), clean_str(row.get("FUNDDESC", "")),
                clean_str(row.get("TD_PURRED", "")), clean_str(row.get("TD_TRNO", "")),
                clean_str(row.get("INVNAME", "")).strip(), clean_str(row.get("TRNMODE", "")),
                clean_str(row.get("TRNSTAT", "")), clean_str(row.get("TD_BRANCH", "")),
                parse_date_safe(row.get("TD_TRDT", "")), parse_date_safe(row.get("TD_PRDT", "")),
                _sf(row.get("TD_POP", 0)), _sf(row.get("TD_UNITS", 0)), _sf(row.get("TD_AMT", 0)),
                _sf(row.get("LOAD1", 0)), clean_str(row.get("TD_AGENT", "")),
                clean_str(row.get("TD_BROKER", "")), _sf(row.get("BROKPER", 0)),
                _sf(row.get("BROKCOMM", 0)), _sf(row.get("STT", 0)),
                clean_str(row.get("PAN1", "")).upper(), clean_str(row.get("SIPREGSLNO", "")),
                parse_date_safe(row.get("SIPREGDT", "")), clean_str(row.get("CHQBANK", "")),
                parse_date_safe(row.get("CHQDATE", "")), clean_str(row.get("TD_TRTYPE", "")),
                clean_str(row.get("TRDESC", "")), parse_date_safe(row.get("PURDATE", "")),
                _sf(row.get("PURAMT", 0)), _sf(row.get("PURUNITS", 0)),
                clean_str(row.get("TRFLAG", "")), parse_date_safe(row.get("SFUNDDT", "")),
                clean_str(row.get("IHNO", "")), clean_str(row.get("BRANCHCODE", "")),
                clean_str(row.get("INWARDNO", "")), clean_str(row.get("NCTREMARKS", "")),
                clean_str(row.get("GUARDPANNO", "")).upper(), clean_str(row.get("CAN", "")),
                clean_str(row.get("EXCHORGTRTYPE", "")), clean_str(row.get("ELECTRXNFLAG", "")),
                clean_str(row.get("CLEARED", "")), clean_str(row.get("INVSTATE", "")),
                parse_date_safe(row.get("CRDATE", "")), batch_id,
            ))
        except Exception as exc:
            log.warning("Skipped KFinTech R2 row: %s", exc)
            skipped += 1

    if not rows:
        return False, "0 KFinTech transaction rows parsed", {}

    inserted = dupes = 0
    with get_conn() as conn:
        if replace:
            conn.execute("DELETE FROM kfintech_transactions")
            conn.executemany(
                "INSERT OR IGNORE INTO kfintech_transactions "
                "(product_code,fund_code,folio_no,scheme_code,div_opt,scheme_name,pur_red,trxn_no,inv_name,"
                "trxn_mode,trxn_status,branch,trade_date,post_date,nav,units,amount,load_amount,agent_code,"
                "broker_code,broker_pct,broker_comm,stt,pan,sip_reg_no,sip_reg_date,chq_bank,chq_date,trxn_type,"
                "trdesc,pur_date,pur_amt,pur_units,trflag,sfund_date,ih_no,branch_code,inward_no,remarks,"
                "guard_pan,can,exch_org_trtype,elec_trxn_flag,cleared,inv_state,rep_date,upload_batch) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows)
            inserted = len(rows)
        else:
            before = conn.execute("SELECT COUNT(*) FROM kfintech_transactions").fetchone()[0]
            conn.executemany(
                "INSERT OR IGNORE INTO kfintech_transactions "
                "(product_code,fund_code,folio_no,scheme_code,div_opt,scheme_name,pur_red,trxn_no,inv_name,"
                "trxn_mode,trxn_status,branch,trade_date,post_date,nav,units,amount,load_amount,agent_code,"
                "broker_code,broker_pct,broker_comm,stt,pan,sip_reg_no,sip_reg_date,chq_bank,chq_date,trxn_type,"
                "trdesc,pur_date,pur_amt,pur_units,trflag,sfund_date,ih_no,branch_code,inward_no,remarks,"
                "guard_pan,can,exch_org_trtype,elec_trxn_flag,cleared,inv_state,rep_date,upload_batch) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows)
            after = conn.execute("SELECT COUNT(*) FROM kfintech_transactions").fetchone()[0]
            inserted = after - before
            dupes = len(rows) - inserted

    total_amt = sum(r[16] for r in rows)
    preview = {"rows": inserted, "skipped": skipped, "duplicates": dupes, "total_amount": total_amt,
               "schemes": len({r[5] for r in rows}), "folios": len({r[2] for r in rows})}
    msg = f"Imported {inserted} KFinTech transactions | Rs {total_amt:,.2f} total"
    if skipped: msg += f" | Skipped {skipped}"
    if dupes: msg += f" | {dupes} duplicates"
    return True, msg, preview


def parse_kfintech_folio_master(file, replace: bool) -> tuple[bool, str, dict]:
    df = _read_csv_auto(file)
    if df is None:
        return False, "Could not parse KFinTech folio master file (MFSD211)", {}
    df = _clean_cols(df)
    required = ["FOLIO", "INVESTOR NAME"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        return False, f"Missing columns: {missing}. Found: {list(df.columns)[:20]}", {}

    batch_id = f"KF_R9_{datetime.now().strftime('%Y%m%d%H%M%S')}"
    rows, skipped = [], 0
    for _, row in df.iterrows():
        folio = clean_str(row.get("FOLIO", ""))
        if not folio:
            skipped += 1
            continue
        try:
            rows.append((
                clean_str(row.get("PRODUCT CODE", "")), clean_str(row.get("FUND", "")),
                folio, clean_str(row.get("DIVIDEND OPTION", "")),
                clean_str(row.get("FUND DESCRIPTION", "")), clean_str(row.get("INVESTOR NAME", "")).strip(),
                clean_str(row.get("JOINT NAME 1", "")), clean_str(row.get("JOINT NAME 2", "")),
                clean_str(row.get("ADDRESS #1", "")), clean_str(row.get("ADDRESS #2", "")),
                clean_str(row.get("ADDRESS #3", "")), clean_str(row.get("CITY", "")),
                clean_str(row.get("PINCODE", "")), clean_str(row.get("STATE", "")),
                clean_str(row.get("COUNTRY", "")), parse_date_safe(row.get("DATE OF BIRTH", "")),
                clean_str(row.get("EMAIL", "")).lower(), clean_str(row.get("MOBILE NUMBER", "")),
                clean_str(row.get("PAN NUMBER", "")).upper(), clean_str(row.get("TAX STATUS", "")),
                clean_str(row.get("OCC CODE", "")), clean_str(row.get("OCCUPATION DESCRIPTION", "")),
                clean_str(row.get("MODE OF HOLDING DESCRIPTION", "")), clean_str(row.get("MAPIN ID", "")),
                clean_str(row.get("BANK NAME", "")), clean_str(row.get("BRANCH", "")),
                clean_str(row.get("ACCOUNT TYPE", "")), clean_str(row.get("BANKACCNO", "")),
                clean_str(row.get("BANK ADDRESS #1", "")), clean_str(row.get("BANK ADDRESS #2", "")),
                clean_str(row.get("BANK ADDRESS #3", "")), clean_str(row.get("BANK CITY", "")),
                clean_str(row.get("BANK STATE", "")), clean_str(row.get("BROKER CODE", "")),
                clean_str(row.get("HOLDER 1 AADHAAR INFO", "")), clean_str(row.get("HOLDER 2 AADHAAR INFO", "")),
                clean_str(row.get("HOLDER 3 AADHAAR INFO", "")), clean_str(row.get("GUARDIAN AADHAAR INFO", "")),
                parse_date_safe(row.get("REPORT DATE", "")), batch_id,
            ))
        except Exception as exc:
            log.warning("Skipped KFinTech R9 row: %s", exc)
            skipped += 1

    if not rows:
        return False, "0 KFinTech folio master rows parsed", {}

    inserted = dupes = 0
    with get_conn() as conn:
        if replace:
            conn.execute("DELETE FROM kfintech_folio_master")
            conn.executemany(
                "INSERT OR IGNORE INTO kfintech_folio_master "
                "(product_code,fund_code,folio_no,div_opt,scheme_name,inv_name,joint1_name,joint2_name,"
                "address1,address2,address3,city,pincode,state,country,dob,email,mobile,pan_no,tax_status,"
                "occ_code,occ_desc,holding_nature,mapin_id,bank_name,branch,ac_type,ac_no,bank_address1,"
                "bank_address2,bank_address3,bank_city,bank_state,broker_code,aadhaar1,aadhaar2,aadhaar3,"
                "guardian_aadhaar,rep_date,upload_batch) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows)
            inserted = len(rows)
        else:
            before = conn.execute("SELECT COUNT(*) FROM kfintech_folio_master").fetchone()[0]
            conn.executemany(
                "INSERT OR IGNORE INTO kfintech_folio_master "
                "(product_code,fund_code,folio_no,div_opt,scheme_name,inv_name,joint1_name,joint2_name,"
                "address1,address2,address3,city,pincode,state,country,dob,email,mobile,pan_no,tax_status,"
                "occ_code,occ_desc,holding_nature,mapin_id,bank_name,branch,ac_type,ac_no,bank_address1,"
                "bank_address2,bank_address3,bank_city,bank_state,broker_code,aadhaar1,aadhaar2,aadhaar3,"
                "guardian_aadhaar,rep_date,upload_batch) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows)
            after = conn.execute("SELECT COUNT(*) FROM kfintech_folio_master").fetchone()[0]
            inserted = after - before
            dupes = len(rows) - inserted

    preview = {"rows": inserted, "skipped": skipped, "duplicates": dupes,
               "unique_folios": len({r[2] for r in rows}), "unique_investors": len({r[5] for r in rows})}
    msg = f"Imported {inserted} KFinTech folio records"
    if skipped: msg += f" | Skipped {skipped}"
    if dupes: msg += f" | {dupes} duplicates"
    return True, msg, preview


def parse_kfintech_sip_master(file, replace: bool) -> tuple[bool, str, dict]:
    df = _read_csv_auto(file)
    if df is None:
        return False, "Could not parse KFinTech SIP master file (MFSD243)", {}
    df = _clean_cols(df)
    required = ["FOLIO", "REGSLNO", "AMOUNT"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        return False, f"Missing columns: {missing}. Found: {list(df.columns)[:20]}", {}

    batch_id = f"KF_R49_{datetime.now().strftime('%Y%m%d%H%M%S')}"

    def _sip_status(terminate_str: str, to_str: str) -> str:
        if terminate_str and str(terminate_str).strip() not in ("", "None", "NaN"):
            return "Ceased"
        try:
            end = pd.to_datetime(to_str, dayfirst=True, errors="coerce")
            if pd.notna(end) and end.date() < datetime.now().date():
                return "Completed"
        except Exception:
            pass
        return "Active"

    rows, skipped = [], 0
    for _, row in df.iterrows():
        folio = clean_str(row.get("FOLIO", ""))
        reg_sl = clean_str(row.get("REGSLNO", ""))
        if not folio or not reg_sl:
            skipped += 1
            continue
        try:
            status = _sip_status(row.get("TERMINATEDATE", ""), row.get("END DATE", ""))
            rows.append((
                clean_str(row.get("ZONE", "")), clean_str(row.get("BRANCH", "")),
                clean_str(row.get("LOCATION", "")), clean_str(row.get("IHNO", "")),
                folio, clean_str(row.get("INVESTOR NAME", "")).strip(),
                parse_date_safe(row.get("REGISTRATIONDATE", "")),
                parse_date_safe(row.get("START DATE", "")), parse_date_safe(row.get("END DATE", "")),
                int(_sf(row.get("NO OF INSTALLMENTS", 0))), _sf(row.get("AMOUNT", 0)),
                clean_str(row.get("SCHEME", "")), clean_str(row.get("PLAN", "")),
                clean_str(row.get("AGENTCODE", "")), clean_str(row.get("AGENTNAME", "")),
                clean_str(row.get("SUBBROKER", "")), clean_str(row.get("SCHEME NAME", "")),
                clean_str(row.get("PAN", "")).upper(), clean_str(row.get("SIPTYPE", "")),
                clean_str(row.get("SIP MODE", "")), clean_str(row.get("FUND CODE", "")),
                clean_str(row.get("PRODUCT CODE", "")), clean_str(row.get("FREQUENCY", "")),
                clean_str(row.get("TRTYPE", "")), clean_str(row.get("TO SCHEME", "")),
                clean_str(row.get("TO PLAN", "")), parse_date_safe(row.get("TERMINATEDATE", "")),
                status, clean_str(row.get("TOPRODUCTCODE", "")), clean_str(row.get("TOSCHEMENAME", "")),
                clean_str(row.get("ECSNO", "")), clean_str(row.get("ECSBANKNAME", "")),
                clean_str(row.get("ECSAcno", "")), clean_str(row.get("ECSHOLDERNAME", "")),
                reg_sl, clean_str(row.get("INVDPID", "")), clean_str(row.get("INVCLIENTID", "")),
                clean_str(row.get("DP_INVNAME", "")), clean_str(row.get("MODIFYFLAG", "")),
                clean_str(row.get("UMRNCODE", "")), batch_id,
            ))
        except Exception as exc:
            log.warning("Skipped KFinTech R49 row: %s", exc)
            skipped += 1

    if not rows:
        return False, "0 KFinTech SIP master rows parsed", {}

    inserted = dupes = 0
    with get_conn() as conn:
        if replace:
            conn.execute("DELETE FROM kfintech_sip_master")
            conn.executemany(
                "INSERT OR IGNORE INTO kfintech_sip_master "
                "(zone,branch,location,ih_no,folio_no,inv_name,reg_date,from_date,to_date,installments,"
                "sip_amount,scheme,plan,agent_code,agent_name,subbroker,scheme_name,pan,sip_type,sip_mode,"
                "fund_code,product_code,frequency,trtype,to_scheme,to_plan,terminate_date,status,"
                "to_product_code,to_scheme_name,ecs_no,ecs_bank,ecs_ac_no,ecs_holder,reg_sl_no,"
                "inv_dp_id,inv_client_id,dp_inv_name,modify_flag,umrn_code,upload_batch) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows)
            inserted = len(rows)
        else:
            before = conn.execute("SELECT COUNT(*) FROM kfintech_sip_master").fetchone()[0]
            conn.executemany(
                "INSERT OR IGNORE INTO kfintech_sip_master "
                "(zone,branch,location,ih_no,folio_no,inv_name,reg_date,from_date,to_date,installments,"
                "sip_amount,scheme,plan,agent_code,agent_name,subbroker,scheme_name,pan,sip_type,sip_mode,"
                "fund_code,product_code,frequency,trtype,to_scheme,to_plan,terminate_date,status,"
                "to_product_code,to_scheme_name,ecs_no,ecs_bank,ecs_ac_no,ecs_holder,reg_sl_no,"
                "inv_dp_id,inv_client_id,dp_inv_name,modify_flag,umrn_code,upload_batch) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows)
            after = conn.execute("SELECT COUNT(*) FROM kfintech_sip_master").fetchone()[0]
            inserted = after - before
            dupes = len(rows) - inserted

    sc = {}
    for r in rows:
        sc[r[27]] = sc.get(r[27], 0) + 1
    preview = {"rows": inserted, "skipped": skipped, "duplicates": dupes,
               "active": sc.get("Active", 0), "ceased": sc.get("Ceased", 0), "completed": sc.get("Completed", 0)}
    msg = f"Imported {inserted} KFinTech SIPs | Active: {sc.get('Active', 0)} | Ceased: {sc.get('Ceased', 0)} | Completed: {sc.get('Completed', 0)}"
    if skipped: msg += f" | Skipped {skipped}"
    if dupes: msg += f" | {dupes} duplicates"
    return True, msg, preview


# ══════════════════════════════════════════════════════════════════════════════
# STREAMLIT UI  —  call render_data_manager() inside your Admin Panel tab
# ══════════════════════════════════════════════════════════════════════════════

def _upload_section(label, hint, key, parse_fn, replace_key, replace_label, btn_key, metrics_fn=None):
    """Generic upload widget. metrics_fn(preview) → optional extra st calls."""
    st.caption(hint)
    f = st.file_uploader(label, type=["csv", "txt", "tsv", "xlsx"], key=key)
    replace = st.checkbox(replace_label, key=replace_key)
    if replace:
        st.warning("Replace mode: existing data will be deleted and reimported.", icon="⚠️")
    else:
        st.info("Append mode: duplicates silently skipped.", icon="ℹ️")
    if st.button(f"📤 Upload {label}", key=btn_key) and f:
        with st.spinner("Parsing…"):
            result = parse_fn(f, replace)
            ok, msg, preview = result if len(result) == 3 else (*result, {})
            (st.success if ok else st.error)(msg)
            if ok and preview and metrics_fn:
                metrics_fn(preview)
            st.cache_data.clear()


def render_data_manager():
    """Drop-in replacement for tab_bse + tab_rta content in your Admin Panel."""

    tab_bse, tab_cams, tab_kfin = st.tabs(["📥 BSE Data", "🟢 CAMS Data", "🔵 KFinTech Data"])

    # ──────────────────────────── BSE ────────────────────────────
    with tab_bse:
        st.subheader("Client Master (BSE)")
        f1 = st.file_uploader("Client Master Excel", type=["xlsx"], key="dm_client_file")
        r1 = st.checkbox("Replace existing clients", key="dm_replace_clients")
        if r1:
            st.warning("Replace mode: ALL existing clients deleted.", icon="⚠️")
        else:
            st.info("Append mode: new client codes only.", icon="ℹ️")
        if st.button("Import Clients", key="dm_import_clients") and f1:
            with st.spinner("Importing…"):
                ok, msg = parse_bse_client_master(f1, r1)
                (st.success if ok else st.error)(msg)
                if ok: st.cache_data.clear()

        st.divider()
        st.subheader("SIP Report (BSE)")
        f2 = st.file_uploader("SIP Report Excel", type=["xlsx"], key="dm_sip_file")
        r2 = st.checkbox("Replace existing holdings", key="dm_replace_holdings")
        if r2:
            st.warning("Replace mode: ALL existing holdings deleted.", icon="⚠️")
        else:
            st.info("Append mode: duplicates (folio + scheme + start_date) skipped.", icon="ℹ️")
        if st.button("Import SIPs", key="dm_import_sips") and f2:
            with st.spinner("Importing…"):
                ok, msg, preview = parse_bse_sip_report(f2, r2)
                (st.success if ok else st.error)(msg)
                if preview:
                    c1, c2, c3 = st.columns(3)
                    if "active" in preview: c1.metric("Active in file", preview["active"])
                    if "cancelled" in preview: c2.metric("Cancelled (skipped)", preview["cancelled"])
                    if "first_order" in preview: c3.metric("First Order = Y", preview["first_order"])
                if ok: st.cache_data.clear()

        st.divider()
        st.subheader("🗑️ BSE Delete")
        col1, col2 = st.columns(2)
        with col1:
            if st.button("⚠️ Clear All Clients", key="dm_del_clients"):
                with get_conn() as conn: conn.execute("DELETE FROM clients")
                st.warning("All clients deleted.")
                st.cache_data.clear()
        with col2:
            if st.button("⚠️ Clear All Holdings", key="dm_del_holdings"):
                with get_conn() as conn: conn.execute("DELETE FROM holdings")
                st.warning("All holdings deleted.")
                st.cache_data.clear()

    # ──────────────────────────── CAMS ────────────────────────────
    with tab_cams:
        cams_section = st.radio(
            "Section", ["📦 AUM", "💼 Brokerage", "📂 Reports (R2/R9/R49)"],
            horizontal=True, key="dm_cams_section"
        )
        st.divider()

        if cams_section == "📦 AUM":
            st.subheader("CAMS AUM Report")
            st.caption("Required: `FOLIOCHK`, `RUPEE_BAL`, `SCH_NAME`, `AMC_CODE`")
            f = st.file_uploader("CAMS AUM CSV/TSV", type=["csv", "txt", "tsv"], key="dm_cams_aum")
            rp = st.checkbox("Replace existing CAMS AUM", key="dm_replace_cams_aum")
            if rp:
                st.warning("Replace mode.", icon="⚠️")
            else:
                st.info("Append mode: duplicate (folio + scheme + date) skipped.", icon="ℹ️")
            if st.button("📤 Upload CAMS AUM", key="dm_upload_cams_aum") and f:
                with st.spinner("Parsing…"):
                    ok, msg, preview = parse_cams_aum(f, rp)
                    (st.success if ok else st.error)(msg)
                    if ok and preview:
                        c1, c2, c3, c4 = st.columns(4)
                        c1.metric("Inserted", preview.get("rows", 0))
                        c2.metric("Total AUM", format_aum(preview.get("total_aum", 0)))
                        c3.metric("Duplicates", preview.get("duplicates", 0))
                        c4.metric("Skipped", preview.get("skipped", 0))
                    st.cache_data.clear()

            st.divider()
            st.markdown("#### Existing CAMS AUM")
            with get_conn() as conn:
                aum_sum = pd.read_sql(
                    "SELECT rep_date AS 'Date', amc_code AS 'AMC', COUNT(*) AS 'Folios', ROUND(SUM(rupee_bal),2) AS 'AUM (Rs)' FROM cams_aum GROUP BY rep_date, amc_code ORDER BY rep_date DESC",
                    conn)
            if aum_sum.empty:
                st.info("No CAMS AUM data yet.")
            else:
                aum_sum["AUM (Rs)"] = aum_sum["AUM (Rs)"].apply(format_aum)
                st.dataframe(aum_sum, use_container_width=True, hide_index=True)
            if st.button("⚠️ Clear CAMS AUM", key="dm_del_cams_aum"):
                with get_conn() as conn: conn.execute("DELETE FROM cams_aum")
                st.warning("CAMS AUM deleted.")
                st.cache_data.clear()

        elif cams_section == "💼 Brokerage":
            st.subheader("CAMS Brokerage")
            st.caption("Required: `FOLIO_NO`, `BRKAGE_AMT`, `AMC_CODE`")
            f = st.file_uploader("CAMS Brokerage CSV/TSV", type=["csv", "txt", "tsv"], key="dm_cams_brok")
            rp = st.checkbox("Replace ALL CAMS brokerage", key="dm_replace_cams_brok")
            if rp:
                st.warning("Replace mode.", icon="⚠️")
            else:
                st.info("Append mode: duplicate (trxn_no + folio + month) skipped.", icon="ℹ️")
            if st.button("📤 Upload CAMS Brokerage", key="dm_upload_cams_brok") and f:
                with st.spinner("Parsing…"):
                    ok, msg, preview = parse_cams_brokerage(f, rp)
                    (st.success if ok else st.error)(msg)
                    if ok and preview:
                        c1, c2, c3, c4 = st.columns(4)
                        c1.metric("Inserted", preview.get("rows", 0))
                        c2.metric("Total Brokerage", format_brokerage(preview.get("total_brokerage", 0)))
                        c3.metric("Duplicates", preview.get("duplicates", 0))
                        c4.metric("Skipped", preview.get("skipped", 0))
                        if preview.get("months"): st.info(f"Months: **{', '.join(preview['months'])}**")
                    st.cache_data.clear()

            st.divider()
            st.markdown("#### Existing CAMS Brokerage")
            with get_conn() as conn:
                brok_sum = pd.read_sql(
                    "SELECT accrual_month AS 'Month', COUNT(*) AS 'Rows', COUNT(DISTINCT folio_no) AS 'Folios', ROUND(SUM(brkage_amt),2) AS 'Total (Rs)' FROM cams_brokerage GROUP BY accrual_month ORDER BY accrual_month DESC",
                    conn)
            if brok_sum.empty:
                st.info("No CAMS brokerage data yet.")
            else:
                st.dataframe(brok_sum, use_container_width=True, hide_index=True)
            if st.button("⚠️ Clear CAMS Brokerage", key="dm_del_cams_brok"):
                with get_conn() as conn: conn.execute("DELETE FROM cams_brokerage")
                st.warning("CAMS brokerage deleted.")
                st.cache_data.clear()

        else:  # Reports
            cams_report = st.radio(
                "Report", ["WBR2 — Transactions", "WBR9 — Folio Master", "WBR49 — SIP Master"],
                horizontal=True, key="dm_cams_report"
            )
            st.divider()

            if cams_report == "WBR2 — Transactions":
                st.subheader("WBR2 — Transaction Report")
                st.caption("Required: `FOLIO_NO`, `TRXNNO`, `AMOUNT`")
                f = st.file_uploader("R2 CSV", type=["csv", "txt", "tsv"], key="dm_cams_r2")
                rp = st.checkbox("Replace ALL CAMS transactions", key="dm_replace_cams_r2")
                if rp: st.warning("Replace mode.", icon="⚠️")
                if st.button("Upload R2", key="dm_upload_cams_r2") and f:
                    with st.spinner("Parsing…"):
                        ok, msg, preview = parse_cams_transactions(f, rp)
                        (st.success if ok else st.error)(msg)
                        if ok and preview:
                            c1, c2, c3, c4 = st.columns(4)
                            c1.metric("Inserted", preview.get("rows", 0))
                            c2.metric("Total Amount", format_currency(preview.get("total_amount", 0), decimals=0))
                            c3.metric("Folios", preview.get("folios", 0))
                            c4.metric("Schemes", preview.get("schemes", 0))
                        st.cache_data.clear()
                st.divider()
                with get_conn() as conn:
                    txn_sum = pd.read_sql(
                        "SELECT trade_date AS 'Date', amc_code AS 'AMC', COUNT(*) AS 'Txns', COUNT(DISTINCT folio_no) AS 'Folios', ROUND(SUM(amount),2) AS 'Amount' FROM cams_transactions GROUP BY trade_date, amc_code ORDER BY trade_date DESC LIMIT 30",
                        conn)
                if txn_sum.empty:
                    st.info("No CAMS transaction data yet.")
                else:
                    st.dataframe(txn_sum, use_container_width=True, hide_index=True)
                if st.button("⚠️ Clear CAMS Transactions", key="dm_del_cams_r2"):
                    with get_conn() as conn: conn.execute("DELETE FROM cams_transactions")
                    st.warning("Deleted.")
                    st.cache_data.clear()

            elif cams_report == "WBR9 — Folio Master":
                st.subheader("WBR9 — Folio Master")
                st.caption("Required: `FOLIOCHK`, `INV_NAME`")
                f = st.file_uploader("R9 CSV", type=["csv", "txt", "tsv"], key="dm_cams_r9")
                rp = st.checkbox("Replace ALL CAMS folio master", key="dm_replace_cams_r9")
                if rp: st.warning("Replace mode.", icon="⚠️")
                if st.button("Upload R9", key="dm_upload_cams_r9") and f:
                    with st.spinner("Parsing…"):
                        ok, msg, preview = parse_cams_folio_master(f, rp)
                        (st.success if ok else st.error)(msg)
                        if ok and preview:
                            c1, c2, c3, c4 = st.columns(4)
                            c1.metric("Inserted", preview.get("rows", 0))
                            c2.metric("AUM", format_aum(preview.get("total_aum", 0)))
                            c3.metric("Folios", preview.get("unique_folios", 0))
                            c4.metric("Investors", preview.get("unique_investors", 0))
                        st.cache_data.clear()
                st.divider()
                with get_conn() as conn:
                    fm_sum = pd.read_sql(
                        "SELECT amc_code AS 'AMC', COUNT(DISTINCT folio_no) AS 'Folios', COUNT(DISTINCT pan_no) AS 'Investors', ROUND(SUM(rupee_bal),2) AS 'AUM' FROM cams_folio_master GROUP BY amc_code ORDER BY 4 DESC",
                        conn)
                if fm_sum.empty:
                    st.info("No CAMS folio master data yet.")
                else:
                    fm_sum["AUM"] = fm_sum["AUM"].apply(format_aum)
                    st.dataframe(fm_sum, use_container_width=True, hide_index=True)
                if st.button("⚠️ Clear CAMS Folio Master", key="dm_del_cams_r9"):
                    with get_conn() as conn: conn.execute("DELETE FROM cams_folio_master")
                    st.warning("Deleted.")
                    st.cache_data.clear()

            else:  # R49
                st.subheader("WBR49 — SIP Master")
                st.caption("Required: `FOLIO_NO`, `AUTO_TRNO`, `AUTO_AMOUNT`")
                f = st.file_uploader("R49 CSV", type=["csv", "txt", "tsv"], key="dm_cams_r49")
                rp = st.checkbox("Replace ALL CAMS SIP master", key="dm_replace_cams_r49")
                if rp: st.warning("Replace mode.", icon="⚠️")
                if st.button("Upload R49", key="dm_upload_cams_r49") and f:
                    with st.spinner("Parsing…"):
                        ok, msg, preview = parse_cams_sip_master(f, rp)
                        (st.success if ok else st.error)(msg)
                        if ok and preview:
                            c1, c2, c3, c4 = st.columns(4)
                            c1.metric("Inserted", preview.get("rows", 0))
                            c2.metric("Active", preview.get("active", 0))
                            c3.metric("Ceased", preview.get("ceased", 0))
                            c4.metric("Completed", preview.get("completed", 0))
                        st.cache_data.clear()
                st.divider()
                with get_conn() as conn:
                    sip_sum = pd.read_sql(
                        "SELECT amc_code AS 'AMC', status AS 'Status', COUNT(*) AS 'SIPs', COUNT(DISTINCT folio_no) AS 'Folios', ROUND(SUM(sip_amount),2) AS 'Amount' FROM cams_sip_master GROUP BY amc_code, status ORDER BY amc_code",
                        conn)
                if sip_sum.empty:
                    st.info("No CAMS SIP master data yet.")
                else:
                    st.dataframe(sip_sum, use_container_width=True, hide_index=True)
                if st.button("⚠️ Clear CAMS SIP Master", key="dm_del_cams_r49"):
                    with get_conn() as conn: conn.execute("DELETE FROM cams_sip_master")
                    st.warning("Deleted.")
                    st.cache_data.clear()

    # ──────────────────────────── KFinTech ────────────────────────────
    with tab_kfin:
        kf_section = st.radio(
            "Section", ["📦 AUM", "💼 Brokerage", "📂 Reports (MFSD201/211/243)"],
            horizontal=True, key="dm_kf_section"
        )
        st.divider()

        if kf_section == "📦 AUM":
            st.subheader("KFinTech AUM Report")
            st.caption("Expected: `Folio Number`, `Fund Description`, `AUM`, `Balance`, `NAV`, `Report Date`")
            f = st.file_uploader("KFinTech AUM CSV/TSV", type=["csv", "txt", "tsv"], key="dm_kf_aum")
            rp = st.checkbox("Replace existing KFinTech AUM", key="dm_replace_kf_aum")
            if rp:
                st.warning("Replace mode.", icon="⚠️")
            else:
                st.info("Append mode: duplicate (folio + scheme + date) skipped.", icon="ℹ️")
            if st.button("📤 Upload KFinTech AUM", key="dm_upload_kf_aum") and f:
                with st.spinner("Parsing…"):
                    ok, msg, preview = parse_kfintech_aum(f, rp)
                    (st.success if ok else st.error)(msg)
                    if ok and preview:
                        c1, c2, c3, c4 = st.columns(4)
                        c1.metric("Inserted", preview.get("rows", 0))
                        c2.metric("Total AUM", format_aum(preview.get("total_aum", 0)))
                        c3.metric("Duplicates", preview.get("duplicates", 0))
                        c4.metric("Skipped", preview.get("skipped", 0))
                    st.cache_data.clear()

            st.divider()
            st.markdown("#### Existing KFinTech AUM")
            with get_conn() as conn:
                kf_aum_sum = pd.read_sql(
                    "SELECT rep_date AS 'Date', amc_code AS 'AMC', COUNT(*) AS 'Folios', ROUND(SUM(rupee_bal),2) AS 'AUM (Rs)' FROM kfintech_aum GROUP BY rep_date, amc_code ORDER BY rep_date DESC",
                    conn)
            if kf_aum_sum.empty:
                st.info("No KFinTech AUM data yet.")
            else:
                kf_aum_sum["AUM (Rs)"] = kf_aum_sum["AUM (Rs)"].apply(format_aum)
                st.dataframe(kf_aum_sum, use_container_width=True, hide_index=True)
            if st.button("⚠️ Clear KFinTech AUM", key="dm_del_kf_aum"):
                with get_conn() as conn: conn.execute("DELETE FROM kfintech_aum")
                st.warning("KFinTech AUM deleted.")
                st.cache_data.clear()

        elif kf_section == "💼 Brokerage":
            st.subheader("KFinTech Brokerage")
            st.caption("Required: `Account Number`, `Brokerage (in Rs.)`, `Fund`")
            f = st.file_uploader("KFinTech Brokerage CSV/TSV", type=["csv", "txt", "tsv"], key="dm_kf_brok")
            rp = st.checkbox("Replace ALL KFinTech brokerage", key="dm_replace_kf_brok")
            if rp:
                st.warning("Replace mode.", icon="⚠️")
            else:
                st.info("Append mode: duplicate (trxn_no + folio + month) skipped.", icon="ℹ️")
            if st.button("📤 Upload KFinTech Brokerage", key="dm_upload_kf_brok") and f:
                with st.spinner("Parsing…"):
                    ok, msg, preview = parse_kfintech_brokerage(f, rp)
                    (st.success if ok else st.error)(msg)
                    if ok and preview:
                        c1, c2, c3, c4 = st.columns(4)
                        c1.metric("Inserted", preview.get("rows", 0))
                        c2.metric("Total Brokerage", format_brokerage(preview.get("total_brokerage", 0)))
                        c3.metric("Duplicates", preview.get("duplicates", 0))
                        c4.metric("Skipped", preview.get("skipped", 0))
                        if preview.get("months"): st.info(f"Months: **{', '.join(preview['months'])}**")
                    st.cache_data.clear()

            st.divider()
            st.markdown("#### Existing KFinTech Brokerage")
            with get_conn() as conn:
                kf_brok_sum = pd.read_sql(
                    "SELECT accrual_month AS 'Month', COUNT(*) AS 'Rows', COUNT(DISTINCT folio_no) AS 'Folios', ROUND(SUM(brkage_amt),2) AS 'Total (Rs)' FROM kfintech_brokerage GROUP BY accrual_month ORDER BY accrual_month DESC",
                    conn)
            if kf_brok_sum.empty:
                st.info("No KFinTech brokerage data yet.")
            else:
                st.dataframe(kf_brok_sum, use_container_width=True, hide_index=True)
            if st.button("⚠️ Clear KFinTech Brokerage", key="dm_del_kf_brok"):
                with get_conn() as conn: conn.execute("DELETE FROM kfintech_brokerage")
                st.warning("KFinTech brokerage deleted.")
                st.cache_data.clear()

        else:  # Reports
            kf_report = st.radio(
                "Report", ["MFSD201 — Transactions", "MFSD211 — Folio Master", "MFSD243 — SIP Master"],
                horizontal=True, key="dm_kf_report"
            )
            st.divider()

            if kf_report == "MFSD201 — Transactions":
                st.subheader("MFSD201 — Transaction Report")
                st.caption("Required: `TD_ACNO`, `TD_TRNO`, `TD_AMT`")
                f = st.file_uploader("MFSD201 CSV", type=["csv", "txt", "tsv"], key="dm_kf_r2")
                rp = st.checkbox("Replace ALL KFinTech transactions", key="dm_replace_kf_r2")
                if rp: st.warning("Replace mode.", icon="⚠️")
                if st.button("Upload MFSD201", key="dm_upload_kf_r2") and f:
                    with st.spinner("Parsing…"):
                        ok, msg, preview = parse_kfintech_transactions(f, rp)
                        (st.success if ok else st.error)(msg)
                        if ok and preview:
                            c1, c2, c3, c4 = st.columns(4)
                            c1.metric("Inserted", preview.get("rows", 0))
                            c2.metric("Total Amount", format_currency(preview.get("total_amount", 0), decimals=0))
                            c3.metric("Folios", preview.get("folios", 0))
                            c4.metric("Schemes", preview.get("schemes", 0))
                        st.cache_data.clear()
                st.divider()
                with get_conn() as conn:
                    kf_txn_sum = pd.read_sql(
                        "SELECT trade_date AS 'Date', fund_code AS 'Fund', COUNT(*) AS 'Txns', COUNT(DISTINCT folio_no) AS 'Folios', ROUND(SUM(amount),2) AS 'Amount' FROM kfintech_transactions GROUP BY trade_date, fund_code ORDER BY trade_date DESC LIMIT 30",
                        conn)
                if kf_txn_sum.empty:
                    st.info("No KFinTech transaction data yet.")
                else:
                    st.dataframe(kf_txn_sum, use_container_width=True, hide_index=True)
                if st.button("⚠️ Clear KFinTech Transactions", key="dm_del_kf_r2"):
                    with get_conn() as conn: conn.execute("DELETE FROM kfintech_transactions")
                    st.warning("Deleted.")
                    st.cache_data.clear()

            elif kf_report == "MFSD211 — Folio Master":
                st.subheader("MFSD211 — Folio Master")
                st.caption("Required: `Folio`, `Investor Name`")
                f = st.file_uploader("MFSD211 CSV", type=["csv", "txt", "tsv"], key="dm_kf_r9")
                rp = st.checkbox("Replace ALL KFinTech folio master", key="dm_replace_kf_r9")
                if rp: st.warning("Replace mode.", icon="⚠️")
                if st.button("Upload MFSD211", key="dm_upload_kf_r9") and f:
                    with st.spinner("Parsing…"):
                        ok, msg, preview = parse_kfintech_folio_master(f, rp)
                        (st.success if ok else st.error)(msg)
                        if ok and preview:
                            c1, c2, c3, c4 = st.columns(4)
                            c1.metric("Inserted", preview.get("rows", 0))
                            c2.metric("Folios", preview.get("unique_folios", 0))
                            c3.metric("Investors", preview.get("unique_investors", 0))
                            c4.metric("Skipped", preview.get("skipped", 0))
                        st.cache_data.clear()
                st.divider()
                with get_conn() as conn:
                    kf_fm_sum = pd.read_sql(
                        "SELECT fund_code AS 'Fund', COUNT(DISTINCT folio_no) AS 'Folios', COUNT(DISTINCT pan_no) AS 'Investors', COUNT(*) AS 'Records' FROM kfintech_folio_master GROUP BY fund_code ORDER BY 2 DESC",
                        conn)
                if kf_fm_sum.empty:
                    st.info("No KFinTech folio master data yet.")
                else:
                    st.dataframe(kf_fm_sum, use_container_width=True, hide_index=True)
                if st.button("⚠️ Clear KFinTech Folio Master", key="dm_del_kf_r9"):
                    with get_conn() as conn: conn.execute("DELETE FROM kfintech_folio_master")
                    st.warning("Deleted.")
                    st.cache_data.clear()

            else:  # MFSD243
                st.subheader("MFSD243 — SIP Master")
                st.caption("Required: `Folio`, `RegSlno`, `Amount`")
                f = st.file_uploader("MFSD243 CSV", type=["csv", "txt", "tsv"], key="dm_kf_r49")
                rp = st.checkbox("Replace ALL KFinTech SIP master", key="dm_replace_kf_r49")
                if rp: st.warning("Replace mode.", icon="⚠️")
                if st.button("Upload MFSD243", key="dm_upload_kf_r49") and f:
                    with st.spinner("Parsing…"):
                        ok, msg, preview = parse_kfintech_sip_master(f, rp)
                        (st.success if ok else st.error)(msg)
                        if ok and preview:
                            c1, c2, c3, c4 = st.columns(4)
                            c1.metric("Inserted", preview.get("rows", 0))
                            c2.metric("Active", preview.get("active", 0))
                            c3.metric("Ceased", preview.get("ceased", 0))
                            c4.metric("Completed", preview.get("completed", 0))
                        st.cache_data.clear()
                st.divider()
                with get_conn() as conn:
                    kf_sip_sum = pd.read_sql(
                        "SELECT fund_code AS 'Fund', status AS 'Status', COUNT(*) AS 'SIPs', COUNT(DISTINCT folio_no) AS 'Folios', ROUND(SUM(sip_amount),2) AS 'Amount' FROM kfintech_sip_master GROUP BY fund_code, status ORDER BY fund_code",
                        conn)
                if kf_sip_sum.empty:
                    st.info("No KFinTech SIP master data yet.")
                else:
                    st.dataframe(kf_sip_sum, use_container_width=True, hide_index=True)
                if st.button("⚠️ Clear KFinTech SIP Master", key="dm_del_kf_r49"):
                    with get_conn() as conn: conn.execute("DELETE FROM kfintech_sip_master")
                    st.warning("Deleted.")
                    st.cache_data.clear()
