"""
Parsers for BSE StAR MF uploads: Client Master, SIP Report.
"""
from datetime import datetime

import pandas as pd

from core.db import get_conn
from core.helpers import clean_str, get_rta, is_active_status, normalize_columns, normalize_folio, parse_date_safe


def parse_client_master(file, replace: bool) -> tuple[bool, str]:
    file.seek(0)
    try:
        df = pd.read_excel(file)
    except Exception as exc:
        return False, f"File read error: {exc}"
    df = normalize_columns(df)

    def _col(*candidates):
        return next((c for c in candidates if c in df.columns), None)

    code_col, fname_col = _col("client_code", "member_code"), _col("primary_holder_first_name")
    lname_col, pan_col = _col("primary_holder_last_name"), _col("primary_holder_pan")
    mobile_col, email_col = _col("indian_mobile_no_"), _col("email", "primary_holder_email")
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
                "INSERT INTO clients (client_code, name, pan, mobile, email, kyc_status, start_date) "
                "VALUES (?,?,?,?,?,?,?)", rows)
            inserted = len(rows)
        else:
            existing = {r[0] for r in conn.execute("SELECT client_code FROM clients").fetchall()}
            new_rows = [r for r in rows if r[0] not in existing]
            skipped = len(rows) - len(new_rows)
            if new_rows:
                conn.executemany(
                    "INSERT OR IGNORE INTO clients (client_code, name, pan, mobile, email, kyc_status, start_date) "
                    "VALUES (?,?,?,?,?,?,?)", new_rows)
            inserted = len(new_rows)

    msg = f"Imported {inserted} clients"
    if skipped:
        msg += f" | Skipped {skipped} already existing"
    return True, msg


def parse_sip_report(file, replace: bool) -> tuple[bool, str, dict]:
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
        preview["active"], preview["cancelled"] = int(active_count), len(df) - int(active_count)
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
        except Exception:
            skipped += 1

    if not rows:
        return False, "0 SIPs imported (all skipped or invalid)", preview

    inserted = duplicate_skipped = 0
    with get_conn() as conn:
        if replace:
            conn.execute("DELETE FROM holdings")
            conn.executemany(
                "INSERT INTO holdings (client_code, folio_no, scheme_code, scheme_name, amc, rta, "
                "investment_type, sip_amount, sip_day, start_date, end_date, status, first_order) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            inserted = len(rows)
        else:
            existing = set(conn.execute(
                "SELECT LOWER(TRIM(folio_no)), LOWER(TRIM(scheme_code)), start_date FROM holdings"
            ).fetchall())
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
                    "INSERT INTO holdings (client_code, folio_no, scheme_code, scheme_name, amc, rta, "
                    "investment_type, sip_amount, sip_day, start_date, end_date, status, first_order) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", new_rows)
            inserted = len(new_rows)

    msg = f"Imported {inserted} Active SIPs"
    if skipped:
        msg += f" | Skipped {skipped} cancelled/invalid"
    if duplicate_skipped:
        msg += f" | Skipped {duplicate_skipped} already existing"
    return True, msg, preview
