"""
Old - data_manager.py
All Upload / Parse / Delete logic for BSE, CAMS, and KFinTech data.
Every parser inserts RAW file data into the exact tables defined in init.py's init_db().
Call render_data_manager() inside the Admin Panel tab.

NOTE: Column names in every INSERT below match init.py's CREATE TABLE statements
column-for-column. init.py's tables mirror the *source file* headers exactly
(snake_case only), so a few names look unusual (e.g. `foliochk`, `trxntype`,
`brokerage_acrual_month` with the original typo). Do not "fix" these names
without updating init.py first.
"""

import logging
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime

import pandas as pd
import streamlit as st

log = logging.getLogger(__name__)
DB_PATH = os.path.join(os.path.dirname(__file__), "mfd_local.db")


def set_db_path(path: str):
    """Override the shared DB_PATH at runtime."""
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

def _cflex(columns, *base_names: str) -> str | None:
    """
    Flexible header matcher for BSE exports whose numbering style varies
    (nominee1_name vs nominee_1_name vs nominee_name_1, bank1_account_no vs
    account_no_1, etc). `columns` is any iterable of already-lowercased,
    underscore-joined column names (df.columns or row.index).
    """
    cols = set(columns)
    variants = set()
    for base in base_names:
        variants.add(base)
        m = re.match(r"^([a-z]+)(\d)_(.+)$", base)
        if m:
            word, num, rest = m.groups()
            variants.add(f"{word}_{num}_{rest}")
            variants.add(f"{word}{num}{rest}")
            variants.add(f"{word}_{rest}_{num}")
            variants.add(f"{rest}_{num}")
            variants.add(f"{word}_{num}{rest}")
        m2 = re.match(r"^(bank)(\d)_(.+)$", base)
        if m2:
            _, num, rest = m2.groups()
            variants.add(f"bank_{rest}_{num}")
            variants.add(f"{rest}_{num}")
            variants.add(f"bank{num}_{rest}")
    for v in variants:
        if v in cols:
            return v
    return None


# ══════════════════════════════════════════════════════════════════════════════
# BSE PARSERS
# ══════════════════════════════════════════════════════════════════════════════

def parse_bse_client_master(file, replace: bool) -> tuple[bool, str]:
    """
    BSE Client Master Excel → clients table.
    ALL 209 columns populated in exact init.py schema order.
    Unmapped fields default to empty string.
    """
    file.seek(0)
    try:
        df = pd.read_excel(file, dtype=str)
    except Exception as exc:
        return False, f"File read error: {exc}"

    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]

    def _c(*candidates):
        return next((c for c in candidates if c in df.columns), None)

    # ── Column resolution ──────────────────────────────────────────────
    code_col = _c("client_code", "ucc")
    member_col = _c("member_code", "member_id")
    fname_col = _c("primary_holder_first_name", "first_name")
    mname_col = _c("primary_holder_middle_name", "middle_name")
    lname_col = _c("primary_holder_last_name", "last_name")
    tax_col = _c("tax_status")
    gender_col = _c("gender")
    dob_col = _c("primary_holder_dob_incorp", "dob", "date_of_birth")
    occ_col = _c("occupation_code", "occupation")
    hold_col = _c("holding_nature", "mode_of_holding")

    s2_fname_col = _c("second_holder_first_name")
    s2_mname_col = _c("second_holder_middle_name")
    s2_lname_col = _c("second_holder_last_name")
    s2_dob_col = _c("second_holder_dob")

    s3_fname_col = _c("third_holder_first_name")
    s3_mname_col = _c("third_holder_middle_name")
    s3_lname_col = _c("third_holder_last_name")
    s3_dob_col = _c("third_holder_dob")

    g_fname_col = _c("guardian_first_name")
    g_mname_col = _c("guardian_middle_name")
    g_lname_col = _c("guardian_last_name")
    g_dob_col = _c("guardian_dob")

    pan_exempt_col = _c("primary_holder_pan_exempt")
    s2_pan_exempt_col = _c("second_holder_pan_exempt")
    s3_pan_exempt_col = _c("third_holder_pan_exempt")
    g_pan_exempt_col = _c("guardian_pan_exempt")

    pan_col = _c("primary_holder_pan", "pan")
    s2_pan_col = _c("second_holder_pan")
    s3_pan_col = _c("third_holder_pan")
    g_pan_col = _c("guardian_pan")

    exempt_cat_col = _c("primary_holder_exempt_category")
    s2_exempt_cat_col = _c("second_holder_exempt_category")
    s3_exempt_cat_col = _c("third_holder_exempt_category")
    g_exempt_cat_col = _c("guardian_exempt_category")

    client_type_col = _c("client_type")
    pms_col = _c("pms")
    default_dp_col = _c("default_dp")
    cdsl_dpid_col = _c("cdsl_dpid")
    cdsl_cltid_col = _c("cdsl_cltid")
    cmbp_id_col = _c("cmbp_id")
    nsdl_dpid_col = _c("nsdl_dpid")
    nsdl_cltid_col = _c("nsdl_cltid")

    kyc_col = _c("primary_holder_kyc_type", "kyc_type")
    ckyc_col = _c("primary_holder_ckyc_number", "ckyc_number")
    s2_kyc_col = _c("second_holder_kyc_type")
    s2_ckyc_col = _c("second_holder_ckyc_number")
    s3_kyc_col = _c("third_holder_kyc_type")
    s3_ckyc_col = _c("third_holder_ckyc_number")
    g_kyc_col = _c("guardian_kyc_type")
    g_ckyc_col = _c("guardian_ckyc_number")

    kra_exempt_col = _c("primary_holder_kra_exempt_ref")
    s2_kra_exempt_col = _c("second_holder_kra_exempt_ref")
    s3_kra_exempt_col = _c("third_holder_kra_exempt_ref")
    g_kra_exempt_col = _c("guardian_exempt_ref_no")

    aadh_col = _c("aadhaar_updated", "aadhaar_flag")
    mapin_col = _c("mapin_id")
    paperless_col = _c("paperless_flag")
    lei_no_col = _c("lei_no")
    lei_validity_col = _c("lei_validity")

    email_decl_col = _c("email_decl_flag")
    mobile_decl_col = _c("mobile_decl_flag")

    email_col = _c("email", "primary_holder_email")
    mobile_col = _c("indian_mobile_no", "mobile", "mobile_no")
    resi_phone_col = _c("resi_phone")
    resi_fax_col = _c("resi_fax")
    office_phone_col = _c("office_phone")
    office_fax_col = _c("office_fax")
    comm_mode_col = _c("comm_mode")

    addr1_col = _c("address1", "address_1", "address_line_1")
    addr2_col = _c("address2", "address_2")
    addr3_col = _c("address3", "address_3")
    city_col = _c("city")
    state_col = _c("state")
    pin_col = _c("pincode", "pin_code")
    country_col = _c("country")

    f_addr1_col = _c("foreign_address1")
    f_addr2_col = _c("foreign_address2")
    f_addr3_col = _c("foreign_address3")
    f_city_col = _c("foreign_city")
    f_pin_col = _c("foreign_pincode")
    f_state_col = _c("foreign_state")
    f_country_col = _c("foreign_country")
    f_resi_phone_col = _c("foreign_resi_phone")
    f_resi_fax_col = _c("foreign_resi_fax")
    f_office_phone_col = _c("foreign_office_phone")
    f_office_fax_col = _c("foreign_office_fax")

    cheque_name_col = _c("cheque_name")
    div_pay_col = _c("div_pay_mode")

    branch_dealer_col = _c("branch_dealer")
    nom_opt_col = _c("nomination_opt")
    nom_auth_mode_col = _c("nomination_auth_mode")
    nom_flag_col = _c("nomination_flag")
    nom_auth_date_col = _c("nomination_auth_date")
    g_relationship_col = _c("guardian_relationship")

    created_by_col = _c("created_by")
    created_col = _c("created_at", "creation_date")
    modified_by_col = _c("last_modified_by")
    modified_col = _c("last_modified_at", "last_modified_date")

    if not code_col or not fname_col:
        return False, "Missing critical columns: client_code / primary_holder_first_name"

    batch = _batch_id("BSE_CLIENT", file.name)
    client_rows = []

    for _, row in df.iterrows():
        code = clean_str(row.get(code_col, ""))
        if not code:
            continue

        # ── Build ALL 209 columns in exact init.py schema order ──────────
        # [0-9] Core identity
        core = (
            clean_str(row.get(member_col, "")) if member_col else "",
            code,
            clean_str(row.get(fname_col, "")),
            clean_str(row.get(mname_col, "")) if mname_col else "",
            clean_str(row.get(lname_col, "")) if lname_col else "",
            clean_str(row.get(tax_col, "")) if tax_col else "",
            clean_str(row.get(gender_col, "")) if gender_col else "",
            parse_date_safe(row.get(dob_col, "")) if dob_col else "",
            clean_str(row.get(occ_col, "")) if occ_col else "",
            clean_str(row.get(hold_col, "")) if hold_col else "",
        )

        # [10-21] Second/Third holder + Guardian names + DOBs
        holders = (
            clean_str(row.get(s2_fname_col, "")) if s2_fname_col else "",
            clean_str(row.get(s2_mname_col, "")) if s2_mname_col else "",
            clean_str(row.get(s2_lname_col, "")) if s2_lname_col else "",
            clean_str(row.get(s3_fname_col, "")) if s3_fname_col else "",
            clean_str(row.get(s3_mname_col, "")) if s3_mname_col else "",
            clean_str(row.get(s3_lname_col, "")) if s3_lname_col else "",
            parse_date_safe(row.get(s2_dob_col, "")) if s2_dob_col else "",
            parse_date_safe(row.get(s3_dob_col, "")) if s3_dob_col else "",
            clean_str(row.get(g_fname_col, "")) if g_fname_col else "",
            clean_str(row.get(g_mname_col, "")) if g_mname_col else "",
            clean_str(row.get(g_lname_col, "")) if g_lname_col else "",
            parse_date_safe(row.get(g_dob_col, "")) if g_dob_col else "",
        )

        # [22-25] PAN exempt flags
        pan_exempt = (
            clean_str(row.get(pan_exempt_col, "")) if pan_exempt_col else "",
            clean_str(row.get(s2_pan_exempt_col, "")) if s2_pan_exempt_col else "",
            clean_str(row.get(s3_pan_exempt_col, "")) if s3_pan_exempt_col else "",
            clean_str(row.get(g_pan_exempt_col, "")) if g_pan_exempt_col else "",
        )

        # [26-29] PAN numbers
        pan_nums = (
            clean_str(row.get(pan_col, "")).upper() if pan_col else "",
            clean_str(row.get(s2_pan_col, "")).upper() if s2_pan_col else "",
            clean_str(row.get(s3_pan_col, "")).upper() if s3_pan_col else "",
            clean_str(row.get(g_pan_col, "")).upper() if g_pan_col else "",
        )

        # [30-33] Exempt categories
        exempt = (
            clean_str(row.get(exempt_cat_col, "")) if exempt_cat_col else "",
            clean_str(row.get(s2_exempt_cat_col, "")) if s2_exempt_cat_col else "",
            clean_str(row.get(s3_exempt_cat_col, "")) if s3_exempt_cat_col else "",
            clean_str(row.get(g_exempt_cat_col, "")) if g_exempt_cat_col else "",
        )

        # [34-41] Client type, PMS, DP IDs
        client_meta = (
            clean_str(row.get(client_type_col, "")) if client_type_col else "",
            clean_str(row.get(pms_col, "")) if pms_col else "",
            clean_str(row.get(default_dp_col, "")) if default_dp_col else "",
            clean_str(row.get(cdsl_dpid_col, "")) if cdsl_dpid_col else "",
            clean_str(row.get(cdsl_cltid_col, "")) if cdsl_cltid_col else "",
            clean_str(row.get(cmbp_id_col, "")) if cmbp_id_col else "",
            clean_str(row.get(nsdl_dpid_col, "")) if nsdl_dpid_col else "",
            clean_str(row.get(nsdl_cltid_col, "")) if nsdl_cltid_col else "",
        )

        # [42-91] Bank 1–5 (10 fields each) — FIXED with _cflex
        bank_fields = []
        for seq in range(1, 6):
            pfx = f"bank{seq}_"
            acno_col = _cflex(df.columns, f"{pfx}account_no", f"{pfx}ac_no", f"account_no_{seq}")
            type_col = _cflex(df.columns, f"{pfx}account_type", f"account_type_{seq}")
            micr_col = _cflex(df.columns, f"{pfx}micr_no", f"micr_no_{seq}", f"micr_{seq}")
            ifsc_col = _cflex(df.columns, f"{pfx}ifsc_code", f"ifsc_code_{seq}", f"ifsc_{seq}")
            bname_col = _cflex(df.columns, f"{pfx}bank_name", f"bank_name_{seq}")
            branch_col = _cflex(df.columns, f"{pfx}bank_branch", f"bank_branch_{seq}", f"branch_{seq}")

            has_acct = bool(acno_col and clean_str(row.get(acno_col, "")))
            bank_fields += [
                clean_str(row.get(type_col or "", "")),
                clean_str(row.get(acno_col or "", "")),
                clean_str(row.get(micr_col or "", "")),
                clean_str(row.get(ifsc_col or "", "")),
                clean_str(row.get(bname_col or "", "")),
                clean_str(row.get(branch_col or "", "")),
                ("Y" if seq == 1 and has_acct else ("N" if has_acct else "")),
                parse_date_safe(row.get(created_col, "")) if (created_col and has_acct) else "",
                parse_date_safe(row.get(modified_col, "")) if (modified_col and has_acct) else "",
                ("Active" if has_acct else ""),
            ]

        # [92-93] Cheque name, Div pay mode
        cheque_div = (
            clean_str(row.get(cheque_name_col, "")) if cheque_name_col else "",
            clean_str(row.get(div_pay_col, "")) if div_pay_col else "",
        )

        # [94-104] Address + Contact
        address_contact = (
            clean_str(row.get(addr1_col, "")) if addr1_col else "",
            clean_str(row.get(addr2_col, "")) if addr2_col else "",
            clean_str(row.get(addr3_col, "")) if addr3_col else "",
            clean_str(row.get(city_col, "")) if city_col else "",
            clean_str(row.get(state_col, "")) if state_col else "",
            clean_str(row.get(pin_col, "")) if pin_col else "",
            clean_str(row.get(country_col, "")) if country_col else "",
            clean_str(row.get(resi_phone_col, "")) if resi_phone_col else "",
            clean_str(row.get(resi_fax_col, "")) if resi_fax_col else "",
            clean_str(row.get(office_phone_col, "")) if office_phone_col else "",
            clean_str(row.get(office_fax_col, "")) if office_fax_col else "",
        )

        # [105-106] Email, Comm mode
        email_comm = (
            clean_str(row.get(email_col, "")).lower() if email_col else "",
            clean_str(row.get(comm_mode_col, "")) if comm_mode_col else "",
        )

        # [107-117] Foreign address + contact
        foreign = (
            clean_str(row.get(f_addr1_col, "")) if f_addr1_col else "",
            clean_str(row.get(f_addr2_col, "")) if f_addr2_col else "",
            clean_str(row.get(f_addr3_col, "")) if f_addr3_col else "",
            clean_str(row.get(f_city_col, "")) if f_city_col else "",
            clean_str(row.get(f_pin_col, "")) if f_pin_col else "",
            clean_str(row.get(f_state_col, "")) if f_state_col else "",
            clean_str(row.get(f_country_col, "")) if f_country_col else "",
            clean_str(row.get(f_resi_phone_col, "")) if f_resi_phone_col else "",
            clean_str(row.get(f_resi_fax_col, "")) if f_resi_fax_col else "",
            clean_str(row.get(f_office_phone_col, "")) if f_office_phone_col else "",
            clean_str(row.get(f_office_fax_col, "")) if f_office_fax_col else "",
        )

        # [118] Mobile
        mobile = (clean_str(row.get(mobile_col, "")) if mobile_col else "",)

        # [119-126] KYC/CKYC for all holders
        kyc_ckyc = (
            clean_str(row.get(kyc_col, "")) if kyc_col else "",
            clean_str(row.get(ckyc_col, "")) if ckyc_col else "",
            clean_str(row.get(s2_kyc_col, "")) if s2_kyc_col else "",
            clean_str(row.get(s2_ckyc_col, "")) if s2_ckyc_col else "",
            clean_str(row.get(s3_kyc_col, "")) if s3_kyc_col else "",
            clean_str(row.get(s3_ckyc_col, "")) if s3_ckyc_col else "",
            clean_str(row.get(g_kyc_col, "")) if g_kyc_col else "",
            clean_str(row.get(g_ckyc_col, "")) if g_ckyc_col else "",
        )

        # [127-130] KRA exempt refs
        kra = (
            clean_str(row.get(kra_exempt_col, "")) if kra_exempt_col else "",
            clean_str(row.get(s2_kra_exempt_col, "")) if s2_kra_exempt_col else "",
            clean_str(row.get(s3_kra_exempt_col, "")) if s3_kra_exempt_col else "",
            clean_str(row.get(g_kra_exempt_col, "")) if g_kra_exempt_col else "",
        )

        # [131-135] Aadhaar, Mapin, Paperless, LEI
        ids = (
            clean_str(row.get(aadh_col, "")) if aadh_col else "",
            clean_str(row.get(mapin_col, "")) if mapin_col else "",
            clean_str(row.get(paperless_col, "")) if paperless_col else "",
            clean_str(row.get(lei_no_col, "")) if lei_no_col else "",
            clean_str(row.get(lei_validity_col, "")) if lei_validity_col else "",
        )

        # [136-137] Email/Mobile decl flags
        decl_flags = (
            clean_str(row.get(email_decl_col, "")) if email_decl_col else "",
            clean_str(row.get(mobile_decl_col, "")) if mobile_decl_col else "",
        )

        # [138] Branch dealer
        branch_dealer = (clean_str(row.get(branch_dealer_col, "")) if branch_dealer_col else "",)

        # [139-140] Nomination opt, auth mode
        nom_opt = (
            clean_str(row.get(nom_opt_col, "")) if nom_opt_col else "",
            clean_str(row.get(nom_auth_mode_col, "")) if nom_auth_mode_col else "",
        )

        # [141-157] Nominee 1 (17 fields) — FIXED with _cflex internally
        nom1 = _build_nominee(row, _c, 1)

        # [158-174] Nominee 2 (17 fields)
        nom2 = _build_nominee(row, _c, 2)

        # [175-191] Nominee 3 (17 fields)
        nom3 = _build_nominee(row, _c, 3)

        # [192] Nom SOA
        nom_soa = (clean_str(row.get(_c("nom_soa"), "")),)

        # [193-200] Second/Third holder email/mobile decl
        holder_comm = (
            clean_str(row.get(_c("second_holder_email"), "")).lower(),
            clean_str(row.get(_c("second_holder_email_decl"), "")),
            clean_str(row.get(_c("second_holder_mobile"), "")),
            clean_str(row.get(_c("second_holder_mobile_decl"), "")),
            clean_str(row.get(_c("third_holder_email"), "")).lower(),
            clean_str(row.get(_c("third_holder_email_decl"), "")),
            clean_str(row.get(_c("third_holder_mobile"), "")),
            clean_str(row.get(_c("third_holder_mobile_decl"), "")),
        )

        # [201-203] Nomination flag, auth date, guardian relationship
        nom_end = (
            clean_str(row.get(nom_flag_col, "")) if nom_flag_col else "",
            parse_date_safe(row.get(nom_auth_date_col, "")) if nom_auth_date_col else "",
            clean_str(row.get(g_relationship_col, "")) if g_relationship_col else "",
        )

        # [204-208] Created/Modified by/at + upload_batch
        audit = (
            clean_str(row.get(created_by_col, "")) if created_by_col else "",
            parse_date_safe(row.get(created_col, "")) if created_col else "",
            clean_str(row.get(modified_by_col, "")) if modified_by_col else "",
            parse_date_safe(row.get(modified_col, "")) if modified_col else "",
            batch,
        )

        # ── Combine all 209 fields ─────────────────────────────────────
        full_row = (
                core + holders + pan_exempt + pan_nums + exempt + client_meta +
                tuple(bank_fields) + cheque_div + address_contact + email_comm +
                foreign + mobile + kyc_ckyc + kra + ids + decl_flags +
                branch_dealer + nom_opt + nom1 + nom2 + nom3 + nom_soa +
                holder_comm + nom_end + audit
        )

        client_rows.append(full_row)

    if not client_rows:
        return False, "No valid rows found"

    # ── INSERT with ALL 209 columns in schema order ────────────────────
    insert_sql = """INSERT OR IGNORE INTO clients
        (member_code, client_code, primary_holder_first_name, primary_holder_middle_name,
         primary_holder_last_name, tax_status, gender, primary_holder_dob_incorp,
         occupation_code, holding_nature, second_holder_first_name, second_holder_middle_name,
         second_holder_last_name, third_holder_first_name, third_holder_middle_name,
         third_holder_last_name, second_holder_dob, third_holder_dob, guardian_first_name,
         guardian_middle_name, guardian_last_name, guardian_dob, primary_holder_pan_exempt,
         second_holder_pan_exempt, third_holder_pan_exempt, guardian_pan_exempt,
         primary_holder_pan, second_holder_pan, third_holder_pan, guardian_pan,
         primary_holder_exempt_category, second_holder_exempt_category,
         third_holder_exempt_category, guardian_exempt_category, client_type, pms,
         default_dp, cdsl_dpid, cdsl_cltid, cmbp_id, nsdl_dpid, nsdl_cltid,
         account_type_1, account_no_1, micr_no_1, ifsc_code_1, bank_name_1, bank_branch_1,
         default_bank_flag_1, bank1_created_at, bank1_last_modified_at, bank1_status,
         account_type_2, account_no_2, micr_no_2, ifsc_code_2, bank_name_2, bank_branch_2,
         default_bank_flag_2, bank2_created_at, bank2_last_modified_at, bank2_status,
         account_type_3, account_no_3, micr_no_3, ifsc_code_3, bank_name_3, bank_branch_3,
         default_bank_flag_3, bank3_created_at, bank3_last_modified_at, bank3_status,
         account_type_4, account_no_4, micr_no_4, ifsc_code_4, bank_name_4, bank_branch_4,
         default_bank_flag_4, bank4_created_at, bank4_last_modified_at, bank4_status,
         account_type_5, account_no_5, micr_no_5, ifsc_code_5, bank_name_5, bank_branch_5,
         default_bank_flag_5, bank5_created_at, bank5_last_modified_at, bank5_status,
         cheque_name, div_pay_mode, address1, address2, address3, city, state, pincode,
         country, resi_phone, resi_fax, office_phone, office_fax, email, comm_mode,
         foreign_address1, foreign_address2, foreign_address3, foreign_city,
         foreign_pincode, foreign_state, foreign_country, foreign_resi_phone,
         foreign_resi_fax, foreign_office_phone, foreign_office_fax, mobile,
         primary_holder_kyc_type, primary_holder_ckyc_number, second_holder_kyc_type,
         second_holder_ckyc_number, third_holder_kyc_type, third_holder_ckyc_number,
         guardian_kyc_type, guardian_ckyc_number, primary_holder_kra_exempt_ref,
         second_holder_kra_exempt_ref, third_holder_kra_exempt_ref, guardian_exempt_ref_no,
         aadhaar_updated, mapin_id, paperless_flag, lei_no, lei_validity,
         email_decl_flag, mobile_decl_flag, branch_dealer, nomination_opt,
         nomination_auth_mode, nominee1_name, nominee1_relationship, nominee1_percentage,
         nominee1_minor_flag, nominee1_dob, nominee1_guardian, nominee1_guardian_pan,
         nom1_id_typ, nom1_idno, nom1_email, nom1_mob, nom1_add1, nom1_add2, nom1_add3,
         nom1_city, nom1_pin, nom1_con, nominee2_name, nominee2_relationship,
         nominee2_percentage, nominee2_dob, nominee2_minor_flag, nominee2_guardian,
         nominee2_guardian_pan, nom2_id_typ, nom2_idno, nom2_email, nom2_mob, nom2_add1,
         nom2_add2, nom2_add3, nom2_city, nom2_pin, nom2_con, nominee3_name,
         nominee3_relationship, nominee3_percentage, nominee3_dob, nominee3_minor_flag,
         nominee3_guardian, nominee3_guardian_pan, nom3_id_typ, nom3_idno, nom3_email,
         nom3_mob, nom3_add1, nom3_add2, nom3_add3, nom3_city, nom3_pin, nom3_con,
         nom_soa, second_holder_email, second_holder_email_decl, second_holder_mobile,
         second_holder_mobile_decl, third_holder_email, third_holder_email_decl,
         third_holder_mobile, third_holder_mobile_decl, nomination_flag,
         nomination_auth_date, guardian_relationship, created_by, created_at,
         last_modified_by, last_modified_at, upload_batch)
        VALUES (""" + ",".join(["?"] * 209) + ")"

    inserted = skipped = 0
    with get_conn() as conn:
        if replace:
            conn.execute("DELETE FROM clients")
            conn.executemany(insert_sql, client_rows)
            inserted = len(client_rows)
        else:
            existing = {r[1] for r in conn.execute("SELECT client_code FROM clients").fetchall()}
            new_rows = [r for r in client_rows if r[1] not in existing]
            skipped = len(client_rows) - len(new_rows)
            if new_rows:
                conn.executemany(insert_sql, new_rows)
            inserted = len(new_rows)

    msg = f"Imported {inserted} clients"
    if skipped:
        msg += f" | Skipped {skipped} existing"
    return True, msg



def _build_nominee(row, _c_func, seq: int) -> tuple:
    """Build a 17-field nominee tuple in init.py schema order. Uses flexible
    header matching since BSE exports vary in numbering style."""
    pfx = f"nominee{seq}_"
    npfx = f"nom{seq}_"
    cols = row.index

    def F(*bases):
        return _cflex(cols, *bases)

    name_val = clean_str(row.get(F(f"{pfx}name", f"nominee_{seq}_name") or "", ""))
    relationship = clean_str(row.get(F(f"{pfx}relationship") or "", ""))
    percentage = _sf(row.get(F(f"{pfx}percentage") or "", 0))
    minor_flag = clean_str(row.get(F(f"{pfx}is_minor", f"{pfx}minor_flag") or "", ""))
    dob = parse_date_safe(row.get(F(f"{pfx}dob") or "", ""))
    guardian = clean_str(row.get(F(f"{pfx}guardian_name") or "", ""))
    guardian_pan = clean_str(row.get(F(f"{pfx}guardian_pan") or "", ""))
    id_typ = clean_str(row.get(F(f"{npfx}id_typ") or "", ""))
    idno = clean_str(row.get(F(f"{npfx}idno") or "", ""))
    nom_email = clean_str(row.get(F(f"{pfx}email", f"{npfx}email") or "", "")).lower()
    nom_mob = clean_str(row.get(F(f"{pfx}mobile", f"{npfx}mob") or "", ""))
    add1 = clean_str(row.get(F(f"{pfx}address1", f"{npfx}add1") or "", ""))
    add2 = clean_str(row.get(F(f"{pfx}address2", f"{npfx}add2") or "", ""))
    add3 = clean_str(row.get(F(f"{pfx}address3", f"{npfx}add3") or "", ""))
    nom_city = clean_str(row.get(F(f"{pfx}city", f"{npfx}city") or "", ""))
    nom_pin = clean_str(row.get(F(f"{pfx}pincode", f"{npfx}pin") or "", ""))
    nom_con = clean_str(row.get(F(f"{npfx}con") or "", ""))
    return (
        name_val, relationship, percentage, minor_flag, dob, guardian, guardian_pan,
        id_typ, idno, nom_email, nom_mob, add1, add2, add3, nom_city, nom_pin, nom_con,
    )


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
    sub_col = _c("sub_broker_arn_code", "sub_broker", "sub_broker_code")
    regn_col = _c("regn_date", "registration_date")
    pgref_col = _c("pg_bank_ref_no", "pg_ref_no")
    rem_col = _c("remarks")
    email_col = _c("primary_holder_email", "primary_email", "email")
    mob_col = _c("primary_holder_mobile", "primary_mobile", "mobile")
    dpc_col = _c("dpc_flag")
    dpsub_col = _c("dp_trans_sub_broker", "dp_trans_sub")
    euindecl_col = _c("euin_decl")
    exrem_col = _c("exchange_remark")
    health_col = _c("health_decl_flag")
    nomdob_col = _c("nominee_dob")
    disc_col = _c("disclaimer_flag")
    intref_col = _c("internal_ref_no")
    s2email_col = _c("second_holder_email")
    s2mob_col = _c("second_holder_mobile")
    s3email_col = _c("third_holder_email")
    s3mob_col = _c("third_holder_mobile")

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
            clean_str(row.get(status_col, "")) if status_col else "",
            clean_str(row.get(member_col, "")) if member_col else "",
            clean_str(row.get(client_col, "")),
            clean_str(row.get(name_col, "")) if name_col else "",
            clean_str(row.get(pgref_col, "")) if pgref_col else "",
            regn,
            parse_date_safe(row.get(regn_col, "")) if regn_col else "",
            clean_str(row.get(amc_col, "")) if amc_col else "",
            clean_str(row.get(scheme_col, "")) if scheme_col else "",
            clean_str(row.get(sname_col, "")) if sname_col else "",
            clean_str(row.get(freq_col, "")) if freq_col else "",
            parse_date_safe(row.get(sd_col, "")) if sd_col else "",
            parse_date_safe(row.get(ed_col, "")) if ed_col else "",
            _sf(row.get(amt_col, 0)) if amt_col else 0.0,
            0.0,  # brokerage — not resolved from a known header; keep 0.0 unless mapped
            "",  # entry_by — not resolved; fill if/when a source column is identified
            clean_str(row.get(mandate_col, "")) if mandate_col else "",
            clean_str(row.get(dpc_col, "")) if dpc_col else "",
            clean_str(row.get(dpsub_col, "")) if dpsub_col else "",
            clean_str(row.get(euin_col, "")) if euin_col else "",
            clean_str(row.get(euindecl_col, "")) if euindecl_col else "",
            clean_str(row.get(fo_col, "")) if fo_col else "",
            clean_str(row.get(folio_col, "")) if folio_col else "",
            clean_str(row.get(rem_col, "")) if rem_col else "",
            clean_str(row.get(sub_col, "")) if sub_col else "",
            _si(row.get(inst_col, 0)) if inst_col else 0,
            clean_str(row.get(exrem_col, "")) if exrem_col else "",
            clean_str(row.get(health_col, "")) if health_col else "",
            parse_date_safe(row.get(nomdob_col, "")) if nomdob_col else "",
            clean_str(row.get(disc_col, "")) if disc_col else "",
            clean_str(row.get(intref_col, "")) if intref_col else "",
            clean_str(row.get(email_col, "")).lower() if email_col else "",
            clean_str(row.get(mob_col, "")) if mob_col else "",
            clean_str(row.get(s2email_col, "")).lower() if s2email_col else "",
            clean_str(row.get(s2mob_col, "")) if s2mob_col else "",
            clean_str(row.get(s3email_col, "")).lower() if s3email_col else "",
            clean_str(row.get(s3mob_col, "")) if s3mob_col else "",
            batch,
        ))

    if not rows:
        return False, "0 XSIP rows found", {}

    insert_sql = """INSERT OR IGNORE INTO bse_xsip
        (status, member_code, client_code, client_name, pg_bank_ref_no, xsip_regn_no,
         regn_date, amc_name, rta_scheme_code, scheme_name, frequency_type, start_date,
         end_date, installments_amt, brokerage, entry_by, mandate_id, dpc_flag,
         dp_trans_sub_broker, euin, euin_decl, first_order, folio_no, remarks,
         sub_broker_arn_code, no_of_installments, exchange_remark, health_decl_flag,
         nominee_dob, disclaimer_flag, internal_ref_no, primary_holder_email,
         primary_holder_mobile, second_holder_email, second_holder_mobile,
         third_holder_email, third_holder_mobile, upload_batch)
        VALUES (""" + ",".join(["?"] * 38) + ")"

    inserted = dupes = 0
    with get_conn() as conn:
        if replace:
            conn.execute("DELETE FROM bse_xsip")
            conn.executemany(insert_sql, rows)
            inserted = len(rows)
        else:
            before = _count_before_after(conn, "bse_xsip")
            conn.executemany(insert_sql, rows)
            inserted, dupes = _inserted_dupes(before, conn, "bse_xsip", len(rows))

    active = sum(1 for r in rows if "active" in str(r[0]).lower())
    preview = {"rows": inserted, "skipped": skipped, "duplicates": dupes, "active": active}
    msg = f"Imported {inserted} XSIP records | Active: {active}"
    if skipped: msg += f" | Skipped {skipped}"
    if dupes:   msg += f" | {dupes} duplicates"
    return True, msg, preview


def parse_bse_scheme_master(file, replace: bool) -> tuple[bool, str, dict]:
    """
    BSE Scheme Master Excel/CSV → bse_scheme_master table.
    Stores all raw columns. Reuses existing helpers (clean_str, _sf, parse_date_safe).
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

    folio_col = next((c for c in ["FOLIOCHK", "FOLIO_NO"] if c in df.columns), None)
    if not folio_col or "INV_NAME" not in df.columns:
        return False, f"Missing FOLIOCHK/INV_NAME. Found: {list(df.columns)[:20]}", {}

    batch = _batch_id("CAMS_R9", file.name)
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
                clean_str(row.get("ADDRESS1", "")),
                clean_str(row.get("ADDRESS2", "")),
                clean_str(row.get("ADDRESS3", "")),
                clean_str(row.get("CITY", "")),
                clean_str(row.get("PINCODE", "")),
                clean_str(row.get("PRODUCT", row.get("PRODCODE", ""))),
                clean_str(row.get("SCH_NAME", row.get("SCHEME", ""))),
                parse_date_safe(row.get("REP_DATE", "")),
                _sf(row.get("CLOS_BAL", 0)),
                _sf(row.get("RUPEE_BAL", 0)),
                clean_str(row.get("JNT_NAME1", "")),
                clean_str(row.get("JNT_NAME2", "")),
                clean_str(row.get("PHONE_OFF", "")),
                clean_str(row.get("PHONE_RES", "")),
                clean_str(row.get("EMAIL", "")).lower(),
                clean_str(row.get("HOLDING_NATURE", "")),
                clean_str(row.get("UIN_NO", "")),
                clean_str(row.get("PAN_NO", "")).upper(),
                clean_str(row.get("JOINT1_PAN", "")).upper(),
                clean_str(row.get("JOINT2_PAN", "")).upper(),
                clean_str(row.get("GUARD_PAN", "")).upper(),
                clean_str(row.get("TAX_STATUS", "")),
                clean_str(row.get("BROKER_CODE", row.get("BROKCODE", ""))),
                clean_str(row.get("SUBBROKER", "")),
                clean_str(row.get("REINV_FLAG", "")),
                clean_str(row.get("BANK_NAME", "")),
                clean_str(row.get("BRANCH", "")),
                clean_str(row.get("AC_TYPE", "")),
                clean_str(row.get("AC_NO", "")),
                clean_str(row.get("B_ADDRESS1", "")),
                clean_str(row.get("B_ADDRESS2", "")),
                clean_str(row.get("B_ADDRESS3", "")),
                clean_str(row.get("B_CITY", "")),
                clean_str(row.get("B_PINCODE", "")),
                parse_date_safe(row.get("INV_DOB", "")),
                clean_str(row.get("MOBILE_NO", "")),
                clean_str(row.get("OCCUPATION", "")),
                clean_str(row.get("INV_IIN", "")),
                clean_str(row.get("NOM_NAME", "")),
                clean_str(row.get("RELATION", "")),
                clean_str(row.get("NOM_ADDR1", "")),
                clean_str(row.get("NOM_ADDR2", "")),
                clean_str(row.get("NOM_ADDR3", "")),
                clean_str(row.get("NOM_CITY", "")),
                clean_str(row.get("NOM_STATE", "")),
                clean_str(row.get("NOM_PINCODE", "")),
                clean_str(row.get("NOM_PH_OFF", "")),
                clean_str(row.get("NOM_PH_RES", "")),
                clean_str(row.get("NOM_EMAIL", "")),
                _sf(row.get("NOM_PERCENTAGE", 0)),
                clean_str(row.get("NOM2_NAME", "")),
                clean_str(row.get("NOM2_RELATION", "")),
                clean_str(row.get("NOM2_ADDR1", "")),
                clean_str(row.get("NOM2_ADDR2", "")),
                clean_str(row.get("NOM2_ADDR3", "")),
                clean_str(row.get("NOM2_CITY", "")),
                clean_str(row.get("NOM2_STATE", "")),
                clean_str(row.get("NOM2_PINCODE", "")),
                clean_str(row.get("NOM2_PH_OFF", "")),
                clean_str(row.get("NOM2_PH_RES", "")),
                clean_str(row.get("NOM2_EMAIL", "")),
                _sf(row.get("NOM2_PERCENTAGE", 0)),
                clean_str(row.get("NOM3_NAME", "")),
                clean_str(row.get("NOM3_RELATION", "")),
                clean_str(row.get("NOM3_ADDR1", "")),
                clean_str(row.get("NOM3_ADDR2", "")),
                clean_str(row.get("NOM3_ADDR3", "")),
                clean_str(row.get("NOM3_CITY", "")),
                clean_str(row.get("NOM3_STATE", "")),
                clean_str(row.get("NOM3_PINCODE", "")),
                clean_str(row.get("NOM3_PH_OFF", "")),
                clean_str(row.get("NOM3_PH_RES", "")),
                clean_str(row.get("NOM3_EMAIL", "")),
                _sf(row.get("NOM3_PERCENTAGE", 0)),
                clean_str(row.get("IFSC_CODE", "")),
                clean_str(row.get("DP_ID", "")),
                clean_str(row.get("DEMAT", "")),
                clean_str(row.get("GUARD_NAME", "")),
                clean_str(row.get("BROKCODE", "")),
                parse_date_safe(row.get("FOLIO_DATE", "")),
                clean_str(row.get("AADHAAR", "")),
                clean_str(row.get("TPA_LINKED", "")),
                clean_str(row.get("FH_CKYC_NO", "")),
                clean_str(row.get("JH1_CKYC", "")),
                clean_str(row.get("JH2_CKYC", "")),
                clean_str(row.get("G_CKYC_NO", "")),
                parse_date_safe(row.get("JH1_DOB", "")),
                parse_date_safe(row.get("JH2_DOB", "")),
                parse_date_safe(row.get("GUARDIAN_DOB", "")),
                clean_str(row.get("AMC_CODE", "")),
                clean_str(row.get("GST_STATE_CODE", "")),
                clean_str(row.get("FOLIO_OLD", "")),
                clean_str(row.get("SCHEME_FOLIO_NUMBER", "")),
                clean_str(row.get("COUNTRY", "")),
                clean_str(row.get("REMARKS", "")),
                clean_str(row.get("JH1_EMAIL", "")).lower(),
                clean_str(row.get("JH2_EMAIL", "")).lower(),
                clean_str(row.get("JH1_MOBILE_NO", "")),
                clean_str(row.get("JH2_MOBILE_NO", "")),
                batch,
            ))
        except Exception as exc:
            log.warning("Skipped CAMS folio row: %s", exc)
            skipped += 1

    if not rows:
        return False, "0 folio rows parsed", {}

    insert_sql = """INSERT OR IGNORE INTO cams_folio
        (foliochk, inv_name, address1, address2, address3, city, pincode, product, sch_name,
         rep_date, clos_bal, rupee_bal, jnt_name1, jnt_name2, phone_off, phone_res, email,
         holding_nature, uin_no, pan_no, joint1_pan, joint2_pan, guard_pan, tax_status,
         broker_code, subbroker, reinv_flag, bank_name, branch, ac_type, ac_no,
         b_address1, b_address2, b_address3, b_city, b_pincode, inv_dob, mobile_no,
         occupation, inv_iin, nom_name, relation, nom_addr1, nom_addr2, nom_addr3,
         nom_city, nom_state, nom_pincode, nom_ph_off, nom_ph_res, nom_email, nom_percentage,
         nom2_name, nom2_relation, nom2_addr1, nom2_addr2, nom2_addr3, nom2_city, nom2_state,
         nom2_pincode, nom2_ph_off, nom2_ph_res, nom2_email, nom2_percentage,
         nom3_name, nom3_relation, nom3_addr1, nom3_addr2, nom3_addr3, nom3_city, nom3_state,
         nom3_pincode, nom3_ph_off, nom3_ph_res, nom3_email, nom3_percentage,
         ifsc_code, dp_id, demat, guard_name, brokcode, folio_date, aadhaar, tpa_linked,
         fh_ckyc_no, jh1_ckyc, jh2_ckyc, g_ckyc_no, jh1_dob, jh2_dob, guardian_dob,
         amc_code, gst_state_code, folio_old, scheme_folio_number, country, remarks,
         jh1_email, jh2_email, jh1_mobile_no, jh2_mobile_no, upload_batch)
        VALUES (""" + ",".join(["?"] * 102) + ")"

    inserted = dupes = 0
    with get_conn() as conn:
        if replace:
            conn.execute("DELETE FROM cams_folio")
            conn.executemany(insert_sql, rows)
            inserted = len(rows)
        else:
            before = _count_before_after(conn, "cams_folio")
            conn.executemany(insert_sql, rows)
            inserted, dupes = _inserted_dupes(before, conn, "cams_folio", len(rows))

    total_aum = sum(r[11] for r in rows)  # rupee_bal index
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
                clean_str(row.get("PRODCODE", "")),
                clean_str(row.get("SCHEME", "")),
                clean_str(row.get("INV_NAME", "")).strip(),
                clean_str(row.get("TRXNTYPE", "")),
                clean_str(row.get("TRXNNO", "")),
                clean_str(row.get("TRXNMODE", "")),
                clean_str(row.get("TRXNSTAT", "")),
                clean_str(row.get("USERCODE", "")),
                clean_str(row.get("USRTRXNO", "")),
                parse_date_safe(row.get("TRADDATE", "")),
                parse_date_safe(row.get("POSTDATE", "")),
                _sf(row.get("PURPRICE", 0)),
                _sf(row.get("UNITS", 0)),
                _sf(row.get("AMOUNT", 0)),
                clean_str(row.get("BROKCODE", "")),
                clean_str(row.get("SUBBROK", "")),
                _sf(row.get("BROKPERC", 0)),
                _sf(row.get("BROKCOMM", 0)),
                clean_str(row.get("ALTFOLIO", "")),
                parse_date_safe(row.get("REP_DATE", "")),
                clean_str(row.get("TIME1", "")),
                clean_str(row.get("TRXNSUBTYP", "")),
                clean_str(row.get("APPLICATION_NO", "")),
                clean_str(row.get("TRXN_NATURE", "")),
                _sf(row.get("TAX", 0)),
                _sf(row.get("TOTAL_TAX", 0)),
                clean_str(row.get("TE_15H", "")),
                clean_str(row.get("MICR_NO", "")),
                clean_str(row.get("REMARKS", "")),
                clean_str(row.get("SWFLAG", "")),
                clean_str(row.get("OLD_FOLIO", "")),
                _si(row.get("SEQ_NO", 0)),
                clean_str(row.get("REINVEST_FLAG", "")),
                clean_str(row.get("MULT_BROK", "")),
                _sf(row.get("STT", 0)),
                clean_str(row.get("LOCATION", "")),
                clean_str(row.get("SCHEME_TYPE", "")),
                clean_str(row.get("TAX_STATUS", "")),
                _sf(row.get("LOAD", 0)),
                clean_str(row.get("SCANREFNO", "")),
                clean_str(row.get("PAN", "")).upper(),
                clean_str(row.get("INV_IIN", "")),
                clean_str(row.get("TARG_SRC_SCHEME", "")),
                clean_str(row.get("TRXN_TYPE_FLAG", "")),
                clean_str(row.get("TICOB_TRTYPE", "")),
                clean_str(row.get("TICOB_TRNO", "")),
                parse_date_safe(row.get("TICOB_POSTED_DATE", "")),
                clean_str(row.get("DP_ID", "")),
                _sf(row.get("TRXN_CHARGES", 0)),
                _sf(row.get("ELIGIB_AMT", 0)),
                clean_str(row.get("SRC_OF_TXN", "")),
                clean_str(row.get("TRXN_SUFFIX", "")),
                clean_str(row.get("SIPTRXNNO", "")),
                clean_str(row.get("TER_LOCATION", "")),
                clean_str(row.get("EUIN", "")),
                clean_str(row.get("EUIN_VALID", "")),
                clean_str(row.get("EUIN_OPTED", "")),
                clean_str(row.get("SUB_BRK_ARN", "")),
                clean_str(row.get("EXCH_DC_FLAG", "")),
                clean_str(row.get("SRC_BRK_CODE", "")),
                parse_date_safe(row.get("SYS_REGN_DATE", "")),
                clean_str(row.get("AC_NO", "")),
                clean_str(row.get("BANK_NAME", "")),
                clean_str(row.get("REVERSAL_CODE", "")),
                clean_str(row.get("EXCHANGE_FLAG", "")),
                parse_date_safe(row.get("CA_INITIATED_DATE", "")),
                clean_str(row.get("GST_STATE_CODE", "")),
                _sf(row.get("IGST_AMOUNT", 0)),
                _sf(row.get("CGST_AMOUNT", 0)),
                _sf(row.get("SGST_AMOUNT", 0)),
                clean_str(row.get("REV_REMARK", "")),
                clean_str(row.get("ORIGINAL_TRXNNO", "")),
                _sf(row.get("STAMP_DUTY", 0)),
                clean_str(row.get("FOLIO_OLD", "")),
                clean_str(row.get("SCHEME_FOLIO_NUMBER", "")),
                clean_str(row.get("AMC_REF_NO", "")),
                clean_str(row.get("REQUEST_REF_NO", "")),
                clean_str(row.get("TRANSMISSION_FLAG", "")),
                batch,
            ))
        except Exception as exc:
            log.warning("Skipped CAMS txn row: %s", exc)
            skipped += 1

    if not rows:
        return False, "0 transaction rows parsed", {}

    insert_sql = """INSERT OR IGNORE INTO cams_transactions
        (amc_code, folio_no, prodcode, scheme, inv_name, trxntype, trxnno, trxnmode, trxnstat,
         usercode, usrtrxno, traddate, postdate, purprice, units, amount, brokcode, subbrok,
         brokperc, brokcomm, altfolio, rep_date, time1, trxnsubtyp, application_no, trxn_nature,
         tax, total_tax, te_15h, micr_no, remarks, swflag, old_folio, seq_no, reinvest_flag,
         mult_brok, stt, location, scheme_type, tax_status, load, scanrefno, pan, inv_iin,
         targ_src_scheme, trxn_type_flag, ticob_trtype, ticob_trno, ticob_posted_date, dp_id,
         trxn_charges, eligib_amt, src_of_txn, trxn_suffix, siptrxnno, ter_location, euin,
         euin_valid, euin_opted, sub_brk_arn, exch_dc_flag, src_brk_code, sys_regn_date, ac_no,
         bank_name, reversal_code, exchange_flag, ca_initiated_date, gst_state_code,
         igst_amount, cgst_amount, sgst_amount, rev_remark, original_trxnno, stamp_duty,
         folio_old, scheme_folio_number, amc_ref_no, request_ref_no, transmission_flag,
         upload_batch)
        VALUES (""" + ",".join(["?"] * 81) + ")"

    inserted = dupes = 0
    with get_conn() as conn:
        if replace:
            conn.execute("DELETE FROM cams_transactions")
            conn.executemany(insert_sql, rows)
            inserted = len(rows)
        else:
            before = _count_before_after(conn, "cams_transactions")
            conn.executemany(insert_sql, rows)
            inserted, dupes = _inserted_dupes(before, conn, "cams_transactions", len(rows))

    total_amt = sum(r[15] for r in rows)  # amount index
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
                clean_str(row.get("PRODUCT", "")),
                clean_str(row.get("SCHEME", "")),
                folio,
                clean_str(row.get("INV_NAME", "")).strip(),
                clean_str(row.get("AUT_TRNTYP", "")),
                autotr,
                _sf(row.get("AUTO_AMOUNT", 0)),
                parse_date_safe(row.get("FROM_DATE", "")),
                parse_date_safe(row.get("TO_DATE", "")),
                parse_date_safe(row.get("CEASE_DATE", "")),
                clean_str(row.get("PERIODICITY", "")),
                _si(row.get("PERIOD_DAY", 0)),
                clean_str(row.get("INV_IIN", "")),
                clean_str(row.get("PAYMENT_MODE", "")),
                clean_str(row.get("TARGET_SCHEME", "")),
                parse_date_safe(row.get("REG_DATE", "")),
                clean_str(row.get("SUBBROKER", "")),
                clean_str(row.get("REMARKS", "")),
                clean_str(row.get("TOP_UP_FRQ", "")),
                _sf(row.get("TOP_UP_AMT", 0)),
                clean_str(row.get("AC_TYPE", "")),
                clean_str(row.get("BANK", "")),
                clean_str(row.get("BRANCH", "")),
                clean_str(row.get("INSTRM_NO", "")),
                clean_str(row.get("CHEQ_MICR_NO", "")),
                clean_str(row.get("AC_HOLDER_NAME", "")),
                clean_str(row.get("PAN", "")).upper(),
                _sf(row.get("TOP_UP_PERC", 0)),
                clean_str(row.get("EUIN", "")),
                clean_str(row.get("SUB_ARN_CODE", "")),
                clean_str(row.get("TER_LOCATION", "")),
                clean_str(row.get("SCHEME_CODE", "")),
                clean_str(row.get("TARGET_SCHEME_CODE", "")),
                clean_str(row.get("AMC_CODE", "")),
                clean_str(row.get("USER_CODE", "")),
                clean_str(row.get("PACKAGE_NAME", "")),
                clean_str(row.get("SPECIAL_PRODUCT", "")),
                clean_str(row.get("SUBTRXNDESC", "")),
                parse_date_safe(row.get("PAUSE_FROM_DATE", "")),
                parse_date_safe(row.get("PAUSE_TO_DATE", "")),
                clean_str(row.get("FOLIO_OLD", "")),
                clean_str(row.get("FT_SIP_REGNO", "")),
                clean_str(row.get("SCHEME_FOLIO_NUMBER", "")),
                clean_str(row.get("REQUEST_REF_NO", "")),
                batch,
            ))
        except Exception as exc:
            log.warning("Skipped CAMS SIP row: %s", exc)
            skipped += 1

    if not rows:
        return False, "0 SIP rows parsed", {}

    insert_sql = """INSERT OR IGNORE INTO cams_sip
        (product, scheme, folio_no, inv_name, aut_trntyp, auto_trno, auto_amount, from_date,
         to_date, cease_date, periodicity, period_day, inv_iin, payment_mode, target_scheme,
         reg_date, subbroker, remarks, top_up_frq, top_up_amt, ac_type, bank, branch,
         instrm_no, cheq_micr_no, ac_holder_name, pan, top_up_perc, euin, sub_arn_code,
         ter_location, scheme_code, target_scheme_code, amc_code, user_code, package_name,
         special_product, subtrxndesc, pause_from_date, pause_to_date, folio_old,
         ft_sip_regno, scheme_folio_number, request_ref_no, upload_batch)
        VALUES (""" + ",".join(["?"] * 45) + ")"

    inserted = dupes = 0
    with get_conn() as conn:
        if replace:
            conn.execute("DELETE FROM cams_sip")
            conn.executemany(insert_sql, rows)
            inserted = len(rows)
        else:
            before = _count_before_after(conn, "cams_sip")
            conn.executemany(insert_sql, rows)
            inserted, dupes = _inserted_dupes(before, conn, "cams_sip", len(rows))

    preview = {"rows": inserted, "skipped": skipped, "duplicates": dupes}
    msg = f"Imported {inserted} SIPs"
    if skipped: msg += f" | Skipped {skipped}"
    if dupes:   msg += f" | {dupes} duplicates"
    return True, msg, preview


def parse_cams_aum(file, replace: bool) -> tuple[bool, str, dict]:
    """CAMS AUM report (WBR4) → cams_aum (all raw columns)."""
    df = _read_csv_auto(file)
    if df is None:
        return False, "Could not parse AUM file", {}
    df = _clean_cols(df)

    folio_col = next((c for c in ["FOLIOCHK", "FOLIO_NO"] if c in df.columns), None)
    if not folio_col or "RUPEE_BAL" not in df.columns:
        return False, f"Missing FOLIOCHK or RUPEE_BAL. Found: {list(df.columns)[:15]}", {}

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
                clean_str(row.get("ADDRESS1", "")),
                clean_str(row.get("ADDRESS2", "")),
                clean_str(row.get("ADDRESS3", "")),
                clean_str(row.get("CITY", "")),
                clean_str(row.get("PINCODE", "")),
                clean_str(row.get("PRODUCT", "")),
                clean_str(row.get("SCH_NAME", "")),
                parse_date_safe(row.get("REP_DATE", "")),
                _sf(row.get("CLOS_BAL", 0)),
                _sf(row.get("RUPEE_BAL", 0)),
                clean_str(row.get("SUBBROK", "")),
                clean_str(row.get("REINV_FLAG", "")),
                clean_str(row.get("JOINT1_NAME", "")),
                clean_str(row.get("JOINT2_NAME", "")),
                clean_str(row.get("PHONE_OFF", "")),
                clean_str(row.get("PHONE_RES", "")),
                clean_str(row.get("EMAIL", "")).lower(),
                clean_str(row.get("HOLDING_NATURE", "")),
                clean_str(row.get("UIN_NO", "")),
                clean_str(row.get("BROKER_CODE", "")),
                clean_str(row.get("PAN_NO", "")).upper(),
                clean_str(row.get("JOINT1_PAN", "")).upper(),
                clean_str(row.get("JOINT2_PAN", "")).upper(),
                clean_str(row.get("GUARD_PAN", "")).upper(),
                clean_str(row.get("TAX_STATUS", "")),
                clean_str(row.get("INV_IIN", "")),
                clean_str(row.get("ALTFOLIO", "")),
                clean_str(row.get("EUIN", "")),
                clean_str(row.get("EXCHANGE_FLAG", "")),
                clean_str(row.get("TPA_LINKED", "")),
                clean_str(row.get("FH_CKYC_NO", "")),
                clean_str(row.get("JH1_CKYC", "")),
                clean_str(row.get("JH2_CKYC", "")),
                clean_str(row.get("G_CKYC_NO", "")),
                parse_date_safe(row.get("JH1_DOB", "")),
                parse_date_safe(row.get("JH2_DOB", "")),
                parse_date_safe(row.get("GUARDIAN_DOB", "")),
                clean_str(row.get("AMC_CODE", "")),
                clean_str(row.get("GST_STATE_CODE", "")),
                clean_str(row.get("FOLIO_OLD", "")),
                clean_str(row.get("SCHEME_FOLIO_NUMBER", "")),
                clean_str(row.get("COUNTRY", "")),
                batch,
            ))
        except Exception as exc:
            log.warning("Skipped CAMS AUM row: %s", exc)
            skipped += 1

    if not rows:
        return False, "0 AUM rows parsed", {}

    insert_sql = """INSERT OR IGNORE INTO cams_aum
        (foliochk, inv_name, address1, address2, address3, city, pincode, product, sch_name,
         rep_date, clos_bal, rupee_bal, subbrok, reinv_flag, joint1_name, joint2_name,
         phone_off, phone_res, email, holding_nature, uin_no, broker_code, pan_no,
         joint1_pan, joint2_pan, guard_pan, tax_status, inv_iin, altfolio, euin,
         exchange_flag, tpa_linked, fh_ckyc_no, jh1_ckyc, jh2_ckyc, g_ckyc_no, jh1_dob,
         jh2_dob, guardian_dob, amc_code, gst_state_code, folio_old, scheme_folio_number,
         country, upload_batch)
        VALUES (""" + ",".join(["?"] * 45) + ")"

    inserted = dupes = 0
    with get_conn() as conn:
        if replace:
            conn.execute("DELETE FROM cams_aum")
            conn.executemany(insert_sql, rows)
            inserted = len(rows)
        else:
            before = _count_before_after(conn, "cams_aum")
            conn.executemany(insert_sql, rows)
            inserted, dupes = _inserted_dupes(before, conn, "cams_aum", len(rows))

    total_aum = sum(r[11] for r in rows)  # rupee_bal index
    preview = {"rows": inserted, "skipped": skipped, "duplicates": dupes, "total_aum": total_aum}
    msg = f"Imported {inserted} AUM rows | {format_aum(total_aum)}"
    if skipped: msg += f" | Skipped {skipped}"
    if dupes:   msg += f" | {dupes} duplicates"
    return True, msg, preview


def parse_cams_brokerage(file, replace: bool) -> tuple[bool, str, dict]:
    """WBR77 → cams_brokerage (ALL raw columns)."""
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
                # Source header has the typo BROKERAGE_ACRUAL_MONTH (no 'C') — this
                # maps to init.py's brokerage_acrual_month column (same typo, kept
                # intentionally so header and column match exactly).
                clean_str(row.get("BROKERAGE_ACRUAL_MONTH", "")),
                clean_str(row.get("PREV_TRXN_NO", "")),
                parse_date_safe(row.get("PREV_TRXN_DATE", "")),
                batch,
            ))
        except Exception as exc:
            log.warning("Skipped WBR77 row: %s", exc)
            skipped += 1

    if not rows:
        return False, "0 rows parsed", {}

    sql = """INSERT OR IGNORE INTO cams_brokerage
        (amc_code, proc_date, folio_no, scheme_code, trxn_type, trxn_no, plot_amount,
         plot_units, post_date, trade_date_time, entry_date, user_code, user_trxnno,
         trxn_nature, ter_location, sys_reg_date, aut_txn_no, auto_amount, aut_txn_type,
         cease_date, remed_date, forf_date, src_brk_code, brok_code, brh_code, sub_brk_arn,
         ae_code, arn_emp_code, euin_opted, euin_valid, brk_comm_paid, adj_flag,
         brkage_type, brkage_rate, total_upfront, defer_frequency, defer_no_of_installment,
         pay_installment_no, brkage_amt, brkage_from, brkage_to, proc_from_date,
         proc_to_date, trxn_desc, spl_upf_tenure, upf_tenure_end_date, brk_pay_dt,
         clw_type, clw_period, rec_flag, p_si_date, rec_period, clw_amt, upf_paid, fee_id,
         am_code, am_comm, am_rate, avg_assets, cam_comm, cam_rate, mam_comm, mam_rate,
         no_of_days, orig_ae_code, orig_brh_code, orig_brk_code, rate_ref_id, ref_no,
         trxn_app_no, txn_sch_code, clw_prd, clw_required, p_si_mis_code,
         p_si_user_trxnno, seq_no, p_si_amt, p_si_tr_no, p_si_type, pur_si_units, remarks,
         to_scheme, trxn_sign, brk_posted, inv_name, brok_gst_state_code, igst_rate,
         cgst_rate, sgst_rate, igst_value, cgst_value, sgst_value, location_code,
         prev_folio, brok_category, p_scheme_code, p_trxn_type, p_trxn_no, p_folio_no,
         p_plot_amount, p_plot_units, folio_old, scheme_folio_number, amc_ref_no,
         request_ref_no, write_off_reason, hold_reason, brokerage_acrual_month,
         prev_trxn_no, prev_trxn_date, upload_batch)
        VALUES (""" + ",".join(["?"] * 111) + ")"

    inserted = dupes = 0
    with get_conn() as conn:
        if replace:
            conn.execute("DELETE FROM cams_brokerage")
            conn.executemany(sql, rows)
            inserted = len(rows)
        else:
            before = _count_before_after(conn, "cams_brokerage")
            conn.executemany(sql, rows)
            inserted, dupes = _inserted_dupes(before, conn, "cams_brokerage", len(rows))

    total = sum(r[38] for r in rows)  # brkage_amt index
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

    folio_col = _kf_col(df, "FOLIO", "FOLIO NUMBER", "ACCOUNT NUMBER")
    name_col = _kf_col(df, "INVESTOR NAME")
    if not folio_col or not name_col:
        return False, f"Missing Folio/Investor Name. Found: {list(df.columns)[:20]}", {}

    prod_col = _kf_col(df, "PRODUCT CODE")
    fund_col = _kf_col(df, "FUND")
    divopt_col = _kf_col(df, "DIVIDEND OPTION")
    funddesc_col = _kf_col(df, "FUND DESCRIPTION")
    jnt1_col = _kf_col(df, "JOINT NAME 1")
    jnt2_col = _kf_col(df, "JOINT NAME 2")
    addr1_col = _kf_col(df, "ADDRESS #1")
    addr2_col = _kf_col(df, "ADDRESS #2")
    addr3_col = _kf_col(df, "ADDRESS #3")
    city_col = _kf_col(df, "CITY")
    pin_col = _kf_col(df, "PINCODE")
    state_col = _kf_col(df, "STATE")
    country_col = _kf_col(df, "COUNTRY")
    tpin_col = _kf_col(df, "TPIN")
    dob_col = _kf_col(df, "DATE OF BIRTH")
    fname_col = _kf_col(df, "F NAME")
    mname_col = _kf_col(df, "M NAME")
    phr_col = _kf_col(df, "PHONE RESIDENCE")
    phr1_col = _kf_col(df, "PHONE RES#1")
    phr2_col = _kf_col(df, "PHONE RES#2")
    pho_col = _kf_col(df, "PHONE OFFICE")
    pho1_col = _kf_col(df, "PHONE OFF#1")
    pho2_col = _kf_col(df, "PHONE OFF#2")
    faxr_col = _kf_col(df, "FAX RESIDENCE")
    faxo_col = _kf_col(df, "FAX OFFICE")
    tax_col = _kf_col(df, "TAX STATUS")
    occ_col = _kf_col(df, "OCC CODE")
    email_col = _kf_col(df, "EMAIL")
    bankacc_col = _kf_col(df, "BANKACCNO")
    bankname_col = _kf_col(df, "BANK NAME")
    actype_col = _kf_col(df, "ACCOUNT TYPE")
    branch_col = _kf_col(df, "BRANCH")
    bankaddr1_col = _kf_col(df, "BANK ADDRESS #1")
    bankaddr2_col = _kf_col(df, "BANK ADDRESS #2")
    bankaddr3_col = _kf_col(df, "BANK ADDRESS #3")
    bankcity_col = _kf_col(df, "BANK CITY")
    bankphone_col = _kf_col(df, "BANK PHONE")
    bankstate_col = _kf_col(df, "BANK STATE")
    bankcountry_col = _kf_col(df, "BANK COUNTRY")
    invid_col = _kf_col(df, "INVESTOR ID")
    brok_col = _kf_col(df, "BROKER CODE")
    pan_col = _kf_col(df, "PAN NUMBER")
    mob_col = _kf_col(df, "MOBILE NUMBER")
    rep_col = _kf_col(df, "REPORT DATE")
    reptime_col = _kf_col(df, "REPORT TIME")
    occd_col = _kf_col(df, "OCCUPATION DESCRIPTION")
    moh_col = _kf_col(df, "MODE OF HOLDING")
    mohd_col = _kf_col(df, "MODE OF HOLDING DESCRIPTION")
    mapin_col = _kf_col(df, "MAPIN ID")
    h1aad_col = _kf_col(df, "HOLDER 1 AADHAAR INFO")
    h2aad_col = _kf_col(df, "HOLDER 2 AADHAAR INFO")
    h3aad_col = _kf_col(df, "HOLDER 3 AADHAAR INFO")
    gaad_col = _kf_col(df, "GUARDIAN AADHAAR INFO")

    batch = _batch_id("KFIN_R9", file.name)
    rows, skipped = [], 0
    for _, row in df.iterrows():
        folio = clean_str(row.get(folio_col, ""))
        if not folio:
            skipped += 1
            continue
        try:
            rows.append((
                clean_str(row.get(prod_col or "", "")),
                clean_str(row.get(fund_col or "", "")),
                folio,
                clean_str(row.get(divopt_col or "", "")),
                clean_str(row.get(funddesc_col or "", "")),
                clean_str(row.get(name_col, "")).strip(),
                clean_str(row.get(jnt1_col or "", "")),
                clean_str(row.get(jnt2_col or "", "")),
                clean_str(row.get(addr1_col or "", "")),
                clean_str(row.get(addr2_col or "", "")),
                clean_str(row.get(addr3_col or "", "")),
                clean_str(row.get(city_col or "", "")),
                clean_str(row.get(pin_col or "", "")),
                clean_str(row.get(state_col or "", "")),
                clean_str(row.get(country_col or "", "")),
                clean_str(row.get(tpin_col or "", "")),
                parse_date_safe(row.get(dob_col or "", "")),
                clean_str(row.get(fname_col or "", "")),
                clean_str(row.get(mname_col or "", "")),
                clean_str(row.get(phr_col or "", "")),
                clean_str(row.get(phr1_col or "", "")),
                clean_str(row.get(phr2_col or "", "")),
                clean_str(row.get(pho_col or "", "")),
                clean_str(row.get(pho1_col or "", "")),
                clean_str(row.get(pho2_col or "", "")),
                clean_str(row.get(faxr_col or "", "")),
                clean_str(row.get(faxo_col or "", "")),
                clean_str(row.get(tax_col or "", "")),
                clean_str(row.get(occ_col or "", "")),
                clean_str(row.get(email_col or "", "")).lower(),
                clean_str(row.get(bankacc_col or "", "")),
                clean_str(row.get(bankname_col or "", "")),
                clean_str(row.get(actype_col or "", "")),
                clean_str(row.get(branch_col or "", "")),
                clean_str(row.get(bankaddr1_col or "", "")),
                clean_str(row.get(bankaddr2_col or "", "")),
                clean_str(row.get(bankaddr3_col or "", "")),
                clean_str(row.get(bankcity_col or "", "")),
                clean_str(row.get(bankphone_col or "", "")),
                clean_str(row.get(bankstate_col or "", "")),
                clean_str(row.get(bankcountry_col or "", "")),
                clean_str(row.get(invid_col or "", "")),
                clean_str(row.get(brok_col or "", "")),
                clean_str(row.get(pan_col or "", "")).upper(),
                clean_str(row.get(mob_col or "", "")),
                parse_date_safe(row.get(rep_col or "", "")),
                clean_str(row.get(reptime_col or "", "")),
                clean_str(row.get(occd_col or "", "")),
                clean_str(row.get(moh_col or "", "")),
                clean_str(row.get(mohd_col or "", "")),
                clean_str(row.get(mapin_col or "", "")),
                clean_str(row.get(h1aad_col or "", "")),
                clean_str(row.get(h2aad_col or "", "")),
                clean_str(row.get(h3aad_col or "", "")),
                clean_str(row.get(gaad_col or "", "")),
                batch,
            ))
        except Exception as exc:
            log.warning("Skipped KFin folio row: %s", exc)
            skipped += 1

    if not rows:
        return False, "0 folio rows parsed", {}

    insert_sql = """INSERT OR IGNORE INTO kfin_folio
        (product_code, fund, folio, dividend_option, fund_description, investor_name,
         joint_name_1, joint_name_2, address1, address2, address3, city, pincode, state,
         country, tpin, date_of_birth, f_name, m_name, phone_residence, phone_res_1,
         phone_res_2, phone_office, phone_off_1, phone_off_2, fax_residence, fax_office,
         tax_status, occ_code, email, bankaccno, bank_name, account_type, branch,
         bank_address1, bank_address2, bank_address3, bank_city, bank_phone, bank_state,
         bank_country, investor_id, broker_code, pan_number, mobile_number, report_date,
         report_time, occupation_description, mode_of_holding, mode_of_holding_description,
         mapin_id, holder_1_aadhaar_info, holder_2_aadhaar_info, holder_3_aadhaar_info,
         guardian_aadhaar_info, upload_batch)
        VALUES (""" + ",".join(["?"] * 56) + ")"

    inserted = dupes = 0
    with get_conn() as conn:
        if replace:
            conn.execute("DELETE FROM kfin_folio")
            conn.executemany(insert_sql, rows)
            inserted = len(rows)
        else:
            before = _count_before_after(conn, "kfin_folio")
            conn.executemany(insert_sql, rows)
            inserted, dupes = _inserted_dupes(before, conn, "kfin_folio", len(rows))

    preview = {"rows": inserted, "skipped": skipped, "duplicates": dupes,
               "unique_folios": len({r[2] for r in rows}),
               "unique_investors": len({r[5] for r in rows})}
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
                clean_str(row.get("SMCODE", "")),
                clean_str(row.get("CHQNO", "")),
                clean_str(row.get("INVNAME", "")).strip(),
                clean_str(row.get("TRNMODE", "")),
                clean_str(row.get("TRNSTAT", "")),
                clean_str(row.get("TD_BRANCH", "")),
                clean_str(row.get("ISCTRNO", "")),
                parse_date_safe(row.get("TD_TRDT", "")),
                parse_date_safe(row.get("TD_PRDT", "")),
                clean_str(row.get("TD_POP", "")),
                _sf(row.get("LOADPER", 0)),
                _sf(row.get("TD_UNITS", 0)),
                _sf(row.get("TD_AMT", 0)),
                _sf(row.get("LOAD1", 0)),
                clean_str(row.get("TD_AGENT", "")),
                clean_str(row.get("TD_BROKER", "")),
                _sf(row.get("BROKPER", 0)),
                _sf(row.get("BROKCOMM", 0)),
                clean_str(row.get("INVID", "")),
                parse_date_safe(row.get("CRDATE", "")),
                clean_str(row.get("CRTIME", "")),
                clean_str(row.get("TRNSUB", "")),
                clean_str(row.get("TD_APPNO", "")),
                clean_str(row.get("UNQNO", "")),
                clean_str(row.get("TRDESC", "")),
                clean_str(row.get("TD_TRTYPE", "")),
                parse_date_safe(row.get("PURDATE", "")),
                _sf(row.get("PURAMT", 0)),
                _sf(row.get("PURUNITS", 0)),
                clean_str(row.get("TRFLAG", "")),
                parse_date_safe(row.get("SFUNDDT", "")),
                parse_date_safe(row.get("CHQDATE", "")),
                clean_str(row.get("CHQBANK", "")),
                _sf(row.get("TD_NAV", 0)),
                clean_str(row.get("TD_PTRNO", "")),
                _sf(row.get("STT", 0)),
                clean_str(row.get("IHNO", "")),
                clean_str(row.get("BRANCHCODE", "")),
                clean_str(row.get("INWARDNO", "")),
                clean_str(row.get("NCTREMARKS", "")),
                clean_str(row.get("PAN1", "")).upper(),
                _sf(row.get("TRCHARGES", 0)),
                parse_date_safe(row.get("SIPREGDT", "")),
                clean_str(row.get("SIPREGSLNO", "")),
                _sf(row.get("DIVPER", 0)),
                clean_str(row.get("GUARDPANNO", "")).upper(),
                clean_str(row.get("CAN", "")),
                clean_str(row.get("EXCHORGTRTYPE", "")),
                clean_str(row.get("ELECRFLAG", "")),
                clean_str(row.get("CLEARED", "")),
                clean_str(row.get("INVSTATE", "")),
                batch,
            ))
        except Exception as exc:
            log.warning("Skipped KFin txn row: %s", exc)
            skipped += 1

    if not rows:
        return False, "0 transaction rows parsed", {}

    insert_sql = """INSERT OR IGNORE INTO kfin_transactions
        (fmcode, td_fund, td_acno, schpln, divopt, funddesc, td_purred, td_trno, smcode,
         chqno, invname, trnmode, trnstat, td_branch, isctrno, td_trdt, td_prdt, td_pop,
         loadper, td_units, td_amt, load1, td_agent, td_broker, brokper, brokcomm, invid,
         crdate, crtime, trnsub, td_appno, unqno, trdesc, td_trtype, purdate, puramt,
         purunits, trflag, sfunddt, chqdate, chqbank, td_nav, td_ptrno, stt, ihno,
         branchcode, inwardno, nctremarks, pan1, trcharges, sipregdt, sipregslno, divper,
         guardpanno, can, exchorgtrtype, elecrtrxnflag, cleared, invstate, upload_batch)
        VALUES (""" + ",".join(["?"] * 60) + ")"

    inserted = dupes = 0
    with get_conn() as conn:
        if replace:
            conn.execute("DELETE FROM kfin_transactions")
            conn.executemany(insert_sql, rows)
            inserted = len(rows)
        else:
            before = _count_before_after(conn, "kfin_transactions")
            conn.executemany(insert_sql, rows)
            inserted, dupes = _inserted_dupes(before, conn, "kfin_transactions", len(rows))

    total_amt = sum(r[20] for r in rows)  # td_amt index
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

    folio_col = _kf_col(df, "FOLIO")
    regsl_col = _kf_col(df, "REGSLNO")
    amt_col = _kf_col(df, "AMOUNT")
    if not folio_col or not regsl_col:
        return False, f"Missing FOLIO/REGSLNO. Found: {list(df.columns)[:20]}", {}

    zone_col = _kf_col(df, "ZONE")
    branch_col = _kf_col(df, "BRANCH")
    loc_col = _kf_col(df, "LOCATION")
    ihno_col = _kf_col(df, "IHNO")
    name_col = _kf_col(df, "INVESTOR NAME")
    regdate_col = _kf_col(df, "REGISTRATIONDATE")
    start_col = _kf_col(df, "START DATE")
    end_col = _kf_col(df, "END DATE")
    inst_col = _kf_col(df, "NO OF INSTALLMENTS")
    scheme_col = _kf_col(df, "SCHEME")
    plan_col = _kf_col(df, "PLAN")
    agentcode_col = _kf_col(df, "AGENTCODE")
    agentname_col = _kf_col(df, "AGENTNAME")
    subbroker_col = _kf_col(df, "SUBBROKER")
    schemename_col = _kf_col(df, "SCHEME NAME")
    pan_col = _kf_col(df, "PAN")
    siptype_col = _kf_col(df, "SIPTYPE")
    sipmode_col = _kf_col(df, "SIP MODE")
    fundcode_col = _kf_col(df, "FUND CODE")
    prodcode_col = _kf_col(df, "PRODUCT CODE")
    freq_col = _kf_col(df, "FREQUENCY")
    trtype_col = _kf_col(df, "TRTYPE")
    toscheme_col = _kf_col(df, "TO SCHEME")
    toplan_col = _kf_col(df, "TO PLAN")
    termdate_col = _kf_col(df, "TERMINATEDATE")
    status_col = _kf_col(df, "STATUS")
    toprodcode_col = _kf_col(df, "TOPRODUCTCODE")
    toschemename_col = _kf_col(df, "TOSCHEMENAME")
    ecsno_col = _kf_col(df, "ECSNO")
    ecsbank_col = _kf_col(df, "ECSBANKNAME")
    ecsacno_col = _kf_col(df, "ECSACNO")
    ecsholder_col = _kf_col(df, "ECSHOLDERNAME")
    invdpid_col = _kf_col(df, "INVDPID")
    invclientid_col = _kf_col(df, "INVCLIENTID")
    dpinvname_col = _kf_col(df, "DP_INVNAME")
    modifyflag_col = _kf_col(df, "MODIFYFLAG")
    umrn_col = _kf_col(df, "UMRNCODE")

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
                clean_str(row.get(zone_col or "", "")),
                clean_str(row.get(branch_col or "", "")),
                clean_str(row.get(loc_col or "", "")),
                clean_str(row.get(ihno_col or "", "")),
                folio,
                clean_str(row.get(name_col or "", "")).strip(),
                parse_date_safe(row.get(regdate_col or "", "")),
                parse_date_safe(row.get(start_col or "", "")),
                parse_date_safe(row.get(end_col or "", "")),
                _si(row.get(inst_col or "", 0)),
                _sf(row.get(amt_col or "", 0)),
                clean_str(row.get(scheme_col or "", "")),
                clean_str(row.get(plan_col or "", "")),
                clean_str(row.get(agentcode_col or "", "")),
                clean_str(row.get(agentname_col or "", "")),
                clean_str(row.get(subbroker_col or "", "")),
                clean_str(row.get(schemename_col or "", "")),
                clean_str(row.get(pan_col or "", "")).upper(),
                clean_str(row.get(siptype_col or "", "")),
                clean_str(row.get(sipmode_col or "", "")),
                clean_str(row.get(fundcode_col or "", "")),
                clean_str(row.get(prodcode_col or "", "")),
                clean_str(row.get(freq_col or "", "")),
                clean_str(row.get(trtype_col or "", "")),
                clean_str(row.get(toscheme_col or "", "")),
                clean_str(row.get(toplan_col or "", "")),
                parse_date_safe(row.get(termdate_col or "", "")),
                clean_str(row.get(status_col or "", "Active")),
                clean_str(row.get(toprodcode_col or "", "")),
                clean_str(row.get(toschemename_col or "", "")),
                clean_str(row.get(ecsno_col or "", "")),
                clean_str(row.get(ecsbank_col or "", "")),
                clean_str(row.get(ecsacno_col or "", "")),
                clean_str(row.get(ecsholder_col or "", "")),
                regsl,
                clean_str(row.get(invdpid_col or "", "")),
                clean_str(row.get(invclientid_col or "", "")),
                clean_str(row.get(dpinvname_col or "", "")),
                clean_str(row.get(modifyflag_col or "", "")),
                clean_str(row.get(umrn_col or "", "")),
                batch,
            ))
        except Exception as exc:
            log.warning("Skipped KFin SIP row: %s", exc)
            skipped += 1

    if not rows:
        return False, "0 SIP rows parsed", {}

    insert_sql = """INSERT OR IGNORE INTO kfin_sip
        (zone, branch, location, ihno, folio, investor_name, registration_date, start_date,
         end_date, no_of_installments, amount, scheme, plan, agent_code, agent_name,
         subbroker, scheme_name, pan, sip_type, sip_mode, fund_code, product_code,
         frequency, trtype, to_scheme, to_plan, terminate_date, status, to_product_code,
         to_scheme_name, ecsno, ecsbankname, ecsacno, ecsholdername, regslno, invdpid,
         invclientid, dp_invname, modifyflag, umrncode, upload_batch)
        VALUES (""" + ",".join(["?"] * 41) + ")"

    inserted = dupes = 0
    with get_conn() as conn:
        if replace:
            conn.execute("DELETE FROM kfin_sip")
            conn.executemany(insert_sql, rows)
            inserted = len(rows)
        else:
            before = _count_before_after(conn, "kfin_sip")
            conn.executemany(insert_sql, rows)
            inserted, dupes = _inserted_dupes(before, conn, "kfin_sip", len(rows))

    active = sum(1 for r in rows if "active" in str(r[27]).lower())  # status index
    ceased = sum(1 for r in rows if "cease" in str(r[27]).lower())
    completed = sum(1 for r in rows if "complet" in str(r[27]).lower())
    preview = {"rows": inserted, "skipped": skipped, "duplicates": dupes,
               "active": active, "ceased": ceased, "completed": completed}
    msg = f"Imported {inserted} KFin SIPs | Active: {active} | Ceased: {ceased} | Completed: {completed}"
    if skipped: msg += f" | Skipped {skipped}"
    if dupes:   msg += f" | {dupes} duplicates"
    return True, msg, preview


def parse_kfin_aum(file, replace: bool) -> tuple[bool, str, dict]:
    """KFin AUM report (MFSD203) → kfin_aum (all raw columns)."""
    df = _read_csv_auto(file)
    if df is None:
        return False, "Could not parse file", {}
    df = _clean_cols(df)

    folio_col = _kf_col(df, "FOLIO NUMBER", "FOLIO", "ACCOUNT NUMBER")
    aum_col = _kf_col(df, "AUM", "CURRENT VALUE", "MARKET VALUE", "VALUE")
    if not folio_col or not aum_col:
        return False, f"Missing Folio Number/AUM column. Found: {list(df.columns)[:20]}", {}

    prod_col = _kf_col(df, "PRODUCT CODE")
    fund_col = _kf_col(df, "FUND")
    schcode_col = _kf_col(df, "SCHEME CODE")
    divopt_col = _kf_col(df, "DIVIDEND OPTION")
    funddesc_col = _kf_col(df, "FUND DESCRIPTION")
    bal_col = _kf_col(df, "BALANCE")
    pledged_col = _kf_col(df, "PLEDGED")
    txdate_col = _kf_col(df, "TRANSACTION DATE")
    txtype_col = _kf_col(df, "TRANSACTION TYPE")
    holdmode_col = _kf_col(df, "HOLD MODE")
    agentcode_col = _kf_col(df, "AGENT CODE")
    brokcode_col = _kf_col(df, "BROKER CODE")
    poutcode_col = _kf_col(df, "P-OUT CODE")
    invid_col = _kf_col(df, "INVESTOR ID")
    invname_col = _kf_col(df, "INVESTOR NAME")
    addr1_col = _kf_col(df, "ADDRESS #1")
    addr2_col = _kf_col(df, "ADDRESS #2")
    addr3_col = _kf_col(df, "ADDRESS #3")
    city_col = _kf_col(df, "CITY")
    pin_col = _kf_col(df, "PINCODE")
    phr_col = _kf_col(df, "PHONE RESIDENCE")
    pho_col = _kf_col(df, "PHONE OFFICE")
    fax_col = _kf_col(df, "FAX")
    email_col = _kf_col(df, "EMAIL")
    nav_col = _kf_col(df, "NAV")
    repdate_col = _kf_col(df, "REPORT DATE")
    reptime_col = _kf_col(df, "REPORT TIME")
    todate_col = _kf_col(df, "TO DATE")

    batch = _batch_id("KFIN_AUM", file.name)
    rows, skipped = [], 0
    for _, row in df.iterrows():
        folio = clean_str(row.get(folio_col, ""))
        if not folio:
            skipped += 1
            continue
        try:
            rows.append((
                clean_str(row.get(prod_col or "", "")),
                clean_str(row.get(fund_col or "", "")),
                folio,
                clean_str(row.get(schcode_col or "", "")),
                clean_str(row.get(divopt_col or "", "")),
                clean_str(row.get(funddesc_col or "", "")),
                _sf(row.get(bal_col or "", 0)),
                clean_str(row.get(pledged_col or "", "")),
                parse_date_safe(row.get(txdate_col or "", "")),
                clean_str(row.get(txtype_col or "", "")),
                clean_str(row.get(holdmode_col or "", "")),
                clean_str(row.get(agentcode_col or "", "")),
                clean_str(row.get(brokcode_col or "", "")),
                clean_str(row.get(poutcode_col or "", "")),
                clean_str(row.get(invid_col or "", "")),
                clean_str(row.get(invname_col or "", "")).strip(),
                clean_str(row.get(addr1_col or "", "")),
                clean_str(row.get(addr2_col or "", "")),
                clean_str(row.get(addr3_col or "", "")),
                clean_str(row.get(city_col or "", "")),
                clean_str(row.get(pin_col or "", "")),
                clean_str(row.get(phr_col or "", "")),
                clean_str(row.get(pho_col or "", "")),
                clean_str(row.get(fax_col or "", "")),
                clean_str(row.get(email_col or "", "")).lower(),
                _sf(row.get(aum_col, 0)),
                _sf(row.get(nav_col or "", 0)),
                parse_date_safe(row.get(repdate_col or "", "")),
                clean_str(row.get(reptime_col or "", "")),
                parse_date_safe(row.get(todate_col or "", "")),
                batch,
            ))
        except Exception as exc:
            log.warning("Skipped KFin AUM row: %s", exc)
            skipped += 1

    if not rows:
        return False, "0 AUM rows parsed", {}

    insert_sql = """INSERT OR IGNORE INTO kfin_aum
        (product_code, fund, folio_number, scheme_code, dividend_option, fund_description,
         balance, pledged, transaction_date, transaction_type, hold_mode, agent_code,
         broker_code, p_out_code, investor_id, investor_name, address1, address2, address3,
         city, pincode, phone_residence, phone_office, fax, email, aum, nav, report_date,
         report_time, to_date, upload_batch)
        VALUES (""" + ",".join(["?"] * 31) + ")"

    inserted = dupes = 0
    with get_conn() as conn:
        if replace:
            conn.execute("DELETE FROM kfin_aum")
            conn.executemany(insert_sql, rows)
            inserted = len(rows)
        else:
            before = _count_before_after(conn, "kfin_aum")
            conn.executemany(insert_sql, rows)
            inserted, dupes = _inserted_dupes(before, conn, "kfin_aum", len(rows))

    total_aum = sum(r[25] for r in rows)  # aum index
    preview = {"rows": inserted, "skipped": skipped, "duplicates": dupes, "total_aum": total_aum}
    msg = f"Imported {inserted} KFin AUM rows | {format_aum(total_aum)}"
    if skipped: msg += f" | Skipped {skipped}"
    if dupes:   msg += f" | {dupes} duplicates"
    return True, msg, preview


def parse_kfin_brokerage(file, replace: bool) -> tuple[bool, str, dict]:
    """MFSD205 → kfin_brokerage (ALL raw columns)."""
    df = _read_csv_auto(file)
    if df is None:
        return False, "Could not parse file", {}
    df = _clean_cols(df)

    acno_col = _kf_col(df, "ACCOUNT NUMBER")
    brok_col = _kf_col(df, "BROKERAGE (IN RS.)", "BROKERAGE")
    if not acno_col or not brok_col:
        return False, f"Missing Account Number/Brokerage. Found: {list(df.columns)[:20]}", {}

    prod_col = _kf_col(df, "PRODUCT CODE")
    funddesc_col = _kf_col(df, "FUND DESCRIPTION")
    fund_col = _kf_col(df, "FUND")
    scheme_col = _kf_col(df, "SCHEME")
    plan_col = _kf_col(df, "PLAN")
    option_col = _kf_col(df, "OPTION")
    appno_col = _kf_col(df, "APPLICATION NUMBER")
    invname_col = _kf_col(df, "INVESTOR NAME")
    addr1_col = _kf_col(df, "ADDRESS #1")
    addr2_col = _kf_col(df, "ADDRESS #2")
    addr3_col = _kf_col(df, "ADDRESS #3")
    city_col = _kf_col(df, "CITY")
    pin_col = _kf_col(df, "PINCODE")
    txdesc_col = _kf_col(df, "TRANSACTION DESCRIPTION")
    from_col = _kf_col(df, "FROM DATE")
    to_col = _kf_col(df, "TO DATE")
    amt_col = _kf_col(df, "AMOUNT (IN RS.)", "AMOUNT")
    units_col = _kf_col(df, "UNITS")
    txdate_col = _kf_col(df, "TRANSACTION DATE")
    procdate_col = _kf_col(df, "PROCESS DATE")
    pct_col = _kf_col(df, "PERCENTAGE (%)", "PERCENTAGE")
    subbrok_col = _kf_col(df, "SUB-BROKER")
    actype_col = _kf_col(df, "ACCOUNT TYPE")
    brokhead_col = _kf_col(df, "BROKERAGE HEAD")
    broktype_col = _kf_col(df, "BROKERAGE TYPE")
    txno_col = _kf_col(df, "TRANSACTION NUMBER")
    branchcode_col = _kf_col(df, "BRANCH CODE")
    chqno_col = _kf_col(df, "CHEQUE NUMBER")
    startdate_col = _kf_col(df, "STARTING DATE")
    enddate_col = _kf_col(df, "ENDING DATE")
    warrantno_col = _kf_col(df, "WARRANT NUMBER")
    warrantdate_col = _kf_col(df, "WARRANT DATE")
    dailyprod_col = _kf_col(df, "DAILY PRODUCT")
    cumnav_col = _kf_col(df, "CUMULATIVE NAV")
    avgassets_col = _kf_col(df, "AVERAGE ASSETS")
    txid_col = _kf_col(df, "TRANSACTION ID")
    schemecode_col = _kf_col(df, "SCHEME CODE")
    txhead_col = _kf_col(df, "TRANSACTION HEAD")
    feetype_col = _kf_col(df, "FEE TYPE")
    adjflag_col = _kf_col(df, "ADJUSTMENT FLAG")
    switchflag_col = _kf_col(df, "SWITCH FLAG")
    grossbrok_col = _kf_col(df, "GROSSBROKERAGE")
    stt_col = _kf_col(df, "STTAMOUNT")
    educess_col = _kf_col(df, "EDUCESSAMOUNT")
    trantype_col = _kf_col(df, "TRANTYPECODE")

    batch = _batch_id("KFIN_MFSD205", file.name)
    rows, skipped = [], 0
    for _, row in df.iterrows():
        try:
            rows.append((
                clean_str(row.get(prod_col or "", "")),
                clean_str(row.get(funddesc_col or "", "")),
                clean_str(row.get(fund_col or "", "")),
                clean_str(row.get(scheme_col or "", "")),
                clean_str(row.get(plan_col or "", "")),
                clean_str(row.get(option_col or "", "")),
                clean_str(row.get(acno_col, "")),
                clean_str(row.get(appno_col or "", "")),
                clean_str(row.get(invname_col or "", "")).strip(),
                clean_str(row.get(addr1_col or "", "")),
                clean_str(row.get(addr2_col or "", "")),
                clean_str(row.get(addr3_col or "", "")),
                clean_str(row.get(city_col or "", "")),
                clean_str(row.get(pin_col or "", "")),
                clean_str(row.get(txdesc_col or "", "")),
                parse_date_safe(row.get(from_col or "", "")),
                parse_date_safe(row.get(to_col or "", "")),
                _sf(row.get(amt_col or "", 0)),
                _sf(row.get(units_col or "", 0)),
                parse_date_safe(row.get(txdate_col or "", "")),
                parse_date_safe(row.get(procdate_col or "", "")),
                _sf(row.get(pct_col or "", 0)),
                _sf(row.get(brok_col, 0)),
                clean_str(row.get(subbrok_col or "", "")),
                clean_str(row.get(actype_col or "", "")),
                clean_str(row.get(brokhead_col or "", "")),
                clean_str(row.get(broktype_col or "", "")),
                clean_str(row.get(txno_col or "", "")),
                clean_str(row.get(branchcode_col or "", "")),
                clean_str(row.get(chqno_col or "", "")),
                parse_date_safe(row.get(startdate_col or "", "")),
                parse_date_safe(row.get(enddate_col or "", "")),
                clean_str(row.get(warrantno_col or "", "")),
                parse_date_safe(row.get(warrantdate_col or "", "")),
                _sf(row.get(dailyprod_col or "", 0)),
                _sf(row.get(cumnav_col or "", 0)),
                _sf(row.get(avgassets_col or "", 0)),
                clean_str(row.get(txid_col or "", "")),
                clean_str(row.get(schemecode_col or "", "")),
                clean_str(row.get(txhead_col or "", "")),
                clean_str(row.get(feetype_col or "", "")),
                clean_str(row.get(adjflag_col or "", "")),
                clean_str(row.get(switchflag_col or "", "")),
                _sf(row.get(grossbrok_col or "", 0)),
                _sf(row.get(stt_col or "", 0)),
                _sf(row.get(educess_col or "", 0)),
                clean_str(row.get(trantype_col or "", "")),
                batch,
            ))
        except Exception as exc:
            log.warning("Skipped MFSD205 row: %s", exc)
            skipped += 1

    if not rows:
        return False, "0 rows parsed", {}

    sql = """INSERT OR IGNORE INTO kfin_brokerage
        (product_code, fund_description, fund, scheme, plan, option, account_number,
         application_number, investor_name, address1, address2, address3, city, pincode,
         transaction_description, from_date, to_date, amount_rs, units, transaction_date,
         process_date, percentage, brokerage_rs, sub_broker, account_type, brokerage_head,
         brokerage_type, transaction_number, branch_code, cheque_number, starting_date,
         ending_date, warrant_number, warrant_date, daily_product, cumulative_nav,
         average_assets, transaction_id, scheme_code, transaction_head, fee_type,
         adjustment_flag, switch_flag, grossbrokerage, sttamount, educessamount,
         trantypecode, upload_batch)
        VALUES (""" + ",".join(["?"] * 48) + ")"

    inserted = dupes = 0
    with get_conn() as conn:
        if replace:
            conn.execute("DELETE FROM kfin_brokerage")
            conn.executemany(sql, rows)
            inserted = len(rows)
        else:
            before = _count_before_after(conn, "kfin_brokerage")
            conn.executemany(sql, rows)
            inserted, dupes = _inserted_dupes(before, conn, "kfin_brokerage", len(rows))

    total = sum(r[22] for r in rows)  # brokerage_rs index
    months = sorted({str(r[15])[:7] for r in rows if r[15]})  # from_date index
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
            st.caption("Excel file · Maps: clients (single flattened table, incl. bank1-5/nominee1-3)")
            f = st.file_uploader("Client Master (.xlsx)", type=["xlsx"], key="dm_bse_cm")
            rp = st.checkbox("Replace all clients", key="dm_bse_cm_rp")
            if rp:
                st.warning("All clients will be deleted first.", icon="⚠️")
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
                    "SELECT client_code, primary_holder_first_name||' '||primary_holder_last_name AS name, "
                    "primary_holder_pan AS pan, email, mobile, city FROM clients ORDER BY client_code",
                    conn)
            if df_c.empty:
                st.info("No client data yet.")
            else:
                st.dataframe(df_c, use_container_width=True, hide_index=True)
                st.metric("Total Clients", len(df_c))
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
            st.caption("Excel/CSV · All columns stored · Required: scheme_code, rta_scheme_code, isin")
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
            st.caption("Required: FOLIOCHK, RUPEE_BAL")
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
                st.warning("Cleared.")
                st.cache_data.clear()

        elif cams_sec == "💼 Brokerage (WBR77)":
            st.subheader("CAMS Brokerage — WBR77")
            st.caption("Required: AMC_CODE, FOLIO_NO, BRKAGE_AMT · All columns stored")
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
                st.warning("Cleared.")
                st.cache_data.clear()

        elif cams_sec == "📂 Folio (WBR9)":
            st.subheader("CAMS Folio Master — WBR9")
            st.caption("Required: FOLIOCHK, INV_NAME · All columns stored")
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
                    "SELECT amc_code,COUNT(DISTINCT foliochk) folios,COUNT(DISTINCT pan_no) investors,ROUND(SUM(rupee_bal),2) aum FROM cams_folio GROUP BY amc_code ORDER BY aum DESC",
                    conn)
            if s.empty:
                st.info("No CAMS folio data.")
            else:
                s["aum"] = s["aum"].apply(format_aum)
                st.dataframe(s, use_container_width=True, hide_index=True)
            if st.button("⚠️ Clear CAMS Folio", key="dm_cams_folio_del"):
                with get_conn() as conn: conn.execute("DELETE FROM cams_folio")
                st.warning("Cleared.")
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
                    "SELECT traddate,amc_code,COUNT(*) txns,COUNT(DISTINCT folio_no) folios,ROUND(SUM(amount),2) amount FROM cams_transactions GROUP BY traddate,amc_code ORDER BY traddate DESC LIMIT 30",
                    conn)
            if s.empty:
                st.info("No CAMS transaction data.")
            else:
                st.dataframe(s, use_container_width=True, hide_index=True)
            if st.button("⚠️ Clear CAMS Transactions", key="dm_cams_txn_del"):
                with get_conn() as conn: conn.execute("DELETE FROM cams_transactions")
                st.warning("Cleared.")
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
                        _metrics(preview, [("rows", "Inserted"), ("duplicates", "Duplicates"),
                                           ("skipped", "Skipped")])
                    if ok: st.cache_data.clear()
            st.divider()
            with get_conn() as conn:
                s = pd.read_sql(
                    "SELECT amc_code,COUNT(*) sips,COUNT(DISTINCT folio_no) folios,ROUND(SUM(auto_amount),2) amount "
                    "FROM cams_sip GROUP BY amc_code",
                    conn)
            if s.empty:
                st.info("No CAMS SIP data.")
            else:
                st.dataframe(s, use_container_width=True, hide_index=True)
            if st.button("⚠️ Clear CAMS SIP", key="dm_cams_sip_del"):
                with get_conn() as conn: conn.execute("DELETE FROM cams_sip")
                st.warning("Cleared.")
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
            st.caption("Required: Folio Number, AUM column")
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
                    "SELECT report_date,fund,COUNT(*) folios,ROUND(SUM(aum),2) aum FROM kfin_aum GROUP BY "
                    "report_date,fund ORDER BY report_date DESC",
                    conn)
            if s.empty:
                st.info("No KFin AUM data.")
            else:
                s["aum"] = s["aum"].apply(format_aum)
                st.dataframe(s, use_container_width=True, hide_index=True)
            if st.button("⚠️ Clear KFin AUM", key="dm_kf_aum_del"):
                with get_conn() as conn: conn.execute("DELETE FROM kfin_aum")
                st.warning("Cleared.")
                st.cache_data.clear()

        elif kf_sec == "💼 Brokerage (MFSD205)":
            st.subheader("KFin Brokerage — MFSD205")
            st.caption("Required: Account Number, Brokerage (in Rs.) · All columns stored")
            f = st.file_uploader("MFSD205 (CSV/TSV)", type=["csv", "txt", "tsv"], key="dm_kf_brok")
            rp = st.checkbox("Replace KFin Brokerage", key="dm_kf_brok_rp")
            if rp:
                st.warning("Replace mode.", icon="⚠️")
            else:
                st.info("Dedup: transaction_number + account_number", icon="ℹ️")
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
                    "SELECT from_date month,COUNT(*) rows,COUNT(DISTINCT account_number) folios,ROUND(SUM("
                    "brokerage_rs),4) brokerage FROM kfin_brokerage GROUP BY month ORDER BY month DESC",
                    conn)
            if s.empty:
                st.info("No KFin brokerage data.")
            else:
                st.dataframe(s, use_container_width=True, hide_index=True)
            if st.button("⚠️ Clear KFin Brokerage", key="dm_kf_brok_del"):
                with get_conn() as conn: conn.execute("DELETE FROM kfin_brokerage")
                st.warning("Cleared.")
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
                    "SELECT fund,COUNT(DISTINCT folio) folios,COUNT(DISTINCT pan_number) investors FROM kfin_folio GROUP BY fund ORDER BY folios DESC",
                    conn)
            if s.empty:
                st.info("No KFin folio data.")
            else:
                st.dataframe(s, use_container_width=True, hide_index=True)
            if st.button("⚠️ Clear KFin Folio", key="dm_kf_folio_del"):
                with get_conn() as conn: conn.execute("DELETE FROM kfin_folio")
                st.warning("Cleared.")
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
                    "SELECT td_trdt,td_fund,COUNT(*) txns,COUNT(DISTINCT td_acno) folios,ROUND(SUM(td_amt),2) amount FROM kfin_transactions GROUP BY td_trdt,td_fund ORDER BY td_trdt DESC LIMIT 30",
                    conn)
            if s.empty:
                st.info("No KFin transaction data.")
            else:
                st.dataframe(s, use_container_width=True, hide_index=True)
            if st.button("⚠️ Clear KFin Transactions", key="dm_kf_txn_del"):
                with get_conn() as conn: conn.execute("DELETE FROM kfin_transactions")
                st.warning("Cleared.")
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
                    "SELECT fund_code,status,COUNT(*) sips,COUNT(DISTINCT folio) folios,ROUND(SUM(amount),2) amount "
                    "FROM kfin_sip GROUP BY fund_code,status ORDER BY fund_code",
                    conn)
            if s.empty:
                st.info("No KFin SIP data.")
            else:
                st.dataframe(s, use_container_width=True, hide_index=True)
            if st.button("⚠️ Clear KFin SIP", key="dm_kf_sip_del"):
                with get_conn() as conn: conn.execute("DELETE FROM kfin_sip")
                st.warning("Cleared.")
                st.cache_data.clear()
