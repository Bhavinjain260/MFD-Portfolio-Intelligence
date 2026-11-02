"""
core/folios.py
==============
Single source of truth for folio → RTA, transactions, schemes, units, invested.
Pure: no Streamlit.
"""
from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

from core.db import get_conn
from core.tax import replay_folio_scheme

log = logging.getLogger(__name__)

EMPTY_TXN_COLS = [
    "txn_no", "date", "rta", "folio_no", "product_code", "trxn_type",
    "trxn_mode", "trxn_status", "units", "price", "amount",
    "brokerage_code", "sub_broker", "remarks", "scheme_name",
    "direction", "signed_units",
]


def _empty_txn_df() -> pd.DataFrame:
    return pd.DataFrame(columns=EMPTY_TXN_COLS)


def get_folio_rta(folio_no: str) -> Optional[str]:
    """Return 'CAMS', 'KFinTech', or None."""
    with get_conn() as conn:
        if conn.execute(
            "SELECT 1 FROM cams_wbr9_folio WHERE foliochk=? LIMIT 1", (folio_no,)
        ).fetchone():
            return "CAMS"
        if conn.execute(
            "SELECT 1 FROM kfin_mfsd211_folio WHERE folio=? LIMIT 1", (folio_no,)
        ).fetchone():
            return "KFinTech"
    return None


def get_folio_transactions(
    folio_no: str,
    rta: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> pd.DataFrame:
    """
    Every transaction for a folio, one normalised shape.
    Columns: see EMPTY_TXN_COLS.
    """
    rta = rta or get_folio_rta(folio_no)
    if rta is None:
        return _empty_txn_df()

    with get_conn() as conn:
        if rta == "CAMS":
            df = pd.read_sql(
                """
                SELECT trxnno AS txn_no,
                       COALESCE(txn_date_iso, traddate) AS date,
                       folio_no,
                       UPPER(TRIM(prodcode)) AS product_code,
                       trxntype AS trxn_type,
                       trxnmode AS trxn_mode,
                       trxnstat AS trxn_status,
                       units, purprice AS price, amount,
                       brokcode AS brokerage_code,
                       subbrok AS sub_broker,
                       remarks
                FROM v_cams_txn_active
                WHERE folio_no = ?
                ORDER BY COALESCE(txn_date_iso, traddate)
                """,
                conn, params=(folio_no,),
            )
        else:
            df = pd.read_sql(
                """
                SELECT td_trno AS txn_no,
                       COALESCE(txn_date_iso, td_trdt) AS date,
                       td_acno AS folio_no,
                       UPPER(TRIM(fmcode)) AS product_code,
                       td_purred AS trxn_type,
                       trnmode AS trxn_mode,
                       trnstat AS trxn_status,
                       td_units AS units,
                       td_pop AS price,
                       td_amt AS amount,
                       td_broker AS brokerage_code,
                       '' AS sub_broker,
                       trdesc AS remarks
                FROM v_kfin_txn_active
                WHERE td_acno = ?
                ORDER BY COALESCE(txn_date_iso, td_trdt)
                """,
                conn, params=(folio_no,),
            )

    if df.empty:
        return _empty_txn_df()

    df["rta"] = rta
    df["date"] = pd.to_datetime(df["date"], errors="coerce", format="mixed")
    df = df.dropna(subset=["date"])

    if rta == "CAMS":
        is_red = df["trxn_type"].astype(str).str.strip().str.upper().eq("R1")
        df["direction"] = is_red.map({True: "OUT", False: "IN"})
        df["signed_units"] = df["units"].where(~is_red, -df["units"])
    else:
        df["direction"] = df["units"].apply(
            lambda u: "OUT" if u < 0 else ("IN" if u > 0 else "NEUTRAL")
        )
        df["signed_units"] = df["units"]

    pcodes = tuple(sorted(df["product_code"].dropna().unique()))
    if pcodes:
        ph = ",".join("?" * len(pcodes))
        with get_conn() as conn:
            schemes = pd.read_sql(
                f"""SELECT UPPER(TRIM(Channel_Partner_Code)) AS pc,
                           MAX(Scheme_Name) AS scheme_name
                    FROM bse_scheme_master
                    WHERE UPPER(TRIM(Channel_Partner_Code)) IN ({ph})
                    GROUP BY UPPER(TRIM(Channel_Partner_Code))""",
                conn, params=pcodes,
            )
        name_map = dict(zip(schemes["pc"], schemes["scheme_name"]))
        df["scheme_name"] = df["product_code"].map(name_map).fillna(df["product_code"])
    else:
        df["scheme_name"] = df["product_code"]

    if date_from:
        df = df[df["date"] >= pd.to_datetime(date_from)]
    if date_to:
        df = df[df["date"] <= pd.to_datetime(date_to)]

    return df[EMPTY_TXN_COLS].reset_index(drop=True)


def get_folio_schemes(folio_no: str, rta: str | None = None) -> pd.DataFrame:
    """Distinct product_code + scheme_name for a folio."""
    txns = get_folio_transactions(folio_no, rta=rta)
    if txns.empty:
        return pd.DataFrame(columns=["folio_no", "product_code", "scheme_name"])
    return (
        txns[["folio_no", "product_code", "scheme_name"]]
        .drop_duplicates()
        .reset_index(drop=True)
    )


def get_folio_units(folio_no: str, product_code: str, rta: str | None = None) -> float:
    """Current remaining units for one folio+scheme (FIFO for CAMS, signed sum for KFin)."""
    rta = rta or get_folio_rta(folio_no)
    if rta is None:
        return 0.0

    txns = get_folio_transactions(folio_no, rta=rta)
    txns = txns[txns["product_code"] == product_code.strip().upper()]
    if txns.empty:
        return 0.0

    if rta == "CAMS":
        replay = txns.rename(columns={
            "date": "traddate", "trxn_type": "trxntype",
            "price": "purprice",
        })
        # trxn_nature not always present in normalised cols
        if "trxn_nature" not in replay.columns:
            replay["trxn_nature"] = ""
        try:
            lots, _ = replay_folio_scheme(
                replay[["traddate", "trxntype", "trxn_nature", "units", "purprice", "amount"]]
            )
            return float(sum(l.remaining_units for l in lots))
        except Exception:
            log.exception("[folios] FIFO failed for %s/%s", folio_no, product_code)
            return float(txns["signed_units"].sum())
    return float(txns["signed_units"].sum())


def get_folio_invested(folio_no: str, product_code: str, rta: str | None = None) -> float:
    """Cost basis of remaining units (FIFO for CAMS; KFin uses purchase-only cost approx)."""
    rta = rta or get_folio_rta(folio_no)
    if rta is None:
        return 0.0

    txns = get_folio_transactions(folio_no, rta=rta)
    txns = txns[txns["product_code"] == product_code.strip().upper()]
    if txns.empty:
        return 0.0

    if rta == "CAMS":
        replay = txns.rename(columns={
            "date": "traddate", "trxn_type": "trxntype",
            "price": "purprice",
        })
        if "trxn_nature" not in replay.columns:
            replay["trxn_nature"] = ""
        try:
            lots, _ = replay_folio_scheme(
                replay[["traddate", "trxntype", "trxn_nature", "units", "purprice", "amount"]]
            )
            return round(sum(l.remaining_units * l.rate for l in lots), 2)
        except Exception:
            log.exception("[folios] FIFO invested failed for %s/%s", folio_no, product_code)
            return 0.0

    # KFin: approximate invested as sum of positive cash outflows still holding
    # Prefer FIFO when possible — map KFin cols into replay shape
    replay = txns.copy()
    replay["traddate"] = replay["date"]
    replay["trxntype"] = replay["trxn_type"]
    replay["trxn_nature"] = ""
    replay["purprice"] = replay["price"]
    # KFin units already signed; convert to positive units + redemption flag
    rows = []
    for _, r in replay.iterrows():
        u = float(r["units"] or 0)
        if u > 0:
            rows.append({
                "traddate": r["traddate"], "trxntype": "P", "trxn_nature": "",
                "units": u, "purprice": float(r["price"] or 0), "amount": float(r["amount"] or 0),
            })
        elif u < 0:
            rows.append({
                "traddate": r["traddate"], "trxntype": "R1", "trxn_nature": "redemption",
                "units": abs(u), "purprice": float(r["price"] or 0), "amount": float(r["amount"] or 0),
            })
    if not rows:
        return 0.0
    try:
        lots, _ = replay_folio_scheme(pd.DataFrame(rows))
        return round(sum(l.remaining_units * l.rate for l in lots), 2)
    except Exception:
        return 0.0
