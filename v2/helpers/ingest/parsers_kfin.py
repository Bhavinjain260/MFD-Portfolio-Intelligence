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


def parse_kfin_mfsd203_aum(file, replace: bool) -> tuple[bool, str, dict]:
    df = _read_csv_auto(file)
    if df is None:
        sflog.record_failure(
            source="manual_upload", stage="parse",
            rta="KFinTech", report="MFSD203", file=getattr(file, "name", ""),
            msg="Could not parse file",
        )
        return False, "Could not parse file", {}
    df = _clean_cols(df)
    if "FOLIO_NUMBER" not in df.columns:
        sflog.record_failure(
            source="manual_upload", stage="parse",
            rta="KFinTech", report="MFSD203", file=getattr(file, "name", ""),
            msg="Missing FOLIO_NUMBER",
        )
        return False, "Missing FOLIO_NUMBER", {}
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
    if "FOLIO" not in df.columns:
        sflog.record_failure(
            source="manual_upload", stage="parse",
            rta="KFinTech", report="MFSD211", file=getattr(file, "name", ""),
            msg="Missing FOLIO",
        )
        return False, "Missing FOLIO", {}
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
    if df is None:
        sflog.record_failure(
            source="manual_upload", stage="parse",
            rta="KFinTech", report="MFSD201", file=getattr(file, "name", ""),
            msg="Could not parse file — no separator produced >5 columns",
        )
        return False, "Could not parse file", {}

    df = _clean_cols(df)

    # ══════════════════════════════════════════════════════════════
    # HEADER FAMILY DETECTION + MAPPING
    # ══════════════════════════════════════════════════════════════
    # KFinTech ships MFSD201 in two flavours:
    #   1. BULK EXPORT — raw technical headers (td_trno, td_trdt, ...)
    #      This is what the DB schema and this parser were written for.
    #   2. MAILBACK REPORT — friendly headers
    #      ("Product Code", "Transaction Number", "Transaction Date", ...)
    #      This is what actually lands in mailback_sync/kfintech/mfsd201/
    #
    # Detect which family this file uses and rename friendly headers to
    # their technical equivalents so the rest of the parser runs unchanged.
    # ══════════════════════════════════════════════════════════════


    # Alternative naming pattern used by some KFinTech mailback exports
    MAILBACK_TO_TECHNICAL_ALT = {
        "PRODUCT":                    "FMCODE",
        "CODE_FUND_FOLIO":           "TD_ACNO",
        "FOLIO_NUMBER":              "TD_ACNO",
        "NUMBER_SCHEME":             "SMCODE",
        "CODE_DIVIDEND":             "DIVOPT",
        "OPTION_FUND":               "FUNDDESC",
        "DESCRIPTION_TRANSACTION":   "TRDESC",
        "HEAD_TRANSACTION":          "TD_PURRED",
        "NUMBER_SWITCH_REF":         "TD_PURRED",
        "NO_INSTRUMENT":             "CHQNO",
        "NUMBER_INVESTOR":           "INVNAME",
        "NAME_TRANSACTION":          "TRNMODE",
        "MODE_TRANSACTION":          "TRNMODE",
        "STATUS_BRANCH":             "TRNSTAT",
        "NAME_BRANCH":               "TD_BRANCH",
        "NO_TRANSACTION":            "TD_TRNO",
        "NUMBER_TRANSACTION":        "TD_TRNO",
        "DATE_PROCESS":              "TD_PRDT",
        "DATE_TRANSACTION":          "TD_TRDT",
        "DATE_PRICE_LOAD":           "TD_POP",
        "PERCENTAGE_UNITS_AMOUNT_LOAD": "LOADPER",
        "AMOUNT_AGENT":              "TD_AGENT",
        "AMOUNT_BROKER":             "TD_BROKER",
        "PERCENTAGE_BROKER":         "BROKPER",
        "AMOUNT_COMMISSION":         "BROKCOMM",
        "DATE_CREATED":              "CRDATE",
        "TIME_CREATED":              "CRTIME",
        "ID_APPLICATION":            "TD_APPNO",
        "ID_UNIQUE":                 "UNQNO",
        "TYPE_TRANSACTION":          "TD_TRTYPE",
        "DATE_PURCHASE":             "PURDATE",
        "AMOUNT_PURCHASE":           "PURAMT",
        "UNITS_PURCHASE":            "PURUNITS",
        "FLAG_TRANSACTION":          "TRFLAG",
        "DATE_SWITCH_FUND":          "SFUNDDT",
        "DATE_CHEQUE":               "CHQDATE",
        "BANK_CHEQUE":               "CHQBANK",
        "NAV":                       "TD_NAV",
        "NO_PREVIOUS_TRANSACTION":   "TD_PTRNO",
        "CHARGE_TRANSACTION":        "TRCHARGES",
        "DATE_SIP_REGISTRATION":     "SIPREGDT",
        "NUMBER_SIP_SERIAL":         "SIPREGSLNO",
        "PERCENTAGE_DIVIDEND":       "DIVPER",
        "COMMON_ACCOUNT_NUMBER":     "CAN",
        "NUMBER_INVESTOR_ID":        "INVID",
        "PAN":                       "PAN1",
        "REMARKS":                   "NCTREMARKS",
        "STATE_INVESTOR":            "INVSTATE",
        "FLAG_ELECTRONIC_TRANSACTION": "ELECTRXNFLAG",
        "IHNO":                      "IHNO",
        "BRANCHCODE":                "BRANCHCODE",
        "INWARDNO":                  "INWARDNO",
        "STT":                       "STT",
    }


    MAILBACK_TO_TECHNICAL = {
        "PRODUCT_CODE":       "FMCODE",
        "FUND":               "TD_FUND",
        "FOLIO_NUMBER":       "TD_ACNO",
        "SCHEME_CODE":        "SMCODE",
        "DIVIDEND_OPTION":    "DIVOPT",
        "FUND_DESCRIPTION":   "FUNDDESC",
        "TRANSACTION_HEAD":   "TD_PURRED",
        "TRANSACTION_NUMBER": "TD_TRNO",
        "INVESTOR_NAME":      "INVNAME",
        "TRANSACTION_MODE":   "TRNMODE",
        "TRANSACTION_STATUS": "TRNSTAT",
        "BRANCH_NAME":        "TD_BRANCH",
        "BRANCH_TRANSACTION_NO": "ISCTRNO",
        "TRANSACTION_DATE":   "TD_TRDT",
        "PROCESS_DATE":       "TD_PRDT",
        "PRICE":              "TD_POP",
        "LOAD_PERCENTAGE":    "LOADPER",
        "UNITS":              "TD_UNITS",
        "AMOUNT":             "TD_AMT",
        "LOAD_AMOUNT":        "LOAD1",
        "AGENT_CODE":         "TD_AGENT",
        "SUB_BROKER_CODE":    "TD_BROKER",
        "BROKERAGE_PERCENTAGE": "BROKPER",
        "COMMISSION":         "BROKCOMM",
        "INVESTOR_ID":        "INVID",
        "REPORT_DATE":        "CRDATE",
        "REPORT_TIME":        "CRTIME",
        "TRANSACTION_SUB":    "TRNSUB",
        "APPLICATION_NUMBER": "TD_APPNO",
        "TRANSACTION_ID":     "UNQNO",
        "TRANSACTION_DESCRIPTION": "TRDESC",
        "TRANSACTION_TYPE":   "TD_TRTYPE",
        "PURCHASE_DATE":      "PURDATE",
        "PURCHASE_AMOUNT":    "PURAMT",
        "PURCHASE_UNITS":     "PURUNITS",
        "TRANSACTION_FLAG":   "TRFLAG",
        "SWITCH_FUND_DATE":   "SFUNDDT",
        "INSTRUMENT_DATE":    "CHQDATE",
        "INSTRUMENT_BANK":    "CHQBANK",
        "NAV":                "TD_NAV",
        "PURCHASE_TRANSACTION_NO": "TD_PTRNO",
        "STT":                "STT",
        "IHNO":               "IHNO",
        "BRANCH_CODE":        "BRANCHCODE",
        "INWARD_NUMBER":      "INWARDNO",
        "REMARKS":            "NCTREMARKS",
        "PAN1":               "PAN1",
        "TRCHARGES":          "TRCHARGES",
        "SIP_REGN_DATE":      "SIPREGDT",
        "DIVPER":             "DIVPER",
        "GUARDPANNO":         "GUARDPANNO",
        "INVESTORSTATE":      "INVSTATE",
        "COMMON_ACCOUNT_NUMBER": "CAN",
        "EXCHANGE_ORGTRTYPE": "EXCHORGTRTYPE",
        "ELECTRONIC_TRANSACTION_FLAG": "ELECTRXNFLAG",
        # "STATUS" collides with TRANSTAT — only map it if TRANSTAT is absent
    }

        # Detect family: if TD_TRNO is present, file is already technical.
    if "TD_TRNO" not in df.columns:
        # Try standard mailback format first (TRANSACTION_NUMBER, FOLIO_NUMBER, etc.)
        if "TRANSACTION_NUMBER" in df.columns:
            df = df.rename(columns={k: v for k, v in MAILBACK_TO_TECHNICAL.items() if k in df.columns})
        
        # Try alternative naming convention if standard didn't work (NO_TRANSACTION, CODE_FUND_FOLIO, etc.)
        if "TD_TRNO" not in df.columns and any(k in df.columns for k in ["NO_TRANSACTION", "NUMBER_TRANSACTION"]):
            df = df.rename(columns={k: v for k, v in MAILBACK_TO_TECHNICAL_ALT.items() if k in df.columns})
 
    if "TD_TRNO" not in df.columns:
            cols_seen = list(df.columns)[:20]
            sflog.record_failure(
                source="manual_upload", stage="parse",
                rta="KFinTech", report="MFSD201", file=getattr(file, "name", ""),
                msg=f"Missing TD_TRNO after header mapping — columns seen: {cols_seen}",
            )
            return False, f"Missing TD_TRNO — columns seen: {cols_seen}", {}

    batch = _batch_id("KFIN_201", file.name)
    rows, skipped = [], 0
    for _, row in df.iterrows():
        trno = raw_val(row.get("TD_TRNO", ""))
        if not trno:
            skipped += 1
            continue
        raw_td_trdt = raw_val(row.get("TD_TRDT", ""))
        rows.append((raw_val(row.get("FMCODE", "")), raw_val(row.get("TD_FUND", "")), raw_val(row.get("TD_ACNO", "")),raw_val(row.get("SCHPLN", "")), raw_val(row.get("DIVOPT", "")), raw_val(row.get("FUNDDESC", "")),
                     raw_val(row.get("TD_PURRED", "")), trno, raw_val(row.get("SMCODE", "")),
                     raw_val(row.get("CHQNO", "")), raw_val(row.get("INVNAME", "")), raw_val(row.get("TRNMODE", "")),
                     raw_val(row.get("TRNSTAT", "")), raw_val(row.get("TD_BRANCH", "")),
                     raw_val(row.get("ISCTRNO", "")), raw_td_trdt, _kfin_date_to_iso(raw_td_trdt),
                     raw_val(row.get("TD_PRDT", "")),
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
    if "FOLIO" not in df.columns:
        sflog.record_failure(
            source="manual_upload", stage="parse",
            rta="KFinTech", report="MFSD243", file=getattr(file, "name", ""),
            msg="Missing FOLIO",
        )
        return False, "Missing FOLIO", {}
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
    """Parse KFintech MFSD 205 Brokerage file."""
    df = _read_csv_auto(file)
    if df is None:
        sflog.record_failure(
            source="manual_upload", stage="parse",
            rta="KFinTech", report="MFSD205", file=getattr(file, "name", ""),
            msg="Could not parse file",
        )
        return False, "Could not parse file", {}
    df = _clean_cols(df)

    # DEBUG: Print all columns found
    log.warning("KFIN 205 COLUMNS FOUND: %s", list(df.columns))

    def _c(*cands):
        return next((c for c in cands if c in df.columns), None)

    # Column detection
    prod_col = _c("PRODUCT_CODE", "PRODUCT", "PRODCODE")
    fund_desc_col = _c("FUND_DESCRIPTION", "FUND_DESC", "SCHEME_NAME")
    fund_col = _c("FUND", "FUND_CODE", "AMC_CODE")
    scheme_col = _c("SCHEME", "SCHEME_NAME")
    plan_col = _c("PLAN", "PLAN_TYPE")
    option_col = _c("OPTION", "PLAN_OPTION")
    acnt_col = _c("ACCOUNT_NUMBER", "ACCT_NO", "FOLIO", "FOLIO_NO")
    app_no_col = _c("APPLICATION_NUMBER", "APP_NO", "TRXN_NO")
    inv_name_col = _c("INVESTOR_NAME", "INV_NAME", "NAME")
    addr1_col = _c("ADDRESS1", "ADDR1", "ADDRESS_1")
    addr2_col = _c("ADDRESS2", "ADDR2", "ADDRESS_2")
    addr3_col = _c("ADDRESS3", "ADDR3", "ADDRESS_3")
    city_col = _c("CITY")
    pin_col = _c("PINCODE", "PIN")
    txn_desc_col = _c("TRANSACTION_DESCRIPTION", "TRXN_DESC", "DESCRIPTION")
    from_dt_col = _c("FROM_DATE", "START_DATE")
    to_dt_col = _c("TO_DATE", "END_DATE")
    amt_col = _c("AMOUNT", "TRXN_AMT", "AMOUNT_IN_RS")
    units_col = _c("UNITS")
    txn_dt_col = _c("TRANSACTION_DATE", "TRXN_DATE", "TRADE_DATE")
    proc_dt_col = _c("PROCESS_DATE", "PROCESSED_DATE")
    pct_col = _c("PERCENTAGE", "BROKERAGE_PCT")
    brokerage_col = _c("BROKERAGE", "BROK_AMT", "BROKERAGE_AMT", "BROKERAGE_AMOUNT", "COMMISSION", "BROKERAGE_IN_RS")
    sub_broker_col = _c("SUB_BROKER", "SUB_BROKER_CODE")
    acnt_type_col = _c("ACCOUNT_TYPE", "ACCT_TYPE")
    brokerage_head_col = _c("BROKERAGE_HEAD", "BROK_HEAD")
    brokerage_type_col = _c("BROKERAGE_TYPE", "BROK_TYPE")
    txn_no_col = _c("TRANSACTION_NUMBER", "TRXN_NO", "TRANSACTION_NO")
    branch_col = _c("BRANCH_CODE", "BRANCH")
    cheque_col = _c("CHEQUE_NUMBER", "CHEQUE_NO")
    start_dt_col = _c("STARTING_DATE")
    end_dt_col = _c("ENDING_DATE")
    warrant_no_col = _c("WARRANT_NUMBER", "WARRANT_NO")
    warrant_dt_col = _c("WARRANT_DATE")
    daily_prod_col = _c("DAILY_PRODUCT")
    cum_nav_col = _c("CUMULATIVE_NAV")
    avg_assets_col = _c("AVERAGE_ASSETS", "AVG_AUM")
    txn_id_col = _c("TRANSACTION_ID", "TRXN_ID")
    scheme_code_col = _c("SCHEME_CODE")
    txn_head_col = _c("TRANSACTION_HEAD", "TRXN_HEAD")
    fee_type_col = _c("FEE_TYPE")
    adj_flag_col = _c("ADJUSTMENT_FLAG", "ADJ_FLAG")
    switch_flag_col = _c("SWITCH_FLAG")
    gross_brok_col = _c("GROSS_BROKERAGE", "GROSS_BROK", "GROSS_COMMISSION", "GROSSBROKERAGE")
    stt_col = _c("STT_AMOUNT", "STT", "STTAMOUNT")
    educess_col = _c("EDUCESS_AMOUNT", "EDUCESS", "CESS", "EDUCESSAMOUNT")
    tran_type_col = _c("TRAN_TYPE_CODE", "TRXN_TYPE", "TRANTYPECODE")

    # DEBUG: Log which numeric columns were found
    log.warning("AMOUNT column: %s", amt_col)
    log.warning("BROKERAGE column: %s", brokerage_col)
    log.warning("UNITS column: %s", units_col)

    batch = _batch_id("KFIN_205", file.name)
    rows, skipped = [], 0

    for idx, row in df.iterrows():
        acnt = raw_val(row.get(acnt_col, "")) if acnt_col else ""
        if not acnt:
            skipped += 1
            continue

        # DEBUG: Print first row's raw values
        if idx == 0:
            log.warning("FIRST ROW RAW AMOUNT: '%s'", row.get(amt_col, "COL_NOT_FOUND"))
            log.warning("FIRST ROW RAW BROKERAGE: '%s'", row.get(brokerage_col, "COL_NOT_FOUND"))
            log.warning("FIRST ROW RAW UNITS: '%s'", row.get(units_col, "COL_NOT_FOUND"))
            log.warning("FIRST ROW TYPE AMOUNT: %s", type(row.get(amt_col)))

        # Use raw_num for REAL columns
        amount = raw_num(row.get(amt_col, 0)) if amt_col else 0.0
        units = raw_num(row.get(units_col, 0)) if units_col else 0.0
        percentage = raw_num(row.get(pct_col, 0)) if pct_col else 0.0
        brokerage = raw_num(row.get(brokerage_col, 0)) if brokerage_col else 0.0
        gross_brokerage = raw_num(row.get(gross_brok_col, 0)) if gross_brok_col else 0.0
        stt_amount = raw_num(row.get(stt_col, 0)) if stt_col else 0.0
        educess_amount = raw_num(row.get(educess_col, 0)) if educess_col else 0.0
        daily_product = raw_num(row.get(daily_prod_col, 0)) if daily_prod_col else 0.0
        cumulative_nav = raw_num(row.get(cum_nav_col, 0)) if cum_nav_col else 0.0
        average_assets = raw_num(row.get(avg_assets_col, 0)) if avg_assets_col else 0.0

        # ═══ TEMPORARY DIAGNOSTIC ═══
        # from collections import Counter
        # keys = [(r[6], r[27]) for r in rows]  # account_number, transaction_number
        # key_counts = Counter(keys)
        # duplicates = {k: v for k, v in key_counts.items() if v > 1}
        #
        # print(f"Total rows parsed: {len(rows)}")
        # print(f"Unique (account, txn) pairs: {len(key_counts)}")
        # print(f"Pairs with duplicates: {len(duplicates)}")
        # print(f"Duplicate pairs (showing first 10):")
        # for (acc, txn), count in list(duplicates.items())[:10]:
        #     print(f"  Account={acc!r}, Txn={txn!r} → {count} rows")
        #
        # # Show what distinguishes the duplicates — check fee_type / brokerage_type
        # print("\nSample duplicate group details:")
        # for (acc, txn), count in list(duplicates.items())[:3]:
        #     group = [r for r in rows if r[6] == acc and r[27] == txn]
        #     print(f"\n  Group: {acc!r} / {txn!r} ({count} rows)")
        #     for i, row in enumerate(group):
        #         # Indices: brokerage_type=26, fee_type=40, amount=17, brokerage=22
        #         print(f"    Row {i + 1}: type={row[26]!r}, fee={row[40]!r}, amt={row[17]!r}, brok={row[22]!r}")
        # # ═══ END DIAGNOSTIC ═══

        # DEBUG: Print parsed values for first row
        if idx == 0:
            log.warning("PARSED AMOUNT: %s", amount)
            log.warning("PARSED BROKERAGE: %s", brokerage)
            log.warning("PARSED UNITS: %s", units)

        rows.append((
            raw_val(row.get(prod_col, "")) if prod_col else "",
            raw_val(row.get(fund_desc_col, "")) if fund_desc_col else "",
            raw_val(row.get(fund_col, "")) if fund_col else "",
            raw_val(row.get(scheme_col, "")) if scheme_col else "",
            raw_val(row.get(plan_col, "")) if plan_col else "",
            raw_val(row.get(option_col, "")) if option_col else "",
            acnt,
            raw_val(row.get(app_no_col, "")) if app_no_col else "",
            raw_val(row.get(inv_name_col, "")) if inv_name_col else "",
            raw_val(row.get(addr1_col, "")) if addr1_col else "",
            raw_val(row.get(addr2_col, "")) if addr2_col else "",
            raw_val(row.get(addr3_col, "")) if addr3_col else "",
            raw_val(row.get(city_col, "")) if city_col else "",
            raw_val(row.get(pin_col, "")) if pin_col else "",
            raw_val(row.get(txn_desc_col, "")) if txn_desc_col else "",
            raw_val(row.get(from_dt_col, "")) if from_dt_col else "",
            raw_val(row.get(to_dt_col, "")) if to_dt_col else "",
            amount,
            units,
            raw_val(row.get(txn_dt_col, "")) if txn_dt_col else "",
            raw_val(row.get(proc_dt_col, "")) if proc_dt_col else "",
            percentage,
            brokerage,
            raw_val(row.get(sub_broker_col, "")) if sub_broker_col else "",
            raw_val(row.get(acnt_type_col, "")) if acnt_type_col else "",
            raw_val(row.get(brokerage_head_col, "")) if brokerage_head_col else "",
            raw_val(row.get(brokerage_type_col, "")) if brokerage_type_col else "",
            raw_val(row.get(txn_no_col, "")) if txn_no_col else "",
            raw_val(row.get(branch_col, "")) if branch_col else "",
            raw_val(row.get(cheque_col, "")) if cheque_col else "",
            raw_val(row.get(start_dt_col, "")) if start_dt_col else "",
            raw_val(row.get(end_dt_col, "")) if end_dt_col else "",
            raw_val(row.get(warrant_no_col, "")) if warrant_no_col else "",
            raw_val(row.get(warrant_dt_col, "")) if warrant_dt_col else "",
            daily_product,
            cumulative_nav,
            average_assets,
            raw_val(row.get(txn_id_col, "")) if txn_id_col else "",
            raw_val(row.get(scheme_code_col, "")) if scheme_code_col else "",
            raw_val(row.get(txn_head_col, "")) if txn_head_col else "",
            raw_val(row.get(fee_type_col, "")) if fee_type_col else "",
            raw_val(row.get(adj_flag_col, "")) if adj_flag_col else "",
            raw_val(row.get(switch_flag_col, "")) if switch_flag_col else "",
            gross_brokerage,
            stt_amount,
            educess_amount,
            raw_val(row.get(tran_type_col, "")) if tran_type_col else "",
            batch
        ))

    if not rows:
        return False, "0 valid rows found", {}

    brok_columns = ["product_code", "fund_description", "fund", "scheme", "plan", "option", "account_number",
                    "application_number", "investor_name", "address1", "address2", "address3", "city", "pincode",
                    "transaction_description", "from_date", "to_date", "amount", "units", "transaction_date",
                    "process_date", "percentage", "brokerage", "sub_broker", "account_type", "brokerage_head",
                    "brokerage_type", "transaction_number", "branch_code", "cheque_number", "starting_date",
                    "ending_date", "warrant_number", "warrant_date", "daily_product", "cumulative_nav",
                    "average_assets", "transaction_id", "scheme_code", "transaction_head", "fee_type",
                    "adjustment_flag", "switch_flag", "gross_brokerage", "stt_amount", "educess_amount",
                    "tran_type_code", "upload_batch"]
    conflict_cols = ["transaction_number", "account_number", "from_date", "percentage"]
    update_cols = [c for c in brok_columns if c not in conflict_cols]
    insert_sql = (
            f"INSERT INTO kfin_mfsd205_brokerage ({', '.join(brok_columns)}) "
            f"VALUES ({', '.join(['?'] * len(brok_columns))}) "
            f"ON CONFLICT({', '.join(conflict_cols)}) DO UPDATE SET " + ", ".join(
        f"{c}=excluded.{c}" for c in update_cols)
    )

    inserted = updated = 0
    with get_conn() as conn:
        if replace:
            conn.execute("DELETE FROM kfin_mfsd205_brokerage")
            conn.executemany(insert_sql, rows)
            inserted = len(rows)
        else:
            before = _count_before_after(conn, "kfin_mfsd205_brokerage")
            conn.executemany(insert_sql, rows)
            after = _count_before_after(conn, "kfin_mfsd205_brokerage")
            inserted = after - before
            updated = len(rows) - inserted

    if inserted or updated:
        bump()

    msg = f"Imported {inserted} new | Updated {updated} existing | Skipped: {skipped}"
    return True, msg, {"rows": inserted, "skipped": skipped, "updated": updated}


