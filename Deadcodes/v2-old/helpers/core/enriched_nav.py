"""Enriched folio NAV dataframe — extracted from app.py (Phase 11 polish).

Note: still depends on get_all_folios_with_isin_and_nav living in app.py until
that loader is moved fully into core.holdings.
"""
from __future__ import annotations
import logging
import pandas as pd
from core.db import get_conn

log = logging.getLogger(__name__)

try:
    from core.nav import _amfi
except Exception:
    _amfi = None

def get_enriched_folio_nav_df() -> pd.DataFrame:
    """
    Single source of truth for folio+NAV+invested data.
    Shared across ALL browser sessions. Recomputes only on:
      - data_version() change (upload happened)
      - TTL expiry (30 min safety net)
    """
    log.info("[NAV-ENRICH] Cache miss — rebuilding enriched folio_nav_df (v=%s)", _v)

    # NOTE: no network calls here. The standalone background_worker.py
    # is responsible for keeping the NAV snapshot fresh on disk. If the
    # snapshot is missing, this just works with whatever's available.

    folio_nav_df = get_all_folios_with_isin_and_nav(get_conn, _v)
    if folio_nav_df.empty:
        return folio_nav_df

    folio_nav_df['product_code_norm'] = (
        folio_nav_df['product_code'].astype(str).str.strip().str.upper()
    )

    # ── CAMS: overwrite file_aum & units from transaction sums ──
    cams_folios_all = folio_nav_df.loc[folio_nav_df['rta'] == 'CAMS', 'folio_id'].unique().tolist()
    if cams_folios_all:
        cams_invested_all = get_cams_invested_per_scheme(cams_folios_all, _v)
        if not cams_invested_all.empty:
            cams_invested_all['product_code_norm'] = (
                cams_invested_all['product_code'].astype(str).str.strip().str.upper()
            )
            folio_nav_df = folio_nav_df.merge(
                cams_invested_all,
                on=['folio_id', 'product_code_norm'],
                how='left',
                suffixes=('', '_cams'),
            )
            m = folio_nav_df['rta'] == 'CAMS'
            has = m & folio_nav_df['invested_amount'].notna()
            folio_nav_df.loc[has, 'file_aum']      = folio_nav_df.loc[has, 'invested_amount']
            folio_nav_df.loc[has, 'units']         = folio_nav_df.loc[has, 'total_units']
            folio_nav_df.loc[has, 'nav_based_aum'] = (
                folio_nav_df.loc[has, 'units'] * folio_nav_df.loc[has, 'current_nav']
            )
            folio_nav_df = folio_nav_df.drop(
                columns=['invested_amount', 'total_units', 'product_code_norm_cams'],
                errors='ignore',
            )

    # ── KFinTech: overwrite file_aum from transaction sums ──
    kfin_folios_all = folio_nav_df.loc[folio_nav_df['rta'] == 'KFinTech', 'folio_id'].unique().tolist()
    if kfin_folios_all:
        kfin_invested_all = get_kfin_invested_per_scheme(kfin_folios_all, _v)
        if not kfin_invested_all.empty:
            kfin_invested_all['product_code_norm'] = (
                kfin_invested_all['product_code'].astype(str).str.strip().str.upper()
            )
            folio_nav_df = folio_nav_df.merge(
                kfin_invested_all,
                on=['folio_id', 'product_code_norm'],
                how='left',
                suffixes=('', '_kfin'),
            )
            m = folio_nav_df['rta'] == 'KFinTech'
            has = m & folio_nav_df['invested_amount'].notna()
            folio_nav_df.loc[has, 'file_aum']      = folio_nav_df.loc[has, 'invested_amount']
            folio_nav_df.loc[has, 'nav_based_aum'] = (
                folio_nav_df.loc[has, 'units'] * folio_nav_df.loc[has, 'current_nav']
            )
            folio_nav_df = folio_nav_df.drop(
                columns=['invested_amount', 'product_code_norm_kfin'],
                errors='ignore',
            )

    folio_nav_df = folio_nav_df.drop(columns=['product_code_norm'], errors='ignore')

    # ── Guarantee numeric dtypes at the source so every downstream
    #    arithmetic/diff/sort op stays float64. Any TEXT row from SQLite
    #    will otherwise poison the whole column into object dtype.
    for col in ("units", "current_nav", "file_aum", "nav_based_aum"):
        if col in folio_nav_df.columns:
            folio_nav_df[col] = pd.to_numeric(folio_nav_df[col], errors="coerce")

    log.info("[NAV-ENRICH] Rebuilt %s rows", len(folio_nav_df))
    return folio_nav_df


