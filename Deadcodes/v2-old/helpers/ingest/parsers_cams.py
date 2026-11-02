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


def parse_cams_wbr4_aum(file, replace: bool) -> tuple[bool, str, dict]:
    df = _read_csv_auto(file)
    if df is None:
        sflog.record_failure(
            source="manual_upload", stage="parse",
            rta="CAMS", report="WBR4", file=getattr(file, "name", ""),
            msg="Could not parse file",
        )
        return False, "Could not parse file", {}
    df = _clean_cols(df)
    if "FOLIOCHK" not in df.columns:
        sflog.record_failure(
            source="manual_upload", stage="parse",
            rta="CAMS", report="WBR4", file=getattr(file, "name", ""),
            msg=f"Missing FOLIOCHK. Found: {list(df.columns)[:10]}",
        )
        return False, f"Missing FOLIOCHK. Found: {list(df.columns)[:10]}", {}
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
    if df is None:
        sflog.record_failure(
            source="manual_upload", stage="parse",
            rta="CAMS", report="WBR9", file=getattr(file, "name", ""),
            msg="Could not parse file",
        )
        return False, "Could not parse file", {}
    df = _clean_cols(df)
    if "FOLIOCHK" not in df.columns:
        sflog.record_failure(
            source="manual_upload", stage="parse",
            rta="CAMS", report="WBR9", file=getattr(file, "name", ""),
            msg=f"Missing FOLIOCHK. Found: {list(df.columns)[:10]}",
        )
        return False, f"Missing FOLIOCHK. Found: {list(df.columns)[:10]}", {}
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
    if df is None:
        sflog.record_failure(
            source="manual_upload", stage="parse",
            rta="CAMS", report="WBR2", file=getattr(file, "name", ""),
            msg="Could not parse file",
        )
        return False, "Could not parse file", {}
    df = _clean_cols(df)
    if "TRXNNO" not in df.columns:
        sflog.record_failure(
            source="manual_upload", stage="parse",
            rta="CAMS", report="WBR2", file=getattr(file, "name", ""),
            msg=f"Missing TRXNNO. Found: {list(df.columns)[:10]}",
        )
        return False, f"Missing TRXNNO. Found: {list(df.columns)[:10]}", {}
    batch = _batch_id("CAMS_R2", file.name);
    rows, skipped = [], 0
    for _, row in df.iterrows():
        trxnno = raw_val(row.get("TRXNNO", ""))
        if not trxnno: skipped += 1; continue
        raw_traddate = raw_val(row.get("TRADDATE", ""))
        rows.append((raw_val(row.get("AMC_CODE", "")),
                     raw_val(row.get("FOLIO_NO", "")),
                     raw_val(row.get("PRODCODE", "")),
                     raw_val(row.get("SCHEME", "")),
                     raw_val(row.get("INV_NAME", "")),
                     raw_val(row.get("TRXNTYPE", "")),
                     trxnno,
                     raw_val(row.get("TRXNMODE", "")),
                     raw_val(row.get("TRXNSTAT", "")),
                     raw_val(row.get("USERCODE", "")),
                     raw_val(row.get("USRTRXNO", "")),
                     raw_traddate,
                     _cams_date_to_iso(raw_traddate),
                     raw_val(row.get("POSTDATE", "")),
                     raw_val(row.get("PURPRICE", "")),
                     raw_val(row.get("UNITS", "")),
                     raw_val(row.get("AMOUNT", "")),
                     raw_val(row.get("BROKCODE", "")),
                     raw_val(row.get("SUBBROK", "")),
                     raw_val(row.get("BROKPERC", "")),
                     raw_val(row.get("BROKCOMM", "")),
                     raw_val(row.get("ALTFOLIO", "")),
                     raw_val(row.get("REP_DATE", "")),
                     raw_val(row.get("TIME1", "")),
                     raw_val(row.get("TRXNSUBTYP", "")),
                     raw_val(row.get("APPLICATION_NO", "")),
                     raw_val(row.get("TRXN_NATURE", "")),
                     raw_val(row.get("TAX", "")),
                     raw_val(row.get("TOTAL_TAX", "")),
                     raw_val(row.get("TE_15H", "")),
                     raw_val(row.get("MICR_NO", "")),
                     raw_val(row.get("REMARKS", "")),
                     raw_val(row.get("SWFLAG", "")),
                     raw_val(row.get("OLD_FOLIO", "")),
                     raw_val(row.get("SEQ_NO", "")),
                     raw_val(row.get("REINVEST_FLAG", "")),
                     raw_val(row.get("MULT_BROK", "")),
                     raw_val(row.get("STT", "")),
                     raw_val(row.get("LOCATION", "")),
                     raw_val(row.get("SCHEME_TYPE", "")),
                     raw_val(row.get("TAX_STATUS", "")),
                     raw_val(row.get("LOAD", "")),
                     raw_val(row.get("SCANREFNO", "")),
                     raw_val(row.get("PAN", "")),
                     raw_val(row.get("INV_IIN", "")),
                     raw_val(row.get("TARG_SRC_SCHEME", "")),
                     raw_val(row.get("TRXN_TYPE_FLAG", "")),
                     raw_val(row.get("TICOB_TRTYPE", "")),
                     raw_val(row.get("TICOB_TRNO", "")),
                     raw_val(row.get("TICOB_POSTED_DATE", "")),
                     raw_val(row.get("DP_ID", "")),
                     raw_val(row.get("TRXN_CHARGES", "")),
                     raw_val(row.get("ELIGIB_AMT", "")),
                     raw_val(row.get("SRC_OF_TXN", "")),
                     raw_val(row.get("TRXN_SUFFIX", "")),
                     raw_val(row.get("SIPTRXNNO", "")),
                     raw_val(row.get("TER_LOCATION", "")),
                     raw_val(row.get("EUIN", "")),
                     raw_val(row.get("EUIN_VALID", "")),
                     raw_val(row.get("EUIN_OPTED", "")),
                     raw_val(row.get("SUB_BRK_ARN", "")),
                     raw_val(row.get("EXCH_DC_FLAG", "")),
                     raw_val(row.get("SRC_BRK_CODE", "")),
                     raw_val(row.get("SYS_REGN_DATE", "")),
                     raw_val(row.get("AC_NO", "")),
                     raw_val(row.get("BANK_NAME", "")),
                     raw_val(row.get("REVERSAL_CODE", "")),
                     raw_val(row.get("EXCHANGE_FLAG", "")),
                     raw_val(row.get("CA_INITIATED_DATE", "")),
                     raw_val(row.get("GST_STATE_CODE", "")),
                     raw_val(row.get("IGST_AMOUNT", "")),
                     raw_val(row.get("CGST_AMOUNT", "")),
                     raw_val(row.get("SGST_AMOUNT", "")),
                     raw_val(row.get("REV_REMARK", "")),
                     raw_val(row.get("ORIGINAL_TRXNNO", "")),
                     raw_val(row.get("STAMP_DUTY", "")),
                     raw_val(row.get("FOLIO_OLD", "")),
                     raw_val(row.get("SCHEME_FOLIO_NUMBER", "")),
                     raw_val(row.get("AMC_REF_NO", "")),
                     raw_val(row.get("REQUEST_REF_NO", "")),
                     raw_val(row.get("TRANSMISSION_FLAG", "")),
                     batch))
    return _execute_cams_import("cams_wbr2_transaction", rows, batch, replace, skipped)



def parse_cams_wbr49_sip(file, replace: bool) -> tuple[bool, str, dict]:
    df = _read_csv_auto(file)
    if df is None:
        sflog.record_failure(
            source="manual_upload", stage="parse",
            rta="CAMS", report="WBR49", file=getattr(file, "name", ""),
            msg="Could not parse file",
        )
        return False, "Could not parse file", {}
    df = _clean_cols(df)
    if "AUTO_TRNO" not in df.columns:
        sflog.record_failure(
            source="manual_upload", stage="parse",
            rta="CAMS", report="WBR49", file=getattr(file, "name", ""),
            msg=f"Missing AUTO_TRNO. Found: {list(df.columns)[:10]}",
        )
        return False, f"Missing AUTO_TRNO. Found: {list(df.columns)[:10]}", {}
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
    if df is None:
        sflog.record_failure(
            source="manual_upload", stage="parse",
            rta="CAMS", report="WBR77", file=getattr(file, "name", ""),
            msg="Could not parse file",
        )
        return False, "Could not parse file", {}
    df = _clean_cols(df)
    if "TRXN_NO" not in df.columns:
        sflog.record_failure(
            source="manual_upload", stage="parse",
            rta="CAMS", report="WBR77", file=getattr(file, "name", ""),
            msg=f"Missing TRXN_NO. Found: {list(df.columns)[:10]}",
        )
        return False, f"Missing TRXN_NO. Found: {list(df.columns)[:10]}", {}
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
                     raw_val(row.get("P_SI_USER_TRXNNO", "")), raw_val(row.get("SEQ_NO", "")),
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





