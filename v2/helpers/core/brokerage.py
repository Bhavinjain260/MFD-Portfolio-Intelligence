"""
core/brokerage.py
=================
Brokerage report, sub-broker CRUD, attribution, periods.
Extracted from app.py (Phase 9). Pure: no Streamlit.

UI (render_*, PDF/Excel generators, email body) stays in ui/.
"""
from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

from core.db import get_conn, current as data_version

log = logging.getLogger(__name__)

# Optional AMFI index for AMC resolution — soft dependency
try:
    from core.nav import _amfi, fetch_amc_by_isin
except Exception:
    _amfi = None
    def fetch_amc_by_isin(isin: str) -> str:
        return ""


# --- from app.py L1847-2021: load_brokerage_report ---
def load_brokerage_report() -> dict:
    """
    Returns {
        "merged":     DataFrame[amc, month, file_amount, manual_amount, variance, status]
        "detail":     DataFrame[amc, rta, client, folio, scheme_code, isin, txn_date,
                                 txn_amount, brokerage_pct, brokerage_amount, brokerage_type]
        "manual_raw": DataFrame[amc, month, manual_amount]
    }

    AMC resolution path (matches Dashboard's proven join):
        brokerage.folio_no / account_number
            -> cams_wbr9_folio.foliochk / kfin_mfsd211_folio.folio   (get product code)
            -> Channel_Partner_Code (bse_scheme_master) -> ISIN
            -> _amfi.get_amc(isin)                       (canonical AMC name)

    Date handling: CAMS proc_date is DD-MM-YYYY, KFin proc_date is
    YYYY-MM-DD. Each is parsed in its OWN format BEFORE concatenation —
    parsing a combined column once with a single dayfirst= flag lets
    pandas' format inference lock onto whichever format it sees first
    and silently return NaT for every row in the other format.
    """
    # ---- Resolve BSE's AMC fallback column FIRST, on its own connection,
    #      BEFORE opening the connection used for the real reads ----
    bse_amc_col = _get_bse_amc_column()
    bse_amc_select = f"MAX({bse_amc_col}) AS bse_amc_name" if bse_amc_col else "NULL AS bse_amc_name"
    bse_dedup = f"""
        SELECT
            UPPER(TRIM(Channel_Partner_Code)) AS cp_code,
            MAX(ISIN) AS ISIN,
            {bse_amc_select}
        FROM bse_scheme_master
        WHERE Channel_Partner_Code IS NOT NULL AND TRIM(Channel_Partner_Code) != ''
        GROUP BY UPPER(TRIM(Channel_Partner_Code))
    """

    # ---- CAMS brokerage -> cams_wbr9_folio (get product code) -> Channel_Partner_Code -> ISIN ----
    cams_sql = f"""
        SELECT
            cb.proc_date              AS proc_date,
            cb.brokerage_accrual_month AS accrual_month,
            cb.inv_name               AS client,
            cb.folio_no               AS folio,
            cb.scheme_code            AS scheme_code,
            cb.trxn_no                AS txn_no,
            cb.plot_amount            AS txn_amount,
            cb.brkage_rate            AS brokerage_pct,
            cb.brkage_amt             AS brokerage_amount,
            cb.brkage_type            AS brokerage_type,
            cf.product                AS folio_product_code,
            sm.ISIN                   AS isin,
            sm.bse_amc_name           AS bse_amc_name,
            'CAMS'                    AS rta
        FROM cams_wbr77_brokerage cb
        LEFT JOIN cams_wbr9_folio cf
            ON UPPER(TRIM(cb.folio_no)) = UPPER(TRIM(cf.foliochk))
        LEFT JOIN ({bse_dedup}) sm
            ON UPPER(TRIM(cf.product)) = sm.cp_code
    """

    # ---- KFin brokerage -> kfin_mfsd211_folio (get product_code) -> Channel_Partner_Code -> ISIN ----
    kfin_sql = f"""
        SELECT
            kb.process_date        AS proc_date,
            NULL                   AS accrual_month,
            kb.investor_name       AS client,
            kb.account_number      AS folio,
            kb.scheme_code         AS scheme_code,
            kb.transaction_number  AS txn_no,
            kb.amount           AS txn_amount,
            kb.percentage          AS brokerage_pct,
            kb.brokerage        AS brokerage_amount,
            kb.brokerage_type      AS brokerage_type,
            kf.product_code        AS folio_product_code,
            sm.ISIN                AS isin,
            sm.bse_amc_name        AS bse_amc_name,
            'KFinTech'             AS rta
        FROM kfin_mfsd205_brokerage kb
        LEFT JOIN kfin_mfsd211_folio kf
            ON UPPER(TRIM(kb.account_number)) = UPPER(TRIM(kf.folio))
        LEFT JOIN ({bse_dedup}) sm
            ON UPPER(TRIM(kf.product_code)) = sm.cp_code
    """

    # ---- Open the connection once, run both reads on it, then close ----
    with get_conn() as conn:
        cams_df = pd.read_sql(cams_sql, conn)
        kfin_df = pd.read_sql(kfin_sql, conn)
        manual_df = pd.read_sql("SELECT amc, month, year, amount FROM monthly_brokerage", conn)

    # ---- Parse each RTA's date format SEPARATELY, before combining ----
    # NOTE: both CAMS and KFin proc_date are YYYY-MM-DD in this dataset —
    # confirmed via direct query (e.g. CAMS: '2026-02-06', '2026-05-06').
    # dayfirst=True on an already year-first string SILENTLY SWAPS month
    # and day (e.g. '2026-02-06' -> June 2nd instead of Feb 6th) instead
    # of raising an error, which is what caused older-month CAMS rows to
    # disappear (they got miscategorized into the current month). Use
    # dayfirst=False for both — if a future upload genuinely uses
    # DD-MM-YYYY, change that side's flag back to True at that time.
    if not cams_df.empty:
        cams_df["proc_date"] = pd.to_datetime(cams_df["proc_date"], errors="coerce", dayfirst=False)
    if not kfin_df.empty:
        kfin_df["proc_date"] = pd.to_datetime(kfin_df["proc_date"], errors="coerce", dayfirst=False)

    detail = pd.concat([cams_df, kfin_df], ignore_index=True)

    # ---- AMC name: AMFI-canonical (via ISIN) first, then BSE fallback, then raw product code ----
    detail["amfi_amc_name"] = detail["isin"].apply(
        lambda i: (fetch_amc_by_isin(str(i)) if _amfi is None else _amfi.get_amc(i))
                   if pd.notna(i) and str(i).strip() else None
    )
    detail["amc"] = detail["amfi_amc_name"]
    detail["amc"] = detail["amc"].fillna(detail["bse_amc_name"])
    detail["amc"] = detail["amc"].fillna(detail["folio_product_code"])
    detail["amc"] = detail["amc"].fillna(detail["scheme_code"])
    detail["amc"] = detail["amc"].fillna("⚠️ Unresolved")

    # ---- Month key: prefer explicit accrual_month (CAMS), else derive from
    #      the already-parsed proc_date. Anything still unresolved falls
    #      into an explicit "Unknown" bucket instead of silently vanishing
    #      from downstream filters (which never match NaN). ----
    month_from_accrual = detail["accrual_month"].astype(str).str.strip()
    month_from_proc = detail["proc_date"].dt.strftime("%Y-%m")
    detail["month"] = month_from_accrual.where(
        month_from_accrual.str.match(r"^\d{4}-\d{2}$", na=False), month_from_proc
    )
    detail["month"] = detail["month"].fillna("Unknown")

    detail["txn_amount"] = pd.to_numeric(detail["txn_amount"], errors="coerce")
    detail["brokerage_pct"] = pd.to_numeric(detail["brokerage_pct"], errors="coerce")
    detail["brokerage_amount"] = pd.to_numeric(detail["brokerage_amount"], errors="coerce").fillna(0.0)
    detail["txn_date"] = detail["proc_date"].dt.strftime("%Y-%m-%d")

    # ---- File-side grouped by AMC + month ----
    file_grouped = (
        detail.dropna(subset=["month"])
        .groupby(["amc", "month"], dropna=False)["brokerage_amount"]
        .sum()
        .reset_index()
        .rename(columns={"brokerage_amount": "file_amount"})
    )

    # ---- Manual entries ----
    if not manual_df.empty:
        manual_df["month"] = manual_df["year"].astype(str) + "-" + manual_df["month"].astype(str).str.zfill(2)
        manual_grouped = (
            manual_df.groupby(["amc", "month"], dropna=False)["amount"]
            .sum()
            .reset_index()
            .rename(columns={"amount": "manual_amount"})
        )
    else:
        manual_grouped = pd.DataFrame(columns=["amc", "month", "manual_amount"])

    merged = pd.merge(file_grouped, manual_grouped, on=["amc", "month"], how="outer")
    merged["file_amount"] = merged["file_amount"].fillna(0.0)
    merged["manual_amount"] = merged["manual_amount"].fillna(0.0)
    merged["variance"] = merged["file_amount"] - merged["manual_amount"]

    def _status(row):
        if row["manual_amount"] == 0 and row["file_amount"] > 0:
            return "⚠️ Not yet received"
        if row["file_amount"] == 0 and row["manual_amount"] > 0:
            return "❓ Received, no file match"
        if abs(row["variance"]) < 1:
            return "✅ Matched"
        return "🔶 Mismatch"

    if not merged.empty:
        merged["status"] = merged.apply(_status, axis=1)
        merged = merged.sort_values(["month", "amc"], ascending=[False, True])
    else:
        merged["status"] = pd.Series(dtype="object")

    return {"merged": merged, "detail": detail, "manual_raw": manual_grouped}



# --- from app.py L2174-2197: get_bse_amc ---
def _get_bse_amc_column() -> Optional[str]:
    """
    Probe bse_scheme_master's actual schema to find whichever column holds
    the AMC/fund-house name string. Used only as a fallback label for ISINs
    that don't resolve via AMFI.
    """
    candidates = [
        "amc_code", "amc_ind", "amc_name",
        "AMC", "AMC_Name", "AMC_NAME", "Amc_Name",
        "Fund_Name", "FUND_NAME", "Fund", "FUND",
        "AMC_Code", "AMC_CODE",
    ]
    with get_conn() as conn:
        cols = [row[1] for row in conn.execute("PRAGMA table_info(bse_scheme_master)").fetchall()]
    # log.info("[NAV-FLOW] bse_scheme_master columns: %s", cols)
    for c in candidates:
        if c in cols:
            log.info("[NAV-FLOW] Using '%s' as BSE AMC-name fallback column", c)
            return c
    log.warning("[NAV-FLOW] No AMC-name column found in bse_scheme_master (checked %s). "
                "BSE fallback name disabled — rows will rely on AMFI ISIN match only.", candidates)
    return None


# --- from app.py L3299-3615: sub_broker_block ---
def ensure_sub_broker_tables() -> None:
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sub_brokers (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                name         TEXT NOT NULL,
                email        TEXT,
                phone        TEXT,
                role         TEXT NOT NULL DEFAULT 'Sub-Broker',
                sharing_pct  REAL NOT NULL DEFAULT 50.0,
                is_active    INTEGER DEFAULT 1,
                notes        TEXT,
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sub_broker_clients (
                sub_broker_id INTEGER NOT NULL,
                client_code   TEXT NOT NULL UNIQUE,
                assigned_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (sub_broker_id, client_code)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sub_broker_payouts (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                sub_broker_id      INTEGER NOT NULL,
                period_type        TEXT NOT NULL,
                period_key         TEXT NOT NULL,
                brokerage_generated REAL NOT NULL DEFAULT 0,
                share_amount       REAL NOT NULL DEFAULT 0,
                transferred_amount REAL NOT NULL DEFAULT 0,
                transfer_date      TEXT,
                transfer_reference TEXT,
                transfer_notes     TEXT,
                status             TEXT NOT NULL DEFAULT 'pending',
                created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(sub_broker_id, period_type, period_key)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sbc_sub    ON sub_broker_clients(sub_broker_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sbc_client ON sub_broker_clients(client_code)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sbp_sub    ON sub_broker_payouts(sub_broker_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sbp_period ON sub_broker_payouts(period_type, period_key)")


# ══════════════════════════════════════════════════════════════
# SUB-BROKER CRUD
# ══════════════════════════════════════════════════════════════
def list_sub_brokers() -> pd.DataFrame:
    with get_conn() as conn:
        return pd.read_sql("""
            SELECT sb.id, sb.name, sb.email, sb.phone, sb.role,
                   sb.sharing_pct, sb.is_active, sb.notes, sb.created_at,
                   (SELECT COUNT(*) FROM sub_broker_clients sbc
                    WHERE sbc.sub_broker_id = sb.id) AS client_count
            FROM sub_brokers sb
            ORDER BY sb.is_active DESC, sb.name
        """, conn)


def create_sub_broker(name: str, email: str, phone: str,
                      sharing_pct: float, notes: str = "") -> int:
    with get_conn() as conn:
        cur = conn.execute("""
            INSERT INTO sub_brokers (name, email, phone, role, sharing_pct, notes)
            VALUES (?, ?, ?, 'Sub-Broker', ?, ?)
        """, (name.strip(), email.strip() or None, phone.strip() or None,
              float(sharing_pct), notes.strip() or None))
        return cur.lastrowid


def update_sub_broker(sb_id: int, **kwargs) -> None:
    allowed = {"name", "email", "phone", "sharing_pct", "is_active", "notes"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    with get_conn() as conn:
        conn.execute(f"UPDATE sub_brokers SET {set_clause} WHERE id = ?",
                     list(fields.values()) + [sb_id])


def delete_sub_broker(sb_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM sub_broker_clients WHERE sub_broker_id = ?", (sb_id,))
        conn.execute("DELETE FROM sub_broker_payouts WHERE sub_broker_id = ?", (sb_id,))
        conn.execute("DELETE FROM sub_brokers WHERE id = ?", (sb_id,))


def assign_clients_to_sub_broker(sb_id: int, client_codes: list) -> None:
    with get_conn() as conn:
        for cc in client_codes:
            conn.execute("DELETE FROM sub_broker_clients WHERE client_code = ?", (cc,))
            conn.execute("""
                INSERT INTO sub_broker_clients (sub_broker_id, client_code)
                VALUES (?, ?)
            """, (sb_id, cc))


def unassign_client_from_sub_broker(sb_id: int, client_code: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM sub_broker_clients WHERE sub_broker_id = ? AND client_code = ?",
            (sb_id, client_code)
        )


def get_clients_for_sub_broker(sb_id: int) -> pd.DataFrame:
    with get_conn() as conn:
        return pd.read_sql("""
            SELECT sbc.client_code,
                   COALESCE(
                       NULLIF(TRIM(COALESCE(bcm.primary_holder_first_name,'') || ' ' ||
                                   COALESCE(bcm.primary_holder_last_name,'')), ''),
                       sbc.client_code
                   ) AS name,
                   bcm.primary_holder_pan AS pan,
                   bcm.indian_mobile_no   AS mobile,
                   bcm.email,
                   sbc.assigned_at
            FROM sub_broker_clients sbc
            LEFT JOIN bse_client_master bcm ON sbc.client_code = bcm.client_code
            WHERE sbc.sub_broker_id = ?
            ORDER BY name
        """, conn, params=(sb_id,))


def get_client_to_sub_broker_map() -> dict:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT client_code, sub_broker_id FROM sub_broker_clients"
        ).fetchall()
    return {r[0]: r[1] for r in rows}


def get_folio_to_client_code_map() -> dict:
    """Map folio_id → client_code via PAN matching (adults + minor/guardian)."""
    with get_conn() as conn:
        cams = pd.read_sql("""
            SELECT foliochk AS folio, UPPER(TRIM(pan_no)) AS pan
            FROM cams_wbr9_folio
            WHERE pan_no IS NOT NULL AND TRIM(pan_no) != ''
            UNION ALL
            SELECT foliochk AS folio, UPPER(TRIM(guard_pan)) AS pan
            FROM cams_wbr9_folio
            WHERE guard_pan IS NOT NULL AND TRIM(guard_pan) != ''
        """, conn)
        kfin = pd.read_sql("""
            SELECT folio AS folio, UPPER(TRIM(pan_number)) AS pan
            FROM kfin_mfsd211_folio
            WHERE pan_number IS NOT NULL AND TRIM(pan_number) != ''
        """, conn)
        clients = pd.read_sql("""
            SELECT client_code,
                   UPPER(TRIM(primary_holder_pan)) AS pan
            FROM bse_client_master
            WHERE primary_holder_pan IS NOT NULL AND TRIM(primary_holder_pan) != ''
            UNION ALL
            SELECT client_code,
                   UPPER(TRIM(guardian_pan)) AS pan
            FROM bse_client_master
            WHERE guardian_pan IS NOT NULL AND TRIM(guardian_pan) != ''
        """, conn)

    folios  = pd.concat([cams, kfin], ignore_index=True).drop_duplicates("folio")
    clients = clients.drop_duplicates("pan", keep="first")
    merged  = folios.merge(clients, on="pan", how="left")
    return dict(zip(merged["folio"].astype(str), merged["client_code"]))


# ══════════════════════════════════════════════════════════════
# SUB-BROKER PAYOUT RECORDS
# ══════════════════════════════════════════════════════════════
def list_payouts(sub_broker_id: int | None = None) -> pd.DataFrame:
    q = """
        SELECT p.id, p.sub_broker_id, sb.name AS sub_broker_name,
               p.period_type, p.period_key,
               p.brokerage_generated, p.share_amount,
               p.transferred_amount, p.transfer_date,
               p.transfer_reference, p.transfer_notes,
               p.status, p.created_at, p.updated_at
        FROM sub_broker_payouts p
        LEFT JOIN sub_brokers sb ON p.sub_broker_id = sb.id
    """
    params = []
    if sub_broker_id is not None:
        q += " WHERE p.sub_broker_id = ?"
        params.append(sub_broker_id)
    q += " ORDER BY p.period_key DESC, sb.name"
    with get_conn() as conn:
        return pd.read_sql(q, conn, params=params)


def record_payout(sb_id: int, period_type: str, period_key: str,
                  brokerage_generated: float, share_amount: float,
                  transferred_amount: float, transfer_date: str,
                  transfer_reference: str = "", transfer_notes: str = "") -> None:
    if transferred_amount <= 0:
        status = "pending"
    elif transferred_amount + 0.01 >= share_amount:
        status = "transferred"
    else:
        status = "partial"

    with get_conn() as conn:
        conn.execute("""
            INSERT INTO sub_broker_payouts
                (sub_broker_id, period_type, period_key,
                 brokerage_generated, share_amount,
                 transferred_amount, transfer_date, transfer_reference,
                 transfer_notes, status, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(sub_broker_id, period_type, period_key) DO UPDATE SET
                brokerage_generated = excluded.brokerage_generated,
                share_amount        = excluded.share_amount,
                transferred_amount  = excluded.transferred_amount,
                transfer_date       = excluded.transfer_date,
                transfer_reference  = excluded.transfer_reference,
                transfer_notes      = excluded.transfer_notes,
                status              = excluded.status,
                updated_at          = CURRENT_TIMESTAMP
        """, (sb_id, period_type, period_key,
              float(brokerage_generated), float(share_amount),
              float(transferred_amount), transfer_date,
              transfer_reference.strip() or None,
              transfer_notes.strip() or None, status))


def delete_payout(payout_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM sub_broker_payouts WHERE id = ?", (payout_id,))


# ══════════════════════════════════════════════════════════════
# BROKERAGE ATTRIBUTION + SHARING COMPUTATION
# ══════════════════════════════════════════════════════════════
def _attribute_brokerage(period_type: str, period_key: str) -> pd.DataFrame:
    """Return brokerage detail rows for the period, enriched with sub_broker_id."""
    data = load_brokerage_report()
    detail = data["detail"].copy()
    if detail.empty:
        return detail

    if period_type == "month":
        detail = detail[detail["month"] == period_key]
    else:  # year
        detail = detail[detail["month"].astype(str).str.startswith(period_key + "-")]

    if detail.empty:
        return detail

    folio_client = get_folio_to_client_code_map()
    client_sb    = get_client_to_sub_broker_map()

    detail["client_code"]   = detail["folio"].astype(str).map(folio_client)
    detail["sub_broker_id"] = detail["client_code"].map(client_sb)
    return detail


def compute_sub_broker_summary(period_type: str, period_key: str) -> pd.DataFrame:
    """One row per sub-broker with their brokerage + share for the period."""
    detail = _attribute_brokerage(period_type, period_key)
    sbs    = list_sub_brokers()
    if detail.empty or sbs.empty:
        return pd.DataFrame()

    sb_lookup = sbs.set_index("id").to_dict("index")
    assigned  = detail[detail["sub_broker_id"].notna()]

    rows = []
    for sb_id, grp in assigned.groupby("sub_broker_id"):
        if sb_id not in sb_lookup:
            continue
        sb = sb_lookup[sb_id]
        pct  = float(sb["sharing_pct"] or 0.0)
        brok = float(grp["brokerage_amount"].sum())
        share = brok * pct / 100.0
        rows.append({
            "sub_broker_id":    int(sb_id),
            "name":             sb["name"],
            "sharing_pct":      pct,
            "clients":          int(grp["client_code"].nunique()),
            "total_brokerage":  brok,
            "sub_broker_share": share,
            "my_share":         brok - share,
        })
    return pd.DataFrame(rows)


def get_available_brokerage_periods() -> tuple[list, list]:
    data = load_brokerage_report()
    detail = data["detail"]
    if detail.empty:
        return [], []
    months = sorted(
        [m for m in detail["month"].dropna().unique() if m and m != "Unknown"],
        reverse=True,
    )
    years = sorted({m[:4] for m in months if len(m) >= 4}, reverse=True)
    return months, years


# ══════════════════════════════════════════════════════════════
# PDF / EXCEL GENERATION FOR SHARING REPORT
# ══════════════════════════════════════════════════════════════
def _period_label(period_type: str, period_key: str) -> str:
    if period_type == "year":
        return f"Year {period_key}"
    try:
        from datetime import datetime as _dt
        return _dt.strptime(period_key, "%Y-%m").strftime("%B %Y")
    except Exception:
        return period_key

