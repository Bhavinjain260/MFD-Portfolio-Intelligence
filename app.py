import logging
import os
import re
import time
import requests
import warnings
from datetime import datetime, timedelta, date as date_cls
from typing import Optional

import pandas as pd
import plotly.express as px
import streamlit as st

import capital_gain as cg
import data_manager
import data_manager as dm
import xirr
from init_db import init_db, get_conn
from theme_patch import THEME_WATCHER_JS, render_theme
from xirr import compute_xirr_debug

log = logging.getLogger(__name__)

warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

# ==================== CONSTANTS ====================
PAGE_SIZE = 20

_WHITESPACE_RE = re.compile(r"\s+")





# Delay between successive lookback requests so we don't hammer AMFI.
LOOKBACK_DELAY_SECONDS = 1.5

_AMFI_SESSION = requests.Session()
_AMFI_SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/plain,text/html,application/xhtml+xml,*/*",
    "Referer": "https://www.amfiindia.com/",
})


# ==================== DB INIT (cached per session) ====================
def ensure_db() -> None:
    """Run init_db() once per session. Schema lives in init.py — single source of truth."""
    if not st.session_state.get("db_initialized"):
        init_db()
        st.session_state["db_initialized"] = True


# ==================== PURE HELPERS ====================
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


def format_currency(val, decimals: int = 2) -> str:
    try:
        return f"Rs {float(val):,.{decimals}f}"
    except (TypeError, ValueError):
        return "Rs -"


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


def format_brokerage(val) -> str:
    try:
        amount = float(val)
        formatted = f"{amount:.8f}".rstrip('0').rstrip('.')
        return f"Rs {formatted}"
    except (TypeError, ValueError):
        return "Rs -"


def get_client_cams_schemes(folio_ids: list[str]) -> pd.DataFrame:
    if not folio_ids:
        return pd.DataFrame(columns=["folio_no", "prodcode", "scheme"])
    placeholders = ",".join(["?"] * len(folio_ids))
    with get_conn() as conn:
        return pd.read_sql(f"""
            SELECT DISTINCT folio_no, prodcode, scheme
            FROM cams_wbr2_transaction
            WHERE folio_no IN ({placeholders})
        """, conn, params=folio_ids)


def get_cams_txns_raw(folio_no: str, product_code: str) -> pd.DataFrame:
    """
    Same output columns as before: traddate, trxntype, trxn_nature, units,
    purprice, amount — but now genuinely chronologically ordered.

    traddate values look like "1/16/2026" or "1/16/2026 12:00" (unpadded
    M/D/YYYY, sometimes with a time component). pd.to_datetime with
    format=None (infer) handles both; errors='coerce' turns anything
    unparseable into NaT rather than crashing, and rows with NaT sort last
    (still visible, just flagged) instead of silently vanishing.
    """
    with get_conn() as conn:
        df = pd.read_sql("""
            SELECT traddate, trxntype, trxn_nature, units, purprice, amount
            FROM cams_wbr2_transaction
            WHERE folio_no = ? AND UPPER(TRIM(prodcode)) = ?
        """, conn, params=(folio_no, product_code.strip().upper()))

    if df.empty:
        return df

    df["_sort_date"] = pd.to_datetime(df["traddate"], errors="coerce")
    df = df.sort_values("_sort_date", kind="stable").drop(columns=["_sort_date"]).reset_index(drop=True)
    return df


def get_isin_for_cams_product(prodcode: str) -> Optional[str]:
    with get_conn() as conn:
        row = conn.execute("""
            SELECT ISIN FROM bse_scheme_master
            WHERE UPPER(TRIM(Channel_Partner_Code)) = ?
            LIMIT 1
        """, (prodcode.strip().upper(),)).fetchone()
        return row[0] if row else None


def get_client_kfin_schemes(folio_ids: list[str]) -> pd.DataFrame:
    if not folio_ids:
        return pd.DataFrame(columns=["folio_no", "prodcode", "scheme"])
    placeholders = ",".join(["?"] * len(folio_ids))
    with get_conn() as conn:
        df = pd.read_sql(f"""
            SELECT DISTINCT td_acno AS folio_no, UPPER(TRIM(fmcode)) AS prodcode
            FROM kfin_mfsd201_transaction
            WHERE td_acno IN ({placeholders})
        """, conn, params=folio_ids)
        bse = pd.read_sql("""
            SELECT UPPER(TRIM(Channel_Partner_Code)) AS cp_code,
                   MAX(Scheme_Name) AS scheme_name
            FROM bse_scheme_master
            WHERE Channel_Partner_Code IS NOT NULL AND TRIM(Channel_Partner_Code) != ''
            GROUP BY UPPER(TRIM(Channel_Partner_Code))
        """, conn)
    df = df.merge(bse, left_on="prodcode", right_on="cp_code", how="left")
    df["scheme"] = df["scheme_name"].fillna(df["prodcode"])
    return df[["folio_no", "prodcode", "scheme"]]


def ensure_family_tables() -> None:
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS client_family (
                family_id INTEGER PRIMARY KEY AUTOINCREMENT,
                family_name TEXT NOT NULL,
                head_client_code TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS client_family_member (
                family_id INTEGER NOT NULL,
                client_code TEXT NOT NULL,
                is_head INTEGER DEFAULT 0,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (family_id, client_code)
            )
        """)


def get_family_for_client(client_code: str) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute("""
            SELECT f.family_id, f.family_name, f.head_client_code
            FROM client_family_member m JOIN client_family f ON m.family_id = f.family_id
            WHERE m.client_code = ?
        """, (client_code,)).fetchone()
    if not row:
        return None
    return {"family_id": row[0], "family_name": row[1], "head_client_code": row[2]}


def get_family_members(family_id: int) -> pd.DataFrame:
    with get_conn() as conn:
        return pd.read_sql("""
            SELECT m.client_code, m.is_head,
                   b.primary_holder_first_name || ' ' || b.primary_holder_last_name AS name
            FROM client_family_member m
            LEFT JOIN bse_client_master b ON m.client_code = b.client_code
            WHERE m.family_id = ?
        """, conn, params=(family_id,))


def create_family(head_client_code: str, family_name: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO client_family (family_name, head_client_code) VALUES (?, ?)",
            (family_name, head_client_code)
        )
        family_id = cur.lastrowid
        conn.execute(
            "INSERT INTO client_family_member (family_id, client_code, is_head) VALUES (?, ?, 1)",
            (family_id, head_client_code)
        )
    return family_id


def add_family_member(family_id: int, client_code: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO client_family_member (family_id, client_code, is_head) VALUES (?, ?, 0)",
            (family_id, client_code)
        )


def remove_family_member(family_id: int, client_code: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM client_family_member WHERE family_id = ? AND client_code = ?",
            (family_id, client_code)
        )


def delete_family(family_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM client_family_member WHERE family_id = ?", (family_id,))
        conn.execute("DELETE FROM client_family WHERE family_id = ?", (family_id,))


def get_client_identity(client_code: str) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute("""
            SELECT primary_holder_first_name || ' ' || primary_holder_last_name AS name,
                   primary_holder_pan AS pan, guardian_pan AS guardian_pan
            FROM bse_client_master WHERE client_code = ?
        """, (client_code,)).fetchone()
    if not row:
        return None
    name, pan, guardian_pan = row
    is_minor = pan is None or str(pan).strip() == ""
    match_pan = guardian_pan if is_minor else pan
    return {"name": name, "pan": pan, "is_minor": is_minor, "match_pan": match_pan}


def compute_client_holdings(client_code: str, folio_nav_df: pd.DataFrame) -> pd.DataFrame:
    """Same enrichment logic as the Clients tab, factored out so Family Portfolio can reuse it per member."""
    identity = get_client_identity(client_code)
    if not identity:
        return pd.DataFrame()
    name, match_pan, is_minor = identity["name"], identity["match_pan"], identity["is_minor"]

    with get_conn() as conn:
        if is_minor:
            cams_f = pd.read_sql(
                "SELECT foliochk FROM cams_wbr9_folio WHERE TRIM(UPPER(inv_name))=?",
                conn, params=(name,))
            kfin_f = pd.read_sql(
                "SELECT folio FROM kfin_mfsd211_folio WHERE TRIM(UPPER(investor_name))=?",
                conn, params=(name,))
        else:
            cams_f = pd.read_sql(
                "SELECT foliochk FROM cams_wbr9_folio WHERE TRIM(UPPER(pan_no))=? OR TRIM(UPPER(inv_name))=?",
                conn, params=(match_pan, name))
            kfin_f = pd.read_sql(
                "SELECT folio FROM kfin_mfsd211_folio WHERE TRIM(UPPER(pan_number))=? OR TRIM(UPPER(investor_name))=?",
                conn, params=(match_pan, name))

    all_folios = set(cams_f['foliochk'].tolist() + kfin_f['folio'].tolist())
    if not all_folios:
        return pd.DataFrame()

    holdings = folio_nav_df[folio_nav_df['folio_id'].isin(all_folios)].copy()
    if holdings.empty:
        return holdings

    drop_leftover = [c for c in holdings.columns
                     if c.endswith('_kfin') or c.endswith('_cams')
                     or c in ('product_code_norm', 'invested_amount', 'total_units')]
    holdings = holdings.drop(columns=drop_leftover, errors='ignore')
    holdings['product_code_norm'] = holdings['product_code'].astype(str).str.strip().str.upper()

    if 'KFinTech' in holdings['rta'].values:
        kfin_invested_df = get_kfin_invested_per_scheme(kfin_f['folio'].tolist())
        if not kfin_invested_df.empty:
            kfin_invested_df['product_code_norm'] = kfin_invested_df['product_code'].astype(str).str.strip().str.upper()
            holdings = holdings.merge(kfin_invested_df, on=['folio_id', 'product_code_norm'],
                                      how='left', suffixes=('', '_kfin_fam'))
            kfin_mask = holdings['rta'] == 'KFinTech'
            has_txn = kfin_mask & holdings['invested_amount'].notna()
            holdings.loc[has_txn, 'file_aum'] = holdings.loc[has_txn, 'invested_amount']
            holdings.loc[has_txn, 'nav_based_aum'] = (
                    holdings.loc[has_txn, 'units'] * holdings.loc[has_txn, 'current_nav']
            )
            holdings = holdings.drop(columns=['invested_amount', 'product_code_norm_kfin_fam'], errors='ignore')

    if 'CAMS' in holdings['rta'].values:
        cams_invested_df = get_cams_invested_per_scheme(cams_f['foliochk'].tolist())
        if not cams_invested_df.empty:
            cams_invested_df['product_code_norm'] = cams_invested_df['product_code'].astype(str).str.strip().str.upper()
            holdings = holdings.merge(cams_invested_df, on=['folio_id', 'product_code_norm'],
                                      how='left', suffixes=('', '_cams_fam'))
            cams_mask = holdings['rta'] == 'CAMS'
            has_txn = cams_mask & holdings['invested_amount'].notna()
            holdings.loc[has_txn, 'file_aum'] = holdings.loc[has_txn, 'invested_amount']
            holdings.loc[has_txn, 'units'] = holdings.loc[has_txn, 'total_units']
            holdings.loc[has_txn, 'nav_based_aum'] = (
                    holdings.loc[has_txn, 'units'] * holdings.loc[has_txn, 'current_nav']
            )
            holdings = holdings.drop(columns=['invested_amount', 'total_units', 'product_code_norm_cams_fam'],
                                     errors='ignore')

    holdings = holdings.drop(columns=['product_code_norm'], errors='ignore')
    holdings['client_code'] = client_code
    holdings['client_name'] = name
    return holdings


def get_kfin_txns_raw(folio_no: str, product_code: str) -> pd.DataFrame:
    with get_conn() as conn:
        df = pd.read_sql("""
            SELECT td_trdt AS traddate, td_units AS units,
                   td_pop AS purprice, td_amt AS amount
            FROM kfin_mfsd201_transaction
            WHERE td_acno = ? AND UPPER(TRIM(fmcode)) = ?
        """, conn, params=(folio_no, product_code.strip().upper()))

    if df.empty:
        return df

    df["_sort_date"] = pd.to_datetime(df["traddate"], errors="coerce")
    df = df.sort_values("_sort_date", kind="stable").drop(columns=["_sort_date"]).reset_index(drop=True)
    return df


# ==================== Cams, Karvy and Manual entir Brokerage Data HELPERS ====================
def _resolve_amc_via_isin(get_conn, scheme_code_col_sql: str, table: str, scheme_code_value_alias: str):
    """
    Not used directly — kept as documentation of the join shape.
    Actual resolution happens inline in the loader below via a single
    bse_scheme_master join, exactly like get_all_folios_with_isin_and_nav().
    """
    pass


@st.cache_data(ttl=60, show_spinner=False)
def load_brokerage_report(_get_conn) -> dict:
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
    bse_amc_col = _get_bse_amc_column(_get_conn)
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
    with _get_conn() as conn:
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
        lambda i: _amfi.get_amc(i) if pd.notna(i) and str(i).strip() else None
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


def format_brokerage_inr(val) -> str:
    try:
        return f"Rs {float(val):,.2f}"
    except (TypeError, ValueError):
        return "Rs -"


@st.cache_data(ttl=60, show_spinner=False)
def load_dedup_sip_counts() -> dict:
    def _clean_regn(val):
        if pd.isna(val):
            return ""
        s = str(val).strip().upper()
        s = s.replace(".0", "") if s.endswith(".0") else s
        return re.sub(r'[^A-Z0-9]', '', s)

    active_statuses = ["ACTIVE", "LIVE SIP", "REGISTERED"]

    with get_conn() as conn:
        bse = pd.read_sql("SELECT status, xsip_regn_no AS regn FROM bse_sip", conn)
        cams = pd.read_sql("""
            SELECT
                CASE WHEN cease_date IS NULL OR cease_date = '' THEN 'Active' ELSE 'Ceased' END AS status,
                request_ref_no AS regn
            FROM cams_wbr49_sip
        """, conn)
        kfin = pd.read_sql("SELECT status, reg_slno AS regn FROM kfin_mfsd243_sip", conn)

    bse = bse[bse["status"].astype(str).str.strip().str.upper().isin(active_statuses)].copy()
    cams = cams[cams["status"].astype(str).str.strip().str.upper().isin(active_statuses)].copy()
    kfin = kfin[kfin["status"].astype(str).str.strip().str.upper().isin(active_statuses)].copy()

    bse["_key"] = bse["regn"].apply(_clean_regn)
    cams["_key"] = cams["regn"].apply(_clean_regn)
    kfin["_key"] = kfin["regn"].apply(_clean_regn)

    bse_keys = set(bse["_key"])
    rta_keys = set(cams["_key"]) | set(kfin["_key"])

    bse_unmatched = bse[~bse["_key"].isin(rta_keys)]
    cams_direct = cams[~cams["_key"].isin(bse_keys)]
    kfin_direct = kfin[~kfin["_key"].isin(bse_keys)]

    return {
        "active_sips_deduped": len(bse) + len(cams_direct) + len(kfin_direct),
        "bse_sips": len(bse),
        "bse_unmatched_in_rta": len(bse_unmatched),
        "cams_direct_sips": len(cams_direct),
        "kfin_direct_sips": len(kfin_direct),
    }


# ==================== AMFI NAV SERVICE ====================

AMFI_TEXT_URL = "https://portal.amfiindia.com/spages/NAVAll.txt"
NAV_HISTORY_URL = "https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx"

NAV_TEXT_DIR = os.environ.get("NAV_TEXT_DIR", "nav_data")


def _ensure_text_dir() -> None:
    os.makedirs(NAV_TEXT_DIR, exist_ok=True)


def _snapshot_path(date_str: str) -> str:
    return os.path.join(NAV_TEXT_DIR, f"nav_{date_str}.txt")


def _latest_snapshot_path() -> Optional[str]:
    _ensure_text_dir()
    available = sorted(f for f in os.listdir(NAV_TEXT_DIR) if f.startswith("nav_") and f.endswith(".txt"))
    return os.path.join(NAV_TEXT_DIR, available[-1]) if available else None


def get_snapshot_status() -> dict:
    _ensure_text_dir()
    today = datetime.now().strftime("%Y-%m-%d")
    today_path = _snapshot_path(today)
    latest_path = _latest_snapshot_path()
    status = {
        "has_today": os.path.exists(today_path),
        "today_path": today_path if os.path.exists(today_path) else None,
        "latest_path": latest_path,
        "latest_date": None,
        "latest_bytes": None,
    }
    if latest_path:
        fname = os.path.basename(latest_path)
        status["latest_date"] = fname.replace("nav_", "").replace(".txt", "")
        status["latest_bytes"] = os.path.getsize(latest_path)
    return status


# ==================== THE LIVE-FILE DOWNLOAD ====================

def download_and_save_nav(timeout: int = 30) -> dict:
    log.info("[AMFI] Downloading NAV file from %s", AMFI_TEXT_URL)
    res = requests.get(AMFI_TEXT_URL, timeout=timeout)
    res.raise_for_status()
    text = res.text

    _ensure_text_dir()
    today = datetime.now().strftime("%Y-%m-%d")
    path = _snapshot_path(today)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)

    size = os.path.getsize(path)
    log.info("[AMFI] Saved NAV file: %s (%s bytes, %s lines)", path, size, text.count("\n") + 1)
    return {"path": path, "bytes": size, "date": today}


from datetime import time as time_cls

# NAV re-publish cutoffs during the day. Domestic NAVs settle ~3 PM,
# foreign/international scheme NAVs land later, ~11 PM.
NAV_REDOWNLOAD_TIMES = [time_cls(15, 0), time_cls(23, 0)]


def _last_passed_cutoff_today(now: datetime) -> Optional[time_cls]:
    """Latest cutoff time that has already passed today, or None if before all of them."""
    passed = [t for t in NAV_REDOWNLOAD_TIMES if now.time() >= t]
    return max(passed) if passed else None


def download_and_save_nav_if_needed(force: bool = False) -> dict:
    """
    Re-fetches if:
      - no file exists for today, OR
      - today's file was saved BEFORE the most recent cutoff time that has
        already passed (e.g. file saved at 9 AM, now it's 4 PM → 3 PM cutoff
        already passed and file predates it → stale, refetch).
    Otherwise skip — avoids hammering AMFI on every page load.
    """
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    today_path = _snapshot_path(today)

    if not force and os.path.exists(today_path):
        cutoff = _last_passed_cutoff_today(now)
        if cutoff is None:
            size = os.path.getsize(today_path)
            log.info("[AMFI] Before first cutoff — keeping existing file for %s", today)
            return {"ran": False, "ok": True, "reason": "before first cutoff, file is current", "bytes": size}

        file_mtime = datetime.fromtimestamp(os.path.getmtime(today_path))
        cutoff_dt = datetime.combine(now.date(), cutoff)
        if file_mtime >= cutoff_dt:
            size = os.path.getsize(today_path)
            log.info("[AMFI] File already fresh for cutoff %s (saved %s)", cutoff, file_mtime.time())
            return {"ran": False, "ok": True, "reason": f"already fresh past {cutoff} cutoff", "bytes": size}

        log.info("[AMFI] File saved at %s predates %s cutoff — redownloading", file_mtime.time(), cutoff)

    try:
        result = download_and_save_nav()
        return {"ran": True, "ok": True, "reason": "downloaded", "bytes": result["bytes"]}
    except Exception as e:
        log.exception("[AMFI] Download failed")
        return {"ran": True, "ok": False, "reason": f"download failed: {e}", "bytes": None}


# ==================== PARSER (shared by live + history files) ====================

def _parse_nav_text(text: str) -> tuple[dict, dict, list[dict]]:
    """
    Parses AMFI's raw text format. Returns:
      nav_map:  {isin: (nav, nav_date)}
      amc_map:  {isin: amc_name}
      records:  list of {isin, scheme_code, isin_payout, scheme_name,
                          amc_name, category, nav, nav_date}
    """
    nav_map: dict[str, tuple[float, str]] = {}
    amc_map: dict[str, str] = {}
    records: list[dict] = []
    current_amc = ""
    current_category = ""

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if ";" not in line:
            if line.lower().endswith("mutual fund"):
                current_amc = line
            elif "(" in line and ")" in line:
                current_category = line
            continue

        parts = line.split(";")
        if len(parts) < 6 or parts[0] == "Scheme Code":
            continue

        scheme_code = parts[0].strip()
        isin_1 = parts[1].strip()
        isin_2 = parts[2].strip()
        scheme_name = parts[3].strip()
        nav_str = parts[4].strip()
        date_str = parts[5].strip()

        try:
            nav = float(nav_str) if nav_str not in ("N.A.", "") else 0.0
        except ValueError:
            nav = 0.0

        try:
            nav_date = datetime.strptime(date_str, "%d-%b-%Y").strftime("%Y-%m-%d")
        except ValueError:
            nav_date = date_str

        if nav <= 0:
            continue

        isin_clean = isin_1 if isin_1 and isin_1 != "-" else None
        isin_payout_clean = isin_2 if isin_2 and isin_2 != "-" else None
        primary_isin = isin_clean or isin_payout_clean

        if primary_isin:
            records.append({
                "isin": primary_isin.upper(),
                "scheme_code": scheme_code,
                "isin_payout": isin_payout_clean.upper() if isin_payout_clean else None,
                "scheme_name": scheme_name,
                "amc_name": current_amc or None,
                "category": current_category or None,
                "nav": nav,
                "nav_date": nav_date,
            })

        for isin in (isin_1, isin_2):
            if isin and isin != "-":
                isin_u = isin.upper()
                nav_map[isin_u] = (nav, nav_date)
                if current_amc:
                    amc_map[isin_u] = current_amc

    return nav_map, amc_map, records


# ==================== BUSINESS DAY LOGIC (needs _parse_nav_text above) ====================

def get_last_business_day(from_date: date_cls | None = None) -> date_cls:
    """Most recent business day BEFORE from_date. Weekend-only rollback (no holiday calendar)."""
    from_date = from_date or datetime.now().date()
    candidate = from_date - timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def _fmt_amfi_date(d) -> str:
    return d.strftime("%d-%b-%Y")


def _get_file_nav_date(path: str) -> Optional[str]:
    """Actual NAV date embedded in a saved file's data (not filename)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if ";" not in line:
                    continue
                parts = line.split(";")
                if len(parts) < 6 or parts[0] == "Scheme Code":
                    continue
                try:
                    return datetime.strptime(parts[5].strip(), "%d-%b-%Y").strftime("%Y-%m-%d")
                except ValueError:
                    continue
    except Exception:
        pass
    return None


def _have_snapshot_for_date(iso_date: str) -> bool:
    _ensure_text_dir()
    for fname in os.listdir(NAV_TEXT_DIR):
        if not (fname.startswith("nav_") and fname.endswith(".txt")):
            continue
        if _get_file_nav_date(os.path.join(NAV_TEXT_DIR, fname)) == iso_date:
            return True
    return False


import time
import requests

# Delay between successive lookback requests so we don't hammer AMFI.
LOOKBACK_DELAY_SECONDS = 1.5

_AMFI_SESSION = requests.Session()
_AMFI_SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/plain,text/html,application/xhtml+xml,*/*",
    "Referer": "https://www.amfiindia.com/",
})


def _looks_like_amfi_format(text: str) -> bool:
    """
    True if the response has AMFI's recognizable header/shape at all
    (even with zero data rows, e.g. a genuine holiday). False means it's
    an error/captcha/HTML page — a blocked request, not a holiday.
    """
    if not text or not text.strip():
        return False
    lowered = text.lower()
    if "<html" in lowered or "captcha" in lowered:
        return False
    return "scheme code" in lowered and ";" in text


def _normalize_history_text_to_live_format(text: str) -> str:
    """
    Rewrites DownloadNAVHistoryReport_Po.aspx's 8-column rows:
        Scheme Code;Scheme Name;ISIN Payout;ISIN Reinvest;NAV;Repurchase;Sale;Date
    into the SAME 6-column layout as the live NAVAll.txt file:
        Scheme Code;ISIN Payout;ISIN Reinvest;Scheme Name;NAV;Date
    (dropping Repurchase/Sale price, which we don't use).

    This lets every downstream reader — _parse_nav_text, _get_file_nav_date,
    the whole AMFINavIndex — stay untouched: saved historical snapshots look
    exactly like a live snapshot, just for a past date. Non-data lines
    (section headers, AMC name lines, blank lines) are passed through as-is.
    """
    out_lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if ";" not in stripped:
            out_lines.append(line)
            continue
        parts = stripped.split(";")
        if len(parts) < 8 or parts[0].strip() == "Scheme Code":
            # Header row or malformed — normalize header to the live format,
            # pass through anything else unchanged.
            if parts and parts[0].strip() == "Scheme Code":
                out_lines.append(
                    "Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;"
                    "Scheme Name;Net Asset Value;Date"
                )
            else:
                out_lines.append(line)
            continue
        scheme_code, scheme_name, isin_payout, isin_reinvest, nav, _repurchase, _sale, date_field = parts[:8]
        out_lines.append(
            f"{scheme_code};{isin_payout};{isin_reinvest};{scheme_name};{nav};{date_field}"
        )
    return "\n".join(out_lines)

def _extract_nav_date_from_text(text: str) -> Optional[str]:

        for line in text.splitlines():
            line = line.strip()
            if ";" not in line:
                continue
            parts = line.split(";")
            if len(parts) < 5 or parts[0].strip() == "Scheme Code":
                continue
            date_field = parts[-1].strip()
            try:
                return datetime.strptime(date_field, "%d-%b-%Y").strftime("%Y-%m-%d")
            except ValueError:
                continue
        return None

def download_business_day_nav(target_date, timeout: int = 30) -> dict:
    """
    Fetches AMFI history for one exact calendar date and verifies the date
    embedded in the response actually matches what we asked for.

    Returns dict with an extra "actual_date" key. Caller (the lookback loop)
    is responsible for comparing requested vs actual and stepping back if
    they differ — this function itself just fetches + reports, it does not
    guess "holiday" vs "blocked".

    Raises ValueError if the response doesn't parse as AMFI's format at all
    (e.g. blocked/error page) — that's a hard failure, not a date mismatch.
    """
    date_str = _fmt_amfi_date(target_date)
    log.info("[AMFI-HIST] Requesting NAV history for %s", date_str)

    res = _AMFI_SESSION.get(
        NAV_HISTORY_URL,
        params={"tp": 1, "frmdt": date_str, "todt": date_str},
        timeout=timeout,
    )
    log.info("[AMFI-HIST] Request URL: %s", res.url)
    res.raise_for_status()
    text = res.text

    if not _looks_like_amfi_format(text):
        # Doesn't even have AMFI's shape at all — blocked/error/captcha page,
        # NOT a holiday. Hard failure, caller should stop, not step back.
        snippet = text.strip()[:200].replace("\n", " ")
        log.error(
            "[AMFI-HIST] Response for %s doesn't look like AMFI data at all "
            "(status=%s, len=%s). Snippet: %r — likely blocked/error page.",
            date_str, res.status_code, len(text), snippet
        )
        raise ValueError(f"blocked_or_invalid_response for {date_str}: {snippet!r}")

    # Normalize to the live file's 6-column layout BEFORE saving, so every
    # downstream reader (_parse_nav_text, _get_file_nav_date, AMFINavIndex)
    # works on historical snapshots exactly like it does on live ones.
    text = _normalize_history_text_to_live_format(text)

    actual_date = _extract_nav_date_from_text(text)
    if actual_date is None:
        # Not expected in practice — AMFI has always returned the last
        # working day's data rather than a truly empty body for a holiday.
        # If this ever fires, treat it as a hard failure rather than
        # silently guessing, since we have no real date to trust.
        snippet = text.strip()[:200].replace("\n", " ")
        log.error(
            "[AMFI-HIST] %s: response has AMFI's shape but zero parseable "
            "date rows — unexpected. Snippet: %r", date_str, snippet
        )
        raise ValueError(f"no_date_in_response for {date_str}: {snippet!r}")

    log.info("[AMFI-HIST] Requested %s, file actually dated %s", date_str, actual_date)

    return {
        "text": text,
        "requested_date": target_date.strftime("%Y-%m-%d"),
        "actual_date": actual_date,
    }


def _save_nav_snapshot(text: str, iso_date: str) -> dict:
    _ensure_text_dir()
    path = _snapshot_path(iso_date)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    nav_map, _, _ = _parse_nav_text(text)
    size = os.path.getsize(path)
    log.info("[AMFI-HIST] Saved %s: %s bytes, %s ISINs", path, size, len(nav_map))
    return {"path": path, "bytes": size, "date": iso_date, "record_count": len(nav_map)}


def sync_previous_business_day_nav(force: bool = False, timeout: int = 30, max_lookback: int = 10) -> dict:
    """
    Call AFTER today's live NAV file is downloaded. Reads the actual NAV date
    of the latest snapshot, computes target = last business day before that,
    then fetches it via the history endpoint and checks the date embedded
    in the response:
      - matches target      -> save it, done.
      - earlier than target -> AMFI has confirmed (via its own data, not a
                                guess) that target was a holiday/weekend —
                                it returns the last working day's NAVs
                                instead of an empty body. We save that date
                                and stop; no further lookback needed.

    This replaces the old "empty response = holiday" heuristic, which
    misfired when AMFI blocked/rate-limited several requests in a row.
    """
    latest_path = _latest_snapshot_path()
    if latest_path is None:
        return {"ran": False, "ok": False, "reason": "no live snapshot yet"}

    latest_nav_date_str = _get_file_nav_date(latest_path)
    if not latest_nav_date_str:
        return {"ran": False, "ok": False, "reason": "could not read NAV date from latest snapshot"}

    latest_nav_date = datetime.strptime(latest_nav_date_str, "%Y-%m-%d").date()
    target = get_last_business_day(latest_nav_date)

    for i in range(max_lookback):
        iso_date = target.strftime("%Y-%m-%d")

        if not force and _have_snapshot_for_date(iso_date):
            log.info("[AMFI-HIST] Already have %s — skipping fetch", iso_date)
            return {"ran": False, "ok": True, "reason": f"already have {iso_date}", "date": iso_date}

        if i > 0:
            time.sleep(LOOKBACK_DELAY_SECONDS)

        try:
            result = download_business_day_nav(target, timeout=timeout)
        except ValueError as e:
            # Genuinely blocked/error response (not AMFI's format at all).
            # Don't misread this as a holiday — stop and surface it.
            log.error("[AMFI-HIST] Stopping lookback — %s", e)
            return {"ran": True, "ok": False, "reason": str(e), "date": iso_date}
        except requests.RequestException:
            log.exception("[AMFI-HIST] Network error for %s", iso_date)
            return {"ran": True, "ok": False, "reason": "network error", "date": iso_date}

        actual_date = result["actual_date"]

        if actual_date == iso_date:
            # AMFI genuinely has data for the exact date we asked for.
            saved = _save_nav_snapshot(result["text"], actual_date)
            return {"ran": True, "ok": True, "reason": "downloaded", **saved}

        # AMFI returned real data, but dated earlier than requested — this
        # is how AMFI signals "that date was a holiday/weekend": it just
        # gives back the last working day's NAVs instead of an empty body.
        # Trust their date and stop; no need to loop further ourselves.
        log.info(
            "[AMFI-HIST] Requested %s but AMFI returned data dated %s instead (holiday/weekend)",
            iso_date, actual_date
        )
        if not force and _have_snapshot_for_date(actual_date):
            log.info("[AMFI-HIST] Already have %s — skipping save", actual_date)
            return {"ran": False, "ok": True, "reason": f"already have {actual_date}", "date": actual_date}

        saved = _save_nav_snapshot(result["text"], actual_date)
        return {"ran": True, "ok": True, "reason": f"requested {iso_date}, saved actual {actual_date}", **saved}

    return {"ran": True, "ok": False, "reason": f"no business day found within {max_lookback} days"}


def sync_previous_business_day_nav_if_needed(force: bool = False) -> dict:
    """
    Runs at most once per calendar day — but ONLY skips if the previous
    attempt actually succeeded (i.e. we now hold a snapshot for the
    business day just before today's live NAV date). A failed attempt
    (blocked response, network error, etc.) does NOT set the "done" flag,
    so the very next rerun/page load retries automatically instead of
    silently going dark until tomorrow — which is what was happening
    before: the old gate latched on "did we try", not "did it work".
    """
    key = "prev_nav_done_for"
    today = datetime.now().strftime("%Y-%m-%d")

    if not force and st.session_state.get(key) == today:
        return {"ran": False, "ok": True, "reason": "already synced successfully today"}

    result = sync_previous_business_day_nav(force=force)

    if result.get("ok"):
        st.session_state[key] = today
    else:
        log.warning(
            "[AMFI-HIST] Previous-day sync did not succeed (%s) — will retry on next rerun",
            result.get("reason")
        )

    return result


# ==================== PREVIOUS NAV MAP (single definition) ====================

def _previous_snapshot_path(current_nav_date: Optional[str] = None) -> tuple[Optional[str], Optional[str]]:
    _ensure_text_dir()
    available = sorted(f for f in os.listdir(NAV_TEXT_DIR) if f.startswith("nav_") and f.endswith(".txt"))
    if not available:
        return None, None

    if current_nav_date is None:
        latest_path = os.path.join(NAV_TEXT_DIR, available[-1])
        current_nav_date = _get_file_nav_date(latest_path)
    if current_nav_date is None:
        return None, None

    for fname in reversed(available):
        path = os.path.join(NAV_TEXT_DIR, fname)
        file_nav_date = _get_file_nav_date(path)
        if file_nav_date and file_nav_date < current_nav_date:
            return path, file_nav_date
    return None, None


@st.cache_data(ttl=3600, show_spinner=False)
def load_previous_nav_map() -> dict:
    path, _ = _previous_snapshot_path()
    if not path:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except Exception:
        return {}
    nav_map, _, _ = _parse_nav_text(text)
    return {isin: nav for isin, (nav, _) in nav_map.items()}


def get_previous_nav_date() -> Optional[str]:
    _, date_str = _previous_snapshot_path()
    return date_str


# ==================== IN-MEMORY INDEX (loaded from file, not network) ====================

class AMFINavIndex:
    """
    In-memory ISIN-keyed index, loaded from the saved file. Loaded once per
    process (TTL-cached so repeated calls within the same run are free) and
    NEVER triggers a network call itself — only reads NAV_TEXT_DIR.
    """

    _nav_by_isin: dict[str, tuple[float, str]] = {}
    _amc_by_isin: dict[str, str] = {}
    _records: list[dict] = []
    _loaded_from: Optional[str] = None
    _last_load: Optional[datetime] = None
    _ttl_seconds: int = 3600  # re-read the file at most once/hour within a run

    def _is_fresh(self) -> bool:
        if not self._nav_by_isin or self._last_load is None:
            return False
        age = (datetime.now() - self._last_load).total_seconds()
        return age < self._ttl_seconds

    def load(self, force: bool = False) -> dict[str, tuple[float, str]]:
        """Loads (or reloads) the index from the latest saved file on disk."""
        if not force and self._is_fresh():
            log.debug("[AMFI] In-memory index fresh (loaded from %s) — reusing", self._loaded_from)
            return self._nav_by_isin

        path = _latest_snapshot_path()
        if path is None:
            log.error("[AMFI] No saved NAV file found in '%s' (abs: %s). "
                      "Run download_and_save_nav_if_needed() first, or check NAV_TEXT_DIR.",
                      NAV_TEXT_DIR, os.path.abspath(NAV_TEXT_DIR))
            return {}

        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except Exception:
            log.exception("[AMFI] Failed to read saved NAV file '%s'", path)
            return {}

        if not text.strip():
            log.error("[AMFI] Saved NAV file '%s' is empty", path)
            return {}

        nav_map, amc_map, records = _parse_nav_text(text)
        self._nav_by_isin = nav_map
        self._amc_by_isin = amc_map
        self._records = records
        self._loaded_from = path
        self._last_load = datetime.now()

        log.info("[AMFI] Loaded index from '%s': %s ISINs (NAV), %s ISINs (AMC), %s records",
                 path, len(nav_map), len(amc_map), len(records))
        return nav_map

    def get_nav(self, isin: str) -> Optional[tuple[float, str]]:
        if not self._nav_by_isin:
            self.load()
        if not isin:
            return None
        return self._nav_by_isin.get(isin.strip().upper())

    def get_amc(self, isin: str) -> str:
        if not self._amc_by_isin:
            self.load()
        if not isin:
            return ""
        return self._amc_by_isin.get(isin.strip().upper(), "")

    def get_records(self) -> list[dict]:
        if not self._records:
            self.load()
        return self._records


_amfi = AMFINavIndex()


# ==================== PUBLIC LOOKUP API ====================

def fetch_nav_by_isin(isin: str) -> Optional[tuple[float, str]]:
    """Single ISIN → (nav, nav_date) lookup, reading from the file-backed index."""
    isin = isin.strip().upper()
    result = _amfi.get_nav(isin)
    if result:
        nav, nav_date = result
        log.debug("ISIN %s | NAV: %s | Date: %s", isin, nav, nav_date)
        return nav, nav_date
    log.debug("No NAV found for ISIN: %s", isin)
    return None


def fetch_amc_by_isin(isin: str) -> str:
    """Single ISIN → canonical AMC name lookup, reading from the file-backed index."""
    return _amfi.get_amc(isin)


def load_nav_dataframe() -> pd.DataFrame:
    """
    Full NAV+AMC table as a DataFrame with just isin, nav, nav_date, amc_name —
    reads from the saved file via the in-memory index, no network call.
    """
    _amfi.load()
    records = _amfi.get_records()
    df = pd.DataFrame(records, columns=["isin", "scheme_code", "isin_payout",
                                        "scheme_name", "amc_name", "category",
                                        "nav", "nav_date"])
    return df


# ==================== FOLIO JOIN (CAMS/KFin × BSE × AMFI) ====================

def _get_bse_amc_column(get_conn) -> Optional[str]:
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


def get_all_folios_with_isin_and_nav(get_conn, force_reload: bool = False) -> pd.DataFrame:
    """
    Master batch function — reads NAV/AMC from the saved file (NOT live AMFI):
      1. Load AMFI NAV + AMC index from disk (in-memory cached per process)
      2. Query CAMS folio master JOIN deduplicated bse_scheme_master
      3. Query KFin folio master JOIN deduplicated bse_scheme_master + units
      4. Map NAV + AMC name (canonical, from saved file) in-memory by ISIN
      5. Return combined DataFrame

    force_reload=True re-reads the saved file from disk (not the network) —
    use this if you just ran download_and_save_nav() and want fresh numbers
    without restarting the app.
    """
    log.info("=" * 60)
    log.info("[NAV-FLOW] Starting get_all_folios_with_isin_and_nav()")

    # ── Step 1: Load NAV + AMC index from saved file ──
    log.info("[NAV-FLOW][Step 1] Loading AMFI NAV + AMC index from saved file (force_reload=%s)...",
             force_reload)
    nav_map = _amfi.load(force=force_reload)
    log.info("[NAV-FLOW][Step 1] Index ready: %s ISINs (NAV), %s ISINs (AMC)",
             len(nav_map), len(_amfi._amc_by_isin))
    if not nav_map:
        log.error("[NAV-FLOW][Step 1] nav_map is EMPTY — no saved NAV file found or it's unreadable. "
                  "Every row downstream will have current_nav=None, so total AUM will be 0. "
                  "Fix: call download_and_save_nav_if_needed() at startup, or use the admin "
                  "'Redownload NAV' button. Checked dir: %s", os.path.abspath(NAV_TEXT_DIR))

    # ── Step 2: Deduplicated BSE Scheme Master subquery ──
    log.info("[NAV-FLOW][Step 2] Building deduplicated BSE lookup...")
    bse_amc_col = _get_bse_amc_column(get_conn)
    bse_amc_select = f"MAX({bse_amc_col}) AS bse_amc_name" if bse_amc_col else "NULL AS bse_amc_name"
    bse_dedup = f"""
        SELECT 
            UPPER(TRIM(Channel_Partner_Code)) AS cp_code,
            MAX(ISIN) AS ISIN,
            MAX(Scheme_Name) AS Scheme_Name,
            {bse_amc_select}
        FROM bse_scheme_master
        WHERE Channel_Partner_Code IS NOT NULL AND TRIM(Channel_Partner_Code) != ''
        GROUP BY UPPER(TRIM(Channel_Partner_Code))
    """

    # ── Step 3: CAMS Query ──
    log.info("[NAV-FLOW][Step 3] Building CAMS SQL...")
    cams_sql = f"""
    SELECT 
        f.foliochk          AS folio_id,
        f.product           AS product_code,
        f.inv_name          AS investor_name,
        f.rupee_bal         AS file_aum,
        f.clos_bal          AS units,
        bsm.ISIN            AS isin,
        bsm.Scheme_Name     AS scheme_name,
        bsm.bse_amc_name    AS bse_amc_name,
        'CAMS'              AS rta
    FROM cams_wbr9_folio f
    LEFT JOIN ({bse_dedup}) bsm
        ON UPPER(TRIM(f.product)) = bsm.cp_code
    WHERE f.product IS NOT NULL AND TRIM(f.product) != ''
    """

    # ── Step 4: KFin Query ──
    log.info("[NAV-FLOW][Step 4] Building KFin SQL...")
    kfin_sql = f"""
    SELECT 
        f.folio             AS folio_id,
        f.product_code      AS product_code,
        f.investor_name     AS investor_name,
        NULL                AS file_aum,
        u.total_units       AS units,
        bsm.ISIN            AS isin,
        bsm.Scheme_Name     AS scheme_name,
        bsm.bse_amc_name    AS bse_amc_name,
        'KFinTech'          AS rta
    FROM kfin_mfsd211_folio f
    INNER JOIN (
        SELECT 
            td_acno AS folio_id,
            fmcode  AS product_code,
            SUM(td_units) AS total_units
        FROM kfin_mfsd201_transaction
        WHERE td_units IS NOT NULL
        GROUP BY td_acno, fmcode
        HAVING total_units != 0
    ) u 
        ON f.folio = u.folio_id 
        AND UPPER(TRIM(f.product_code)) = UPPER(TRIM(u.product_code))
    LEFT JOIN ({bse_dedup}) bsm
        ON UPPER(TRIM(f.product_code)) = bsm.cp_code
    WHERE f.product_code IS NOT NULL AND TRIM(f.product_code) != ''
    """

    # ── Step 5: Execute Queries ──
    log.info("[NAV-FLOW][Step 5] Executing SQL queries...")
    with get_conn() as conn:
        cams_df = pd.read_sql(cams_sql, conn)
        log.info("[NAV-FLOW][Step 5] CAMS rows fetched: %s", len(cams_df))
        kfin_df = pd.read_sql(kfin_sql, conn)
        log.info("[NAV-FLOW][Step 5] KFin rows fetched: %s", len(kfin_df))

    # ── Step 6: Combine ──
    combined = pd.concat([cams_df, kfin_df], ignore_index=True)
    log.info("[NAV-FLOW][Step 6] Combined rows: %s", len(combined))

    # ── Step 7: NAV + AMC Lookup ──
    log.info("[NAV-FLOW][Step 7] Mapping ISIN → NAV and ISIN → AMC name...")

    def _lookup_nav(isin):
        if pd.isna(isin) or not str(isin).strip():
            return pd.Series([None, None])
        hit = nav_map.get(str(isin).strip().upper())
        return pd.Series(hit) if hit else pd.Series([None, None])

    nav_cols = combined["isin"].apply(_lookup_nav)
    combined["current_nav"] = nav_cols[0]
    combined["nav_date"] = nav_cols[1]

    combined["amfi_amc_name"] = combined["isin"].apply(
        lambda i: _amfi.get_amc(i) if pd.notna(i) and str(i).strip() else None
    )

    combined["amc_name"] = combined["amfi_amc_name"].fillna(combined["bse_amc_name"])
    combined["amc_name_source"] = combined["amfi_amc_name"].apply(
        lambda x: "AMFI" if pd.notna(x) and str(x).strip() else None
    )
    combined.loc[combined["amc_name_source"].isna() & combined["bse_amc_name"].notna(),
    "amc_name_source"] = "BSE (unresolved)"
    combined["amc_name_source"] = combined["amc_name_source"].fillna("Unknown")

    total_with_isin = combined["isin"].notna().sum()
    total_with_nav = combined["current_nav"].notna().sum()
    total_with_amc = combined["amc_name"].notna().sum()
    total_amfi_amc = (combined["amc_name_source"] == "AMFI").sum()
    log.info("[NAV-FLOW][Step 7] ISIN→NAV: %s with ISIN, %s with NAV (%s%% coverage)",
             total_with_isin, total_with_nav,
             round(total_with_nav / total_with_isin * 100, 2) if total_with_isin else 0)
    log.info("[NAV-FLOW][Step 7] AMC name resolved: %s/%s rows (%s via AMFI-canonical, %s via BSE fallback)",
             total_with_amc, len(combined), total_amfi_amc, total_with_amc - total_amfi_amc)

    # ── Step 8: Calculate AUM ──
    combined["nav_based_aum"] = combined.apply(
        lambda r: r["units"] * r["current_nav"]
        if pd.notna(r.get("units")) and pd.notna(r["current_nav"])
        else None,
        axis=1
    )
    total_aum = combined["nav_based_aum"].sum()
    log.info("[NAV-FLOW][Step 8] Total NAV-based AUM: ₹%s", f"{total_aum:,.2f}" if pd.notna(total_aum) else "N/A")

    # ── Step 9: Final Flags & Reorder ──
    combined["has_isin"] = combined["isin"].notna()
    combined["has_nav"] = combined["current_nav"].notna()
    combined["has_amc"] = combined["amc_name"].notna()

    front = [
        "rta", "folio_id", "investor_name", "product_code",
        "scheme_name", "isin", "has_isin",
        "amc_name", "amc_name_source", "has_amc",
        "current_nav", "nav_date", "has_nav",
        "units", "file_aum", "nav_based_aum"
    ]
    front = [c for c in front if c in combined.columns]
    back = [c for c in combined.columns if c not in front]
    result = combined[front + back]

    log.info("[NAV-FLOW] Complete. Returning %s rows x %s cols", len(result), len(result.columns))
    log.info("=" * 60)
    return result


# def get_folio_nav_summary(get_conn, force_reload: bool = False) -> dict:
#     """Quick stats for Streamlit metrics. Reads from saved file via get_all_folios_with_isin_and_nav."""
#     log.info("[NAV-FLOW] Generating folio NAV summary...")
#
#     df = get_all_folios_with_isin_and_nav(get_conn, force_reload=force_reload)
#     cams_df = df[df["rta"] == "CAMS"]
#     kfin_df = df[df["rta"] == "KFinTech"]
#
#     cams_nav_aum = float(cams_df["nav_based_aum"].sum()) if "nav_based_aum" in cams_df.columns else 0.0
#     cams_file_aum = float(cams_df["file_aum"].sum()) if "file_aum" in cams_df.columns else 0.0
#     kfin_nav_aum = float(kfin_df["nav_based_aum"].sum()) if "nav_based_aum" in kfin_df.columns else 0.0
#
#     cams_unmatched = int((cams_df["has_isin"] & ~cams_df["has_nav"]).sum())
#     kfin_unmatched = int((kfin_df["has_isin"] & ~kfin_df["has_nav"]).sum())
#
#     return {
#         "total_folios": len(df),
#         "cams_wbr9_folios": len(cams_df),
#         "kfin_mfsd211_folios": len(kfin_df),
#         "with_isin": int(df["has_isin"].sum()),
#         "isin_coverage_pct": round(df["has_isin"].mean() * 100, 2) if len(df) else 0,
#         "with_nav": int(df["has_nav"].sum()),
#         "nav_coverage_pct": round(df["has_nav"].mean() * 100, 2) if len(df) else 0,
#         "with_amc": int(df["has_amc"].sum()),
#         "amc_coverage_pct": round(df["has_amc"].mean() * 100, 2) if len(df) else 0,
#         "amc_resolved_via_amfi": int((df["amc_name_source"] == "AMFI").sum()),
#
#         "total_aum": cams_nav_aum + kfin_nav_aum,
#         "cams_wbr4_aum": cams_nav_aum,
#         "cams_file_aum": cams_file_aum,
#         "cams_unmatched_nav": cams_unmatched,
#         "kfin_mfsd203_aum": kfin_nav_aum,
#         "kfin_unmatched_nav": kfin_unmatched,
#
#         "cams_with_nav": int(cams_df["has_nav"].sum()),
#         "cams_total": len(cams_df),
#         "kfin_with_nav": int(kfin_df["has_nav"].sum()),
#         "kfin_total": len(kfin_df),
#
#         "df": df,
#     }

def get_folio_nav_summary(get_conn, force_reload: bool = False) -> dict:
    """Quick stats for Streamlit metrics."""
    log.info("[NAV-FLOW] Generating folio NAV summary...")

    df = get_all_folios_with_isin_and_nav(get_conn, force_reload=force_reload)
    cams_df = df[df["rta"] == "CAMS"]
    kfin_df = df[df["rta"] == "KFinTech"]

    cams_nav_aum = float(cams_df["nav_based_aum"].sum()) if "nav_based_aum" in cams_df.columns else 0.0
    cams_file_aum = float(cams_df["file_aum"].sum()) if "file_aum" in cams_df.columns else 0.0
    kfin_nav_aum = float(kfin_df["nav_based_aum"].sum()) if "nav_based_aum" in kfin_df.columns else 0.0

    cams_unmatched = int((cams_df["has_isin"] & ~cams_df["has_nav"]).sum())
    kfin_unmatched = int((kfin_df["has_isin"] & ~kfin_df["has_nav"]).sum())

    return {
        "total_folios": len(df),
        "cams_folios": len(cams_df),
        "kfin_folios": len(kfin_df),
        "with_isin": int(df["has_isin"].sum()),
        "isin_coverage_pct": round(df["has_isin"].mean() * 100, 2) if len(df) else 0,
        "with_nav": int(df["has_nav"].sum()),
        "nav_coverage_pct": round(df["has_nav"].mean() * 100, 2) if len(df) else 0,
        "with_amc": int(df["has_amc"].sum()),
        "amc_coverage_pct": round(df["has_amc"].mean() * 100, 2) if len(df) else 0,
        "amc_resolved_via_amfi": int((df["amc_name_source"] == "AMFI").sum()),

        "total_aum": cams_nav_aum + kfin_nav_aum,
        "cams_aum": cams_nav_aum,
        "cams_file_aum": cams_file_aum,
        "cams_unmatched_nav": cams_unmatched,
        "kfin_aum": kfin_nav_aum,
        "kfin_unmatched_nav": kfin_unmatched,

        "cams_with_nav": int(cams_df["has_nav"].sum()),
        "cams_total": len(cams_df),
        "kfin_with_nav": int(kfin_df["has_nav"].sum()),
        "kfin_total": len(kfin_df),

        "df": df,
    }


def load_amc_breakdown_by_isin(get_conn) -> pd.DataFrame:
    """AMC-wise AUM + folio breakdown, grouped by canonical AMFI AMC name (via ISIN)."""
    df = get_all_folios_with_isin_and_nav(get_conn)
    if df.empty:
        return pd.DataFrame()

    df = df.copy()
    df["amc_name"] = df["amc_name"].fillna("⚠️ Unresolved (no ISIN match)")

    grouped = (
        df.groupby(["amc_name", "rta"], dropna=False)
        .agg(
            folios=("folio_id", "nunique"),
            records=("folio_id", "count"),
            aum=("nav_based_aum", "sum"),
        )
        .reset_index()
        .sort_values("aum", ascending=False)
    )
    return grouped


@st.cache_data(ttl=60, show_spinner=False)
def load_active_amcs() -> list:
    """AMCs you currently have business with (from folio holdings, not brokerage files)."""
    df = get_all_folios_with_isin_and_nav(get_conn)
    if df.empty:
        return []
    return sorted(df["amc_name"].dropna().unique().tolist())


def normalize_folio(folio: str) -> str:
    if not folio:
        return ""
    try:
        if pd.isna(folio):
            return ""
    except Exception:
        pass
    return str(folio).strip().split("/")[0].strip().lower()


def theme_plotly(fig, dark: bool):
    text_c = "#e6edf3" if dark else "#1a1a2e"
    grid_c = "#30363d" if dark else "#e2e8f0"
    fig.update_layout(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font_color=text_c,
        title_font_color=text_c,
        legend_font_color=text_c,
        xaxis=dict(gridcolor=grid_c, linecolor=grid_c),
        yaxis=dict(gridcolor=grid_c, linecolor=grid_c),
    )
    return fig


# Calcute the Invested Amount for Karvy Schemes
@st.cache_data(ttl=180, show_spinner=False)
def get_kfin_invested_amount(folio_list):
    if not folio_list:
        return 0.0

    with get_conn() as conn:
        placeholders = ','.join(['?'] * len(folio_list))
        query = f"""
            SELECT COALESCE(SUM(td_amt), 0) as total_invested
            FROM kfin_mfsd201_transaction 
            WHERE td_acno IN ({placeholders})
        """
        result = conn.execute(query, folio_list).fetchone()[0]
        return float(result) if result is not None else 0.0


@st.cache_data(ttl=180, show_spinner=False)
def get_kfin_invested_per_scheme(folio_list: list) -> pd.DataFrame:
    """
    Return invested amount PER SCHEME for KFin folios.
    Groups by folio + product_code so each scheme gets its own total.
    """
    if not folio_list:
        return pd.DataFrame(columns=["folio_id", "product_code", "invested_amount"])

    placeholders = ",".join(["?"] * len(folio_list))
    with get_conn() as conn:
        query = f"""
            SELECT
                td_acno   AS folio_id,
                UPPER(TRIM(fmcode)) AS product_code,
                COALESCE(SUM(td_amt), 0) AS invested_amount
            FROM kfin_mfsd201_transaction
            WHERE td_acno IN ({placeholders})
            GROUP BY td_acno, UPPER(TRIM(fmcode))
        """
        return pd.read_sql(query, conn, params=folio_list)


@st.cache_data(ttl=180, show_spinner=False)
def get_cams_invested_per_scheme(folio_list: list) -> pd.DataFrame:
    """
    Return invested amount (FIFO cost basis) AND total units PER SCHEME for CAMS folios.

    invested_amount = sum(remaining_units * purchase_rate) across all open lots.
    total_units     = sum(remaining_units) — same net units as before.

    Uses cg.replay_folio_scheme() so redemptions consume oldest lots first.
    """
    if not folio_list:
        return pd.DataFrame(columns=["folio_id", "product_code", "invested_amount", "total_units"])

    # ── Detect which column holds the scheme code ──
    scheme_col_candidates = [
        "prodcode", "product_code", "scheme_code", "schemecode",
        "scheme", "product", "fund_code", "fundcode", "plan_code", "plancode"
    ]
    scheme_col = None
    with get_conn() as conn:
        cols = [row[1] for row in conn.execute("PRAGMA table_info(cams_wbr2_transaction)").fetchall()]
    for c in scheme_col_candidates:
        if c in cols:
            scheme_col = c
            break

    placeholders = ",".join(["?"] * len(folio_list))
    with get_conn() as conn:
        if scheme_col:
            query = f"""
                SELECT
                    folio_no      AS folio_id,
                    UPPER(TRIM({scheme_col})) AS product_code,
                    traddate,
                    trxntype,
                    trxn_nature,
                    units,
                    purprice,
                    amount
                FROM cams_wbr2_transaction
                WHERE folio_no IN ({placeholders})
            """
        else:
            query = f"""
                SELECT
                    folio_no      AS folio_id,
                    NULL          AS product_code,
                    traddate,
                    trxntype,
                    trxn_nature,
                    units,
                    purprice,
                    amount
                FROM cams_wbr2_transaction
                WHERE folio_no IN ({placeholders})
            """
        df = pd.read_sql(query, conn, params=folio_list)

    if df.empty:
        return pd.DataFrame(columns=["folio_id", "product_code", "invested_amount", "total_units"])

    # ── Chronological sort (critical for FIFO) ──
    df["_sort_date"] = pd.to_datetime(df["traddate"], errors="coerce")
    df = df.sort_values(["folio_id", "product_code", "_sort_date"], kind="stable").reset_index(drop=True)
    df = df.drop(columns=["_sort_date"])

    # ── Compute FIFO cost basis per folio+scheme ──
    results = []
    for (fid, pcode), group in df.groupby(["folio_id", "product_code"]):
        try:
            lots, _ = cg.replay_folio_scheme(group)
            remaining_units = sum(l.remaining_units for l in lots)
            fifo_cost = sum(l.remaining_units * l.rate for l in lots)
            results.append({
                "folio_id": fid,
                "product_code": pcode,
                "invested_amount": fifo_cost,
                "total_units": remaining_units,
            })
        except Exception as e:
            log.warning("FIFO failed for CAMS folio %s / %s: %s", fid, pcode, e)
            # Fallback to net cash flow (same as old behaviour)
            net_amount = 0.0
            net_units = 0.0
            for _, r in group.iterrows():
                is_redemption = str(r.get("trxntype", "")).strip().upper() == "R1"
                amt = float(r["amount"]) if pd.notna(r["amount"]) else 0.0
                ut = float(r["units"]) if pd.notna(r["units"]) else 0.0
                if is_redemption:
                    net_amount -= amt
                    net_units -= ut
                else:
                    net_amount += amt
                    net_units += ut
            results.append({
                "folio_id": fid,
                "product_code": pcode,
                "invested_amount": net_amount,
                "total_units": net_units,
            })

    return pd.DataFrame(results)


# ==================== DATA LOADERS ====================
@st.cache_data(ttl=60, show_spinner=False)
def load_table_summary(table: str) -> pd.DataFrame:
    with get_conn() as conn:
        return pd.read_sql(f"SELECT * FROM {table} LIMIT 1000", conn)


@st.cache_data(ttl=60, show_spinner=False)
def load_db_stats() -> dict:
    stats = {}
    with get_conn() as conn:
        tables = [
            "bse_client_master", "bse_sip", "bse_scheme_master",
            "cams_wbr4_aum", "cams_wbr9_folio", "cams_wbr2_transaction",
            "cams_wbr49_sip", "cams_wbr77_brokerage",
            "kfin_mfsd203_aum", "kfin_mfsd211_folio", "kfin_mfsd201_transaction",
            "kfin_mfsd243_sip", "kfin_mfsd205_brokerage",
            "monthly_brokerage", "amc_code_map"
        ]
        for t in tables:
            try:
                stats[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except:
                stats[t] = 0
    return stats


# @st.cache_data(ttl=60, show_spinner=False)
# def load_dashboard_summary() -> dict:
#     """Load key metrics for dashboard."""
#     summary = {}
#     with get_conn() as conn:
#         # ── BSE ──
#         summary["total_clients"] = conn.execute("SELECT COUNT(*) FROM bse_client_master").fetchone()[0]
#         summary["total_xsip"] = conn.execute("SELECT COUNT(*) FROM bse_sip").fetchone()[0]
#         summary["active_xsip"] = conn.execute(
#             "SELECT COUNT(*) FROM bse_sip WHERE LOWER(COALESCE(status, '')) LIKE '%active%'"
#         ).fetchone()[0]
#         summary["bse_schemes"] = conn.execute("SELECT COUNT(*) FROM bse_scheme_master").fetchone()[0]
#
#         # ── CAMS ──
#         summary["cams_wbr9_folios"] = conn.execute(
#             "SELECT COUNT(DISTINCT foliochk) FROM cams_wbr9_folio"
#         ).fetchone()[0]
#         summary["cams_txns"] = conn.execute("SELECT COUNT(*) FROM cams_wbr2_transaction").fetchone()[0]
#         summary["cams_wbr49_sips"] = conn.execute("SELECT COUNT(*) FROM cams_wbr49_sip").fetchone()[0]
#         summary["cams_wbr4_aum"] = conn.execute(
#             "SELECT COALESCE(SUM(rupee_bal), 0) FROM cams_wbr4_aum"
#         ).fetchone()[0]
#         summary["cams_wbr77_brokerage"] = conn.execute(
#             "SELECT COALESCE(SUM(brkage_amt), 0) FROM cams_wbr77_brokerage"
#         ).fetchone()[0]
#         summary["cams_amcs"] = conn.execute(
#             "SELECT COUNT(DISTINCT amc_code) FROM cams_wbr9_folio WHERE COALESCE(amc_code, '') != ''"
#         ).fetchone()[0]
#
#         # ── KFinTech ──
#         summary["kfin_mfsd211_folios"] = conn.execute(
#             "SELECT COUNT(DISTINCT Folio) FROM kfin_mfsd211_folio"
#         ).fetchone()[0]
#         summary["kfin_txns"] = conn.execute("SELECT COUNT(*) FROM kfin_mfsd201_transaction").fetchone()[0]
#         summary["kfin_mfsd243_sips"] = conn.execute("SELECT COUNT(*) FROM kfin_mfsd243_sip").fetchone()[0]
#         summary["kfin_mfsd205_brokerage"] = conn.execute(
#             "SELECT COALESCE(SUM(brokerage_rs), 0) FROM kfin_mfsd205_brokerage"
#         ).fetchone()[0]
#         summary["kfin_amcs"] = conn.execute(
#             "SELECT COUNT(DISTINCT Fund) FROM kfin_mfsd211_folio WHERE COALESCE(Fund, '') != ''"
#         ).fetchone()[0]
#
#         # KFin AUM: sum td_amt grouped by td_acno from MFSD201
#         try:
#             kfin_mfsd203_aum_result = conn.execute("""
#                 SELECT COALESCE(SUM(inner_sum), 0) FROM (
#                     SELECT td_acno, SUM(td_amt) as inner_sum
#                     FROM kfin_mfsd201_transaction
#                     GROUP BY td_acno
#                 )
#             """).fetchone()[0]
#             summary["kfin_mfsd203_aum"] = float(kfin_mfsd203_aum_result) if kfin_mfsd203_aum_result else 0.0
#         except Exception as e:
#             log.warning("KFin AUM calculation failed: %s", e)
#             summary["kfin_mfsd203_aum"] = 0.0
#
#         # ── Totals ──
#         summary["total_aum"] = summary["cams_wbr4_aum"] + summary["kfin_mfsd203_aum"]
#         summary["total_brokerage"] = summary["cams_wbr77_brokerage"] + summary["kfin_mfsd205_brokerage"]
#
#     return summary

@st.cache_data(ttl=60, show_spinner=False)
def load_dashboard_summary() -> dict:
    """Load key metrics for dashboard."""
    summary = {}
    with get_conn() as conn:
        # ── BSE ──
        summary["total_clients"] = conn.execute("SELECT COUNT(*) FROM bse_client_master").fetchone()[0]
        summary["total_xsip"] = conn.execute("SELECT COUNT(*) FROM bse_sip").fetchone()[0]
        summary["active_xsip"] = conn.execute(
            "SELECT COUNT(*) FROM bse_sip WHERE LOWER(COALESCE(status, '')) LIKE '%active%'"
        ).fetchone()[0]
        summary["bse_schemes"] = conn.execute("SELECT COUNT(*) FROM bse_scheme_master").fetchone()[0]

        # ── CAMS ──
        summary["cams_folios"] = conn.execute(
            "SELECT COUNT(DISTINCT foliochk) FROM cams_wbr9_folio"
        ).fetchone()[0]
        summary["cams_txns"] = conn.execute("SELECT COUNT(*) FROM cams_wbr2_transaction").fetchone()[0]
        summary["cams_sips"] = conn.execute("SELECT COUNT(*) FROM cams_wbr49_sip").fetchone()[0]
        # Invested amount = sum of transaction amounts (same logic as KFin)
        summary["cams_aum"] = conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN trxntype = 'R1' THEN -amount ELSE amount END), 0) FROM cams_wbr2_transaction"
        ).fetchone()[0]
        summary["cams_brokerage"] = conn.execute(
            "SELECT COALESCE(SUM(brkage_amt), 0) FROM cams_wbr77_brokerage"
        ).fetchone()[0]
        summary["cams_amcs"] = conn.execute(
            "SELECT COUNT(DISTINCT amc_code) FROM cams_wbr9_folio WHERE COALESCE(amc_code, '') != ''"
        ).fetchone()[0]

        # ── KFinTech ──
        summary["kfin_folios"] = conn.execute(
            "SELECT COUNT(DISTINCT Folio) FROM kfin_mfsd211_folio"
        ).fetchone()[0]
        summary["kfin_txns"] = conn.execute("SELECT COUNT(*) FROM kfin_mfsd201_transaction").fetchone()[0]
        summary["kfin_sips"] = conn.execute("SELECT COUNT(*) FROM kfin_mfsd243_sip").fetchone()[0]
        summary["kfin_brokerage"] = conn.execute(
            "SELECT COALESCE(SUM(brokerage), 0) FROM kfin_mfsd205_brokerage"
        ).fetchone()[0]
        summary["kfin_amcs"] = conn.execute(
            "SELECT COUNT(DISTINCT Fund) FROM kfin_mfsd211_folio WHERE COALESCE(Fund, '') != ''"
        ).fetchone()[0]

        # KFin AUM: sum td_amt grouped by td_acno from MFSD201
        try:
            kfin_aum_result = conn.execute("""
                                           SELECT COALESCE(SUM(inner_sum), 0)
                                           FROM (SELECT td_acno, SUM(td_amt) as inner_sum
                                                 FROM kfin_mfsd201_transaction
                                                 GROUP BY td_acno)
                                           """).fetchone()[0]
            summary["kfin_aum"] = float(kfin_aum_result) if kfin_aum_result else 0.0
        except Exception as e:
            log.warning("KFin AUM calculation failed: %s", e)
            summary["kfin_aum"] = 0.0

        # ── Totals ──
        summary["total_aum"] = summary["cams_aum"] + summary["kfin_aum"]
        summary["total_brokerage"] = summary["cams_brokerage"] + summary["kfin_brokerage"]

    return summary


@st.cache_data(ttl=60, show_spinner=False)
def load_amc_breakdown() -> pd.DataFrame:
    """AMC-wise AUM and folio summary. KFin uses Fund column, td_acno for join."""
    with get_conn() as conn:
        # ── CAMS ──
        # ── CAMS ──
        # Folio counts from folio master
        cams_wbr9_folio_df = pd.read_sql("""
            SELECT amc_code as amc,
                   COUNT(DISTINCT foliochk) as folios,
                   COUNT(*) as records
            FROM cams_wbr9_folio 
            WHERE COALESCE(amc_code, '') != ''
            GROUP BY amc_code
        """, conn)

        # AUM from transactions (invested amount) instead of rupee_bal
        cams_txn_aum_df = pd.read_sql("""
                                      SELECT amc_code                                                                 as amc,
                                             COALESCE(SUM(CASE WHEN trxntype = 'R1' THEN -amount ELSE amount END),
                                                      0)                                                              as aum
                                      FROM cams_wbr2_transaction
                                      WHERE COALESCE(amc_code, '') != ''
                                      GROUP BY amc_code
                                      """, conn)

        if cams_wbr9_folio_df.empty and cams_txn_aum_df.empty:
            cams_combined = pd.DataFrame()
        elif cams_wbr9_folio_df.empty:
            cams_combined = cams_txn_aum_df.copy()
            cams_combined["folios"] = 0
            cams_combined["records"] = 0
        elif cams_txn_aum_df.empty:
            cams_combined = cams_wbr9_folio_df.copy()
            cams_combined["aum"] = 0.0
        else:
            cams_combined = cams_wbr9_folio_df.merge(cams_txn_aum_df, on="amc", how="outer").fillna(0)

        if not cams_combined.empty:
            cams_combined["rta"] = "CAMS"

        # ── KFinTech ──
        # AUM from transactions grouped by Fund (AMC) via td_acno join
        try:
            kfin_mfsd203_aum_df = pd.read_sql("""
                                              SELECT kf.Fund                     as amc,
                                                     COALESCE(SUM(kt.td_amt), 0) as aum
                                              FROM kfin_mfsd201_transaction kt
                                                       JOIN kfin_mfsd211_folio kf ON kt.td_acno = kf.Folio
                                              WHERE COALESCE(kf.Fund, '') != ''
                                              GROUP BY kf.Fund
                                              """, conn)
        except Exception as e:
            log.warning("KFin AUM breakdown failed: %s", e)
            kfin_mfsd203_aum_df = pd.DataFrame(columns=["amc", "aum"])

        try:
            kfin_mfsd211_folio_df = pd.read_sql("""
                                                SELECT Fund                  as amc,
                                                       COUNT(DISTINCT Folio) as folios,
                                                       COUNT(*)              as records
                                                FROM kfin_mfsd211_folio
                                                WHERE COALESCE(Fund, '') != ''
                                                GROUP BY Fund
                                                """, conn)
        except Exception as e:
            log.warning("KFin folio breakdown failed: %s", e)
            kfin_mfsd211_folio_df = pd.DataFrame(columns=["amc", "folios", "records"])

        if kfin_mfsd203_aum_df.empty and kfin_mfsd211_folio_df.empty:
            kfin_combined = pd.DataFrame()
        elif kfin_mfsd203_aum_df.empty:
            kfin_combined = kfin_mfsd211_folio_df.copy()
            kfin_combined["aum"] = 0.0
        elif kfin_mfsd211_folio_df.empty:
            kfin_combined = kfin_mfsd203_aum_df.copy()
            kfin_combined["folios"] = 0
            kfin_combined["records"] = 0
        else:
            kfin_combined = kfin_mfsd211_folio_df.merge(kfin_mfsd203_aum_df, on="amc", how="outer").fillna(0)

        if not kfin_combined.empty:
            kfin_combined["rta"] = "KFinTech"

        # ── Combine ──
        if cams_combined.empty and kfin_combined.empty:
            return pd.DataFrame()

        combined = pd.concat([cams_combined, kfin_combined], ignore_index=True)
        for col in ["amc", "folios", "records", "aum", "rta"]:
            if col not in combined.columns:
                combined[col] = 0 if col != "rta" else ""
        combined = combined[["amc", "folios", "records", "aum", "rta"]]
        combined = combined.sort_values("aum", ascending=False)
        return combined


@st.cache_data(ttl=60, show_spinner=False)
def load_recent_uploads(limit: int = 10) -> pd.DataFrame:
    """Show recent upload batches."""
    with get_conn() as conn:
        batches = []
        for table, batch_col in [
            ("bse_client_master", "upload_batch"),
            ("bse_sip", "upload_batch"),
            ("bse_scheme_master", "upload_batch"),
            ("cams_wbr4_aum", "upload_batch"),
            ("cams_wbr9_folio", "upload_batch"),
            ("cams_wbr2_transaction", "upload_batch"),
            ("cams_wbr49_sip", "upload_batch"),
            ("cams_wbr77_brokerage", "upload_batch"),
            ("kfin_mfsd203_aum", "upload_batch"),
            ("kfin_mfsd211_folio", "upload_batch"),
            ("kfin_mfsd201_transaction", "upload_batch"),
            ("kfin_mfsd243_sip", "upload_batch"),
            ("kfin_mfsd205_brokerage", "upload_batch"),
        ]:
            try:
                rows = conn.execute(f"""
                    SELECT '{table}' as source_table, 
                           {batch_col} as batch_id,
                           COUNT(*) as row_count,
                           MAX(id) as max_id
                    FROM {table}
                    WHERE {batch_col} IS NOT NULL
                    GROUP BY {batch_col}
                    ORDER BY max_id DESC
                    LIMIT {limit}
                """).fetchall()
                batches.extend(rows)
            except:
                pass

        if not batches:
            return pd.DataFrame()

        df = pd.DataFrame(batches, columns=["Table", "Batch ID", "Rows", "_max_id"])
        df = df.sort_values("_max_id", ascending=False).head(limit)
        return df.drop(columns=["_max_id"])


# ==================== APP INIT ====================
st.set_page_config(page_title="MFD Portfolio Intelligence", layout="wide", page_icon="📊")

ensure_db()
ensure_family_tables()


# ==================== BSE SCHEME MASTER AUTO-DOWNLOAD ====================
def _auto_bse_scheme_master():
    """Non-blocking: starts background download if needed, once per day."""
    from bse_auto import should_auto_download, start_background_download

    # Only run if user hasn't disabled it
    if not st.session_state.get("bse_auto_toggle", True):
        return

    # Only schedule once per day
    last_run = st.session_state.get("bse_auto_last_run")
    today = datetime.now().strftime("%Y-%m-%d")
    if last_run == today:
        return

    st.session_state["bse_auto_last_run"] = today

    if should_auto_download():
        log.info("[BSE-AUTO-STARTUP] Scheduling background download...")

        start_background_download(parse_func=dm.parse_bse_scheme_master)
        # Don't block — let the UI render. The status will show on next rerun.
    else:
        log.info("[BSE-AUTO-STARTUP] Today's file already exists.")


_auto_bse_scheme_master()


# ==================== GLOBAL BSE DOWNLOAD/PARSE NOTIFICATION ====================
# Runs on every page, not just Admin Panel, so the toast fires wherever the user is.
def _notify_bse_scheme_status():
    from bse_auto import get_download_status
    status = get_download_status()
    if status["done"] and not st.session_state.get("bse_notified", False):
        st.session_state["bse_notified"] = True
        if status["ok"]:
            st.toast(f"✅ BSE Scheme Master ready: {status['msg']}")
        else:
            st.toast(f"❌ BSE download failed: {status['msg']}")


_notify_bse_scheme_status()

# -------------------- THEME (native Streamlit System/Light/Dark) --------------------
current_theme = st.context.theme.type
dark = current_theme == "dark"

st.html(THEME_WATCHER_JS)

if "last_theme" not in st.session_state:
    st.session_state["last_theme"] = current_theme
elif st.session_state["last_theme"] != current_theme:
    st.session_state["last_theme"] = current_theme
    st.rerun()

st.markdown(render_theme(dark), unsafe_allow_html=True)

# ==================== CSS FIXES ====================
st.markdown("""
<style>
    .metric-card {
        background: linear-gradient(135deg, #1a1a2e 0%, #202036 100%);
        border-radius: 10px;
        padding: 1.1rem;
        border-left: 3px solid #2d2d44;
        margin-bottom: 0.75rem;
        box-shadow: 0 2px 8px rgba(0,0,0,0.15);
    }
    .metric-card.primary { border-left-color: #6366f1; }
    .metric-card.success { border-left-color: #10b981; }
    .metric-card.info   { border-left-color: #3b82f6; }
    .metric-card.warning{ border-left-color: #f59e0b; }
    .metric-card .label {
        color: #8b8b9a;
        font-size: 0.72rem;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.04em;
        margin-bottom: 0.4rem;
    }
    .metric-card .value {
        color: #e6edf3;
        font-size: 1.35rem;
        font-weight: 700;
        font-family: 'SF Mono', ui-monospace, monospace;
    }
    .metric-card .sub {
        color: #6b7280;
        font-size: 0.78rem;
        margin-top: 0.2rem;
    }
    [data-testid="stDataFrame"] > div[data-testid="stDataFrameContainer"] {
        background-color: transparent !important;
    }
    [data-testid="stDataFrame"] th {
        white-space: nowrap !important;
    }
</style>
""", unsafe_allow_html=True)

# ==================== SIDEBAR NAVIGATION ====================
with st.sidebar:
    st.markdown("## 📊 MFD Portfolio")
    st.divider()

    nav_options = ["📊 Dashboard", "👥 Clients", "💰 Brokerage Report", "🧮 Capital Gains", "⚙️ Admin Panel"]

    if "nav_mode" not in st.session_state:
        st.session_state["nav_mode"] = "📊 Dashboard"

    selected_nav = st.radio(
        "Navigation",
        nav_options,
        index=nav_options.index(st.session_state["nav_mode"]),
        label_visibility="collapsed"
    )

    if selected_nav != st.session_state["nav_mode"]:
        st.session_state["nav_mode"] = selected_nav
        st.rerun()

    st.divider()
    st.caption("Minimal UI v2.0")

mode = st.session_state.get("nav_mode", "📊 Dashboard")

# ==================== 📊 DASHBOARD ====================
if mode == "📊 Dashboard":
    st.header("📊 Portfolio Overview")

    # ── Auto-fetch NAV on first load ──
    nav_ready = False
    folio_nav_df = pd.DataFrame()
    nav_stats = {}

    if "folio_nav_df" not in st.session_state or "folio_nav_summary" not in st.session_state:
        with st.spinner("⏳ Fetching ISIN mappings & latest NAVs from AMFI... (5–10s)"):
            try:
                download_and_save_nav_if_needed()


                folio_nav_df = get_all_folios_with_isin_and_nav(get_conn)

                # ── Normalize product_code once for all merges ──
                folio_nav_df['product_code_norm'] = folio_nav_df['product_code'].astype(str).str.strip().str.upper()

                # ═══════════════════════════════════════════════════════════
                # CAMS: overwrite file_aum & units from transaction sums
                # ═══════════════════════════════════════════════════════════
                cams_folios_all = folio_nav_df[folio_nav_df['rta'] == 'CAMS']['folio_id'].unique().tolist()
                if cams_folios_all:
                    cams_invested_all = get_cams_invested_per_scheme(cams_folios_all)
                    if not cams_invested_all.empty:
                        cams_invested_all['product_code_norm'] = cams_invested_all['product_code'].astype(
                            str).str.strip().str.upper()
                        folio_nav_df = folio_nav_df.merge(
                            cams_invested_all,
                            left_on=['folio_id', 'product_code_norm'],
                            right_on=['folio_id', 'product_code_norm'],
                            how='left',
                            suffixes=('', '_cams')
                        )
                        cams_mask = folio_nav_df['rta'] == 'CAMS'
                        has_txn = cams_mask & folio_nav_df['invested_amount'].notna()
                        folio_nav_df.loc[has_txn, 'file_aum'] = folio_nav_df.loc[has_txn, 'invested_amount']
                        folio_nav_df.loc[has_txn, 'units'] = folio_nav_df.loc[has_txn, 'total_units']
                        folio_nav_df.loc[has_txn, 'nav_based_aum'] = (
                                folio_nav_df.loc[has_txn, 'units'] * folio_nav_df.loc[has_txn, 'current_nav']
                        )
                        folio_nav_df = folio_nav_df.drop(
                            columns=['invested_amount', 'total_units', 'product_code_norm_cams'], errors='ignore'
                        )

                # ═══════════════════════════════════════════════════════════
                # KFinTech: overwrite file_aum from transaction sums
                # (units are already correct from the master query)
                # ═══════════════════════════════════════════════════════════
                kfin_folios_all = folio_nav_df[folio_nav_df['rta'] == 'KFinTech']['folio_id'].unique().tolist()
                if kfin_folios_all:
                    kfin_invested_all = get_kfin_invested_per_scheme(kfin_folios_all)
                    if not kfin_invested_all.empty:
                        kfin_invested_all['product_code_norm'] = kfin_invested_all['product_code'].astype(
                            str).str.strip().str.upper()
                        folio_nav_df = folio_nav_df.merge(
                            kfin_invested_all,
                            left_on=['folio_id', 'product_code_norm'],
                            right_on=['folio_id', 'product_code_norm'],
                            how='left',
                            suffixes=('', '_kfin')
                        )
                        kfin_mask = folio_nav_df['rta'] == 'KFinTech'
                        has_txn = kfin_mask & folio_nav_df['invested_amount'].notna()
                        folio_nav_df.loc[has_txn, 'file_aum'] = folio_nav_df.loc[has_txn, 'invested_amount']
                        # Recalculate NAV-based AUM even though units haven't changed
                        folio_nav_df.loc[has_txn, 'nav_based_aum'] = (
                                folio_nav_df.loc[has_txn, 'units'] * folio_nav_df.loc[has_txn, 'current_nav']
                        )
                        folio_nav_df = folio_nav_df.drop(
                            columns=['invested_amount', 'product_code_norm_kfin'], errors='ignore'
                        )

                # Clean up temp column
                folio_nav_df = folio_nav_df.drop(columns=['product_code_norm'], errors='ignore')

                nav_stats = get_folio_nav_summary(get_conn)
                st.session_state["folio_nav_df"] = folio_nav_df
                st.session_state["folio_nav_summary"] = nav_stats
                nav_ready = True
                st.toast("✅ NAV data synced!")
            except Exception as e:
                st.error(f"Failed to fetch NAV: {e}")
                log.exception("Auto NAV fetch failed")
    else:
        folio_nav_df = st.session_state["folio_nav_df"]
        nav_stats = st.session_state["folio_nav_summary"]
        nav_ready = True

    # ── Skeleton loading if NAV not ready ──
    if not nav_ready:
        st.info("⏳ Loading portfolio data... Please wait.")
        skel1, skel2, skel3 = st.columns(3)
        with skel1:
            st.markdown(
                '<div style="background:#1e1e2e;border-radius:12px;padding:20px;height:100px;">'
                '<div style="background:#30363d;height:14px;width:60%;border-radius:4px;margin-bottom:12px;"></div>'
                '<div style="background:#30363d;height:28px;width:80%;border-radius:4px;"></div></div>',
                unsafe_allow_html=True)
        with skel2:
            st.markdown(
                '<div style="background:#1e1e2e;border-radius:12px;padding:20px;height:100px;">'
                '<div style="background:#30363d;height:14px;width:60%;border-radius:4px;margin-bottom:12px;"></div>'
                '<div style="background:#30363d;height:28px;width:80%;border-radius:4px;"></div></div>',
                unsafe_allow_html=True)
        with skel3:
            st.markdown(
                '<div style="background:#1e1e2e;border-radius:12px;padding:20px;height:100px;">'
                '<div style="background:#30363d;height:14px;width:60%;border-radius:4px;margin-bottom:12px;"></div>'
                '<div style="background:#30363d;height:28px;width:80%;border-radius:4px;"></div></div>',
                unsafe_allow_html=True)
        st.stop()

    # ── Merge base stats + NAV stats ──
    base_summary = load_dashboard_summary()
    summary = {**base_summary, **nav_stats}

    # ── Refresh button ──
    c_refresh, _ = st.columns([1, 5])
    with c_refresh:
        if st.button("🔄 Refresh Data", width="stretch"):
            st.cache_data.clear()
            st.session_state.pop("folio_nav_df", None)
            st.session_state.pop("folio_nav_summary", None)
            _amfi.load(force=True)
            st.rerun()

    # ── AUM Cards (muted) ──
    nav_coverage = summary.get("nav_coverage_pct", 0)
    with_nav = summary.get("with_nav", 0)
    total = summary.get("total_folios", 0)

    if nav_coverage == 0:
        st.warning("⚠️ AMFI NAV not available. Showing file-based AUM as fallback.")
    else:
        st.caption(f"📡 AMFI NAV synced: **{with_nav}/{total}** folios ({nav_coverage}%) | AUM = Units × NAV")

    aum_col1, aum_col2, aum_col3 = st.columns(3)

    with aum_col1:
        st.markdown(
            f'<div class="metric-card primary"><div class="label">📦 Total AUM (All RTAs)</div>'
            f'<div class="value">{format_aum(summary.get("total_aum", 0))}</div></div>',
            unsafe_allow_html=True)
    with aum_col2:
        unmatched = summary.get("cams_unmatched_nav", 0)
        sub = f'<div class="sub">{unmatched} unmatched NAV</div>' if unmatched else ''
        st.markdown(
            f'<div class="metric-card success"><div class="label">🟢 CAMS Current Value</div>'
            f'<div class="value">{format_aum(summary.get("cams_aum", 0))}</div>{sub}</div>',
            unsafe_allow_html=True)
    with aum_col3:
        unmatched = summary.get("kfin_unmatched_nav", 0)
        sub = f'<div class="sub">{unmatched} unmatched NAV</div>' if unmatched else ''
        st.markdown(
            f'<div class="metric-card info"><div class="label">🔵 KFinTech Current Value</div>'
            f'<div class="value">{format_aum(summary.get("kfin_aum", 0))}</div>{sub}</div>',
            unsafe_allow_html=True)

    st.divider()

    # ── Top metrics row ──
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("👥 Clients", summary.get("total_clients", 0))
    m2.metric("📋 BSE SIPs", summary.get("total_xsip", 0))
    dedup_sips = load_dedup_sip_counts()
    m3.metric("✅ Active SIPs", dedup_sips["active_sips_deduped"])
    m4.metric("🏢 CAMS AMCs", summary.get("cams_amcs", 0))
    m5.metric("🏢 KFinTech AMCs", summary.get("kfin_amcs", 0))

    # ── Second metrics row ──
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("📂 CAMS Folios", summary.get("cams_folios", 0))
    m2.metric("💱 CAMS Txns", summary.get("cams_txns", 0))
    m3.metric("🔄 CAMS SIPs", summary.get("cams_sips", 0))
    m4.metric("💰 CAMS Brokerage", format_brokerage(summary.get("cams_brokerage", 0)))
    m5.metric("💰 KFin Brokerage", format_brokerage(summary.get("kfin_brokerage", 0)))

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("📂 KFin Folios", summary.get("kfin_folios", 0))
    m2.metric("💱 KFin Txns", summary.get("kfin_txns", 0))
    m3.metric("🔄 KFin SIPs", summary.get("kfin_sips", 0))
    m4.metric("💰 Total Brokerage", format_brokerage(summary.get("total_brokerage", 0)))
    m5.metric("📊 Total Records", sum(summary.get(k, 0) for k in [
        "total_clients", "total_xsip", "cams_txns", "cams_sips",
        "kfin_txns", "kfin_sips"
    ]))

    # ── ISIN + Current NAV Section ──
    st.divider()
    st.subheader("📈 Folio-Level ISIN & Current NAV")

    if "folio_nav_df" in st.session_state:
        df = st.session_state["folio_nav_df"]

        f1, f2, f3 = st.columns([2, 2, 2])
        with f1:
            rta_filter = st.multiselect(
                "RTA", df["rta"].unique(), default=df["rta"].unique(), key="nav_rta_filter"
            )
        with f2:
            show_only = st.radio(
                "Show", ["All", "With ISIN only", "With NAV only", "Missing ISIN", "Missing NAV"],
                horizontal=True, key="nav_show_filter"
            )
        with f3:
            search_folio = st.text_input("🔍 Search Folio / Investor", "", key="nav_search")

        view = df[df["rta"].isin(rta_filter)]

        if show_only == "With ISIN only":
            view = view[view["has_isin"]]
        elif show_only == "With NAV only":
            view = view[view["has_nav"]]
        elif show_only == "Missing ISIN":
            view = view[~view["has_isin"]]
        elif show_only == "Missing NAV":
            view = view[view["has_isin"] & ~view["has_nav"]]

        if search_folio.strip():
            mask = (
                    view["folio_id"].astype(str).str.contains(search_folio, case=False, na=False) |
                    view["investor_name"].astype(str).str.contains(search_folio, case=False, na=False)
            )
            view = view[mask]

        display_cols = [
            "rta", "folio_id", "investor_name", "product_code",
            "scheme_name", "isin", "current_nav", "nav_date",
            "units", "file_aum", "nav_based_aum"
        ]
        display_cols = [c for c in display_cols if c in view.columns]

        st.dataframe(
            view[display_cols],
            width="stretch",
            hide_index=True,
            column_config={
                "current_nav": st.column_config.NumberColumn("Current NAV", format="₹ %.4f"),
                "nav_based_aum": st.column_config.NumberColumn("NAV-based AUM", format="₹ %.2f"),
                "file_aum": st.column_config.NumberColumn("File AUM", format="₹ %.2f"),
                "units": st.column_config.NumberColumn("Units", format="%.4f"),
            }
        )

        st.caption(f"Showing {len(view):,} of {len(df):,} folios")

        if not view.empty:
            csv = view.to_csv(index=False).encode("utf-8")
            st.download_button(
                "⬇️ Download NAV Report (CSV)",
                csv,
                "folio_nav_report.csv",
                "text/csv",
            )

        # ── AMC / Scheme Breakdown (drilldown) ──
        st.divider()
        st.subheader("🏢 Portfolio Breakdown")

        if nav_ready and not folio_nav_df.empty:
            bd_df = folio_nav_df.copy()
            bd_df["amc_name"] = bd_df["amc_name"].fillna("⚠️ Unresolved (no ISIN match)")
            bd_df["scheme_name"] = bd_df["scheme_name"].fillna("⚠️ Unresolved")

            breakdown_mode = st.radio(
                "Breakdown by", ["AMC-wise", "Scheme-wise"], horizontal=True, key="bd_mode"
            )

            if "bd_selected_amc" not in st.session_state:
                st.session_state["bd_selected_amc"] = None
            if "bd_selected_scheme" not in st.session_state:
                st.session_state["bd_selected_scheme"] = None

            if breakdown_mode != st.session_state.get("bd_last_mode"):
                st.session_state["bd_selected_amc"] = None
                st.session_state["bd_selected_scheme"] = None
                st.session_state["bd_last_mode"] = breakdown_mode

            # ── LEVEL 1: AMC list (AMC-wise mode only) ──
            if breakdown_mode == "AMC-wise" and st.session_state["bd_selected_amc"] is None:
                amc_df = (
                    bd_df.groupby(["amc_name", "rta"], dropna=False)
                    .agg(folios=("folio_id", "nunique"), records=("folio_id", "count"), aum=("nav_based_aum", "sum"))
                    .reset_index().sort_values("aum", ascending=False)
                )
                amc_df["aum_display"] = amc_df["aum"].apply(format_aum)
                amc_disp = amc_df[["amc_name", "rta", "folios", "records", "aum_display"]].rename(
                    columns={"amc_name": "AMC Name", "rta": "RTA", "folios": "Folios", "records": "Records",
                             "aum_display": "AUM"}
                )
                sel = st.dataframe(amc_disp, width="stretch", hide_index=True,
                                   on_select="rerun", selection_mode="single-row", key="bd_amc_table")

                fig = px.pie(amc_df, values="aum", names="amc_name", hole=0.4, title="AUM Distribution by AMC",
                             color_discrete_sequence=px.colors.qualitative.Vivid)
                fig = theme_plotly(fig, dark)
                fig.update_traces(textposition='inside', textinfo='percent+label', pull=[0.02] * len(amc_df))
                fig.update_layout(showlegend=False, margin=dict(t=40, b=20, l=20, r=20))
                st.plotly_chart(fig, width="stretch")

                if sel and len(sel["selection"]["rows"]) > 0:
                    idx = sel["selection"]["rows"][0]
                    st.session_state["bd_selected_amc"] = amc_disp.iloc[idx]["AMC Name"]
                    st.rerun()

            # ── LEVEL 2: Scheme list ──
            # ── LEVEL 2: Scheme list ──
            elif st.session_state["bd_selected_scheme"] is None and (
                    (breakdown_mode == "AMC-wise" and st.session_state["bd_selected_amc"] is not None)
                    or breakdown_mode == "Scheme-wise"
            ):

                if breakdown_mode == "AMC-wise":
                    sel_amc = st.session_state["bd_selected_amc"]
                    if st.button("⬅️ Back to AMCs"):
                        st.session_state["bd_selected_amc"] = None
                        st.rerun()
                    st.markdown(f"### {sel_amc} — Schemes")
                    scheme_src = bd_df[bd_df["amc_name"] == sel_amc]
                else:
                    scheme_src = bd_df

                scheme_df = (
                    scheme_src.groupby(["scheme_name", "amc_name", "rta"], dropna=False)
                    .agg(folios=("folio_id", "nunique"), records=("folio_id", "count"), aum=("nav_based_aum", "sum"))
                    .reset_index().sort_values("aum", ascending=False)
                )
                scheme_df["aum_display"] = scheme_df["aum"].apply(format_aum)
                cols = ["scheme_name", "rta", "folios", "records", "aum_display"] if breakdown_mode == "AMC-wise" \
                    else ["scheme_name", "amc_name", "rta", "folios", "records", "aum_display"]
                rename_map = {"scheme_name": "Scheme Name", "amc_name": "AMC Name", "rta": "RTA",
                              "folios": "Folios", "records": "Records", "aum_display": "AUM"}
                scheme_disp = scheme_df[cols].rename(columns=rename_map)

                sel2 = st.dataframe(scheme_disp, width="stretch", hide_index=True,
                                    on_select="rerun", selection_mode="single-row", key="bd_scheme_table")

                if sel2 and len(sel2["selection"]["rows"]) > 0:
                    idx = sel2["selection"]["rows"][0]
                    st.session_state["bd_selected_scheme"] = scheme_disp.iloc[idx]["Scheme Name"]
                    st.rerun()

            # ── LEVEL 3: Client list for selected scheme ──
            if st.session_state["bd_selected_scheme"] is not None:
                sel_scheme = st.session_state["bd_selected_scheme"]
                if st.button("⬅️ Back to Schemes"):
                    st.session_state["bd_selected_scheme"] = None
                    st.rerun()

                st.divider()
                st.subheader(f"👥 Client-wise Investment — {sel_scheme}")

                scheme_clients = bd_df[bd_df["scheme_name"] == sel_scheme].copy()
                client_summary = (
                    scheme_clients.groupby("investor_name", dropna=False)
                    .agg(folios=("folio_id", "nunique"), units=("units", "sum"),
                         invested=("file_aum", "sum"), current_value=("nav_based_aum", "sum"))
                    .reset_index()
                )
                client_summary["gain_loss"] = client_summary["current_value"] - client_summary["invested"]
                client_summary = client_summary.sort_values("current_value", ascending=False)

                cc1, cc2, cc3 = st.columns(3)
                cc1.metric("👤 Clients", client_summary["investor_name"].nunique())
                cc2.metric("💰 Total Invested", format_aum(client_summary["invested"].sum()))
                cc3.metric("📈 Current Value", format_aum(client_summary["current_value"].sum()))

                client_disp = client_summary.rename(
                    columns={"investor_name": "Client", "folios": "Folios", "current_value": "Current Value"})
                st.dataframe(client_disp, width="stretch", hide_index=True,
                             column_config={"Current Value": st.column_config.NumberColumn(format="₹ %.2f")})

                csv = client_disp.to_csv(index=False).encode("utf-8")
                st.download_button(f"⬇️ Download {sel_scheme} Client Breakdown (CSV)",
                                   csv, f"scheme_{sel_scheme.replace(' ', '_')[:50]}_clients.csv", "text/csv",
                                   key="bd_client_download")
        else:
            st.info("Breakdown requires NAV data. Refresh if still loading.")

    # ── Recent Uploads ──
    st.divider()
    st.subheader("📤 Recent Uploads")
    uploads_df = load_recent_uploads()
    if not uploads_df.empty:
        st.dataframe(uploads_df, width="stretch", hide_index=True)
    else:
        st.info("No uploads yet. Go to Admin Panel to upload data.")




# ==================== 👥 CLIENTS ====================
elif mode == "👥 Clients":
    st.header("👤 Client Portfolio & Analytics")

    # ── Client Search ──
    @st.cache_data(ttl=300)
    def load_clients_search():
        with get_conn() as conn:
            return pd.read_sql("""
                SELECT client_code,
                       primary_holder_first_name || ' ' || primary_holder_last_name   AS name,
                       primary_holder_pan                                             AS pan,
                       guardian_pan                                                   AS guardian_pan,
                       guardian_first_name || ' ' || COALESCE(guardian_last_name, '') AS guardian_name,
                       guardian_relationship                                          AS guardian_relationship,
                       indian_mobile_no                                               AS mobile,
                       email,
                       city
                FROM bse_client_master
                WHERE primary_holder_pan IS NOT NULL
                   OR guardian_pan IS NOT NULL
            """, conn)

    clients_df = load_clients_search()
    if clients_df.empty:
        st.warning("No clients found. Upload client master.")
        st.stop()

    clients_df['display'] = clients_df.apply(
        lambda r: f"{r['name']} | PAN: {r['pan'] or 'Minor'} | {r['client_code']}", axis=1)

    selected_display = st.selectbox(
        "🔍 Search / Select Client", clients_df['display'].tolist(), key="client_select",
        index=None, placeholder="Type to search..."
    )
    if not selected_display:
        st.stop()

    selected_client = clients_df[clients_df['display'] == selected_display].iloc[0]
    client_code = selected_client['client_code']
    pan = selected_client['pan']
    name = selected_client['name']

    is_minor = pd.isna(pan) or str(pan).strip() == ""
    match_pan = selected_client['guardian_pan'] if is_minor else pan

    # ── Compact Header ──
    st.divider()
    hc1, hc2, hc3, hc4, hc5 = st.columns([3, 1, 1, 1, 1])
    with hc1:
        st.markdown(f"### {name}")
    with hc2:
        st.markdown(f"**PAN:** `{pan if not is_minor else 'Minor'}`")
    with hc3:
        st.markdown(f"**Code:** `{client_code}`")
    with hc4:
        st.markdown(f"📱 {selected_client.get('mobile') or 'N/A'}")
    with hc5:
        st.markdown(f"📍 {selected_client.get('city') or 'N/A'}")
    if selected_client.get('email'):
        st.markdown(f"✉️ {selected_client['email']}")
    if is_minor:
        st.warning(
            f"🧒 Minor account — Guardian: {selected_client.get('guardian_name', 'N/A')} ({selected_client.get('guardian_relationship', '')}) | Guardian PAN: {selected_client.get('guardian_pan', 'N/A')}")
    st.divider()

    # ── NAV Data ──
    if "folio_nav_df" not in st.session_state:
        with st.spinner("Loading NAV..."):
            download_and_save_nav_if_needed()
            st.session_state["folio_nav_df"] = get_all_folios_with_isin_and_nav(get_conn)

    folio_nav_df = st.session_state["folio_nav_df"]

    # ── FAMILY PORTFOLIO ──
    family = get_family_for_client(client_code)

    if family is None:
        st.subheader("👨‍👩‍👧‍👦 Family Portfolio")
        with st.expander("➕ Create a Family", expanded=False):
            fam_name_input = st.text_input(
                "Family Name", value=f"{name}'s Family", key="new_family_name"
            )
            if st.button("Create Family (you as Head)", key="create_family_btn"):
                create_family(client_code, fam_name_input.strip() or f"{name}'s Family")
                st.success("Family created.")
                st.rerun()
        st.divider()

    # ── Folio lookup (used by Portfolio & AUM and Active SIPs) ──
    with get_conn() as conn:
        if is_minor:
            cams_f = pd.read_sql(
                "SELECT foliochk FROM cams_wbr9_folio WHERE TRIM(UPPER(inv_name))=?",
                conn, params=(name,))
            kfin_f = pd.read_sql(
                "SELECT folio FROM kfin_mfsd211_folio WHERE TRIM(UPPER(investor_name))=?",
                conn, params=(name,))
        else:
            cams_f = pd.read_sql(
                "SELECT foliochk FROM cams_wbr9_folio WHERE TRIM(UPPER(pan_no))=? OR TRIM(UPPER(inv_name))=?",
                conn, params=(match_pan, name))
            kfin_f = pd.read_sql(
                "SELECT folio FROM kfin_mfsd211_folio WHERE TRIM(UPPER(pan_number))=? OR TRIM(UPPER(investor_name))=?",
                conn, params=(match_pan, name))

    all_folios = set(cams_f['foliochk'].tolist() + kfin_f['folio'].tolist())

    # Stop only if no folios AND no family (preserve original UX for non-family clients)
    if not all_folios and family is None:
        with get_conn() as conn:
            pending_sip = pd.read_sql(
                "SELECT scheme_name, installments_amt, frequency_type, status, start_date "
                "FROM bse_sip WHERE client_code = ? AND UPPER(TRIM(status)) = 'ACTIVE'",
                conn, params=(client_code,)
            )
        if not pending_sip.empty:
            st.warning("⏳ No portfolio yet — SIP registered, first installment pending.")
            st.dataframe(
                pending_sip.rename(columns={
                    "scheme_name": "Scheme", "installments_amt": "Amount",
                    "frequency_type": "Frequency", "status": "Status", "start_date": "Start Date"
                }),
                use_container_width=True, hide_index=True
            )
        else:
            st.info("No folios found.")
        st.stop()

    # ── Tabs ──
    if family is None:
        tab_portfolio, tab_sips = st.tabs(["📈 Portfolio & AUM", "🔄 Active SIPs"])
    else:
        tab_family, tab_portfolio, tab_sips = st.tabs(
            ["👨‍👩‍👧‍👦 Family Portfolio", "📈 Portfolio & AUM", "🔄 Active SIPs"]
        )

    # ═══════════════════════════════════════════════════════════
    # TAB FAMILY — Family Portfolio (only when family exists)
    # ═══════════════════════════════════════════════════════════
    if family is not None:
        with tab_family:
            family_id = family["family_id"]
            members_df = get_family_members(family_id)
            member_codes = members_df["client_code"].tolist()

            fam_holdings_list = []
            member_rows = []
            for mc in member_codes:
                h = compute_client_holdings(mc, folio_nav_df)
                mname_arr = members_df.loc[members_df['client_code'] == mc, 'name'].values
                mname = mname_arr[0] if len(mname_arr) else mc
                inv = h['file_aum'].sum() if not h.empty else 0.0
                cur = h['nav_based_aum'].sum() if not h.empty else 0.0
                member_rows.append({
                    "Client": mname, "Code": mc,
                    "Invested": inv, "Current Value": cur, "Gain/Loss": cur - inv,
                    "Head": "👑" if mc == family["head_client_code"] else ""
                })
                if not h.empty:
                    fam_holdings_list.append(h)

            fam_holdings = pd.concat(fam_holdings_list, ignore_index=True) if fam_holdings_list else pd.DataFrame()
            fam_invested = fam_holdings['file_aum'].sum() if not fam_holdings.empty else 0.0
            fam_current = fam_holdings['nav_based_aum'].sum() if not fam_holdings.empty else 0.0
            fam_gain = fam_current - fam_invested

            st.caption(f"**{family['family_name']}**")
            fc1, fc2, fc3, fc4 = st.columns(4)
            fc1.metric("💰 Family Invested", format_aum(fam_invested))
            fc2.metric("📈 Family Current Value", format_aum(fam_current))
            fc3.metric("💹 Family Gain/Loss", format_aum(fam_gain))
            fc4.metric("👥 Members", len(member_codes))

            fam_left, fam_right = st.columns([1, 1])

            with fam_left:
                st.markdown("**Member Breakdown**")
                member_summary_df = pd.DataFrame(member_rows).sort_values("Current Value", ascending=False)
                st.dataframe(
                    member_summary_df,
                    width="stretch",
                    hide_index=True,
                    column_config={
                        "Invested": st.column_config.NumberColumn(format="₹ %.2f"),
                        "Current Value": st.column_config.NumberColumn(format="₹ %.2f"),
                        "Gain/Loss": st.column_config.NumberColumn(format="₹ %.2f"),
                    },
                )

            with fam_right:
                st.markdown("**Individual Member Portfolio**")
                member_options = {
                    f"{r['Client']} ({r['Code']})": r["Code"]
                    for _, r in member_summary_df.iterrows()
                }
                default_label = next(
                    (lbl for lbl, code in member_options.items() if code == client_code),
                    list(member_options.keys())[0],
                )
                selected_member_label = st.selectbox(
                    "Select member",
                    list(member_options.keys()),
                    index=list(member_options.keys()).index(default_label),
                    key="fam_member_select",
                )
                selected_member_code = member_options[selected_member_label]

                member_holdings = compute_client_holdings(selected_member_code, folio_nav_df)
                if not member_holdings.empty:
                    mem_inv = member_holdings["file_aum"].sum()
                    mem_cur = member_holdings["nav_based_aum"].sum()
                    mem_gain = mem_cur - mem_inv

                    m1, m2, m3 = st.columns(3)
                    m1.metric("Invested", format_aum(mem_inv))
                    m2.metric("Current Value", format_aum(mem_cur))
                    m3.metric("Gain/Loss", format_aum(mem_gain))

                    mem_schemes = (
                        member_holdings.groupby(["amc_name", "scheme_name"], dropna=False)
                        .agg(
                            units=("units", "sum"),
                            invested=("file_aum", "sum"),
                            current=("nav_based_aum", "sum"),
                        )
                        .reset_index()
                    )
                    mem_schemes["gain"] = mem_schemes["current"] - mem_schemes["invested"]
                    mem_schemes = mem_schemes.sort_values("current", ascending=False)

                    st.dataframe(
                        mem_schemes.rename(
                            columns={
                                "amc_name": "AMC",
                                "scheme_name": "Scheme",
                                "units": "Units",
                                "invested": "Invested",
                                "current": "Current Value",
                                "gain": "Gain/Loss",
                            }
                        ),
                        width="stretch",
                        hide_index=True,
                        column_config={
                            "Invested": st.column_config.NumberColumn(format="₹ %.2f"),
                            "Current Value": st.column_config.NumberColumn(format="₹ %.2f"),
                            "Gain/Loss": st.column_config.NumberColumn(format="₹ %.2f"),
                            "Units": st.column_config.NumberColumn(format="%.4f"),
                        },
                    )
                else:
                    st.info("No holdings found for this member.")

            with st.expander("⚙️ Manage Family Members"):
                all_clients_df = load_clients_search()
                all_clients_df["display"] = all_clients_df.apply(
                    lambda r: f"{r['name']} | PAN: {r['pan'] or 'Minor'} | {r['client_code']}",
                    axis=1,
                )
                existing_codes = set(member_codes)
                addable = all_clients_df[~all_clients_df["client_code"].isin(existing_codes)]

                if not addable.empty:
                    add_choice = st.selectbox(
                        "Add member",
                        addable["display"].tolist(),
                        index=None,
                        placeholder="Search client...",
                        key="fam_add_select",
                    )
                    if add_choice and st.button("➕ Add to Family", key="fam_add_btn"):
                        add_code = addable[addable["display"] == add_choice].iloc[0]["client_code"]
                        add_family_member(family_id, add_code)
                        st.success("Added.")
                        st.rerun()
                else:
                    st.caption("All clients already in this family.")

                removable = members_df[members_df["client_code"] != family["head_client_code"]]
                if not removable.empty:
                    rem_choice = st.selectbox(
                        "Remove member",
                        removable["client_code"].tolist(),
                        key="fam_remove_select",
                    )
                    if st.button("🗑️ Remove Member", key="fam_remove_btn"):
                        remove_family_member(family_id, rem_choice)
                        st.success("Removed.")
                        st.rerun()

                st.divider()
                if st.button("🗑️ Delete Family (removes for all members)", key="fam_delete_btn"):
                    delete_family(family_id)
                    st.success("Family deleted.")
                    st.rerun()

    # ═══════════════════════════════════════════════════════════
    # TAB 1 — Portfolio & AUM
    # ═══════════════════════════════════════════════════════════
    with tab_portfolio:
        if not all_folios:
            with get_conn() as conn:
                pending_sip = pd.read_sql(
                    "SELECT scheme_name, installments_amt, frequency_type, status, start_date "
                    "FROM bse_sip WHERE client_code = ? AND UPPER(TRIM(status)) = 'ACTIVE'",
                    conn, params=(client_code,)
                )
            if not pending_sip.empty:
                st.warning("⏳ No portfolio yet — SIP registered, first installment pending.")
                st.dataframe(
                    pending_sip.rename(columns={
                        "scheme_name": "Scheme", "installments_amt": "Amount",
                        "frequency_type": "Frequency", "status": "Status", "start_date": "Start Date"
                    }),
                    use_container_width=True, hide_index=True
                )
            else:
                st.info("No holdings found.")
        else:
            show_debug = st.toggle("🐞 Show debug logs", value=False, key="cams_debug_toggle")

            holdings = folio_nav_df[folio_nav_df['folio_id'].isin(all_folios)].copy()

            if not holdings.empty:
                # ── Clean up any leftover merge columns from Dashboard init ──
                drop_leftover = [c for c in holdings.columns
                                 if c.endswith('_kfin') or c.endswith('_cams')
                                 or c in ('product_code_norm', 'invested_amount', 'total_units')]
                holdings = holdings.drop(columns=drop_leftover, errors='ignore')

                holdings['product_code_norm'] = holdings['product_code'].astype(str).str.strip().str.upper()

                # ── KFinTech: replace file_aum with transaction-summed invested amount ──
                if 'KFinTech' in holdings['rta'].values:
                    kfin_invested_df = get_kfin_invested_per_scheme(kfin_f['folio'].tolist())
                    if not kfin_invested_df.empty:
                        kfin_invested_df['product_code_norm'] = kfin_invested_df['product_code'].astype(
                            str).str.strip().str.upper()
                        holdings = holdings.merge(
                            kfin_invested_df,
                            on=['folio_id', 'product_code_norm'],
                            how='left',
                            suffixes=('', '_kfin_client')
                        )
                        kfin_mask = holdings['rta'] == 'KFinTech'
                        has_txn = kfin_mask & holdings['invested_amount'].notna()
                        holdings.loc[has_txn, 'file_aum'] = holdings.loc[has_txn, 'invested_amount']
                        holdings.loc[has_txn, 'nav_based_aum'] = (
                                holdings.loc[has_txn, 'units'] * holdings.loc[has_txn, 'current_nav']
                        )
                        holdings = holdings.drop(
                            columns=['invested_amount', 'product_code_norm_kfin_client'], errors='ignore')

                # ── CAMS: replace file_aum AND units with transaction-summed values ──
                if 'CAMS' in holdings['rta'].values:
                    cams_invested_df = get_cams_invested_per_scheme(cams_f['foliochk'].tolist())
                    if not cams_invested_df.empty:
                        cams_invested_df['product_code_norm'] = cams_invested_df['product_code'].astype(
                            str).str.strip().str.upper()
                        holdings = holdings.merge(
                            cams_invested_df,
                            on=['folio_id', 'product_code_norm'],
                            how='left',
                            suffixes=('', '_cams_client')
                        )
                        cams_mask = holdings['rta'] == 'CAMS'
                        has_txn = cams_mask & holdings['invested_amount'].notna()
                        holdings.loc[has_txn, 'file_aum'] = holdings.loc[has_txn, 'invested_amount']
                        holdings.loc[has_txn, 'units'] = holdings.loc[has_txn, 'total_units']
                        holdings.loc[has_txn, 'nav_based_aum'] = (
                                holdings.loc[has_txn, 'units'] * holdings.loc[has_txn, 'current_nav']
                        )
                        holdings = holdings.drop(
                            columns=['invested_amount', 'total_units', 'product_code_norm_cams_client'],
                            errors='ignore')

                # Clean up temp column
                holdings = holdings.drop(columns=['product_code_norm'], errors='ignore')

                prev_nav_map = load_previous_nav_map()
                holdings["prev_nav"] = holdings["isin"].apply(
                    lambda i: prev_nav_map.get(str(i).strip().upper()) if pd.notna(i) else None
                )
                holdings["one_day_diff"] = (
                        (holdings["current_nav"] - holdings["prev_nav"]) * holdings["units"]
                ).fillna(0.0)



                total_invested = holdings['file_aum'].sum()
                total_current = holdings['nav_based_aum'].sum() or 0
                total_gain_loss = total_current - total_invested
                total_one_day_diff = holdings["one_day_diff"].sum()

                h1, h2, h3, h4, h5 = st.columns(5)
                h1.metric("Total Invested", format_aum(total_invested))
                h2.metric("Current Value", format_aum(total_current))
                h3.metric("Gain / Loss", format_aum(total_gain_loss),
                          delta=f"{(total_gain_loss / total_invested * 100):.2f}%" if total_invested > 0 else "0%")
                h4.metric("Total Folios", len(all_folios))
                h5.metric("1-Day Diff", format_aum(total_one_day_diff), delta=format_aum(total_one_day_diff))

                holdings["gain_loss"] = holdings["nav_based_aum"] - holdings["file_aum"]

                # ═══════════════════════════════════════════════════════════
                # COMPUTE XIRR PER FOLIO (like invested value calculation)
                # ═══════════════════════════════════════════════════════════
                # st.caption("⏳ Computing XIRR per folio...")

                folio_xirr_map = {}
                xirr_errors = []

                for fid in holdings["folio_id"].unique():
                    folio_rows = holdings[holdings["folio_id"] == fid]
                    if folio_rows.empty:
                        continue

                    folio_row = folio_rows.iloc[0]
                    frta = folio_row["rta"]
                    fprod = folio_row.get("product_code")
                    fvalue = folio_row["nav_based_aum"]

                    if pd.isna(fvalue) or fvalue <= 0:
                        xirr_errors.append(f"{fid}: No NAV/current value")
                        continue

                    try:
                        # Call XIRR function with verbose=True for terminal logging
                        xres = xirr.compute_xirr_for_folio(
                            folio_no=fid,
                            get_conn=get_conn,
                            rta=frta,
                            product_code=fprod,
                            current_value=float(fvalue),
                            verbose=True,  # <-- Prints to terminal for Excel verification
                        )

                        if xres["xirr"] is not None:
                            folio_xirr_map[fid] = xres["xirr_pct"]  # use pre-computed %
                            if show_debug:
                                st.write(f"✅ {fid}: XIRR = {xres['xirr_pct']}%")
                        else:
                            err_msg = xres.get("error") or "Unknown error"
                            xirr_errors.append(f"{fid}: {err_msg}")
                            if show_debug:
                                st.write(f"❌ {fid}: {err_msg}")

                    except Exception as e:
                        xirr_errors.append(f"{fid}: Exception - {e}")
                        if show_debug:
                            st.write(f"❌ {fid}: Exception - {e}")

                if show_debug and xirr_errors:
                    with st.expander("XIRR Errors"):
                        for err in xirr_errors:
                            st.caption(err)

                # ── Club rows by scheme (across folios) for display ──
                grouped_holdings = (
                    holdings.groupby(["amc_name", "scheme_name"], dropna=False)
                    .agg(
                        units=("units", "sum"),
                        file_aum=("file_aum", "sum"),
                        nav_based_aum=("nav_based_aum", "sum"),
                        one_day_diff=("one_day_diff", "sum"),
                        folios=("folio_id", "nunique"),
                        rta=("rta", lambda s: ", ".join(sorted(set(s.dropna())))),
                        folio_ids=("folio_id", lambda s: list(s.unique())),
                    )
                    .reset_index()
                )
                grouped_holdings["gain_loss"] = grouped_holdings["nav_based_aum"] - grouped_holdings["file_aum"]
                grouped_holdings["portfolio_pct"] = (
                        grouped_holdings["nav_based_aum"] / total_current * 100
                ).fillna(0) if total_current > 0 else 0

                # ── Average XIRR across folios for each scheme ──
                def _avg_xirr_for_scheme(folio_ids):
                    vals = [folio_xirr_map.get(fid) for fid in folio_ids if fid in folio_xirr_map]
                    return sum(vals) / len(vals) if vals else None

                grouped_holdings["xirr"] = grouped_holdings["folio_ids"].apply(_avg_xirr_for_scheme)

                display_holdings = grouped_holdings[[
                    'rta', 'amc_name', 'scheme_name', 'folios', 'units', 'file_aum',
                    'nav_based_aum', 'gain_loss', 'one_day_diff', 'xirr', 'portfolio_pct'
                ]].rename(columns={
                    'rta': 'RTA', 'amc_name': 'AMC', 'scheme_name': 'Scheme', 'folios': 'Folios',
                    'file_aum': 'Invested', 'nav_based_aum': 'Current Value',
                    'gain_loss': 'Gain/Loss', 'one_day_diff': '1D Diff',
                    'xirr': 'XIRR', 'portfolio_pct': '% Portfolio'
                })

                display_holdings_sorted = display_holdings.sort_values("Current Value", ascending=False).reset_index(
                    drop=True)

                selected = st.dataframe(
                    display_holdings_sorted,
                    width="stretch", hide_index=True, on_select="rerun", selection_mode="single-row",
                    column_config={
                        "Units": st.column_config.NumberColumn(format="%.4f"),
                        "Invested": st.column_config.NumberColumn(format="₹ %.2f"),
                        "Current Value": st.column_config.NumberColumn(format="₹ %.2f"),
                        "Gain/Loss": st.column_config.NumberColumn(format="₹ %.2f"),
                        "1D Diff": st.column_config.NumberColumn(format="₹ %.2f"),
                        "XIRR": st.column_config.NumberColumn(format="%.2f%%"),
                        "% Portfolio": st.column_config.NumberColumn(format="%.2f%%"),
                    }
                )

                # Transaction View
                if selected and len(selected["selection"]["rows"]) > 0:
                    idx = selected["selection"]["rows"][0]
                    row = display_holdings_sorted.iloc[idx]
                    scheme_sel = row['Scheme']
                    amc_sel = row['AMC']

                    # Underlying folios for this scheme
                    scheme_folios = holdings[
                        (holdings['scheme_name'] == scheme_sel) & (holdings['amc_name'] == amc_sel)
                        ][['folio_id', 'rta']].drop_duplicates().reset_index(drop=True)

                    st.divider()
                    st.subheader(f"📜 Transactions — {scheme_sel}")

                    if len(scheme_folios) > 1:
                        folio_options = ["All Folios"] + [
                            f"{r['folio_id']} ({r['rta']})" for _, r in scheme_folios.iterrows()
                        ]
                        folio_choice = st.radio(
                            "Filter by Folio", folio_options, horizontal=True, key="txn_folio_filter"
                        )
                    else:
                        folio_choice = "All Folios"

                    if folio_choice == "All Folios":
                        folios_to_fetch = scheme_folios
                    else:
                        sel_folio_id = folio_choice.split(" (")[0]
                        folios_to_fetch = scheme_folios[scheme_folios['folio_id'] == sel_folio_id]

                    txn_frames = []

                    folio_ids_selected = folios_to_fetch['folio_id'].tolist()
                    sel_holdings = holdings[
                        (holdings['scheme_name'] == scheme_sel) &
                        (holdings['amc_name'] == amc_sel) &
                        (holdings['folio_id'].isin(folio_ids_selected))
                        ]

                    sel_invested = sel_holdings['file_aum'].sum()
                    sel_current = sel_holdings['nav_based_aum'].sum()
                    sel_gain = sel_current - sel_invested

                    tm1, tm2, tm3 = st.columns(3)
                    tm1.metric("Invested (selected folio(s))", format_aum(sel_invested))
                    tm2.metric("Current Value (selected folio(s))", format_aum(sel_current))
                    tm3.metric("Gain/Loss (selected folio(s))", format_aum(sel_gain))

                    # ── Per-folio breakdown: show invested + current per folio
                    # so multi-folio schemes are transparent
                    if len(scheme_folios) > 1:
                        per_folio_breakdown = (
                            holdings[
                                (holdings['scheme_name'] == scheme_sel) &
                                (holdings['amc_name'] == amc_sel)
                                ]
                            .groupby('folio_id', dropna=False)
                            .agg(
                                rta=('rta', 'first'),
                                units=('units', 'sum'),
                                invested=('file_aum', 'sum'),
                                current_value=('nav_based_aum', 'sum'),
                            )
                            .reset_index()
                        )
                        per_folio_breakdown['gain_loss'] = (
                                per_folio_breakdown['current_value'] - per_folio_breakdown['invested']
                        )

                        with st.expander("📊 Per-folio breakdown for this scheme", expanded=False):
                            st.dataframe(
                                per_folio_breakdown.rename(columns={
                                    'folio_id': 'Folio', 'rta': 'RTA', 'units': 'Units',
                                    'invested': 'Invested', 'current_value': 'Current Value',
                                    'gain_loss': 'Gain/Loss'
                                }),
                                width="stretch", hide_index=True,
                                column_config={
                                    'Units': st.column_config.NumberColumn(format="%.4f"),
                                    'Invested': st.column_config.NumberColumn(format="₹ %.2f"),
                                    'Current Value': st.column_config.NumberColumn(format="₹ %.2f"),
                                    'Gain/Loss': st.column_config.NumberColumn(format="₹ %.2f"),
                                }
                            )
                            breakdown_sum_invested = per_folio_breakdown['invested'].sum()
                            breakdown_sum_current = per_folio_breakdown['current_value'].sum()
                            st.caption(
                                f"Sum of folios above → Invested: {format_aum(breakdown_sum_invested)}, "
                                f"Current: {format_aum(breakdown_sum_current)}. Compare this to the "
                                f"scheme row in the main table above — if they differ, the scheme-level "
                                f"groupby and this per-folio sum are reading different data somewhere."
                            )
                    with get_conn() as conn:
                        for _, fr in folios_to_fetch.iterrows():
                            fid, frta = fr['folio_id'], fr['rta']
                            if frta == 'CAMS':
                                df_t = pd.read_sql("""
                                                   SELECT trxnno,
                                                          traddate,
                                                          trxntype,
                                                          trxnmode,
                                                          trxnstat,
                                                          purprice,
                                                          units,
                                                          amount,
                                                          brokcode,
                                                          subbrok,
                                                          remarks
                                                   FROM cams_wbr2_transaction
                                                   WHERE folio_no = ?
                                                   ORDER BY traddate DESC
                                                   """, conn, params=(fid,))
                            else:
                                df_t = pd.read_sql("""
                                                   SELECT td_trno   as trxnno,
                                                          td_trdt   as traddate,
                                                          td_purred as trxntype,
                                                          trnmode   as trxnmode,
                                                          trnstat   as trxnstat,
                                                          td_pop    as purprice,
                                                          td_units  as units,
                                                          td_amt    as amount,
                                                          td_broker as brokcode,
                                                          ''        as subbrok,
                                                          trdesc    as remarks
                                                   FROM kfin_mfsd201_transaction
                                                   WHERE td_acno = ?
                                                   ORDER BY td_trdt DESC
                                                   """, conn, params=(fid,))
                            if not df_t.empty:
                                df_t.insert(0, 'folio_id', fid)
                                df_t.insert(1, 'rta', frta)
                            txn_frames.append(df_t)

                    txn_df = pd.concat(txn_frames, ignore_index=True) if txn_frames else pd.DataFrame()

                    if not txn_df.empty:
                        try:
                            from st_aggrid import AgGrid, GridOptionsBuilder, GridUpdateMode

                            gb = GridOptionsBuilder.from_dataframe(txn_df)
                            gb.configure_default_column(filter=True, sortable=True, resizable=True, flex=1)
                            gb.configure_pagination(paginationAutoPageSize=False, paginationPageSize=15)
                            gb.configure_grid_options(domLayout='normal')
                            grid_opts = gb.build()
                            AgGrid(
                                txn_df,
                                gridOptions=grid_opts,
                                height=350,
                                update_mode=GridUpdateMode.NO_UPDATE,
                                fit_columns_on_grid_load=True,
                                allow_unsafe_jscode=True,
                                theme="alpine-dark" if dark else "alpine",
                                key=f"txn_grid_{scheme_sel}_{folio_choice}"
                            )
                        except ImportError:
                            st.dataframe(
                                txn_df,
                                width="stretch",
                                hide_index=True,
                                column_config={
                                    "units": st.column_config.NumberColumn(format="%.4f"),
                                    "amount": st.column_config.NumberColumn(format="₹ %.2f"),
                                    "purprice": st.column_config.NumberColumn(format="₹ %.4f"),
                                }
                            )
                    else:
                        st.info("No transactions found for this folio.")
            else:
                st.info("No holdings found.")

    # ═══════════════════════════════════════════════════════════
    # TAB 2 — Active SIPs
    # ═══════════════════════════════════════════════════════════
    with tab_sips:
        st.subheader("🔄 All SIPs (Deduplicated)")

        with get_conn() as conn:
            # ── Probe BSE SIP columns ──
            bse_cols = [row[1] for row in conn.execute("PRAGMA table_info(bse_sip)").fetchall()]

            bse_select = ["amc_name", "scheme_name", "installments_amt", "status", "frequency_type",
                          "'BSE' as source", "client_code", "xsip_regn_no as sip_regn_no"]
            bse_sql = f"SELECT {', '.join(bse_select)} FROM bse_sip WHERE client_code = ?"
            bse_sip = pd.read_sql(bse_sql, conn, params=(client_code,))

            # ── CAMS SIP ──
            cams_select = ["scheme as scheme_name", "auto_amount as installments_amt",
                           "periodicity as frequency_type",
                           "CASE WHEN cease_date IS NULL OR cease_date = '' THEN 'Active' ELSE 'Ceased' END as status",
                           "folio_no", "'CAMS' as source", "request_ref_no as sip_regn_no"]
            if is_minor:
                cams_sql = f"SELECT {', '.join(cams_select)} FROM cams_wbr49_sip WHERE folio_no IN (SELECT foliochk FROM cams_wbr9_folio WHERE TRIM(UPPER(inv_name)) = ?)"
                cams_wbr49_sip = pd.read_sql(cams_sql, conn, params=(name,))
            else:
                cams_sql = f"SELECT {', '.join(cams_select)} FROM cams_wbr49_sip WHERE folio_no IN (SELECT foliochk FROM cams_wbr9_folio WHERE TRIM(UPPER(pan_no)) = ?)"
                cams_wbr49_sip = pd.read_sql(cams_sql, conn, params=(match_pan,))

            # ── KFin SIP ──
            kfin_select = ["scheme_name", "amount as installments_amt", "frequency as frequency_type",
                           "status", "folio", "'KFin' as source", "reg_slno as sip_regn_no"]

            kfin_folio_list = kfin_f['folio'].tolist()
            if kfin_folio_list:
                placeholders = ','.join(['?'] * len(kfin_folio_list))
                kfin_sql = f"SELECT {', '.join(kfin_select)} FROM kfin_mfsd243_sip WHERE folio IN ({placeholders})"
                kfin_mfsd243_sip = pd.read_sql(kfin_sql, conn, params=tuple(kfin_folio_list))
            else:
                kfin_mfsd243_sip = pd.DataFrame()

            active_statuses = ["ACTIVE", "LIVE SIP", "REGISTERED"]

            if not bse_sip.empty:
                bse_sip = bse_sip[bse_sip["status"].astype(str).str.strip().str.upper().isin(active_statuses)]

            if not cams_wbr49_sip.empty:
                cams_wbr49_sip = cams_wbr49_sip[
                    cams_wbr49_sip["status"].astype(str).str.strip().str.upper().isin(active_statuses)]

            if not kfin_mfsd243_sip.empty:
                kfin_mfsd243_sip = kfin_mfsd243_sip[
                    kfin_mfsd243_sip["status"].astype(str).str.strip().str.upper().isin(active_statuses)]


        # ── DEDUP BY SIP REGISTRATION NUMBER (exact, cross-RTA) ──
        def _clean_regn(val):
            if pd.isna(val):
                return ""
            s = str(val).strip().upper()
            s = s.replace(".0", "") if s.endswith(".0") else s
            s = re.sub(r'[^A-Z0-9]', '', s)
            return s


        def _make_match_key(df):
            return df["sip_regn_no"].apply(_clean_regn)


        # BSE SIPs are the primary source
        all_sips = []
        bse_keys = set()

        if not bse_sip.empty:
            bse_sip["_match_key"] = _make_match_key(bse_sip)
            bse_keys = set(bse_sip["_match_key"])
            all_sips.append(bse_sip)

        # Add CAMS SIPs only if they DON'T match a BSE SIP
        if not cams_wbr49_sip.empty:
            cams_wbr49_sip["_match_key"] = _make_match_key(cams_wbr49_sip)
            cams_direct = cams_wbr49_sip[~cams_wbr49_sip["_match_key"].isin(bse_keys)].copy()
            if not cams_direct.empty:
                cams_direct["source"] = "CAMS (Direct)"
                all_sips.append(cams_direct)

        # Add KFin SIPs only if they DON'T match a BSE SIP
        if not kfin_mfsd243_sip.empty:
            kfin_mfsd243_sip["_match_key"] = _make_match_key(kfin_mfsd243_sip)
            kfin_direct = kfin_mfsd243_sip[~kfin_mfsd243_sip["_match_key"].isin(bse_keys)].copy()
            if not kfin_direct.empty:
                kfin_direct["source"] = "KFin (Direct)"
                all_sips.append(kfin_direct)

        if st.toggle("🐞 Show raw regn numbers", key="sip_regn_debug"):
            st.write("BSE:", bse_sip[["scheme_name", "sip_regn_no"]] if not bse_sip.empty else "empty")
            st.write("CAMS:", cams_wbr49_sip[["scheme_name", "sip_regn_no"]] if not cams_wbr49_sip.empty else "empty")
            st.write("KFin:",
                     kfin_mfsd243_sip[["scheme_name", "sip_regn_no"]] if not kfin_mfsd243_sip.empty else "empty")

        if all_sips:
            final_sips = pd.concat(all_sips, ignore_index=True)
            final_sips = final_sips.drop(columns=["_match_key"], errors='ignore')

            active = len(final_sips[final_sips['status'].str.contains('Active|Live', na=False, case=False)])
            total_monthly = final_sips['installments_amt'].sum()

            s1, s2, s3 = st.columns(3)
            s1.metric("Total SIPs", len(final_sips))
            s2.metric("Active SIPs", active)
            s3.metric("Monthly Commitment", format_currency(total_monthly))

            # Show breakdown by source
            source_breakdown = final_sips['source'].value_counts().to_dict()
            st.caption("Sources: " + " | ".join([f"{k}: {v}" for k, v in source_breakdown.items()]))

            # ── FIX: Define display_cols OUTSIDE try block so it's available in both branches ──
            display_cols = ['source', 'scheme_name', 'installments_amt', 'frequency_type', 'status']
            display_cols = [c for c in display_cols if c in final_sips.columns]

            try:
                from st_aggrid import AgGrid, GridOptionsBuilder, GridUpdateMode

                gb = GridOptionsBuilder.from_dataframe(final_sips[display_cols])
                gb.configure_default_column(filter=True, sortable=True, resizable=True, flex=1)
                gb.configure_pagination(paginationAutoPageSize=False, paginationPageSize=10)
                grid_opts = gb.build()
                AgGrid(
                    final_sips[display_cols],
                    gridOptions=grid_opts,
                    height=300,
                    update_mode=GridUpdateMode.NO_UPDATE,
                    fit_columns_on_grid_load=True,
                    allow_unsafe_jscode=True,
                    theme="alpine-dark" if dark else "alpine",
                    key="sip_grid"
                )
            except ImportError:
                st.dataframe(
                    final_sips[display_cols].sort_values('source'),
                    width="stretch",
                    hide_index=True,
                    column_config={
                        "installments_amt": st.column_config.NumberColumn("Amount", format="₹ %.2f"),
                        "source": st.column_config.TextColumn("Source")
                    }
                )
        else:
            st.info("No SIP records found.")






# ==================== 💰 BROKERAGE REPORT ====================
elif mode == "💰 Brokerage Report":
    st.header("💰 Brokerage Report")
    st.caption(
        "File-reported brokerage (CAMS + KFin), AMC names resolved the same way as your Dashboard (AMFI-canonical via "
        "ISIN)."
    )

    data = load_brokerage_report(get_conn)
    merged = data["merged"]
    detail = data["detail"]

    if merged.empty and detail.empty:
        st.info("No brokerage data yet. Upload CAMS/KFin brokerage files and log manual entries first.")
        st.stop()

    # ── Top summary cards ──
    total_file = merged["file_amount"].sum() if not merged.empty else 0.0
    total_manual = merged["manual_amount"].sum() if not merged.empty else 0.0
    total_variance = total_file - total_manual
    pending_count = (merged["status"] == "⚠️ Not yet received").sum() if not merged.empty else 0

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("📄 Total File Brokerage", format_brokerage_inr(total_file))
    c2.metric("🏦 Total Received (Manual)", format_brokerage_inr(total_manual))
    c3.metric("📊 Variance", format_brokerage_inr(total_variance))
    c4.metric("⚠️ Pending AMC-Months", int(pending_count))

    st.divider()

    # ════════════════════════════════════════════════════════════
    # SECTION 1 — Record a manual brokerage receipt
    # ════════════════════════════════════════════════════════════
    st.subheader("✍️ Record Manual Brokerage Receipt")

    known_amcs = sorted(detail["amc"].dropna().unique()) if not detail.empty else []
    amc_dropdown_options = known_amcs + ["➕ Add new AMC..."]

    with st.form("manual_brokerage_form", clear_on_submit=True):
        mc1, mc2, mc3, mc4 = st.columns(4)
        with mc1:
            m_amc_choice = st.selectbox("AMC Name", amc_dropdown_options)
        with mc2:
            m_month = st.selectbox("Month", [f"{i:02d}" for i in range(1, 13)])
        with mc3:
            m_year = st.number_input("Year", min_value=2015, max_value=2100, value=pd.Timestamp.now().year)
        with mc4:
            m_amount = st.number_input("Amount (Rs)", min_value=0.0, step=100.0)

        m_amc_new = ""
        if m_amc_choice == "➕ Add new AMC...":
            m_amc_new = st.text_input("New AMC Name")

        m_notes = st.text_input("Notes (optional)")
        submitted = st.form_submit_button("➕ Add Entry")

        if submitted:
            m_amc_final = m_amc_new.strip() if m_amc_choice == "➕ Add new AMC..." else m_amc_choice
            if not m_amc_final:
                st.error("AMC name is required.")
            else:
                with get_conn() as conn:
                    conn.execute('''
                                 INSERT INTO monthly_brokerage (amc, month, year, amount, notes)
                                 VALUES (?, ?, ?, ?, ?) ON CONFLICT(amc, month, year) DO
                                 UPDATE SET
                                     amount = excluded.amount,
                                     notes = excluded.notes,
                                     timestamp = CURRENT_TIMESTAMP
                                 ''', (m_amc_final, m_month, int(m_year), float(m_amount), m_notes.strip() or None))
                st.cache_data.clear()
                st.success(f"Logged {format_brokerage_inr(m_amount)} for {m_amc_final} ({m_month}-{m_year}).")
                st.rerun()

    # ── Existing manual log ──
    with st.expander("📜 View / Delete Manual Entry Log", expanded=False):
        with get_conn() as conn:
            log_df = pd.read_sql(
                "SELECT amc, month, year, amount, notes, timestamp FROM monthly_brokerage ORDER BY year DESC, month DESC",
                conn
            )

        if log_df.empty:
            st.info("No manual entries logged yet.")
        else:
            st.dataframe(
                log_df, width="stretch", hide_index=True,
                column_config={"amount": st.column_config.NumberColumn(format="₹ %.2f")}
            )

            st.divider()
            st.caption("Delete an entry")
            del1, del2, del3 = st.columns([3, 2, 2])

            log_df["_label"] = (
                    log_df["amc"] + " — " + log_df["month"].astype(str) + "/" + log_df["year"].astype(str)
                    + " (" + log_df["amount"].apply(format_brokerage_inr) + ")"
            )
            with del1:
                entry_to_delete = st.selectbox(
                    "Select entry to delete", log_df["_label"].tolist(), key="brok_delete_select"
                )
            with del2:
                confirm_delete = st.checkbox("Confirm", key="brok_delete_confirm")
            with del3:
                if st.button("🗑️ Delete Entry", key="brok_delete_btn", disabled=not confirm_delete):
                    row = log_df[log_df["_label"] == entry_to_delete].iloc[0]
                    with get_conn() as conn:
                        conn.execute(
                            "DELETE FROM monthly_brokerage WHERE amc = ? AND month = ? AND year = ?",
                            (row["amc"], row["month"], int(row["year"]))
                        )
                    st.cache_data.clear()
                    st.success(f"Deleted entry: {entry_to_delete}")
                    st.rerun()

    # ════════════════════════════════════════════════════════════
    # SECTION 2 — AMC-wise bifurcation
    # ════════════════════════════════════════════════════════════
    st.subheader("🏢 AMC-wise Bifurcation")

    if not merged.empty:
        available_months = sorted(merged["month"].dropna().unique(), reverse=True)
        bif_month_filter = st.multiselect(
            "📅 Month(s)", available_months, default=available_months,
            key="brok_bif_month_filter"
        )

        merged_view = merged[merged["month"].isin(bif_month_filter)]

        amc_grouped = (
            merged_view.groupby("amc")[["file_amount", "manual_amount", "variance"]]
            .sum()
            .reset_index()
        )

        active_amcs = load_active_amcs()
        amc_summary = pd.DataFrame({"amc": active_amcs}).merge(amc_grouped, on="amc", how="left")
        amc_summary[["file_amount", "manual_amount", "variance"]] = amc_summary[
            ["file_amount", "manual_amount", "variance"]
        ].fillna(0.0)
        amc_summary = amc_summary.sort_values("file_amount", ascending=False)

        amc_summary["status"] = amc_summary.apply(
            lambda r: "⚠️ Pending" if r["manual_amount"] == 0 and r["file_amount"] > 0
            else ("✅ Matched" if abs(r["variance"]) < 1 else "🔶 Mismatch"),
            axis=1
        )

        display_amc = amc_summary.rename(columns={
            "amc": "AMC", "file_amount": "File Brokerage",
            "manual_amount": "Received (Manual)", "variance": "Variance", "status": "Status"
        })
        st.dataframe(
            display_amc, width="stretch", hide_index=True,
            column_config={
                "File Brokerage": st.column_config.NumberColumn(format="₹ %.2f"),
                "Received (Manual)": st.column_config.NumberColumn(format="₹ %.2f"),
                "Variance": st.column_config.NumberColumn(format="₹ %.2f"),
            }
        )

        if not amc_summary.empty:
            chart_df = amc_summary.copy()
            chart_df["file_amount"] = pd.to_numeric(chart_df["file_amount"], errors="coerce").fillna(0.0)
            chart_df["manual_amount"] = pd.to_numeric(chart_df["manual_amount"], errors="coerce").fillna(0.0)

            chart_long = chart_df.melt(
                id_vars="amc",
                value_vars=["file_amount", "manual_amount"],
                var_name="type",
                value_name="amount"
            )
            chart_long["type"] = chart_long["type"].map({
                "file_amount": "File Brokerage",
                "manual_amount": "Received (Manual)"
            })

            fig = px.bar(
                chart_long, x="amc", y="amount", color="type",
                barmode="group", title="File vs Received, by AMC",
                labels={"amount": "Amount (₹)", "amc": "AMC", "type": "Type"},
                color_discrete_sequence=px.colors.qualitative.Bold
            )
            fig = theme_plotly(fig, dark)
            fig.update_layout(
                legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
                xaxis_tickangle=-45
            )
            st.plotly_chart(fig, width="stretch")
        else:
            st.info("No data for the selected month(s).")
    else:
        st.info("No file brokerage data parsed yet.")

    st.divider()

    # ════════════════════════════════════════════════════════════
    # SECTION 3 — AMC Drilldown: client-level detail
    # ════════════════════════════════════════════════════════════
    st.subheader("🔍 AMC Drilldown — Client-level Detail")

    # ── NORMALIZE COLUMN NAMES ──
    detail_norm = detail.copy()
    col_map = {}
    if "BRKAGE_AMT" in detail_norm.columns:
        col_map["BRKAGE_AMT"] = "brokerage_amount"
    if "BROKERAGE" in detail_norm.columns:
        col_map["BROKERAGE"] = "brokerage_amount"
    if "BRKAGE_RATE" in detail_norm.columns:
        col_map["BRKAGE_RATE"] = "brokerage_pct"
    if "PERCENTAGE" in detail_norm.columns:
        col_map["PERCENTAGE"] = "brokerage_pct"
    if "BRKAGE_TYPE" in detail_norm.columns:
        col_map["BRKAGE_TYPE"] = "brokerage_type"
    if "BROKERAGE_TYPE" in detail_norm.columns:
        col_map["BROKERAGE_TYPE"] = "brokerage_type"
    if "INV_NAME" in detail_norm.columns:
        col_map["INV_NAME"] = "client"
    if "INVESTOR_NAME" in detail_norm.columns:
        col_map["INVESTOR_NAME"] = "client"
    if "FOLIO_NO" in detail_norm.columns:
        col_map["FOLIO_NO"] = "folio"
    if "ACCOUNT_NUMBER" in detail_norm.columns:
        col_map["ACCOUNT_NUMBER"] = "folio"
    if "PLOT_AMOUNT" in detail_norm.columns:
        col_map["PLOT_AMOUNT"] = "txn_amount"
    if "AMOUNT" in detail_norm.columns:
        col_map["AMOUNT"] = "txn_amount"
    if "TRADE_DATE_TIME" in detail_norm.columns:
        col_map["TRADE_DATE_TIME"] = "txn_date"
    if "TRANSACTION_DATE" in detail_norm.columns:
        col_map["TRANSACTION_DATE"] = "txn_date"
    if "SCHEME_CODE" in detail_norm.columns:
        col_map["SCHEME_CODE"] = "scheme_code"
    if col_map:
        detail_norm = detail_norm.rename(columns=col_map)

    # ── DRILLDOWN ──
    all_amcs = sorted(detail_norm["amc"].dropna().unique()) if not detail_norm.empty else []
    if not all_amcs:
        st.info("No detail rows available.")
    else:
        amc_options = ["All"] + all_amcs
        selected_amc = st.selectbox("Select AMC", amc_options, key="brok_drilldown_amc")

        if selected_amc == "All":
            amc_detail = detail_norm.copy()
        else:
            amc_detail = detail_norm[detail_norm["amc"] == selected_amc].copy()

        dc1, dc2 = st.columns([2, 2])
        with dc1:
            available_drilldown_months = sorted(amc_detail["month"].dropna().unique(), reverse=True)
            month_f = st.multiselect(
                "📅 Month(s)", available_drilldown_months, default=available_drilldown_months,
                key="brok_drilldown_month"
            )
        with dc2:
            client_search = st.text_input("🔍 Search Client / Folio", "", key="brok_drilldown_search")

        amc_detail = amc_detail[amc_detail["month"].isin(month_f)]
        if client_search.strip():
            mask = (
                    amc_detail["client"].astype(str).str.contains(client_search, case=False, na=False)
                    | amc_detail["folio"].astype(str).str.contains(client_search, case=False, na=False)
            )
            amc_detail = amc_detail[mask]

        amc_total = amc_detail["brokerage_amount"].sum()
        label = "All AMCs" if selected_amc == "All" else selected_amc
        st.metric(f"💰 Total Brokerage — {label}", format_brokerage_inr(amc_total))

        detail_cols = [
            "rta", "client", "folio", "scheme_code", "txn_date",
            "txn_amount", "brokerage_pct", "brokerage_amount", "brokerage_type"
        ]
        detail_cols = [c for c in detail_cols if c in amc_detail.columns]
        display_detail = amc_detail[detail_cols].rename(columns={
            "rta": "RTA", "client": "Client", "folio": "Folio", "scheme_code": "Scheme Code",
            "txn_date": "Date", "txn_amount": "Txn Amount", "brokerage_pct": "Brokerage %",
            "brokerage_amount": "Brokerage Amount", "brokerage_type": "Type",
        })

        # ── AgGrid for Brokerage Drilldown ──
        try:
            from st_aggrid import AgGrid, GridOptionsBuilder, GridUpdateMode

            gb = GridOptionsBuilder.from_dataframe(display_detail)
            gb.configure_default_column(filter=True, sortable=True, resizable=True, flex=1, minWidth=120)
            gb.configure_pagination(paginationAutoPageSize=False, paginationPageSize=25)
            gb.configure_grid_options(domLayout='normal')
            grid_opts = gb.build()
            AgGrid(
                display_detail,
                gridOptions=grid_opts,
                height=500,
                update_mode=GridUpdateMode.NO_UPDATE,
                fit_columns_on_grid_load=True,
                allow_unsafe_jscode=True,
                theme="alpine-dark" if dark else "alpine",
                key="brokerage_drilldown_grid"
            )
        except ImportError:
            st.dataframe(
                display_detail.sort_values("Date",
                                           ascending=False) if "Date" in display_detail.columns else display_detail,
                width="stretch", hide_index=True,
                column_config={
                    "Txn Amount": st.column_config.NumberColumn(format="₹ %.2f"),
                    "Brokerage %": st.column_config.NumberColumn(format="%.4f"),
                    "Brokerage Amount": st.column_config.NumberColumn(format="₹ %.2f"),
                }
            )
        st.caption(f"Showing {len(amc_detail):,} brokerage records for {label}")

        if not amc_detail.empty:
            csv = display_detail.to_csv(index=False).encode("utf-8")
            st.download_button(
                f"⬇️ Download {label} Brokerage Detail (CSV)",
                csv, f"brokerage_{label.replace(' ', '_')}.csv", "text/csv",
                key="brok_drilldown_download"
            )

    st.divider()



# ==================== 🧮 CAPITAL GAINS ====================
elif mode == "🧮 Capital Gains":
    st.header("🧮 Capital Gains (CAMS, FIFO)")
    st.caption(
        "FIFO cost basis. Redemption detected via trxntype='R1' or trxn_nature containing "
        "'Redemption'. Tax category is a heuristic guess from the scheme name — confirm it "
        "before relying on the tax figure."
    )


    @st.cache_data(ttl=300)
    def _cg_clients():
        with get_conn() as conn:
            return pd.read_sql("""
                SELECT client_code,
                       primary_holder_first_name || ' ' || primary_holder_last_name AS name,
                       primary_holder_pan AS pan
                FROM bse_client_master
                WHERE primary_holder_pan IS NOT NULL
            """, conn)


    cg_clients = _cg_clients()
    if cg_clients.empty:
        st.warning("No clients found.")
        st.stop()

    cg_clients["display"] = cg_clients["name"] + " | " + cg_clients["pan"].fillna("Minor")
    cg_sel = st.selectbox("Client", cg_clients["display"].tolist(), index=None, placeholder="Search...")
    if not cg_sel:
        st.stop()

    cg_row = cg_clients[cg_clients["display"] == cg_sel].iloc[0]
    cg_pan, cg_name = cg_row["pan"], cg_row["name"]

    with get_conn() as conn:
        cg_cams_folios = pd.read_sql(
            "SELECT foliochk FROM cams_wbr9_folio WHERE TRIM(UPPER(pan_no))=? OR TRIM(UPPER(inv_name))=?",
            conn, params=(str(cg_pan).upper(), cg_name.upper())
        )
        cg_kfin_folios = pd.read_sql(
            "SELECT folio FROM kfin_mfsd211_folio WHERE TRIM(UPPER(pan_number))=? OR TRIM(UPPER(investor_name))=?",
            conn, params=(str(cg_pan).upper(), cg_name.upper())
        )

    cams_folio_ids = cg_cams_folios["foliochk"].tolist()
    kfin_folio_ids = cg_kfin_folios["folio"].tolist()

    if not cams_folio_ids and not kfin_folio_ids:
        st.info("No folios for this client.")
        st.stop()

    cams_schemes = get_client_cams_schemes(cams_folio_ids)
    cams_schemes["rta"] = "CAMS"

    kfin_schemes = get_client_kfin_schemes(kfin_folio_ids)
    kfin_schemes["rta"] = "KFinTech"

    schemes_df = pd.concat([cams_schemes, kfin_schemes], ignore_index=True)
    if schemes_df.empty:
        st.info("No transactions for this client.")
        st.stop()

    schemes_df["label"] = schemes_df["folio_no"] + " — " + schemes_df["scheme"] + " (" + schemes_df["rta"] + ")"
    sel_scheme = st.selectbox("Folio / Scheme", schemes_df["label"].tolist())
    srow = schemes_df[schemes_df["label"] == sel_scheme].iloc[0]
    folio_no, prodcode, scheme_name, rta_sel = srow["folio_no"], srow["prodcode"], srow["scheme"], srow["rta"]
    is_kfin = rta_sel == "KFinTech"

    if is_kfin:
        st.info("⚠️ KFinTech: no redemption history yet, so only What-if Redemption is shown. "
                "Realized Gains unlocks once redemption transactions exist and the redemption code is confirmed.")

    txns = get_kfin_txns_raw(folio_no, prodcode) if is_kfin else get_cams_txns_raw(folio_no, prodcode)
    if txns.empty:
        st.info("No transactions found.")
        st.stop()

    lots, matches = cg.replay_folio_scheme(txns)
    default_cat = cg.classify_tax_category(scheme_name=scheme_name)

    tc1, tc2 = st.columns(2)
    with tc1:
        tax_cat = st.radio(
            "Tax category", ["equity", "debt"],
            index=0 if default_cat == "equity" else 1, horizontal=True, key="cg_cat",
            help="Equity = ≥65% equity allocation. Debt = everything else (debt funds, FoFs, "
                 "gold/silver ETFs, international funds — 'specified mutual funds')."
        )
    with tc2:
        if tax_cat == "debt":
            slab = st.number_input(
                "Income slab rate (%) — applied to debt gains",
                0.0, 42.0, 30.0, key="cg_slab"
            ) / 100
            st.caption(f"Slab rate applied: {slab * 100:.1f}%")
        else:
            slab = 0.30  # unused for equity

    if is_kfin:
        tab2 = st.tabs(["🔮 What-if Redemption"])[0]
        tab1 = None
    else:
        tab1, tab2 = st.tabs(["✅ Realized Gains", "🔮 What-if Redemption"])

        # ── TAB 1: already-placed redemptions (CAMS only) ──
    if tab1 is not None:
        with tab1:
            if not matches:
                st.info("No redemptions found for this folio/scheme yet.")
            else:
                tax = cg.tax_for_matches(matches, tax_cat, slab)
                c1, c2, c3, c4 = st.columns(4)
                c1.metric("Total Gain", format_currency(tax["total_gain"]))
                c2.metric("STCG", format_currency(tax["stcg_gain"]))
                c2.caption(f"Tax: {format_currency(tax['stcg_tax'])}")
                c3.metric("LTCG", format_currency(tax["ltcg_gain"]))
                c3.caption(f"Tax: {format_currency(tax['ltcg_tax'])}")
                c4.metric("Estimated Tax", format_currency(tax["total_tax"]))

                if tax_cat == "equity":
                    used, limit = tax["exemption_used"], tax["exemption_limit"]
                    st.progress(min(used / limit, 1.0) if limit else 0.0,
                                text=f"LTCG exemption used: {format_currency(used)} / {format_currency(limit)}")
                    st.caption(f"Taxable LTCG: {format_currency(tax['ltcg_taxable'])} "
                               f"— exemption assumed available in full; reduce if you have other equity LTCG this FY.")
                else:
                    st.caption(f"Slab rate applied: {slab * 100:.1f}%")

                st.dataframe(cg.matches_to_df(matches), width="stretch", hide_index=True)

    # ── TAB 2: hypothetical future redemption ──
    with tab2:
        remaining_units = sum(l.remaining_units for l in lots)
        st.metric("Units Currently Held (per transaction history)", f"{remaining_units:.4f}")

        if remaining_units <= 0:
            st.info("No remaining units to redeem.")
        else:
            # ── Reliable NAV: reuse same canonical AMFI source as Dashboard/Client ──
            cg_nav_df = st.session_state.get("folio_nav_df")
            if cg_nav_df is None:
                download_and_save_nav_if_needed()
                cg_nav_df = get_all_folios_with_isin_and_nav(get_conn)
                st.session_state["folio_nav_df"] = cg_nav_df

            nav_match = cg_nav_df[
                (cg_nav_df["folio_id"] == folio_no) &
                (cg_nav_df["product_code"].astype(str).str.strip().str.upper() == prodcode.strip().upper())
                ]
            auto_nav = float(nav_match["current_nav"].iloc[0]) if not nav_match.empty and pd.notna(
                nav_match["current_nav"].iloc[0]) else 0.0
            auto_nav_date = nav_match["nav_date"].iloc[0] if not nav_match.empty and pd.notna(
                nav_match["nav_date"].iloc[0]) else None

            invested_value = sum(l.remaining_units * l.rate for l in lots)
            current_value_est = remaining_units * auto_nav
            unrealized_gain = current_value_est - invested_value

            s1, s2, s3 = st.columns(3)
            s1.metric("Invested", format_currency(invested_value))
            s2.metric("Current Value", format_currency(current_value_est))
            s3.metric("Unrealized Gain", format_currency(unrealized_gain))

            n1, n2 = st.columns(2)
            with n1:
                nav_key = f"cg_hyp_nav_{folio_no}_{prodcode}"
                if nav_key not in st.session_state:
                    st.session_state[nav_key] = auto_nav
                nav_input = st.number_input(
                    "Redemption NAV (₹)", min_value=0.0, key=nav_key,
                    help="Auto-filled from latest AMFI NAV (folio + scheme matched). Edit to project a different price."
                )
                if auto_nav_date:
                    st.caption(f"📅 NAV as of {auto_nav_date}")
                else:
                    st.caption("⚠️ No NAV date found — enter manually")
            with n2:
                units_key = f"cg_hyp_units_{folio_no}_{prodcode}"
                if units_key not in st.session_state:
                    st.session_state[units_key] = float(remaining_units)
                redeem_units = st.number_input(
                    "Units to redeem", min_value=0.0, max_value=float(remaining_units),
                    key=units_key
                )

            if nav_input <= 0:
                st.warning("Enter a redemption NAV to calculate.")
            else:
                hyp_matches = cg.hypothetical_redemption(txns, redeem_units, nav_input)
                hyp_tax = cg.tax_for_matches(hyp_matches, tax_cat, slab)

                c1, c2, c3, c4 = st.columns(4)
                c1.metric("Projected Gain", format_currency(hyp_tax["total_gain"]))
                c2.metric("STCG", format_currency(hyp_tax["stcg_gain"]))
                c2.caption(f"Tax: {format_currency(hyp_tax['stcg_tax'])}")
                c3.metric("LTCG", format_currency(hyp_tax["ltcg_gain"]))
                c3.caption(f"Tax: {format_currency(hyp_tax['ltcg_tax'])}")
                c4.metric("Estimated Tax", format_currency(hyp_tax["total_tax"]))

                if tax_cat == "equity":
                    used, limit = hyp_tax["exemption_used"], hyp_tax["exemption_limit"]
                    st.progress(min(used / limit, 1.0) if limit else 0.0,
                                text=f"LTCG exemption used: {format_currency(used)} / {format_currency(limit)}")
                    st.caption(f"Taxable LTCG: {format_currency(hyp_tax['ltcg_taxable'])} "
                               f"— exemption assumed available in full; reduce if you have other equity LTCG this FY.")

                st.dataframe(cg.matches_to_df(hyp_matches), width="stretch", hide_index=True)

# ==================== ⚙️ ADMIN PANEL ====================
elif mode == "⚙️ Admin Panel":
    st.header("⚙️ Admin Panel")

    # ── Manual NAV redownload ──
    nav_status = get_snapshot_status()
    nc1, nc2 = st.columns([3, 1])
    with nc1:
        if nav_status["latest_date"]:
            st.caption(f"📡 Latest saved NAV snapshot: **{nav_status['latest_date']}** "
                       f"({nav_status['latest_bytes']:,} bytes)")
        else:
            st.caption("📡 No NAV snapshot saved yet.")
    with nc2:
        if st.button("🔄 Redownload NAV", width="stretch",
                     help="Force-fetch latest NAV from AMFI, ignoring cycle cache"):
            with st.spinner("Fetching latest NAV from AMFI..."):
                result = download_and_save_nav_if_needed(force=True)
            if result["ok"]:
                st.cache_data.clear()
                st.session_state.pop("folio_nav_df", None)
                st.session_state.pop("folio_nav_summary", None)
                _amfi.load(force=True)
                st.success(f"✅ {result['reason']}")
                st.rerun()
            else:
                st.error(f"❌ {result['reason']}")
    nc3 = st.columns(1)[0]
    with nc3:
          prev_status = st.session_state.get("prev_nav_done_for")
          if prev_status:
              st.caption(f"✅ Previous business day NAV synced ({prev_status})")
          else:
              st.caption("⚠️ Previous business day NAV not yet synced")
          if st.button("🔄 Sync Previous Business Day NAV", width="stretch",
                       help="Force-retry fetching the prior working day's NAV history — "
                            "use this if the 1-day-diff column looks empty/zero."):
              with st.spinner("Fetching previous business day NAV from AMFI..."):
                  result = sync_previous_business_day_nav_if_needed(force=True)
              if result["ok"]:
                  st.cache_data.clear()
                  st.session_state.pop("folio_nav_df", None)
                  st.session_state.pop("folio_nav_summary", None)
                  st.success(f"✅ {result['reason']}")
                  st.rerun()
              else:
                  st.error(f"❌ {result['reason']}")
    st.divider()

    tab_upload, tab_raw = st.tabs(["📤 Upload Data", "📄 View Raw Data"])

    # ---------- UPLOAD TAB ----------
    with tab_upload:
        data_manager.render_data_manager()

    # ---------- RAW DATA TAB ----------
    with tab_raw:
        st.subheader("📄 Raw Data Explorer")
        st.caption("View raw uploaded data directly from the database.")

        source = st.radio(
            "Select Data Source",
            ["BSE", "CAMS", "KFinTech"],
            horizontal=True,
            key="raw_source"
        )

        if source == "BSE":
            data_type = st.radio(
                "Select Data Type",
                ["Bse Client Master", "BSE SiP", "BSE Scheme Master"],
                horizontal=True,
                key="raw_bse_type"
            )
            table_map = {
                "Bse Client Master": "bse_client_master",
                "BSE SiP": "bse_sip",
                "BSE Scheme Master": "bse_scheme_master"
            }
            table = table_map[data_type]

        elif source == "CAMS":
            data_type = st.radio(
                "Select Data Type",
                ["Folio Master", "Transactions", "SIP Master", "AUM", "Brokerage"],
                horizontal=True,
                key="raw_cams_type"
            )
            table_map = {
                "Folio Master": "cams_wbr9_folio",
                "Transactions": "cams_wbr2_transaction",
                "SIP Master": "cams_wbr49_sip",
                "AUM": "cams_wbr4_aum",
                "Brokerage": "cams_wbr77_brokerage"
            }
            table = table_map[data_type]

        else:
            data_type = st.radio(
                "Select Data Type",
                ["Folio Master", "Transactions", "SIP Master", "AUM", "Brokerage"],
                horizontal=True,
                key="raw_kfin_type"
            )
            table_map = {
                "Folio Master": "kfin_mfsd211_folio",
                "Transactions": "kfin_mfsd201_transaction",
                "SIP Master": "kfin_mfsd243_sip",
                "AUM": "kfin_mfsd203_aum",
                "Brokerage": "kfin_mfsd205_brokerage"
            }
            table = table_map[data_type]

        st.divider()

        with get_conn() as conn:
            total_rows = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

        st.metric(f"Total Records in {table}", f"{total_rows:,}")

        if total_rows > 0:
            df_raw = load_table_summary(table)
            if not df_raw.empty:
                # ── View options ──
                view_col1, view_col2 = st.columns([1, 3])
                with view_col1:
                    auto_fit = st.toggle("🔍 Auto-fit columns", value=True,
                                         help="Resize columns to fit content so full headers are visible")
                with view_col2:
                    st.caption(f"Showing {len(df_raw):,} rows × {len(df_raw.columns)} columns")

                # ── AgGrid with dark mode support and auto-sizing ──
                try:
                    from st_aggrid import AgGrid, GridOptionsBuilder, GridUpdateMode

                    gb = GridOptionsBuilder.from_dataframe(df_raw)
                    gb.configure_default_column(
                        filter=True,
                        sortable=True,
                        resizable=True,
                        wrapText=True,
                        autoHeaderHeight=True,
                        minWidth=100
                    )

                    if auto_fit:
                        gb.configure_grid_options(
                            autoSizeStrategy={'type': 'fitCellContents'},
                            suppressColumnVirtualisation=True
                        )
                    else:
                        gb.configure_grid_options(
                            autoSizeStrategy={'type': 'fitGridWidth'},
                            suppressColumnVirtualisation=True
                        )

                    gb.configure_pagination(
                        paginationAutoPageSize=False,
                        paginationPageSize=50
                    )

                    grid_opts = gb.build()

                    AgGrid(
                        df_raw,
                        gridOptions=grid_opts,
                        height=600,
                        update_mode=GridUpdateMode.NO_UPDATE,
                        fit_columns_on_grid_load=not auto_fit,
                        allow_unsafe_jscode=True,
                        theme="alpine-dark" if dark else "alpine",
                        key=f"raw_{table}_{auto_fit}"
                    )
                except ImportError:
                    st.dataframe(
                        df_raw,
                        width="stretch",
                        hide_index=True,
                        column_config={
                            col: st.column_config.Column(label=col, width="large")
                            for col in df_raw.columns
                        }
                    )

                csv = df_raw.to_csv(index=False).encode("utf-8")
                st.download_button(
                    label="⬇️ Download Raw Data (CSV)",
                    data=csv,
                    file_name=f"{source}_{data_type.replace(' ', '_')}_raw.csv",
                    mime="text/csv",
                )
            else:
                st.info("No data to display.")
        else:
            st.info(f"No data in `{table}` yet. Upload data in the Upload Data tab.")

        # DB Stats at bottom
        st.divider()
        st.subheader("🗄️ Database Stats")
        stats = load_db_stats()

        cols = st.columns(3)
        categories = {
            "BSE": ["bse_client_master", "bse_sip", "bse_scheme_master"],
            "CAMS": ["cams_wbr4_aum", "cams_wbr9_folio", "cams_wbr2_transaction", "cams_wbr49_sip",
                     "cams_wbr77_brokerage"],
            "KFinTech": ["kfin_mfsd203_aum", "kfin_mfsd211_folio", "kfin_mfsd201_transaction", "kfin_mfsd243_sip",
                         "kfin_mfsd205_brokerage"],
        }
        for i, (cat, tables) in enumerate(categories.items()):
            with cols[i]:
                st.markdown(f"**{cat}**")
                for t in tables:
                    st.caption(f"{t}: **{stats.get(t, 0):,}**")

        with cols[0]:
            st.markdown("**Other**")
            st.caption(f"monthly_brokerage: **{stats.get('monthly_brokerage', 0):,}**")
            st.caption(f"amc_code_map: **{stats.get('amc_code_map', 0):,}**")
