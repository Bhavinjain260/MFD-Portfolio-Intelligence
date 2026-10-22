"""
core/holdings.py
================
Invested amounts (FIFO), enriched folio+NAV structures.
Pure: no Streamlit.

NOTE on KFin invested:
  CAMS uses FIFO cost basis via core.tax.replay_folio_scheme.
  KFin units are signed at source; we also run FIFO after mapping
  so both RTAs are comparable. Documented in get_kfin_invested.
"""
from __future__ import annotations

import logging
from typing import Iterable

import pandas as pd

from core.db import get_conn
from core.folios import get_folio_transactions, get_folio_invested, get_folio_units
from core.tax import replay_folio_scheme

log = logging.getLogger(__name__)


def _fifo_cams_folio_scheme(grp: pd.DataFrame) -> float:
    """
    Single FIFO implementation used by holdings and recon.
    grp must have: traddate/raw_date, trxntype, trxn_nature, units, purprice, amount.
    """
    cols = ["traddate", "trxntype", "trxn_nature", "units", "purprice", "amount"]
    # Accept raw_date alias
    if "traddate" not in grp.columns and "raw_date" in grp.columns:
        grp = grp.rename(columns={"raw_date": "traddate"})
    for c in cols:
        if c not in grp.columns:
            if c == "trxn_nature":
                grp = grp.copy()
                grp["trxn_nature"] = ""
            else:
                return 0.0
    try:
        lots, _ = replay_folio_scheme(grp[cols])
        return float(sum(l.remaining_units for l in lots))
    except Exception:
        log.exception("[holdings] FIFO failed")
        return 0.0


def get_cams_invested(folio_list: Iterable[str]) -> dict:
    """
    {folio_id: {prodcode: invested_amount}} using FIFO cost basis.
    """
    result: dict = {}
    for folio_id in folio_list:
        txns = get_folio_transactions(folio_id, rta="CAMS")
        if txns.empty:
            result[folio_id] = {}
            continue
        per_scheme: dict = {}
        for pcode, grp in txns.groupby("product_code"):
            replay = grp.rename(columns={
                "date": "traddate", "trxn_type": "trxntype", "price": "purprice",
            })
            if "trxn_nature" not in replay.columns:
                replay["trxn_nature"] = ""
            try:
                lots, _ = replay_folio_scheme(
                    replay[["traddate", "trxntype", "trxn_nature", "units", "purprice", "amount"]]
                )
                per_scheme[pcode] = round(
                    sum(l.remaining_units * l.rate for l in lots), 2
                )
            except Exception:
                per_scheme[pcode] = 0.0
        result[folio_id] = per_scheme
    return result


def get_kfin_invested(folio_list: Iterable[str]) -> dict:
    """
    {folio_id: {prodcode: invested_amount}} using FIFO after sign-normalisation.
    KFin stores signed units; we convert to purchase/redemption rows then FIFO
    so numbers are comparable to CAMS.
    """
    result: dict = {}
    for folio_id in folio_list:
        txns = get_folio_transactions(folio_id, rta="KFinTech")
        if txns.empty:
            result[folio_id] = {}
            continue
        per_scheme: dict = {}
        for pcode, grp in txns.groupby("product_code"):
            per_scheme[pcode] = get_folio_invested(folio_id, pcode, rta="KFinTech")
        result[folio_id] = per_scheme
    return result


def bse_scheme_lookup() -> pd.DataFrame:
    with get_conn() as conn:
        return pd.read_sql("""
            SELECT UPPER(TRIM(Channel_Partner_Code)) AS product_code,
                   MAX(Scheme_Name) AS scheme_name,
                   MAX(ISIN)        AS isin
            FROM bse_scheme_master
            WHERE Channel_Partner_Code IS NOT NULL
              AND TRIM(Channel_Partner_Code) != ''
            GROUP BY UPPER(TRIM(Channel_Partner_Code))
        """, conn)


def get_isin_for_product(prodcode: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute("""
            SELECT ISIN FROM bse_scheme_master
            WHERE UPPER(TRIM(Channel_Partner_Code)) = ?
            LIMIT 1
        """, (prodcode.strip().upper(),)).fetchone()
    return row[0] if row else None


def get_client_holdings(client_code: str) -> pd.DataFrame:
    """
    Flat holdings table for one client: folio, rta, product, scheme, units, invested.
    Uses get_client_folios + get_folio_* so matching is consistent everywhere.
    """
    from core.clients import get_client_folios

    folios = get_client_folios(client_code)
    rows = []
    for f in folios["cams"]:
        txns = get_folio_transactions(f, rta="CAMS")
        for pcode in txns["product_code"].unique() if not txns.empty else []:
            units = get_folio_units(f, pcode, rta="CAMS")
            invested = get_folio_invested(f, pcode, rta="CAMS")
            scheme = txns.loc[txns["product_code"] == pcode, "scheme_name"].iloc[0] if not txns.empty else pcode
            rows.append({
                "folio_id": f, "rta": "CAMS", "product_code": pcode,
                "scheme_name": scheme, "units": units, "invested": invested,
            })
    for f in folios["kfin"]:
        txns = get_folio_transactions(f, rta="KFinTech")
        for pcode in txns["product_code"].unique() if not txns.empty else []:
            units = get_folio_units(f, pcode, rta="KFinTech")
            invested = get_folio_invested(f, pcode, rta="KFinTech")
            scheme = txns.loc[txns["product_code"] == pcode, "scheme_name"].iloc[0] if not txns.empty else pcode
            rows.append({
                "folio_id": f, "rta": "KFinTech", "product_code": pcode,
                "scheme_name": scheme, "units": units, "invested": invested,
            })
    return pd.DataFrame(rows)


# Placeholders for full enriched NAV frames (require live NAV index + app-level merges).
# After nav_service is moved to core/nav.py, wire get_all_folios_with_nav here.

def get_all_folios_with_nav() -> pd.DataFrame:
    """
    Skeleton: folio masters joined with ISIN from bse_scheme_master.
    NAV enrichment is left to ui layer or a later pass once core/nav is fully ported.
    """
    with get_conn() as conn:
        cams = pd.read_sql("""
            SELECT 'CAMS' AS rta, foliochk AS folio_id, product AS product_code,
                   inv_name AS investor, clos_bal AS file_units, amc_code AS amc
            FROM cams_wbr9_folio
        """, conn)
        kfin = pd.read_sql("""
            SELECT 'KFinTech' AS rta, folio AS folio_id, product_code,
                   investor_name AS investor, NULL AS file_units, fund AS amc
            FROM kfin_mfsd211_folio
        """, conn)
    df = pd.concat([cams, kfin], ignore_index=True)
    bse = bse_scheme_lookup()
    df["product_code"] = df["product_code"].astype(str).str.strip().str.upper()
    return df.merge(
        bse.rename(columns={"product_code": "product_code"}),
        on="product_code", how="left",
    ).copy()
