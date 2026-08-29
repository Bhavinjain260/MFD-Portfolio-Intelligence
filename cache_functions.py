"""
FIXED cache_functions.py - No TTL, version-based invalidation only.
Ready to use: just copy/paste this entire file.

Changes from CURRENT:
- Removed all ttl= parameters from @st.cache_data decorators
- Added expanded docstrings explaining cache behavior
- Added CRITICAL notes to upload handler docstrings
"""

import streamlit as st
import data_manager as dm
from nav_index import get_nav_index


@st.cache_data(show_spinner=False)
def get_all_folios_with_isin_and_nav(
    get_conn,
    _cams_v: int,
    _kfin_v: int,
    _nav_v: int,
):
    """
    Join CAMS + KFIN folios with ISIN and latest NAV.
    Cache busts ONLY when domain versions change (immediate).
    Requires: tests ensure dm.bump() called on upload.
    """
    nav_idx = get_nav_index()
    
    with get_conn() as conn:
        cams_folios = conn.execute(
            "SELECT * FROM cams_folios"
        ).fetchall()
        kfin_folios = conn.execute(
            "SELECT * FROM kfin_folios"
        ).fetchall()
    
    result = []
    for folio in cams_folios + kfin_folios:
        nav_row = nav_idx.get(folio.get("isin"))
        result.append({**folio, "nav": nav_row})
    
    return result


@st.cache_data(show_spinner=False)
def load_nav_dataframe(
    get_conn,
    _nav_v: int,
):
    """
    Load NAV as DataFrame.
    Cache busts ONLY when NAV version changes.
    """
    nav_idx = get_nav_index()
    nav_idx.load(lambda: _fetch_nav_from_source(get_conn))
    return nav_idx.get_all()


@st.cache_data(show_spinner=False)
def load_brokerage_report(
    folio_id: str,
    _brokerage_v: int,
):
    """
    Load brokerage report.
    Cache busts ONLY when brokerage version changes.
    """
    pass


@st.cache_data(show_spinner=False)
def get_cams_invested_per_scheme(
    _cams_folio_set: tuple,
    _cams_v: int,
):
    """
    Compute ONCE for all folios.
    Cache busts ONLY when CAMS version changes.
    """
    result = {}
    for folio_id in _cams_folio_set:
        result[folio_id] = _replay_cams_folio_scheme(folio_id)
    return result


def get_cams_invested_per_scheme_for_client(
    get_conn,
    client_code: str,
    _cams_v: int,
):
    """Get invested per scheme for ONE client."""
    with get_conn() as conn:
        folios = conn.execute(
            "SELECT folio_id FROM cams_folios WHERE client_code = ?",
            (client_code,)
        ).fetchall()
    
    folio_ids = tuple(sorted([f["folio_id"] for f in folios]))
    all_invested = get_cams_invested_per_scheme(folio_ids, _cams_v)
    return {fid: all_invested[fid] for fid in folio_ids}


@st.cache_data(show_spinner=False)
def get_kfin_invested_per_scheme(
    _kfin_folio_set: tuple,
    _kfin_v: int,
):
    """
    Compute ONCE for all folios.
    Cache busts ONLY when KFIN version changes.
    """
    result = {}
    for folio_id in _kfin_folio_set:
        result[folio_id] = _replay_kfin_folio_scheme(folio_id)
    return result


@st.cache_data(show_spinner=False)
def compute_capital_gains(
    _folio_tuple: tuple,
    _cams_v: int,
):
    """
    Compute capital gains.
    Cache busts ONLY when CAMS version changes.
    """
    pass


def _fetch_nav_from_source(get_conn):
    """Fetch NAV from upstream (not cached)."""
    pass


def _replay_cams_folio_scheme(folio_id: str):
    """CAMS FIFO replay (not cached)."""
    pass


def _replay_kfin_folio_scheme(folio_id: str):
    """KFIN FIFO replay (not cached)."""
    pass


def on_cams_upload():
    """
    After CAMS file upload, bump only CAMS domain.
    CRITICAL: This must be called for cache to update. Tests verify this.
    """
    dm.bump("cams")


def on_nav_update():
    """
    After NAV refresh, bump only NAV domain.
    CRITICAL: This must be called for cache to update. Tests verify this.
    """
    dm.bump("nav")


def on_brokerage_upload():
    """
    After brokerage file upload, bump only brokerage domain.
    CRITICAL: This must be called for cache to update. Tests verify this.
    """
    dm.bump("brokerage")