"""
ui/cached.py
============
All @st.cache_data wrappers. Each delegates to exactly one core/ function.
"""
from __future__ import annotations

import streamlit as st

from core.db import current as data_version
from core.folios import get_folio_transactions
from core.xirr import compute_xirr_for_folio, compute_portfolio_xirr
from core.recon import compute_recon
from core.clients import search_clients, get_client_folios, get_client_schemes
from core.holdings import get_cams_invested, get_kfin_invested, get_client_holdings


@st.cache_data(ttl=1800, show_spinner=False)
def folio_transactions_cached(folio_no: str, _v: int):
    return get_folio_transactions(folio_no)


@st.cache_data(ttl=1800, show_spinner=False)
def client_folios_cached(client_code: str, _v: int):
    return get_client_folios(client_code)


@st.cache_data(ttl=1800, show_spinner=False)
def client_schemes_cached(client_code: str, _v: int):
    return get_client_schemes(client_code)


@st.cache_data(ttl=1800, show_spinner=False)
def cams_invested_cached(folio_tuple: tuple, _v: int):
    return get_cams_invested(folio_tuple)


@st.cache_data(ttl=1800, show_spinner=False)
def kfin_invested_cached(folio_tuple: tuple, _v: int):
    return get_kfin_invested(folio_tuple)


@st.cache_data(ttl=1800, show_spinner=False)
def client_holdings_cached(client_code: str, _v: int):
    return get_client_holdings(client_code)


@st.cache_data(ttl=180, show_spinner=False)
def folio_xirr_cached(
    folio_no: str,
    product_code: str,
    rta: str,
    current_value: float,
    as_of_date: str | None,
    _v: int,
):
    return compute_xirr_for_folio(
        folio_no, product_code, rta, current_value, as_of_date=as_of_date
    )


@st.cache_data(show_spinner="Reconciling units…")
def recon_cached(_v: int, ignore_dates: bool = True):
    return compute_recon(ignore_dates=ignore_dates)


@st.cache_data(ttl=300, show_spinner=False)
def search_clients_cached(query: str, limit: int = 20):
    return search_clients(query, limit=limit)


from core.brokerage import load_brokerage_report, compute_sub_broker_summary, list_sub_brokers

@st.cache_data(ttl=1800, show_spinner=False)
def brokerage_report_cached(_v: int):
    return load_brokerage_report()

@st.cache_data(ttl=1800, show_spinner=False)
def sub_broker_summary_cached(period_type: str, period_key: str, _v: int):
    return compute_sub_broker_summary(period_type, period_key)
