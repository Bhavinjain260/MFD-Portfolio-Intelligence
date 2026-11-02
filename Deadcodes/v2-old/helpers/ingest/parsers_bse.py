"""Auto-extracted parsers (Phase 8). Uses data_manager helpers where still shared."""
from __future__ import annotations
import logging
from typing import Optional, Any
import pandas as pd

log = logging.getLogger(__name__)

try:
    from core.db import get_conn, bump
except Exception:
    from init_db import get_conn
    def bump():
        try:
            from data_manager import bump as _b
            _b()
        except Exception:
            pass

# Shared helpers still live in data_manager until fully moved
try:
    from data_manager import (
        raw_val, raw_num, read_csv_auto, _normalize_col,
        clean_str,  # may not exist
    )
except Exception:
    from ingest.common import raw_val, raw_num, read_csv_auto
    def _normalize_col(c):
        return str(c).strip().lower().replace(" ", "_")


def parse_bse_client_master(file, replace: bool) -> tuple[bool, str]:
    file.seek(0)
    try:
        df = pd.read_excel(file, dtype=str)
    except Exception as exc:
        return False, f"File read error: {exc}"

    df.columns = [re.sub(r"[^a-z0-9]+", "_", c.strip().lower()).strip("_") for c in df.columns]

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
    mobile_col = _c("indian_mobile_no", "mobile", "mobile_no", "mobile_number", "phone", "contact_no",
                    "primary_holder_mobile")
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

    bse_client_columns = [c.strip() for c in """member_code, client_code, primary_holder_first_name,
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
    nomination_auth_date, guardian_relationship, created_by, created_at, last_modified_by,
    last_modified_at""".split(",")] + ["upload_batch"]

    update_cols = [c for c in bse_client_columns if c != "client_code"]
    insert_sql = (
            f"INSERT INTO bse_client_master ({', '.join(bse_client_columns)}) "
            f"VALUES ({', '.join(['?'] * len(bse_client_columns))}) "
            f"ON CONFLICT(client_code) DO UPDATE SET " + ", ".join(f"{c}=excluded.{c}" for c in update_cols)
    )

    inserted = updated = 0
    with get_conn() as conn:
        if replace:
            conn.execute("DELETE FROM bse_client_master")
            conn.executemany(insert_sql, client_rows)
            inserted = len(client_rows)
        else:
            before = _count_before_after(conn, "bse_client_master")
            conn.executemany(insert_sql, client_rows)
            after = _count_before_after(conn, "bse_client_master")
            inserted = after - before
            updated = len(client_rows) - inserted
    msg = f"Imported {inserted} new clients"
    if updated:
        msg += f" | Updated {updated} existing"

    if inserted or updated:
        bump()

    return True, msg



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
    bse_sip_columns = ["status", "member_code", "client_code", "client_name", "pg_bank_reference_no",
                       "xsip_regn_no", "regn_date", "amc_name", "rta_scheme_code", "scheme_name",
                       "frequency_type", "start_date", "end_date", "installments_amt", "brokerage",
                       "entry_by", "mandate_id", "dpc_flag", "dp_trans", "sub_broker", "euin", "euin_decl",
                       "first_order", "folio_no", "remarks", "sub_broker_arncode", "no_of_installments",
                       "exchange_remark", "health_declaration_flag", "nominee_dob", "disclaimer_flag",
                       "internal_ref_no", "primary_holder_email", "primary_holder_mobile",
                       "second_holder_email", "second_holder_mobile", "third_holder_email",
                       "third_holder_mobile", "upload_batch"]
    update_cols = [c for c in bse_sip_columns if c != "xsip_regn_no"]
    insert_sql = (
            f"INSERT INTO bse_sip ({', '.join(bse_sip_columns)}) VALUES ({', '.join(['?'] * len(bse_sip_columns))}) "
            f"ON CONFLICT(xsip_regn_no) DO UPDATE SET " + ", ".join(f"{c}=excluded.{c}" for c in update_cols)
    )
    inserted = updated = 0
    with get_conn() as conn:
        if replace:
            conn.execute("DELETE FROM bse_sip")
            conn.executemany(insert_sql, rows)
            inserted = len(rows)
        else:
            before = _count_before_after(conn, "bse_sip")
            conn.executemany(insert_sql, rows)
            after = _count_before_after(conn, "bse_sip")
            inserted = after - before
            updated = len(rows) - inserted
    active = sum(1 for r in rows if "active" in str(r[0]).lower())
    msg = f"Imported {inserted} new SIP records | Active: {active}"
    if skipped:
        msg += f" | Skipped {skipped}"
    if updated:
        msg += f" | Updated {updated}"

    if inserted or updated:
        bump()

    return True, msg, {"rows": inserted, "skipped": skipped, "updated": updated, "active": active}



def parse_bse_scheme_master(file, replace: bool) -> tuple[bool, str, dict]:
    file.seek(0);
    fname = getattr(file, "name", "")
    try:
        if fname.lower().endswith((".xlsx", ".xls")):
            df = pd.read_excel(file, dtype=str)
        else:
            df = None
            for sep in ("\t", ",", "|"):
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
    scheme_columns = ["unique_no", "scheme_code", "rta_scheme_code", "amc_scheme_code", "isin", "amc_code",
                      "scheme_type", "scheme_plan", "scheme_name", "purchase_allowed", "purchase_transaction_mode",
                      "min_purchase_amount", "additional_purchase_amount", "max_purchase_amount",
                      "purchase_amount_multiplier", "purchase_cutoff_time", "redemption_allowed",
                      "redemption_transaction_mode", "min_redemption_qty", "redemption_qty_multiplier",
                      "max_redemption_qty", "redemption_amount_min", "redemption_amount_max",
                      "redemption_amount_multiple", "redemption_cutoff_time", "rta_agent_code", "amc_active_flag",
                      "dividend_reinvestment_flag", "sip_flag", "stp_flag", "swp_flag", "switch_flag",
                      "settlement_type", "amc_ind", "face_value", "start_date", "end_date", "exit_load_flag",
                      "exit_load", "lock_in_period_flag", "lock_in_period", "channel_partner_code",
                      "reopening_date", "upload_batch"]
    conflict_cols = ["scheme_code", "rta_scheme_code"]
    update_cols = [c for c in scheme_columns if c not in conflict_cols]
    sql = (
            f"INSERT INTO bse_scheme_master ({', '.join(scheme_columns)}) "
            f"VALUES ({', '.join(['?'] * len(scheme_columns))}) "
            f"ON CONFLICT({', '.join(conflict_cols)}) DO UPDATE SET " + ", ".join(
        f"{c}=excluded.{c}" for c in update_cols)
    )
    inserted = updated = 0
    with get_conn() as conn:
        if replace:
            conn.execute("DELETE FROM bse_scheme_master")
            conn.executemany(sql, rows)
            inserted = len(rows)
        else:
            before = _count_before_after(conn, "bse_scheme_master")
            conn.executemany(sql, rows)
            after = _count_before_after(conn, "bse_scheme_master")
            inserted = after - before
            updated = len(rows) - inserted
    if inserted or updated:
        bump()

    return True, f"Imported {inserted} new schemes | Updated {updated} | Skipped: {skipped}", \
        {"rows": inserted, "skipped": skipped, "updated": updated}


# ══════════════════════════════════════════════════════════════════════════════
# CAMS PARSERS (Raw Inserts)
# ══════════════════════════════════════════════════════════════════════════════

