"""
data_manager.py
All Upload / Parse / Delete logic for BSE, CAMS, and KFinTech data.
Every parser inserts RAW file data into the exact tables defined in init.py's init_db().
Call render_data_manager() inside the Admin Panel tab.
"""

import logging
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime

import pandas as pd
import streamlit as st

from Init import DB_PATH


log = logging.getLogger(__name__)


def set_db_path(path: str):
    """Kept for compatibility; overrides the shared DB_PATH at runtime if ever needed."""
    global DB_PATH
    DB_PATH = path


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


# ══════════════════════════════════════════════════════════════════════════════
# PURE HELPERS
# ══════════════════════════════════════════════════════════════════════════════

AMC_SUFFIXES = [" MUTUAL FUND", " MF", " FUND", " AMC", " INDIA", " MANAGEMENT", " LTD", " LIMITED"]
_WS = re.compile(r"\s+")


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


def _sf(val) -> float:
    """Safe float — strips commas, returns 0.0 on failure."""
    try:
        v = str(val).replace(",", "").strip()
        return float(v) if v not in ("", "None", "NaN", "nan") else 0.0
    except Exception:
        return 0.0


def _si(val) -> int:
    """Safe int."""
    try:
        return int(float(str(val).replace(",", "").strip()))
    except Exception:
        return 0


def parse_date_safe(val) -> str:
    if isinstance(val, float) and pd.isna(val):
        return ""
    if str(val).strip() in {"", "None", "NaN", "nan"}:
        return ""
    try:
        dt = pd.to_datetime(val, dayfirst=True, errors="coerce")
        return dt.strftime("%Y-%m-%d") if pd.notna(dt) else ""
    except Exception:
        return ""


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


def _batch_id(prefix: str, name: str = "") -> str:
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    return f"{prefix}_{name}_{ts}" if name else f"{prefix}_{ts}"


def _clean_cols(df: pd.DataFrame) -> pd.DataFrame:
    """Strip quotes/BOM/ZWS and uppercase all column names."""
    df.columns = [
        c.strip()
        .replace("\u200b", "")
        .replace("\ufeff", "")
        .replace("\u00a0", "")
        .strip("'\"")
        .upper()
        for c in df.columns
    ]
    return df


def _read_csv_auto(file) -> pd.DataFrame | None:
    for sep in ("\t", ",", ";"):
        try:
            file.seek(0)
            df = pd.read_csv(
                file, sep=sep, quotechar="'", dtype=str,
                encoding="utf-8", encoding_errors="replace"
            )
            if len(df.columns) > 5:
                return df
        except Exception:
            continue
    return None


def _count_before_after(conn, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _inserted_dupes(before: int, conn, table: str, total: int) -> tuple[int, int]:
    after = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    inserted = after - before
    dupes = total - inserted
    return inserted, dupes


# ══════════════════════════════════════════════════════════════════════════════
# BSE SCHEME MASTER — COLUMN RESOLUTION
# ══════════════════════════════════════════════════════════════════════════════

_COL_MAP = {
    "unique_no": ["unique no", "uniqueno", "unique_no"],
    "scheme_code": ["scheme code", "scheme_code"],
    "rta_scheme_code": ["rta scheme code", "rta_scheme_code"],
    "amc_scheme_code": ["amc scheme code", "amc_scheme_code"],
    "isin": ["isin"],
    "amc_code": ["amc code", "amc_code"],
    "scheme_type": ["scheme type", "scheme_type"],
    "scheme_plan": ["scheme plan", "scheme_plan"],
    "scheme_name": ["scheme name", "scheme_name"],
    "purchase_allowed": ["purchase allowed", "purchase_allowed"],
    "purchase_transaction_mode": ["purchase transaction mode", "purchase_transaction_mode"],
    "min_purchase_amount": ["minimum purchase amount", "min_purchase_amount"],
    "additional_purchase_amount": ["additional purchase amount", "additional_purchase_amount"],
    "max_purchase_amount": ["maximum purchase amount", "max_purchase_amount"],
    "purchase_amount_multiplier": ["purchase amount multiplier", "purchase_amount_multiplier"],
    "purchase_cutoff_time": ["purchase cutoff time", "purchase_cutoff_time"],
    "redemption_allowed": ["redemption allowed", "redemption_allowed"],
    "redemption_transaction_mode": ["redemption transaction mode", "redemption_transaction_mode"],
    "min_redemption_qty": ["minimum redemption qty", "min_redemption_qty"],
    "redemption_qty_multiplier": ["redemption qty multiplier", "redemption_qty_multiplier"],
    "max_redemption_qty": ["maximum redemption qty", "max_redemption_qty"],
    "redemption_amount_minimum": ["redemption amount - minimum", "redemption amount minimum",
                                  "redemption_amount_minimum"],
    "redemption_amount_maximum": ["redemption amount maximum", "redemption amount - maximum",
                                  "redemption amount \u2013 maximum", "redemption_amount_maximum"],
    "redemption_amount_multiple": ["redemption amount multiple", "redemption_amount_multiple"],
    "redemption_cutoff_time": ["redemption cut off time", "redemption cutoff time", "redemption_cutoff_time"],
    "rta_agent_code": ["rta agent code", "rta_agent_code"],
    "amc_active_flag": ["amc active flag", "amc_active_flag"],
    "dividend_reinvestment_flag": ["dividend reinvestment flag", "dividend_reinvestment_flag"],
    "sip_flag": ["sip flag", "sip_flag"],
    "stp_flag": ["stp flag", "stp_flag"],
    "swp_flag": ["swp flag", "swp_flag"],
    "switch_flag": ["switch flag", "switch_flag"],
    "settlement_type": ["settlement type", "settlement_type"],
    "amc_ind": ["amc_ind", "amc ind"],
    "face_value": ["face value", "face_value"],
    "start_date": ["start date", "start_date"],
    "end_date": ["end date", "end_date"],
    "exit_load_flag": ["exit load flag", "exit_load_flag"],
    "exit_load": ["exit load", "exit_load"],
    "lock_in_period_flag": ["lock-in period flag", "lock in period flag", "lock_in_period_flag"],
    "lock_in_period": ["lock-in period", "lock in period", "lock_in_period"],
    "channel_partner_code": ["channel partner code", "channel_partner_code"],
    "reopening_date": ["reopening date", "reopening_date"],
}

_REAL_FIELDS = {
    "min_purchase_amount", "additional_purchase_amount", "max_purchase_amount",
    "purchase_amount_multiplier", "min_redemption_qty", "redemption_qty_multiplier",
    "max_redemption_qty", "redemption_amount_minimum", "redemption_amount_maximum",
    "redemption_amount_multiple", "face_value", "exit_load",
}
_DATE_FIELDS = {"start_date", "end_date", "reopening_date"}


def _resolve_columns(df: pd.DataFrame) -> dict[str, str | None]:
    """Map logical field name → actual df column name (normalised headers)."""
    norm_headers = {
        col.lower()
        .strip()
        .replace("\u200b", "")
        .replace("\u00e2\u20ac\u201c", "-")
        .replace("\u201c", "-")
        .replace("\u2013", "-")
        .replace("\u2014", "-")
        .replace("  ", " "): col
        for col in df.columns
    }
    resolved: dict[str, str | None] = {}
    for field, candidates in _COL_MAP.items():
        found = None
        for cand in candidates:
            if cand.lower().strip() in norm_headers:
                found = norm_headers[cand.lower().strip()]
                break
        resolved[field] = found
    return resolved


# ══════════════════════════════════════════════════════════════════════════════
# BSE PARSERS
# ══════════════════════════════════════════════════════════════════════════════

def parse_bse_client_master(file, replace: bool) -> tuple[bool, str]:
    """
    BSE Client Master Excel → clients + clients_bank + clients_nominee tables.
    Maps all raw columns from the BSE Client Master file.
    """
    file.seek(0)
    try:
        df = pd.read_excel(file, dtype=str)
    except Exception as exc:
        return False, f"File read error: {exc}"

    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]

    def _c(*candidates):
        return next((c for c in candidates if c in df.columns), None)

    # Core identity columns
    code_col = _c("client_code", "ucc")
    fname_col = _c("primary_holder_first_name", "first_name")
    mname_col = _c("primary_holder_middle_name", "middle_name")
    lname_col = _c("primary_holder_last_name", "last_name")
    pan_col = _c("primary_holder_pan", "pan")
    tax_col = _c("tax_status")
    gender_col = _c("gender")
    dob_col = _c("date_of_birth", "dob")
    occ_col = _c("occupation_code", "occupation")
    hold_col = _c("holding_nature", "mode_of_holding")
    kyc_col = _c("kyc_type")
    ckyc_col = _c("ckyc_number")
    aadh_col = _c("aadhaar_updated", "aadhaar_flag")
    email_col = _c("email", "primary_holder_email")
    mobile_col = _c("indian_mobile_no_", "mobile", "mobile_no")
    addr1_col = _c("address1", "address_1", "address_line_1")
    addr2_col = _c("address2", "address_2")
    addr3_col = _c("address3", "address_3")
    city_col = _c("city")
    state_col = _c("state")
    pin_col = _c("pincode", "pin_code")
    country_col = _c("country")
    created_col = _c("created_at", "creation_date")
    modified_col = _c("last_modified_at", "last_modified_date")
    member_col = _c("member_code", "member_id")

    if not code_col or not fname_col:
        return False, "Missing critical columns: client_code / primary_holder_first_name"

    client_rows = []
    bank_rows = []
    nominee_rows = []

    for _, row in df.iterrows():
        code = clean_str(row.get(code_col, ""))
        if not code:
            continue

        client_rows.append((
            code,
            clean_str(row.get(member_col, "")) if member_col else "",
            clean_str(row.get(fname_col, "")),
            clean_str(row.get(mname_col, "")) if mname_col else "",
            clean_str(row.get(lname_col, "")) if lname_col else "",
            clean_str(row.get(pan_col, "")).upper() if pan_col else "",
            clean_str(row.get(tax_col, "")) if tax_col else "",
            clean_str(row.get(gender_col, "")) if gender_col else "",
            parse_date_safe(row.get(dob_col, "")) if dob_col else "",
            clean_str(row.get(occ_col, "")) if occ_col else "",
            clean_str(row.get(hold_col, "")) if hold_col else "",
            clean_str(row.get(kyc_col, "")) if kyc_col else "",
            clean_str(row.get(ckyc_col, "")) if ckyc_col else "",
            clean_str(row.get(aadh_col, "")) if aadh_col else "",
            clean_str(row.get(email_col, "")).lower() if email_col else "",
            clean_str(row.get(mobile_col, "")) if mobile_col else "",
            clean_str(row.get(addr1_col, "")) if addr1_col else "",
            clean_str(row.get(addr2_col, "")) if addr2_col else "",
            clean_str(row.get(addr3_col, "")) if addr3_col else "",
            clean_str(row.get(city_col, "")) if city_col else "",
            clean_str(row.get(state_col, "")) if state_col else "",
            clean_str(row.get(pin_col, "")) if pin_col else "",
            clean_str(row.get(country_col, "")) if country_col else "",
            parse_date_safe(row.get(created_col, "")) if created_col else "",
            parse_date_safe(row.get(modified_col, "")) if modified_col else "",
        ))

        # Bank accounts — BSE has up to 5 (bank1_*, bank2_*, ...)
        for seq in range(1, 6):
            pfx = f"bank{seq}_"
            acno = _c(f"{pfx}account_no", f"{pfx}ac_no", f"bank_account_no_{seq}")
            if acno and clean_str(row.get(acno, "")):
                bank_rows.append((
                    code, seq,
                    clean_str(row.get(_c(f"{pfx}account_type", f"bank_account_type_{seq}") or "", "")),
                    clean_str(row.get(acno, "")),
                    clean_str(row.get(_c(f"{pfx}micr_no", f"micr_{seq}") or "", "")),
                    clean_str(row.get(_c(f"{pfx}ifsc_code", f"ifsc_{seq}") or "", "")),
                    clean_str(row.get(_c(f"{pfx}bank_name", f"bank_name_{seq}") or "", "")),
                    clean_str(row.get(_c(f"{pfx}bank_branch", f"branch_{seq}") or "", "")),
                    "Y" if seq == 1 else "N",
                    "Active",
                    parse_date_safe(row.get(created_col, "")) if created_col else "",
                ))

        # Nominees — BSE has up to 3
        for seq in range(1, 4):
            pfx = f"nominee{seq}_"
            nname = _c(f"{pfx}name", f"nominee_{seq}_name")
            if nname and clean_str(row.get(nname, "")):
                nominee_rows.append((
                    code, seq,
                    clean_str(row.get(nname, "")),
                    clean_str(row.get(_c(f"{pfx}relationship") or "", "")),
                    _sf(row.get(_c(f"{pfx}percentage") or "", 0)),
                    clean_str(row.get(_c(f"{pfx}is_minor") or "", "")),
                    parse_date_safe(row.get(_c(f"{pfx}dob") or "", "")),
                    clean_str(row.get(_c(f"{pfx}guardian_name") or "", "")),
                    clean_str(row.get(_c(f"{pfx}guardian_pan") or "", "")),
                    clean_str(row.get(_c(f"{pfx}pan") or "", "")).upper(),
                    clean_str(row.get(_c(f"{pfx}email") or "", "")).lower(),
                    clean_str(row.get(_c(f"{pfx}mobile") or "", "")),
                    clean_str(row.get(_c(f"{pfx}address1") or "", "")),
                    clean_str(row.get(_c(f"{pfx}address2") or "", "")),
                    clean_str(row.get(_c(f"{pfx}address3") or "", "")),
                    clean_str(row.get(_c(f"{pfx}city") or "", "")),
                    clean_str(row.get(_c(f"{pfx}pincode") or "", "")),
                ))

    if not client_rows:
        return False, "No valid rows found"

    inserted = skipped = 0
    with get_conn() as conn:
        if replace:
            conn.execute("DELETE FROM clients")
            conn.executemany(
                """INSERT OR IGNORE INTO clients
                (client_code,member_code,first_name,middle_name,last_name,pan,tax_status,gender,dob,
                 occupation_code,holding_nature,kyc_type,ckyc_number,aadhaar_updated,email,mobile,
                 address1,address2,address3,city,state,pincode,country,created_at,last_modified_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                client_rows)
            inserted = len(client_rows)
        else:
            existing = {r[0] for r in conn.execute("SELECT client_code FROM clients").fetchall()}
            new_rows = [r for r in client_rows if r[0] not in existing]
            skipped = len(client_rows) - len(new_rows)
            if new_rows:
                conn.executemany(
                    """INSERT OR IGNORE INTO clients
                    (client_code,member_code,first_name,middle_name,last_name,pan,tax_status,gender,dob,
                     occupation_code,holding_nature,kyc_type,ckyc_number,aadhaar_updated,email,mobile,
                     address1,address2,address3,city,state,pincode,country,created_at,last_modified_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    new_rows)
            inserted = len(new_rows)

        if bank_rows:
            conn.executemany(
                """INSERT OR IGNORE INTO clients_bank
                (client_code,bank_seq,account_type,account_no,micr_no,ifsc_code,bank_name,bank_branch,
                 is_default,status,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                bank_rows)
        if nominee_rows:
            conn.executemany(
                """INSERT OR IGNORE INTO clients_nominee
                (client_code,nominee_seq,name,relationship,percentage,is_minor,dob,guardian_name,
                 guardian_pan,pan,email,mobile,address1,address2,address3,city,pincode)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                nominee_rows)

    msg = f"Imported {inserted} clients"
    if skipped:
        msg += f" | Skipped {skipped} existing"
    if bank_rows:
        msg += f" | {len(bank_rows)} bank records"
    if nominee_rows:
        msg += f" | {len(nominee_rows)} nominee records"
    return True, msg


def parse_bse_xsip(file, replace: bool) -> tuple[bool, str, dict]:
    """
    BSE XSIP Report Excel → bse_xsip table.
    """
    file.seek(0)
    try:
        df = pd.read_excel(file, dtype=str)
    except Exception as exc:
        return False, f"File read error: {exc}", {}

    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]

    def _c(*cands):
        return next((c for c in cands if c in df.columns), None)

    xsip_col = _c("xsip_regn_no", "registration_no", "regn_no")
    client_col = _c("client_code", "ucc")
    name_col = _c("client_name", "investor_name", "name")
    member_col = _c("member_code")
    status_col = _c("status", "sip_status")
    amc_col = _c("amc_name", "amc")
    scheme_col = _c("rta_scheme_code", "scheme_code")
    sname_col = _c("scheme_name")
    freq_col = _c("frequency_type", "frequency")
    sd_col = _c("start_date")
    ed_col = _c("end_date")
    amt_col = _c("installments_amt", "sip_amount", "amount")
    inst_col = _c("no_of_installments", "installments")
    fo_col = _c("first_order")
    folio_col = _c("folio_no", "folio")
    mandate_col = _c("mandate_id")
    euin_col = _c("euin")
    sub_col = _c("sub_broker", "sub_broker_code")
    regn_col = _c("regn_date", "registration_date")
    pgref_col = _c("pg_bank_ref_no", "pg_ref_no")
    rem_col = _c("remarks")
    email_col = _c("primary_email", "email")
    mob_col = _c("primary_mobile", "mobile")

    if not xsip_col or not client_col:
        return False, f"Missing: xsip_regn_no / client_code. Found: {list(df.columns)[:15]}", {}

    batch = _batch_id("BSE_XSIP", file.name)
    rows, skipped = [], 0
    for _, row in df.iterrows():
        regn = clean_str(row.get(xsip_col, ""))
        if not regn:
            skipped += 1
            continue
        rows.append((
            regn,
            clean_str(row.get(client_col, "")),
            clean_str(row.get(name_col, "")) if name_col else "",
            clean_str(row.get(member_col, "")) if member_col else "",
            clean_str(row.get(status_col, "")) if status_col else "",
            clean_str(row.get(amc_col, "")) if amc_col else "",
            clean_str(row.get(scheme_col, "")) if scheme_col else "",
            clean_str(row.get(sname_col, "")) if sname_col else "",
            clean_str(row.get(freq_col, "")) if freq_col else "",
            parse_date_safe(row.get(sd_col, "")) if sd_col else "",
            parse_date_safe(row.get(ed_col, "")) if ed_col else "",
            _sf(row.get(amt_col, 0)) if amt_col else 0.0,
            _si(row.get(inst_col, 0)) if inst_col else 0,
            clean_str(row.get(fo_col, "")) if fo_col else "",
            clean_str(row.get(folio_col, "")) if folio_col else "",
            clean_str(row.get(mandate_col, "")) if mandate_col else "",
            clean_str(row.get(euin_col, "")) if euin_col else "",
            clean_str(row.get(sub_col, "")) if sub_col else "",
            parse_date_safe(row.get(regn_col, "")) if regn_col else "",
            clean_str(row.get(pgref_col, "")) if pgref_col else "",
            clean_str(row.get(rem_col, "")) if rem_col else "",
            clean_str(row.get(email_col, "")).lower() if email_col else "",
            clean_str(row.get(mob_col, "")) if mob_col else "",
            batch,
        ))

    if not rows:
        return False, "0 XSIP rows found", {}

    inserted = dupes = 0
    with get_conn() as conn:
        if replace:
            conn.execute("DELETE FROM bse_xsip")
            conn.executemany(
                """INSERT OR IGNORE INTO bse_xsip
                (xsip_regn_no,client_code,client_name,member_code,status,amc_name,rta_scheme_code,
                 scheme_name,frequency_type,start_date,end_date,installments_amt,no_of_installments,
                 first_order,folio_no,mandate_id,euin,sub_broker,regn_date,pg_bank_ref_no,remarks,
                 primary_email,primary_mobile,upload_batch)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                rows)
            inserted = len(rows)
        else:
            before = _count_before_after(conn, "bse_xsip")
            conn.executemany(
                """INSERT OR IGNORE INTO bse_xsip
                (xsip_regn_no,client_code,client_name,member_code,status,amc_name,rta_scheme_code,
                 scheme_name,frequency_type,start_date,end_date,installments_amt,no_of_installments,
                 first_order,folio_no,mandate_id,euin,sub_broker,regn_date,pg_bank_ref_no,remarks,
                 primary_email,primary_mobile,upload_batch)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                rows)
            inserted, dupes = _inserted_dupes(before, conn, "bse_xsip", len(rows))

    active = sum(1 for r in rows if "active" in str(r[4]).lower())
    preview = {"rows": inserted, "skipped": skipped, "duplicates": dupes, "active": active}
    msg = f"Imported {inserted} XSIP records | Active: {active}"
    if skipped: msg += f" | Skipped {skipped}"
    if dupes:   msg += f" | {dupes} duplicates"
    return True, msg, preview


def parse_bse_scheme_master(file, replace: bool) -> tuple[bool, str, dict]:
    """
    BSE Scheme Master Excel/CSV → bse_scheme_master table.
    Stores all 42 raw columns. Reuses existing helpers (clean_str, _sf, parse_date_safe).
    """
    file.seek(0)
    fname = getattr(file, "name", "")
    try:
        if fname.lower().endswith((".xlsx", ".xls")):
            df = pd.read_excel(file, dtype=str)
        else:
            df = None
            for sep in ("\t", ","):
                file.seek(0)
                try:
                    candidate = pd.read_csv(file, sep=sep, dtype=str,
                                            encoding="utf-8", encoding_errors="replace")
                    if len(candidate.columns) > 5:
                        df = candidate
                        break
                except Exception:
                    continue
            if df is None:
                return False, "Could not parse file — try Excel or TSV format", {}
    except Exception as exc:
        return False, f"File read error: {exc}", {}

    df = _clean_cols(df)
    col = _resolve_columns(df)

    required = ["scheme_code", "rta_scheme_code", "isin"]
    missing = [f for f in required if col[f] is None]
    if missing:
        return False, f"Missing required columns: {missing}. Found headers: {list(df.columns[:10])}", {}

    batch = _batch_id("BSE_SCHEME", fname)
    rows: list[tuple] = []
    skipped = 0

    for _, row in df.iterrows():
        sc = clean_str(row.get(col["scheme_code"], ""))
        rta = clean_str(row.get(col["rta_scheme_code"], ""))
        if not sc and not rta:
            skipped += 1
            continue

        def _get(field: str):
            src = col[field]
            if src is None:
                return 0.0 if field in _REAL_FIELDS else ""
            raw = row.get(src, "")
            if field in _REAL_FIELDS:
                return _sf(raw)
            if field in _DATE_FIELDS:
                return parse_date_safe(raw)
            return clean_str(raw)

        try:
            rows.append((
                _get("unique_no"), sc, rta,
                _get("amc_scheme_code"), _get("isin"), _get("amc_code"),
                _get("scheme_type"), _get("scheme_plan"), _get("scheme_name"),
                _get("purchase_allowed"), _get("purchase_transaction_mode"),
                _get("min_purchase_amount"), _get("additional_purchase_amount"),
                _get("max_purchase_amount"), _get("purchase_amount_multiplier"),
                _get("purchase_cutoff_time"), _get("redemption_allowed"),
                _get("redemption_transaction_mode"), _get("min_redemption_qty"),
                _get("redemption_qty_multiplier"), _get("max_redemption_qty"),
                _get("redemption_amount_minimum"), _get("redemption_amount_maximum"),
                _get("redemption_amount_multiple"), _get("redemption_cutoff_time"),
                _get("rta_agent_code"), _get("amc_active_flag"),
                _get("dividend_reinvestment_flag"), _get("sip_flag"),
                _get("stp_flag"), _get("swp_flag"), _get("switch_flag"),
                _get("settlement_type"), _get("amc_ind"), _get("face_value"),
                _get("start_date"), _get("end_date"), _get("exit_load_flag"),
                _get("exit_load"), _get("lock_in_period_flag"), _get("lock_in_period"),
                _get("channel_partner_code"), _get("reopening_date"),
                batch,
            ))
        except Exception as exc:
            log.warning("Skipped scheme master row: %s", exc)
            skipped += 1

    if not rows:
        return False, "0 rows parsed — check file format", {}

    sql = """
        INSERT OR IGNORE INTO bse_scheme_master
        (unique_no, scheme_code, rta_scheme_code, amc_scheme_code, isin,
         amc_code, scheme_type, scheme_plan, scheme_name,
         purchase_allowed, purchase_transaction_mode,
         min_purchase_amount, additional_purchase_amount, max_purchase_amount,
         purchase_amount_multiplier, purchase_cutoff_time,
         redemption_allowed, redemption_transaction_mode,
         min_redemption_qty, redemption_qty_multiplier, max_redemption_qty,
         redemption_amount_minimum, redemption_amount_maximum, redemption_amount_multiple,
         redemption_cutoff_time, rta_agent_code, amc_active_flag,
         dividend_reinvestment_flag, sip_flag, stp_flag, swp_flag, switch_flag,
         settlement_type, amc_ind, face_value, start_date, end_date,
         exit_load_flag, exit_load, lock_in_period_flag, lock_in_period,
         channel_partner_code, reopening_date, upload_batch)
        VALUES (""" + ",".join(["?"] * 44) + ")"

    inserted = dupes = 0
    with get_conn() as conn:
        if replace:
            conn.execute("DELETE FROM bse_scheme_master")
            conn.executemany(sql, rows)
            inserted = len(rows)
        else:
            before = _count_before_after(conn, "bse_scheme_master")
            conn.executemany(sql, rows)
            inserted, dupes = _inserted_dupes(before, conn, "bse_scheme_master", len(rows))

    cams_count = sum(1 for r in rows if "CAMS" in str(r[25]).upper())
    kfin_count = sum(1 for r in rows if any(k in str(r[25]).upper() for k in ("KFIN", "KARVY", "KFINTECH")))
    sip_count = sum(1 for r in rows if str(r[28]).upper() == "Y")
    isin_count = sum(1 for r in rows if r[4])

    preview = {
        "rows": inserted, "skipped": skipped, "duplicates": dupes,
        "cams": cams_count, "kfin": kfin_count,
        "sip_eligible": sip_count, "with_isin": isin_count,
    }

    msg = f"Imported {inserted} schemes"
    if skipped: msg += f" | Skipped {skipped}"
    if dupes:   msg += f" | {dupes} duplicates"
    msg += f" | CAMS: {cams_count} | KFin: {kfin_count} | SIP-eligible: {sip_count}"
    return True, msg, preview


# ══════════════════════════════════════════════════════════════════════════════
# CAMS PARSERS
# ══════════════════════════════════════════════════════════════════════════════

def parse_cams_folio(file, replace: bool) -> tuple[bool, str, dict]:
    """WBR9 → cams_folio (all raw columns)."""
    df = _read_csv_auto(file)
    if df is None:
        return False, "Could not parse file", {}
    df = _clean_cols(df)

    # WBR9 actual column names
    required = ["FOLIO_NO", "INV_NAME"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        # Try alternate header
        alt = [c for c in ["FOLIOCHK"] if c in df.columns]
        if not alt:
            return False, f"Missing columns: {missing}. Found: {list(df.columns)[:20]}", {}
        df.rename(columns={"FOLIOCHK": "FOLIO_NO"}, inplace=True)

    batch = _batch_id("CAMS_R9", file.name)
    rows, skipped = [], 0
    for _, row in df.iterrows():
        folio = clean_str(row.get("FOLIO_NO", ""))
        if not folio:
            skipped += 1
            continue
        try:
            rows.append((
                folio,
                clean_str(row.get("INV_NAME", "")).strip(),
                clean_str(row.get("PAN_NO", "")).upper(),
                clean_str(row.get("PRODUCT", row.get("PRODCODE", ""))),
                clean_str(row.get("SCH_NAME", row.get("SCHEME", ""))),
                clean_str(row.get("AMC_CODE", "")),
                _sf(row.get("CLOS_BAL", 0)),
                _sf(row.get("RUPEE_BAL", 0)),
                parse_date_safe(row.get("REP_DATE", "")),
                clean_str(row.get("ADDRESS1", "")),
                clean_str(row.get("ADDRESS2", "")),
                clean_str(row.get("ADDRESS3", "")),
                clean_str(row.get("CITY", "")),
                clean_str(row.get("PINCODE", "")),
                clean_str(row.get("EMAIL", "")).lower(),
                clean_str(row.get("MOBILE_NO", "")),
                parse_date_safe(row.get("INV_DOB", "")),
                clean_str(row.get("OCCUPATION", "")),
                clean_str(row.get("TAX_STATUS", "")),
                clean_str(row.get("HOLDING_NATURE", "")),
                clean_str(row.get("JNT_NAME1", "")),
                clean_str(row.get("JNT_NAME2", "")),
                clean_str(row.get("JOINT1_PAN", "")).upper(),
                clean_str(row.get("JOINT2_PAN", "")).upper(),
                clean_str(row.get("BANK_NAME", "")),
                clean_str(row.get("BRANCH", "")),
                clean_str(row.get("AC_TYPE", "")),
                clean_str(row.get("AC_NO", "")),
                clean_str(row.get("IFSC_CODE", "")),
                clean_str(row.get("NOM_NAME", "")),
                clean_str(row.get("NOM_RELATION", row.get("RELATION", ""))),
                _sf(row.get("NOM_PERCENTAGE", 0)),
                clean_str(row.get("NOM_EMAIL", "")),
                clean_str(row.get("NOM2_NAME", "")),
                clean_str(row.get("NOM2_RELATION", "")),
                _sf(row.get("NOM2_PERCENTAGE", 0)),
                clean_str(row.get("NOM3_NAME", "")),
                clean_str(row.get("NOM3_RELATION", "")),
                _sf(row.get("NOM3_PERCENTAGE", 0)),
                clean_str(row.get("BROKER_CODE", row.get("BROK_CODE", ""))),
                parse_date_safe(row.get("FOLIO_DATE", "")),
                clean_str(row.get("FOLIO_OLD", "")),
                clean_str(row.get("SCHEME_FOLIO_NUMBER", "")),
                clean_str(row.get("GST_STATE_CODE", "")),
                batch,
            ))
        except Exception as exc:
            log.warning("Skipped CAMS folio row: %s", exc)
            skipped += 1

    if not rows:
        return False, "0 folio rows parsed", {}

    inserted = dupes = 0
    with get_conn() as conn:
        if replace:
            conn.execute("DELETE FROM cams_folio")
            conn.executemany(
                """INSERT OR IGNORE INTO cams_folio
                (folio_no,inv_name,pan_no,product,sch_name,amc_code,clos_bal,rupee_bal,rep_date,
                 address1,address2,address3,city,pincode,email,mobile_no,inv_dob,occupation,tax_status,
                 holding_nature,jnt_name1,jnt_name2,joint1_pan,joint2_pan,bank_name,branch,ac_type,
                 ac_no,ifsc_code,nom_name,nom_relation,nom_percentage,nom_email,nom2_name,nom2_relation,
                 nom2_percentage,nom3_name,nom3_relation,nom3_percentage,broker_code,folio_date,folio_old,
                 scheme_folio_number,gst_state_code,upload_batch)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                rows)
            inserted = len(rows)
        else:
            before = _count_before_after(conn, "cams_folio")
            conn.executemany(
                """INSERT OR IGNORE INTO cams_folio
                (folio_no,inv_name,pan_no,product,sch_name,amc_code,clos_bal,rupee_bal,rep_date,
                 address1,address2,address3,city,pincode,email,mobile_no,inv_dob,occupation,tax_status,
                 holding_nature,jnt_name1,jnt_name2,joint1_pan,joint2_pan,bank_name,branch,ac_type,
                 ac_no,ifsc_code,nom_name,nom_relation,nom_percentage,nom_email,nom2_name,nom2_relation,
                 nom2_percentage,nom3_name,nom3_relation,nom3_percentage,broker_code,folio_date,folio_old,
                 scheme_folio_number,gst_state_code,upload_batch)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                rows)
            inserted, dupes = _inserted_dupes(before, conn, "cams_folio", len(rows))

    total_aum = sum(r[7] for r in rows)
    preview = {"rows": inserted, "skipped": skipped, "duplicates": dupes,
               "total_aum": total_aum,
               "unique_folios": len({r[0] for r in rows}),
               "unique_investors": len({r[1] for r in rows})}
    msg = f"Imported {inserted} folio records | AUM: {format_aum(total_aum)}"
    if skipped: msg += f" | Skipped {skipped}"
    if dupes:   msg += f" | {dupes} duplicates"
    return True, msg, preview


def parse_cams_transactions(file, replace: bool) -> tuple[bool, str, dict]:
    """WBR2 → cams_transactions (all raw columns)."""
    df = _read_csv_auto(file)
    if df is None:
        return False, "Could not parse file", {}
    df = _clean_cols(df)

    # WBR2 column names
    required = ["FOLIO_NO", "TRXNNO", "AMOUNT"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        return False, f"Missing columns: {missing}. Found: {list(df.columns)[:20]}", {}

    batch = _batch_id("CAMS_R2", file.name)
    rows, skipped = [], 0
    for _, row in df.iterrows():
        try:
            rows.append((
                clean_str(row.get("AMC_CODE", "")),
                clean_str(row.get("FOLIO_NO", "")),
                clean_str(row.get("PRODCODE", row.get("PRODUCT", ""))),
                clean_str(row.get("SCHEME", "")),
                clean_str(row.get("INV_NAME", "")).strip(),
                clean_str(row.get("PAN", "")).upper(),
                clean_str(row.get("TRXNNO", "")),
                clean_str(row.get("TRXNTYPE", "")),
                clean_str(row.get("TRXNMODE", "")),
                clean_str(row.get("TRXNSTAT", "")),
                clean_str(row.get("TRXNSUBTYPE", "")),
                parse_date_safe(row.get("TRADDATE", "")),
                parse_date_safe(row.get("POSTDATE", "")),
                _sf(row.get("PURPRICE", 0)),
                _sf(row.get("UNITS", 0)),
                _sf(row.get("AMOUNT", 0)),
                _sf(row.get("STAMP_DUTY", 0)),
                _sf(row.get("STT", 0)),
                clean_str(row.get("SIPTRXNNO", "")),
                clean_str(row.get("REQUEST_REF_NO", "")),
                _sf(row.get("IGST_AMOUNT", 0)),
                _sf(row.get("CGST_AMOUNT", 0)),
                _sf(row.get("SGST_AMOUNT", 0)),
                clean_str(row.get("AC_NO", "")),
                clean_str(row.get("BANK_NAME", "")),
                clean_str(row.get("BROKER_CODE", row.get("BROK_CODE", ""))),
                clean_str(row.get("EUIN", "")),
                clean_str(row.get("REMARKS", "")),
                parse_date_safe(row.get("SYS_REGN_DATE", "")),
                parse_date_safe(row.get("REP_DATE", "")),
                batch,
            ))
        except Exception as exc:
            log.warning("Skipped CAMS txn row: %s", exc)
            skipped += 1

    if not rows:
        return False, "0 transaction rows parsed", {}

    inserted = dupes = 0
    with get_conn() as conn:
        if replace:
            conn.execute("DELETE FROM cams_transactions")
            conn.executemany(
                """INSERT OR IGNORE INTO cams_transactions
                (amc_code,folio_no,product,scheme,inv_name,pan,trxn_no,trxn_type,trxn_mode,trxn_status,
                 trxn_subtype,trade_date,post_date,nav,units,amount,stamp_duty,stt,sip_trxn_no,
                 request_ref_no,igst_amount,cgst_amount,sgst_amount,ac_no,bank_name,broker_code,euin,
                 remarks,sys_regn_date,rep_date,upload_batch)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                rows)
            inserted = len(rows)
        else:
            before = _count_before_after(conn, "cams_transactions")
            conn.executemany(
                """INSERT OR IGNORE INTO cams_transactions
                (amc_code,folio_no,product,scheme,inv_name,pan,trxn_no,trxn_type,trxn_mode,trxn_status,
                 trxn_subtype,trade_date,post_date,nav,units,amount,stamp_duty,stt,sip_trxn_no,
                 request_ref_no,igst_amount,cgst_amount,sgst_amount,ac_no,bank_name,broker_code,euin,
                 remarks,sys_regn_date,rep_date,upload_batch)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                rows)
            inserted, dupes = _inserted_dupes(before, conn, "cams_transactions", len(rows))

    total_amt = sum(r[15] for r in rows)
    preview = {"rows": inserted, "skipped": skipped, "duplicates": dupes,
               "total_amount": total_amt,
               "schemes": len({r[3] for r in rows}),
               "folios": len({r[1] for r in rows})}
    msg = f"Imported {inserted} transactions | {format_currency(total_amt, 0)}"
    if skipped: msg += f" | Skipped {skipped}"
    if dupes:   msg += f" | {dupes} duplicates"
    return True, msg, preview


def parse_cams_sip(file, replace: bool) -> tuple[bool, str, dict]:
    """WBR49 → cams_sip (all raw columns)."""
    df = _read_csv_auto(file)
    if df is None:
        return False, "Could not parse file", {}
    df = _clean_cols(df)

    required = ["FOLIO_NO", "AUTO_TRNO", "AUTO_AMOUNT"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        return False, f"Missing columns: {missing}. Found: {list(df.columns)[:20]}", {}

    batch = _batch_id("CAMS_R49", file.name)
    rows, skipped = [], 0
    for _, row in df.iterrows():
        folio = clean_str(row.get("FOLIO_NO", ""))
        autotr = clean_str(row.get("AUTO_TRNO", ""))
        if not folio or not autotr:
            skipped += 1
            continue
        try:
            rows.append((
                folio,
                clean_str(row.get("INV_NAME", "")).strip(),
                clean_str(row.get("PAN", "")).upper(),
                clean_str(row.get("PRODUCT", "")),
                clean_str(row.get("SCHEME", "")),
                clean_str(row.get("AMC_CODE", "")),
                autotr,
                clean_str(row.get("REQUEST_REF_NO", "")),
                _sf(row.get("AUTO_AMOUNT", 0)),
                parse_date_safe(row.get("FROM_DATE", "")),
                parse_date_safe(row.get("TO_DATE", "")),
                parse_date_safe(row.get("CEASE_DATE", "")),
                clean_str(row.get("PERIODICITY", "")),
                _si(row.get("PERIOD_DAY", 1)),
                clean_str(row.get("PAYMENT_MODE", "")),
                clean_str(row.get("BANK", "")),
                clean_str(row.get("AC_TYPE", "")),
                clean_str(row.get("STATUS", "Active")),
                clean_str(row.get("EUIN", "")),
                clean_str(row.get("BROKER_CODE", row.get("BROK_CODE", ""))),
                parse_date_safe(row.get("REG_DATE", "")),
                clean_str(row.get("REMARKS", "")),
                batch,
            ))
        except Exception as exc:
            log.warning("Skipped CAMS SIP row: %s", exc)
            skipped += 1

    if not rows:
        return False, "0 SIP rows parsed", {}

    inserted = dupes = 0
    with get_conn() as conn:
        if replace:
            conn.execute("DELETE FROM cams_sip")
            conn.executemany(
                """INSERT OR IGNORE INTO cams_sip
                (folio_no,inv_name,pan,product,scheme,amc_code,auto_trno,request_ref_no,auto_amount,
                 from_date,to_date,cease_date,periodicity,period_day,payment_mode,bank,ac_type,status,
                 euin,broker_code,reg_date,remarks,upload_batch)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                rows)
            inserted = len(rows)
        else:
            before = _count_before_after(conn, "cams_sip")
            conn.executemany(
                """INSERT OR IGNORE INTO cams_sip
                (folio_no,inv_name,pan,product,scheme,amc_code,auto_trno,request_ref_no,auto_amount,
                 from_date,to_date,cease_date,periodicity,period_day,payment_mode,bank,ac_type,status,
                 euin,broker_code,reg_date,remarks,upload_batch)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                rows)
            inserted, dupes = _inserted_dupes(before, conn, "cams_sip", len(rows))

    active = sum(1 for r in rows if "active" in str(r[17]).lower())
    ceased = sum(1 for r in rows if "cease" in str(r[17]).lower())
    completed = sum(1 for r in rows if "complet" in str(r[17]).lower())
    preview = {"rows": inserted, "skipped": skipped, "duplicates": dupes,
               "active": active, "ceased": ceased, "completed": completed}
    msg = f"Imported {inserted} SIPs | Active: {active} | Ceased: {ceased} | Completed: {completed}"
    if skipped: msg += f" | Skipped {skipped}"
    if dupes:   msg += f" | {dupes} duplicates"
    return True, msg, preview


def parse_cams_aum(file, replace: bool) -> tuple[bool, str, dict]:
    """CAMS AUM report → cams_aum (all raw columns)."""
    df = _read_csv_auto(file)
    if df is None:
        return False, "Could not parse AUM file", {}
    df = _clean_cols(df)

    folio_col = next((c for c in ["FOLIO_NO", "FOLIOCHK"] if c in df.columns), None)
    if not folio_col or "RUPEE_BAL" not in df.columns:
        return False, f"Missing FOLIO_NO/FOLIOCHK or RUPEE_BAL. Found: {list(df.columns)[:15]}", {}

    batch = _batch_id("CAMS_AUM", file.name)
    rows, skipped = [], 0
    for _, row in df.iterrows():
        folio = clean_str(row.get(folio_col, ""))
        if not folio:
            skipped += 1
            continue
        try:
            rows.append((
                folio,
                clean_str(row.get("INV_NAME", "")).strip(),
                clean_str(row.get("PAN_NO", "")).upper(),
                clean_str(row.get("PRODUCT", row.get("PRODCODE", ""))),
                clean_str(row.get("SCH_NAME", row.get("SCHEME", ""))),
                clean_str(row.get("AMC_CODE", "")),
                _sf(row.get("CLOS_BAL", 0)),
                _sf(row.get("RUPEE_BAL", 0)),
                clean_str(row.get("EMAIL", "")).lower(),
                parse_date_safe(row.get("REP_DATE", "")),
                batch,
            ))
        except Exception as exc:
            log.warning("Skipped CAMS AUM row: %s", exc)
            skipped += 1

    if not rows:
        return False, "0 AUM rows parsed", {}

    inserted = dupes = 0
    with get_conn() as conn:
        if replace:
            conn.execute("DELETE FROM cams_aum")
            conn.executemany(
                """INSERT OR IGNORE INTO cams_aum
                (folio_no,inv_name,pan_no,product,sch_name,amc_code,clos_bal,rupee_bal,email,rep_date,upload_batch)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                rows)
            inserted = len(rows)
        else:
            before = _count_before_after(conn, "cams_aum")
            conn.executemany(
                """INSERT OR IGNORE INTO cams_aum
                (folio_no,inv_name,pan_no,product,sch_name,amc_code,clos_bal,rupee_bal,email,rep_date,upload_batch)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                rows)
            inserted, dupes = _inserted_dupes(before, conn, "cams_aum", len(rows))

    total_aum = sum(r[7] for r in rows)
    preview = {"rows": inserted, "skipped": skipped, "duplicates": dupes, "total_aum": total_aum}
    msg = f"Imported {inserted} AUM rows | {format_aum(total_aum)}"
    if skipped: msg += f" | Skipped {skipped}"
    if dupes:   msg += f" | {dupes} duplicates"
    return True, msg, preview


def parse_cams_brokerage(file, replace: bool) -> tuple[bool, str, dict]:
    """WBR77 → cams_brokerage (ALL 107 raw columns)."""
    df = _read_csv_auto(file)
    if df is None:
        return False, "Could not parse file", {}
    df = _clean_cols(df)

    required = ["FOLIO_NO", "BRKAGE_AMT", "AMC_CODE"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        return False, f"Missing columns: {missing}. Found: {list(df.columns)[:15]}", {}

    batch = _batch_id("CAMS_WBR77", file.name)
    rows, skipped = [], 0

    for _, row in df.iterrows():
        try:
            rows.append((
                clean_str(row.get("AMC_CODE", "")),
                parse_date_safe(row.get("PROC_DATE", "")),
                clean_str(row.get("FOLIO_NO", "")),
                clean_str(row.get("SCHEME_CODE", "")),
                clean_str(row.get("TRXN_TYPE", "")),
                clean_str(row.get("TRXN_NO", "")),
                _sf(row.get("PLOT_AMOUNT", 0)),
                _sf(row.get("PLOT_UNITS", 0)),
                parse_date_safe(row.get("POST_DATE", "")),
                parse_date_safe(row.get("TRADE_DATE_TIME", "")),
                parse_date_safe(row.get("ENTRY_DATE", "")),
                clean_str(row.get("USER_CODE", "")),
                clean_str(row.get("USER_TRXNNO", "")),
                clean_str(row.get("TRXN_NATURE", "")),
                clean_str(row.get("TER_LOCATION", "")),
                parse_date_safe(row.get("SYS_REG_DATE", "")),
                clean_str(row.get("AUT_TXN_NO", "")),
                _sf(row.get("AUTO_AMOUNT", 0)),
                clean_str(row.get("AUT_TXN_TYPE", "")),
                parse_date_safe(row.get("CEASE_DATE", "")),
                parse_date_safe(row.get("REMED_DATE", "")),
                parse_date_safe(row.get("FORF_DATE", "")),
                clean_str(row.get("SRC_BRK_CODE", "")),
                clean_str(row.get("BROK_CODE", "")),
                clean_str(row.get("BRH_CODE", "")),
                clean_str(row.get("SUB_BRK_ARN", "")),
                clean_str(row.get("AE_CODE", "")),
                clean_str(row.get("ARN_EMP_CODE", "")),
                clean_str(row.get("EUIN_OPTED", "")),
                clean_str(row.get("EUIN_VALID", "")),
                clean_str(row.get("BRK_COMM_PAID", "")),
                clean_str(row.get("ADJ_FLAG", "")),
                clean_str(row.get("BRKAGE_TYPE", "")),
                _sf(row.get("BRKAGE_RATE", 0)),
                _sf(row.get("TOTAL_UPFRONT", 0)),
                clean_str(row.get("DEFER_FREQUENCY", "")),
                _si(row.get("DEFER_NO_OF_INSTALLMENT", 0)),
                _si(row.get("PAY_INSTALLMENT_NO", 0)),
                _sf(row.get("BRKAGE_AMT", 0)),
                parse_date_safe(row.get("BRKAGE_FROM", "")),
                parse_date_safe(row.get("BRKAGE_TO", "")),
                parse_date_safe(row.get("PROC_FROM_DATE", "")),
                parse_date_safe(row.get("PROC_TO_DATE", "")),
                clean_str(row.get("TRXN_DESC", "")),
                _si(row.get("SPL_UPF_TENURE", 0)),
                parse_date_safe(row.get("UPF_TENURE_END_DATE", "")),
                parse_date_safe(row.get("BRK_PAY_DT", "")),
                clean_str(row.get("CLW_TYPE", "")),
                clean_str(row.get("CLW_PERIOD", "")),
                clean_str(row.get("REC_FLAG", "")),
                parse_date_safe(row.get("P_SI_DATE", "")),
                clean_str(row.get("REC_PERIOD", "")),
                _sf(row.get("CLW_AMT", 0)),
                _sf(row.get("UPF_PAID", 0)),
                clean_str(row.get("FEE_ID", "")),
                clean_str(row.get("AM_CODE", "")),
                _sf(row.get("AM_COMM", 0)),
                _sf(row.get("AM_RATE", 0)),
                _sf(row.get("AVG_ASSETS", 0)),
                _sf(row.get("CAM_COMM", 0)),
                _sf(row.get("CAM_RATE", 0)),
                _sf(row.get("MAM_COMM", 0)),
                _sf(row.get("MAM_RATE", 0)),
                _si(row.get("NO_OF_DAYS", 0)),
                clean_str(row.get("ORIG_AE_CODE", "")),
                clean_str(row.get("ORIG_BRH_CODE", "")),
                clean_str(row.get("ORIG_BRK_CODE", "")),
                clean_str(row.get("RATE_REF_ID", "")),
                clean_str(row.get("REF_NO", "")),
                clean_str(row.get("TRXN_APP_NO", "")),
                clean_str(row.get("TXN_SCH_CODE", "")),
                clean_str(row.get("CLW_PRD", "")),
                clean_str(row.get("CLW_REQUIRED", "")),
                clean_str(row.get("P_SI_MIS_CODE", "")),
                clean_str(row.get("P_SI_USER_TRXNNO", "")),
                _si(row.get("SEQ_NO", 0)),
                _sf(row.get("P_SI_AMT", 0)),
                clean_str(row.get("P_SI_TR_NO", "")),
                clean_str(row.get("P_SI_TYPE", "")),
                _sf(row.get("PUR_SI_UNITS", 0)),
                clean_str(row.get("REMARKS", "")),
                clean_str(row.get("TO_SCHEME", "")),
                clean_str(row.get("TRXN_SIGN", "")),
                clean_str(row.get("BRK_POSTED", "")),
                clean_str(row.get("INV_NAME", "")).strip(),
                clean_str(row.get("BROK_GST_STATE_CODE", "")),
                _sf(row.get("IGST_RATE", 0)),
                _sf(row.get("CGST_RATE", 0)),
                _sf(row.get("SGST_RATE", 0)),
                _sf(row.get("IGST_VALUE", 0)),
                _sf(row.get("CGST_VALUE", 0)),
                _sf(row.get("SGST_VALUE", 0)),
                clean_str(row.get("LOCATION_CODE", "")),
                clean_str(row.get("PREV_FOLIO", "")),
                clean_str(row.get("BROK_CATEGORY", "")),
                clean_str(row.get("P_SCHEME_CODE", "")),
                clean_str(row.get("P_TRXN_TYPE", "")),
                clean_str(row.get("P_TRXN_NO", "")),
                clean_str(row.get("P_FOLIO_NO", "")),
                _sf(row.get("P_PLOT_AMOUNT", 0)),
                _sf(row.get("P_PLOT_UNITS", 0)),
                clean_str(row.get("FOLIO_OLD", "")),
                clean_str(row.get("SCHEME_FOLIO_NUMBER", "")),
                clean_str(row.get("AMC_REF_NO", "")),
                clean_str(row.get("REQUEST_REF_NO", "")),
                clean_str(row.get("WRITE_OFF_REASON", "")),
                clean_str(row.get("HOLD_REASON", "")),
                parse_date_safe(row.get("BROKERAGE_ACRUAL_MONTH", "")),
                clean_str(row.get("PREV_TRXN_NO", "")),
                parse_date_safe(row.get("PREV_TRXN_DATE", "")),
                batch,
            ))
        except Exception as exc:
            log.warning("Skipped WBR77 row: %s", exc)
            skipped += 1

    if not rows:
        return False, "0 rows parsed", {}

    inserted = dupes = 0
    sql = """INSERT OR IGNORE INTO cams_brokerage
        (amc_code,proc_date,folio_no,scheme_code,trxn_type,trxn_no,plot_amount,plot_units,
         post_date,trade_date_time,entry_date,user_code,user_trxnno,trxn_nature,ter_location,
         sys_reg_date,aut_txn_no,auto_amount,aut_txn_type,cease_date,remed_date,forf_date,
         src_brk_code,brok_code,brh_code,sub_brk_arn,ae_code,arn_emp_code,euin_opted,euin_valid,
         brk_comm_paid,adj_flag,brkage_type,brkage_rate,total_upfront,defer_frequency,
         defer_no_of_installment,pay_installment_no,brkage_amt,brkage_from,brkage_to,
         proc_from_date,proc_to_date,trxn_desc,spl_upf_tenure,upf_tenure_end_date,brk_pay_dt,
         clw_type,clw_period,rec_flag,p_si_date,rec_period,clw_amt,upf_paid,fee_id,am_code,
         am_comm,am_rate,avg_assets,cam_comm,cam_rate,mam_comm,mam_rate,no_of_days,
         orig_ae_code,orig_brh_code,orig_brk_code,rate_ref_id,ref_no,trxn_app_no,txn_sch_code,
         clw_prd,clw_required,p_si_mis_code,p_si_user_trxnno,seq_no,p_si_amt,p_si_tr_no,
         p_si_type,pur_si_units,remarks,to_scheme,trxn_sign,brk_posted,inv_name,
         brok_gst_state_code,igst_rate,cgst_rate,sgst_rate,igst_value,cgst_value,sgst_value,
         location_code,prev_folio,brok_category,p_scheme_code,p_trxn_type,p_trxn_no,p_folio_no,
         p_plot_amount,p_plot_units,folio_old,scheme_folio_number,amc_ref_no,request_ref_no,
         write_off_reason,hold_reason,brokerage_acrual_month,prev_trxn_no,prev_trxn_date,upload_batch)
        VALUES (""" + ",".join(["?"] * 111) + ")"

    with get_conn() as conn:
        if replace:
            conn.execute("DELETE FROM cams_brokerage")
            conn.executemany(sql, rows)
            inserted = len(rows)
        else:
            before = _count_before_after(conn, "cams_brokerage")
            conn.executemany(sql, rows)
            inserted, dupes = _inserted_dupes(before, conn, "cams_brokerage", len(rows))

    total = sum(r[38] for r in rows)
    months = sorted({r[106] for r in rows if r[106]})  # brokerage_acrual_month index
    preview = {"rows": inserted, "skipped": skipped, "duplicates": dupes,
               "total_brokerage": total, "months": months}
    msg = f"Imported {inserted} WBR77 rows | {format_brokerage(total)} brokerage"
    if skipped: msg += f" | Skipped {skipped}"
    if dupes:   msg += f" | {dupes} duplicates"
    return True, msg, preview


# ══════════════════════════════════════════════════════════════════════════════
# KFINTECH PARSERS
# ══════════════════════════════════════════════════════════════════════════════

def _kf_col(df: pd.DataFrame, *candidates) -> str | None:
    """Find first matching column (case-insensitive, space/underscore flexible)."""
    cols_upper = {c.upper().replace(" ", "_"): c for c in df.columns}
    for cand in candidates:
        key = cand.upper().replace(" ", "_")
        if key in cols_upper:
            return cols_upper[key]
        # partial match
        for col_key, col_orig in cols_upper.items():
            if key in col_key or col_key in key:
                return col_orig
    return None


def parse_kfin_folio(file, replace: bool) -> tuple[bool, str, dict]:
    """MFSD211 → kfin_folio (all raw columns)."""
    df = _read_csv_auto(file)
    if df is None:
        return False, "Could not parse file", {}
    df = _clean_cols(df)

    folio_col = _kf_col(df, "FOLIO", "FOLIO NUMBER", "FOLIO_NO", "ACCOUNT NUMBER")
    name_col = _kf_col(df, "INVESTOR NAME", "INV_NAME")
    if not folio_col or not name_col:
        return False, f"Missing Folio/Investor Name. Found: {list(df.columns)[:20]}", {}

    batch = _batch_id("KFIN_R9", file.name)
    rows, skipped = [], 0
    for _, row in df.iterrows():
        folio = clean_str(row.get(folio_col, ""))
        if not folio:
            skipped += 1
            continue
        try:
            prod_col = _kf_col(df, "PRODUCT CODE", "PROD CODE")
            fund_col = _kf_col(df, "FUND", "AMC CODE", "FUND CODE")
            sch_col = _kf_col(df, "SCHEME CODE", "SCH CODE")
            dopt_col = _kf_col(df, "DIVIDEND OPTION", "DIV OPT", "OPTION")
            fdesc_col = _kf_col(df, "FUND DESCRIPTION", "SCHEME NAME", "DESCRIPTION")
            email_col = _kf_col(df, "EMAIL", "E-MAIL", "EMAIL ID")
            mob_col = _kf_col(df, "MOBILE NUMBER", "MOBILE", "MOBILE_NO")
            dob_col = _kf_col(df, "DATE OF BIRTH", "DOB")
            tax_col = _kf_col(df, "TAX STATUS")
            occ_col = _kf_col(df, "OCC CODE", "OCCUPATION CODE")
            occd_col = _kf_col(df, "OCCUPATION DESCRIPTION", "OCC DESC")
            hold_col = _kf_col(df, "MODE OF HOLDING DESCRIPTION", "HOLDING NATURE")
            jnt1_col = _kf_col(df, "JOINT NAME 1", "JOINT1_NAME")
            jnt2_col = _kf_col(df, "JOINT NAME 2", "JOINT2_NAME")
            bkac_col = _kf_col(df, "BANKACCNO", "BANK AC NO", "AC_NO")
            bknm_col = _kf_col(df, "BANK NAME")
            actyp_col = _kf_col(df, "ACCOUNT TYPE", "AC TYPE")
            bkbr_col = _kf_col(df, "BRANCH")
            brok_col = _kf_col(df, "BROKER CODE", "AGENT CODE")
            mapin_col = _kf_col(df, "MAPIN ID")
            rep_col = _kf_col(df, "REPORT DATE", "REP DATE")

            rows.append((
                folio,
                clean_str(row.get(name_col, "")).strip(),
                clean_str(row.get(_kf_col(df, "PAN NUMBER", "PAN_NO", "PAN") or "", "")).upper(),
                clean_str(row.get(fund_col or "", "")),
                clean_str(row.get(prod_col or "", "")),
                clean_str(row.get(sch_col or "", "")),
                clean_str(row.get(dopt_col or "", "")),
                clean_str(row.get(fdesc_col or "", "")),
                clean_str(row.get(email_col or "", "")).lower(),
                clean_str(row.get(mob_col or "", "")),
                clean_str(row.get(_kf_col(df, "ADDRESS #1", "ADDRESS1") or "", "")),
                clean_str(row.get(_kf_col(df, "ADDRESS #2", "ADDRESS2") or "", "")),
                clean_str(row.get(_kf_col(df, "ADDRESS #3", "ADDRESS3") or "", "")),
                clean_str(row.get(_kf_col(df, "CITY") or "", "")),
                clean_str(row.get(_kf_col(df, "PINCODE") or "", "")),
                clean_str(row.get(_kf_col(df, "STATE") or "", "")),
                clean_str(row.get(_kf_col(df, "COUNTRY") or "", "")),
                parse_date_safe(row.get(dob_col or "", "")),
                clean_str(row.get(tax_col or "", "")),
                clean_str(row.get(occ_col or "", "")),
                clean_str(row.get(occd_col or "", "")),
                clean_str(row.get(hold_col or "", "")),
                clean_str(row.get(jnt1_col or "", "")),
                clean_str(row.get(jnt2_col or "", "")),
                clean_str(row.get(bkac_col or "", "")),
                clean_str(row.get(bknm_col or "", "")),
                clean_str(row.get(actyp_col or "", "")),
                clean_str(row.get(bkbr_col or "", "")),
                clean_str(row.get(brok_col or "", "")),
                clean_str(row.get(mapin_col or "", "")),
                parse_date_safe(row.get(rep_col or "", "")),
                batch,
            ))
        except Exception as exc:
            log.warning("Skipped KFin folio row: %s", exc)
            skipped += 1

    if not rows:
        return False, "0 folio rows parsed", {}

    inserted = dupes = 0
    with get_conn() as conn:
        if replace:
            conn.execute("DELETE FROM kfin_folio")
            conn.executemany(
                """INSERT OR IGNORE INTO kfin_folio
                (folio_no,inv_name,pan_no,fund_code,product_code,scheme_code,div_opt,fund_desc,
                 email,mobile_no,address1,address2,address3,city,pincode,state,country,dob,
                 tax_status,occupation_code,occ_desc,holding_nature,joint_name1,joint_name2,
                 bank_accno,bank_name,ac_type,bank_branch,broker_code,mapin_id,rep_date,upload_batch)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                rows)
            inserted = len(rows)
        else:
            before = _count_before_after(conn, "kfin_folio")
            conn.executemany(
                """INSERT OR IGNORE INTO kfin_folio
                (folio_no,inv_name,pan_no,fund_code,product_code,scheme_code,div_opt,fund_desc,
                 email,mobile_no,address1,address2,address3,city,pincode,state,country,dob,
                 tax_status,occupation_code,occ_desc,holding_nature,joint_name1,joint_name2,
                 bank_accno,bank_name,ac_type,bank_branch,broker_code,mapin_id,rep_date,upload_batch)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                rows)
            inserted, dupes = _inserted_dupes(before, conn, "kfin_folio", len(rows))

    preview = {"rows": inserted, "skipped": skipped, "duplicates": dupes,
               "unique_folios": len({r[0] for r in rows}),
               "unique_investors": len({r[1] for r in rows})}
    msg = f"Imported {inserted} KFin folio records"
    if skipped: msg += f" | Skipped {skipped}"
    if dupes:   msg += f" | {dupes} duplicates"
    return True, msg, preview


def parse_kfin_transactions(file, replace: bool) -> tuple[bool, str, dict]:
    """MFSD201 → kfin_transactions (all raw columns)."""
    df = _read_csv_auto(file)
    if df is None:
        return False, "Could not parse file", {}
    df = _clean_cols(df)

    required = ["TD_ACNO", "TD_TRNO", "TD_AMT"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        return False, f"Missing columns: {missing}. Found: {list(df.columns)[:20]}", {}

    batch = _batch_id("KFIN_R2", file.name)
    rows, skipped = [], 0
    for _, row in df.iterrows():
        try:
            rows.append((
                clean_str(row.get("FMCODE", "")),
                clean_str(row.get("TD_FUND", "")),
                clean_str(row.get("TD_ACNO", "")),
                clean_str(row.get("SCHPLN", "")),
                clean_str(row.get("DIVOPT", "")),
                clean_str(row.get("FUNDDESC", "")),
                clean_str(row.get("TD_PURRED", "")),
                clean_str(row.get("TD_TRNO", "")),
                clean_str(row.get("INVNAME", "")).strip(),
                clean_str(row.get("TRNMODE", "")),
                clean_str(row.get("TRNSTAT", "")),
                clean_str(row.get("TD_BRANCH", "")),
                parse_date_safe(row.get("TD_TRDT", "")),
                parse_date_safe(row.get("TD_PRDT", "")),
                _sf(row.get("TD_POP", 0)),
                _sf(row.get("TD_UNITS", 0)),
                _sf(row.get("TD_AMT", 0)),
                _sf(row.get("STT", 0)),
                clean_str(row.get("SIPREGSLNO", "")),
                parse_date_safe(row.get("SIPREGDT", "")),
                clean_str(row.get("CHQBANK", "")),
                clean_str(row.get("TD_AGENT", "")),
                clean_str(row.get("TD_BROKER", "")),
                clean_str(row.get("INWARDNO", "")),
                clean_str(row.get("NCTREMARKS", "")),
                parse_date_safe(row.get("PURDATE", "")),
                batch,
            ))
        except Exception as exc:
            log.warning("Skipped KFin txn row: %s", exc)
            skipped += 1

    if not rows:
        return False, "0 transaction rows parsed", {}

    inserted = dupes = 0
    with get_conn() as conn:
        if replace:
            conn.execute("DELETE FROM kfin_transactions")
            conn.executemany(
                """INSERT OR IGNORE INTO kfin_transactions
                (fund_code,product_code,folio_no,scheme_code,div_opt,fund_desc,trxn_type,trxn_no,
                 inv_name,trxn_mode,trxn_status,branch,trade_date,post_date,nav,units,amount,stt,
                 sip_reg_no,sip_reg_date,chq_bank,agent_code,ih_no,inward_no,remarks,rep_date,upload_batch)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                rows)
            inserted = len(rows)
        else:
            before = _count_before_after(conn, "kfin_transactions")
            conn.executemany(
                """INSERT OR IGNORE INTO kfin_transactions
                (fund_code,product_code,folio_no,scheme_code,div_opt,fund_desc,trxn_type,trxn_no,
                 inv_name,trxn_mode,trxn_status,branch,trade_date,post_date,nav,units,amount,stt,
                 sip_reg_no,sip_reg_date,chq_bank,agent_code,ih_no,inward_no,remarks,rep_date,upload_batch)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                rows)
            inserted, dupes = _inserted_dupes(before, conn, "kfin_transactions", len(rows))

    total_amt = sum(r[16] for r in rows)
    preview = {"rows": inserted, "skipped": skipped, "duplicates": dupes,
               "total_amount": total_amt,
               "folios": len({r[2] for r in rows}),
               "schemes": len({r[3] for r in rows})}
    msg = f"Imported {inserted} KFin transactions | {format_currency(total_amt, 0)}"
    if skipped: msg += f" | Skipped {skipped}"
    if dupes:   msg += f" | {dupes} duplicates"
    return True, msg, preview


def parse_kfin_sip(file, replace: bool) -> tuple[bool, str, dict]:
    """MFSD243 → kfin_sip (all raw columns)."""
    df = _read_csv_auto(file)
    if df is None:
        return False, "Could not parse file", {}
    df = _clean_cols(df)

    folio_col = _kf_col(df, "FOLIO", "FOLIO NUMBER", "FOLIO_NO")
    regsl_col = _kf_col(df, "REGSLNO", "REG_SL_NO", "REGISTRATION SL NO")
    amt_col = _kf_col(df, "AMOUNT", "SIP AMOUNT")
    if not folio_col or not regsl_col:
        return False, f"Missing FOLIO/REGSLNO. Found: {list(df.columns)[:20]}", {}

    batch = _batch_id("KFIN_R49", file.name)
    rows, skipped = [], 0
    for _, row in df.iterrows():
        folio = clean_str(row.get(folio_col, ""))
        regsl = clean_str(row.get(regsl_col, ""))
        if not folio or not regsl:
            skipped += 1
            continue
        try:
            rows.append((
                folio,
                clean_str(row.get(_kf_col(df, "INVESTOR NAME", "INV_NAME") or "", "")).strip(),
                clean_str(row.get(_kf_col(df, "PAN", "PAN NUMBER") or "", "")).upper(),
                clean_str(row.get(_kf_col(df, "FUND CODE", "FUND") or "", "")),
                clean_str(row.get(_kf_col(df, "PRODUCT CODE") or "", "")),
                clean_str(row.get(_kf_col(df, "SCHEME", "SCHEME CODE") or "", "")),
                clean_str(row.get(_kf_col(df, "SCHEME NAME") or "", "")),
                regsl,
                clean_str(row.get(_kf_col(df, "IHNO", "IH NO") or "", "")),
                _sf(row.get(amt_col or "", 0)),
                parse_date_safe(row.get(_kf_col(df, "START DATE", "FROM DATE") or "", "")),
                parse_date_safe(row.get(_kf_col(df, "END DATE", "TO DATE") or "", "")),
                _si(row.get(_kf_col(df, "NO OF INSTALLMENTS", "INSTALLMENTS") or "", 0)),
                clean_str(row.get(_kf_col(df, "FREQUENCY") or "", "")),
                clean_str(row.get(_kf_col(df, "SIPTYPE", "SIP TYPE") or "", "")),
                clean_str(row.get(_kf_col(df, "SIP MODE") or "", "")),
                parse_date_safe(row.get(_kf_col(df, "TERMINATEDATE", "TERMINATE DATE") or "", "")),
                clean_str(row.get(_kf_col(df, "STATUS") or "", "Active")),
                clean_str(row.get(_kf_col(df, "ECSBANKNAME", "ECS BANK NAME") or "", "")),
                clean_str(row.get(_kf_col(df, "ECSAcno", "ECS AC NO") or "", "")),
                clean_str(row.get(_kf_col(df, "AGENTCODE", "AGENT CODE") or "", "")),
                clean_str(row.get(_kf_col(df, "AGENTNAME", "AGENT NAME") or "", "")),
                parse_date_safe(row.get(_kf_col(df, "REGISTRATIONDATE", "REG DATE") or "", "")),
                clean_str(row.get(_kf_col(df, "ZONE") or "", "")),
                clean_str(row.get(_kf_col(df, "BRANCH") or "", "")),
                batch,
            ))
        except Exception as exc:
            log.warning("Skipped KFin SIP row: %s", exc)
            skipped += 1

    if not rows:
        return False, "0 SIP rows parsed", {}

    inserted = dupes = 0
    with get_conn() as conn:
        if replace:
            conn.execute("DELETE FROM kfin_sip")
            conn.executemany(
                """INSERT OR IGNORE INTO kfin_sip
                (folio_no,inv_name,pan,fund_code,product_code,scheme_code,scheme_name,reg_sl_no,
                 ih_no,amount,start_date,end_date,no_of_installments,frequency,sip_type,sip_mode,
                 terminate_date,status,ecs_bank,ecs_ac_no,agent_code,agent_name,reg_date,zone,branch,upload_batch)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                rows)
            inserted = len(rows)
        else:
            before = _count_before_after(conn, "kfin_sip")
            conn.executemany(
                """INSERT OR IGNORE INTO kfin_sip
                (folio_no,inv_name,pan,fund_code,product_code,scheme_code,scheme_name,reg_sl_no,
                 ih_no,amount,start_date,end_date,no_of_installments,frequency,sip_type,sip_mode,
                 terminate_date,status,ecs_bank,ecs_ac_no,agent_code,agent_name,reg_date,zone,branch,upload_batch)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                rows)
            inserted, dupes = _inserted_dupes(before, conn, "kfin_sip", len(rows))

    active = sum(1 for r in rows if "active" in str(r[17]).lower())
    ceased = sum(1 for r in rows if "cease" in str(r[17]).lower())
    completed = sum(1 for r in rows if "complet" in str(r[17]).lower())
    preview = {"rows": inserted, "skipped": skipped, "duplicates": dupes,
               "active": active, "ceased": ceased, "completed": completed}
    msg = f"Imported {inserted} KFin SIPs | Active: {active} | Ceased: {ceased} | Completed: {completed}"
    if skipped: msg += f" | Skipped {skipped}"
    if dupes:   msg += f" | {dupes} duplicates"
    return True, msg, preview


def parse_kfin_aum(file, replace: bool) -> tuple[bool, str, dict]:
    """KFin AUM report → kfin_aum (all raw columns)."""
    df = _read_csv_auto(file)
    if df is None:
        return False, "Could not parse file", {}
    df = _clean_cols(df)

    folio_col = _kf_col(df, "FOLIO NUMBER", "FOLIO", "ACCOUNT NUMBER", "FOLIO_NO")
    aum_col = _kf_col(df, "AUM", "RUPEE_BAL", "CURRENT VALUE", "MARKET VALUE", "VALUE")
    if not folio_col or not aum_col:
        return False, f"Missing Folio/AUM column. Found: {list(df.columns)[:20]}", {}

    batch = _batch_id("KFIN_AUM", file.name)
    rows, skipped = [], 0
    for _, row in df.iterrows():
        folio = clean_str(row.get(folio_col, ""))
        if not folio:
            skipped += 1
            continue
        try:
            nav_col = _kf_col(df, "NAV", "NET ASSET VALUE")
            units_col = _kf_col(df, "BALANCE", "UNITS", "CLOSING BALANCE")
            aum_val = _sf(row.get(aum_col, 0))
            if aum_val == 0 and nav_col and units_col:
                aum_val = _sf(row.get(nav_col, 0)) * _sf(row.get(units_col, 0))
            rows.append((
                folio,
                clean_str(row.get(_kf_col(df, "INVESTOR NAME", "INV_NAME") or "", "")).strip(),
                clean_str(row.get(_kf_col(df, "FUND", "AMC CODE", "FUND CODE") or "", "")),
                clean_str(row.get(_kf_col(df, "PRODUCT CODE") or "", "")),
                clean_str(row.get(_kf_col(df, "SCHEME CODE", "SCH CODE") or "", "")),
                clean_str(row.get(_kf_col(df, "DIVIDEND OPTION", "DIV OPT", "OPTION") or "", "")),
                clean_str(row.get(_kf_col(df, "FUND DESCRIPTION", "SCHEME NAME", "DESCRIPTION") or "", "")),
                clean_str(row.get(_kf_col(df, "EMAIL", "E-MAIL") or "", "")).lower(),
                _sf(row.get(units_col or "", 0)),
                aum_val,
                _sf(row.get(nav_col or "", 0)),
                parse_date_safe(row.get(_kf_col(df, "REPORT DATE", "AS ON DATE", "DATE") or "", "")),
                batch,
            ))
        except Exception as exc:
            log.warning("Skipped KFin AUM row: %s", exc)
            skipped += 1

    if not rows:
        return False, "0 AUM rows parsed", {}

    inserted = dupes = 0
    with get_conn() as conn:
        if replace:
            conn.execute("DELETE FROM kfin_aum")
            conn.executemany(
                """INSERT OR IGNORE INTO kfin_aum
                (folio_no,inv_name,fund_code,product_code,scheme_code,div_opt,fund_desc,email,
                 balance,aum,nav,rep_date,upload_batch)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                rows)
            inserted = len(rows)
        else:
            before = _count_before_after(conn, "kfin_aum")
            conn.executemany(
                """INSERT OR IGNORE INTO kfin_aum
                (folio_no,inv_name,fund_code,product_code,scheme_code,div_opt,fund_desc,email,
                 balance,aum,nav,rep_date,upload_batch)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                rows)
            inserted, dupes = _inserted_dupes(before, conn, "kfin_aum", len(rows))

    total_aum = sum(r[9] for r in rows)
    preview = {"rows": inserted, "skipped": skipped, "duplicates": dupes, "total_aum": total_aum}
    msg = f"Imported {inserted} KFin AUM rows | {format_aum(total_aum)}"
    if skipped: msg += f" | Skipped {skipped}"
    if dupes:   msg += f" | {dupes} duplicates"
    return True, msg, preview


def parse_kfin_brokerage(file, replace: bool) -> tuple[bool, str, dict]:
    """MFSD205 → kfin_brokerage (ALL 48 raw columns)."""
    df = _read_csv_auto(file)
    if df is None:
        return False, "Could not parse file", {}
    df = _clean_cols(df)

    acno_col = _kf_col(df, "ACCOUNT NUMBER", "FOLIO NUMBER", "FOLIO_NO")
    brok_col = _kf_col(df, "BROKERAGE (IN RS.)", "BROKERAGE", "GROSS BROKERAGE")
    fund_col = _kf_col(df, "FUND", "AMC CODE", "FUND CODE")
    if not acno_col or not brok_col:
        return False, f"Missing Account Number/Brokerage. Found: {list(df.columns)[:20]}", {}

    batch = _batch_id("KFIN_MFSD205", file.name)
    rows, skipped = [], 0
    for _, row in df.iterrows():
        try:
            rows.append((
                clean_str(row.get(_kf_col(df, "PRODUCT CODE") or "", "")),
                clean_str(row.get(_kf_col(df, "FUND DESCRIPTION") or "", "")),
                clean_str(row.get(fund_col or "", "")),
                clean_str(row.get(_kf_col(df, "SCHEME") or "", "")),
                clean_str(row.get(_kf_col(df, "PLAN") or "", "")),
                clean_str(row.get(_kf_col(df, "OPTION") or "", "")),
                clean_str(row.get(acno_col or "", "")),
                clean_str(row.get(_kf_col(df, "APPLICATION NUMBER") or "", "")),
                clean_str(row.get(_kf_col(df, "INVESTOR NAME", "INV_NAME") or "", "")).strip(),
                clean_str(row.get(_kf_col(df, "ADDRESS #1", "ADDRESS1") or "", "")),
                clean_str(row.get(_kf_col(df, "ADDRESS #2", "ADDRESS2") or "", "")),
                clean_str(row.get(_kf_col(df, "ADDRESS #3", "ADDRESS3") or "", "")),
                clean_str(row.get(_kf_col(df, "CITY") or "", "")),
                clean_str(row.get(_kf_col(df, "PINCODE") or "", "")),
                clean_str(row.get(_kf_col(df, "TRANSACTION DESCRIPTION") or "", "")),
                parse_date_safe(row.get(_kf_col(df, "FROM DATE", "STARTING DATE") or "", "")),
                parse_date_safe(row.get(_kf_col(df, "TO DATE", "ENDING DATE") or "", "")),
                _sf(row.get(_kf_col(df, "AMOUNT (IN RS.)", "AMOUNT") or "", 0)),
                _sf(row.get(_kf_col(df, "UNITS") or "", 0)),
                parse_date_safe(row.get(_kf_col(df, "TRANSACTION DATE") or "", "")),
                parse_date_safe(row.get(_kf_col(df, "PROCESS DATE") or "", "")),
                _sf(row.get(_kf_col(df, "PERCENTAGE (%)", "PERCENTAGE") or "", 0)),
                _sf(row.get(brok_col or "", 0)),
                clean_str(row.get(_kf_col(df, "SUB-BROKER", "SUB BROKER") or "", "")),
                clean_str(row.get(_kf_col(df, "ACCOUNT TYPE") or "", "")),
                clean_str(row.get(_kf_col(df, "BROKERAGE HEAD") or "", "")),
                clean_str(row.get(_kf_col(df, "BROKERAGE TYPE") or "", "")),
                clean_str(row.get(_kf_col(df, "TRANSACTION NUMBER") or "", "")),
                clean_str(row.get(_kf_col(df, "BRANCH CODE") or "", "")),
                clean_str(row.get(_kf_col(df, "CHEQUE NUMBER") or "", "")),
                parse_date_safe(row.get(_kf_col(df, "STARTING DATE") or "", "")),
                parse_date_safe(row.get(_kf_col(df, "ENDING DATE") or "", "")),
                clean_str(row.get(_kf_col(df, "WARRANT NUMBER") or "", "")),
                parse_date_safe(row.get(_kf_col(df, "WARRANT DATE") or "", "")),
                _sf(row.get(_kf_col(df, "DAILY PRODUCT") or "", 0)),
                _sf(row.get(_kf_col(df, "CUMULATIVE NAV") or "", 0)),
                _sf(row.get(_kf_col(df, "AVERAGE ASSETS") or "", 0)),
                clean_str(row.get(_kf_col(df, "TRANSACTION ID") or "", "")),
                clean_str(row.get(_kf_col(df, "SCHEME CODE") or "", "")),
                clean_str(row.get(_kf_col(df, "TRANSACTION HEAD") or "", "")),
                clean_str(row.get(_kf_col(df, "FEE TYPE") or "", "")),
                clean_str(row.get(_kf_col(df, "ADJUSTMENT FLAG") or "", "")),
                clean_str(row.get(_kf_col(df, "SWITCH FLAG") or "", "")),
                _sf(row.get(_kf_col(df, "GROSSBROKERAGE", "GROSS BROKERAGE") or "", 0)),
                _sf(row.get(_kf_col(df, "STTAMOUNT", "STT AMOUNT") or "", 0)),
                _sf(row.get(_kf_col(df, "EDUCESSAMOUNT", "EDUCESS AMOUNT") or "", 0)),
                clean_str(row.get(_kf_col(df, "TRANTYPECODE", "TRAN TYPE CODE") or "", "")),
                batch,
            ))
        except Exception as exc:
            log.warning("Skipped MFSD205 row: %s", exc)
            skipped += 1

    if not rows:
        return False, "0 rows parsed", {}

    inserted = dupes = 0
    sql = """INSERT OR IGNORE INTO kfin_brokerage
        (product_code,fund_description,fund,scheme,plan,option,account_number,application_number,
         investor_name,address_1,address_2,address_3,city,pincode,transaction_description,
         from_date,to_date,amount,units,transaction_date,process_date,percentage,brokerage,
         sub_broker,account_type,brokerage_head,brokerage_type,transaction_number,branch_code,
         cheque_number,starting_date,ending_date,warrant_number,warrant_date,daily_product,
         cumulative_nav,average_assets,transaction_id,scheme_code,transaction_head,fee_type,
         adjustment_flag,switch_flag,gross_brokerage,stt_amount,educess_amount,tran_type_code,upload_batch)
        VALUES (""" + ",".join(["?"] * 48) + ")"

    with get_conn() as conn:
        if replace:
            conn.execute("DELETE FROM kfin_brokerage")
            conn.executemany(sql, rows)
            inserted = len(rows)
        else:
            before = _count_before_after(conn, "kfin_brokerage")
            conn.executemany(sql, rows)
            inserted, dupes = _inserted_dupes(before, conn, "kfin_brokerage", len(rows))

    total = sum(r[22] for r in rows)
    months = sorted({str(r[15])[:7] for r in rows if r[15]})
    preview = {"rows": inserted, "skipped": skipped, "duplicates": dupes,
               "total_brokerage": total, "months": months}
    msg = f"Imported {inserted} MFSD205 rows | {format_brokerage(total)} brokerage"
    if skipped: msg += f" | Skipped {skipped}"
    if dupes:   msg += f" | {dupes} duplicates"
    return True, msg, preview


# ══════════════════════════════════════════════════════════════════════════════
# STREAMLIT UI
# ══════════════════════════════════════════════════════════════════════════════

def _metrics(preview: dict, keys: list[tuple[str, str]]):
    cols = st.columns(len(keys))
    for col, (k, label) in zip(cols, keys):
        col.metric(label, preview.get(k, 0))


def render_data_manager():
    tab_bse, tab_cams, tab_kfin = st.tabs(["📥 BSE Data", "🟢 CAMS Data", "🔵 KFinTech Data"])

    # ─── BSE ─────────────────────────────────────────────────────────────────
    with tab_bse:
        bse_sec = st.radio("Section", ["👤 Client Master", "📋 XSIP Report", "📋 Scheme Master"],
                           horizontal=True, key="dm_bse_sec")
        st.divider()

        if bse_sec == "👤 Client Master":
            st.subheader("BSE Client Master")
            st.caption("Excel file · Maps: clients + clients_bank + clients_nominee")
            f = st.file_uploader("Client Master (.xlsx)", type=["xlsx"], key="dm_bse_cm")
            rp = st.checkbox("Replace all clients", key="dm_bse_cm_rp")
            if rp:
                st.warning("All clients + banks + nominees will be deleted first.", icon="⚠️")
            else:
                st.info("Append mode: existing client_codes skipped.", icon="ℹ️")
            if st.button("📤 Import Client Master", key="dm_bse_cm_btn") and f:
                with st.spinner("Importing…"):
                    ok, msg = parse_bse_client_master(f, rp)
                    (st.success if ok else st.error)(msg)
                    if ok: st.cache_data.clear()

            st.divider()
            st.markdown("#### Existing Clients")
            with get_conn() as conn:
                df_c = pd.read_sql(
                    "SELECT client_code, first_name||' '||last_name AS name, pan, email, mobile, city FROM clients ORDER BY client_code",
                    conn)
            if df_c.empty:
                st.info("No client data yet.")
            else:
                st.dataframe(df_c, use_container_width=True, hide_index=True)
                c1, c2 = st.columns(2)
                c1.metric("Total Clients", len(df_c))
            if st.button("⚠️ Clear All Clients", key="dm_bse_cm_del"):
                with get_conn() as conn: conn.execute("DELETE FROM clients")
                st.warning("All clients deleted.")
                st.cache_data.clear()


        elif bse_sec == "📋 XSIP Report":
            st.subheader("BSE XSIP Report")
            st.caption("Excel file · Maps: bse_xsip table")
            f = st.file_uploader("XSIP Report (.xlsx)", type=["xlsx"], key="dm_bse_xsip")
            rp = st.checkbox("Replace all XSIP records", key="dm_bse_xsip_rp")
            if rp:
                st.warning("All XSIP records will be deleted first.", icon="⚠️")
            else:
                st.info("Append mode: duplicate xsip_regn_no skipped.", icon="ℹ️")
            if st.button("📤 Import XSIP", key="dm_bse_xsip_btn") and f:
                with st.spinner("Importing…"):
                    ok, msg, preview = parse_bse_xsip(f, rp)
                    (st.success if ok else st.error)(msg)
                    if ok and preview:
                        _metrics(preview, [("rows", "Inserted"), ("active", "Active"), ("duplicates", "Duplicates"),
                                           ("skipped", "Skipped")])
                    if ok: st.cache_data.clear()

            st.divider()
            st.markdown("#### Existing XSIP")
            with get_conn() as conn:
                df_x = pd.read_sql(
                    "SELECT xsip_regn_no, client_code, amc_name, scheme_name, installments_amt, start_date, status FROM bse_xsip ORDER BY start_date DESC",
                    conn)
            if df_x.empty:
                st.info("No XSIP data yet.")
            else:
                st.dataframe(df_x, use_container_width=True, hide_index=True)
            if st.button("⚠️ Clear All XSIP", key="dm_bse_xsip_del"):
                with get_conn() as conn: conn.execute("DELETE FROM bse_xsip")
                st.warning("All XSIP records deleted.")
                st.cache_data.clear()

        else:  # Scheme Master
            st.subheader("BSE Scheme Master")
            st.caption("Excel/CSV · All 42 columns stored · Required: scheme_code, rta_scheme_code, isin")
            f = st.file_uploader("Scheme Master (.xlsx / .csv / .txt)", type=["xlsx", "csv", "txt"],
                                 key="dm_bse_scheme")
            rp = st.checkbox("Replace all Scheme Master records", key="dm_bse_scheme_rp")
            if rp:
                st.warning("All scheme master records will be deleted first.", icon="⚠️")
            else:
                st.info("Append mode: duplicate (scheme_code, rta_scheme_code) pairs skipped.", icon="ℹ️")
            if st.button("📤 Import Scheme Master", key="dm_bse_scheme_btn") and f:
                with st.spinner("Importing…"):
                    ok, msg, preview = parse_bse_scheme_master(f, rp)
                    (st.success if ok else st.error)(msg)
                    if ok and preview:
                        _metrics(preview, [("rows", "Inserted"), ("with_isin", "With ISIN"),
                                           ("sip_eligible", "SIP-eligible"), ("duplicates", "Duplicates")])
                    if ok: st.cache_data.clear()

            st.divider()
            st.markdown("#### Existing Scheme Master")
            with get_conn() as conn:
                df_s = pd.read_sql(
                    """SELECT scheme_code, rta_scheme_code, isin, scheme_name, rta_agent_code,
                              settlement_type, sip_flag, channel_partner_code
                       FROM bse_scheme_master
                       ORDER BY scheme_code LIMIT 200""", conn)
            if df_s.empty:
                st.info("No scheme master data yet.")
            else:
                st.dataframe(df_s, use_container_width=True, hide_index=True)
            if st.button("⚠️ Clear All Scheme Master", key="dm_bse_scheme_del"):
                with get_conn() as conn: conn.execute("DELETE FROM bse_scheme_master")
                st.warning("All scheme master records deleted.")
                st.cache_data.clear()

    # ─── CAMS ─────────────────────────────────────────────────────────────────
    with tab_cams:
        cams_sec = st.radio("Section",
                            ["📦 AUM", "💼 Brokerage (WBR77)", "📂 Folio (WBR9)", "💳 Transactions (WBR2)",
                             "🔄 SIP (WBR49)"],
                            horizontal=True, key="dm_cams_sec")
        st.divider()

        if cams_sec == "📦 AUM":
            st.subheader("CAMS AUM Report")
            st.caption("Required: FOLIO_NO / FOLIOCHK, RUPEE_BAL")
            f = st.file_uploader("CAMS AUM (CSV/TSV)", type=["csv", "txt", "tsv"], key="dm_cams_aum")
            rp = st.checkbox("Replace CAMS AUM", key="dm_cams_aum_rp")
            if rp: st.warning("Replace mode.", icon="⚠️")
            if st.button("📤 Upload AUM", key="dm_cams_aum_btn") and f:
                with st.spinner("Parsing…"):
                    ok, msg, preview = parse_cams_aum(f, rp)
                    (st.success if ok else st.error)(msg)
                    if ok and preview:
                        _metrics(preview,
                                 [("rows", "Inserted"), ("total_aum", "Total AUM"), ("duplicates", "Duplicates"),
                                  ("skipped", "Skipped")])
                    if ok: st.cache_data.clear()
            st.divider()
            with get_conn() as conn:
                s = pd.read_sql(
                    "SELECT rep_date,amc_code,COUNT(*) folios,ROUND(SUM(rupee_bal),2) aum FROM cams_aum GROUP BY rep_date,amc_code ORDER BY rep_date DESC",
                    conn)
            if s.empty:
                st.info("No CAMS AUM data.")
            else:
                s["aum"] = s["aum"].apply(format_aum)
                st.dataframe(s, use_container_width=True, hide_index=True)
            if st.button("⚠️ Clear CAMS AUM", key="dm_cams_aum_del"):
                with get_conn() as conn: conn.execute("DELETE FROM cams_aum")
                st.warning("Cleared.");
                st.cache_data.clear()

        elif cams_sec == "💼 Brokerage (WBR77)":
            st.subheader("CAMS Brokerage — WBR77")
            st.caption("Required: AMC_CODE, FOLIO_NO, BRKAGE_AMT · All 107 columns stored")
            f = st.file_uploader("WBR77 (CSV/TSV)", type=["csv", "txt", "tsv"], key="dm_cams_brok")
            rp = st.checkbox("Replace CAMS Brokerage", key="dm_cams_brok_rp")
            if rp:
                st.warning("Replace mode.", icon="⚠️")
            else:
                st.info("Dedup: trxn_no + folio_no + brokerage_acrual_month", icon="ℹ️")
            if st.button("📤 Upload WBR77", key="dm_cams_brok_btn") and f:
                with st.spinner("Parsing…"):
                    ok, msg, preview = parse_cams_brokerage(f, rp)
                    (st.success if ok else st.error)(msg)
                    if ok and preview:
                        _metrics(preview, [("rows", "Inserted"), ("total_brokerage", "Total Brokerage"),
                                           ("duplicates", "Duplicates"), ("skipped", "Skipped")])
                        if preview.get("months"): st.info(f"Months: **{', '.join(preview['months'])}**")
                    if ok: st.cache_data.clear()
            st.divider()
            with get_conn() as conn:
                s = pd.read_sql(
                    "SELECT brokerage_acrual_month month,COUNT(*) rows,COUNT(DISTINCT folio_no) folios,ROUND(SUM(brkage_amt),4) brokerage FROM cams_brokerage GROUP BY month ORDER BY month DESC",
                    conn)
            if s.empty:
                st.info("No CAMS brokerage data.")
            else:
                st.dataframe(s, use_container_width=True, hide_index=True)
            if st.button("⚠️ Clear CAMS Brokerage", key="dm_cams_brok_del"):
                with get_conn() as conn: conn.execute("DELETE FROM cams_brokerage")
                st.warning("Cleared.");
                st.cache_data.clear()

        elif cams_sec == "📂 Folio (WBR9)":
            st.subheader("CAMS Folio Master — WBR9")
            st.caption("Required: FOLIO_NO, INV_NAME · All columns stored")
            f = st.file_uploader("WBR9 (CSV/TSV)", type=["csv", "txt", "tsv"], key="dm_cams_folio")
            rp = st.checkbox("Replace CAMS Folio", key="dm_cams_folio_rp")
            if rp: st.warning("Replace mode.", icon="⚠️")
            if st.button("📤 Upload WBR9", key="dm_cams_folio_btn") and f:
                with st.spinner("Parsing…"):
                    ok, msg, preview = parse_cams_folio(f, rp)
                    (st.success if ok else st.error)(msg)
                    if ok and preview:
                        _metrics(preview, [("rows", "Inserted"), ("total_aum", "AUM"), ("unique_folios", "Folios"),
                                           ("unique_investors", "Investors")])
                    if ok: st.cache_data.clear()
            st.divider()
            with get_conn() as conn:
                s = pd.read_sql(
                    "SELECT amc_code,COUNT(DISTINCT folio_no) folios,COUNT(DISTINCT pan_no) investors,ROUND(SUM(rupee_bal),2) aum FROM cams_folio GROUP BY amc_code ORDER BY aum DESC",
                    conn)
            if s.empty:
                st.info("No CAMS folio data.")
            else:
                s["aum"] = s["aum"].apply(format_aum)
                st.dataframe(s, use_container_width=True, hide_index=True)
            if st.button("⚠️ Clear CAMS Folio", key="dm_cams_folio_del"):
                with get_conn() as conn: conn.execute("DELETE FROM cams_folio")
                st.warning("Cleared.");
                st.cache_data.clear()

        elif cams_sec == "💳 Transactions (WBR2)":
            st.subheader("CAMS Transactions — WBR2")
            st.caption("Required: FOLIO_NO, TRXNNO, AMOUNT")
            f = st.file_uploader("WBR2 (CSV/TSV)", type=["csv", "txt", "tsv"], key="dm_cams_txn")
            rp = st.checkbox("Replace CAMS Transactions", key="dm_cams_txn_rp")
            if rp: st.warning("Replace mode.", icon="⚠️")
            if st.button("📤 Upload WBR2", key="dm_cams_txn_btn") and f:
                with st.spinner("Parsing…"):
                    ok, msg, preview = parse_cams_transactions(f, rp)
                    (st.success if ok else st.error)(msg)
                    if ok and preview:
                        _metrics(preview, [("rows", "Inserted"), ("total_amount", "Total Amount"), ("folios", "Folios"),
                                           ("schemes", "Schemes")])
                    if ok: st.cache_data.clear()
            st.divider()
            with get_conn() as conn:
                s = pd.read_sql(
                    "SELECT trade_date,amc_code,COUNT(*) txns,COUNT(DISTINCT folio_no) folios,ROUND(SUM(amount),2) amount FROM cams_transactions GROUP BY trade_date,amc_code ORDER BY trade_date DESC LIMIT 30",
                    conn)
            if s.empty:
                st.info("No CAMS transaction data.")
            else:
                st.dataframe(s, use_container_width=True, hide_index=True)
            if st.button("⚠️ Clear CAMS Transactions", key="dm_cams_txn_del"):
                with get_conn() as conn: conn.execute("DELETE FROM cams_transactions")
                st.warning("Cleared.");
                st.cache_data.clear()

        else:  # SIP WBR49
            st.subheader("CAMS SIP Master — WBR49")
            st.caption("Required: FOLIO_NO, AUTO_TRNO, AUTO_AMOUNT")
            f = st.file_uploader("WBR49 (CSV/TSV)", type=["csv", "txt", "tsv"], key="dm_cams_sip")
            rp = st.checkbox("Replace CAMS SIP", key="dm_cams_sip_rp")
            if rp: st.warning("Replace mode.", icon="⚠️")
            if st.button("📤 Upload WBR49", key="dm_cams_sip_btn") and f:
                with st.spinner("Parsing…"):
                    ok, msg, preview = parse_cams_sip(f, rp)
                    (st.success if ok else st.error)(msg)
                    if ok and preview:
                        _metrics(preview, [("rows", "Inserted"), ("active", "Active"), ("ceased", "Ceased"),
                                           ("completed", "Completed")])
                    if ok: st.cache_data.clear()
            st.divider()
            with get_conn() as conn:
                s = pd.read_sql(
                    "SELECT amc_code,status,COUNT(*) sips,COUNT(DISTINCT folio_no) folios,ROUND(SUM(auto_amount),2) amount FROM cams_sip GROUP BY amc_code,status ORDER BY amc_code",
                    conn)
            if s.empty:
                st.info("No CAMS SIP data.")
            else:
                st.dataframe(s, use_container_width=True, hide_index=True)
            if st.button("⚠️ Clear CAMS SIP", key="dm_cams_sip_del"):
                with get_conn() as conn: conn.execute("DELETE FROM cams_sip")
                st.warning("Cleared.");
                st.cache_data.clear()

    # ─── KFINTECH ─────────────────────────────────────────────────────────────
    with tab_kfin:
        kf_sec = st.radio("Section",
                          ["📦 AUM", "💼 Brokerage (MFSD205)", "📂 Folio (MFSD211)", "💳 Transactions (MFSD201)",
                           "🔄 SIP (MFSD243)"],
                          horizontal=True, key="dm_kf_sec")
        st.divider()

        if kf_sec == "📦 AUM":
            st.subheader("KFin AUM Report")
            st.caption("Required: Folio Number, AUM/Value column")
            f = st.file_uploader("KFin AUM (CSV/TSV)", type=["csv", "txt", "tsv"], key="dm_kf_aum")
            rp = st.checkbox("Replace KFin AUM", key="dm_kf_aum_rp")
            if rp: st.warning("Replace mode.", icon="⚠️")
            if st.button("📤 Upload KFin AUM", key="dm_kf_aum_btn") and f:
                with st.spinner("Parsing…"):
                    ok, msg, preview = parse_kfin_aum(f, rp)
                    (st.success if ok else st.error)(msg)
                    if ok and preview:
                        _metrics(preview,
                                 [("rows", "Inserted"), ("total_aum", "Total AUM"), ("duplicates", "Duplicates"),
                                  ("skipped", "Skipped")])
                    if ok: st.cache_data.clear()
            st.divider()
            with get_conn() as conn:
                s = pd.read_sql(
                    "SELECT rep_date,fund_code,COUNT(*) folios,ROUND(SUM(aum),2) aum FROM kfin_aum GROUP BY rep_date,fund_code ORDER BY rep_date DESC",
                    conn)
            if s.empty:
                st.info("No KFin AUM data.")
            else:
                s["aum"] = s["aum"].apply(format_aum)
                st.dataframe(s, use_container_width=True, hide_index=True)
            if st.button("⚠️ Clear KFin AUM", key="dm_kf_aum_del"):
                with get_conn() as conn: conn.execute("DELETE FROM kfin_aum")
                st.warning("Cleared.");
                st.cache_data.clear()

        elif kf_sec == "💼 Brokerage (MFSD205)":
            st.subheader("KFin Brokerage — MFSD205")
            st.caption("Required: Account Number, Brokerage (in Rs.), Fund · All 48 columns stored")
            f = st.file_uploader("MFSD205 (CSV/TSV)", type=["csv", "txt", "tsv"], key="dm_kf_brok")
            rp = st.checkbox("Replace KFin Brokerage", key="dm_kf_brok_rp")
            if rp:
                st.warning("Replace mode.", icon="⚠️")
            else:
                st.info("Dedup: transaction_id", icon="ℹ️")
            if st.button("📤 Upload MFSD205", key="dm_kf_brok_btn") and f:
                with st.spinner("Parsing…"):
                    ok, msg, preview = parse_kfin_brokerage(f, rp)
                    (st.success if ok else st.error)(msg)
                    if ok and preview:
                        _metrics(preview, [("rows", "Inserted"), ("total_brokerage", "Total Brokerage"),
                                           ("duplicates", "Duplicates"), ("skipped", "Skipped")])
                        if preview.get("months"): st.info(f"Months: **{', '.join(preview['months'])}**")
                    if ok: st.cache_data.clear()
            st.divider()
            with get_conn() as conn:
                s = pd.read_sql(
                    "SELECT from_date month,COUNT(*) rows,COUNT(DISTINCT account_number) folios,ROUND(SUM(brokerage),4) brokerage FROM kfin_brokerage GROUP BY month ORDER BY month DESC",
                    conn)
            if s.empty:
                st.info("No KFin brokerage data.")
            else:
                st.dataframe(s, use_container_width=True, hide_index=True)
            if st.button("⚠️ Clear KFin Brokerage", key="dm_kf_brok_del"):
                with get_conn() as conn: conn.execute("DELETE FROM kfin_brokerage")
                st.warning("Cleared.");
                st.cache_data.clear()

        elif kf_sec == "📂 Folio (MFSD211)":
            st.subheader("KFin Folio Master — MFSD211")
            st.caption("Required: Folio, Investor Name")
            f = st.file_uploader("MFSD211 (CSV/TSV)", type=["csv", "txt", "tsv"], key="dm_kf_folio")
            rp = st.checkbox("Replace KFin Folio", key="dm_kf_folio_rp")
            if rp: st.warning("Replace mode.", icon="⚠️")
            if st.button("📤 Upload MFSD211", key="dm_kf_folio_btn") and f:
                with st.spinner("Parsing…"):
                    ok, msg, preview = parse_kfin_folio(f, rp)
                    (st.success if ok else st.error)(msg)
                    if ok and preview:
                        _metrics(preview,
                                 [("rows", "Inserted"), ("unique_folios", "Folios"), ("unique_investors", "Investors"),
                                  ("skipped", "Skipped")])
                    if ok: st.cache_data.clear()
            st.divider()
            with get_conn() as conn:
                s = pd.read_sql(
                    "SELECT fund_code,COUNT(DISTINCT folio_no) folios,COUNT(DISTINCT pan_no) investors FROM kfin_folio GROUP BY fund_code ORDER BY folios DESC",
                    conn)
            if s.empty:
                st.info("No KFin folio data.")
            else:
                st.dataframe(s, use_container_width=True, hide_index=True)
            if st.button("⚠️ Clear KFin Folio", key="dm_kf_folio_del"):
                with get_conn() as conn: conn.execute("DELETE FROM kfin_folio")
                st.warning("Cleared.");
                st.cache_data.clear()

        elif kf_sec == "💳 Transactions (MFSD201)":
            st.subheader("KFin Transactions — MFSD201")
            st.caption("Required: TD_ACNO, TD_TRNO, TD_AMT")
            f = st.file_uploader("MFSD201 (CSV/TSV)", type=["csv", "txt", "tsv"], key="dm_kf_txn")
            rp = st.checkbox("Replace KFin Transactions", key="dm_kf_txn_rp")
            if rp: st.warning("Replace mode.", icon="⚠️")
            if st.button("📤 Upload MFSD201", key="dm_kf_txn_btn") and f:
                with st.spinner("Parsing…"):
                    ok, msg, preview = parse_kfin_transactions(f, rp)
                    (st.success if ok else st.error)(msg)
                    if ok and preview:
                        _metrics(preview, [("rows", "Inserted"), ("total_amount", "Total Amount"), ("folios", "Folios"),
                                           ("schemes", "Schemes")])
                    if ok: st.cache_data.clear()
            st.divider()
            with get_conn() as conn:
                s = pd.read_sql(
                    "SELECT trade_date,fund_code,COUNT(*) txns,COUNT(DISTINCT folio_no) folios,ROUND(SUM(amount),2) amount FROM kfin_transactions GROUP BY trade_date,fund_code ORDER BY trade_date DESC LIMIT 30",
                    conn)
            if s.empty:
                st.info("No KFin transaction data.")
            else:
                st.dataframe(s, use_container_width=True, hide_index=True)
            if st.button("⚠️ Clear KFin Transactions", key="dm_kf_txn_del"):
                with get_conn() as conn: conn.execute("DELETE FROM kfin_transactions")
                st.warning("Cleared.");
                st.cache_data.clear()

        else:  # MFSD243
            st.subheader("KFin SIP Master — MFSD243")
            st.caption("Required: Folio, RegSlno, Amount")
            f = st.file_uploader("MFSD243 (CSV/TSV)", type=["csv", "txt", "tsv"], key="dm_kf_sip")
            rp = st.checkbox("Replace KFin SIP", key="dm_kf_sip_rp")
            if rp: st.warning("Replace mode.", icon="⚠️")
            if st.button("📤 Upload MFSD243", key="dm_kf_sip_btn") and f:
                with st.spinner("Parsing…"):
                    ok, msg, preview = parse_kfin_sip(f, rp)
                    (st.success if ok else st.error)(msg)
                    if ok and preview:
                        _metrics(preview, [("rows", "Inserted"), ("active", "Active"), ("ceased", "Ceased"),
                                           ("completed", "Completed")])
                    if ok: st.cache_data.clear()
            st.divider()
            with get_conn() as conn:
                s = pd.read_sql(
                    "SELECT fund_code,status,COUNT(*) sips,COUNT(DISTINCT folio_no) folios,ROUND(SUM(amount),2) amount FROM kfin_sip GROUP BY fund_code,status ORDER BY fund_code",
                    conn)
            if s.empty:
                st.info("No KFin SIP data.")
            else:
                st.dataframe(s, use_container_width=True, hide_index=True)
            if st.button("⚠️ Clear KFin SIP", key="dm_kf_sip_del"):
                with get_conn() as conn: conn.execute("DELETE FROM kfin_sip")
                st.warning("Cleared.");
                st.cache_data.clear()