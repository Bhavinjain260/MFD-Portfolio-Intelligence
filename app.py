import logging
import os
import re
import time
import requests
import threading
import warnings
from datetime import datetime, timedelta, date as date_cls
from datetime import timedelta, datetime as dt
from datetime import time as time_cls
from datetime import timedelta
from typing import Optional

import pandas as pd
import plotly.express as px
import streamlit as st

import capital_gain as cg

import capital_gain as cg_row
import data_manager
import nav_scheduler
import data_manager as dm
from data_manager import current as data_version
import xirr
from init_db import init_db, get_conn
from theme_patch import THEME_WATCHER_JS, render_theme
import cams_mailback_sync
import cams_mailback_sync as mail_sync
import email_tempate

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

@st.cache_data(show_spinner=False)
def get_client_cams_schemes(folio_ids: list[str], _v: int) -> pd.DataFrame:
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

@st.cache_data(show_spinner=False)
def get_client_kfin_schemes(folio_ids: list[str], _v: int) -> pd.DataFrame:
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


def search_clients(query: str, limit: int = 20) -> pd.DataFrame:
    """
    Search bse_client_master by name, code, PAN, guardian PAN, email, mobile.
    Includes minors who have empty primary_holder_pan but valid guardian_pan.
    """
    if not query or not query.strip():
        return pd.DataFrame()
    q = f"%{query.strip().upper()}%"
    q_exact = query.strip().upper()

    with get_conn() as conn:
        df = pd.read_sql("""
            SELECT
                client_code,
                COALESCE(
                    NULLIF(TRIM(primary_holder_first_name || ' ' ||
                              COALESCE(primary_holder_middle_name || ' ', '') ||
                              primary_holder_last_name), ''),
                    client_code
                ) AS display_name,
                COALESCE(
                    NULLIF(TRIM(primary_holder_pan), ''),
                    NULLIF(TRIM(guardian_pan), ''),
                    '—'
                ) AS display_pan,
                tax_status,
                CASE
                    WHEN TRIM(COALESCE(primary_holder_pan,'')) = ''
                    THEN 'Minor'
                    ELSE 'Adult'
                END AS client_category,
                email,
                indian_mobile_no AS mobile,
                guardian_first_name || ' ' || guardian_last_name AS guardian_name
            FROM bse_client_master
            WHERE
                UPPER(TRIM(client_code))                             LIKE ?
                OR UPPER(TRIM(primary_holder_first_name))             LIKE ?
                OR UPPER(TRIM(primary_holder_last_name))              LIKE ?
                OR UPPER(TRIM(primary_holder_first_name || ' ' ||
                              primary_holder_last_name))              LIKE ?
                OR UPPER(TRIM(primary_holder_pan))                    LIKE ?
                OR UPPER(TRIM(guardian_pan))                          LIKE ?
                OR UPPER(TRIM(email))                                 LIKE ?
                OR UPPER(TRIM(indian_mobile_no))                      LIKE ?
            ORDER BY
                CASE WHEN UPPER(TRIM(client_code)) = ? THEN 0
                     WHEN UPPER(TRIM(primary_holder_first_name)) = ? THEN 1
                     ELSE 2
                END,
                display_name
            LIMIT ?
        """, conn, params=(
            q, q, q, q, q, q, q, q,
            q_exact, q_exact,
            limit
        ))
    return df



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
                   COALESCE(
                       NULLIF(TRIM(
                           COALESCE(b.primary_holder_first_name,'') || ' ' ||
                           COALESCE(b.primary_holder_last_name,'')
                       ), ''),
                       m.client_code
                   ) AS name
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

def get_family_folios_by_rta(family_id: int) -> tuple:
    """Returns (cams_folio_list, kfin_folio_list) across all family members —
    same matching logic as get_family_all_folios, but split by RTA since the
    Valuation Report needs folio→RTA mapping, not just a flat folio set."""
    members = get_family_members(family_id)
    if members.empty:
        return [], []

    cams_folios = set()
    kfin_folios = set()

    with get_conn() as conn:
        for _, member in members.iterrows():
            client_code = member["client_code"]
            identity = get_client_identity(client_code)
            if not identity:
                continue

            name_clean = identity["name"].strip().upper() if identity["name"] else ""
            match_pan = identity["match_pan"]

            if identity["is_minor"]:
                if name_clean:
                    for row in conn.execute(
                        "SELECT foliochk FROM cams_wbr9_folio WHERE TRIM(UPPER(inv_name)) LIKE ? || '%'",
                        (name_clean,)
                    ).fetchall():
                        cams_folios.add(row[0])
                    for row in conn.execute(
                        "SELECT folio FROM kfin_mfsd211_folio WHERE TRIM(UPPER(investor_name)) LIKE ? || '%'",
                        (name_clean,)
                    ).fetchall():
                        kfin_folios.add(row[0])
            else:
                if match_pan and match_pan.strip():
                    pan_up = match_pan.strip().upper()
                    for row in conn.execute(
                        "SELECT foliochk FROM cams_wbr9_folio WHERE TRIM(UPPER(pan_no)) = ?", (pan_up,)
                    ).fetchall():
                        cams_folios.add(row[0])
                    for row in conn.execute(
                        "SELECT folio FROM kfin_mfsd211_folio WHERE TRIM(UPPER(pan_number)) = ?", (pan_up,)
                    ).fetchall():
                        kfin_folios.add(row[0])
                if name_clean:
                    for row in conn.execute(
                        "SELECT foliochk FROM cams_wbr9_folio WHERE TRIM(UPPER(inv_name)) LIKE ? || '%'",
                        (name_clean,)
                    ).fetchall():
                        cams_folios.add(row[0])
                    for row in conn.execute(
                        "SELECT folio FROM kfin_mfsd211_folio WHERE TRIM(UPPER(investor_name)) LIKE ? || '%'",
                        (name_clean,)
                    ).fetchall():
                        kfin_folios.add(row[0])

    return sorted(cams_folios), sorted(kfin_folios)

def get_investor_names_for_folios(cams_folios: list, kfin_folios: list) -> dict:
    """folio_id -> investor name, straight from the folio master tables."""
    name_map = {}
    with get_conn() as conn:
        if cams_folios:
            ph = ",".join(["?"] * len(cams_folios))
            rows = conn.execute(
                f"SELECT foliochk, inv_name FROM cams_wbr9_folio WHERE foliochk IN ({ph})",
                cams_folios
            ).fetchall()
            for folio_id, name in rows:
                name_map[folio_id] = (name or "").strip()
        if kfin_folios:
            ph = ",".join(["?"] * len(kfin_folios))
            rows = conn.execute(
                f"SELECT folio, investor_name FROM kfin_mfsd211_folio WHERE folio IN ({ph})",
                kfin_folios
            ).fetchall()
            for folio_id, name in rows:
                name_map.setdefault(folio_id, (name or "").strip())
    return name_map


def get_family_all_folios(family_id: int) -> set:
    """
    Gather ALL folio IDs for ALL family members.
      • Adults  – matched by PAN in folio tables.
      • Minors  – matched by investor_name (because their folio PAN is empty).
    """
    members = get_family_members(family_id)
    if members.empty:
        return set()

    all_folios: set = set()

    with get_conn() as conn:
        for _, member in members.iterrows():
            client_code = member["client_code"]
            identity = get_client_identity(client_code)
            if not identity:
                continue

            name_clean = identity["name"].strip().upper() if identity["name"] else ""
            match_pan = identity["match_pan"]

            if identity["is_minor"]:
                if name_clean:
                    for row in conn.execute(
                        "SELECT foliochk FROM cams_wbr9_folio "
                        "WHERE TRIM(UPPER(inv_name)) LIKE ? || '%'",
                        (name_clean,)
                    ).fetchall():
                        all_folios.add(row[0])
                    for row in conn.execute(
                        "SELECT folio FROM kfin_mfsd211_folio "
                        "WHERE TRIM(UPPER(investor_name)) LIKE ? || '%'",
                        (name_clean,)
                    ).fetchall():
                        all_folios.add(row[0])
            else:
                if match_pan and match_pan.strip():
                    pan_up = match_pan.strip().upper()
                    for row in conn.execute(
                        "SELECT foliochk FROM cams_wbr9_folio "
                        "WHERE TRIM(UPPER(pan_no)) = ?", (pan_up,)
                    ).fetchall():
                        all_folios.add(row[0])
                    for row in conn.execute(
                        "SELECT folio FROM kfin_mfsd211_folio "
                        "WHERE TRIM(UPPER(pan_number)) = ?", (pan_up,)
                    ).fetchall():
                        all_folios.add(row[0])
                if name_clean:
                    for row in conn.execute(
                        "SELECT foliochk FROM cams_wbr9_folio "
                        "WHERE TRIM(UPPER(inv_name)) LIKE ? || '%'",
                        (name_clean,)
                    ).fetchall():
                        all_folios.add(row[0])
                    for row in conn.execute(
                        "SELECT folio FROM kfin_mfsd211_folio "
                        "WHERE TRIM(UPPER(investor_name)) LIKE ? || '%'",
                        (name_clean,)
                    ).fetchall():
                        all_folios.add(row[0])

    return all_folios


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

@st.cache_data(show_spinner=False)
def compute_client_holdings(client_code: str, _folio_nav_df: pd.DataFrame, _v: int) -> pd.DataFrame:
    """
    Same enrichment logic as the Clients tab, factored out so Family Portfolio
    can reuse it per member.

    NOTE: param is _folio_nav_df so Streamlit skips hashing it — hashing a
    multi-thousand-row DataFrame on every call would cost nearly as much as
    just recomputing. Cache key is client_code only. This means the cache
    won't auto-invalidate if folio_nav_df's contents change without
    client_code changing — st.cache_data.clear() on Refresh handles that.
    """
    folio_nav_df = _folio_nav_df

    identity = get_client_identity(client_code)
    if not identity:
        return pd.DataFrame()
    name, match_pan, is_minor = identity["name"], identity["match_pan"], identity["is_minor"]
    name_clean = name.strip().upper() if name else ""

    with get_conn() as conn:
        if is_minor:
            cams_f = pd.read_sql(
                "SELECT foliochk FROM cams_wbr9_folio WHERE TRIM(UPPER(inv_name)) LIKE ? || '%'",
                conn, params=(name_clean,))
            kfin_f = pd.read_sql(
                "SELECT folio FROM kfin_mfsd211_folio WHERE TRIM(UPPER(investor_name)) LIKE ? || '%'",
                conn, params=(name_clean,))
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
        kfin_invested_df = get_kfin_invested_per_scheme(sorted(kfin_f['folio'].tolist()), _v)
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
        cams_invested_df = get_cams_invested_per_scheme(sorted(cams_f['foliochk'].tolist()), _v)
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


# ==================== VALUATION REPORT HELPERS ====================
def fetch_all_folio_transactions(folio_no: str, rta: str) -> pd.DataFrame:
    """
    Reuses get_cams_txns_raw / get_kfin_txns_raw (same functions the
    Clients > Portfolio and Transactions tabs already rely on), looping
    over each scheme in the folio.

    Sign convention matches the rest of the app:
      - CAMS: units/amount stored positive; redemption = trxntype 'R1'
        (same check as load_dashboard_summary / get_cams_invested_per_scheme)
      - KFinTech: units/amount already signed at source (no direction
        guessing needed — same assumption get_kfin_invested_per_scheme makes)
    """
    if rta == 'CAMS':
        with get_conn() as conn:
            codes = pd.read_sql(
                "SELECT DISTINCT TRIM(UPPER(prodcode)) AS pc "
                "FROM cams_wbr2_transaction WHERE folio_no = ?",
                conn, params=(folio_no,)
            )['pc'].tolist()
        frames = []
        for pc in codes:
            t = get_cams_txns_raw(folio_no, pc)
            if not t.empty:
                t['product_code'] = pc
                frames.append(t)
        df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    else:
        with get_conn() as conn:
            codes = pd.read_sql(
                "SELECT DISTINCT TRIM(UPPER(fmcode)) AS pc "
                "FROM kfin_mfsd201_transaction WHERE td_acno = ?",
                conn, params=(folio_no,)
            )['pc'].tolist()
        frames = []
        for pc in codes:
            t = get_kfin_txns_raw(folio_no, pc)
            if not t.empty:
                t['product_code'] = pc
                frames.append(t)
        df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    if df.empty:
        return df

    if 'trxntype' not in df.columns:
        df['trxntype'] = ''

    

    df['_date'] = pd.to_datetime(df['traddate'], errors='coerce')
    still_nat = df['_date'].isna() & df['traddate'].notna()
    if still_nat.any():
        log.warning("[VALUATION] Folio %s: %d rows with unparseable dates",
                    folio_no, still_nat.sum())
    df = df.dropna(subset=['_date'])

    df['units'] = pd.to_numeric(df['units'], errors='coerce').fillna(0)
    df['amount'] = pd.to_numeric(df['amount'], errors='coerce').fillna(0)
    df['purprice'] = pd.to_numeric(df['purprice'], errors='coerce')

    if rta == 'CAMS':
        is_redemption = df['trxntype'].astype(str).str.strip().str.upper() == 'R1'
        df['direction'] = is_redemption.map({True: 'OUT', False: 'IN'})
        df['signed_units'] = df['units'].where(~is_redemption, -df['units'])
    else:
        df['direction'] = df['units'].apply(
            lambda u: 'OUT' if u < 0 else ('IN' if u > 0 else 'NEUTRAL')
        )
        df['signed_units'] = df['units']

    df = df.sort_values('_date', kind='stable').reset_index(drop=True)
    return df

def calc_units_before(txn_df: pd.DataFrame, product_code: str,
                      before_date: pd.Timestamp) -> float:
    mask = (txn_df['product_code'] == product_code) & (txn_df['_date'] < before_date)
    return max(float(txn_df.loc[mask, 'signed_units'].sum()), 0.0)


def calc_units_upto(txn_df: pd.DataFrame, product_code: str,
                    as_of_date: pd.Timestamp) -> float:
    mask = (txn_df['product_code'] == product_code) & (txn_df['_date'] <= as_of_date)
    return max(float(txn_df.loc[mask, 'signed_units'].sum()), 0.0)


def calc_invested_upto(txn_df: pd.DataFrame, product_code: str,
                       as_of_date: pd.Timestamp) -> float:
    mask = (
        (txn_df['product_code'] == product_code)
        & (txn_df['_date'] <= as_of_date)
        & (txn_df['direction'] == 'IN')
    )
    return float(txn_df.loc[mask, 'amount'].sum())


def get_or_fetch_nav_for_date(target_iso: str) -> dict:
    iso = target_iso
    if _have_snapshot_for_date(iso):
        path = _snapshot_path(iso)
        with open(path, 'r', encoding='utf-8') as f:
            nav_map, _, _ = _parse_nav_text(f.read())
        return {k: v[0] for k, v in nav_map.items() if v[0] > 0}

    try:
        target_d = datetime.strptime(iso, "%Y-%m-%d").date()
        resp = download_business_day_nav(target_d, timeout=30)
        actual = resp.get("actual_date")
        if actual and resp.get("text"):
            _save_nav_snapshot(resp["text"], actual)
            with open(_snapshot_path(actual), 'r', encoding='utf-8') as f:
                nav_map, _, _ = _parse_nav_text(f.read())
            return {k: v[0] for k, v in nav_map.items() if v[0] > 0}
    except Exception as e:
        log.warning("[VALUATION] NAV fetch failed for %s: %s", iso, e)

    return {}

# ==================== PDF GENERATION ====================


def _find_font_path() -> tuple:
    """
    Try to find a Unicode-capable TTF font.
    Returns (regular_path, bold_path) or (None, None).
    """
    candidates = [
        # Linux
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        # macOS
        ("/Library/Fonts/Arial Unicode.ttf",
         "/Library/Fonts/Arial Bold.ttf"),
        # Windows
        (r"C:\Windows\Fonts\arial.ttf",
         r"C:\Windows\Fonts\arialbd.ttf"),
        # Common Linux alternatives
        ("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
         "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
        ("/usr/share/fonts/TTF/DejaVuSans.ttf",
         "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"),
    ]
    for regular, bold in candidates:
        if os.path.exists(regular):
            return (regular, bold if os.path.exists(bold) else regular)
    return (None, None)



def _shorten_scheme_for_summary(name: str) -> str:
    """
    Truncate scheme name at the word 'FUND' (case-insensitive).
    'BANDHAN SMALL CAP FUND REGULAR PLAN-GROWTH' → 'BANDHAN SMALL CAP FUND'
    'SBI SILVER ETF FUND OF FUND - REGULAR PLAN - GROWTH' → 'SBI SILVER ETF FUND OF FUND'
    If 'FUND' is not present, returns the original name unchanged.
    """
    if not name:
        return ""
    # Find the last occurrence of the whole word FUND
    m = re.search(r'\bFUND\b', name, flags=re.IGNORECASE)
    if m:
        return name[:m.end()].strip()
    return name.strip()

def generate_capital_gain_pdf(
    client_name: str,
    pan: str,
    client_code: str,
    fy_str: str,
    detail_rows: list[dict],
    total_buy: float,
    total_sale: float,
    total_gain: float,
) -> bytes | None:
    """PDF version of the capital gain report (FIFO realized gains, CAMS). Landscape — 11 columns."""
    try:
        from fpdf import FPDF
    except ImportError:
        return None

    reg_path, bold_path = _find_font_path()
    if not reg_path:
        return None

    pdf = FPDF(orientation='L', unit='mm', format='A4')
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_font('Main', '', reg_path, uni=True)
    pdf.add_font('Main', 'B', bold_path, uni=True)
    pdf.set_margins(12, 12, 12)

    BLUE   = (41, 128, 185)
    DARK   = (44, 62, 80)
    GREY   = (127, 140, 141)
    LIGHT  = (236, 240, 241)
    WHITE  = (255, 255, 255)
    GREEN  = (39, 174, 96)
    RED    = (192, 57, 43)
    PAGE_W = 297 - 24  # A4 landscape width minus margins = 273

    def _fmt_inr(val) -> str:
        try:
            return f"\u20b9{float(val):,.2f}"
        except (TypeError, ValueError):
            return "N/A"

    def _fmt_units(val) -> str:
        try:
            return f"{float(val):.4f}"
        except (TypeError, ValueError):
            return "N/A"

    def _fmt_date(val) -> str:
        if val is None:
            return ""
        try:
            return val.strftime("%Y-%m-%d")
        except AttributeError:
            return str(val)

    pdf.add_page()

    pdf.set_font('Main', 'B', 16)
    pdf.set_text_color(*DARK)
    pdf.cell(PAGE_W, 10, 'Capital Gain Report', align='C', new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)

    pdf.set_font('Main', '', 9)
    pdf.set_text_color(*GREY)
    pdf.cell(PAGE_W, 5, f'Client: {client_name}', new_x="LMARGIN", new_y="NEXT")
    pdf.cell(PAGE_W, 5, f'PAN: {pan or "N/A"}   |   Code: {client_code}   |   FY: {fy_str}',
             new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    pdf.set_fill_color(*LIGHT)
    col_w = PAGE_W / 3
    pdf.set_font('Main', 'B', 8)
    pdf.set_text_color(*GREY)
    pdf.cell(col_w, 5, 'Total Buy', border=0, fill=True, new_x="RIGHT")
    pdf.cell(col_w, 5, 'Total Sale', border=0, fill=True, new_x="RIGHT")
    pdf.cell(col_w, 5, 'Total Gain / Loss', border=0, fill=True, new_x="LMARGIN", new_y="NEXT")

    pdf.set_font('Main', 'B', 11)
    pdf.set_text_color(*DARK)
    pdf.cell(col_w, 7, _fmt_inr(total_buy), border=0, fill=True, new_x="RIGHT")
    pdf.cell(col_w, 7, _fmt_inr(total_sale), border=0, fill=True, new_x="RIGHT")
    gain_color = GREEN if total_gain >= 0 else RED
    pdf.set_text_color(*gain_color)
    pdf.cell(col_w, 7, _fmt_inr(total_gain), border=0, fill=True, new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(*DARK)
    pdf.ln(6)

    pdf.set_font('Main', 'B', 11)
    pdf.set_text_color(*BLUE)
    pdf.cell(PAGE_W, 6, 'Realized Gains — Transaction Detail', new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    headers = ['Scheme', 'Folio', 'Buy Date', 'Buy Units', 'Buy NAV', 'Buy Value',
               'Sale Date', 'Sell Units', 'Sell NAV', 'Sell Value', 'Gain/Loss']
    widths  = [55, 28, 20, 18, 18, 22, 20, 18, 18, 22, 0]
    widths[-1] = PAGE_W - sum(widths[:-1])
    aligns  = ['L', 'C', 'C', 'R', 'R', 'R', 'C', 'R', 'R', 'R', 'R']

    row_h = 6
    pdf.set_font('Main', 'B', 7)
    pdf.set_fill_color(*BLUE)
    pdf.set_text_color(*WHITE)
    for h, w in zip(headers, widths):
        pdf.cell(w, row_h, h, border=1, align='C', fill=True)
    pdf.ln()

    rows_sorted = sorted(
        detail_rows,
        key=lambda r: (r.get("Sale Date") or date_cls.min),
        reverse=True,
    )

    pdf.set_font('Main', '', 7)
    pdf.set_text_color(*DARK)
    for i, row in enumerate(rows_sorted):
        fill = (i % 2 == 1)
        if fill:
            pdf.set_fill_color(*LIGHT)

        gl = row.get('Gain/Loss')
        vals = [
            str(row.get('Scheme', ''))[:40],
            str(row.get('Folio', '')),
            _fmt_date(row.get('Buy Date')),
            _fmt_units(row.get('Buy Units')),
            _fmt_units(row.get('Buy NAV')),
            _fmt_inr(row.get('Buy Value')),
            _fmt_date(row.get('Sale Date')),
            _fmt_units(row.get('Sell Units')),
            _fmt_units(row.get('Sell NAV')),
            _fmt_inr(row.get('Sell Value')),
            _fmt_inr(gl),
        ]
        for v, w, a in zip(vals, widths, aligns):
            pdf.cell(w, row_h, v, border=1, align=a, fill=fill)
        pdf.ln()

    pdf.set_font('Main', 'B', 7)
    pdf.set_fill_color(200, 200, 200)
    total_vals = ['TOTAL', '', '', '', '', _fmt_inr(total_buy), '', '', '', _fmt_inr(total_sale), _fmt_inr(total_gain)]
    for v, w, a in zip(total_vals, widths, aligns):
        pdf.cell(w, row_h, v, border=1, align=a, fill=True)
    pdf.ln(8)

    pdf.set_font('Main', '', 7)
    pdf.set_text_color(*GREY)
    pdf.cell(PAGE_W, 4, f'Report generated on {datetime.now().strftime("%d/%m/%Y %H:%M")}',
             align='C', new_x="LMARGIN", new_y="NEXT")

    return bytes(pdf.output())

def generate_capital_gain_html(
    client_name: str,
    pan: str,
    client_code: str,
    fy_str: str,
    detail_rows: list[dict],
    total_buy: float,
    total_sale: float,
    total_gain: float,
) -> str:
    """Generate self-contained HTML for Capital Gain Report (email-friendly)."""
    def fi(v):
        try:
            return f"₹{float(v):,.2f}"
        except:
            return "N/A"

    gain_cls = "positive" if total_gain >= 0 else "negative"

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Capital Gain Report - {client_name}</title>
<style>
  @page {{ size: A4; margin: 12mm; }}
  body {{ font-family: Arial, Helvetica, sans-serif; font-size: 10px; color: #2c3e50; margin: 0; padding: 15px; }}
  h1 {{ font-size: 17px; text-align: center; margin: 0 0 3px 0; }}
  .meta {{ text-align: center; color: #7f8c8d; font-size: 9px; margin-bottom: 12px; }}
  .metrics {{ display: flex; gap: 10px; margin-bottom: 14px; }}
  .metric-box {{ flex: 1; background: #ecf0f1; padding: 8px; border-radius: 4px; text-align: center; }}
  .metric-box .label {{ font-size: 8px; color: #7f8c8d; text-transform: uppercase; }}
  .metric-box .value {{ font-size: 14px; font-weight: bold; margin-top: 2px; }}
  .positive {{ color: #27ae60; }}
  .negative {{ color: #c0392b; }}
  table {{ border-collapse: collapse; width: 100%; margin-bottom: 10px; font-size: 9px; }}
  th {{ background: #2980b9; color: white; padding: 4px 5px; text-align: center; font-weight: bold; }}
  td {{ padding: 3px 5px; border: 1px solid #dcdcdc; }}
  tr:nth-child(even) {{ background: #f7fafc; }}
  .total-row {{ font-weight: bold; background: #e0e0e0 !important; }}
  .right {{ text-align: right; }}
  .center {{ text-align: center; }}
  .footer {{ text-align: center; color: #aaa; font-size: 8px; margin-top: 20px; border-top: 1px solid #eee; padding-top: 6px; }}
</style></head><body>
<h1>Capital Gain Report</h1>
<div class="meta">
  {client_name} &nbsp;|&nbsp; PAN: {pan or "N/A"} &nbsp;|&nbsp; Code: {client_code} &nbsp;|&nbsp; FY: {fy_str}
</div>
<div class="metrics">
  <div class="metric-box"><div class="label">Total Buy</div><div class="value">{fi(total_buy)}</div></div>
  <div class="metric-box"><div class="label">Total Sale</div><div class="value">{fi(total_sale)}</div></div>
  <div class="metric-box"><div class="label">Gain / Loss</div><div class="value {gain_cls}">{fi(total_gain)}</div></div>
</div>
<table>
<tr>
  <th class="left">Scheme</th>
  <th class="center">Folio</th>
  <th class="center">Buy Date</th>
  <th class="right">Buy Units</th>
  <th class="right">Buy NAV</th>
  <th class="right">Buy Value</th>
  <th class="center">Sale Date</th>
  <th class="right">Sell Units</th>
  <th class="right">Sell NAV</th>
  <th class="right">Sell Value</th>
  <th class="right">Gain/Loss</th>
</tr>
"""

    for r in sorted(detail_rows, key=lambda x: x.get('Sale Date') or '', reverse=True):
        gl = r.get('Gain/Loss')
        gl_cls = "positive" if gl and gl >= 0 else ("negative" if gl and gl < 0 else "")
        
        buy_date = r['Buy Date'].strftime('%Y-%m-%d') if r.get('Buy Date') else ''
        sale_date = r['Sale Date'].strftime('%Y-%m-%d') if r.get('Sale Date') else ''
        
        html += f"""<tr>
  <td>{r.get('Scheme', '')}</td>
  <td class="center">{r.get('Folio', '')}</td>
  <td class="center">{buy_date}</td>
  <td class="right">{r.get('Buy Units', 0):.4f}</td>
  <td class="right">{r.get('Buy NAV', 0):.4f}</td>
  <td class="right">{fi(r.get('Buy Value'))}</td>
  <td class="center">{sale_date}</td>
  <td class="right">{r.get('Sell Units', 0):.4f}</td>
  <td class="right">{r.get('Sell NAV', 0):.4f}</td>
  <td class="right">{fi(r.get('Sell Value'))}</td>
  <td class="right {gl_cls}">{fi(gl)}</td>
</tr>"""

    html += f"""<tr class="total-row">
  <td colspan="5" style="text-align: left;">TOTAL</td>
  <td class="right">{fi(total_buy)}</td>
  <td colspan="3"></td>
  <td class="right">{fi(total_sale)}</td>
  <td class="right {gain_cls}">{fi(total_gain)}</td>
</tr>
</table>
<div class="footer">Report generated on {datetime.now().strftime("%d/%m/%Y %H:%M")}</div>
</body></html>"""
    return html

def generate_valuation_pdf(
    client_name: str,
    pan: str,
    client_code: str,
    mobile: str,
    val_date_str: str,
    period_from_str: str,
    summary_rows: list[dict],
    rta_txns: dict,
    total_invested: float,
    total_value: float,
    total_gain: float,
) -> bytes | None:
    """
    Generate a PDF bytes buffer for the valuation report.
    Returns None if fpdf2 is not installed or no font found.
    """
    try:
        from fpdf import FPDF
    except ImportError:
        return None

    reg_path, bold_path = _find_font_path()
    if not reg_path:
        return None

    pdf = FPDF(orientation='P', unit='mm', format='A4')
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_font('Main', '', reg_path, uni=True)
    pdf.add_font('Main', 'B', bold_path, uni=True)
    pdf.set_margins(12, 12, 12)

    # ── Colour palette ──
    BLUE   = (41, 128, 185)
    DARK   = (44, 62, 80)
    GREY   = (127, 140, 141)
    LIGHT  = (236, 240, 241)
    WHITE  = (255, 255, 255)
    GREEN  = (39, 174, 96)
    RED    = (192, 57, 43)
    PAGE_W = 210 - 24  # A4 width minus margins

    def _fmt_inr(val) -> str:
        try:
            return f"\u20b9{float(val):,.2f}"
        except (TypeError, ValueError):
            return "N/A"

    def _fmt_pct(val) -> str:
        try:
            return f"{float(val):.2f}%"
        except (TypeError, ValueError):
            return "N/A"

    def _fmt_units(val) -> str:
        try:
            return f"{float(val):.4f}"
        except (TypeError, ValueError):
            return "N/A"

    # ════════════════════════════════════════
    # PAGE 1 — HEADER + SUMMARY TABLE
    # ════════════════════════════════════════
    pdf.add_page()

    # Title
    pdf.set_font('Main', 'B', 16)
    pdf.set_text_color(*DARK)
    pdf.cell(PAGE_W, 10, 'Portfolio Valuation Report', align='C', new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)

    # Client info
    pdf.set_font('Main', '', 9)
    pdf.set_text_color(*GREY)
    pdf.cell(PAGE_W, 5, f'Client: {client_name}', new_x="LMARGIN", new_y="NEXT")
    pdf.cell(PAGE_W, 5,
             f'PAN: {pan or "N/A"}   |   Code: {client_code}   |   Mobile: {mobile or "N/A"}',
             new_x="LMARGIN", new_y="NEXT")
    pdf.cell(PAGE_W, 5,
             f'Period: {period_from_str} to {val_date_str}',
             new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    # Metrics bar
    pdf.set_fill_color(*LIGHT)
    col_w = PAGE_W / 3
    pdf.set_font('Main', 'B', 8)
    pdf.set_text_color(*GREY)
    pdf.cell(col_w, 5, 'Total Invested', border=0, fill=True, new_x="RIGHT")
    pdf.cell(col_w, 5, 'Total Value', border=0, fill=True, new_x="RIGHT")
    pdf.cell(col_w, 5, 'Gain / Loss', border=0, fill=True, new_x="LMARGIN", new_y="NEXT")

    pdf.set_font('Main', 'B', 11)
    pdf.set_text_color(*DARK)
    pdf.cell(col_w, 7, _fmt_inr(total_invested), border=0, fill=True, new_x="RIGHT")
    pdf.cell(col_w, 7, _fmt_inr(total_value), border=0, fill=True, new_x="RIGHT")
    gain_color = GREEN if total_gain >= 0 else RED
    pdf.set_text_color(*gain_color)
    pdf.cell(col_w, 7, _fmt_inr(total_gain), border=0, fill=True, new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(*DARK)
    pdf.ln(6)

    # Section header
    pdf.set_font('Main', 'B', 11)
    pdf.set_text_color(*BLUE)
    pdf.cell(PAGE_W, 6, 'Scheme Summary', new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    # Summary table
    s_headers = ['Scheme', 'Folio', 'Invested', 'Value', 'Gain/Loss', 'Return %']
    s_widths  = [70, 25, 30, 30, 30, 20]  # sum ≈ 205 ≈ PAGE_W
    # Adjust last col to fill
    s_widths[-1] = PAGE_W - sum(s_widths[:-1])

    row_h = 6
    pdf.set_font('Main', 'B', 7)
    pdf.set_fill_color(*BLUE)
    pdf.set_text_color(*WHITE)
    for h, w in zip(s_headers, s_widths):
        pdf.cell(w, row_h, h, border=1, align='C', fill=True)
    pdf.ln()

    pdf.set_font('Main', '', 7)
    pdf.set_text_color(*DARK)
    for i, row in enumerate(summary_rows):
        if i % 2 == 1:
            pdf.set_fill_color(*LIGHT)
            fill = True
        else:
            fill = False

        vals = [
            row.get('Scheme', ''),
            str(row.get('Folio', '')),
            _fmt_inr(row.get('Invested')),
            _fmt_inr(row.get('Value')),
            _fmt_inr(row.get('Gain/Loss')),
            _fmt_pct(
                (row.get('Gain/Loss', 0) / row.get('Invested', 1) * 100)
                if row.get('Gain/Loss') is not None and row.get('Invested', 0) > 0
                else None
            ),
        ]
        aligns = ['L', 'C', 'R', 'R', 'R', 'R']
        for v, w, a in zip(vals, s_widths, aligns):
            pdf.cell(w, row_h, v, border=1, align=a, fill=fill)
        pdf.ln()

    # Total row
    pdf.set_font('Main', 'B', 7)
    pdf.set_fill_color(200, 200, 200)
    n_folios = len(set(r.get('Folio', '') for r in summary_rows))
    total_vals = [
        'TOTAL',
        f'{n_folios} folios',
        _fmt_inr(total_invested),
        _fmt_inr(total_value),
        _fmt_inr(total_gain),
        _fmt_pct(total_gain / total_invested * 100) if total_invested > 0 else 'N/A',
    ]
    for v, w, a in zip(total_vals, s_widths, aligns):
        pdf.cell(w, row_h, v, border=1, align=a, fill=True)
    pdf.ln(8)

    # ════════════════════════════════════════
    # TRANSACTIONS — PER RTA
    # ════════════════════════════════════════
    t_headers = ['Date', 'Type', 'Units', 'Price', 'Amount', 'Balance']
    t_widths  = [22, 22, 22, 22, 28, 22]
    t_widths[-1] = PAGE_W - sum(t_widths[:-1])
    t_aligns  = ['C', 'L', 'R', 'R', 'R', 'R']

    all_entries = rta_txns.get('CAMS', []) + rta_txns.get('KFinTech', [])
    all_entries.sort(key=lambda e: e['label'])

    for entry in all_entries:
            label   = entry['label']
            opening = entry['opening']
            tdf     = entry['df']

            pdf.set_font('Main', 'B', 8)
            pdf.set_text_color(*DARK)
            pdf.cell(PAGE_W, 5,
                     f'{label}  —  Opening: {opening:.4f} units',
                     new_x="LMARGIN", new_y="NEXT")

            if tdf.empty:
                pdf.set_font('Main', '', 7)
                pdf.set_text_color(*GREY)
                pdf.cell(PAGE_W, 5, '(no transactions during this period)',
                         new_x="LMARGIN", new_y="NEXT")
                pdf.ln(3)
                continue

            # Table header
            pdf.set_font('Main', 'B', 6.5)
            pdf.set_fill_color(*BLUE)
            pdf.set_text_color(*WHITE)
            for h, w in zip(t_headers, t_widths):
                pdf.cell(w, 5, h, border=1, align='C', fill=True)
            pdf.ln()

            # Rows
            pdf.set_font('Main', '', 6.5)
            pdf.set_text_color(*DARK)
            for r_idx, (_, row) in enumerate(tdf.iterrows()):
                if r_idx % 2 == 1:
                    pdf.set_fill_color(*LIGHT)
                    fill = True
                else:
                    fill = False

                date_str = row['_date'].strftime('%Y-%m-%d') if pd.notna(row['_date']) else ''
                price = row.get('purprice', row.get('td_pop', None))
                amount = row.get('amount', 0)

                vals = [
                    date_str,
                    str(row.get('trxntype', '')),
                    _fmt_units(row.get('signed_units', 0)),
                    _fmt_units(price) if pd.notna(price) else '',
                    _fmt_inr(amount),
                    _fmt_units(row.get('Balance', 0)),
                ]
                for v, w, a in zip(vals, t_widths, t_aligns):
                    pdf.cell(w, 4.5, v, border=1, align=a, fill=fill)
                pdf.ln()

            pdf.ln(4)

    # ── Footer ──
    pdf.set_font('Main', '', 7)
    pdf.set_text_color(*GREY)
    pdf.cell(PAGE_W, 4,
             f'Report generated on {datetime.now().strftime("%d/%m/%Y %H:%M")}',
             align='C', new_x="LMARGIN", new_y="NEXT")

    return bytes(pdf.output())


def generate_valuation_html(
    client_name: str,
    pan: str,
    client_code: str,
    mobile: str,
    val_date_str: str,
    period_from_str: str,
    summary_rows: list[dict],
    rta_txns: dict,
    total_invested: float,
    total_value: float,
    total_gain: float,
    show_investor: bool = False,
) -> str:
    """Generate a self-contained HTML report (print → Save as PDF)."""
    def fi(v):
        try:
            return f"₹{float(v):,.2f}"
        except:
            return "N/A"
    def fp(v):
        try:
            return f"{float(v):.2f}%"
        except:
            return "N/A"
    def fu(v):
        try:
            return f"{float(v):.4f}"
        except:
            return "N/A"

    n_folios = len(set(r.get('Folio', '') for r in summary_rows))
    gain_cls = "positive" if total_gain >= 0 else "negative"

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Valuation Report - {client_name}</title>
<style>
  @page {{ size: A4; margin: 12mm; }}
  body {{ font-family: Arial, Helvetica, sans-serif; font-size: 10px; color: #2c3e50; margin: 0; padding: 15px; }}
  h1 {{ font-size: 17px; text-align: center; margin: 0 0 3px 0; }}
  .meta {{ text-align: center; color: #7f8c8d; font-size: 9px; margin-bottom: 12px; }}
  .metrics {{ display: flex; gap: 10px; margin-bottom: 14px; }}
  .metric-box {{ flex: 1; background: #ecf0f1; padding: 8px; border-radius: 4px; text-align: center; }}
  .metric-box .label {{ font-size: 8px; color: #7f8c8d; text-transform: uppercase; }}
  .metric-box .value {{ font-size: 14px; font-weight: bold; margin-top: 2px; }}
  .positive {{ color: #27ae60; }}
  .negative {{ color: #c0392b; }}
  h2 {{ font-size: 12px; color: #2980b9; border-bottom: 2px solid #2980b9; padding-bottom: 3px; margin: 16px 0 6px 0; }}
  h3 {{ font-size: 11px; color: #2980b9; margin: 14px 0 4px 0; }}
  .scheme-hdr {{ font-size: 9.5px; font-weight: bold; margin: 8px 0 3px 0; color: #2c3e50; }}
  .no-txn {{ font-size: 9px; color: #aaa; font-style: italic; margin-bottom: 6px; }}
  table {{ border-collapse: collapse; width: 100%; margin-bottom: 10px; font-size: 9px; table-layout: fixed; }}
  th {{ background: #2980b9; color: white; padding: 4px 5px; text-align: center; font-weight: bold; }}
  td {{ padding: 3px 5px; border: 1px solid #dcdcdc; word-wrap: break-word; overflow-wrap: break-word; white-space: normal; }}
  tr:nth-child(even) {{ background: #f7fafc; }}
  .total-row {{ font-weight: bold; background: #e0e0e0 !important; }}
  .right {{ text-align: right; }}
  .left {{ text-align: left; }}
  .center {{ text-align: center; }}
  .footer {{ text-align: center; color: #aaa; font-size: 8px; margin-top: 20px; border-top: 1px solid #eee; padding-top: 6px; }}
  @media print {{ body {{ padding: 5px; }} .no-print {{ display: none; }} }}
</style></head><body>
<h1>Portfolio Valuation Report</h1>
<div class="meta">
  {client_name} &nbsp;|&nbsp; PAN: {pan or "N/A"} &nbsp;|&nbsp; Code: {client_code} &nbsp;|&nbsp; Mobile: {mobile or "N/A"}<br>
  Period: {period_from_str} to {val_date_str}
</div>
<div class="metrics">
  <div class="metric-box"><div class="label">Total Invested</div><div class="value">{fi(total_invested)}</div></div>
  <div class="metric-box"><div class="label">Total Value</div><div class="value">{fi(total_value)}</div></div>
  <div class="metric-box"><div class="label">Gain / Loss</div><div class="value {gain_cls}">{fi(total_gain)}</div></div>
</div>
"""

    investor_th = '<th class="left" style="width:18%">Investor</th>' if show_investor else ''
    investor_w  = '18%' if show_investor else '0%'
    scheme_w    = '32%' if show_investor else '38%'
    html += f"""<h2>Scheme Summary</h2>
<table>
<tr>{investor_th}<th class="left" style="width:{scheme_w}">Scheme</th><th class="center" style="width:12%">Folio</th><th class="right" style="width:14%">Invested</th><th class="right" style="width:14%">Value</th><th class="right" style="width:14%">Gain/Loss</th><th class="right" style="width:14%">Return %</th></tr>"""

    for r in summary_rows:
        gl = r.get('Gain/Loss')
        gl_cls = "positive" if gl and gl >= 0 else ("negative" if gl and gl < 0 else "")
        ret = (
            f"{(gl / r['Invested'] * 100):.2f}%"
            if gl is not None and r.get('Invested', 0) > 0 else "N/A"
        )
        investor_td = f'<td class="left">{r.get("Investor","")}</td>' if show_investor else ''
        scheme_display = _shorten_scheme_for_summary(r.get('Scheme', ''))
        html += f"""<tr>
{investor_td}
<td class="left">{scheme_display}</td>
<td class="center">{r.get('Folio','')}</td>
<td class="right">{fi(r.get('Invested'))}</td>
<td class="right">{fi(r.get('Value'))}</td>
<td class="right {gl_cls}">{fi(gl)}</td>
<td class="right">{ret}</td></tr>"""

    total_ret = f"{(total_gain/total_invested*100):.2f}%" if total_invested > 0 else "N/A"
    total_investor_td = '<td class="left"></td>' if show_investor else ''
    html += f"""<tr class="total-row">
{total_investor_td}<td class="left">TOTAL</td><td class="center">{n_folios} folios</td>
<td class="right">{fi(total_invested)}</td><td class="right">{fi(total_value)}</td>
<td class="right {gain_cls}">{fi(total_gain)}</td><td class="right">{total_ret}</td></tr>
</table>

<h2>Transactions</h2>"""

    all_entries = rta_txns.get('CAMS', []) + rta_txns.get('KFinTech', [])
    all_entries.sort(key=lambda e: e['label'])

    for entry in all_entries:
            label   = entry['label']
            opening = entry['opening']
            tdf     = entry['df']
            html += f'<div class="scheme-hdr">{label} &mdash; Opening: {opening:.4f} units</div>'

            if tdf.empty:
                html += '<div class="no-txn">(no transactions during this period)</div>'
                continue

            html += """<table>
<tr><th class="center">Date</th><th class="left">Type</th><th class="right">Units</th><th class="right">Price</th><th class="right">Amount</th><th class="right">Balance</th></tr>"""

            for _, row in tdf.iterrows():
                d = row['_date'].strftime('%Y-%m-%d') if pd.notna(row['_date']) else ''
                price = row.get('purprice', None)
                if price is None:
                    price = row.get('td_pop', None)
                html += f"""<tr>
<td class="center">{d}</td>
<td class="left">{row.get('trxntype','')}</td>
<td class="right">{fu(row.get('signed_units',0))}</td>
<td class="right">{fu(price) if pd.notna(price) else ''}</td>
<td class="right">{fi(row.get('amount',0))}</td>
<td class="right">{fu(row.get('Balance',0))}</td></tr>"""

            html += "</table>"

    html += f"""<div class="footer">Report generated on {datetime.now().strftime("%d/%m/%Y %H:%M")}</div>
</body></html>"""
    return html


def generate_valuation_pdf(
    client_name: str,
    pan: str,
    client_code: str,
    mobile: str,
    val_date_str: str,
    period_from_str: str,
    summary_rows: list[dict],
    rta_txns: dict,
    total_invested: float,
    total_value: float,
    total_gain: float,
    show_investor: bool = False,
) -> bytes | None:
    """
    Generate a PDF bytes buffer for the valuation report.
    Returns None if fpdf2 is not installed or no font found.
    """
    try:
        from fpdf import FPDF
    except ImportError:
        return None

    reg_path, bold_path = _find_font_path()
    if not reg_path:
        return None

    pdf = FPDF(orientation='P', unit='mm', format='A4')
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_font('Main', '', reg_path, uni=True)
    pdf.add_font('Main', 'B', bold_path, uni=True)
    pdf.set_margins(12, 12, 12)

    # ── Colour palette ──
    BLUE   = (41, 128, 185)
    DARK   = (44, 62, 80)
    GREY   = (127, 140, 141)
    LIGHT  = (236, 240, 241)
    WHITE  = (255, 255, 255)
    GREEN  = (39, 174, 96)
    RED    = (192, 57, 43)
    PAGE_W = 210 - 24  # A4 width minus margins

    def _fmt_inr(val) -> str:
        try:
            return f"\u20b9{float(val):,.2f}"
        except (TypeError, ValueError):
            return "N/A"

    def _fmt_pct(val) -> str:
        try:
            return f"{float(val):.2f}%"
        except (TypeError, ValueError):
            return "N/A"

    def _fmt_units(val) -> str:
        try:
            return f"{float(val):.4f}"
        except (TypeError, ValueError):
            return "N/A"

    def _truncate_for_cell(text: str, col_width_mm: float, font_size_pt: float = 7) -> str:
        """Truncate text with ellipsis so it never exceeds the cell width in fpdf2."""
        if not text:
            return ""
        # Approx chars per mm at 7pt (~0.55 mm/char for typical TTF fonts)
        max_chars = int(col_width_mm / 0.55)
        if len(text) > max_chars:
            return text[: max_chars - 1].strip() + "…"
        return text

    # ════════════════════════════════════════
    # PAGE 1 — HEADER + SUMMARY TABLE
    # ════════════════════════════════════════
    pdf.add_page()

    # Title
    pdf.set_font('Main', 'B', 16)
    pdf.set_text_color(*DARK)
    pdf.cell(PAGE_W, 10, 'Portfolio Valuation Report', align='C', new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)

    # Client info
    pdf.set_font('Main', '', 9)
    pdf.set_text_color(*GREY)
    pdf.cell(PAGE_W, 5, f'Client: {client_name}', new_x="LMARGIN", new_y="NEXT")
    pdf.cell(PAGE_W, 5,
             f'PAN: {pan or "N/A"}   |   Code: {client_code}   |   Mobile: {mobile or "N/A"}',
             new_x="LMARGIN", new_y="NEXT")
    pdf.cell(PAGE_W, 5,
             f'Period: {period_from_str} to {val_date_str}',
             new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    # Metrics bar
    pdf.set_fill_color(*LIGHT)
    col_w = PAGE_W / 3
    pdf.set_font('Main', 'B', 8)
    pdf.set_text_color(*GREY)
    pdf.cell(col_w, 5, 'Total Invested', border=0, fill=True, new_x="RIGHT")
    pdf.cell(col_w, 5, 'Total Value', border=0, fill=True, new_x="RIGHT")
    pdf.cell(col_w, 5, 'Gain / Loss', border=0, fill=True, new_x="LMARGIN", new_y="NEXT")

    pdf.set_font('Main', 'B', 11)
    pdf.set_text_color(*DARK)
    pdf.cell(col_w, 7, _fmt_inr(total_invested), border=0, fill=True, new_x="RIGHT")
    pdf.cell(col_w, 7, _fmt_inr(total_value), border=0, fill=True, new_x="RIGHT")
    gain_color = GREEN if total_gain >= 0 else RED
    pdf.set_text_color(*gain_color)
    pdf.cell(col_w, 7, _fmt_inr(total_gain), border=0, fill=True, new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(*DARK)
    pdf.ln(6)

    # Section header
    pdf.set_font('Main', 'B', 11)
    pdf.set_text_color(*BLUE)
    pdf.cell(PAGE_W, 6, 'Scheme Summary', new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    # Summary table
    if show_investor:
        s_headers = ['Investor', 'Scheme', 'Folio', 'Invested', 'Value', 'Gain/Loss', 'Return %']
        s_widths  = [30, 40, 16, 24, 24, 24, 28]  # sums to 186 = PAGE_W
        scheme_col_idx = 1
    else:
        s_headers = ['Scheme', 'Folio', 'Invested', 'Value', 'Gain/Loss', 'Return %']
        s_widths  = [55, 20, 28, 28, 28, 27]  # sums to 186 = PAGE_W
        scheme_col_idx = 0
    # Adjust last col to fill (kept for safety if PAGE_W/headers change later)
    s_widths[-1] = PAGE_W - sum(s_widths[:-1])

    row_h = 6
    pdf.set_font('Main', 'B', 7)
    pdf.set_fill_color(*BLUE)
    pdf.set_text_color(*WHITE)
    for h, w in zip(s_headers, s_widths):
        pdf.cell(w, row_h, h, border=1, align='C', fill=True)
    pdf.ln()

    pdf.set_font('Main', '', 7)
    pdf.set_text_color(*DARK)
    aligns = ['L', 'L', 'C', 'R', 'R', 'R', 'R'] if show_investor else ['L', 'C', 'R', 'R', 'R', 'R']

    for i, row in enumerate(summary_rows):
        if i % 2 == 1:
            pdf.set_fill_color(*LIGHT)
            fill = True
        else:
            fill = False

        short_scheme = _shorten_scheme_for_summary(str(row.get('Scheme', '')))
        if show_investor:
            scheme_text = _truncate_for_cell(short_scheme, s_widths[scheme_col_idx], 7)
            vals = [str(row.get('Investor', '')), scheme_text, str(row.get('Folio', ''))]
        else:
            scheme_text = _truncate_for_cell(short_scheme, s_widths[scheme_col_idx], 7)
            vals = [scheme_text, str(row.get('Folio', ''))]
        vals += [
            _fmt_inr(row.get('Invested')),
            _fmt_inr(row.get('Value')),
            _fmt_inr(row.get('Gain/Loss')),
            _fmt_pct(
                (row.get('Gain/Loss', 0) / row.get('Invested', 1) * 100)
                if row.get('Gain/Loss') is not None and row.get('Invested', 0) > 0
                else None
            ),
        ]
        for v, w, a in zip(vals, s_widths, aligns):
            pdf.cell(w, row_h, v, border=1, align=a, fill=fill)
        pdf.ln()

    # Total row
    pdf.set_font('Main', 'B', 7)
    pdf.set_fill_color(200, 200, 200)
    n_folios = len(set(r.get('Folio', '') for r in summary_rows))
    if show_investor:
        total_vals = ['', 'TOTAL', f'{n_folios} folios']
    else:
        total_vals = ['TOTAL', f'{n_folios} folios']
    total_vals += [
        _fmt_inr(total_invested),
        _fmt_inr(total_value),
        _fmt_inr(total_gain),
        _fmt_pct(total_gain / total_invested * 100) if total_invested > 0 else 'N/A',
    ]
    for v, w, a in zip(total_vals, s_widths, aligns):
        pdf.cell(w, row_h, v, border=1, align=a, fill=True)
    pdf.ln(8)

    # ════════════════════════════════════════
    # TRANSACTIONS — PER RTA
    # ════════════════════════════════════════
    t_headers = ['Date', 'Type', 'Units', 'Price', 'Amount', 'Balance']
    t_widths  = [22, 22, 22, 22, 28, 22]
    t_widths[-1] = PAGE_W - sum(t_widths[:-1])
    t_aligns  = ['C', 'L', 'R', 'R', 'R', 'R']

    all_entries = rta_txns.get('CAMS', []) + rta_txns.get('KFinTech', [])
    all_entries.sort(key=lambda e: e['label'])

    for entry in all_entries:
            label   = entry['label']
            opening = entry['opening']
            tdf     = entry['df']

            pdf.set_font('Main', 'B', 8)
            pdf.set_text_color(*DARK)
            pdf.cell(PAGE_W, 5,
                     f'{label}  —  Opening: {opening:.4f} units',
                     new_x="LMARGIN", new_y="NEXT")

            if tdf.empty:
                pdf.set_font('Main', '', 7)
                pdf.set_text_color(*GREY)
                pdf.cell(PAGE_W, 5, '(no transactions during this period)',
                         new_x="LMARGIN", new_y="NEXT")
                pdf.ln(3)
                continue

            # Table header
            pdf.set_font('Main', 'B', 6.5)
            pdf.set_fill_color(*BLUE)
            pdf.set_text_color(*WHITE)
            for h, w in zip(t_headers, t_widths):
                pdf.cell(w, 5, h, border=1, align='C', fill=True)
            pdf.ln()

            # Rows
            pdf.set_font('Main', '', 6.5)
            pdf.set_text_color(*DARK)
            for r_idx, (_, row) in enumerate(tdf.iterrows()):
                if r_idx % 2 == 1:
                    pdf.set_fill_color(*LIGHT)
                    fill = True
                else:
                    fill = False

                date_str = row['_date'].strftime('%Y-%m-%d') if pd.notna(row['_date']) else ''
                price = row.get('purprice', row.get('td_pop', None))
                amount = row.get('amount', 0)

                vals = [
                    date_str,
                    str(row.get('trxntype', '')),
                    _fmt_units(row.get('signed_units', 0)),
                    _fmt_units(price) if pd.notna(price) else '',
                    _fmt_inr(amount),
                    _fmt_units(row.get('Balance', 0)),
                ]
                for v, w, a in zip(vals, t_widths, t_aligns):
                    pdf.cell(w, 4.5, v, border=1, align=a, fill=fill)
                pdf.ln()

            pdf.ln(4)

    # ── Footer ──
    pdf.set_font('Main', '', 7)
    pdf.set_text_color(*GREY)
    pdf.cell(PAGE_W, 4,
             f'Report generated on {datetime.now().strftime("%d/%m/%Y %H:%M")}',
             align='C', new_x="LMARGIN", new_y="NEXT")

    return bytes(pdf.output())



def generate_email_body(client_name: str, report_type: str) -> str:
    """Professional HTML email wrapper — used for both Capital Gain & Valuation emails."""
    greeting = f"Dear {client_name},"
    
    if report_type == "Capital Gain":
        intro = f"""
<p>We are pleased to share your <strong>Capital Gain Report</strong> for review. This report details all realized 
capital gains/losses from your mutual fund investments, calculated using the FIFO (First In First Out) method.</p>

<p><strong>Report Highlights:</strong></p>
<ul>
    <li>Summary of all buy and sell transactions</li>
    <li>Cost basis and realized gains/losses per transaction</li>
    <li>Useful for income tax filing and investment tracking</li>
</ul>
"""
    else:  # Valuation
        intro = f"""
<p>We are pleased to share your <strong>Portfolio Valuation Report</strong> for review. This report provides a 
comprehensive snapshot of your mutual fund holdings, current valuations, and investment performance as of the 
valuation date mentioned in the report.</p>

<p><strong>Report Highlights:</strong></p>
<ul>
    <li>Scheme-wise investment summary and current NAV-based valuations</li>
    <li>Gain/Loss analysis across all holdings</li>
    <li>Detailed transaction history for each holding</li>
</ul>
"""
    
    return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <style>
        body {{
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            line-height: 1.6;
            color: #333;
            margin: 0;
            padding: 20px;
            background-color: #f9f9f9;
        }}
        .email-container {{
            max-width: 700px;
            margin: 0 auto;
            background: white;
            padding: 30px;
            border-radius: 8px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        }}
        .header {{
            border-bottom: 3px solid #2980b9;
            padding-bottom: 15px;
            margin-bottom: 20px;
        }}
        .header h2 {{
            color: #2980b9;
            margin: 0;
            font-size: 24px;
        }}
        .greeting {{
            font-size: 16px;
            color: #2c3e50;
            margin-bottom: 15px;
        }}
        .intro-section {{
            background: #ecf0f1;
            padding: 15px;
            border-left: 4px solid #2980b9;
            margin: 20px 0;
            border-radius: 4px;
        }}
        .intro-section p {{
            margin: 10px 0;
            color: #2c3e50;
            font-size: 14px;
        }}
        .intro-section ul {{
            margin: 10px 0;
            padding-left: 20px;
            color: #2c3e50;
            font-size: 14px;
        }}
        .intro-section li {{
            margin: 8px 0;
        }}
        .attachment-note {{
            background: #d5f4e6;
            border-left: 4px solid #27ae60;
            padding: 15px;
            margin: 25px 0;
            border-radius: 4px;
        }}
        .attachment-note strong {{
            color: #27ae60;
        }}
        .feedback-section {{
            background: #fff3cd;
            border-left: 4px solid #f39c12;
            padding: 15px;
            margin: 25px 0;
            border-radius: 4px;
        }}
        .feedback-section h3 {{
            color: #e67e22;
            margin-top: 0;
            font-size: 16px;
        }}
        .feedback-section p {{
            margin: 8px 0;
            color: #7d6608;
            font-size: 14px;
        }}
        .footer {{
            border-top: 1px solid #ddd;
            padding-top: 20px;
            margin-top: 30px;
            color: #7f8c8d;
            font-size: 12px;
        }}
        .signature {{
            margin-top: 20px;
            color: #2c3e50;
        }}
        .report-attached {{
            color: #27ae60;
            font-weight: bold;
        }}
    </style>
</head>
<body>
    <div class="email-container">
        <div class="header">
            <h2>📊 {report_type} Report</h2>
        </div>
        
        <div class="greeting">
            {greeting}
        </div>
        
        <div class="intro-section">
            {intro}
        </div>
        
        <div class="attachment-note">
            <strong>✓ Report Attached</strong><br>
            Your <strong>{report_type} Report</strong> is attached below as a PDF file. 
            You can download, print, or save it for your records.
        </div>
        
        <div class="feedback-section">
            <h3>⏰ Action Required</h3>
            <p>
                Please review the attached report carefully. If you notice any discrepancies, 
                errors in transaction details, or have any questions, <strong>please revert within 24-48 hours</strong>.
            </p>
            <p>
                This will help us ensure accuracy in your portfolio records and make any necessary corrections 
                at the earliest.
            </p>
        </div>
        
        <p style="color: #2c3e50; margin: 20px 0;">
            <strong>What to Look For:</strong>
        </p>
        <ul style="color: #2c3e50; margin: 10px 0;">
            <li>Verify all folio numbers and scheme names</li>
            <li>Check transaction dates and amounts</li>
            <li>Confirm NAV values and current holdings</li>
            <li>Review valuation dates and calculation methods</li>
        </ul>
        
        <div class="footer">
            <p>
                <strong>Contact Information:</strong><br>
                If you have any questions or need clarification on any aspect of this report, 
                please don't hesitate to reach out to us.
            </p>
            <p>
                <strong>Report Generated:</strong> {datetime.now().strftime("%d %B %Y at %I:%M %p")}<br>
                This is an automated report. For support, contact our team.
            </p>
            <p style="margin-top: 20px; color: #34495e;">
                Thank you for your trust in our services.
            </p>
            <div class="signature">
                <strong>Best Regards,</strong><br>
                Portfolio Intelligence Team<br>
                <em>Your Investment Partner</em>
            </div>
        </div>
    </div>
</body>
</html>"""

# ==================== Cams, Karvy and Manual entir Brokerage Data HELPERS ====================
def _resolve_amc_via_isin(get_conn, scheme_code_col_sql: str, table: str, scheme_code_value_alias: str):
    """
    Not used directly — kept as documentation of the join shape.
    Actual resolution happens inline in the loader below via a single
    bse_scheme_master join, exactly like get_all_folios_with_isin_and_nav().
    """
    pass


@st.cache_data(show_spinner=False)
def load_brokerage_report(_get_conn, _v: int) -> dict:
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


@st.cache_data(show_spinner=False)
def load_dedup_sip_counts(_v: int) -> dict:
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


# ══════════════════════════════════════════════════════════════
# EMAIL REPORT BUTTON UI COMPONENT
# ══════════════════════════════════════════════════════════════
def render_email_report_button(
    client_code: str,
    client_name: str,
    report_type: str,  # "Capital Gain" or "Valuation"
    fy_str: str = None,
    html_content: str = None,
    pdf_content: bytes = None,
    key_prefix: str = "email",
):
    """
    Render an 'Email Report' button with recipient input and status display.
    
    Returns:
        True if email was triggered, False otherwise
    """
    
    # Get client email
    client_email = mail_sync.get_client_email(client_code)
    creds_configured = mail_sync.credentials_configured()
    
    # Check if Gmail is configured
    if not creds_configured:
        st.caption("📧 *Gmail not configured — set up in Admin > Mailback Sync to enable emailing*")
        return False
    
    with st.expander("📧 Email This Report", expanded=False):
        col1, col2 = st.columns([3, 1])
        
        with col1:
                recipient_email = st.text_input(
                "Recipient Email",
                value=client_email or "",
                placeholder="client@example.com",
                key=f"{key_prefix}_recipient_{client_code}"
            )
        with col2:
            st.markdown("<div style='height: 28px'></div>", unsafe_allow_html=True)
            send_btn = st.button(
                "📤 Send",
                type="primary",
                width="stretch",
                key=f"{key_prefix}_send_btn_{client_code}",
                disabled=not recipient_email or not pdf_content
            )

        # Optional CC
        cc_input = st.text_input(
            "CC (optional, comma-separated)",
            placeholder="advisor@example.com, partner@example.com",
            key=f"{key_prefix}_cc_{client_code}"
        )

    
        cc_list = [e.strip() for e in cc_input.split(",") if e.strip()] if cc_input else None
        
        # Status display
    
        email_status = mail_sync.get_email_status()
        if email_status["sending"]:
            st.info("⏳ Sending email...")
        elif email_status["done"]:
            if email_status["ok"]:
                st.success(email_status["msg"])
            else:
                st.error(email_status["msg"])
        
        if send_btn and recipient_email and pdf_content:
            safe_name = client_name.replace(" ", "_").replace("/", "-")[:30]
            if report_type == "Capital Gain" and fy_str:
                filename = f"Capital_Gain_{safe_name}_FY{fy_str}.pdf"
                subject = f"Capital Gain Report - FY {fy_str} - {client_name}"
            else:
                filename = f"Valuation_{safe_name}_{datetime.now().strftime('%Y%m%d')}.pdf"
                subject = f"Portfolio Valuation Report - {client_name} - {datetime.now().strftime('%d/%m/%Y')}"

            with st.spinner("Sending email..."):
                success, msg = mail_sync.send_report_email(
                    to_email=recipient_email,
                    subject=subject,
                    html_body=generate_email_body(client_name, report_type),
                    pdf_bytes=pdf_content,
                    pdf_filename=filename,
                    cc_emails=cc_list,
                )

            if success:
                st.success(msg)
            else:
                st.error(msg)
            return True
    
    return False
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

        # AMFI added Plan/Option as separate columns (8-col format, Aug 2026+).
        # Old: Code;ISIN1;ISIN2;Name;NAV;Date (6 cols)
        # New: Code;ISIN1;ISIN2;Name;Plan;Option;NAV;Date (8 cols)
        if len(parts) >= 8:
            nav_str = parts[6].strip()
            date_str = parts[7].strip()
        else:
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
                date_str = parts[7].strip() if len(parts) >= 8 else parts[5].strip()
                try:
                    return datetime.strptime(date_str, "%d-%b-%Y").strftime("%Y-%m-%d")
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
        Scheme Code;NAV Name;Plan;Option;ISIN Div Payout/ISIN Growth;
        ISIN Div Reinvestment;Net Asset Value;Date
    into the SAME 6-column layout as the live NAVAll.txt file:
        Scheme Code;ISIN Payout;ISIN Reinvest;Scheme Name;NAV;Date

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
            if parts and parts[0].strip() == "Scheme Code":
                out_lines.append(
                    "Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;"
                    "Scheme Name;Net Asset Value;Date"
                )
            else:
                out_lines.append(line)
            continue
        scheme_code, scheme_name, _plan, _option, isin_payout, isin_reinvest, nav, date_field = parts[:8]
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

@st.cache_data(show_spinner=False)
def get_all_folios_with_isin_and_nav(_get_conn, _v: int, force_reload: bool = False) -> pd.DataFrame:
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

    NOTE: param is _get_conn (leading underscore) so Streamlit's cache
    decorator skips hashing it — get_conn is a function object and can't
    be hashed. All internal DB calls must use _get_conn(), not get_conn().
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
    bse_amc_col = _get_bse_amc_column(_get_conn)
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
    with _get_conn() as conn:
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



@st.cache_data(show_spinner=False)
def get_folio_nav_summary(_get_conn, _v: int, force_reload: bool = False) -> dict:
    """Quick stats for Streamlit metrics."""
    log.info("[NAV-FLOW] Generating folio NAV summary...")

    df = get_all_folios_with_isin_and_nav(_get_conn, _v, force_reload=force_reload)
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

def load_amc_breakdown_by_isin(get_conn, _v) -> pd.DataFrame:

    """AMC-wise AUM + folio breakdown, grouped by canonical AMFI AMC name (via ISIN)."""
    df = get_all_folios_with_isin_and_nav(get_conn, _v)
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


@st.cache_data(show_spinner=False)
def load_active_amcs(_v: int) -> list:
    """AMCs you currently have business with (from folio holdings, not brokerage files)."""
    df = get_all_folios_with_isin_and_nav(get_conn, _v)
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
@st.cache_data(show_spinner=False)
def get_kfin_invested_amount(folio_list, _v: int):
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


@st.cache_data(show_spinner=False)
def get_kfin_invested_per_scheme(folio_list: list, _v: int) -> pd.DataFrame:
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


@st.cache_data(show_spinner=False)
def get_cams_invested_per_scheme(folio_list: list, _v: int) -> pd.DataFrame:
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


# ==================== CONSOLIDATED CLIENT LOADER ====================
@st.cache_data
def load_all_clients_with_display(_v: int) -> pd.DataFrame:
    """Single source of truth for client search across all tabs."""
    with get_conn() as conn:
        return pd.read_sql("""
            SELECT 
                client_code,
                primary_holder_first_name || ' ' || primary_holder_last_name AS name,
                primary_holder_pan AS pan,
                guardian_pan,
                indian_mobile_no AS mobile,
                email,
                city
            FROM bse_client_master
        """, conn)

def render_client_selector(key_prefix: str, exclude_minors: bool = False) -> tuple:
    """Reusable client selector UI. Returns (client_code, client_row)."""
    clients_df = load_all_clients_with_display(data_version())
    
    if clients_df.empty:
        st.warning("No clients found.")
        return None, None
    
    if exclude_minors:
        clients_df = clients_df[clients_df['pan'].notna() & (clients_df['pan'].str.strip() != '')]
    
    clients_df['display'] = clients_df.apply(
        lambda r: f"{r['name']} | PAN: {r['pan'] or 'Minor'} | {r['client_code']}",
        axis=1
    )
    
    selected = st.selectbox(
        "🔍 Select Client",
        clients_df['display'].tolist(),
        index=None,
        placeholder="Type to search...",
        key=f"{key_prefix}_client_select"
    )
    
    if not selected:
        return None, None
    
    return selected, clients_df[clients_df['display'] == selected].iloc[0]


# ==================== DATA LOADERS ====================
@st.cache_data(show_spinner=False)
def load_table_summary(table: str, _v: int) -> pd.DataFrame:
    with get_conn() as conn:
        return pd.read_sql(f"SELECT * FROM {table} LIMIT 1000", conn)


@st.cache_data(show_spinner=False)
def load_db_stats(_v: int) -> dict:
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

@st.cache_data(show_spinner=False)
def load_dashboard_summary(_v: int) -> dict:
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


@st.cache_data(show_spinner=False)
def load_amc_breakdown(_v: int) -> pd.DataFrame:
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

@st.cache_data(show_spinner=False)
def load_recent_uploads(_v: int, limit: int = 10) -> pd.DataFrame:
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


# # ==================== CAMS MAILBACK AUTO-SYNC ====================
# def _auto_cams_mailback_sync():
#     """Non-blocking: starts background IMAP sync if needed, once per day."""
#     import cams_mailback_sync as cms

#     if not st.session_state.get("cams_mailback_auto_toggle", True):
#         return

#     last_run = st.session_state.get("cams_mailback_auto_last_run")
#     today = datetime.now().strftime("%Y-%m-%d")
#     if last_run == today:
#         return

#     st.session_state["cams_mailback_auto_last_run"] = today

#     status = cms.get_sync_status()
#     if status["running"]:
#         st.info("⏳ Syncing in background...")
#     elif status["done"]:
#         st.success(status["msg"]) if status["ok"] else st.error(status["msg"])


# _auto_cams_mailback_sync()
# cams_mailback_sync.ensure_poller_started()
# nav_scheduler.ensure_started(get_conn, download_and_save_nav_if_needed, _amfi.load)

cams_mailback_sync.ensure_poller_started()
nav_scheduler.ensure_started(get_conn, download_and_save_nav_if_needed, _amfi.load)


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

    nav_options = ["📊 Dashboard", "👥 Clients", "📋 Transactions", "💰 Brokerage Report", "📊 Reports", "🧮 Capital Gains", "⚙️ Admin Panel"]

    if "nav_mode" not in st.session_state or st.session_state["nav_mode"] not in nav_options:
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
                sync_previous_business_day_nav_if_needed()

                folio_nav_df = get_all_folios_with_isin_and_nav(get_conn, data_version())

                # ── Normalize product_code once for all merges ──
                folio_nav_df['product_code_norm'] = folio_nav_df['product_code'].astype(str).str.strip().str.upper()

                # ═══════════════════════════════════════════════════════════
                # CAMS: overwrite file_aum & units from transaction sums
                # ═══════════════════════════════════════════════════════════
                cams_folios_all = folio_nav_df[folio_nav_df['rta'] == 'CAMS']['folio_id'].unique().tolist()
                if cams_folios_all:
                    cams_invested_all = get_cams_invested_per_scheme(cams_folios_all, data_version())
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
                    kfin_invested_all = get_kfin_invested_per_scheme(kfin_folios_all, data_version())
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

                nav_stats = get_folio_nav_summary(get_conn, data_version())
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
    base_summary = load_dashboard_summary(data_version())
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
    dedup_sips = load_dedup_sip_counts(data_version())
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


        # ── 1-DAY & 1-WEEK DIFFERENCE SECTION ──
    st.divider()
    st.subheader("📊 Portfolio Movement — Day & Week Diff")

    if "folio_nav_df" in st.session_state:
        df_nav = st.session_state["folio_nav_df"].copy()
        df_nav["investor_name"] = df_nav["investor_name"].str.upper().str.strip()
        
        # Load previous day and week NAV data
        prev_nav_map = load_previous_nav_map()
        prev_nav_date = get_previous_nav_date()
        
        if not prev_nav_map:
            st.warning("⚠️ Previous NAV snapshot not available for 1-day diff. Use Admin → Sync Previous Business Day NAV.")
        else:
            # Calculate 1-day diff
            df_nav["prev_nav_1d"] = df_nav["isin"].apply(
                lambda i: prev_nav_map.get(str(i).strip().upper()) if pd.notna(i) else None
            )
            df_nav["diff_1d_value"] = (
                (df_nav["current_nav"] - df_nav["prev_nav_1d"]) * df_nav["units"]
            ).fillna(0.0)
            df_nav["diff_1d_pct"] = (
                (df_nav["current_nav"] - df_nav["prev_nav_1d"]) / df_nav["prev_nav_1d"] * 100
            ).fillna(0.0)
            
            # Summary stats
            total_1d_diff = df_nav["diff_1d_value"].sum()
            top_gainers_1d = df_nav[df_nav["diff_1d_value"] > 0].nlargest(5, "diff_1d_value")[
                ["folio_id", "scheme_name", "current_nav", "prev_nav_1d", "units", "diff_1d_value", "rta"]
            ].copy()
            top_losers_1d = df_nav[df_nav["diff_1d_value"] < 0].nsmallest(5, "diff_1d_value")[
                ["folio_id", "scheme_name", "current_nav", "prev_nav_1d", "units", "diff_1d_value", "rta"]
            ].copy()
            
            # Display metrics
            m1d1, m1d2, m1d3 = st.columns(3)
            m1d1.metric("📈 1-Day Portfolio Change", format_aum(total_1d_diff),
                    delta=f"{(total_1d_diff / summary.get('total_aum', 1) * 100):.2f}%" if summary.get('total_aum', 0) > 0 else None)
            m1d2.metric("📅 Comparison Date", prev_nav_date or "Unknown")
            m1d3.metric("✅ Folios with NAV data", f"{(df_nav['prev_nav_1d'].notna()).sum()} / {len(df_nav)}")
            
            # Top movers
            col1d_gainers, col1d_losers = st.columns(2)
            
            with col1d_gainers:
                st.markdown("#### 🟢 Top Gainers (1D)")
                if not top_gainers_1d.empty:
                    display_gainers = top_gainers_1d.rename(columns={
                        "folio_id": "Folio", "scheme_name": "Scheme", "current_nav": "Current NAV",
                        "prev_nav_1d": "Prev NAV", "units": "Units", "diff_1d_value": "Gain (₹)", "rta": "RTA"
                    })
                    st.dataframe(
                        display_gainers,
                        width="stretch",
                        hide_index=True,
                        column_config={
                            "Current NAV": st.column_config.NumberColumn(format="₹ %.4f"),
                            "Prev NAV": st.column_config.NumberColumn(format="₹ %.4f"),
                            "Units": st.column_config.NumberColumn(format="%.4f"),
                            "Gain (₹)": st.column_config.NumberColumn(format="₹ %.2f"),
                        }
                    )
                else:
                    st.info("No gainers today")
            
            with col1d_losers:
                st.markdown("#### 🔴 Top Losers (1D)")
                if not top_losers_1d.empty:
                    display_losers = top_losers_1d.rename(columns={
                        "folio_id": "Folio", "scheme_name": "Scheme", "current_nav": "Current NAV",
                        "prev_nav_1d": "Prev NAV", "units": "Units", "diff_1d_value": "Loss (₹)", "rta": "RTA"
                    })
                    st.dataframe(
                        display_losers,
                        width="stretch",
                        hide_index=True,
                        column_config={
                            "Current NAV": st.column_config.NumberColumn(format="₹ %.4f"),
                            "Prev NAV": st.column_config.NumberColumn(format="₹ %.4f"),
                            "Units": st.column_config.NumberColumn(format="%.4f"),
                            "Loss (₹)": st.column_config.NumberColumn(format="₹ %.2f"),
                        }
                    )
                else:
                    st.info("No losers today")
            

            # 1-week diff (if available)
            st.divider()
            st.markdown("### 📊 1-Week Movement")
                  
        
            # Convert nav_date to datetime properly
            if not df_nav["nav_date"].empty:
                current_date_str = df_nav["nav_date"].iloc[0]
                current_date = pd.to_datetime(current_date_str).to_pydatetime().date()
            else:
                current_date = dt.now().date()
            
            seven_days_ago = current_date - timedelta(days=7)
            seven_days_ago_iso = seven_days_ago.strftime("%Y-%m-%d")
            
            # Use their existing function - it handles local cache + AMFI fetch
            nav_1wk_map = get_or_fetch_nav_for_date(seven_days_ago_iso)
            
            if nav_1wk_map:
                df_nav["prev_nav_1w"] = df_nav["isin"].apply(
                    lambda i: nav_1wk_map.get(str(i).strip().upper()) if pd.notna(i) else None
                )
                df_nav["diff_1w_value"] = (
                    (df_nav["current_nav"] - df_nav["prev_nav_1w"]) * df_nav["units"]
                ).fillna(0.0)
                
                total_1w_diff = df_nav["diff_1w_value"].sum()
                top_gainers_1w = df_nav[df_nav["diff_1w_value"] > 0].nlargest(5, "diff_1w_value")[
                    ["folio_id", "scheme_name", "current_nav", "prev_nav_1w", "units", "diff_1w_value", "rta"]
                ].copy()
                top_losers_1w = df_nav[df_nav["diff_1w_value"] < 0].nsmallest(5, "diff_1w_value")[
                    ["folio_id", "scheme_name", "current_nav", "prev_nav_1w", "units", "diff_1w_value", "rta"]
                ].copy()
                
                m1w1, m1w2, m1w3 = st.columns(3)
                m1w1.metric("📈 1-Week Portfolio Change", format_aum(total_1w_diff))
                m1w2.metric("📅 Comparison Date", seven_days_ago.strftime("%d %b %Y"))
                m1w3.metric("✅ Schemes with data", f"{(df_nav['prev_nav_1w'].notna()).sum()} / {len(df_nav)}")
                
                col1w_gainers, col1w_losers = st.columns(2)
                
                with col1w_gainers:
                    st.markdown("#### 🟢 Top Gainers (1W)")
                    if not top_gainers_1w.empty:
                        display_gainers_1w = top_gainers_1w.rename(columns={
                            "folio_id": "Folio", "scheme_name": "Scheme", "current_nav": "Current NAV",
                            "prev_nav_1w": "1W Ago", "units": "Units", "diff_1w_value": "Gain (₹)", "rta": "RTA"
                        })
                        st.dataframe(display_gainers_1w, width="stretch", hide_index=True,
                            column_config={"Current NAV": st.column_config.NumberColumn(format="₹ %.4f"),
                                        "1W Ago": st.column_config.NumberColumn(format="₹ %.4f"),
                                        "Gain (₹)": st.column_config.NumberColumn(format="₹ %.2f")})
                    else:
                        st.info("No gainers this week")
                
                with col1w_losers:
                    st.markdown("#### 🔴 Top Losers (1W)")
                    if not top_losers_1w.empty:
                        display_losers_1w = top_losers_1w.rename(columns={
                            "folio_id": "Folio", "scheme_name": "Scheme", "current_nav": "Current NAV",
                            "prev_nav_1w": "1W Ago", "units": "Units", "diff_1w_value": "Loss (₹)", "rta": "RTA"
                        })
                        st.dataframe(display_losers_1w, width="stretch", hide_index=True,
                            column_config={"Current NAV": st.column_config.NumberColumn(format="₹ %.4f"),
                                        "1W Ago": st.column_config.NumberColumn(format="₹ %.4f"),
                                        "Loss (₹)": st.column_config.NumberColumn(format="₹ %.2f")})
                    else:
                        st.info("No losers this week")
            else:
                st.info(f"⚠️ No NAV data available for {seven_days_ago.strftime('%d %b %Y')}")

        # ── CLIENT-WISE DRILLDOWN ──
        st.divider()
        st.markdown("### 👥 Client-wise Performance")

        if "dw_selected_client" not in st.session_state:
            st.session_state["dw_selected_client"] = None

        # LEVEL 1: All clients with invested, current, 1D & 1W diffs
        if st.session_state["dw_selected_client"] is None:
            df_nav["investor_name"] = df_nav["investor_name"].str.upper().str.strip()

            client_perf = (
                df_nav.groupby("investor_name", dropna=False)  # ← Use investor_name (already normalized)
                .agg(
                    invested=("file_aum", "sum"),
                    current=("nav_based_aum", "sum"),
                    diff_1d=("diff_1d_value", "sum"),
                    diff_1w=("diff_1w_value", "sum") if "diff_1w_value" in df_nav.columns else (lambda x: 0),
                )
                .reset_index()
                .sort_values("current", ascending=False)
            )
            
            client_perf = client_perf[client_perf["investor_name"].notna()]
            
            client_disp = client_perf.rename(columns={
                "investor_name": "Client Name",
                "invested": "Invested",
                "current": "Current",
                "diff_1d": "1D Diff",
                "diff_1w": "1W Diff"
            })
            
            sel_client = st.dataframe(
                client_disp,
                width="stretch",
                hide_index=True,
                on_select="rerun",
                selection_mode="single-row",
                key="dw_client_table",
                column_config={
                    "Invested": st.column_config.NumberColumn(format="₹ %.2f"),
                    "Current": st.column_config.NumberColumn(format="₹ %.2f"),
                    "1D Diff": st.column_config.NumberColumn(format="₹ %.2f"),
                    "1W Diff": st.column_config.NumberColumn(format="₹ %.2f"),
                }
            )
            
            if sel_client and len(sel_client["selection"]["rows"]) > 0:
                idx = sel_client["selection"]["rows"][0]
                st.session_state["dw_selected_client"] = client_disp.iloc[idx]["Client Name"]
                st.rerun()

        # LEVEL 2: Scheme-wise breakdown for selected client
        else:
            sel_client_name = st.session_state["dw_selected_client"]
            
            if st.button("⬅️ Back to Clients"):
                st.session_state["dw_selected_client"] = None
                st.rerun()
            
            st.divider()
            st.markdown(f"### {sel_client_name} — Schemes")
            

            client_schemes = df_nav[df_nav["investor_name"] == st.session_state["dw_selected_client"]].copy()
            
            scheme_perf = (
                client_schemes.groupby("scheme_name", dropna=False)
                .agg(
                    invested=("file_aum", "sum"),
                    current=("nav_based_aum", "sum"),
                    diff_1d=("diff_1d_value", "sum"),
                    diff_1w=("diff_1w_value", "sum") if "diff_1w_value" in client_schemes.columns else (lambda x: 0),
                )
                .reset_index()
                .sort_values("current", ascending=False)
            )
            
            scheme_disp = scheme_perf.rename(columns={
                "scheme_name": "Scheme",
                "invested": "Invested",
                "current": "Current",
                "diff_1d": "1D Diff",
                "diff_1w": "1W Diff"
            })
            
            st.dataframe(
                scheme_disp,
                width="stretch",
                hide_index=True,
                column_config={
                    "Invested": st.column_config.NumberColumn(format="₹ %.2f"),
                    "Current": st.column_config.NumberColumn(format="₹ %.2f"),
                    "1D Diff": st.column_config.NumberColumn(format="₹ %.2f"),
                    "1W Diff": st.column_config.NumberColumn(format="₹ %.2f"),
                }
            )

        # ── AMC / Scheme Breakdown (drilldown) ──
        st.divider()
        st.subheader("🏢 Portfolio Breakdown")

        if nav_ready and not folio_nav_df.empty:
            bd_df = folio_nav_df.copy()
            # Normalize investor names globally (handle case/whitespace differences)
            bd_df["investor_name"] = bd_df["investor_name"].str.upper().str.strip()
            bd_df["amc_name"] = bd_df["amc_name"].fillna("⚠️ Unresolved (no ISIN match)")
            bd_df["scheme_name"] = bd_df["scheme_name"].fillna("⚠️ Unresolved")

            breakdown_mode = st.radio(
                "Breakdown by", ["AMC-wise", "Scheme-wise", "Client-wise"], horizontal=True, key="bd_mode"
            )

            if "bd_selected_amc" not in st.session_state:
                st.session_state["bd_selected_amc"] = None
            if "bd_selected_scheme" not in st.session_state:
                st.session_state["bd_selected_scheme"] = None
            if "bd_selected_client" not in st.session_state:
                st.session_state["bd_selected_client"] = None

            if breakdown_mode != st.session_state.get("bd_last_mode"):
                st.session_state["bd_selected_amc"] = None
                st.session_state["bd_selected_scheme"] = None
                st.session_state["bd_selected_client"] = None
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

            # ── CLIENT-WISE BREAKDOWN: LEVEL 1 (All Clients) ──
            elif breakdown_mode == "Client-wise" and st.session_state["bd_selected_client"] is None:
                client_df = (
                    bd_df.groupby("investor_name", dropna=False)
                    .agg(
                        folios=("folio_id", "nunique"),
                        schemes=("scheme_name", "nunique"),
                        rtas=("rta", lambda x: ", ".join(sorted(set(x)))),
                        aum=("nav_based_aum", "sum"),
                        invested=("file_aum", "sum"),
                    )
                    .reset_index()
                    .sort_values("aum", ascending=False)
                )
                client_df["gain_loss"] = client_df["aum"] - client_df["invested"]
                client_df["aum_display"] = client_df["aum"].apply(format_aum)
                
                client_disp = client_df[["investor_name", "rtas", "folios", "schemes", "invested", "aum_display", "gain_loss"]].rename(
                    columns={
                        "investor_name": "Client Name",
                        "rtas": "RTAs",
                        "folios": "Folios",
                        "schemes": "Schemes",
                        "invested": "Invested",
                        "aum_display": "Current Value",
                        "gain_loss": "Gain/Loss"
                    }
                )
                
                sel_client = st.dataframe(
                    client_disp,
                    width="stretch",
                    hide_index=True,
                    on_select="rerun",
                    selection_mode="single-row",
                    key="bd_client_table",
                    column_config={
                        "Invested": st.column_config.NumberColumn(format="₹ %.2f"),
                        "Gain/Loss": st.column_config.NumberColumn(format="₹ %.2f"),
                    }
                )

                # Pie chart by client
                if not client_df.empty:
                    fig_client = px.pie(
                        client_df,
                        values="aum",
                        names="investor_name",
                        hole=0.4,
                        title="AUM Distribution by Client",
                        color_discrete_sequence=px.colors.qualitative.Set3
                    )
                    fig_client = theme_plotly(fig_client, dark)
                    fig_client.update_traces(textposition='inside', textinfo='percent+label', pull=[0.02] * len(client_df))
                    fig_client.update_layout(showlegend=True, margin=dict(t=40, b=20, l=20, r=20))
                    st.plotly_chart(fig_client, width="stretch")

                if sel_client and len(sel_client["selection"]["rows"]) > 0:
                    idx = sel_client["selection"]["rows"][0]
                    st.session_state["bd_selected_client"] = client_disp.iloc[idx]["Client Name"]
                    st.rerun()

            # ── CLIENT-WISE BREAKDOWN: LEVEL 2 (Individual Client Holdings) ──
            elif breakdown_mode == "Client-wise" and st.session_state["bd_selected_client"] is not None:
                sel_client_name = st.session_state["bd_selected_client"]
                
                if st.button("⬅️ Back to Clients"):
                    st.session_state["bd_selected_client"] = None
                    st.rerun()
                
                st.divider()
                st.markdown(f"### 👤 {sel_client_name} — Holdings")
                
                client_holdings = bd_df[bd_df["investor_name"] == sel_client_name].copy()
                
                # Summary metrics
                client_total_invested = client_holdings["file_aum"].sum()
                client_total_value = client_holdings["nav_based_aum"].sum()
                client_total_gain = client_total_value - client_total_invested
                
                cc1, cc2, cc3, cc4 = st.columns(4)
                cc1.metric("💰 Invested", format_aum(client_total_invested))
                cc2.metric("📈 Current Value", format_aum(client_total_value))
                cc3.metric("💹 Gain/Loss", format_aum(client_total_gain))
                cc4.metric("📊 Return %",
                        f"{(client_total_gain / client_total_invested * 100):.2f}%" if client_total_invested > 0 else "N/A")
                
                # Scheme-level breakdown
                scheme_breakdown = (
                    client_holdings.groupby(["scheme_name", "amc_name", "rta"], dropna=False)
                    .agg(
                        folios=("folio_id", "nunique"),
                        units=("units", "sum"),
                        invested=("file_aum", "sum"),
                        aum=("nav_based_aum", "sum"),
                    )
                    .reset_index()
                    .sort_values("aum", ascending=False)
                )
                scheme_breakdown["gain_loss"] = scheme_breakdown["aum"] - scheme_breakdown["invested"]
                
                display_scheme = scheme_breakdown.rename(columns={
                    "scheme_name": "Scheme",
                    "amc_name": "AMC",
                    "rta": "RTA",
                    "folios": "Folios",
                    "units": "Units",
                    "invested": "Invested",
                    "aum": "Current Value",
                    "gain_loss": "Gain/Loss"
                })
                
                st.markdown("#### 📋 Scheme-wise Breakdown")
                st.dataframe(
                    display_scheme,
                    width="stretch",
                    hide_index=True,
                    column_config={
                        "Units": st.column_config.NumberColumn(format="%.4f"),
                        "Invested": st.column_config.NumberColumn(format="₹ %.2f"),
                        "Current Value": st.column_config.NumberColumn(format="₹ %.2f"),
                        "Gain/Loss": st.column_config.NumberColumn(format="₹ %.2f"),
                    }
                )
                
                # Pie chart of schemes for this client
                if not scheme_breakdown.empty and scheme_breakdown["aum"].sum() > 0:
                    fig_scheme = px.pie(
                        scheme_breakdown,
                        values="aum",
                        names="scheme_name",
                        hole=0.4,
                        title=f"Scheme Distribution — {sel_client_name}",
                        color_discrete_sequence=px.colors.qualitative.Pastel
                    )
                    fig_scheme = theme_plotly(fig_scheme, dark)
                    fig_scheme.update_traces(textposition='inside', textinfo='percent+label')
                    fig_scheme.update_layout(showlegend=True, margin=dict(t=40, b=20, l=20, r=20))
                    st.plotly_chart(fig_scheme, width="stretch")
                
                # RTA breakdown for this client
                st.markdown("#### 🏢 RTA-wise Summary")
                rta_breakdown = (
                    client_holdings.groupby("rta")
                    .agg(
                        folios=("folio_id", "nunique"),
                        schemes=("scheme_name", "nunique"),
                        invested=("file_aum", "sum"),
                        aum=("nav_based_aum", "sum"),
                    )
                    .reset_index()
                )
                rta_breakdown["gain_loss"] = rta_breakdown["aum"] - rta_breakdown["invested"]
                
                display_rta = rta_breakdown.rename(columns={
                    "rta": "RTA",
                    "folios": "Folios",
                    "schemes": "Schemes",
                    "invested": "Invested",
                    "aum": "Current Value",
                    "gain_loss": "Gain/Loss"
                })
                
                st.dataframe(
                    display_rta,
                    width="stretch",
                    hide_index=True,
                    column_config={
                        "Invested": st.column_config.NumberColumn(format="₹ %.2f"),
                        "Current Value": st.column_config.NumberColumn(format="₹ %.2f"),
                        "Gain/Loss": st.column_config.NumberColumn(format="₹ %.2f"),
                    }
                )

        else:
                st.info("Breakdown requires NAV data. Refresh if still loading.")

                # ── Recent Uploads ──
                st.divider()
                st.subheader("📤 Recent Uploads")
                uploads_df = load_recent_uploads(data_version())
                if not uploads_df.empty:
                    st.dataframe(uploads_df, width="stretch", hide_index=True)
                else:
                    st.info("No uploads yet. Go to Admin Panel to upload data.")



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


        # ── Recent Uploads ──
        st.divider()
        st.subheader("📤 Recent Uploads")
        uploads_df = load_recent_uploads(data_version())
        if not uploads_df.empty:
            st.dataframe(uploads_df, width="stretch", hide_index=True)
        else:
            st.info("No uploads yet. Go to Admin Panel to upload data.")




# ==================== 👥 CLIENTS ====================
elif mode == "👥 Clients":
    st.header("👤 Client Portfolio & Analytics")

    selected_display, selected_client = render_client_selector("clients_tab")
    
    if not selected_display or selected_client is None:
        st.stop()
    
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
            st.session_state["folio_nav_df"] = get_all_folios_with_isin_and_nav(get_conn, data_version())

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
    name_clean = name.strip().upper() if name else ""

    with get_conn() as conn:
        if is_minor:
            cams_f = pd.read_sql(
                "SELECT foliochk FROM cams_wbr9_folio WHERE TRIM(UPPER(inv_name)) LIKE ? || '%'",
                conn, params=(name_clean,))
            kfin_f = pd.read_sql(
                "SELECT folio FROM kfin_mfsd211_folio WHERE TRIM(UPPER(investor_name)) LIKE ? || '%'",
                conn, params=(name_clean,))
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
                width="stretch", hide_index=True
            )
        else:
            st.info("No folios found.")
        st.stop()

    # ── Tabs ──
    if family is None:
        tab_portfolio, tab_sips, tab_transactions, tab_brokerage = st.tabs(
            ["📈 Portfolio & AUM", "🔄 Active SIPs", "📜 Transactions", "💰 Brokerage"]
        )
    else:
        tab_family, tab_portfolio, tab_sips, tab_transactions, tab_brokerage = st.tabs(
            ["👨‍👩‍👧‍👦 Family Portfolio", "📈 Portfolio & AUM", "🔄 Active SIPs","📜 Transactions", "💰 Brokerage"]
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
                h = compute_client_holdings(mc, _folio_nav_df=folio_nav_df, _v=data_version())
                if h is None:
                    h = pd.DataFrame()
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

                member_holdings = compute_client_holdings(selected_member_code, _folio_nav_df=folio_nav_df, _v=data_version())
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
                all_clients_df = load_all_clients_with_display(data_version())
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
                    width="stretch", hide_index=True
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
                    kfin_invested_df = get_kfin_invested_per_scheme(kfin_f['folio'].tolist(), data_version())
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
                    cams_invested_df = get_cams_invested_per_scheme(cams_f['foliochk'].tolist(), data_version())
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

                # ── DEBUG: 1D Diff diagnosis (hidden behind toggle) ──
                if show_debug:
                    with st.expander("🐞 DEBUG: 1-Day Diff", expanded=False):
                        # Show which file was actually used
                        path_used, date_used = _previous_snapshot_path()
                        st.write(f"**Previous snapshot file:** `{path_used}`")
                        st.write(f"**Previous NAV date:** {date_used or 'None'}")
                        st.write(f"**Previous NAV map size:** {len(prev_nav_map)} ISINs")

                        if not prev_nav_map:
                            st.error("❌ prev_nav_map is EMPTY")
                        else:
                            sample = dict(list(prev_nav_map.items())[:3])
                            st.write("**Sample prev_nav_map:**", sample)

                        def _lookup_prev(isin):
                            if pd.isna(isin):
                                return None, "ISIN is NaN"
                            s = str(isin).strip().upper()
                            if not s:
                                return None, "ISIN is empty string"
                            val = prev_nav_map.get(s)
                            if val is None:
                                return None, f"ISIN {s} not in prev map"
                            return val, "OK"

                        prev_info = holdings["isin"].apply(_lookup_prev)
                        holdings["_prev_nav"] = prev_info.apply(lambda x: x[0])
                        holdings["_prev_reason"] = prev_info.apply(lambda x: x[1])

                        reason_counts = holdings["_prev_reason"].value_counts().to_dict()
                        st.write("**Prev NAV lookup reasons:**", reason_counts)

                        missing_prev = holdings[
                            holdings["current_nav"].notna() & holdings["_prev_nav"].isna()
                        ]
                        st.write(f"**Rows with current_nav but NO prev_nav:** {len(missing_prev)}")

                        if not missing_prev.empty:
                            st.dataframe(
                                missing_prev[["folio_id", "rta", "scheme_name", "isin", "current_nav", "_prev_reason"]]
                                .head(20),
                                hide_index=True
                            )

                        holdings["prev_nav"] = holdings["_prev_nav"]
                        holdings["one_day_diff"] = (
                            (holdings["current_nav"] - holdings["prev_nav"]) * holdings["units"]
                        ).fillna(0.0)

                        holdings["_diff_reason"] = holdings.apply(lambda r:
                            "missing prev_nav" if pd.isna(r["_prev_nav"]) and pd.notna(r["current_nav"])
                            else ("zero units" if pd.isna(r.get("units")) or r.get("units", 0) == 0
                            else ("nav unchanged" if r["current_nav"] == r["_prev_nav"]
                            else "calculated")),
                            axis=1
                        )

                        zero_diff = holdings[holdings["one_day_diff"].fillna(0) == 0]
                        if not zero_diff.empty:
                            st.write("**Why 1D Diff = 0:**", zero_diff["_diff_reason"].value_counts().to_dict())
                            st.dataframe(
                                zero_diff[["folio_id", "rta", "scheme_name", "isin", "current_nav", "prev_nav", "units",
                                           "_diff_reason"]]
                                .head(20),
                                hide_index=True
                            )

                        holdings = holdings.drop(columns=["_prev_nav", "_prev_reason", "_diff_reason"], errors="ignore")
                else:
                    # Normal path: no debug prints, just compute silently
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

                h1, h2, h3 = st.columns(3)
                h1.metric("Total Invested", format_aum(total_invested))
                h2.metric("Current Value", format_aum(total_current))
                h3.metric("Gain / Loss", format_aum(total_gain_loss),
                        delta=f"{(total_gain_loss / total_invested * 100):.2f}%" if total_invested > 0 else "0%")
                h4, h5 = st.columns(2)
                h4.metric("Total Folios", len(all_folios))
                h5.metric("1-Day Diff", format_aum(total_one_day_diff))

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
                            _get_conn=get_conn,
                            rta=frta,
                            product_code=fprod,
                            current_value=round(float(fvalue), 2), 
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
                        current_nav=("current_nav", "first"),  # ← add
                        prev_nav=("prev_nav", "first"),  # ← add
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
                grouped_holdings["abs_return"] = grouped_holdings.apply(
                    lambda r: (r["gain_loss"] / r["file_aum"] * 100) if r["file_aum"] else None, axis=1
                    )

                display_holdings = grouped_holdings[[
    'rta', 'amc_name', 'scheme_name', 'folios', 'units', 'file_aum',
    'nav_based_aum', 'gain_loss', 'one_day_diff', 'xirr', 'abs_return',
    'portfolio_pct', 'current_nav', 'prev_nav'
]].rename(columns={
    'rta': 'RTA', 'amc_name': 'AMC', 'scheme_name': 'Scheme', 'folios': 'Folios',
    'file_aum': 'Invested', 'nav_based_aum': 'Current Value',
    'gain_loss': 'Gain/Loss', 'one_day_diff': '1D Diff',
    'xirr': 'XIRR', 'abs_return': 'Abs Return', 'portfolio_pct': '% Portfolio',
    'current_nav': 'Today NAV', 'prev_nav': 'Prev NAV'
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
                        "Abs Return": st.column_config.NumberColumn(format="%.2f%%"),
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

    # ═══════════════════════════════════════════════════════════
    # TAB TRANSACTIONS — All recent transactions with folio filter
    # ═══════════════════════════════════════════════════════════
    with tab_transactions:
        st.subheader("📜 All Transactions")

        if not all_folios:
            st.info("No folios found for this client.")
        else:
            # ── Build folio → RTA map ──
            folio_rta_map = {}
            for f in cams_f['foliochk'].tolist():
                folio_rta_map[f] = 'CAMS'
            for f in kfin_f['folio'].tolist():
                folio_rta_map[f] = 'KFinTech'

            # ── Filters ──
            folio_scheme_map = {}
            cams_sch = get_client_cams_schemes(cams_f['foliochk'].tolist(), data_version())
            for _, r in cams_sch.iterrows():
                folio_scheme_map.setdefault(r['folio_no'], r['scheme'])
            kfin_sch = get_client_kfin_schemes(kfin_f['folio'].tolist(), data_version())
            for _, r in kfin_sch.iterrows():
                folio_scheme_map.setdefault(r['folio_no'], r['scheme'])

            fc1, fc2 = st.columns([1, 1])
            with fc1:
                sorted_folios = sorted(all_folios)
                folio_options = ["All Folios"] + [
                    f"{folio_scheme_map.get(f, '?')} ({f})" for f in sorted_folios
                ]
                folio_choice = st.selectbox(
                    "Select Folio", folio_options, index=0,
                    key="txn_tab_folio_select"
                )
            with fc2:
                date_filter = st.selectbox(
                    "Time Period",
                    ["All Time", "Last 30 Days", "Last 90 Days", "Last 6 Months", "Last 1 Year"],
                    key="txn_tab_date_filter"
                )

            if folio_choice == "All Folios":
                folios_to_fetch = sorted_folios
            else:
                sel_folio = folio_choice.rsplit("(", 1)[-1].rstrip(")")
                folios_to_fetch = [sel_folio]

            # ── Separate by RTA ──
            cams_folios_list = [f for f in folios_to_fetch if folio_rta_map.get(f) == 'CAMS']
            kfin_folios_list = [f for f in folios_to_fetch if folio_rta_map.get(f) == 'KFinTech']

            txn_frames = []

            with get_conn() as conn:
                # ── CAMS transactions ──
                if cams_folios_list:
                    ph = ','.join(['?'] * len(cams_folios_list))
                    cams_txn = pd.read_sql(f"""
                        SELECT folio_no   AS folio_id,
                               'CAMS'      AS rta,
                               trxnno,
                               traddate,
                               trxntype,
                               trxnmode,
                               trxnstat,
                               purprice,
                               units,
                               amount,
                               brokcode,
                               subbrok,
                               remarks,
                               prodcode    AS product_code
                        FROM cams_wbr2_transaction
                        WHERE folio_no IN ({ph})
                    """, conn, params=tuple(cams_folios_list))
                    if not cams_txn.empty:
                        txn_frames.append(cams_txn)

                # ── KFinTech transactions ──
                if kfin_folios_list:
                    ph = ','.join(['?'] * len(kfin_folios_list))
                    kfin_txn = pd.read_sql(f"""
                        SELECT td_acno    AS folio_id,
                               'KFinTech' AS rta,
                               td_trno    AS trxnno,
                               td_trdt    AS traddate,
                               td_purred  AS trxntype,
                               trnmode    AS trxnmode,
                               trnstat    AS trxnstat,
                               td_pop     AS purprice,
                               td_units   AS units,
                               td_amt     AS amount,
                               td_broker  AS brokcode,
                               ''         AS subbrok,
                               trdesc     AS remarks,
                               UPPER(TRIM(fmcode)) AS product_code
                        FROM kfin_mfsd201_transaction
                        WHERE td_acno IN ({ph})
                    """, conn, params=tuple(kfin_folios_list))
                    if not kfin_txn.empty:
                        txn_frames.append(kfin_txn)

            if txn_frames:
                txn_df = pd.concat(txn_frames, ignore_index=True)

                # ── Resolve scheme names via bse_scheme_master ──
                if not txn_df['product_code'].dropna().empty:
                    with get_conn() as conn:
                        scheme_map_df = pd.read_sql("""
                            SELECT UPPER(TRIM(Channel_Partner_Code)) AS product_code,
                                   MAX(Scheme_Name) AS scheme_name
                            FROM bse_scheme_master
                            WHERE Channel_Partner_Code IS NOT NULL
                              AND TRIM(Channel_Partner_Code) != ''
                            GROUP BY UPPER(TRIM(Channel_Partner_Code))
                        """, conn)
                    scheme_map = dict(zip(
                        scheme_map_df['product_code'],
                        scheme_map_df['scheme_name']
                    ))
                    txn_df['scheme_name'] = (
                        txn_df['product_code'].str.strip().str.upper().map(scheme_map)
                    )
                else:
                    txn_df['scheme_name'] = None

                # ── Parse dates for sorting & filtering ──
                txn_df['_sort_date'] = pd.to_datetime(txn_df['traddate'], errors='coerce')

                # ── Apply date filter ──
                if date_filter != "All Time":
                    now = datetime.now()
                    deltas = {
                        "Last 30 Days": 30,
                        "Last 90 Days": 90,
                        "Last 6 Months": 180,
                        "Last 1 Year": 365,
                    }
                    days = deltas.get(date_filter)
                    if days:
                        cutoff = now - timedelta(days=days)
                        txn_df = txn_df[txn_df['_sort_date'] >= cutoff]

                txn_df = (
                    txn_df
                    .sort_values('_sort_date', ascending=False)
                    .drop(columns=['_sort_date'])
                    .reset_index(drop=True)
                )

                # ── Reorder & rename columns ──
                col_order = [
                    'traddate', 'folio_id', 'rta', 'scheme_name', 'product_code',
                    'trxntype', 'trxnmode', 'trxnstat', 'units', 'purprice', 'amount',
                    'brokcode', 'subbrok', 'remarks', 'trxnno'
                ]
                col_order = [c for c in col_order if c in txn_df.columns]
                txn_df = txn_df[col_order]

                display_txn = txn_df.rename(columns={
                    'traddate': 'Date', 'folio_id': 'Folio', 'rta': 'RTA',
                    'scheme_name': 'Scheme', 'product_code': 'Product Code',
                    'trxntype': 'Type', 'trxnmode': 'Mode', 'trxnstat': 'Status',
                    'units': 'Units', 'purprice': 'Price', 'amount': 'Amount',
                    'brokcode': 'Broker', 'subbrok': 'Sub-Broker',
                    'remarks': 'Remarks', 'trxnno': 'Txn No'
                })

                # ── Metrics ──
                if not txn_df.empty:
                    t1, t2 = st.columns(2)
                    t1.metric("Transactions", len(txn_df))
                    t2.metric("Total Amount", format_aum(txn_df['amount'].sum()))

                    # ── AgGrid (preferred) or plain dataframe ──
                    try:
                        from st_aggrid import AgGrid, GridOptionsBuilder, GridUpdateMode

                        gb = GridOptionsBuilder.from_dataframe(display_txn)
                        gb.configure_default_column(
                            filter=True, sortable=True, resizable=True, flex=1
                        )
                        gb.configure_pagination(
                            paginationAutoPageSize=False, paginationPageSize=20
                        )
                        gb.configure_grid_options(domLayout='normal')
                        grid_opts = gb.build()
                        AgGrid(
                            display_txn,
                            gridOptions=grid_opts,
                            height=450,
                            update_mode=GridUpdateMode.NO_UPDATE,
                            fit_columns_on_grid_load=True,
                            allow_unsafe_jscode=True,
                            theme="alpine-dark" if dark else "alpine",
                            key=f"all_txn_grid_{folio_choice}_{date_filter}"
                        )
                    except ImportError:
                        st.dataframe(
                            display_txn,
                            width="stretch",
                            hide_index=True,
                            column_config={
                                "Units": st.column_config.NumberColumn(format="%.4f"),
                                "Amount": st.column_config.NumberColumn(format="₹ %.2f"),
                                "Price": st.column_config.NumberColumn(format="₹ %.4f"),
                            }
                        )
                else:
                    st.info("No transactions found for the selected filters.")
            else:
                st.info("No transactions found for this client.")

    # ═══════════════════════════════════════════════════════════
    # TAB BROKERAGE — Client-specific brokerage by month
    # ═══════════════════════════════════════════════════════════

    with tab_brokerage:
        st.subheader("💰 Brokerage Generated")

        if not all_folios:
            st.info("No folios found for this client — cannot fetch brokerage.")
        else:
            folio_list = list(all_folios)
            placeholders = ",".join(["?"] * len(folio_list))

            with get_conn() as conn:
                cams_brok = pd.read_sql(f"""
                    SELECT proc_date, inv_name AS client, folio_no AS folio,
                           scheme_code, trxn_no, plot_amount AS txn_amount,
                           brkage_rate AS brokerage_pct, brkage_amt AS brokerage_amount,
                           brkage_type AS brokerage_type
                    FROM cams_wbr77_brokerage
                    WHERE folio_no IN ({placeholders})
                """, conn, params=folio_list)

                kfin_brok = pd.read_sql(f"""
                    SELECT process_date AS proc_date, investor_name AS client,
                           account_number AS folio, scheme_code,
                           transaction_number AS trxn_no, amount AS txn_amount,
                           percentage AS brokerage_pct, brokerage AS brokerage_amount,
                           brokerage_type
                    FROM kfin_mfsd205_brokerage
                    WHERE account_number IN ({placeholders})
                """, conn, params=folio_list)

            # ── Parse dates ──
            if not cams_brok.empty:
                cams_brok["proc_date"] = pd.to_datetime(
                    cams_brok["proc_date"], errors="coerce", dayfirst=False
                )
                cams_brok["rta"] = "CAMS"
            if not kfin_brok.empty:
                kfin_brok["proc_date"] = pd.to_datetime(
                    kfin_brok["proc_date"], errors="coerce", dayfirst=False
                )
                kfin_brok["rta"] = "KFinTech"

            brok_df = pd.concat([cams_brok, kfin_brok], ignore_index=True)

            if brok_df.empty:
                st.info("No brokerage records found for this client's folios.")
            else:
                # ── FORCE datetime after concat (handles empty-side object dtype) ──
                brok_df["proc_date"] = pd.to_datetime(brok_df["proc_date"], errors="coerce")
                brok_df = brok_df.dropna(subset=["proc_date"])  # discard unparseable rows

                # ── EXTRA GUARD: all rows may have had bad dates ──
                if brok_df.empty:
                    st.info("No brokerage records with valid dates found for this client.")
                else:
                    brok_df["month"] = brok_df["proc_date"].dt.strftime("%Y-%m")
                    brok_df["brokerage_amount"] = pd.to_numeric(
                        brok_df["brokerage_amount"], errors="coerce"
                    ).fillna(0)

                    # ── Monthly summary ──
                    monthly = (
                        brok_df.groupby("month")["brokerage_amount"]
                        .sum()
                        .reset_index()
                        .sort_values("month")
                    )

                    # ── Top metrics ──
                    total_brok = brok_df["brokerage_amount"].sum()
                    avg_monthly = monthly["brokerage_amount"].mean() if not monthly.empty else 0
                    if not monthly.empty:
                        best_row = monthly.loc[monthly["brokerage_amount"].idxmax()]
                        best_month, best_amount = best_row["month"], best_row["brokerage_amount"]
                    else:
                        best_month, best_amount = "-", 0

                    m1, m2, m3, m4 = st.columns(4)
                    m1.metric("Total Brokerage", format_brokerage_inr(total_brok))
                    m2.metric("Avg Monthly", format_brokerage_inr(avg_monthly))
                    m3.metric("Best Month", str(best_month))
                    m4.metric("Best Month Amount", format_brokerage_inr(best_amount))

                    st.divider()

                    # ── Monthly bar chart ──
                    if not monthly.empty:
                        fig = px.bar(
                            monthly,
                            x="month",
                            y="brokerage_amount",
                            title="Monthly Brokerage Trend",
                            labels={"brokerage_amount": "Brokerage (₹)", "month": "Month"},
                            color_discrete_sequence=["#6366f1"],
                        )
                        fig = theme_plotly(fig, dark)
                        st.plotly_chart(fig, width="stretch")
                    else:
                        st.info("No monthly brokerage data to display.")

                    st.divider()

                    # ── Month-wise table ──
                    st.subheader("📅 Month-wise Summary")
                    if not monthly.empty:
                        st.dataframe(
                            monthly.rename(
                                columns={"month": "Month", "brokerage_amount": "Brokerage"}
                            ),
                            width="stretch",
                            hide_index=True,
                            column_config={
                                "Brokerage": st.column_config.NumberColumn(format="₹ %.2f")
                            },
                        )
                    else:
                        st.info("No monthly data available.")

                    st.divider()

                    # ── Transaction-level detail ──
                    st.subheader("📜 Transaction-level Detail")
                    detail_cols = [
                        "rta", "proc_date", "client", "folio", "scheme_code",
                        "txn_amount", "brokerage_pct", "brokerage_amount", "brokerage_type",
                    ]
                    detail_cols = [c for c in detail_cols if c in brok_df.columns]
                    detail_display = brok_df[detail_cols].sort_values(
                        "proc_date", ascending=False
                    ).copy()
                    detail_display["proc_date"] = detail_display["proc_date"].dt.strftime(
                        "%Y-%m-%d"
                    )

                    st.dataframe(
                        detail_display.rename(
                            columns={
                                "rta": "RTA",
                                "proc_date": "Date",
                                "client": "Client",
                                "folio": "Folio",
                                "scheme_code": "Scheme",
                                "txn_amount": "Txn Amount",
                                "brokerage_pct": "Brokerage %",
                                "brokerage_amount": "Brokerage",
                                "brokerage_type": "Type",
                            }
                        ),
                        width="stretch",
                        hide_index=True,
                        column_config={
                            "Txn Amount": st.column_config.NumberColumn(format="₹ %.2f"),
                            "Brokerage %": st.column_config.NumberColumn(format="%.4f"),
                            "Brokerage": st.column_config.NumberColumn(format="₹ %.2f"),
                        },
                    )

                    csv = brok_df.to_csv(index=False).encode("utf-8")
                    st.download_button(
                        "⬇️ Download Client Brokerage (CSV)",
                        csv,
                        f"brokerage_{client_code}.csv",
                        "text/csv",
                        key=f"client_brok_dl_{client_code}",
                    )

# ==================== 📋 TRANSACTIONS ====================
elif mode == "📋 Transactions":
    st.header("📋 All Transactions Explorer")
    st.caption("Track all mutual fund transactions across RTAs, AMCs, schemes, clients, and dates.")

    # ── Load all transactions ──
    @st.cache_data(show_spinner=False)
    def load_all_transactions(_v: int) -> pd.DataFrame:
        with get_conn() as conn:
            # CAMS transactions
            cams_txn = pd.read_sql("""
                SELECT 
                    'CAMS' AS rta,
                    folio_no AS folio,
                    inv_name AS client_name,
                    prodcode AS product_code,
                    traddate AS txn_date,
                    trxntype AS txn_type,
                    trxnmode AS txn_mode,
                    trxnstat AS txn_status,
                    units,
                    purprice AS price,
                    amount,
                    brokcode AS broker_code,
                    subbrok AS sub_broker,
                    remarks,
                    trxnno AS txn_no
                FROM cams_wbr2_transaction
            """, conn)

            # KFinTech transactions (with JOIN to get investor name)
            kfin_txn = pd.read_sql("""
                SELECT 
                    'KFinTech' AS rta,
                    kt.td_acno AS folio,
                    kf.investor_name AS client_name,
                    UPPER(TRIM(kt.fmcode)) AS product_code,
                    kt.td_trdt AS txn_date,
                    kt.td_purred AS txn_type,
                    kt.trnmode AS txn_mode,
                    kt.trnstat AS txn_status,
                    kt.td_units AS units,
                    kt.td_pop AS price,
                    kt.td_amt AS amount,
                    kt.td_broker AS broker_code,
                    '' AS sub_broker,
                    kt.trdesc AS remarks,
                    kt.td_trno AS txn_no
                FROM kfin_mfsd201_transaction kt
                LEFT JOIN kfin_mfsd211_folio kf ON kt.td_acno = kf.Folio
            """, conn)

            # Combine
            all_txn = pd.concat([cams_txn, kfin_txn], ignore_index=True)
            
            if all_txn.empty:
                return pd.DataFrame()

            # Parse dates
            all_txn["txn_date"] = pd.to_datetime(all_txn["txn_date"], errors="coerce")
            all_txn = all_txn.dropna(subset=["txn_date"])

            # Resolve scheme names via BSE master
            if not all_txn["product_code"].dropna().empty:
                scheme_map_df = pd.read_sql("""
                    SELECT UPPER(TRIM(Channel_Partner_Code)) AS product_code,
                           MAX(Scheme_Name) AS scheme_name,
                           MAX(ISIN) AS isin
                    FROM bse_scheme_master
                    WHERE Channel_Partner_Code IS NOT NULL
                    GROUP BY UPPER(TRIM(Channel_Partner_Code))
                """, conn)
                scheme_map = dict(zip(scheme_map_df["product_code"], scheme_map_df["scheme_name"]))
                isin_map = dict(zip(scheme_map_df["product_code"], scheme_map_df["isin"]))
            else:
                scheme_map = {}
                isin_map = {}

            all_txn["product_code_norm"] = all_txn["product_code"].astype(str).str.strip().str.upper()
            all_txn["scheme_name"] = all_txn["product_code_norm"].map(scheme_map).fillna(all_txn["product_code"])
            all_txn["isin"] = all_txn["product_code_norm"].map(isin_map)

            # Resolve AMC via ISIN
            folio_nav_df_amc = st.session_state.get("folio_nav_df")
            if folio_nav_df_amc is not None and not folio_nav_df_amc.empty:
                amc_map = dict(zip(
                    folio_nav_df_amc["isin"].astype(str).str.strip().str.upper(),
                    folio_nav_df_amc["amc_name"]
                ))
                all_txn["amc_name"] = all_txn["isin"].astype(str).str.strip().str.upper().map(amc_map)
            else:
                all_txn["amc_name"] = None

            all_txn["amc_name"] = all_txn["amc_name"].fillna("⚠️ Unresolved")

            return all_txn

    all_txn_df = load_all_transactions(data_version())

    if all_txn_df.empty:
        st.info("No transactions found in database.")
        st.stop()

    # ═══════════════════════════════════════════════════════════
    # SECTION 1: FILTER PANEL
    # ═══════════════════════════════════════════════════════════
    st.subheader("🔍 Filters")

    f1, f2, f3, f4, f5 = st.columns(5)

    with f1:
        rta_filter = st.multiselect(
            "RTA",
            options=sorted(all_txn_df["rta"].unique()),
            default=sorted(all_txn_df["rta"].unique()),
            key="txn_rta_filter"
        )

    with f2:
        amc_options = sorted([a for a in all_txn_df["amc_name"].unique() if a])
        amc_filter = st.multiselect(
            "AMC",
            options=amc_options,
            default=amc_options[:5] if len(amc_options) > 5 else amc_options,
            key="txn_amc_filter"
        )

    with f3:
        scheme_options = sorted([s for s in all_txn_df["scheme_name"].unique() if s])
        scheme_filter = st.multiselect(
            "Scheme",
            options=scheme_options,
            default=None,
            key="txn_scheme_filter"
        )

    with f4:
        txn_type_options = sorted([t for t in all_txn_df["txn_type"].unique() if pd.notna(t)])
        txn_type_filter = st.multiselect(
            "Txn Type",
            options=txn_type_options,
            default=txn_type_options,
            key="txn_type_filter"
        )

    with f5:
        date_range = st.date_input(
            "Date Range",
            value=(all_txn_df["txn_date"].min().date(), all_txn_df["txn_date"].max().date()),
            key="txn_date_range"
        )

    # Client search (separate row)
    search_col1, search_col2 = st.columns([3, 2])
    with search_col1:
        client_search = st.text_input(
            "🔍 Search Client / Folio",
            placeholder="Type client name or folio number...",
            key="txn_client_search"
        )

    with search_col2:
        st.markdown("<div style='height: 28px'></div>", unsafe_allow_html=True)
        clear_filters = st.button("🔄 Clear Filters", key="txn_clear_btn", use_container_width=True)

    # Apply filters
    if clear_filters:
        st.rerun()

    filtered_df = all_txn_df[
        (all_txn_df["rta"].isin(rta_filter)) &
        (all_txn_df["amc_name"].isin(amc_filter)) &
        (all_txn_df["txn_type"].isin(txn_type_filter))
    ]

    if scheme_filter:
        filtered_df = filtered_df[filtered_df["scheme_name"].isin(scheme_filter)]

    if len(date_range) == 2:
        filtered_df = filtered_df[
            (filtered_df["txn_date"].dt.date >= date_range[0]) &
            (filtered_df["txn_date"].dt.date <= date_range[1])
        ]

    if client_search.strip():
        mask = (
            filtered_df["client_name"].astype(str).str.contains(client_search, case=False, na=False) |
            filtered_df["folio"].astype(str).str.contains(client_search, case=False, na=False)
        )
        filtered_df = filtered_df[mask]

    # ═══════════════════════════════════════════════════════════
    # SECTION 2: SUMMARY METRICS
    # ═══════════════════════════════════════════════════════════
    st.divider()
    st.subheader("📊 Summary")

    if filtered_df.empty:
        st.info("No transactions match the selected filters.")
        st.stop()

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Total Transactions", f"{len(filtered_df):,}")
    m2.metric("Total Amount", format_aum(filtered_df["amount"].sum()))
    m3.metric("Total Units", f"{filtered_df['units'].sum():.2f}")
    m4.metric("Unique Clients", filtered_df["client_name"].nunique())
    m5.metric("Unique Folios", filtered_df["folio"].nunique())

    st.divider()

    # ═══════════════════════════════════════════════════════════
    # SECTION 3: BREAKDOWN TABS
    # ═══════════════════════════════════════════════════════════
    tab_detail, tab_amc, tab_scheme, tab_client, tab_rta = st.tabs(
        ["📋 Detailed View", "🏢 AMC-wise", "📈 Scheme-wise", "👥 Client-wise", "🔵 RTA-wise"]
    )

    # ── TAB 1: Detailed View ──
    with tab_detail:
        st.subheader("📋 All Transactions")

        display_cols = [
            "txn_date", "rta", "client_name", "folio", "amc_name", "scheme_name",
            "txn_type", "units", "price", "amount", "txn_status"
        ]
        display_cols = [c for c in display_cols if c in filtered_df.columns]

        display_df = filtered_df[display_cols].rename(columns={
            "txn_date": "Date",
            "rta": "RTA",
            "client_name": "Client",
            "folio": "Folio",
            "amc_name": "AMC",
            "scheme_name": "Scheme",
            "txn_type": "Type",
            "units": "Units",
            "price": "Price",
            "amount": "Amount",
            "txn_status": "Status"
        })

        display_df = display_df.sort_values("Date", ascending=False)

        try:
            from st_aggrid import AgGrid, GridOptionsBuilder, GridUpdateMode

            gb = GridOptionsBuilder.from_dataframe(display_df)
            gb.configure_default_column(filter=True, sortable=True, resizable=True, flex=1)
            gb.configure_pagination(paginationAutoPageSize=False, paginationPageSize=25)
            grid_opts = gb.build()
            AgGrid(
                display_df,
                gridOptions=grid_opts,
                height=600,
                update_mode=GridUpdateMode.NO_UPDATE,
                fit_columns_on_grid_load=True,
                theme="alpine-dark" if dark else "alpine",
                key="txn_detail_grid"
            )
        except ImportError:
            st.dataframe(
                display_df,
                width="stretch",
                hide_index=True,
                column_config={
                    "Units": st.column_config.NumberColumn(format="%.4f"),
                    "Price": st.column_config.NumberColumn(format="₹ %.4f"),
                    "Amount": st.column_config.NumberColumn(format="₹ %.2f"),
                }
            )

        csv = display_df.to_csv(index=False).encode("utf-8")
        st.download_button(
            "⬇️ Download Detailed Transactions (CSV)",
            csv,
            "transactions_detail.csv",
            "text/csv",
            key="txn_detail_download"
        )

    # ── TAB 2: AMC-wise ──
    with tab_amc:
        st.subheader("🏢 AMC-wise Breakdown")

        amc_summary = (
            filtered_df.groupby("amc_name")
            .agg(
                transactions=("txn_no", "count"),
                total_units=("units", "sum"),
                total_amount=("amount", "sum"),
                avg_price=("price", "mean"),
                unique_clients=("client_name", "nunique"),
                unique_schemes=("scheme_name", "nunique"),
            )
            .reset_index()
            .sort_values("total_amount", ascending=False)
        )

        amc_display = amc_summary.rename(columns={
            "amc_name": "AMC",
            "transactions": "Count",
            "total_units": "Total Units",
            "total_amount": "Total Amount",
            "avg_price": "Avg Price",
            "unique_clients": "Clients",
            "unique_schemes": "Schemes",
        })

        st.dataframe(
            amc_display,
            width="stretch",
            hide_index=True,
            column_config={
                "Total Units": st.column_config.NumberColumn(format="%.2f"),
                "Total Amount": st.column_config.NumberColumn(format="₹ %.2f"),
                "Avg Price": st.column_config.NumberColumn(format="₹ %.4f"),
            }
        )

        if not amc_summary.empty:
            fig_amc = px.bar(
                amc_summary,
                x="amc_name",
                y="total_amount",
                title="Transaction Amount by AMC",
                labels={"amc_name": "AMC", "total_amount": "Amount (₹)"},
                color_discrete_sequence=["#6366f1"]
            )
            fig_amc = theme_plotly(fig_amc, dark)
            fig_amc.update_xaxes(tickangle=-45)
            st.plotly_chart(fig_amc, width="stretch")

    # ── TAB 3: Scheme-wise ──
    with tab_scheme:
        st.subheader("📈 Scheme-wise Breakdown")

        scheme_summary = (
            filtered_df.groupby(["scheme_name", "amc_name"])
            .agg(
                transactions=("txn_no", "count"),
                total_units=("units", "sum"),
                total_amount=("amount", "sum"),
                avg_price=("price", "mean"),
            )
            .reset_index()
            .sort_values("total_amount", ascending=False)
        )

        scheme_display = scheme_summary.rename(columns={
            "scheme_name": "Scheme",
            "amc_name": "AMC",
            "transactions": "Count",
            "total_units": "Total Units",
            "total_amount": "Total Amount",
            "avg_price": "Avg Price",
        })

        st.dataframe(
            scheme_display,
            width="stretch",
            hide_index=True,
            column_config={
                "Total Units": st.column_config.NumberColumn(format="%.2f"),
                "Total Amount": st.column_config.NumberColumn(format="₹ %.2f"),
                "Avg Price": st.column_config.NumberColumn(format="₹ %.4f"),
            }
        )

        if not scheme_summary.empty and len(scheme_summary) <= 15:
            fig_scheme = px.bar(
                scheme_summary,
                x="scheme_name",
                y="total_amount",
                color="amc_name",
                title="Transaction Amount by Scheme",
                labels={"scheme_name": "Scheme", "total_amount": "Amount (₹)", "amc_name": "AMC"},
            )
            fig_scheme = theme_plotly(fig_scheme, dark)
            fig_scheme.update_xaxes(tickangle=-45)
            st.plotly_chart(fig_scheme, width="stretch")

    # ── TAB 4: Client-wise ──
    with tab_client:
        st.subheader("👥 Client-wise Breakdown")

        client_summary = (
            filtered_df.groupby("client_name")
            .agg(
                transactions=("txn_no", "count"),
                total_units=("units", "sum"),
                total_amount=("amount", "sum"),
                unique_folios=("folio", "nunique"),
                unique_schemes=("scheme_name", "nunique"),
                unique_amcs=("amc_name", "nunique"),
            )
            .reset_index()
            .sort_values("total_amount", ascending=False)
            .head(50)
        )

        client_display = client_summary.rename(columns={
            "client_name": "Client",
            "transactions": "Count",
            "total_units": "Total Units",
            "total_amount": "Total Amount",
            "unique_folios": "Folios",
            "unique_schemes": "Schemes",
            "unique_amcs": "AMCs",
        })

        st.dataframe(
            client_display,
            width="stretch",
            hide_index=True,
            column_config={
                "Total Units": st.column_config.NumberColumn(format="%.2f"),
                "Total Amount": st.column_config.NumberColumn(format="₹ %.2f"),
            }
        )

        if not client_summary.empty and len(client_summary) <= 15:
            fig_client = px.bar(
                client_summary,
                x="client_name",
                y="total_amount",
                title="Top Clients by Transaction Amount (Top 50)",
                labels={"client_name": "Client", "total_amount": "Amount (₹)"},
                color_discrete_sequence=["#10b981"]
            )
            fig_client = theme_plotly(fig_client, dark)
            fig_client.update_xaxes(tickangle=-45)
            st.plotly_chart(fig_client, width="stretch")

    # ── TAB 5: RTA-wise ──
    with tab_rta:
        st.subheader("🔵 RTA-wise Breakdown")

        rta_summary = (
            filtered_df.groupby("rta")
            .agg(
                transactions=("txn_no", "count"),
                total_units=("units", "sum"),
                total_amount=("amount", "sum"),
                avg_price=("price", "mean"),
                unique_clients=("client_name", "nunique"),
                unique_folios=("folio", "nunique"),
                unique_schemes=("scheme_name", "nunique"),
                unique_amcs=("amc_name", "nunique"),
            )
            .reset_index()
            .sort_values("total_amount", ascending=False)
        )

        rta_display = rta_summary.rename(columns={
            "rta": "RTA",
            "transactions": "Count",
            "total_units": "Total Units",
            "total_amount": "Total Amount",
            "avg_price": "Avg Price",
            "unique_clients": "Clients",
            "unique_folios": "Folios",
            "unique_schemes": "Schemes",
            "unique_amcs": "AMCs",
        })

        st.dataframe(
            rta_display,
            width="stretch",
            hide_index=True,
            column_config={
                "Total Units": st.column_config.NumberColumn(format="%.2f"),
                "Total Amount": st.column_config.NumberColumn(format="₹ %.2f"),
                "Avg Price": st.column_config.NumberColumn(format="₹ %.4f"),
            }
        )

        if not rta_summary.empty:
            col1, col2 = st.columns(2)

            with col1:
                fig_rta_amount = px.pie(
                    rta_summary,
                    values="total_amount",
                    names="rta",
                    title="Transaction Amount by RTA",
                    hole=0.4,
                    color_discrete_sequence=px.colors.qualitative.Bold
                )
                fig_rta_amount = theme_plotly(fig_rta_amount, dark)
                st.plotly_chart(fig_rta_amount, width="stretch")

            with col2:
                fig_rta_count = px.pie(
                    rta_summary,
                    values="transactions",
                    names="rta",
                    title="Transaction Count by RTA",
                    hole=0.4,
                    color_discrete_sequence=px.colors.qualitative.Bold
                )
                fig_rta_count = theme_plotly(fig_rta_count, dark)
                st.plotly_chart(fig_rta_count, width="stretch")

    # ═══════════════════════════════════════════════════════════
    # SECTION 4: DOWNLOAD ALL
    # ═══════════════════════════════════════════════════════════
    st.divider()
    st.subheader("📥 Download")

    csv_all = filtered_df.to_csv(index=False).encode("utf-8")
    st.download_button(
        "⬇️ Download All Filtered Transactions (CSV)",
        csv_all,
        f"transactions_all_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
        "text/csv",
        key="txn_all_download"
    )

# ==================== 💰 BROKERAGE REPORT ====================
elif mode == "💰 Brokerage Report":
    st.header("💰 Brokerage Report")
    st.caption(
        "File-reported brokerage (CAMS + KFin), AMC names resolved the same way as your Dashboard (AMFI-canonical via "
        "ISIN)."
    )

    data = load_brokerage_report(get_conn, data_version())
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

    # Investment-based AMCs (from folio holdings) so new AMCs auto-appear
    # when investments are uploaded, not just when brokerage files arrive.
    known_amcs = [a for a in load_active_amcs(data_version()) if a and not a.startswith("⚠️")]
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

        active_amcs = load_active_amcs(data_version())
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


elif mode == "📊 Reports":
    st.header("📊 Reports")

    report_options = ["📈 Portfolio Valuation Report", "🧮 Capital Gain Report"]
    if "reports_sub_mode" not in st.session_state:
        st.session_state["reports_sub_mode"] = report_options[0]

    sub_mode = st.radio(
        "Report Type", report_options,
        index=report_options.index(st.session_state["reports_sub_mode"]),
        horizontal=True, key="reports_sub_nav"
    )
    if sub_mode != st.session_state["reports_sub_mode"]:
        st.session_state["reports_sub_mode"] = sub_mode
        st.rerun()

    st.divider()

    # ═══════════════════════════════════════════════════════════
    # SUB-REPORT 1 — Portfolio Valuation Report
    # ═══════════════════════════════════════════════════════════

    if sub_mode == "📈 Portfolio Valuation Report":
        selected_display, selected_client = render_client_selector("val_report")
        
        if not selected_display or selected_client is None:
            st.info("Select a client to generate the report.")
            st.stop()
        
        client_code = selected_client['client_code']
        client_name = selected_client['name']
        pan = selected_client['pan']
        mobile = selected_client.get('mobile')

        # ── Family scope option ──
        family = get_family_for_client(client_code)
        report_scope = "individual"
        if family is not None:
            scope_choice = st.radio(
                "Report Scope",
                [f"👤 Individual — {client_name}", f"👨‍👩‍👧‍👦 Family — {family['family_name']}"],
                horizontal=True,
                key="val_report_scope"
            )
            report_scope = "family" if scope_choice.startswith("👨‍👩‍👧‍👦") else "individual"

        # ── FY Year selector ──
        today = date_cls.today()
        if today.month >= 4:
            current_fy_start = today.year
        else:
            current_fy_start = today.year - 1

        fy_options = [f"{y}-{str(y + 1)[-2:]}" for y in range(current_fy_start, current_fy_start - 8, -1)]
        selected_fy = st.selectbox("Financial Year", fy_options, key="val_fy_select")
        
        # ── Derive dates from FY ──
                # ── Derive dates from FY ──
        fy_start_year = int(selected_fy.split("-")[0])
        period_from = date_cls(fy_start_year, 4, 1)
        fy_end = date_cls(fy_start_year + 1, 3, 31)
        today = date_cls.today()

        # Default to FY-end, or today if the FY is still in progress
        default_val_date = min(fy_end, today)
        max_pickable = min(fy_end, today)  # never let user pick a future date

        val_date = st.date_input(
            "Valuation As-Of Date",
            value=default_val_date,
            min_value=period_from,
            max_value=max_pickable,
            key=f"val_asof_date_{selected_fy}",
            help="Defaults to FY-end, or today if this FY hasn't ended yet. "
                 "Pick any earlier date within the FY for a point-in-time valuation."
        )

        val_iso  = val_date.strftime("%Y-%m-%d")
        from_iso = period_from.strftime("%Y-%m-%d")
        val_ts   = pd.Timestamp(val_date)
        from_ts  = pd.Timestamp(period_from)

        if report_scope == "family":
            cams_folio_list, kfin_folio_list = get_family_folios_by_rta(family["family_id"])
            cams_f = pd.DataFrame({"foliochk": cams_folio_list})
            kfin_f = pd.DataFrame({"folio": kfin_folio_list})
            report_display_name = family["family_name"]
            report_pan = None
            report_mobile = None
            report_email_lookup_code = family["head_client_code"]
            member_count = len(get_family_members(family["family_id"]))
            st.caption(f"Family report — {member_count} member(s), "
                       f"{len(cams_folio_list)} CAMS + {len(kfin_folio_list)} KFinTech folios")
        else:
            is_minor = pd.isna(pan) or str(pan).strip() == ""
            match_pan = selected_client.get('guardian_pan') if is_minor else pan
            name_clean = client_name.strip().upper() if client_name else ""

            with get_conn() as conn:
                if is_minor:
                    cams_f = pd.read_sql(
                        "SELECT foliochk FROM cams_wbr9_folio WHERE TRIM(UPPER(inv_name)) LIKE ? || '%'",
                        conn, params=(name_clean,))
                    kfin_f = pd.read_sql(
                        "SELECT folio FROM kfin_mfsd211_folio WHERE TRIM(UPPER(investor_name)) LIKE ? || '%'",
                        conn, params=(name_clean,))
                else:
                    cams_f = pd.read_sql(
                        "SELECT foliochk FROM cams_wbr9_folio WHERE TRIM(UPPER(pan_no))=? OR TRIM(UPPER(inv_name))=?",
                        conn, params=(match_pan, client_name))
                    kfin_f = pd.read_sql(
                        "SELECT folio FROM kfin_mfsd211_folio WHERE TRIM(UPPER(pan_number))=? OR TRIM(UPPER(investor_name))=?",
                        conn, params=(match_pan, client_name))

            report_display_name = client_name
            report_pan = pan
            report_mobile = mobile
            report_email_lookup_code = client_code

        all_folios = set(cams_f['foliochk'].tolist() + kfin_f['folio'].tolist())
        investor_map = get_investor_names_for_folios(
        cams_f['foliochk'].tolist(), kfin_f['folio'].tolist()
        )

        if not all_folios:
            st.info("No folios found for this scope.")
            st.stop()

        # ── Folio → RTA map ──
        folio_rta_map = {}
        for f in cams_f['foliochk'].tolist():
            folio_rta_map[f] = 'CAMS'
        for f in kfin_f['folio'].tolist():
            folio_rta_map[f] = 'KFinTech'

        # ── Product code → ISIN + scheme name ──
        with get_conn() as conn:
            pc_info = pd.read_sql("""
                SELECT UPPER(TRIM(Channel_Partner_Code)) AS pc,
                       MAX(ISIN)        AS isin,
                       MAX(Scheme_Name) AS scheme_name
                FROM bse_scheme_master
                WHERE Channel_Partner_Code IS NOT NULL
                  AND TRIM(Channel_Partner_Code) != ''
                GROUP BY UPPER(TRIM(Channel_Partner_Code))
            """, conn)
        isin_lk = dict(zip(pc_info['pc'], pc_info['isin']))
        name_lk = dict(zip(pc_info['pc'], pc_info['scheme_name']))

    
        # ── NAV: use today's live NAV if valuing as of today, else historical ──
        with st.spinner(f"Loading NAV for {val_iso}…"):
            if val_date >= date_cls.today():
                download_and_save_nav_if_needed()
                raw_nav_map = _amfi.load()
                nav_map = {k: v[0] for k, v in raw_nav_map.items() if v[0] > 0}
            else:
                nav_map = get_or_fetch_nav_for_date(val_iso)

        if not nav_map:
            st.warning(
                f"⚠️ Could not load NAV for {val_iso}. "
                "Values will show as N/A."
            )

        # ── Process all folios ──
        all_scheme_rows = []
        rta_scheme_txns = {'CAMS': [], 'KFinTech': []}

        sorted_folios_list = sorted(all_folios)
        prog = st.progress(0, text="Processing folios…")

        for idx, folio_no in enumerate(sorted_folios_list):
            prog.progress(
                (idx + 1) / len(sorted_folios_list),
                text=f"Folio {folio_no}…"
            )
            rta = folio_rta_map.get(folio_no)
            if not rta:
                continue

            all_txn = fetch_all_folio_transactions(folio_no, rta)
            if all_txn.empty:
                continue

            if 'trxntype' not in all_txn.columns:
                all_txn['trxntype'] = ''

            for pc in all_txn['product_code'].unique():
                opening  = calc_units_before(all_txn, pc, from_ts)
                closing  = calc_units_upto(all_txn, pc, val_ts)
                invested = calc_invested_upto(all_txn, pc, val_ts)

                if closing <= 0 and opening <= 0:
                    continue

                isin  = isin_lk.get(pc)
                nav   = nav_map.get(str(isin).strip().upper()) if isin else None
                value = closing * nav if nav else None
                gain  = (value - invested) if value is not None else None
                sname = name_lk.get(pc, pc)

                investor_name = investor_map.get(folio_no, '')

                all_scheme_rows.append({
                    'Scheme':    sname,
                    'Folio':     folio_no,
                    'RTA':       rta,
                    'Investor':  investor_name,
                    'Invested':  invested,
                    'Value':     value,
                    'Gain/Loss': gain,
                })

                mask = (
                    (all_txn['product_code'] == pc)
                    & (all_txn['_date'] >= from_ts)
                    & (all_txn['_date'] <= val_ts)
                )
                pt = all_txn[mask].copy()
                if not pt.empty:
                    pt['Balance'] = opening + pt['signed_units'].cumsum()

                rta_key = rta if rta in rta_scheme_txns else 'CAMS'
                label = f"{investor_name} — {sname} ({folio_no})" if report_scope == "family" else f"{sname} ({folio_no})"
                rta_scheme_txns[rta_key].append({
                    'label':   label,
                    'opening': opening,
                    'df':      pt,
                })

        prog.empty()

        # ═══════════════════════════════════════════════
        #  REPORT HEADER
        # ═══════════════════════════════════════════════
        st.divider()
        hc1, hc2, hc3 = st.columns(3)
        with hc1:
            st.markdown(f"**{report_display_name}**")
            if report_scope == "family":
                st.caption("Family Portfolio")
            else:
                st.caption(f"PAN: `{report_pan or 'Minor'}`  |  Code: `{client_code}`")
        with hc2:
            st.markdown(f"**FY {selected_fy}**")
            st.caption(f"{from_iso} → {val_iso}")
        with hc3:
            if report_mobile:
                st.caption(f"📱 {report_mobile}")
        st.divider()

        if not all_scheme_rows:
            st.info("No holdings found for the selected period.")
            st.stop()

        # ═══════════════════════════════════════════════
        #  SCHEME SUMMARY
        # ═══════════════════════════════════════════════
        sm_df = pd.DataFrame(all_scheme_rows)
        sm_df = sm_df.sort_values('Invested', ascending=False, na_position='last').reset_index(drop=True)

        t_inv  = sm_df['Invested'].sum()
        t_val  = sm_df['Value'].sum()
        t_gain = t_val - t_inv if t_val is not None else None

        m1, m2, m3 = st.columns(3)
        m1.metric("Total Invested", format_aum(t_inv))
        m2.metric("Total Value", format_aum(t_val))
        m3.metric(
            "Gain/Loss",
            format_aum(t_gain) if t_gain is not None else "N/A",
            delta=(
                f"{(t_gain / t_inv * 100):.2f}%"
                if t_gain is not None and t_inv > 0 else None
            ),
        )

        sm_df['Return %'] = sm_df.apply(
            lambda r: (
                f"{(r['Gain/Loss'] / r['Invested'] * 100):.2f}%"
                if r['Gain/Loss'] is not None and r['Invested'] > 0
                else "N/A"
            ),
            axis=1,
        )

        summary_cols = []
        if report_scope == "family":
            summary_cols.append('Investor')
        summary_cols += ['Scheme', 'Folio', 'Invested', 'Value', 'Gain/Loss', 'Return %']

        display_sm = sm_df[summary_cols].copy()

        total_row_dict = {
            'Scheme':    'TOTAL',
            'Folio':     f"{sm_df['Folio'].nunique()} folios",
            'Invested':  t_inv,
            'Value':     t_val,
            'Gain/Loss': t_gain,
            'Return %': (
                f"{(t_gain / t_inv * 100):.2f}%"
                if t_gain is not None and t_inv > 0 else "N/A"
            ),
        }
        if report_scope == "family":
            total_row_dict['Investor'] = f"{len(get_family_members(family['family_id']))} members"
        total_row = pd.DataFrame([total_row_dict])[summary_cols]
        display_sm = pd.concat([display_sm, total_row], ignore_index=True)

        st.dataframe(
            display_sm,
            width="stretch",
            hide_index=True,
            column_config={
                "Invested":  st.column_config.NumberColumn(format="₹ %.2f"),
                "Value":     st.column_config.NumberColumn(format="₹ %.2f"),
                "Gain/Loss": st.column_config.NumberColumn(format="₹ %.2f"),
            },
        )

        st.divider()

        # ═══════════════════════════════════════════════
        #  TRANSACTIONS (RTA-grouped)
        # ═══════════════════════════════════════════════
        st.markdown("### 📑 Transactions")
        st.caption(f"Period: {from_iso} → {val_iso}  |  Opening balance as on {from_iso}")

        all_entries = rta_scheme_txns.get('CAMS', []) + rta_scheme_txns.get('KFinTech', [])
        all_entries.sort(key=lambda e: e['label'])

        if all_entries:
            with st.expander(
                f"▶ All Schemes ({len(all_entries)} scheme"
                f"{'s' if len(all_entries) != 1 else ''})",
                expanded=True,
            ):
                for entry in all_entries:
                    label   = entry['label']
                    opening = entry['opening']
                    tdf     = entry['df']

                    st.markdown(
                        f"**{label}** — Opening: **{opening:.4f}** units"
                    )

                    if tdf.empty:
                        st.caption("_(no transactions during this period)_")
                    else:
                        show_tdf = tdf[[
                            '_date', 'trxntype', 'signed_units', 'amount', 'Balance'
                        ]].copy()
                        show_tdf['Date'] = show_tdf['_date'].dt.strftime('%Y-%m-%d')
                        show_tdf = show_tdf[[
                            'Date', 'trxntype', 'signed_units', 'amount', 'Balance'
                        ]].rename(columns={
                            'trxntype':     'Type',
                            'signed_units': 'Units',
                            'amount':       'Amount',
                            'Balance':      'Balance',
                        })

                        st.dataframe(
                            show_tdf,
                            width="stretch",
                            hide_index=True,
                            column_config={
                                "Units":   st.column_config.NumberColumn(format="%.4f"),
                                "Amount":  st.column_config.NumberColumn(format="₹ %.2f"),
                                "Balance": st.column_config.NumberColumn(format="%.4f"),
                            },
                        )

                    st.caption("")

        # ═══════════════════════════════════════════════
        #  DOWNLOAD BUTTONS
        # ═══════════════════════════════════════════════
        st.divider()
        st.markdown("### 📥 Download Report")

        html_content = generate_valuation_html(
            report_display_name, report_pan, client_code, report_mobile,
            val_iso, from_iso,
            all_scheme_rows, rta_scheme_txns,
            t_inv, t_val, t_gain if t_gain is not None else 0,
            show_investor=(report_scope == "family"),
        )
        pdf_bytes = generate_valuation_pdf(
            report_display_name, report_pan, client_code, report_mobile,
            val_iso, from_iso,
            all_scheme_rows, rta_scheme_txns,
            t_inv, t_val, t_gain if t_gain is not None else 0,
            show_investor=(report_scope == "family"),
        )

        if pdf_bytes:
            st.download_button(
                label="📑 Download PDF",
                data=pdf_bytes,
                file_name=f"Valuation_{report_display_name.replace(' ', '_')}_{selected_fy}.pdf",
                mime="application/pdf",
                width="stretch",
            )
        else:
            st.warning("PDF generation unavailable (fpdf2/font missing).")

        render_email_report_button(
            client_code=report_email_lookup_code,
            client_name=report_display_name,
            report_type="Valuation",
            html_content=html_content,
            pdf_content=pdf_bytes,
            key_prefix="val_email",
        )


    # ═══════════════════════════════════════════════════════════
    # SUB-REPORT 2 — Capital Gain Report
    # ═══════════════════════════════════════════════════════════

    elif sub_mode == "🧮 Capital Gain Report":
        st.subheader("🧮 Capital Gain Report")
        st.caption("Realized gains (CAMS, FIFO). KFinTech redemption tracking isn't available yet.")

        # ── Client selector (same pattern as Valuation Report) ──
        selected_display, selected_client = render_client_selector("cg_report", exclude_minors=False)

        if not selected_display or selected_client is None:
            st.info("Select a client to generate the capital gain report.")
            st.stop()

        client_code = selected_client['client_code']
        client_name = selected_client['name']
        pan = selected_client['pan']
        mobile = selected_client.get('mobile')

        is_minor = pd.isna(pan) or str(pan).strip() == ""
        match_pan = selected_client.get('guardian_pan') if is_minor else pan
        name_clean = client_name.strip().upper() if client_name else ""

        # ── FY selector ──
        today = date_cls.today()
        current_fy_start = today.year if today.month >= 4 else today.year - 1
        fy_options = ["All Time"] + [f"{y}-{str(y+1)[-2:]}" for y in range(current_fy_start, current_fy_start - 8, -1)]
        cgr_fy = st.selectbox("FY (by sale date)", fy_options, key="cgr_fy_select")

        if cgr_fy != "All Time":
            fy_start_year = int(cgr_fy.split("-")[0])
            cgr_from, cgr_to = date_cls(fy_start_year, 4, 1), date_cls(fy_start_year + 1, 3, 31)
        else:
            cgr_from = cgr_to = None

        def _extract_match_dates(m):
            """
            Pull whatever date-like fields exist on the match object, regardless
            of what cg.py names them. Sorted chronologically: earliest = buy date,
            latest = sale date.
            """
            candidates = []
            for attr in dir(m):
                if attr.startswith('_'):
                    continue
                try:
                    val = getattr(m, attr)
                except Exception:
                    continue
                if isinstance(val, (datetime, pd.Timestamp, date_cls)):
                    candidates.append(pd.Timestamp(val))
            candidates = sorted(candidates)
            if len(candidates) >= 2:
                return candidates[0], candidates[-1]
            elif len(candidates) == 1:
                return candidates[0], candidates[0]
            return None, None

        # ── Generate button ──
        gen_clicked = st.button("🔄 Generate Report", type="primary")

        if not gen_clicked:
            st.info("Click **Generate Report** after selecting the client and FY.")
            st.stop()

        with st.spinner(f"Computing realized gains for {client_name}…"):
            # Fetch folios
            with get_conn() as conn:
                if is_minor:
                    cams_folios = pd.read_sql(
                        "SELECT foliochk FROM cams_wbr9_folio WHERE TRIM(UPPER(inv_name)) LIKE ? || '%'",
                        conn, params=(name_clean,)
                    )["foliochk"].tolist()
                else:
                    cams_folios = pd.read_sql(
                        "SELECT foliochk FROM cams_wbr9_folio WHERE TRIM(UPPER(pan_no))=? OR TRIM(UPPER(inv_name))=?",
                        conn, params=(str(match_pan).upper(), name_clean)
                    )["foliochk"].tolist()

            if not cams_folios:
                st.info("No CAMS folios found for this client.")
                st.stop()

            schemes_df = get_client_cams_schemes(cams_folios, data_version())
            if schemes_df.empty:
                st.info("No schemes with transactions found for this client.")
                st.stop()

            detail_rows = []
            for _, srow in schemes_df.iterrows():
                folio_no, prodcode, scheme_name = srow["folio_no"], srow["prodcode"], srow["scheme"]
                txns = get_cams_txns_raw(folio_no, prodcode)
                if txns.empty:
                    continue

                lots, matches = cg.replay_folio_scheme(txns)
                if not matches:
                    continue

                for m in matches:
                    buy_date, sale_date = _extract_match_dates(m)
                    sd = sale_date.date() if sale_date is not None else None

                    if cgr_from and sd and not (cgr_from <= sd <= cgr_to):
                        continue

                    detail_rows.append({
                        "Scheme": scheme_name,
                        "Folio": folio_no,
                        "Buy Date": buy_date.date() if buy_date is not None else None,
                        "Buy Units": m.units,
                        "Buy NAV": (m.cost / m.units) if m.units else 0,
                        "Buy Value": m.cost,
                        "Sale Date": sd,
                        "Sell Units": m.units,
                        "Sell NAV": (m.proceeds / m.units) if m.units else 0,
                        "Sell Value": m.proceeds,
                        "Gain/Loss": m.gain,
                    })

        if not detail_rows:
            st.info("No realized capital gains found for the selected period.")
            st.stop()

        detail_df = pd.DataFrame(detail_rows)
        d_buy = detail_df["Buy Value"].sum()
        d_sale = detail_df["Sell Value"].sum()
        d_gain = d_sale - d_buy

        # ═══════════════════════════════════════════════
        #  REPORT HEADER
        # ═══════════════════════════════════════════════
        st.divider()
        hc1, hc2, hc3 = st.columns(3)
        with hc1:
            st.markdown(f"**{client_name}**")
            st.caption(f"PAN: `{pan or 'Minor'}`  |  Code: `{client_code}`")
        with hc2:
            st.markdown(f"**FY {cgr_fy}**")
        with hc3:
            if mobile:
                st.caption(f"📱 {mobile}")
        st.divider()

        # ═══════════════════════════════════════════════
        #  METRICS
        # ═══════════════════════════════════════════════
        m1, m2, m3 = st.columns(3)
        m1.metric("Total Buy", format_currency(d_buy))
        m2.metric("Total Sale", format_currency(d_sale))
        m3.metric("Total Gain/Loss", format_currency(d_gain))

        # ═══════════════════════════════════════════════
        #  DETAIL TABLE
        # ═══════════════════════════════════════════════
        cols = ["Scheme", "Folio", "Buy Date", "Buy Units", "Buy NAV", "Buy Value",
                "Sale Date", "Sell Units", "Sell NAV", "Sell Value", "Gain/Loss"]

        st.dataframe(
            detail_df[cols].sort_values("Sale Date", ascending=False, na_position="last"),
            width="stretch",
            hide_index=True,
            column_config={
                "Buy Units": st.column_config.NumberColumn(format="%.4f"),
                "Buy NAV": st.column_config.NumberColumn(format="₹ %.4f"),
                "Buy Value": st.column_config.NumberColumn(format="₹ %.2f"),
                "Sell Units": st.column_config.NumberColumn(format="%.4f"),
                "Sell NAV": st.column_config.NumberColumn(format="₹ %.4f"),
                "Sell Value": st.column_config.NumberColumn(format="₹ %.2f"),
                "Gain/Loss": st.column_config.NumberColumn(format="₹ %.2f"),
            }
        )

        # ═══════════════════════════════════════════════
        #  DOWNLOADS
        # ═══════════════════════════════════════════════
        st.divider()
        st.markdown("### 📥 Download Report")

        dl1, dl2 = st.columns(2)

        with dl1:
            csv = detail_df[cols].to_csv(index=False).encode("utf-8")
            st.download_button(
                label="📊 Download Excel (CSV)",
                data=csv,
                file_name=f"CapitalGain_{client_name.replace(' ', '_')}_{cgr_fy}.csv",
                mime="text/csv",
                width="stretch",
            )

        html_content = generate_capital_gain_html(
            client_name, pan, client_code, cgr_fy,
            detail_rows, d_buy, d_sale, d_gain
        )
        pdf_bytes = generate_capital_gain_pdf(
            client_name, pan, client_code, cgr_fy,
            detail_rows, d_buy, d_sale, d_gain
        )

        with dl2:
            if pdf_bytes:
                st.download_button(
                    label="📑 Download PDF",
                    data=pdf_bytes,
                    file_name=f"CapitalGain_{client_name.replace(' ', '_')}_{cgr_fy}.pdf",
                    mime="application/pdf",
                    width="stretch",
                )
            else:
                st.warning("PDF generation unavailable (fpdf2/font missing).")

        render_email_report_button(
            client_code=client_code,
            client_name=client_name,
            report_type="Capital Gain",
            fy_str=cgr_fy,
            html_content=html_content,
            pdf_content=pdf_bytes,
            key_prefix="cg_email",
        )


# ==================== 🧮 CAPITAL GAINS ====================
elif mode == "🧮 Capital Gains":
    st.header("🧮 Capital Gains (CAMS, FIFO)")
    st.caption(...)
    
    selected_display, selected_client = render_client_selector("cg_tab", exclude_minors=True)
    
    if not selected_display or selected_client is None:
        st.info("Select a client with valid PAN.")
        st.stop()
    
    cg_pan = selected_client['pan']
    cg_name = selected_client['name']
    
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

    cams_schemes = get_client_cams_schemes(cams_folio_ids, data_version())
    cams_schemes["rta"] = "CAMS"

    kfin_schemes = get_client_kfin_schemes(kfin_folio_ids, data_version())
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
        tab2, tab3 = st.tabs(["🔮 What-if Redemption", "❓ What if Not Redeemed"])
        tab1 = None
    else:
        tab1, tab2, tab3 = st.tabs(["✅ Realized Gains", "🔮 What-if Redemption", "❓ What if Not Redeemed"])

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
                cg_nav_df = get_all_folios_with_isin_and_nav(get_conn, data_version())
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

    # ═══════════════════════════════════════════════════════════════════════════
    # TAB 3: What if NOT Redeemed — Current Value Scenario
    # ═══════════════════════════════════════════════════════════════════════════
    with tab3:
        st.subheader("📊 Scenario: Units Were NOT Redeemed")
        st.caption("Shows current value if redemptions had NOT happened — compare to actual realized gains above.")

        if not matches and not is_kfin:
            st.info("No redemptions found — all units already held as-is.")
        else:
            # Get current NAV
            cg_nav_df = st.session_state.get("folio_nav_df")
            if cg_nav_df is None:
                download_and_save_nav_if_needed()
                cg_nav_df = get_all_folios_with_isin_and_nav(get_conn, data_version())
                st.session_state["folio_nav_df"] = cg_nav_df

            nav_match = cg_nav_df[
                (cg_nav_df["folio_id"] == folio_no) &
                (cg_nav_df["product_code"].astype(str).str.strip().str.upper() == prodcode.strip().upper())
                ]
            current_nav = float(nav_match["current_nav"].iloc[0]) if not nav_match.empty and pd.notna(
                nav_match["current_nav"].iloc[0]) else 0.0
            nav_date = nav_match["nav_date"].iloc[0] if not nav_match.empty and pd.notna(
                nav_match["nav_date"].iloc[0]) else "Unknown"

            if current_nav <= 0:
                st.warning("⚠️ Current NAV not available. Cannot calculate current value scenario.")
            else:
                # Calculate what was redeemed
                total_redeemed_units = sum(m.units for m in matches)
                total_redeemed_cost = sum(m.cost for m in matches)
                total_redeemed_proceeds = sum(m.proceeds for m in matches)
                total_redeemed_gain = total_redeemed_proceeds - total_redeemed_cost

                # Current value of those redeemed units (if NOT redeemed)
                unredeemed_units_current_value = total_redeemed_units * current_nav

                # Total folio current value = current holdings + unredeemed units
                total_current_units = sum(l.remaining_units for l in lots) + total_redeemed_units
                total_folio_current_value = total_current_units * current_nav

                # Total invested across all units (both held and redeemed)
                all_invested = sum(l.remaining_units * l.rate for l in lots) + total_redeemed_cost

                # Unrealized gain if nothing was redeemed
                total_unrealized_if_not_redeemed = total_folio_current_value - all_invested

                st.markdown("### 1️⃣ Redeemed Units — Current Value (if NOT redeemed)")
                st.markdown(
                    f"**Units redeemed:** {total_redeemed_units:.4f} | **Current NAV:** ₹{current_nav:.4f} (as of {nav_date})")

                nr1, nr2, nr3, nr4 = st.columns(4)
                nr1.metric("Total Cost (Invested)", format_currency(total_redeemed_cost))
                nr2.metric("Actual Proceeds", format_currency(total_redeemed_proceeds))
                nr3.metric("Realized Gain", format_currency(total_redeemed_gain))
                nr4.metric("Current Value If Not Redeemed", format_currency(unredeemed_units_current_value))

                # Show comparison
                st.divider()
                st.markdown("### 2️⃣ Total Folio — Current Value Scenario")
                st.markdown(
                    f"**Total units if NOT redeemed:** {total_current_units:.4f} | **Current NAV:** ₹{current_nav:.4f}")

                tf1, tf2, tf3 = st.columns(3)
                tf1.metric("Total Invested (All Units)", format_currency(all_invested))
                tf2.metric("Total Current Value", format_currency(total_folio_current_value))
                tf3.metric("Total Unrealized Gain (If NOT Redeemed)",
                        format_currency(total_unrealized_if_not_redeemed))

                # Impact analysis
                st.divider()
                st.markdown("### 📈 Impact Analysis")

                impact_col1, impact_col2, impact_col3 = st.columns(3)

                with impact_col1:
                    st.markdown("**Redemption Impact**")
                    st.metric(
                        "Value Lost by Redeeming",
                        format_currency(unredeemed_units_current_value - total_redeemed_proceeds),
                        delta=format_currency(unredeemed_units_current_value - total_redeemed_proceeds)
                    )
                    st.caption(
                        f"= Current value of redeemed units ({format_currency(unredeemed_units_current_value)}) − What you received ({format_currency(total_redeemed_proceeds)})")

                with impact_col2:
                    st.markdown("**Folio Comparison**")
                    current_holdings = sum(l.remaining_units * current_nav for l in lots)
                    holdings_invested = sum(l.remaining_units * l.rate for l in lots)
                    holdings_gain = current_holdings - holdings_invested

                    st.metric("Current Holdings Gain", format_currency(holdings_gain))
                    st.caption("What you have today (excluding redeemed units)")

                with impact_col3:
                    st.markdown("**Tax Consideration**")
                    st.metric(
                        "Tax Paid on Redemption",
                        format_currency(sum(m.proceeds - m.cost for m in matches) * slab if tax_cat == "debt" else
                                        sum(m.gain for m in matches if m.is_ltcg) * cg.LTCG_EQUITY_RATE +
                                        sum(m.gain for m in matches if not m.is_ltcg) * cg.STCG_EQUITY_RATE)
                    )
                    st.caption("Tax impact of selling vs holding")

                # Summary table
                st.divider()
                st.markdown("### 📋 Scenario Comparison")

                comparison_data = {
                    "Metric": [
                        "Units Held",
                        "Current Value",
                        "Total Invested",
                        "Unrealized Gain",
                        "Tax Paid (if redeemed)",
                    ],
                    "Actual (Redeemed)": [
                        f"{sum(l.remaining_units for l in lots):.4f}",
                        format_currency(sum(l.remaining_units * current_nav for l in lots)),
                        format_currency(sum(l.remaining_units * l.rate for l in lots)),
                        format_currency(sum(l.remaining_units * current_nav for l in lots) - sum(
                            l.remaining_units * l.rate for l in lots)),
                        format_currency(sum(m.gain for m in matches) * slab if tax_cat == "debt" else
                                        sum(m.gain for m in matches if m.is_ltcg) * cg.LTCG_EQUITY_RATE +
                                        sum(m.gain for m in matches if not m.is_ltcg) * cg.STCG_EQUITY_RATE),
                    ],
                    "If NOT Redeemed": [
                        f"{total_current_units:.4f}",
                        format_currency(total_folio_current_value),
                        format_currency(all_invested),
                        format_currency(total_unrealized_if_not_redeemed),
                        format_currency(0.0),
                    ]
                }

                comparison_df = pd.DataFrame(comparison_data)
                st.dataframe(comparison_df, width="stretch", hide_index=True)

                st.divider()
                st.caption(
                    "💡 **Insight:** This shows the opportunity cost of redemption. "
                    "If the redeemed units have appreciated since redemption, holding them would have been more valuable. "
                    "However, you've locked in your gains and avoided further market risk."
                )


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

    # st.subheader("🔁 Background Automation")


    # ac1, ac2 = st.columns(2)

    # with ac1:
    #     with st.expander("📬 Mailback Auto-Sync (CAMS + KFinTech)", expanded=False):            
    #         cams_mailback_sync.render_settings_ui()

    # with ac2:
    #     nav_sched_on = st.toggle(            
    #         "📈 Auto NAV download (11 AM & 3 PM daily)",            
    #         value=nav_scheduler.is_nav_schedule_enabled(get_conn),            
    #         key="nav_sched_toggle"        
    #         )
    # if nav_sched_on != nav_scheduler.is_nav_schedule_enabled(get_conn):            
    #     nav_scheduler.set_nav_schedule_enabled(get_conn, nav_sched_on)            
    #     st.rerun()

    # st.divider()

    nav_sched_on = st.toggle(            
        "📈 Auto NAV download (11 AM & 3 PM daily)",            
        value=nav_scheduler.is_nav_schedule_enabled(get_conn),            
        key="nav_sched_toggle"        
    )
    if nav_sched_on != nav_scheduler.is_nav_schedule_enabled(get_conn):            
        nav_scheduler.set_nav_schedule_enabled(get_conn, nav_sched_on)            
        st.rerun()

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
            df_raw = load_table_summary(table, data_version)
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
        stats = load_db_stats(data_version())

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
