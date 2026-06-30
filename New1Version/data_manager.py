"""
New Data_manager.py
data_manager.py
All Upload / Parse / Delete logic for BSE, CAMS, and KFinTech data.
UPDATED: Now inserts data EXACTLY as it appears in the source files.
No .upper(), .lower(), or date reformatting is applied.
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

def raw_val(val) -> str:
    """Extract value EXACTLY as it appears in the file. Handles NaN/None safely."""
    if val is None: return ""
    try:
        if pd.isna(val): return ""
    except (TypeError, ValueError):
        pass
    return str(val).strip()


def format_aum(val) -> str:
    """UI Helper to display AUM nicely."""
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
    """UI Helper to display currency nicely."""
    try:
        return f"Rs {float(val):,.{decimals}f}"
    except (TypeError, ValueError):
        return "Rs -"


def format_brokerage(val) -> str:
    """UI Helper to display brokerage precisely."""
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
    """Strip quotes/BOM/ZWS and uppercase all column names for mapping."""
    df.columns = [
        c.strip().replace("\u200b", "").replace("\ufeff", "").replace("\u00a0", "").strip("'\"").upper()
        for c in df.columns
    ]
    return df


def _read_csv_auto(file) -> pd.DataFrame | None:
    for sep in ("\t", ",", ";"):
        try:
            file.seek(0)
            df = pd.read_csv(file, sep=sep, quotechar="'", dtype=str, encoding="utf-8", encoding_errors="replace")
            if len(df.columns) > 5: return df
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
    "unique_no": ["unique no", "uniqueno", "unique_no"], "scheme_code": ["scheme code", "scheme_code"],
    "rta_scheme_code": ["rta scheme code", "rta_scheme_code"],
    "amc_scheme_code": ["amc scheme code", "amc_scheme_code"],
    "isin": ["isin"], "amc_code": ["amc code", "amc_code"], "scheme_type": ["scheme type", "scheme_type"],
    "scheme_plan": ["scheme plan", "scheme_plan"], "scheme_name": ["scheme name", "scheme_name"],
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
    "redemption_amount_min": ["redemption amount - minimum", "redemption amount minimum", "redemption_amount_minimum",
                              "redemption_amount_min"],
    "redemption_amount_max": ["redemption amount maximum", "redemption amount - maximum",
                              "redemption_amount \u2013 maximum", "redemption_amount_maximum", "redemption_amount_max"],
    "redemption_amount_multiple": ["redemption amount multiple", "redemption_amount_multiple"],
    "redemption_cutoff_time": ["redemption cut off time", "redemption cutoff time", "redemption_cutoff_time"],
    "rta_agent_code": ["rta agent code", "rta_agent_code"], "amc_active_flag": ["amc active flag", "amc_active_flag"],
    "dividend_reinvestment_flag": ["dividend reinvestment flag", "dividend_reinvestment_flag"],
    "sip_flag": ["sip flag", "sip_flag"],
    "stp_flag": ["stp flag", "stp_flag"], "swp_flag": ["swp flag", "swp_flag"],
    "switch_flag": ["switch flag", "switch_flag"],
    "settlement_type": ["settlement type", "settlement_type"], "amc_ind": ["amc_ind", "amc ind"],
    "face_value": ["face value", "face_value"], "start_date": ["start date", "start_date"],
    "end_date": ["end date", "end_date"],
    "exit_load_flag": ["exit load flag", "exit_load_flag"], "exit_load": ["exit load", "exit_load"],
    "lock_in_period_flag": ["lock-in period flag", "lock in period flag", "lock_in_period_flag"],
    "lock_in_period": ["lock-in period", "lock in period", "lock_in_period"],
    "channel_partner_code": ["channel partner code", "channel_partner_code"],
    "reopening_date": ["reopening date", "reopening_date"],
}
_REAL_FIELDS = {"min_purchase_amount", "additional_purchase_amount", "max_purchase_amount",
                "purchase_amount_multiplier", "min_redemption_qty", "redemption_qty_multiplier", "max_redemption_qty",
                "redemption_amount_min", "redemption_amount_max", "redemption_amount_multiple", "face_value",
                "exit_load"}
_DATE_FIELDS = {"start_date", "end_date", "reopening_date"}


def _resolve_columns(df: pd.DataFrame) -> dict[str, str | None]:
    norm_headers = {
        col.lower().strip().replace("\u200b", "").replace("\u00e2\u20ac\u201c", "-").replace("\u201c", "-").replace(
            "\u2013", "-").replace("\u2014", "-").replace("  ", " "): col for col in df.columns}
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
    cols = set(columns)
    variants = set()
    for base in base_names:
        variants.add(base)
        m = re.match(r"^([a-z]+)(\d)_(.+)$", base)
        if m:
            word, num, rest = m.groups()
            variants.update([f"{word}_{num}_{rest}", f"{word}{num}{rest}", f"{word}_{rest}_{num}", f"{rest}_{num}",
                             f"{word}_{num}{rest}"])
        m2 = re.match(r"^(bank)(\d)_(.+)$", base)
        if m2:
            _, num, rest = m2.groups()
            variants.update([f"bank_{rest}_{num}", f"{rest}_{num}", f"bank{num}_{rest}"])
    for v in variants:
        if v in cols: return v
    return None


# ══════════════════════════════════════════════════════════════════════════════
# BSE PARSERS
# ══════════════════════════════════════════════════════════════════════════════

def parse_bse_client_master(file, replace: bool) -> tuple[bool, str]:
    file.seek(0)
    try:
        df = pd.read_excel(file, dtype=str)
    except Exception as exc:
        return False, f"File read error: {exc}"

    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]

    def _c(*candidates):
        return next((c for c in candidates if c in df.columns), None)

    code_col = _c("client_code", "ucc");
    member_col = _c("member_code", "member_id")
    fname_col = _c("primary_holder_first_name", "first_name");
    mname_col = _c("primary_holder_middle_name", "middle_name")
    lname_col = _c("primary_holder_last_name", "last_name");
    tax_col = _c("tax_status");
    gender_col = _c("gender")
    dob_col = _c("primary_holder_dob_incorporation", "primary_holder_dob_incorp", "dob", "date_of_birth")
    occ_col = _c("occupation_code", "occupation");
    hold_col = _c("holding_nature", "mode_of_holding")
    s2_fname_col = _c("second_holder_first_name");
    s2_mname_col = _c("second_holder_middle_name")
    s2_lname_col = _c("second_holder_last_name");
    s2_dob_col = _c("second_holder_dob")
    s3_fname_col = _c("third_holder_first_name");
    s3_mname_col = _c("third_holder_middle_name")
    s3_lname_col = _c("third_holder_last_name");
    s3_dob_col = _c("third_holder_dob")
    g_fname_col = _c("guardian_first_name");
    g_mname_col = _c("guardian_middle_name")
    g_lname_col = _c("guardian_last_name");
    g_dob_col = _c("guardian_dob")
    pan_exempt_col = _c("primary_holder_pan_exempt");
    s2_pan_exempt_col = _c("second_holder_pan_exempt")
    s3_pan_exempt_col = _c("third_holder_pan_exempt");
    g_pan_exempt_col = _c("guardian_pan_exempt")
    pan_col = _c("primary_holder_pan", "pan");
    s2_pan_col = _c("second_holder_pan")
    s3_pan_col = _c("third_holder_pan");
    g_pan_col = _c("guardian_pan")
    exempt_cat_col = _c("primary_holder_exempt_category");
    s2_exempt_cat_col = _c("second_holder_exempt_category")
    s3_exempt_cat_col = _c("third_holder_exempt_category");
    g_exempt_cat_col = _c("guardian_exempt_category")
    client_type_col = _c("client_type");
    pms_col = _c("pms");
    default_dp_col = _c("default_dp")
    cdsl_dpid_col = _c("cdsl_dpid");
    cdsl_cltid_col = _c("cdsl_cltid");
    cmbp_id_col = _c("cmbp_id")
    nsdl_dpid_col = _c("nsdl_dpid");
    nsdl_cltid_col = _c("nsdl_cltid")
    kyc_col = _c("primary_holder_kyc_type", "kyc_type");
    ckyc_col = _c("primary_holder_ckyc_no", "primary_holder_ckyc_number", "ckyc_number")
    s2_kyc_col = _c("second_holder_kyc_type");
    s2_ckyc_col = _c("second_holder_ckyc_no", "second_holder_ckyc_number")
    s3_kyc_col = _c("third_holder_kyc_type");
    s3_ckyc_col = _c("third_holder_ckyc_no", "third_holder_ckyc_number")
    g_kyc_col = _c("guardian_kyc_type");
    g_ckyc_col = _c("guardian_ckyc_no", "guardian_ckyc_number")
    kra_exempt_col = _c("primary_holder_kra_exempt", "primary_holder_kra_exempt_ref")
    s2_kra_exempt_col = _c("second_holder_kra_exempt", "second_holder_kra_exempt_ref")
    s3_kra_exempt_col = _c("third_holder_kra_exempt", "third_holder_kra_exempt_ref")
    g_kra_exempt_col = _c("guardian_kra_exempt", "guardian_exempt_ref_no")
    aadh_col = _c("aadhaar_updated", "aadhaar_flag");
    mapin_col = _c("mapin_id")
    paperless_col = _c("paperless_flag");
    lei_no_col = _c("lei_no");
    lei_validity_col = _c("lei_validity")
    email_decl_col = _c("email_declaration_flag", "email_decl_flag")
    mobile_decl_col = _c("mobile_declaration_flag", "mobile_decl_flag")
    email_col = _c("email", "primary_holder_email");
    mobile_col = _c("indian_mobile_no", "mobile", "mobile_no")
    resi_phone_col = _c("resi_phone");
    resi_fax_col = _c("resi_fax")
    office_phone_col = _c("office_phone");
    office_fax_col = _c("office_fax")
    comm_mode_col = _c("communication_mode", "comm_mode")
    addr1_col = _c("address1", "address_1", "address_line_1");
    addr2_col = _c("address2", "address_2")
    addr3_col = _c("address3", "address_3");
    city_col = _c("city");
    state_col = _c("state")
    pin_col = _c("pincode", "pin_code");
    country_col = _c("country")
    f_addr1_col = _c("foreign_address1");
    f_addr2_col = _c("foreign_address2");
    f_addr3_col = _c("foreign_address3")
    f_city_col = _c("foreign_city");
    f_pin_col = _c("foreign_pincode");
    f_state_col = _c("foreign_state")
    f_country_col = _c("foreign_country");
    f_resi_phone_col = _c("foreign_resi_phone");
    f_resi_fax_col = _c("foreign_resi_fax")
    f_office_phone_col = _c("foreign_off_phone");
    f_office_fax_col = _c("foreign_off_fax")
    cheque_name_col = _c("cheque_name");
    div_pay_col = _c("div_pay_mode")
    branch_col = _c("branch");
    dealer_col = _c("dealer");
    branch_dealer_col = _c("branch_dealer")
    nom_opt_col = _c("nomination_opt");
    nom_auth_mode_col = _c("nomination_auth_mode")
    nom_flag_col = _c("nomination_flag");
    nom_auth_date_col = _c("nomination_auth_date")
    g_relationship_col = _c("guardian_relationship")
    created_by_col = _c("created_by");
    created_col = _c("created_at", "creation_date")
    modified_by_col = _c("last_modified_by");
    modified_col = _c("last_modified_at", "last_modified_date")

    if not code_col or not fname_col: return False, "Missing critical columns: client_code / primary_holder_first_name"

    batch = _batch_id("BSE_CLIENT", file.name)
    client_rows = []

    for _, row in df.iterrows():
        code = raw_val(row.get(code_col, ""))
        if not code: continue
        b_val = raw_val(row.get(branch_col, "")) if branch_col else ""
        d_val = raw_val(row.get(dealer_col, "")) if dealer_col else ""
        if not b_val and not d_val and branch_dealer_col: b_val = raw_val(row.get(branch_dealer_col, ""))

        core = (raw_val(row.get(member_col, "")) if member_col else "", code, raw_val(row.get(fname_col, "")),
                raw_val(row.get(mname_col, "")) if mname_col else "",
                raw_val(row.get(lname_col, "")) if lname_col else "", raw_val(row.get(tax_col, "")) if tax_col else "",
                raw_val(row.get(gender_col, "")) if gender_col else "",
                raw_val(row.get(dob_col, "")) if dob_col else "", raw_val(row.get(occ_col, "")) if occ_col else "",
                raw_val(row.get(hold_col, "")) if hold_col else "")
        holders = (raw_val(row.get(s2_fname_col, "")) if s2_fname_col else "",
                   raw_val(row.get(s2_mname_col, "")) if s2_mname_col else "",
                   raw_val(row.get(s2_lname_col, "")) if s2_lname_col else "",
                   raw_val(row.get(s3_fname_col, "")) if s3_fname_col else "",
                   raw_val(row.get(s3_mname_col, "")) if s3_mname_col else "",
                   raw_val(row.get(s3_lname_col, "")) if s3_lname_col else "",
                   raw_val(row.get(s2_dob_col, "")) if s2_dob_col else "",
                   raw_val(row.get(s3_dob_col, "")) if s3_dob_col else "",
                   raw_val(row.get(g_fname_col, "")) if g_fname_col else "",
                   raw_val(row.get(g_mname_col, "")) if g_mname_col else "",
                   raw_val(row.get(g_lname_col, "")) if g_lname_col else "",
                   raw_val(row.get(g_dob_col, "")) if g_dob_col else "")
        pan_exempt = (raw_val(row.get(pan_exempt_col, "")) if pan_exempt_col else "",
                      raw_val(row.get(s2_pan_exempt_col, "")) if s2_pan_exempt_col else "",
                      raw_val(row.get(s3_pan_exempt_col, "")) if s3_pan_exempt_col else "",
                      raw_val(row.get(g_pan_exempt_col, "")) if g_pan_exempt_col else "")
        pan_nums = (
            raw_val(row.get(pan_col, "")) if pan_col else "", raw_val(row.get(s2_pan_col, "")) if s2_pan_col else "",
            raw_val(row.get(s3_pan_col, "")) if s3_pan_col else "",
            raw_val(row.get(g_pan_col, "")) if g_pan_col else "")
        exempt = (raw_val(row.get(exempt_cat_col, "")) if exempt_cat_col else "",
                  raw_val(row.get(s2_exempt_cat_col, "")) if s2_exempt_cat_col else "",
                  raw_val(row.get(s3_exempt_cat_col, "")) if s3_exempt_cat_col else "",
                  raw_val(row.get(g_exempt_cat_col, "")) if g_exempt_cat_col else "")
        client_meta = (raw_val(row.get(client_type_col, "")) if client_type_col else "",
                       raw_val(row.get(pms_col, "")) if pms_col else "",
                       raw_val(row.get(default_dp_col, "")) if default_dp_col else "",
                       raw_val(row.get(cdsl_dpid_col, "")) if cdsl_dpid_col else "",
                       raw_val(row.get(cdsl_cltid_col, "")) if cdsl_cltid_col else "",
                       raw_val(row.get(cmbp_id_col, "")) if cmbp_id_col else "",
                       raw_val(row.get(nsdl_dpid_col, "")) if nsdl_dpid_col else "",
                       raw_val(row.get(nsdl_cltid_col, "")) if nsdl_cltid_col else "")

        bank_fields = []
        for seq in range(1, 6):
            pfx = f"bank{seq}_"
            acno_col = _cflex(df.columns, f"{pfx}account_no", f"{pfx}ac_no", f"account_no_{seq}")
            type_col = _cflex(df.columns, f"{pfx}account_type", f"account_type_{seq}")
            micr_col = _cflex(df.columns, f"{pfx}micr_no", f"micr_no_{seq}", f"micr_{seq}")
            ifsc_col = _cflex(df.columns, f"{pfx}ifsc_code", f"ifsc_code_{seq}", f"ifsc_{seq}")
            bname_col = _cflex(df.columns, f"{pfx}bank_name", f"bank_name_{seq}")
            branch_col_b = _cflex(df.columns, f"{pfx}bank_branch", f"bank_branch_{seq}", f"branch_{seq}")
            dbflag_col = _cflex(df.columns, f"{pfx}default_bank_flag", f"default_bank_flag_{seq}")
            bcr_col = _cflex(df.columns, f"{pfx}created_at", f"bank{seq}_created_at")
            bmod_col = _cflex(df.columns, f"{pfx}last_modified_at", f"bank{seq}_last_modified_at")
            bstat_col = _cflex(df.columns, f"{pfx}status", f"bank{seq}_status")
            bank_fields += [raw_val(row.get(type_col or "", "")), raw_val(row.get(acno_col or "", "")),
                            raw_val(row.get(micr_col or "", "")), raw_val(row.get(ifsc_col or "", "")),
                            raw_val(row.get(bname_col or "", "")), raw_val(row.get(branch_col_b or "", "")),
                            raw_val(row.get(dbflag_col or "", "")), raw_val(row.get(bcr_col or "", "")),
                            raw_val(row.get(bmod_col or "", "")), raw_val(row.get(bstat_col or "", ""))]

        cheque_div = (raw_val(row.get(cheque_name_col, "")) if cheque_name_col else "",
                      raw_val(row.get(div_pay_col, "")) if div_pay_col else "")
        address_contact = (
            raw_val(row.get(addr1_col, "")) if addr1_col else "", raw_val(row.get(addr2_col, "")) if addr2_col else "",
            raw_val(row.get(addr3_col, "")) if addr3_col else "", raw_val(row.get(city_col, "")) if city_col else "",
            raw_val(row.get(state_col, "")) if state_col else "", raw_val(row.get(pin_col, "")) if pin_col else "",
            raw_val(row.get(country_col, "")) if country_col else "",
            raw_val(row.get(resi_phone_col, "")) if resi_phone_col else "",
            raw_val(row.get(resi_fax_col, "")) if resi_fax_col else "",
            raw_val(row.get(office_phone_col, "")) if office_phone_col else "",
            raw_val(row.get(office_fax_col, "")) if office_fax_col else "")
        email_comm = (raw_val(row.get(email_col, "")) if email_col else "",
                      raw_val(row.get(comm_mode_col, "")) if comm_mode_col else "")
        foreign = (raw_val(row.get(f_addr1_col, "")) if f_addr1_col else "",
                   raw_val(row.get(f_addr2_col, "")) if f_addr2_col else "",
                   raw_val(row.get(f_addr3_col, "")) if f_addr3_col else "",
                   raw_val(row.get(f_city_col, "")) if f_city_col else "",
                   raw_val(row.get(f_pin_col, "")) if f_pin_col else "",
                   raw_val(row.get(f_state_col, "")) if f_state_col else "",
                   raw_val(row.get(f_country_col, "")) if f_country_col else "",
                   raw_val(row.get(f_resi_phone_col, "")) if f_resi_phone_col else "",
                   raw_val(row.get(f_resi_fax_col, "")) if f_resi_fax_col else "",
                   raw_val(row.get(f_office_phone_col, "")) if f_office_phone_col else "",
                   raw_val(row.get(f_office_fax_col, "")) if f_office_fax_col else "")
        mobile = (raw_val(row.get(mobile_col, "")) if mobile_col else "",)
        kyc_ckyc = (
            raw_val(row.get(kyc_col, "")) if kyc_col else "", raw_val(row.get(ckyc_col, "")) if ckyc_col else "",
            raw_val(row.get(s2_kyc_col, "")) if s2_kyc_col else "",
            raw_val(row.get(s2_ckyc_col, "")) if s2_ckyc_col else "",
            raw_val(row.get(s3_kyc_col, "")) if s3_kyc_col else "",
            raw_val(row.get(s3_ckyc_col, "")) if s3_ckyc_col else "",
            raw_val(row.get(g_kyc_col, "")) if g_kyc_col else "",
            raw_val(row.get(g_ckyc_col, "")) if g_ckyc_col else "")
        kra = (raw_val(row.get(kra_exempt_col, "")) if kra_exempt_col else "",
               raw_val(row.get(s2_kra_exempt_col, "")) if s2_kra_exempt_col else "",
               raw_val(row.get(s3_kra_exempt_col, "")) if s3_kra_exempt_col else "",
               raw_val(row.get(g_kra_exempt_col, "")) if g_kra_exempt_col else "")
        ids = (raw_val(row.get(aadh_col, "")) if aadh_col else "", raw_val(row.get(mapin_col, "")) if mapin_col else "",
               raw_val(row.get(paperless_col, "")) if paperless_col else "",
               raw_val(row.get(lei_no_col, "")) if lei_no_col else "",
               raw_val(row.get(lei_validity_col, "")) if lei_validity_col else "")
        decl_flags = (raw_val(row.get(email_decl_col, "")) if email_decl_col else "",
                      raw_val(row.get(mobile_decl_col, "")) if mobile_decl_col else "")
        nom_opt = (raw_val(row.get(nom_opt_col, "")) if nom_opt_col else "",
                   raw_val(row.get(nom_auth_mode_col, "")) if nom_auth_mode_col else "")
        nom1 = _build_nominee(row, _c, 1);
        nom2 = _build_nominee(row, _c, 2);
        nom3 = _build_nominee(row, _c, 3)
        nom_soa = (raw_val(row.get(_c("nom_soa"), "")),)
        holder_comm = (raw_val(row.get(_c("second_holder_email"), "")),
                       raw_val(row.get(_c("second_holder_email_declaration", "second_holder_email_decl"), "")),
                       raw_val(row.get(_c("second_holder_mobile"), "")),
                       raw_val(row.get(_c("second_holder_mobile_declaration", "second_holder_mobile_decl"), "")),
                       raw_val(row.get(_c("third_holder_email"), "")),
                       raw_val(row.get(_c("third_holder_email_declaration", "third_holder_email_decl"), "")),
                       raw_val(row.get(_c("third_holder_mobile"), "")),
                       raw_val(row.get(_c("third_holder_mobile_declaration", "third_holder_mobile_decl"), "")))
        nom_end = (raw_val(row.get(nom_flag_col, "")) if nom_flag_col else "",
                   raw_val(row.get(nom_auth_date_col, "")) if nom_auth_date_col else "",
                   raw_val(row.get(g_relationship_col, "")) if g_relationship_col else "")
        audit = (raw_val(row.get(created_by_col, "")) if created_by_col else "",
                 raw_val(row.get(created_col, "")) if created_col else "",
                 raw_val(row.get(modified_by_col, "")) if modified_by_col else "",
                 raw_val(row.get(modified_col, "")) if modified_col else "", batch)

        full_row = core + holders + pan_exempt + pan_nums + exempt + client_meta + tuple(
            bank_fields) + cheque_div + address_contact + email_comm + foreign + mobile + kyc_ckyc + kra + ids + decl_flags + (
                       b_val, d_val) + nom_opt + nom1 + nom2 + nom3 + nom_soa + holder_comm + nom_end + audit
        client_rows.append(full_row)

    if not client_rows: return False, "No valid rows found"

    insert_sql = """INSERT OR IGNORE INTO bse_client_master (member_code, client_code, primary_holder_first_name, 
    primary_holder_middle_name, primary_holder_last_name, tax_status, gender, primary_holder_dob_incorporation, 
    occupation_code, holding_nature, second_holder_first_name, second_holder_middle_name, second_holder_last_name, 
    third_holder_first_name, third_holder_middle_name, third_holder_last_name, second_holder_dob, third_holder_dob, 
    guardian_first_name, guardian_middle_name, guardian_last_name, guardian_dob, primary_holder_pan_exempt, 
    second_holder_pan_exempt, third_holder_pan_exempt, guardian_pan_exempt, primary_holder_pan, second_holder_pan, 
    third_holder_pan, guardian_pan, primary_holder_exempt_category, second_holder_exempt_category, 
    third_holder_exempt_category, guardian_exempt_category, client_type, pms, default_dp, cdsl_dpid, cdsl_cltid, 
    cmbp_id, nsdl_dpid, nsdl_cltid, account_type_1, account_no_1, micr_no_1, ifsc_code_1, bank_name_1, bank_branch_1, 
    default_bank_flag_1, bank1_created_at, bank1_last_modified_at, bank1_status, account_type_2, account_no_2, 
    micr_no_2, ifsc_code_2, bank_name_2, bank_branch_2, default_bank_flag_2, bank2_created_at, 
    bank2_last_modified_at, bank2_status, account_type_3, account_no_3, micr_no_3, ifsc_code_3, bank_name_3, 
    bank_branch_3, default_bank_flag_3, bank3_created_at, bank3_last_modified_at, bank3_status, account_type_4, 
    account_no_4, micr_no_4, ifsc_code_4, bank_name_4, bank_branch_4, default_bank_flag_4, bank4_created_at, 
    bank4_last_modified_at, bank4_status, account_type_5, account_no_5, micr_no_5, ifsc_code_5, bank_name_5, 
    bank_branch_5, default_bank_flag_5, bank5_created_at, bank5_last_modified_at, bank5_status, cheque_name, 
    div_pay_mode, address1, address2, address3, city, state, pincode, country, resi_phone, resi_fax, office_phone, 
    office_fax, email, communication_mode, foreign_address1, foreign_address2, foreign_address3, foreign_city, 
    foreign_pincode, foreign_state, foreign_country, foreign_resi_phone, foreign_resi_fax, foreign_off_phone, 
    foreign_off_fax, indian_mobile_no, primary_holder_kyc_type, primary_holder_ckyc_no, second_holder_kyc_type, 
    second_holder_ckyc_no, third_holder_kyc_type, third_holder_ckyc_no, guardian_kyc_type, guardian_ckyc_no, 
    primary_holder_kra_exempt, second_holder_kra_exempt, third_holder_kra_exempt, guardian_kra_exempt, 
    aadhaar_updated, mapin_id, paperless_flag, lei_no, lei_validity, email_declaration_flag, mobile_declaration_flag, 
    branch, dealer, nomination_opt, nomination_auth_mode, nominee1_name, nominee1_relationship, 
    nominee1_applicable_pct, nominee1_minor_flag, nominee1_dob, nominee1_guardian, nominee1_guardian_pan, 
    nom1_id_typ, nom1_idno, nom1_email, nom1_mob, nom1_add1, nom1_add2, nom1_add3, nom1_city, nom1_pin, nom1_con, 
    nominee2_name, nominee2_relationship, nominee2_applicable_pct, nominee2_dob, nominee2_minor_flag, 
    nominee2_guardian, nominee2_guardian_pan, nom2_id_typ, nom2_idno, nom2_email, nom2_mob, nom2_add1, nom2_add2, 
    nom2_add3, nom2_city, nom2_pin, nom2_con, nominee3_name, nominee3_relationship, nominee3_applicable_pct, 
    nominee3_dob, nominee3_minor_flag, nominee3_guardian, nominee3_guardian_pan, nom3_id_typ, nom3_idno, nom3_email, 
    nom3_mob, nom3_add1, nom3_add2, nom3_add3, nom3_city, nom3_pin, nom3_con, nom_soa, second_holder_email, 
    second_holder_email_declaration, second_holder_mobile, second_holder_mobile_declaration, third_holder_email, 
    third_holder_email_declaration, third_holder_mobile, third_holder_mobile_declaration, nomination_flag, 
    nomination_auth_date, guardian_relationship, created_by, created_at, last_modified_by, last_modified_at, 
    upload_batch) VALUES (""" + ",".join(
        ["?"] * 210) + ")"

    inserted = skipped = 0
    with get_conn() as conn:
        if replace:
            conn.execute("DELETE FROM bse_client_master");
            conn.executemany(insert_sql, client_rows);
            inserted = len(client_rows)
        else:
            existing = {r[0] for r in conn.execute("SELECT client_code FROM bse_client_master").fetchall()}
            new_rows = [r for r in client_rows if r[1] not in existing];
            skipped = len(client_rows) - len(new_rows)
            if new_rows: conn.executemany(insert_sql, new_rows)
            inserted = len(new_rows)
    msg = f"Imported {inserted} clients"
    if skipped: msg += f" | Skipped {skipped} existing"
    return True, msg


def _build_nominee(row, _c_func, seq: int) -> tuple:
    pfx = f"nominee{seq}_";
    npfx = f"nom{seq}_";
    cols = row.index

    def F(*bases): return _cflex(cols, *bases)

    return (raw_val(row.get(F(f"{pfx}name", f"nominee_{seq}_name") or "", "")),
            raw_val(row.get(F(f"{pfx}relationship") or "", "")),
            raw_val(row.get(F(f"{pfx}applicable_pct", f"{pfx}percentage") or "", "")),
            raw_val(row.get(F(f"{pfx}minor_flag", f"{pfx}is_minor") or "", "")),
            raw_val(row.get(F(f"{pfx}dob") or "", "")),
            raw_val(row.get(F(f"{pfx}guardian", f"{pfx}guardian_name") or "", "")),
            raw_val(row.get(F(f"{pfx}guardian_pan") or "", "")), raw_val(row.get(F(f"{npfx}id_typ") or "", "")),
            raw_val(row.get(F(f"{npfx}idno") or "", "")), raw_val(row.get(F(f"{pfx}email", f"{npfx}email") or "", "")),
            raw_val(row.get(F(f"{pfx}mobile", f"{npfx}mob") or "", "")),
            raw_val(row.get(F(f"{pfx}address1", f"{npfx}add1") or "", "")),
            raw_val(row.get(F(f"{pfx}address2", f"{npfx}add2") or "", "")),
            raw_val(row.get(F(f"{pfx}address3", f"{npfx}add3") or "", "")),
            raw_val(row.get(F(f"{pfx}city", f"{npfx}city") or "", "")),
            raw_val(row.get(F(f"{pfx}pincode", f"{npfx}pin") or "", "")), raw_val(row.get(F(f"{npfx}con") or "", "")))


def parse_bse_sip(file, replace: bool) -> tuple[bool, str, dict]:
    file.seek(0)
    try:
        df = pd.read_excel(file, dtype=str)
    except Exception as exc:
        return False, f"File read error: {exc}", {}
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]

    def _c(*cands):
        return next((c for c in cands if c in df.columns), None)

    xsip_col = _c("xsip_regn_no", "registration_no", "regn_no");
    client_col = _c("client_code", "ucc");
    name_col = _c("client_name", "investor_name", "name");
    member_col = _c("member_code");
    status_col = _c("status", "sip_status");
    amc_col = _c("amc_name", "amc");
    scheme_col = _c("rta_scheme_code", "scheme_code");
    sname_col = _c("scheme_name");
    freq_col = _c("frequency_type", "frequency");
    sd_col = _c("start_date");
    ed_col = _c("end_date");
    amt_col = _c("installments_amt", "sip_amount", "amount");
    inst_col = _c("no_of_installments", "installments");
    fo_col = _c("first_order");
    folio_col = _c("folio_no", "folio");
    mandate_col = _c("mandate_id");
    euin_col = _c("euin");
    sub_col = _c("sub_broker_arncode", "sub_broker_arn_code", "sub_broker");
    regn_col = _c("regn_date", "registration_date");
    pgref_col = _c("pg_bank_reference_no", "pg_bank_ref_no", "pg_ref_no");
    rem_col = _c("remarks");
    email_col = _c("primary_holder_email", "primary_email", "email");
    mob_col = _c("primary_holder_mobile", "primary_mobile", "mobile");
    dpc_col = _c("dpc_flag");
    dp_trans_col = _c("dp_trans");
    dpsub_col = _c("dp_trans_sub_broker");
    euindecl_col = _c("euin_decl");
    exrem_col = _c("exchange_remark");
    health_col = _c("health_declaration_flag", "health_decl_flag");
    nomdob_col = _c("nominee_dob");
    disc_col = _c("disclaimer_flag");
    intref_col = _c("internal_ref_no");
    s2email_col = _c("second_holder_email");
    s2mob_col = _c("second_holder_mobile");
    s3email_col = _c("third_holder_email");
    s3mob_col = _c("third_holder_mobile");
    broker_col = _c("brokerage");
    entry_col = _c("entry_by")
    if not xsip_col or not client_col: return False, f"Missing: xsip_regn_no / client_code", {}
    batch = _batch_id("BSE_SIP", file.name);
    rows, skipped = [], 0
    for _, row in df.iterrows():
        regn = raw_val(row.get(xsip_col, ""))
        if not regn: skipped += 1; continue
        dp_trans_val = raw_val(row.get(dp_trans_col, "")) if dp_trans_col else ""
        sub_broker_val = raw_val(row.get(sub_col, "")) if sub_col else ""
        if not dp_trans_val and dpsub_col: dp_trans_val = raw_val(row.get(dpsub_col, ""))
        rows.append((raw_val(row.get(status_col, "")) if status_col else "",
                     raw_val(row.get(member_col, "")) if member_col else "", raw_val(row.get(client_col, "")),
                     raw_val(row.get(name_col, "")) if name_col else "",
                     raw_val(row.get(pgref_col, "")) if pgref_col else "", regn,
                     raw_val(row.get(regn_col, "")) if regn_col else "",
                     raw_val(row.get(amc_col, "")) if amc_col else "",
                     raw_val(row.get(scheme_col, "")) if scheme_col else "",
                     raw_val(row.get(sname_col, "")) if sname_col else "",
                     raw_val(row.get(freq_col, "")) if freq_col else "", raw_val(row.get(sd_col, "")) if sd_col else "",
                     raw_val(row.get(ed_col, "")) if ed_col else "", raw_val(row.get(amt_col, "")) if amt_col else "",
                     raw_val(row.get(broker_col, "")) if broker_col else "",
                     raw_val(row.get(entry_col, "")) if entry_col else "",
                     raw_val(row.get(mandate_col, "")) if mandate_col else "",
                     raw_val(row.get(dpc_col, "")) if dpc_col else "", dp_trans_val, sub_broker_val,
                     raw_val(row.get(euin_col, "")) if euin_col else "",
                     raw_val(row.get(euindecl_col, "")) if euindecl_col else "",
                     raw_val(row.get(fo_col, "")) if fo_col else "",
                     raw_val(row.get(folio_col, "")) if folio_col else "",
                     raw_val(row.get(rem_col, "")) if rem_col else "", raw_val(row.get(sub_col, "")) if sub_col else "",
                     raw_val(row.get(inst_col, "")) if inst_col else "",
                     raw_val(row.get(exrem_col, "")) if exrem_col else "",
                     raw_val(row.get(health_col, "")) if health_col else "",
                     raw_val(row.get(nomdob_col, "")) if nomdob_col else "",
                     raw_val(row.get(disc_col, "")) if disc_col else "",
                     raw_val(row.get(intref_col, "")) if intref_col else "",
                     raw_val(row.get(email_col, "")) if email_col else "",
                     raw_val(row.get(mob_col, "")) if mob_col else "",
                     raw_val(row.get(s2email_col, "")) if s2email_col else "",
                     raw_val(row.get(s2mob_col, "")) if s2mob_col else "",
                     raw_val(row.get(s3email_col, "")) if s3email_col else "",
                     raw_val(row.get(s3mob_col, "")) if s3mob_col else "", batch))
    if not rows: return False, "0 SIP rows found", {}
    insert_sql = """INSERT OR IGNORE INTO bse_sip (status, member_code, client_code, client_name, pg_bank_reference_no, xsip_regn_no, regn_date, amc_name, rta_scheme_code, scheme_name, frequency_type, start_date, end_date, installments_amt, brokerage, entry_by, mandate_id, dpc_flag, dp_trans, sub_broker, euin, euin_decl, first_order, folio_no, remarks, sub_broker_arncode, no_of_installments, exchange_remark, health_declaration_flag, nominee_dob, disclaimer_flag, internal_ref_no, primary_holder_email, primary_holder_mobile, second_holder_email, second_holder_mobile, third_holder_email, third_holder_mobile, upload_batch) VALUES (""" + ",".join(
        ["?"] * 39) + ")"
    inserted = dupes = 0
    with get_conn() as conn:
        if replace:
            conn.execute("DELETE FROM bse_sip");
            conn.executemany(insert_sql, rows);
            inserted = len(rows)
        else:
            before = _count_before_after(conn, "bse_sip");
            conn.executemany(insert_sql,
                             rows);
            inserted, dupes = _inserted_dupes(
                before, conn, "bse_sip", len(rows))
    active = sum(1 for r in rows if "active" in str(r[0]).lower())
    msg = f"Imported {inserted} SIP records | Active: {active}"
    if skipped:
        msg += f" | Skipped {skipped}"
    if dupes:
        msg += f" | {dupes} duplicates"
    return True, msg, {"rows": inserted, "skipped": skipped, "duplicates": dupes, "active": active}


def parse_bse_scheme_master(file, replace: bool) -> tuple[bool, str, dict]:
    file.seek(0);
    fname = getattr(file, "name", "")
    try:
        if fname.lower().endswith((".xlsx", ".xls")):
            df = pd.read_excel(file, dtype=str)
        else:
            df = None
            for sep in ("\t", ","):
                file.seek(0)
                try:
                    candidate = pd.read_csv(file, sep=sep, dtype=str, encoding="utf-8", encoding_errors="replace")
                    if len(candidate.columns) > 5: df = candidate; break
                except Exception:
                    continue
            if df is None: return False, "Could not parse file", {}
    except Exception as exc:
        return False, f"File read error: {exc}", {}
    df = _clean_cols(df);
    col = _resolve_columns(df)
    required = ["scheme_code", "rta_scheme_code", "isin"]
    missing = [f for f in required if col[f] is None]
    if missing: return False, f"Missing required columns: {missing}", {}
    batch = _batch_id("BSE_SCHEME", fname);
    rows: list[tuple] = [];
    skipped = 0
    for _, row in df.iterrows():
        sc = raw_val(row.get(col["scheme_code"], ""));
        rta = raw_val(row.get(col["rta_scheme_code"], ""))
        if not sc and not rta: skipped += 1; continue

        def _get(field: str):
            src = col[field]
            if src is None: return ""
            return raw_val(row.get(src, ""))

        try:
            rows.append((_get("unique_no"), sc, rta, _get("amc_scheme_code"), _get("isin"), _get("amc_code"),
                         _get("scheme_type"), _get("scheme_plan"), _get("scheme_name"), _get("purchase_allowed"),
                         _get("purchase_transaction_mode"), _get("min_purchase_amount"),
                         _get("additional_purchase_amount"), _get("max_purchase_amount"),
                         _get("purchase_amount_multiplier"), _get("purchase_cutoff_time"), _get("redemption_allowed"),
                         _get("redemption_transaction_mode"), _get("min_redemption_qty"),
                         _get("redemption_qty_multiplier"), _get("max_redemption_qty"), _get("redemption_amount_min"),
                         _get("redemption_amount_max"), _get("redemption_amount_multiple"),
                         _get("redemption_cutoff_time"), _get("rta_agent_code"), _get("amc_active_flag"),
                         _get("dividend_reinvestment_flag"), _get("sip_flag"), _get("stp_flag"), _get("swp_flag"),
                         _get("switch_flag"), _get("settlement_type"), _get("amc_ind"), _get("face_value"),
                         _get("start_date"), _get("end_date"), _get("exit_load_flag"), _get("exit_load"),
                         _get("lock_in_period_flag"), _get("lock_in_period"), _get("channel_partner_code"),
                         _get("reopening_date"), batch))
        except Exception as exc:
            log.warning("Skipped scheme row: %s", exc);
            skipped += 1
    if not rows: return False, "0 rows parsed", {}
    sql = """INSERT OR IGNORE INTO bse_scheme_master (unique_no, scheme_code, rta_scheme_code, amc_scheme_code, isin, amc_code, scheme_type, scheme_plan, scheme_name, purchase_allowed, purchase_transaction_mode, min_purchase_amount, additional_purchase_amount, max_purchase_amount, purchase_amount_multiplier, purchase_cutoff_time, redemption_allowed, redemption_transaction_mode, min_redemption_qty, redemption_qty_multiplier, max_redemption_qty, redemption_amount_min, redemption_amount_max, redemption_amount_multiple, redemption_cutoff_time, rta_agent_code, amc_active_flag, dividend_reinvestment_flag, sip_flag, stp_flag, swp_flag, switch_flag, settlement_type, amc_ind, face_value, start_date, end_date, exit_load_flag, exit_load, lock_in_period_flag, lock_in_period, channel_partner_code, reopening_date, upload_batch) VALUES (""" + ",".join(
        ["?"] * 44) + ")"
    inserted = dupes = 0
    with get_conn() as conn:
        if replace:
            conn.execute("DELETE FROM bse_scheme_master");
            conn.executemany(sql, rows);
            inserted = len(rows)
        else:
            before = _count_before_after(conn, "bse_scheme_master");
            conn.executemany(sql,
                             rows);
            inserted, dupes = _inserted_dupes(
                before, conn, "bse_scheme_master", len(rows))
    return True, f"Imported {inserted} schemes | Skipped: {skipped} | Dupes: {dupes}", {"rows": inserted,
                                                                                        "skipped": skipped,
                                                                                        "duplicates": dupes}


# ══════════════════════════════════════════════════════════════════════════════
# CAMS PARSERS (Raw Inserts)
# ══════════════════════════════════════════════════════════════════════════════

def parse_cams_wbr4_aum(file, replace: bool) -> tuple[bool, str, dict]:
    df = _read_csv_auto(file)
    if df is None: return False, "Could not parse file", {}
    df = _clean_cols(df)
    if "FOLIOCHK" not in df.columns: return False, f"Missing FOLIOCHK. Found: {list(df.columns)[:10]}", {}
    batch = _batch_id("CAMS_R4", file.name);
    rows, skipped = [], 0
    for _, row in df.iterrows():
        folio = raw_val(row.get("FOLIOCHK", ""))
        if not folio: skipped += 1; continue
        rows.append((folio, raw_val(row.get("INV_NAME", "")), raw_val(row.get("ADDRESS1", "")),
                     raw_val(row.get("ADDRESS2", "")), raw_val(row.get("ADDRESS3", "")), raw_val(row.get("CITY", "")),
                     raw_val(row.get("PINCODE", "")), raw_val(row.get("PRODUCT", row.get("PRODCODE", ""))),
                     raw_val(row.get("SCH_NAME", row.get("SCHEME", ""))), raw_val(row.get("REP_DATE", "")),
                     raw_val(row.get("CLOS_BAL", "")), raw_val(row.get("RUPEE_BAL", "")),
                     raw_val(row.get("SUBBROK", "")), raw_val(row.get("REINV_FLAG", "")),
                     raw_val(row.get("JOINT1_NAME", "")), raw_val(row.get("JOINT2_NAME", "")),
                     raw_val(row.get("PHONE_OFF", "")), raw_val(row.get("PHONE_RES", "")),
                     raw_val(row.get("EMAIL", "")), raw_val(row.get("HOLDING_NATURE", "")),
                     raw_val(row.get("UIN_NO", "")), raw_val(row.get("BROKER_CODE", row.get("BROKCODE", ""))),
                     raw_val(row.get("PAN_NO", "")), raw_val(row.get("JOINT1_PAN", "")),
                     raw_val(row.get("JOINT2_PAN", "")), raw_val(row.get("GUARD_PAN", "")),
                     raw_val(row.get("TAX_STATUS", "")), raw_val(row.get("INV_IIN", "")),
                     raw_val(row.get("ALTFOLIO", "")), raw_val(row.get("EUIN", "")),
                     raw_val(row.get("EXCHANGE_FLAG", "")), raw_val(row.get("TPA_LINKED", "")),
                     raw_val(row.get("FH_CKYC_NO", "")), raw_val(row.get("JH1_CKYC", "")),
                     raw_val(row.get("JH2_CKYC", "")), raw_val(row.get("G_CKYC_NO", "")),
                     raw_val(row.get("JH1_DOB", "")), raw_val(row.get("JH2_DOB", "")),
                     raw_val(row.get("GUARDIAN_DOB", "")), raw_val(row.get("AMC_CODE", "")),
                     raw_val(row.get("GST_STATE_CODE", "")), raw_val(row.get("FOLIO_OLD", "")),
                     raw_val(row.get("SCHEME_FOLIO_NUMBER", "")), raw_val(row.get("COUNTRY", "")), batch))
    return _execute_cams_import("cams_wbr4_aum", rows, batch, replace, skipped)


def parse_cams_wbr9_folio(file, replace: bool) -> tuple[bool, str, dict]:
    df = _read_csv_auto(file)
    if df is None: return False, "Could not parse file", {}
    df = _clean_cols(df)
    if "FOLIOCHK" not in df.columns: return False, f"Missing FOLIOCHK. Found: {list(df.columns)[:10]}", {}
    batch = _batch_id("CAMS_R9", file.name);
    rows, skipped = [], 0
    for _, row in df.iterrows():
        folio = raw_val(row.get("FOLIOCHK", ""))
        if not folio: skipped += 1; continue
        rows.append((folio, raw_val(row.get("INV_NAME", "")), raw_val(row.get("ADDRESS1", "")),
                     raw_val(row.get("ADDRESS2", "")), raw_val(row.get("ADDRESS3", "")), raw_val(row.get("CITY", "")),
                     raw_val(row.get("PINCODE", "")), raw_val(row.get("PRODUCT", row.get("PRODCODE", ""))),
                     raw_val(row.get("SCH_NAME", row.get("SCHEME", ""))), raw_val(row.get("REP_DATE", "")),
                     raw_val(row.get("CLOS_BAL", "")), raw_val(row.get("RUPEE_BAL", "")),
                     raw_val(row.get("JNT_NAME1", "")), raw_val(row.get("JNT_NAME2", "")),
                     raw_val(row.get("PHONE_OFF", "")), raw_val(row.get("PHONE_RES", "")),
                     raw_val(row.get("EMAIL", "")), raw_val(row.get("HOLDING_NATURE", "")),
                     raw_val(row.get("UIN_NO", "")), raw_val(row.get("PAN_NO", "")), raw_val(row.get("JOINT1_PAN", "")),
                     raw_val(row.get("JOINT2_PAN", "")), raw_val(row.get("GUARD_PAN", "")),
                     raw_val(row.get("TAX_STATUS", "")), raw_val(row.get("BROKER_CODE", row.get("BROKCODE", ""))),
                     raw_val(row.get("SUBBROKER", "")), raw_val(row.get("REINV_FLAG", "")),
                     raw_val(row.get("BANK_NAME", "")), raw_val(row.get("BRANCH", "")), raw_val(row.get("AC_TYPE", "")),
                     raw_val(row.get("AC_NO", "")), raw_val(row.get("B_ADDRESS1", "")),
                     raw_val(row.get("B_ADDRESS2", "")), raw_val(row.get("B_ADDRESS3", "")),
                     raw_val(row.get("B_CITY", "")), raw_val(row.get("B_PINCODE", "")), raw_val(row.get("INV_DOB", "")),
                     raw_val(row.get("MOBILE_NO", "")), raw_val(row.get("OCCUPATION", "")),
                     raw_val(row.get("INV_IIN", "")), raw_val(row.get("NOM_NAME", "")),
                     raw_val(row.get("RELATION", "")), raw_val(row.get("NOM_ADDR1", "")),
                     raw_val(row.get("NOM_ADDR2", "")), raw_val(row.get("NOM_ADDR3", "")),
                     raw_val(row.get("NOM_CITY", "")), raw_val(row.get("NOM_STATE", "")),
                     raw_val(row.get("NOM_PINCODE", "")), raw_val(row.get("NOM_PH_OFF", "")),
                     raw_val(row.get("NOM_PH_RES", "")), raw_val(row.get("NOM_EMAIL", "")),
                     raw_val(row.get("NOM_PERCENTAGE", "")), raw_val(row.get("NOM2_NAME", "")),
                     raw_val(row.get("NOM2_RELATION", "")), raw_val(row.get("NOM2_ADDR1", "")),
                     raw_val(row.get("NOM2_ADDR2", "")), raw_val(row.get("NOM2_ADDR3", "")),
                     raw_val(row.get("NOM2_CITY", "")), raw_val(row.get("NOM2_STATE", "")),
                     raw_val(row.get("NOM2_PINCODE", "")), raw_val(row.get("NOM2_PH_OFF", "")),
                     raw_val(row.get("NOM2_PH_RES", "")), raw_val(row.get("NOM2_EMAIL", "")),
                     raw_val(row.get("NOM2_PERCENTAGE", "")), raw_val(row.get("NOM3_NAME", "")),
                     raw_val(row.get("NOM3_RELATION", "")), raw_val(row.get("NOM3_ADDR1", "")),
                     raw_val(row.get("NOM3_ADDR2", "")), raw_val(row.get("NOM3_ADDR3", "")),
                     raw_val(row.get("NOM3_CITY", "")), raw_val(row.get("NOM3_STATE", "")),
                     raw_val(row.get("NOM3_PINCODE", "")), raw_val(row.get("NOM3_PH_OFF", "")),
                     raw_val(row.get("NOM3_PH_RES", "")), raw_val(row.get("NOM3_EMAIL", "")),
                     raw_val(row.get("NOM3_PERCENTAGE", "")), raw_val(row.get("IFSC_CODE", "")),
                     raw_val(row.get("DP_ID", "")), raw_val(row.get("DEMAT", "")), raw_val(row.get("GUARD_NAME", "")),
                     raw_val(row.get("BROKCODE", "")), raw_val(row.get("FOLIO_DATE", "")),
                     raw_val(row.get("AADHAAR", "")), raw_val(row.get("TPA_LINKED", "")),
                     raw_val(row.get("FH_CKYC_NO", "")), raw_val(row.get("JH1_CKYC", "")),
                     raw_val(row.get("JH2_CKYC", "")), raw_val(row.get("G_CKYC_NO", "")),
                     raw_val(row.get("JH1_DOB", "")), raw_val(row.get("JH2_DOB", "")),
                     raw_val(row.get("GUARDIAN_DOB", "")), raw_val(row.get("AMC_CODE", "")),
                     raw_val(row.get("GST_STATE_CODE", "")), raw_val(row.get("FOLIO_OLD", "")),
                     raw_val(row.get("SCHEME_FOLIO_NUMBER", "")), raw_val(row.get("COUNTRY", "")),
                     raw_val(row.get("REMARKS", "")), raw_val(row.get("JH1_EMAIL", "")),
                     raw_val(row.get("JH2_EMAIL", "")), raw_val(row.get("JH1_MOBILE_NO", "")),
                     raw_val(row.get("JH2_MOBILE_NO", "")), batch))
    return _execute_cams_import("cams_wbr9_folio", rows, batch, replace, skipped)


def parse_cams_wbr2_transaction(file, replace: bool) -> tuple[bool, str, dict]:
    df = _read_csv_auto(file)
    if df is None: return False, "Could not parse file", {}
    df = _clean_cols(df)
    if "TRXNNO" not in df.columns: return False, f"Missing TRXNNO. Found: {list(df.columns)[:10]}", {}
    batch = _batch_id("CAMS_R2", file.name);
    rows, skipped = [], 0
    for _, row in df.iterrows():
        trxnno = raw_val(row.get("TRXNNO", ""))
        if not trxnno: skipped += 1; continue
        rows.append((raw_val(row.get("AMC_CODE", "")), raw_val(row.get("FOLIO_NO", "")),
                     raw_val(row.get("PRODCODE", "")), raw_val(row.get("SCHEME", "")), raw_val(row.get("INV_NAME", "")),
                     raw_val(row.get("TRXNTYPE", "")), trxnno, raw_val(row.get("TRXNMODE", "")),
                     raw_val(row.get("TRXNSTAT", "")), raw_val(row.get("USERCODE", "")),
                     raw_val(row.get("USRTRXNO", "")), raw_val(row.get("TRADDATE", "")),
                     raw_val(row.get("POSTDATE", "")), raw_val(row.get("PURPRICE", "")), raw_val(row.get("UNITS", "")),
                     raw_val(row.get("AMOUNT", "")), raw_val(row.get("BROKCODE", "")), raw_val(row.get("SUBBROK", "")),
                     raw_val(row.get("BROKPERC", "")), raw_val(row.get("BROKCOMM", "")),
                     raw_val(row.get("ALTFOLIO", "")), raw_val(row.get("REP_DATE", "")), raw_val(row.get("TIME1", "")),
                     raw_val(row.get("TRXNSUBTYP", "")), raw_val(row.get("APPLICATION_NO", "")),
                     raw_val(row.get("TRXN_NATURE", "")), raw_val(row.get("TAX", "")),
                     raw_val(row.get("TOTAL_TAX", "")), raw_val(row.get("TE_15H", "")), raw_val(row.get("MICR_NO", "")),
                     raw_val(row.get("REMARKS", "")), raw_val(row.get("SWFLAG", "")), raw_val(row.get("OLD_FOLIO", "")),
                     raw_val(row.get("SEQ_NO", "")), raw_val(row.get("REINVEST_FLAG", "")),
                     raw_val(row.get("MULT_BROK", "")), raw_val(row.get("STT", "")), raw_val(row.get("LOCATION", "")),
                     raw_val(row.get("SCHEME_TYPE", "")), raw_val(row.get("TAX_STATUS", "")),
                     raw_val(row.get("LOAD", "")), raw_val(row.get("SCANREFNO", "")), raw_val(row.get("PAN", "")),
                     raw_val(row.get("INV_IIN", "")), raw_val(row.get("TARG_SRC_SCHEME", "")),
                     raw_val(row.get("TRXN_TYPE_FLAG", "")), raw_val(row.get("TICOB_TRTYPE", "")),
                     raw_val(row.get("TICOB_TRNO", "")), raw_val(row.get("TICOB_POSTED_DATE", "")),
                     raw_val(row.get("DP_ID", "")), raw_val(row.get("TRXN_CHARGES", "")),
                     raw_val(row.get("ELIGIB_AMT", "")), raw_val(row.get("SRC_OF_TXN", "")),
                     raw_val(row.get("TRXN_SUFFIX", "")), raw_val(row.get("SIPTRXNO", "")),
                     raw_val(row.get("TER_LOCATION", "")), raw_val(row.get("EUIN", "")),
                     raw_val(row.get("EUIN_VALID", "")), raw_val(row.get("EUIN_OPTED", "")),
                     raw_val(row.get("SUB_BRK_ARN", "")), raw_val(row.get("EXCH_DC_FLAG", "")),
                     raw_val(row.get("SRC_BRK_CODE", "")), raw_val(row.get("SYS_REGN_DATE", "")),
                     raw_val(row.get("AC_NO", "")), raw_val(row.get("BANK_NAME", "")),
                     raw_val(row.get("REVERSAL_CODE", "")), raw_val(row.get("EXCHANGE_FLAG", "")),
                     raw_val(row.get("CA_INITIATED_DATE", "")), raw_val(row.get("GST_STATE_CODE", "")),
                     raw_val(row.get("IGST_AMOUNT", "")), raw_val(row.get("CGST_AMOUNT", "")),
                     raw_val(row.get("SGST_AMOUNT", "")), raw_val(row.get("REV_REMARK", "")),
                     raw_val(row.get("ORIGINAL_TRXNNO", "")), raw_val(row.get("STAMP_DUTY", "")),
                     raw_val(row.get("FOLIO_OLD", "")), raw_val(row.get("SCHEME_FOLIO_NUMBER", "")),
                     raw_val(row.get("AMC_REF_NO", "")), raw_val(row.get("REQUEST_REF_NO", "")),
                     raw_val(row.get("TRANSMISSION_FLAG", "")), batch))
    return _execute_cams_import("cams_wbr2_transaction", rows, batch, replace, skipped)


def parse_cams_wbr49_sip(file, replace: bool) -> tuple[bool, str, dict]:
    df = _read_csv_auto(file)
    if df is None: return False, "Could not parse file", {}
    df = _clean_cols(df)
    if "AUTO_TRNO" not in df.columns: return False, f"Missing AUTO_TRNO. Found: {list(df.columns)[:10]}", {}
    batch = _batch_id("CAMS_R49", file.name);
    rows, skipped = [], 0
    for _, row in df.iterrows():
        trno = raw_val(row.get("AUTO_TRNO", ""))
        if not trno: skipped += 1; continue
        rows.append((raw_val(row.get("PRODUCT", "")), raw_val(row.get("SCHEME", "")), raw_val(row.get("FOLIO_NO", "")),
                     raw_val(row.get("INV_NAME", "")), raw_val(row.get("AUT_TRNTYP", "")), trno,
                     raw_val(row.get("AUTO_AMOUNT", "")), raw_val(row.get("FROM_DATE", "")),
                     raw_val(row.get("TO_DATE", "")), raw_val(row.get("CEASE_DATE", "")),
                     raw_val(row.get("PERIODICITY", "")), raw_val(row.get("PERIOD_DAY", "")),
                     raw_val(row.get("INV_IIN", "")), raw_val(row.get("PAYMENT_MODE", "")),
                     raw_val(row.get("TARGET_SCHEME", "")), raw_val(row.get("REG_DATE", "")),
                     raw_val(row.get("SUBBROKER", "")), raw_val(row.get("REMARKS", "")),
                     raw_val(row.get("TOP_UP_FRQ", "")), raw_val(row.get("TOP_UP_AMT", "")),
                     raw_val(row.get("AC_TYPE", "")), raw_val(row.get("BANK", "")), raw_val(row.get("BRANCH", "")),
                     raw_val(row.get("INSTRM_NO", "")), raw_val(row.get("CHEQ_MICR_NO", "")),
                     raw_val(row.get("AC_HOLDER_NAME", "")), raw_val(row.get("PAN", "")),
                     raw_val(row.get("TOP_UP_PERC", "")), raw_val(row.get("EUIN", "")),
                     raw_val(row.get("SUB_ARN_CODE", "")), raw_val(row.get("TER_LOCATION", "")),
                     raw_val(row.get("SCHEME_CODE", "")), raw_val(row.get("TARGET_SCHEME_CODE", "")),
                     raw_val(row.get("AMC_CODE", "")), raw_val(row.get("USER_CODE", "")),
                     raw_val(row.get("PACKAGE_NAME", "")), raw_val(row.get("SPECIAL_PRODUCT", "")),
                     raw_val(row.get("SUBTRXNDESC", "")), raw_val(row.get("PAUSE_FROM_DATE", "")),
                     raw_val(row.get("PAUSE_TO_DATE", "")), raw_val(row.get("FOLIO_OLD", "")),
                     raw_val(row.get("FT_SIP_REGNO", "")), raw_val(row.get("SCHEME_FOLIO_NUMBER", "")),
                     raw_val(row.get("REQUEST_REF_NO", "")), batch))
    return _execute_cams_import("cams_wbr49_sip", rows, batch, replace, skipped)


def parse_cams_wbr77_brokerage(file, replace: bool) -> tuple[bool, str, dict]:
    df = _read_csv_auto(file)
    if df is None: return False, "Could not parse file", {}
    df = _clean_cols(df)
    if "TRXN_NO" not in df.columns: return False, f"Missing TRXN_NO. Found: {list(df.columns)[:10]}", {}
    batch = _batch_id("CAMS_R77", file.name);
    rows, skipped = [], 0
    for _, row in df.iterrows():
        trxn_no = raw_val(row.get("TRXN_NO", ""))
        if not trxn_no: skipped += 1; continue
        rows.append((raw_val(row.get("AMC_CODE", "")), raw_val(row.get("PROC_DATE", "")),
                     raw_val(row.get("FOLIO_NO", "")), raw_val(row.get("SCHEME_CODE", "")),
                     raw_val(row.get("TRXN_TYPE", "")), trxn_no, raw_val(row.get("PLOT_AMOUNT", "")),
                     raw_val(row.get("PLOT_UNITS", "")), raw_val(row.get("POST_DATE", "")),
                     raw_val(row.get("TRADE_DATE_TIME", "")), raw_val(row.get("ENTRY_DATE", "")),
                     raw_val(row.get("USER_CODE", "")), raw_val(row.get("USER_TRXNNO", "")),
                     raw_val(row.get("TRXN_NATURE", "")), raw_val(row.get("TER_LOCATION", "")),
                     raw_val(row.get("SYS_REG_DATE", "")), raw_val(row.get("AUT_TXN_NO", "")),
                     raw_val(row.get("AUTO_AMOUNT", "")), raw_val(row.get("AUT_TXN_TYPE", "")),
                     raw_val(row.get("CEASE_DATE", "")), raw_val(row.get("REMED_DATE", "")),
                     raw_val(row.get("FORF_DATE", "")), raw_val(row.get("SRC_BRK_CODE", "")),
                     raw_val(row.get("BROK_CODE", "")), raw_val(row.get("BRH_CODE", "")),
                     raw_val(row.get("SUB_BRK_ARN", "")), raw_val(row.get("AE_CODE", "")),
                     raw_val(row.get("ARN_EMP_CODE", "")), raw_val(row.get("EUIN_OPTED", "")),
                     raw_val(row.get("EUIN_VALID", "")), raw_val(row.get("BRK_COMM_PAID", "")),
                     raw_val(row.get("ADJ_FLAG", "")), raw_val(row.get("BRKAGE_TYPE", "")),
                     raw_val(row.get("BRKAGE_RATE", "")), raw_val(row.get("TOTAL_UPFRONT", "")),
                     raw_val(row.get("DEFER_FREQUENCY", "")), raw_val(row.get("DEFER_NO_OF_INSTALLMENT", "")),
                     raw_val(row.get("PAY_INSTALLMENT_NO", "")), raw_val(row.get("BRKAGE_AMT", "")),
                     raw_val(row.get("BRKAGE_FROM", "")), raw_val(row.get("BRKAGE_TO", "")),
                     raw_val(row.get("PROC_FROM_DATE", "")), raw_val(row.get("PROC_TO_DATE", "")),
                     raw_val(row.get("TRXN_DESC", "")), raw_val(row.get("SPL_UPF_TENURE", "")),
                     raw_val(row.get("UPF_TENURE_END_DATE", "")), raw_val(row.get("BRK_PAY_DT", "")),
                     raw_val(row.get("CLW_TYPE", "")), raw_val(row.get("CLW_PERIOD", "")),
                     raw_val(row.get("REC_FLAG", "")), raw_val(row.get("P_SI_DATE", "")),
                     raw_val(row.get("REC_PERIOD", "")), raw_val(row.get("CLW_AMT", "")),
                     raw_val(row.get("UPF_PAID", "")), raw_val(row.get("FEE_ID", "")), raw_val(row.get("AM_CODE", "")),
                     raw_val(row.get("AM_COMM", "")), raw_val(row.get("AM_RATE", "")),
                     raw_val(row.get("AVG_ASSETS", "")), raw_val(row.get("CAM_COMM", "")),
                     raw_val(row.get("CAM_RATE", "")), raw_val(row.get("MAM_COMM", "")),
                     raw_val(row.get("MAM_RATE", "")), raw_val(row.get("NO_OF_DAYS", "")),
                     raw_val(row.get("ORIG_AE_CODE", "")), raw_val(row.get("ORIG_BRH_CODE", "")),
                     raw_val(row.get("ORIG_BRK_CODE", "")), raw_val(row.get("RATE_REF_ID", "")),
                     raw_val(row.get("REF_NO", "")), raw_val(row.get("TRXN_APP_NO", "")),
                     raw_val(row.get("TXN_SCH_CODE", "")), raw_val(row.get("CLW_PRD", "")),
                     raw_val(row.get("CLW_REQUIRED", "")), raw_val(row.get("P_SI_MIS_CODE", "")),
                     raw_val(row.get("P_SI_USER_TRXNO", "")), raw_val(row.get("SEQ_NO", "")),
                     raw_val(row.get("P_SI_AMT", "")), raw_val(row.get("P_SI_TR_NO", "")),
                     raw_val(row.get("P_SI_TYPE", "")), raw_val(row.get("PUR_SI_UNITS", "")),
                     raw_val(row.get("REMARKS", "")), raw_val(row.get("TO_SCHEME", "")),
                     raw_val(row.get("TRXN_SIGN", "")), raw_val(row.get("BRK_POSTED", "")),
                     raw_val(row.get("INV_NAME", "")), raw_val(row.get("BROK_GST_STATE_CODE", "")),
                     raw_val(row.get("IGST_RATE", "")), raw_val(row.get("CGST_RATE", "")),
                     raw_val(row.get("SGST_RATE", "")), raw_val(row.get("IGST_VALUE", "")),
                     raw_val(row.get("CGST_VALUE", "")), raw_val(row.get("SGST_VALUE", "")),
                     raw_val(row.get("LOCATION_CODE", "")), raw_val(row.get("PREV_FOLIO", "")),
                     raw_val(row.get("BROK_CATEGORY", "")), raw_val(row.get("P_SCHEME_CODE", "")),
                     raw_val(row.get("P_TRXN_TYPE", "")), raw_val(row.get("P_TRXN_NO", "")),
                     raw_val(row.get("P_FOLIO_NO", "")), raw_val(row.get("P_PLOT_AMOUNT", "")),
                     raw_val(row.get("P_PLOT_UNITS", "")), raw_val(row.get("FOLIO_OLD", "")),
                     raw_val(row.get("SCHEME_FOLIO_NUMBER", "")), raw_val(row.get("AMC_REF_NO", "")),
                     raw_val(row.get("REQUEST_REF_NO", "")), raw_val(row.get("WRITE_OFF_REASON", "")),
                     raw_val(row.get("HOLD_REASON", "")), raw_val(row.get("BROKERAGE_ACCRUAL_MONTH", "")),
                     raw_val(row.get("PREV_TRXN_NO", "")), raw_val(row.get("PREV_TRXN_DATE", "")), batch))
    return _execute_cams_import("cams_wbr77_brokerage", rows, batch, replace, skipped)


def _execute_cams_import(table: str, rows: list, batch: str, replace: bool, skipped: int) -> tuple[bool, str, dict]:
    if not rows: return False, "0 rows found", {}
    cols = ", ".join([f[0] for f in table_column_map[table]])
    placeholders = ", ".join(["?"] * len(table_column_map[table]))
    sql = f"INSERT OR IGNORE INTO {table} ({cols}, upload_batch) VALUES ({placeholders}, ?)"
    inserted = dupes = 0
    with get_conn() as conn:
        if replace:
            conn.execute(f"DELETE FROM {table}");
            conn.executemany(sql, rows);
            inserted = len(rows)
        else:
            before = _count_before_after(conn, table);
            conn.executemany(sql, rows);
            inserted, dupes = _inserted_dupes(
                before, conn, table, len(rows))
    msg = f"Imported {inserted} into {table}"
    if skipped:
        msg += f" | Skipped {skipped}"
    if dupes:
        msg += f" | {dupes} duplicates"
    return True, msg, {"rows": inserted, "skipped": skipped, "duplicates": dupes}


table_column_map = {
    "cams_wbr4_aum": [("FOLIOCHK", "INV_NAME", "ADDRESS1", "ADDRESS2", "ADDRESS3", "CITY", "PINCODE", "PRODUCT",
                       "SCH_NAME", "REP_DATE", "CLOS_BAL", "RUPEE_BAL", "SUBBROK", "REINV_FLAG", "JOINT1_NAME",
                       "JOINT2_NAME", "PHONE_OFF", "PHONE_RES", "EMAIL", "HOLDING_NATURE", "UIN_NO", "BROKER_CODE",
                       "PAN_NO", "JOINT1_PAN", "JOINT2_PAN", "GUARD_PAN", "TAX_STATUS", "INV_IIN", "ALTFOLIO", "EUIN",
                       "EXCHANGE_FLAG", "TPA_LINKED", "FH_CKYC_NO", "JH1_CKYC", "JH2_CKYC", "G_CKYC_NO", "JH1_DOB",
                       "JH2_DOB", "GUARDIAN_DOB", "AMC_CODE", "GST_STATE_CODE", "FOLIO_OLD", "SCHEME_FOLIO_NUMBER",
                       "COUNTRY")],
    "cams_wbr9_folio": [("FOLIOCHK", "INV_NAME", "ADDRESS1", "ADDRESS2", "ADDRESS3", "CITY", "PINCODE", "PRODUCT",
                         "SCH_NAME", "REP_DATE", "CLOS_BAL", "RUPEE_BAL", "JNT_NAME1", "JNT_NAME2", "PHONE_OFF",
                         "PHONE_RES", "EMAIL", "HOLDING_NATURE", "UIN_NO", "PAN_NO", "JOINT1_PAN", "JOINT2_PAN",
                         "GUARD_PAN", "TAX_STATUS", "BROKER_CODE", "SUBBROKER", "REINV_FLAG", "BANK_NAME", "BRANCH",
                         "AC_TYPE", "AC_NO", "B_ADDRESS1", "B_ADDRESS2", "B_ADDRESS3", "B_CITY", "B_PINCODE", "INV_DOB",
                         "MOBILE_NO", "OCCUPATION", "INV_IIN", "NOM_NAME", "RELATION", "NOM_ADDR1", "NOM_ADDR2",
                         "NOM_ADDR3", "NOM_CITY", "NOM_STATE", "NOM_PINCODE", "NOM_PH_OFF", "NOM_PH_RES", "NOM_EMAIL",
                         "NOM_PERCENTAGE", "NOM2_NAME", "NOM2_RELATION", "NOM2_ADDR1", "NOM2_ADDR2", "NOM2_ADDR3",
                         "NOM2_CITY", "NOM2_STATE", "NOM2_PINCODE", "NOM2_PH_OFF", "NOM2_PH_RES", "NOM2_EMAIL",
                         "NOM2_PERCENTAGE", "NOM3_NAME", "NOM3_RELATION", "NOM3_ADDR1", "NOM3_ADDR2", "NOM3_ADDR3",
                         "NOM3_CITY", "NOM3_STATE", "NOM3_PINCODE", "NOM3_PH_OFF", "NOM3_PH_RES", "NOM3_EMAIL",
                         "NOM3_PERCENTAGE", "IFSC_CODE", "DP_ID", "DEMAT", "GUARD_NAME", "BROKCODE", "FOLIO_DATE",
                         "AADHAAR", "TPA_LINKED", "FH_CKYC_NO", "JH1_CKYC", "JH2_CKYC", "G_CKYC_NO", "JH1_DOB",
                         "JH2_DOB", "GUARDIAN_DOB", "AMC_CODE", "GST_STATE_CODE", "FOLIO_OLD", "SCHEME_FOLIO_NUMBER",
                         "COUNTRY", "REMARKS", "JH1_EMAIL", "JH2_EMAIL", "JH1_MOBILE_NO", "JH2_MOBILE_NO")],
    "cams_wbr2_transaction": [("AMC_CODE", "FOLIO_NO", "PRODCODE", "SCHEME", "INV_NAME", "TRXNTYPE", "TRXNNO",
                               "TRXNMODE", "TRXNSTAT", "USERCODE", "USRTRXNO", "TRADDATE", "POSTDATE", "PURPRICE",
                               "UNITS", "AMOUNT", "BROKCODE", "SUBBROK", "BROKPERC", "BROKCOMM", "ALTFOLIO", "REP_DATE",
                               "TIME1", "TRXNSUBTYP", "APPLICATION_NO", "TRXN_NATURE", "TAX", "TOTAL_TAX", "TE_15H",
                               "MICR_NO", "REMARKS", "SWFLAG", "OLD_FOLIO", "SEQ_NO", "REINVEST_FLAG", "MULT_BROK",
                               "STT", "LOCATION", "SCHEME_TYPE", "TAX_STATUS", "LOAD", "SCANREFNO", "PAN", "INV_IIN",
                               "TARG_SRC_SCHEME", "TRXN_TYPE_FLAG", "TICOB_TRTYPE", "TICOB_TRNO", "TICOB_POSTED_DATE",
                               "DP_ID", "TRXN_CHARGES", "ELIGIB_AMT", "SRC_OF_TXN", "TRXN_SUFFIX", "SIPTRXNO",
                               "TER_LOCATION", "EUIN", "EUIN_VALID", "EUIN_OPTED", "SUB_BRK_ARN", "EXCH_DC_FLAG",
                               "SRC_BRK_CODE", "SYS_REGN_DATE", "AC_NO", "BANK_NAME", "REVERSAL_CODE", "EXCHANGE_FLAG",
                               "CA_INITIATED_DATE", "GST_STATE_CODE", "IGST_AMOUNT", "CGST_AMOUNT", "SGST_AMOUNT",
                               "REV_REMARK", "ORIGINAL_TRXNNO", "STAMP_DUTY", "FOLIO_OLD", "SCHEME_FOLIO_NUMBER",
                               "AMC_REF_NO", "REQUEST_REF_NO", "TRANSMISSION_FLAG")],
    "cams_wbr49_sip": [("PRODUCT", "SCHEME", "FOLIO_NO", "INV_NAME", "AUT_TRNTYP", "AUTO_TRNO", "AUTO_AMOUNT",
                        "FROM_DATE", "TO_DATE", "CEASE_DATE", "PERIODICITY", "PERIOD_DAY", "INV_IIN", "PAYMENT_MODE",
                        "TARGET_SCHEME", "REG_DATE", "SUBBROKER", "REMARKS", "TOP_UP_FRQ", "TOP_UP_AMT", "AC_TYPE",
                        "BANK", "BRANCH", "INSTRM_NO", "CHEQ_MICR_NO", "AC_HOLDER_NAME", "PAN", "TOP_UP_PERC", "EUIN",
                        "SUB_ARN_CODE", "TER_LOCATION", "SCHEME_CODE", "TARGET_SCHEME_CODE", "AMC_CODE", "USER_CODE",
                        "PACKAGE_NAME", "SPECIAL_PRODUCT", "SUBTRXNDESC", "PAUSE_FROM_DATE", "PAUSE_TO_DATE",
                        "FOLIO_OLD", "FT_SIP_REGNO", "SCHEME_FOLIO_NUMBER", "REQUEST_REF_NO")],
    "cams_wbr77_brokerage": [("AMC_CODE", "PROC_DATE", "FOLIO_NO", "SCHEME_CODE", "TRXN_TYPE", "TRXN_NO", "PLOT_AMOUNT",
                              "PLOT_UNITS", "POST_DATE", "TRADE_DATE_TIME", "ENTRY_DATE", "USER_CODE", "USER_TRXNNO",
                              "TRXN_NATURE", "TER_LOCATION", "SYS_REG_DATE", "AUT_TXN_NO", "AUTO_AMOUNT",
                              "AUT_TXN_TYPE", "CEASE_DATE", "REMED_DATE", "FORF_DATE", "SRC_BRK_CODE", "BROK_CODE",
                              "BRH_CODE", "SUB_BRK_ARN", "AE_CODE", "ARN_EMP_CODE", "EUIN_OPTED", "EUIN_VALID",
                              "BRK_COMM_PAID", "ADJ_FLAG", "BRKAGE_TYPE", "BRKAGE_RATE", "TOTAL_UPFRONT",
                              "DEFER_FREQUENCY", "DEFER_NO_OF_INSTALLMENT", "PAY_INSTALLMENT_NO", "BRKAGE_AMT",
                              "BRKAGE_FROM", "BRKAGE_TO", "PROC_FROM_DATE", "PROC_TO_DATE", "TRXN_DESC",
                              "SPL_UPF_TENURE", "UPF_TENURE_END_DATE", "BRK_PAY_DT", "CLW_TYPE", "CLW_PERIOD",
                              "REC_FLAG", "P_SI_DATE", "REC_PERIOD", "CLW_AMT", "UPF_PAID", "FEE_ID", "AM_CODE",
                              "AM_COMM", "AM_RATE", "AVG_ASSETS", "CAM_COMM", "CAM_RATE", "MAM_COMM", "MAM_RATE",
                              "NO_OF_DAYS", "ORIG_AE_CODE", "ORIG_BRH_CODE", "ORIG_BRK_CODE", "RATE_REF_ID", "REF_NO",
                              "TRXN_APP_NO", "TXN_SCH_CODE", "CLW_PRD", "CLW_REQUIRED", "P_SI_MIS_CODE",
                              "P_SI_USER_TRXNO", "SEQ_NO", "P_SI_AMT", "P_SI_TR_NO", "P_SI_TYPE", "PUR_SI_UNITS",
                              "REMARKS", "TO_SCHEME", "TRXN_SIGN", "BRK_POSTED", "INV_NAME", "BROK_GST_STATE_CODE",
                              "IGST_RATE", "CGST_RATE", "SGST_RATE", "IGST_VALUE", "CGST_VALUE", "SGST_VALUE",
                              "LOCATION_CODE", "PREV_FOLIO", "BROK_CATEGORY", "P_SCHEME_CODE", "P_TRXN_TYPE",
                              "P_TRXN_NO", "P_FOLIO_NO", "P_PLOT_AMOUNT", "P_PLOT_UNITS", "FOLIO_OLD",
                              "SCHEME_FOLIO_NUMBER", "AMC_REF_NO", "REQUEST_REF_NO", "WRITE_OFF_REASON", "HOLD_REASON",
                              "BROKERAGE_ACCRUAL_MONTH", "PREV_TRXN_NO", "PREV_TRXN_DATE")]
}


# ══════════════════════════════════════════════════════════════════════════════
# KFINTECH PARSERS (Raw Inserts)
# ══════════════════════════════════════════════════════════════════════════════

def parse_kfin_mfsd203_aum(file, replace: bool) -> tuple[bool, str, dict]:
    df = _read_csv_auto(file)
    if df is None: return False, "Could not parse file", {}
    df = _clean_cols(df)
    if "FOLIO_NUMBER" not in df.columns: return False, "Missing FOLIO_NUMBER", {}
    batch = _batch_id("KFIN_203", file.name);
    rows, skipped = [], 0
    for _, row in df.iterrows():
        folio = raw_val(row.get("FOLIO_NUMBER", ""))
        if not folio: skipped += 1; continue
        rows.append((raw_val(row.get("PRODUCT_CODE", "")), raw_val(row.get("FUND", "")), folio,
                     raw_val(row.get("SCHEME_CODE", "")), raw_val(row.get("DIVIDEND_OPTION", "")),
                     raw_val(row.get("FUND_DESCRIPTION", "")), raw_val(row.get("BALANCE", "")),
                     raw_val(row.get("PLEDGED", "")), raw_val(row.get("TRANSACTION_DATE", "")),
                     raw_val(row.get("TRANSACTION_TYPE", "")), raw_val(row.get("HOLD_MODE", "")),
                     raw_val(row.get("AGENT_CODE", "")), raw_val(row.get("BROKER_CODE", "")),
                     raw_val(row.get("P_OUT_CODE", "")), raw_val(row.get("INVESTOR_ID", "")),
                     raw_val(row.get("INVESTOR_NAME", "")), raw_val(row.get("ADDRESS1", "")),
                     raw_val(row.get("ADDRESS2", "")), raw_val(row.get("ADDRESS3", "")), raw_val(row.get("CITY", "")),
                     raw_val(row.get("PINCODE", "")), raw_val(row.get("PHONE_RESIDENCE", "")),
                     raw_val(row.get("PHONE_OFFICE", "")), raw_val(row.get("FAX", "")), raw_val(row.get("EMAIL", "")),
                     raw_val(row.get("AUM", "")), raw_val(row.get("NAV", "")), raw_val(row.get("REPORT_DATE", "")),
                     raw_val(row.get("REPORT_TIME", "")), raw_val(row.get("TO_DATE", "")), batch))
    return _execute_kfin_import("kfin_mfsd203_aum", rows, batch, replace, skipped)


def parse_kfin_mfsd211_folio(file, replace: bool) -> tuple[bool, str, dict]:
    df = _read_csv_auto(file)
    if df is None: return False, "Could not parse file", {}
    df = _clean_cols(df)
    rename_map = {"JOINT_NAME_1": "JOINT_NAME1", "JOINT_NAME_2": "JOINT_NAME2", "PHONE_RES_1": "PHONE_RES1",
                  "PHONE_RES_2": "PHONE_RES2", "PHONE_OFF_1": "PHONE_OFF1", "PHONE_OFF_2": "PHONE_OFF2",
                  "BANKACCNO": "BANK_ACCNO", "HOLDER_1_AADHAAR_INFO": "HOLDER1_AADHAAR",
                  "HOLDER_2_AADHAAR_INFO": "HOLDER2_AADHAAR", "HOLDER_3_AADHAAR_INFO": "HOLDER3_AADHAAR",
                  "GUARDIAN_AADHAAR_INFO": "GUARDIAN_AADHAAR"}
    df.rename(columns=rename_map, inplace=True)
    if "FOLIO" not in df.columns: return False, "Missing FOLIO", {}
    batch = _batch_id("KFIN_211", file.name);
    rows, skipped = [], 0
    for _, row in df.iterrows():
        folio = raw_val(row.get("FOLIO", ""))
        if not folio: skipped += 1; continue
        rows.append((raw_val(row.get("PRODUCT_CODE", "")), raw_val(row.get("FUND", "")), folio,
                     raw_val(row.get("DIVIDEND_OPTION", "")), raw_val(row.get("FUND_DESCRIPTION", "")),
                     raw_val(row.get("INVESTOR_NAME", "")), raw_val(row.get("JOINT_NAME1", "")),
                     raw_val(row.get("JOINT_NAME2", "")), raw_val(row.get("ADDRESS1", "")),
                     raw_val(row.get("ADDRESS2", "")), raw_val(row.get("ADDRESS3", "")), raw_val(row.get("CITY", "")),
                     raw_val(row.get("PINCODE", "")), raw_val(row.get("STATE", "")), raw_val(row.get("COUNTRY", "")),
                     raw_val(row.get("TPIN", "")), raw_val(row.get("DATE_OF_BIRTH", "")),
                     raw_val(row.get("F_NAME", "")), raw_val(row.get("M_NAME", "")),
                     raw_val(row.get("PHONE_RESIDENCE", "")), raw_val(row.get("PHONE_RES1", "")),
                     raw_val(row.get("PHONE_RES2", "")), raw_val(row.get("PHONE_OFFICE", "")),
                     raw_val(row.get("PHONE_OFF1", "")), raw_val(row.get("PHONE_OFF2", "")),
                     raw_val(row.get("FAX_RESIDENCE", "")), raw_val(row.get("FAX_OFFICE", "")),
                     raw_val(row.get("TAX_STATUS", "")), raw_val(row.get("OCC_CODE", "")),
                     raw_val(row.get("EMAIL", "")), raw_val(row.get("BANK_ACCNO", "")),
                     raw_val(row.get("BANK_NAME", "")), raw_val(row.get("ACCOUNT_TYPE", "")),
                     raw_val(row.get("BRANCH", "")), raw_val(row.get("BANK_ADDRESS1", "")),
                     raw_val(row.get("BANK_ADDRESS2", "")), raw_val(row.get("BANK_ADDRESS3", "")),
                     raw_val(row.get("BANK_CITY", "")), raw_val(row.get("BANK_PHONE", "")),
                     raw_val(row.get("BANK_STATE", "")), raw_val(row.get("BANK_COUNTRY", "")),
                     raw_val(row.get("INVESTOR_ID", "")), raw_val(row.get("BROKER_CODE", "")),
                     raw_val(row.get("PAN_NUMBER", "")), raw_val(row.get("MOBILE_NUMBER", "")),
                     raw_val(row.get("REPORT_DATE", "")), raw_val(row.get("REPORT_TIME", "")),
                     raw_val(row.get("OCCUPATION_DESCRIPTION", "")), raw_val(row.get("MODE_OF_HOLDING", "")),
                     raw_val(row.get("MODE_OF_HOLDING_DESCRIPTION", "")), raw_val(row.get("MAPIN_ID", "")),
                     raw_val(row.get("HOLDER1_AADHAAR", "")), raw_val(row.get("HOLDER2_AADHAAR", "")),
                     raw_val(row.get("HOLDER3_AADHAAR", "")), raw_val(row.get("GUARDIAN_AADHAAR", "")), batch))
    return _execute_kfin_import("kfin_mfsd211_folio", rows, batch, replace, skipped)


def parse_kfin_mfsd201_transaction(file, replace: bool) -> tuple[bool, str, dict]:
    df = _read_csv_auto(file)
    if df is None: return False, "Could not parse file", {}
    df = _clean_cols(df)
    if "TD_TRNO" not in df.columns: return False, "Missing TD_TRNO", {}
    batch = _batch_id("KFIN_201", file.name);
    rows, skipped = [], 0
    for _, row in df.iterrows():
        trno = raw_val(row.get("TD_TRNO", ""))
        if not trno: skipped += 1; continue
        rows.append((raw_val(row.get("FMCODE", "")), raw_val(row.get("TD_FUND", "")), raw_val(row.get("TD_ACNO", "")),
                     raw_val(row.get("SCHPLN", "")), raw_val(row.get("DIVOPT", "")), raw_val(row.get("FUNDDESC", "")),
                     raw_val(row.get("TD_PURRED", "")), trno, raw_val(row.get("SMCODE", "")),
                     raw_val(row.get("CHQNO", "")), raw_val(row.get("INVNAME", "")), raw_val(row.get("TRNMODE", "")),
                     raw_val(row.get("TRNSTAT", "")), raw_val(row.get("TD_BRANCH", "")),
                     raw_val(row.get("ISCTRNO", "")), raw_val(row.get("TD_TRDT", "")), raw_val(row.get("TD_PRDT", "")),
                     raw_val(row.get("TD_POP", "")), raw_val(row.get("LOADPER", "")), raw_val(row.get("TD_UNITS", "")),
                     raw_val(row.get("TD_AMT", "")), raw_val(row.get("LOAD1", "")), raw_val(row.get("TD_AGENT", "")),
                     raw_val(row.get("TD_BROKER", "")), raw_val(row.get("BROKPER", "")),
                     raw_val(row.get("BROKCOMM", "")), raw_val(row.get("INVID", "")), raw_val(row.get("CRDATE", "")),
                     raw_val(row.get("CRTIME", "")), raw_val(row.get("TRNSUB", "")), raw_val(row.get("TD_APPNO", "")),
                     raw_val(row.get("UNQNO", "")), raw_val(row.get("TRDESC", "")), raw_val(row.get("TD_TRTYPE", "")),
                     raw_val(row.get("PURDATE", "")), raw_val(row.get("PURAMT", "")), raw_val(row.get("PURUNITS", "")),
                     raw_val(row.get("TRFLAG", "")), raw_val(row.get("SFUNDDT", "")), raw_val(row.get("CHQDATE", "")),
                     raw_val(row.get("CHQBANK", "")), raw_val(row.get("TD_NAV", "")), raw_val(row.get("TD_PTRNO", "")),
                     raw_val(row.get("STT", "")), raw_val(row.get("IHNO", "")), raw_val(row.get("BRANCHCODE", "")),
                     raw_val(row.get("INWARDNO", "")), raw_val(row.get("NCTREMARKS", "")), raw_val(row.get("PAN1", "")),
                     raw_val(row.get("TRCHARGES", "")), raw_val(row.get("SIPREGDT", "")),
                     raw_val(row.get("SIPREGSLNO", "")), raw_val(row.get("DIVPER", "")),
                     raw_val(row.get("GUARDPANNO", "")), raw_val(row.get("CAN", "")),
                     raw_val(row.get("EXCHORGTRTYPE", "")), raw_val(row.get("ELECTRXNFLAG", "")),
                     raw_val(row.get("CLEARED", "")), raw_val(row.get("INVSTATE", "")), batch))
    return _execute_kfin_import("kfin_mfsd201_transaction", rows, batch, replace, skipped)


def parse_kfin_mfsd243_sip(file, replace: bool) -> tuple[bool, str, dict]:
    df = _read_csv_auto(file)
    if df is None: return False, "Could not parse file", {}
    df = _clean_cols(df)
    rename_map = {"ECSBANKNAME": "ECS_BANK_NAME", "ECSACNO": "ECS_ACNO", "ECSHOLDERNAME": "ECS_HOLDER_NAME",
                  "REGSLNO": "REG_SLNO", "INVDPID": "INV_DP_ID", "INVCLIENTID": "INV_CLIENT_ID",
                  "DP_INVNAME": "DP_INV_NAME", "MODIFYFLAG": "MODIFY_FLAG"}
    df.rename(columns=rename_map, inplace=True)
    if "FOLIO" not in df.columns: return False, "Missing FOLIO", {}
    batch = _batch_id("KFIN_243", file.name);
    rows, skipped = [], 0
    for _, row in df.iterrows():
        folio = raw_val(row.get("FOLIO", ""))
        if not folio: skipped += 1; continue
        rows.append((raw_val(row.get("ZONE", "")), raw_val(row.get("BRANCH", "")), raw_val(row.get("LOCATION", "")),
                     raw_val(row.get("IHNO", "")), folio, raw_val(row.get("INVESTOR_NAME", "")),
                     raw_val(row.get("REGISTRATION_DATE", "")), raw_val(row.get("START_DATE", "")),
                     raw_val(row.get("END_DATE", "")), raw_val(row.get("NO_OF_INSTALLMENTS", "")),
                     raw_val(row.get("AMOUNT", "")), raw_val(row.get("SCHEME", "")), raw_val(row.get("PLAN", "")),
                     raw_val(row.get("AGENT_CODE", "")), raw_val(row.get("AGENT_NAME", "")),
                     raw_val(row.get("SUBBROKER", "")), raw_val(row.get("SCHEME_NAME", "")),
                     raw_val(row.get("PAN", "")), raw_val(row.get("SIP_TYPE", "")), raw_val(row.get("SIP_MODE", "")),
                     raw_val(row.get("FUND_CODE", "")), raw_val(row.get("PRODUCT_CODE", "")),
                     raw_val(row.get("FREQUENCY", "")), raw_val(row.get("TRTYPE", "")),
                     raw_val(row.get("TO_SCHEME", "")), raw_val(row.get("TO_PLAN", "")),
                     raw_val(row.get("TERMINATE_DATE", "")), raw_val(row.get("STATUS", "")),
                     raw_val(row.get("TO_PRODUCT_CODE", "")), raw_val(row.get("TO_SCHEME_NAME", "")),
                     raw_val(row.get("ECSNO", "")), raw_val(row.get("ECS_BANK_NAME", "")),
                     raw_val(row.get("ECS_ACNO", "")), raw_val(row.get("ECS_HOLDER_NAME", "")),
                     raw_val(row.get("REG_SLNO", "")), raw_val(row.get("INV_DP_ID", "")),
                     raw_val(row.get("INV_CLIENT_ID", "")), raw_val(row.get("DP_INV_NAME", "")),
                     raw_val(row.get("MODIFY_FLAG", "")), raw_val(row.get("UMRNCODE", "")), batch))
    return _execute_kfin_import("kfin_mfsd243_sip", rows, batch, replace, skipped)


def parse_kfin_mfsd205_brokerage(file, replace: bool) -> tuple[bool, str, dict]:
    df = _read_csv_auto(file)
    if df is None: return False, "Could not parse file", {}
    df = _clean_cols(df)
    rename_map = {"AMOUNT_RS": "AMOUNT", "BROKERAGE_RS": "BROKERAGE", "GROSSBROKERAGE": "GROSS_BROKERAGE",
                  "STTAMOUNT": "STT_AMOUNT", "EDUCESSAMOUNT": "EDUCESS_AMOUNT", "TRANTYPECODE": "TRAN_TYPE_CODE"}
    df.rename(columns=rename_map, inplace=True)
    if "TRANSACTION_NUMBER" not in df.columns: return False, "Missing TRANSACTION_NUMBER", {}
    batch = _batch_id("KFIN_205", file.name);
    rows, skipped = [], 0
    for _, row in df.iterrows():
        trxn_num = raw_val(row.get("TRANSACTION_NUMBER", ""))
        if not trxn_num: skipped += 1; continue
        rows.append((raw_val(row.get("PRODUCT_CODE", "")), raw_val(row.get("FUND_DESCRIPTION", "")),
                     raw_val(row.get("FUND", "")), raw_val(row.get("SCHEME", "")), raw_val(row.get("PLAN", "")),
                     raw_val(row.get("OPTION", "")), raw_val(row.get("ACCOUNT_NUMBER", "")),
                     raw_val(row.get("APPLICATION_NUMBER", "")), raw_val(row.get("INVESTOR_NAME", "")),
                     raw_val(row.get("ADDRESS1", "")), raw_val(row.get("ADDRESS2", "")),
                     raw_val(row.get("ADDRESS3", "")), raw_val(row.get("CITY", "")), raw_val(row.get("PINCODE", "")),
                     raw_val(row.get("TRANSACTION_DESCRIPTION", "")), raw_val(row.get("FROM_DATE", "")),
                     raw_val(row.get("TO_DATE", "")), raw_val(row.get("AMOUNT", "")), raw_val(row.get("UNITS", "")),
                     raw_val(row.get("TRANSACTION_DATE", "")), raw_val(row.get("PROCESS_DATE", "")),
                     raw_val(row.get("PERCENTAGE", "")), raw_val(row.get("BROKERAGE", "")),
                     raw_val(row.get("SUB_BROKER", "")), raw_val(row.get("ACCOUNT_TYPE", "")),
                     raw_val(row.get("BROKERAGE_HEAD", "")), raw_val(row.get("BROKERAGE_TYPE", "")), trxn_num,
                     raw_val(row.get("BRANCH_CODE", "")), raw_val(row.get("CHEQUE_NUMBER", "")),
                     raw_val(row.get("STARTING_DATE", "")), raw_val(row.get("ENDING_DATE", "")),
                     raw_val(row.get("WARRANT_NUMBER", "")), raw_val(row.get("WARRANT_DATE", "")),
                     raw_val(row.get("DAILY_PRODUCT", "")), raw_val(row.get("CUMULATIVE_NAV", "")),
                     raw_val(row.get("AVERAGE_ASSETS", "")), raw_val(row.get("TRANSACTION_ID", "")),
                     raw_val(row.get("SCHEME_CODE", "")), raw_val(row.get("TRANSACTION_HEAD", "")),
                     raw_val(row.get("FEE_TYPE", "")), raw_val(row.get("ADJUSTMENT_FLAG", "")),
                     raw_val(row.get("SWITCH_FLAG", "")), raw_val(row.get("GROSS_BROKERAGE", "")),
                     raw_val(row.get("STT_AMOUNT", "")), raw_val(row.get("EDUCESS_AMOUNT", "")),
                     raw_val(row.get("TRAN_TYPE_CODE", "")), batch))
    return _execute_kfin_import("kfin_mfsd205_brokerage", rows, batch, replace, skipped)


def _execute_kfin_import(table: str, rows: list, batch: str, replace: bool, skipped: int) -> tuple[bool, str, dict]:
    if not rows: return False, "0 rows found", {}
    cols = ", ".join([f[0] for f in kfin_table_column_map[table]])
    placeholders = ", ".join(["?"] * len(kfin_table_column_map[table]))
    sql = f"INSERT OR IGNORE INTO {table} ({cols}, upload_batch) VALUES ({placeholders}, ?)"
    inserted = dupes = 0
    with get_conn() as conn:
        if replace:
            conn.execute(f"DELETE FROM {table}");
            conn.executemany(sql, rows);
            inserted = len(rows)
        else:
            before = _count_before_after(conn, table);
            conn.executemany(sql, rows);
            inserted, dupes = _inserted_dupes(
                before, conn, table, len(rows))
    msg = f"Imported {inserted} into {table}"
    if skipped:
        msg += f" | Skipped {skipped}"
    if dupes:
        msg += f" | {dupes} duplicates"
    return True, msg, {"rows": inserted, "skipped": skipped, "duplicates": dupes}


kfin_table_column_map = {
    "kfin_mfsd203_aum": [("PRODUCT_CODE", "FUND", "FOLIO_NUMBER", "SCHEME_CODE", "DIVIDEND_OPTION", "FUND_DESCRIPTION",
                          "BALANCE", "PLEDGED", "TRANSACTION_DATE", "TRANSACTION_TYPE", "HOLD_MODE", "AGENT_CODE",
                          "BROKER_CODE", "P_OUT_CODE", "INVESTOR_ID", "INVESTOR_NAME", "ADDRESS1", "ADDRESS2",
                          "ADDRESS3", "CITY", "PINCODE", "PHONE_RESIDENCE", "PHONE_OFFICE", "FAX", "EMAIL", "AUM",
                          "NAV", "REPORT_DATE", "REPORT_TIME", "TO_DATE")],
    "kfin_mfsd211_folio": [("PRODUCT_CODE", "FUND", "FOLIO", "DIVIDEND_OPTION", "FUND_DESCRIPTION", "INVESTOR_NAME",
                            "JOINT_NAME1", "JOINT_NAME2", "ADDRESS1", "ADDRESS2", "ADDRESS3", "CITY", "PINCODE",
                            "STATE", "COUNTRY", "TPIN", "DATE_OF_BIRTH", "F_NAME", "M_NAME", "PHONE_RESIDENCE",
                            "PHONE_RES1", "PHONE_RES2", "PHONE_OFFICE", "PHONE_OFF1", "PHONE_OFF2", "FAX_RESIDENCE",
                            "FAX_OFFICE", "TAX_STATUS", "OCC_CODE", "EMAIL", "BANK_ACCNO", "BANK_NAME", "ACCOUNT_TYPE",
                            "BRANCH", "BANK_ADDRESS1", "BANK_ADDRESS2", "BANK_ADDRESS3", "BANK_CITY", "BANK_PHONE",
                            "BANK_STATE", "BANK_COUNTRY", "INVESTOR_ID", "BROKER_CODE", "PAN_NUMBER", "MOBILE_NUMBER",
                            "REPORT_DATE", "REPORT_TIME", "OCCUPATION_DESCRIPTION", "MODE_OF_HOLDING",
                            "MODE_OF_HOLDING_DESCRIPTION", "MAPIN_ID", "HOLDER1_AADHAAR", "HOLDER2_AADHAAR",
                            "HOLDER3_AADHAAR", "GUARDIAN_AADHAAR")],
    "kfin_mfsd201_transaction": [(
        "FMCODE", "TD_FUND", "TD_ACNO", "SCHPLN", "DIVOPT", "FUNDDESC", "TD_PURRED", "TD_TRNO",
        "SMCODE", "CHQNO", "INVNAME", "TRNMODE", "TRNSTAT", "TD_BRANCH", "ISCTRNO", "TD_TRDT",
        "TD_PRDT", "TD_POP", "LOADPER", "TD_UNITS", "TD_AMT", "LOAD1", "TD_AGENT", "TD_BROKER",
        "BROKPER", "BROKCOMM", "INVID", "CRDATE", "CRTIME", "TRNSUB", "TD_APPNO", "UNQNO",
        "TRDESC", "TD_TRTYPE", "PURDATE", "PURAMT", "PURUNITS", "TRFLAG", "SFUNDDT", "CHQDATE",
        "CHQBANK", "TD_NAV", "TD_PTRNO", "STT", "IHNO", "BRANCHCODE", "INWARDNO", "NCTREMARKS",
        "PAN1", "TRCHARGES", "SIPREGDT", "SIPREGSLNO", "DIVPER", "GUARDPANNO", "CAN",
        "EXCHORGTRTYPE", "ELECTRXNFLAG", "CLEARED", "INVSTATE")],
    "kfin_mfsd243_sip": [("ZONE", "BRANCH", "LOCATION", "IHNO", "FOLIO", "INVESTOR_NAME", "REGISTRATION_DATE",
                          "START_DATE", "END_DATE", "NO_OF_INSTALLMENTS", "AMOUNT", "SCHEME", "PLAN", "AGENT_CODE",
                          "AGENT_NAME", "SUBBROKER", "SCHEME_NAME", "PAN", "SIP_TYPE", "SIP_MODE", "FUND_CODE",
                          "PRODUCT_CODE", "FREQUENCY", "TRTYPE", "TO_SCHEME", "TO_PLAN", "TERMINATE_DATE", "STATUS",
                          "TO_PRODUCT_CODE", "TO_SCHEME_NAME", "ECSNO", "ECS_BANK_NAME", "ECS_ACNO", "ECS_HOLDER_NAME",
                          "REG_SLNO", "INV_DP_ID", "INV_CLIENT_ID", "DP_INV_NAME", "MODIFY_FLAG", "UMRNCODE")],
    "kfin_mfsd205_brokerage": [(
        "PRODUCT_CODE", "FUND_DESCRIPTION", "FUND", "SCHEME", "PLAN", "OPTION", "ACCOUNT_NUMBER",
        "APPLICATION_NUMBER", "INVESTOR_NAME", "ADDRESS1", "ADDRESS2", "ADDRESS3", "CITY",
        "PINCODE", "TRANSACTION_DESCRIPTION", "FROM_DATE", "TO_DATE", "AMOUNT", "UNITS",
        "TRANSACTION_DATE", "PROCESS_DATE", "PERCENTAGE", "BROKERAGE", "SUB_BROKER",
        "ACCOUNT_TYPE", "BROKERAGE_HEAD", "BROKERAGE_TYPE", "TRANSACTION_NUMBER", "BRANCH_CODE",
        "CHEQUE_NUMBER", "STARTING_DATE", "ENDING_DATE", "WARRANT_NUMBER", "WARRANT_DATE",
        "DAILY_PRODUCT", "CUMULATIVE_NAV", "AVERAGE_ASSETS", "TRANSACTION_ID", "SCHEME_CODE",
        "TRANSACTION_HEAD", "FEE_TYPE", "ADJUSTMENT_FLAG", "SWITCH_FLAG", "GROSS_BROKERAGE",
        "STT_AMOUNT", "EDUCESS_AMOUNT", "TRAN_TYPE_CODE")]
}


# ══════════════════════════════════════════════════════════════════════════════
# STREAMLIT UI
# ══════════════════════════════════════════════════════════════════════════════

def render_data_manager():
    st.header("📁 Data Upload Manager")
    st.markdown(
        "Upload raw BSE, CAMS, and KFinTech files. Data is inserted exactly as provided in the files (no text case changes or date reformatting).")

    tab_bse, tab_cams, tab_kfin = st.tabs(["⭐ BSE Star", "🏦 CAMS", "🟦 KFinTech"])

    # --------------------- BSE TAB ---------------------
    with tab_bse:
        st.subheader("BSE Client Master")
        with st.container(border=True):
            f_cm = st.file_uploader("Upload Client Master Excel", type=["xlsx", "xls"], key="up_bse_cm")
            c_cm = st.checkbox("Replace all existing data?", key="chk_bse_cm")
            if st.button("Process BSE Clients", type="primary", key="btn_bse_cm") and f_cm:
                with st.spinner("Processing BSE Client Master..."):
                    ok, msg = parse_bse_client_master(f_cm, c_cm)
                    if ok:
                        st.success(msg)
                    else:
                        st.error(msg)

        st.subheader("BSE SIP (XSIP)")
        with st.container(border=True):
            f_sip = st.file_uploader("Upload XSIP Excel", type=["xlsx", "xls"], key="up_bse_sip")
            c_sip = st.checkbox("Replace all existing data?", key="chk_bse_sip")
            if st.button("Process BSE SIPs", type="primary", key="btn_bse_sip") and f_sip:
                with st.spinner("Processing BSE SIPs..."):
                    ok, msg, preview = parse_bse_sip(f_sip, c_sip)
                    if ok:
                        st.success(msg)
                        if preview: st.json(preview)
                    else:
                        st.error(msg)

        st.subheader("BSE Scheme Master")
        with st.container(border=True):
            f_sm = st.file_uploader("Upload Scheme Master (Excel/CSV)", type=["xlsx", "xls", "csv", "tsv"],
                                    key="up_bse_sm")
            c_sm = st.checkbox("Replace all existing data?", key="chk_bse_sm")
            if st.button("Process Scheme Master", type="primary", key="btn_bse_sm") and f_sm:
                with st.spinner("Processing BSE Scheme Master..."):
                    ok, msg, preview = parse_bse_scheme_master(f_sm, c_sm)
                    if ok:
                        st.success(msg)
                        if preview: st.json(preview)
                    else:
                        st.error(msg)

    # --------------------- CAMS TAB ---------------------
    with tab_cams:
        st.subheader("CAMS WBR4 - AUM")
        with st.container(border=True):
            f = st.file_uploader("Upload WBR4 Text/CSV", type=["txt", "csv"], key="up_cams_r4")
            c = st.checkbox("Replace all existing data?", key="chk_cams_r4")
            if st.button("Process WBR4 AUM", type="primary", key="btn_cams_r4") and f:
                with st.spinner("Processing..."): ok, msg, p = parse_cams_wbr4_aum(f, c); (
                    st.success(msg) if ok else st.error(msg))

        st.subheader("CAMS WBR9 - Folio Master")
        with st.container(border=True):
            f = st.file_uploader("Upload WBR9 Text/CSV", type=["txt", "csv"], key="up_cams_r9")
            c = st.checkbox("Replace all existing data?", key="chk_cams_r9")
            if st.button("Process WBR9 Folio", type="primary", key="btn_cams_r9") and f:
                with st.spinner("Processing..."): ok, msg, p = parse_cams_wbr9_folio(f, c); (
                    st.success(msg) if ok else st.error(msg))

        st.subheader("CAMS WBR2 - Transactions")
        with st.container(border=True):
            f = st.file_uploader("Upload WBR2 Text/CSV", type=["txt", "csv"], key="up_cams_r2")
            c = st.checkbox("Replace all existing data?", key="chk_cams_r2")
            if st.button("Process WBR2 Transactions", type="primary", key="btn_cams_r2") and f:
                with st.spinner("Processing..."): ok, msg, p = parse_cams_wbr2_transaction(f, c); (
                    st.success(msg) if ok else st.error(msg))

        st.subheader("CAMS WBR49 - SIP")
        with st.container(border=True):
            f = st.file_uploader("Upload WBR49 Text/CSV", type=["txt", "csv"], key="up_cams_r49")
            c = st.checkbox("Replace all existing data?", key="chk_cams_r49")
            if st.button("Process WBR49 SIP", type="primary", key="btn_cams_r49") and f:
                with st.spinner("Processing..."): ok, msg, p = parse_cams_wbr49_sip(f, c); (
                    st.success(msg) if ok else st.error(msg))

        st.subheader("CAMS WBR77 - Brokerage")
        with st.container(border=True):
            f = st.file_uploader("Upload WBR77 Text/CSV", type=["txt", "csv"], key="up_cams_r77")
            c = st.checkbox("Replace all existing data?", key="chk_cams_r77")
            if st.button("Process WBR77 Brokerage", type="primary", key="btn_cams_r77") and f:
                with st.spinner("Processing..."): ok, msg, p = parse_cams_wbr77_brokerage(f, c); (
                    st.success(msg) if ok else st.error(msg))

    # --------------------- KFINTECH TAB ---------------------
    with tab_kfin:
        st.subheader("KFin MFSD203 - AUM")
        with st.container(border=True):
            f = st.file_uploader("Upload MFSD203 Text/CSV", type=["txt", "csv"], key="up_kfin_203")
            c = st.checkbox("Replace all existing data?", key="chk_kfin_203")
            if st.button("Process MFSD203 AUM", type="primary", key="btn_kfin_203") and f:
                with st.spinner("Processing..."): ok, msg, p = parse_kfin_mfsd203_aum(f, c); (
                    st.success(msg) if ok else st.error(msg))

        st.subheader("KFin MFSD211 - Folio Master")
        with st.container(border=True):
            f = st.file_uploader("Upload MFSD211 Text/CSV", type=["txt", "csv"], key="up_kfin_211")
            c = st.checkbox("Replace all existing data?", key="chk_kfin_211")
            if st.button("Process MFSD211 Folio", type="primary", key="btn_kfin_211") and f:
                with st.spinner("Processing..."): ok, msg, p = parse_kfin_mfsd211_folio(f, c); (
                    st.success(msg) if ok else st.error(msg))

        st.subheader("KFin MFSD201 - Transactions")
        with st.container(border=True):
            f = st.file_uploader("Upload MFSD201 Text/CSV", type=["txt", "csv"], key="up_kfin_201")
            c = st.checkbox("Replace all existing data?", key="chk_kfin_201")
            if st.button("Process MFSD201 Transactions", type="primary", key="btn_kfin_201") and f:
                with st.spinner("Processing..."): ok, msg, p = parse_kfin_mfsd201_transaction(f, c); (
                    st.success(msg) if ok else st.error(msg))

        st.subheader("KFin MFSD243 - SIP")
        with st.container(border=True):
            f = st.file_uploader("Upload MFSD243 Text/CSV", type=["txt", "csv"], key="up_kfin_243")
            c = st.checkbox("Replace all existing data?", key="chk_kfin_243")
            if st.button("Process MFSD243 SIP", type="primary", key="btn_kfin_243") and f:
                with st.spinner("Processing..."): ok, msg, p = parse_kfin_mfsd243_sip(f, c); (
                    st.success(msg) if ok else st.error(msg))

        st.subheader("KFin MFSD205 - Brokerage")
        with st.container(border=True):
            f = st.file_uploader("Upload MFSD205 Text/CSV", type=["txt", "csv"], key="up_kfin_205")
            c = st.checkbox("Replace all existing data?", key="chk_kfin_205")
            if st.button("Process MFSD205 Brokerage", type="primary", key="btn_kfin_205") and f:
                with st.spinner("Processing..."): ok, msg, p = parse_kfin_mfsd205_brokerage(f, c); (
                    st.success(msg) if ok else st.error(msg))