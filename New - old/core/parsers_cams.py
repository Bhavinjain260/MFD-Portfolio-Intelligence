"""
Parsers for CAMS RTA files:
  WBR4  -> AUM report           -> parse_cams_aum
  WBR2  -> Transaction report   -> parse_cams_transactions
  WBR9  -> Folio master         -> parse_cams_folio_master
  WBR49 -> SIP master           -> parse_cams_sip_master
  (separate) Brokerage report   -> parse_cams_brokerage
"""
from datetime import datetime

import pandas as pd

from core.db import get_conn
from core.helpers import clean_header, clean_str, format_aum, parse_date_safe


def _read_delimited(file, min_cols: int = 5):
    """Try tab then comma separated. Returns (df, error) — df is None on failure."""
    last_err = ""
    for sep in ("\t", ","):
        try:
            file.seek(0)
            df = pd.read_csv(file, sep=sep, quotechar="'", dtype=str,
                             encoding="utf-8", encoding_errors="replace")
            if len(df.columns) > min_cols:
                return df, None
        except Exception as exc:
            last_err = str(exc)
    return None, last_err


def _safe_float(val) -> float:
    try:
        return float(str(val).replace(",", "").strip()) if val not in ("", None) else 0.0
    except (ValueError, TypeError):
        return 0.0


# ==================== WBR4 — AUM ====================
def parse_cams_aum(file, replace: bool) -> tuple[bool, str, dict]:
    df, err = _read_delimited(file)
    if df is None:
        return False, f"Could not parse AUM file — {err}", {}
    df.columns = [clean_header(c) for c in df.columns]
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
                folio, clean_str(row.get("INV_NAME", "")).strip(), clean_str(row.get("SCH_NAME", "")),
                clean_str(row.get("AMC_CODE", "")), clean_str(row.get("PAN_NO", "")).upper(),
                clean_str(row.get("EMAIL", "")).lower(), parse_date_safe(row.get("REP_DATE", "")),
                _safe_float(row.get("CLOS_BAL", 0)), _safe_float(row.get("RUPEE_BAL", 0)), batch_id,
            ))
        except Exception:
            skipped += 1

    if not rows:
        return False, "0 AUM rows imported", {}

    inserted = duplicate_skipped = 0
    with get_conn() as conn:
        if replace:
            conn.execute("DELETE FROM cams_aum")
            conn.executemany(
                "INSERT OR IGNORE INTO cams_aum (folio_no, inv_name, scheme_name, amc_code, pan_no, email, "
                "rep_date, units, rupee_bal, upload_batch) VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
            inserted = len(rows)
        else:
            before = conn.execute("SELECT COUNT(*) FROM cams_aum").fetchone()[0]
            conn.executemany(
                "INSERT OR IGNORE INTO cams_aum (folio_no, inv_name, scheme_name, amc_code, pan_no, email, "
                "rep_date, units, rupee_bal, upload_batch) VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
            after = conn.execute("SELECT COUNT(*) FROM cams_aum").fetchone()[0]
            inserted, duplicate_skipped = after - before, len(rows) - inserted

    total_aum = sum(r[8] for r in rows)
    preview = {"rows": inserted, "skipped": skipped, "duplicates": duplicate_skipped, "total_aum": total_aum}
    msg = f"Imported {inserted} AUM rows | Total AUM: {format_aum(total_aum)}"
    if skipped:
        msg += f" | Skipped {skipped}"
    if duplicate_skipped:
        msg += f" | Skipped {duplicate_skipped} duplicates"
    return True, msg, preview


# ==================== WBR2 — Transactions ====================
def parse_cams_transactions(file, replace: bool) -> tuple[bool, str, dict]:
    df, err = _read_delimited(file, min_cols=10)
    if df is None:
        return False, "Could not parse R2 transaction file", {}
    df.columns = [clean_header(c) for c in df.columns]

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
                clean_str(row.get("TRXNSTAT", "")), parse_date_safe(row.get("TRADDATE", "")),
                parse_date_safe(row.get("POSTDATE", "")), _safe_float(row.get("PURPRICE", 0)),
                _safe_float(row.get("UNITS", 0)), _safe_float(row.get("AMOUNT", 0)),
                clean_str(row.get("PAN", "")), clean_str(row.get("REMARKS", "")),
                clean_str(row.get("SIPTRXNNO", "")), _safe_float(row.get("IGST_AMOUNT", 0)),
                _safe_float(row.get("CGST_AMOUNT", 0)), _safe_float(row.get("SGST_AMOUNT", 0)),
                parse_date_safe(row.get("REP_DATE", "")), batch_id,
            ))
        except Exception:
            skipped += 1

    if not rows:
        return False, "0 transaction rows parsed", {}

    inserted = dupes = 0
    cols = ("amc_code,folio_no,scheme_code,scheme_name,inv_name,trxn_type,trxn_no,trxn_mode,trxn_status,"
            "trade_date,post_date,nav,units,amount,pan,remarks,sip_trxn_no,"
            "igst_amount,cgst_amount,sgst_amount,rep_date,upload_batch")
    placeholders = ",".join(["?"] * 22)
    with get_conn() as conn:
        if replace:
            conn.execute("DELETE FROM cams_transactions")
            conn.executemany(f"INSERT OR IGNORE INTO cams_transactions ({cols}) VALUES ({placeholders})", rows)
            inserted = len(rows)
        else:
            before = conn.execute("SELECT COUNT(*) FROM cams_transactions").fetchone()[0]
            conn.executemany(f"INSERT OR IGNORE INTO cams_transactions ({cols}) VALUES ({placeholders})", rows)
            after = conn.execute("SELECT COUNT(*) FROM cams_transactions").fetchone()[0]
            inserted = after - before
            dupes = len(rows) - inserted

    total_amt = sum(r[13] for r in rows)
    preview = {
        "rows": inserted, "skipped": skipped, "duplicates": dupes, "total_amount": total_amt,
        "schemes": len({r[3] for r in rows}), "folios": len({r[1] for r in rows}),
    }
    msg = f"Imported {inserted} transactions | Rs {total_amt:,.2f} total"
    if skipped:
        msg += f" | Skipped {skipped}"
    if dupes:
        msg += f" | {dupes} duplicates"
    return True, msg, preview


# ==================== WBR9 — Folio Master ====================
def parse_cams_folio_master(file, replace: bool) -> tuple[bool, str, dict]:
    df, err = _read_delimited(file, min_cols=10)
    if df is None:
        return False, "Could not parse R9 folio master file", {}
    df.columns = [clean_header(c) for c in df.columns]

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
                parse_date_safe(row.get("REP_DATE", "")), _safe_float(row.get("CLOS_BAL", 0)),
                _safe_float(row.get("RUPEE_BAL", 0)), clean_str(row.get("EMAIL", "")).lower(),
                clean_str(row.get("MOBILE_NO", "")), clean_str(row.get("PAN_NO", "")).upper(),
                clean_str(row.get("JOINT1_PAN", "")).upper(), clean_str(row.get("JOINT2_PAN", "")).upper(),
                clean_str(row.get("TAX_STATUS", "")), clean_str(row.get("HOLDING_NATURE", "")),
                clean_str(row.get("BANK_NAME", "")), clean_str(row.get("BRANCH", "")),
                clean_str(row.get("AC_TYPE", "")), clean_str(row.get("AC_NO", "")),
                clean_str(row.get("IFSC_CODE", "")), parse_date_safe(row.get("INV_DOB", "")),
                clean_str(row.get("NOM_NAME", "")), clean_str(row.get("RELATION", "")),
                parse_date_safe(row.get("FOLIO_DATE", "")), batch_id,
            ))
        except Exception:
            skipped += 1

    if not rows:
        return False, "0 folio master rows parsed", {}

    cols = ("folio_no,inv_name,address1,address2,address3,city,pincode,scheme_code,scheme_name,amc_code,"
            "rep_date,units,rupee_bal,email,mobile,pan_no,joint1_pan,joint2_pan,tax_status,holding_nature,"
            "bank_name,branch,ac_type,ac_no,ifsc_code,inv_dob,nominee_name,nominee_relation,folio_date,upload_batch")
    placeholders = ",".join(["?"] * 30)
    inserted = dupes = 0
    with get_conn() as conn:
        if replace:
            conn.execute("DELETE FROM cams_folio_master")
            conn.executemany(f"INSERT OR IGNORE INTO cams_folio_master ({cols}) VALUES ({placeholders})", rows)
            inserted = len(rows)
        else:
            before = conn.execute("SELECT COUNT(*) FROM cams_folio_master").fetchone()[0]
            conn.executemany(f"INSERT OR IGNORE INTO cams_folio_master ({cols}) VALUES ({placeholders})", rows)
            after = conn.execute("SELECT COUNT(*) FROM cams_folio_master").fetchone()[0]
            inserted = after - before
            dupes = len(rows) - inserted

    total_aum = sum(r[12] for r in rows)
    preview = {
        "rows": inserted, "skipped": skipped, "duplicates": dupes, "total_aum": total_aum,
        "unique_folios": len({r[0] for r in rows}), "unique_investors": len({r[1] for r in rows}),
    }
    msg = f"Imported {inserted} folio records | AUM: {format_aum(total_aum)}"
    if skipped:
        msg += f" | Skipped {skipped}"
    if dupes:
        msg += f" | {dupes} duplicates"
    return True, msg, preview


# ==================== WBR49 — SIP Master ====================
def parse_cams_sip_master(file, replace: bool) -> tuple[bool, str, dict]:
    df, err = _read_delimited(file)
    if df is None:
        return False, "Could not parse R49 SIP master file", {}
    df.columns = [clean_header(c) for c in df.columns]

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
                clean_str(row.get("SCHEME", "")), folio, clean_str(row.get("INV_NAME", "")).strip(),
                sip_no, _safe_float(row.get("AUTO_AMOUNT", 0)), parse_date_safe(row.get("FROM_DATE", "")),
                parse_date_safe(row.get("TO_DATE", "")), parse_date_safe(row.get("CEASE_DATE", "")),
                clean_str(row.get("PERIODICITY", "")), period_day, clean_str(row.get("PAN", "")).upper(),
                clean_str(row.get("PAYMENT_MODE", "")), clean_str(row.get("BANK", "")),
                parse_date_safe(row.get("REG_DATE", "")), clean_str(row.get("REMARKS", "")),
                status, batch_id,
            ))
        except Exception:
            skipped += 1

    if not rows:
        return False, "0 SIP master rows parsed", {}

    cols = ("amc_code,scheme_code,scheme_name,folio_no,inv_name,sip_reg_no,sip_amount,"
            "from_date,to_date,cease_date,periodicity,sip_day,pan,payment_mode,bank_name,"
            "reg_date,remarks,status,upload_batch")
    placeholders = ",".join(["?"] * 19)
    inserted = dupes = 0
    with get_conn() as conn:
        if replace:
            conn.execute("DELETE FROM cams_sip_master")
            conn.executemany(f"INSERT OR IGNORE INTO cams_sip_master ({cols}) VALUES ({placeholders})", rows)
            inserted = len(rows)
        else:
            before = conn.execute("SELECT COUNT(*) FROM cams_sip_master").fetchone()[0]
            conn.executemany(f"INSERT OR IGNORE INTO cams_sip_master ({cols}) VALUES ({placeholders})", rows)
            after = conn.execute("SELECT COUNT(*) FROM cams_sip_master").fetchone()[0]
            inserted = after - before
            dupes = len(rows) - inserted

    sc = {}
    for r in rows:
        sc[r[17]] = sc.get(r[17], 0) + 1

    preview = {
        "rows": inserted, "skipped": skipped, "duplicates": dupes,
        "active": sc.get("Active", 0), "ceased": sc.get("Ceased", 0), "completed": sc.get("Completed", 0),
    }
    msg = (f"Imported {inserted} SIPs | Active: {sc.get('Active', 0)} | "
           f"Ceased: {sc.get('Ceased', 0)} | Completed: {sc.get('Completed', 0)}")
    if skipped:
        msg += f" | Skipped {skipped}"
    if dupes:
        msg += f" | {dupes} duplicates"
    return True, msg, preview


# ==================== Brokerage ====================
def parse_cams_brokerage(file, replace: bool) -> tuple[bool, str, dict]:
    df, err = _read_delimited(file)
    if df is None:
        return False, f"Could not parse file — {err}", {}
    df.columns = [clean_header(c) for c in df.columns]

    required = ["FOLIO_NO", "BRKAGE_AMT", "AMC_CODE"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        return False, f"Missing columns: {missing}. Found: {list(df.columns)[:15]}", {}

    batch_id = f"{file.name}_{datetime.now().strftime('%Y%m%d%H%M%S')}"

    def _parse_accrual_month(val) -> str:
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
                _safe_float(row.get("BRKAGE_AMT", 0)), clean_str(row.get("BRKAGE_TYPE", "")),
                _safe_float(row.get("BRKAGE_RATE", 0)), clean_str(row.get("INV_NAME", "")).strip(),
                parse_date_safe(row.get("PROC_DATE", "")),
                _parse_accrual_month(row.get("BROKERAGE_ACRUAL_MONTH", "")),
                _safe_float(row.get("PLOT_AMOUNT", 0)), _safe_float(row.get("AVG_ASSETS", 0)),
                _safe_float(row.get("IGST_VALUE", 0)), _safe_float(row.get("CGST_VALUE", 0)),
                _safe_float(row.get("SGST_VALUE", 0)), batch_id,
            ))
        except Exception:
            skipped += 1

    if not rows:
        return False, "0 rows imported (all skipped or invalid)", {}

    cols = ("amc_code,folio_no,scheme_code,trxn_no,trxn_type,brkage_amt,brkage_type,brkage_rate,"
            "inv_name,proc_date,accrual_month,plot_amount,avg_assets,igst_value,cgst_value,sgst_value,upload_batch")
    placeholders = ",".join(["?"] * 17)
    inserted = duplicate_skipped = 0
    with get_conn() as conn:
        if replace:
            conn.execute("DELETE FROM cams_brokerage")
            conn.executemany(f"INSERT OR IGNORE INTO cams_brokerage ({cols}) VALUES ({placeholders})", rows)
            inserted = len(rows)
        else:
            before = conn.execute("SELECT COUNT(*) FROM cams_brokerage").fetchone()[0]
            conn.executemany(f"INSERT OR IGNORE INTO cams_brokerage ({cols}) VALUES ({placeholders})", rows)
            after = conn.execute("SELECT COUNT(*) FROM cams_brokerage").fetchone()[0]
            inserted, duplicate_skipped = after - before, len(rows) - inserted

    total_amount = sum(r[5] for r in rows)
    preview = {
        "rows": inserted, "skipped": skipped, "duplicates": duplicate_skipped, "total_brokerage": total_amount,
        "months": sorted({r[10] for r in rows if r[10]}),
    }
    msg = f"Imported {inserted} CAMS rows | Rs {total_amount:,.2f} brokerage"
    if skipped:
        msg += f" | Skipped {skipped} invalid"
    if duplicate_skipped:
        msg += f" | Skipped {duplicate_skipped} duplicates"
    return True, msg, preview
