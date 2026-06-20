"""
Parsers for KFinTech (Karvy) RTA files: AUM report, Brokerage report.
Column names from KFinTech files are far less standardized than CAMS,
so we fuzzy-match against a list of known header variants per field.
"""
from datetime import datetime

import pandas as pd

from core.db import get_conn
from core.helpers import clean_header, clean_str, format_aum, parse_date_safe


def _read_delimited(file, min_cols: int = 5):
    last_err = ""
    for sep in ("\t", ","):
        try:
            file.seek(0)
            df = pd.read_csv(file, sep=sep, dtype=str, encoding="utf-8", encoding_errors="replace")
            if len(df.columns) > min_cols:
                return df, None
        except Exception as exc:
            last_err = str(exc)
    return None, last_err


def _safe_float(val) -> float:
    try:
        if val is None or pd.isna(val):
            return 0.0
        return float(str(val).replace(",", "").strip()) if str(val).strip() not in ("", "None", "NaN", "NULL") else 0.0
    except (ValueError, TypeError):
        return 0.0


def _find_col(df_columns, candidates):
    for c in candidates:
        matches = [col for col in df_columns if col.upper().replace(" ", "_") == c.upper().replace(" ", "_")]
        if matches:
            return matches[0]
        for col in df_columns:
            if c.upper() in col.upper() or col.upper() in c.upper():
                return col
    return None


# ==================== AUM ====================
def parse_kfintech_aum(file, replace: bool) -> tuple[bool, str, dict]:
    df, err = _read_delimited(file)
    if df is None:
        return False, f"Could not parse KFinTech AUM file — {err}", {}
    df.columns = [clean_header(c).replace("#", "").replace("  ", " ").strip() for c in df.columns]

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
    resolved = {key: _find_col(df.columns, candidates) for key, candidates in col_mappings.items()}
    required = ["folio_no", "rupee_bal"]
    missing = [c for c in required if resolved[c] is None]
    if missing:
        return False, f"Missing required columns: {missing}. Found: {list(df.columns)[:20]}. Resolved: {resolved}", {}

    batch_id = f"KF_{file.name}_{datetime.now().strftime('%Y%m%d%H%M%S')}"
    rows, skipped = [], 0
    for _, row in df.iterrows():
        folio = clean_str(row.get(resolved["folio_no"], ""))
        if not folio:
            skipped += 1
            continue
        try:
            aum_val = _safe_float(row.get(resolved["rupee_bal"], 0)) if resolved["rupee_bal"] else 0.0
            if aum_val == 0 and resolved["nav"] and resolved["units"]:
                nav_val = _safe_float(row.get(resolved["nav"], 0))
                units_val = _safe_float(row.get(resolved["units"], 0))
                aum_val = nav_val * units_val
            rows.append((
                folio, clean_str(row.get(resolved["inv_name"], "")).strip(),
                clean_str(row.get(resolved["scheme_name"], "")), clean_str(row.get(resolved["amc_code"], "")),
                clean_str(row.get(resolved["product_code"], "")), clean_str(row.get(resolved["scheme_code"], "")),
                clean_str(row.get(resolved["dividend_opt"], "")), clean_str(row.get(resolved["email"], "")).lower(),
                parse_date_safe(row.get(resolved["rep_date"], "")),
                _safe_float(row.get(resolved["units"], 0)) if resolved["units"] else 0.0, aum_val,
                _safe_float(row.get(resolved["nav"], 0)) if resolved["nav"] else 0.0, aum_val, batch_id,
            ))
        except Exception:
            skipped += 1

    if not rows:
        return False, "0 KFinTech AUM rows imported (all skipped or invalid)", {}

    cols = ("folio_no,inv_name,scheme_name,amc_code,product_code,scheme_code,dividend_opt,"
            "email,rep_date,units,rupee_bal,nav,aum,upload_batch")
    placeholders = ",".join(["?"] * 14)
    inserted = duplicate_skipped = 0
    with get_conn() as conn:
        if replace:
            conn.execute("DELETE FROM kfintech_aum")
            conn.executemany(f"INSERT OR IGNORE INTO kfintech_aum ({cols}) VALUES ({placeholders})", rows)
            inserted = len(rows)
        else:
            before = conn.execute("SELECT COUNT(*) FROM kfintech_aum").fetchone()[0]
            conn.executemany(f"INSERT OR IGNORE INTO kfintech_aum ({cols}) VALUES ({placeholders})", rows)
            after = conn.execute("SELECT COUNT(*) FROM kfintech_aum").fetchone()[0]
            inserted, duplicate_skipped = after - before, len(rows) - inserted

    total_aum = sum(r[10] for r in rows)
    preview = {"rows": inserted, "skipped": skipped, "duplicates": duplicate_skipped, "total_aum": total_aum}
    msg = f"Imported {inserted} KFinTech AUM rows | Total AUM: {format_aum(total_aum)}"
    if skipped:
        msg += f" | Skipped {skipped}"
    if duplicate_skipped:
        msg += f" | Skipped {duplicate_skipped} duplicates"
    return True, msg, preview


# ==================== Brokerage ====================
def parse_kfintech_brokerage(file, replace: bool) -> tuple[bool, str, dict]:
    df, err = _read_delimited(file)
    if df is None:
        return False, f"Could not parse file — {err}", {}
    df.columns = [clean_header(c) for c in df.columns]

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
    resolved = {key: _find_col(df.columns, candidates) for key, candidates in col_mappings.items()}
    required = ["folio_no", "brkage_amt", "amc_code"]
    missing = [c for c in required if resolved[c] is None]
    if missing:
        return False, f"Missing columns: {missing}. Found: {list(df.columns)[:15]}", {}

    batch_id = f"KF_{file.name}_{datetime.now().strftime('%Y%m%d%H%M%S')}"

    def _parse_accrual_month(val) -> str:
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
                clean_str(row.get(resolved["trxn_type"], "")), _safe_float(row.get(resolved["brkage_amt"], 0)),
                clean_str(row.get(resolved["brkage_type"], "")), _safe_float(row.get(resolved["brkage_rate"], 0)),
                clean_str(row.get(resolved["inv_name"], "")).strip(),
                parse_date_safe(row.get(resolved["proc_date"], "")),
                _parse_accrual_month(row.get(resolved["accrual_month"], "")),
                _safe_float(row.get(resolved["plot_amount"], 0)), _safe_float(row.get(resolved["avg_assets"], 0)),
                0.0, 0.0, 0.0, batch_id,
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
            conn.execute("DELETE FROM kfintech_brokerage")
            conn.executemany(f"INSERT OR IGNORE INTO kfintech_brokerage ({cols}) VALUES ({placeholders})", rows)
            inserted = len(rows)
        else:
            before = conn.execute("SELECT COUNT(*) FROM kfintech_brokerage").fetchone()[0]
            conn.executemany(f"INSERT OR IGNORE INTO kfintech_brokerage ({cols}) VALUES ({placeholders})", rows)
            after = conn.execute("SELECT COUNT(*) FROM kfintech_brokerage").fetchone()[0]
            inserted, duplicate_skipped = after - before, len(rows) - inserted

    total_amount = sum(r[5] for r in rows)
    preview = {
        "rows": inserted, "skipped": skipped, "duplicates": duplicate_skipped, "total_brokerage": total_amount,
        "months": sorted({r[10] for r in rows if r[10]}),
    }
    msg = f"Imported {inserted} KFinTech rows | Rs {total_amount:,.2f} brokerage"
    if skipped:
        msg += f" | Skipped {skipped} invalid"
    if duplicate_skipped:
        msg += f" | Skipped {duplicate_skipped} duplicates"
    return True, msg, preview
