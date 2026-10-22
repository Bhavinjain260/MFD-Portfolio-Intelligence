"""
cache_functions.py - COMPLETE with transaction caching
"""

import logging
import streamlit as st
import pandas as pd
from datetime import datetime

log = logging.getLogger("cache_functions")


# ════════════════════════════════════════════════════════════════
# CORE CACHING STRATEGY: Version-based cache busting
# ════════════════════════════════════════════════════════════════

def get_version(domain: str) -> int:
    """
    Get current version for a domain (CAMS, KFIN, NAV, etc).
    Pulls from session state (set by data_manager.py on upload).
    
    Session state keys: "version_cams", "version_kfin", "version_nav", "version_brokerage"
    """
    key = f"version_{domain}"
    if key not in st.session_state:
        st.session_state[key] = 0
    return st.session_state[key]


# ════════════════════════════════════════════════════════════════
# CRITICAL: Client Transaction Cache (solves the redundancy)
# ════════════════════════════════════════════════════════════════

@st.cache_data(show_spinner=False)
def get_client_all_transactions(
    get_conn,
    client_code: str,
    _cams_v: int,
    _kfin_v: int,
):
    """
    CRITICAL FUNCTION - Batch fetch ALL transactions for a client.
    Cache key busts when CAMS or KFIN data changes.
    
    Returns: {
        'cams_txns': DataFrame,           # All CAMS txns for client's folios
        'kfin_txns': DataFrame,           # All KFIN txns for client's folios
        'cams_folios': list,              # Client's CAMS folio IDs
        'kfin_folios': list,              # Client's KFIN folio IDs
        'folio_rta_map': dict,            # {folio_id: 'CAMS'|'KFinTech'}
        'scheme_map': dict,               # {prodcode: scheme_name}
    }
    """
    log.info(f"[CACHE] Loading transactions for client {client_code} (CAMS v{_cams_v}, KFIN v{_kfin_v})")
    
    # Step 1: Get client identity & find folios
    with get_conn() as conn:
        client_row = conn.execute("""
            SELECT primary_holder_first_name, primary_holder_last_name,
                   primary_holder_pan, guardian_pan
            FROM bse_client_master
            WHERE client_code = ?
        """, (client_code,)).fetchone()
    
    if not client_row:
        log.warning(f"Client {client_code} not found")
        return {
            'cams_txns': pd.DataFrame(),
            'kfin_txns': pd.DataFrame(),
            'cams_folios': [],
            'kfin_folios': [],
            'folio_rta_map': {},
            'scheme_map': {},
        }
    
    name = f"{client_row['primary_holder_first_name']} {client_row['primary_holder_last_name']}".strip().upper()
    pan = client_row['primary_holder_pan']
    is_minor = pd.isna(pan) or str(pan).strip() == ""
    match_pan = client_row['guardian_pan'] if is_minor else pan
    
    # Step 2: Batch fetch all folios for this client
    with get_conn() as conn:
        cams_f = pd.read_sql("""
            SELECT foliochk FROM cams_wbr9_folio
            WHERE TRIM(UPPER(pan_no))=? OR TRIM(UPPER(inv_name)) LIKE ? || '%'
        """, conn, params=(str(match_pan).upper() if match_pan else "", name))
        
        kfin_f = pd.read_sql("""
            SELECT folio FROM kfin_mfsd211_folio
            WHERE TRIM(UPPER(pan_number))=? OR TRIM(UPPER(investor_name)) LIKE ? || '%'
        """, conn, params=(str(match_pan).upper() if match_pan else "", name))
    
    cams_folios = cams_f['foliochk'].tolist() if not cams_f.empty else []
    kfin_folios = kfin_f['folio'].tolist() if not kfin_f.empty else []
    
    folio_rta_map = {f: 'CAMS' for f in cams_folios} | {f: 'KFinTech' for f in kfin_folios}
    
    # Step 3: Batch fetch all transactions (SINGLE QUERY PER RTA)
    cams_txns = pd.DataFrame()
    kfin_txns = pd.DataFrame()
    
    with get_conn() as conn:
        if cams_folios:
            ph = ','.join(['?'] * len(cams_folios))
            cams_txns = pd.read_sql(f"""
                SELECT folio_no, traddate, trxntype, trxnmode, trxnstat,
                       purprice, units, amount, prodcode, brokcode, subbrok, remarks, trxnno
                FROM cams_wbr2_transaction
                WHERE folio_no IN ({ph})
                ORDER BY folio_no, traddate
            """, conn, params=cams_folios)
        
        if kfin_folios:
            ph = ','.join(['?'] * len(kfin_folios))
            kfin_txns = pd.read_sql(f"""
                SELECT td_acno as folio_no, td_trdt as traddate, td_purred as trxntype,
                       trnmode as trxnmode, trnstat as trxnstat, td_pop as purprice,
                       td_units as units, td_amt as amount, fmcode as prodcode,
                       td_broker as brokcode, '' as subbrok, trdesc as remarks, td_trno as trxnno
                FROM kfin_mfsd201_transaction
                WHERE td_acno IN ({ph})
                ORDER BY td_acno, td_trdt
            """, conn, params=kfin_folios)
    
    # Step 4: Resolve scheme names
    scheme_map = {}
    if not cams_txns.empty or not kfin_txns.empty:
        all_prodcodes = (
            cams_txns['prodcode'].unique().tolist() +
            kfin_txns['prodcode'].unique().tolist()
        )
        if all_prodcodes:
            ph = ','.join(['?'] * len(all_prodcodes))
            with get_conn() as conn:
                scheme_rows = conn.execute(f"""
                    SELECT UPPER(TRIM(Channel_Partner_Code)) as pc, MAX(Scheme_Name) as sn
                    FROM bse_scheme_master
                    WHERE Channel_Partner_Code IS NOT NULL
                    GROUP BY UPPER(TRIM(Channel_Partner_Code))
                """).fetchall()
            scheme_map = {row[0]: row[1] for row in scheme_rows}
    
    log.info(f"[CACHE] Loaded {len(cams_txns)} CAMS + {len(kfin_txns)} KFIN txns for client {client_code}")
    
    return {
        'cams_txns': cams_txns,
        'kfin_txns': kfin_txns,
        'cams_folios': cams_folios,
        'kfin_folios': kfin_folios,
        'folio_rta_map': folio_rta_map,
        'scheme_map': scheme_map,
    }


@st.cache_data(show_spinner=False)
def load_all_transactions_for_explorer(
    get_conn,
    _cams_v: int,
    _kfin_v: int,
):
    """
    Load ALL transactions (all clients, all folios) for Transactions tab.
    Used by the global transactions explorer that needs to search across everyone.
    """
    log.info(f"[CACHE] Loading all transactions for explorer (CAMS v{_cams_v}, KFIN v{_kfin_v})")
    
    with get_conn() as conn:
        cams_all = pd.read_sql("""
            SELECT 'CAMS' as rta, folio_no, inv_name as client_name, prodcode, traddate,
                   trxntype, trxnmode, trxnstat, units, purprice, amount,
                   brokcode, subbrok, remarks, trxnno
            FROM cams_wbr2_transaction
            ORDER BY traddate DESC
        """, conn)
        
        kfin_all = pd.read_sql("""
            SELECT 'KFinTech' as rta, td_acno as folio_no, investor_name as client_name,
                   fmcode as prodcode, td_trdt as traddate, td_purred as trxntype,
                   trnmode as trxnmode, trnstat as trxnstat, td_units as units,
                   td_pop as purprice, td_amt as amount, td_broker as brokcode,
                   '' as subbrok, trdesc as remarks, td_trno as trxnno
            FROM kfin_mfsd201_transaction
            ORDER BY td_trdt DESC
        """, conn)
    
    all_txn = pd.concat([cams_all, kfin_all], ignore_index=True)
    log.info(f"[CACHE] Loaded {len(all_txn)} total transactions for explorer")
    return all_txn


# ════════════════════════════════════════════════════════════════
# Convenience: Get specific folio's transactions (lightweight)
# ════════════════════════════════════════════════════════════════

@st.cache_data(show_spinner=False)
def get_folio_transactions(
    get_conn,
    folio_no: str,
    rta: str,
    _v: int,
):
    """
    Lightweight: fetch single folio's transactions.
    Use when you ONLY need one folio (e.g., detail drilldown in Portfolio tab).
    """
    with get_conn() as conn:
        if rta == 'CAMS':
            df = pd.read_sql("""
                SELECT folio_no, traddate, trxntype, trxnmode, trxnstat,
                       purprice, units, amount, prodcode
                FROM cams_wbr2_transaction
                WHERE folio_no = ?
                ORDER BY traddate
            """, conn, params=(folio_no,))
        else:
            df = pd.read_sql("""
                SELECT td_acno as folio_no, td_trdt as traddate, td_purred as trxntype,
                       trnmode as trxnmode, trnstat as trxnstat, td_pop as purprice,
                       td_units as units, td_amt as amount, fmcode as prodcode
                FROM kfin_mfsd201_transaction
                WHERE td_acno = ?
                ORDER BY td_trdt
            """, conn, params=(folio_no,))
    return df


# ════════════════════════════════════════════════════════════════
# Computed caches (invested amounts, capital gains)
# ════════════════════════════════════════════════════════════════

@st.cache_data(show_spinner=False)
def get_cams_invested_per_scheme(
    _cams_folio_set: tuple,
    _cams_v: int,
):
    """
    Compute CAMS invested per scheme across multiple folios.
    Pass tuple of sorted folio IDs for stable cache key.
    
    IMPORTANT: Pass SORTED tuple so cache key is stable across reruns
    """
    if not _cams_folio_set:
        return {}
    
    result = {}
    for folio_id in _cams_folio_set:
        result[folio_id] = _replay_cams_folio_scheme_impl(folio_id)
    return result


@st.cache_data(show_spinner=False)
def get_kfin_invested_per_scheme(
    _kfin_folio_set: tuple,
    _kfin_v: int,
):
    """
    Compute KFIN invested per scheme across multiple folios.
    Pass tuple of sorted folio IDs for stable cache key.
    """
    if not _kfin_folio_set:
        return {}
    
    result = {}
    for folio_id in _kfin_folio_set:
        result[folio_id] = _replay_kfin_folio_scheme_impl(folio_id)
    return result


# ════════════════════════════════════════════════════════════════
# Non-cached helpers (actual computation)
# ════════════════════════════════════════════════════════════════

def _replay_cams_folio_scheme_impl(folio_id: str) -> dict:
    """Compute CAMS invested per scheme (no caching - called from cache layer)."""
    try:
        from init_db import get_conn
        from capital_gain import replay_folio_scheme
        
        with get_conn() as conn:
            txns = pd.read_sql("""
                SELECT traddate, trxntype, trxn_nature, units, purprice, amount, prodcode
                FROM cams_wbr2_transaction
                WHERE folio_no = ?
                ORDER BY traddate
            """, conn, params=(folio_id,))
        
        if txns.empty:
            return {}
        
        result = {}
        for prodcode, scheme_txns in txns.groupby("prodcode"):
            lots, _ = replay_folio_scheme(scheme_txns)
            remaining_invested = sum(lot.remaining_units * lot.rate for lot in lots)
            result[prodcode] = round(remaining_invested, 2)
        
        return result
    except Exception as e:
        log.exception(f"CAMS replay failed for {folio_id}: {e}")
        return {}


def _replay_kfin_folio_scheme_impl(folio_id: str) -> dict:
    """Compute KFIN invested per scheme (no caching - called from cache layer)."""
    try:
        from init_db import get_conn
        
        with get_conn() as conn:
            txns = pd.read_sql("""
                SELECT td_trdt as traddate, td_units as units, td_pop as rate, fmcode as prodcode
                FROM kfin_mfsd201_transaction
                WHERE td_acno = ?
                ORDER BY td_trdt
            """, conn, params=(folio_id,))
        
        if txns.empty:
            return {}
        
        txns["traddate"] = pd.to_datetime(txns["traddate"], format="%Y-%m-%d", errors="coerce")
        txns = txns.dropna(subset=["traddate"])
        
        result = {}
        for prodcode, scheme_txns in txns.groupby("prodcode"):
            lots = []
            for _, txn in scheme_txns.iterrows():
                units, rate = float(txn["units"]), float(txn.get("rate", 0))
                if units > 0:
                    lots.append({"units": units, "rate": rate, "remaining": units})
                else:
                    redeem = abs(units)
                    for lot in lots:
                        if lot["remaining"] <= 1e-9:
                            continue
                        take = min(lot["remaining"], redeem)
                        lot["remaining"] -= take
                        redeem -= take
                        if redeem <= 1e-9:
                            break
            
            remaining_invested = sum(lot["remaining"] * lot["rate"] for lot in lots)
            result[prodcode] = round(remaining_invested, 2)
        
        return result
    except Exception as e:
        log.exception(f"KFIN replay failed for {folio_id}: {e}")
        return {}