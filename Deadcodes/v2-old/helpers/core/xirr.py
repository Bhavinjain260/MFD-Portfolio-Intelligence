"""
core/xirr.py
============
XIRR — folio+scheme and portfolio level. Pure: no Streamlit.
Matches Excel's =XIRR(amounts, dates, 0.01)*100.
"""
from __future__ import annotations

import logging
from datetime import datetime

import pandas as pd
from scipy.optimize import newton, brentq

from core.db import get_conn

log = logging.getLogger(__name__)


def calculate_xirr(dates, amounts, guess: float = 0.01):
    dates = pd.to_datetime(dates)
    amounts = [float(a) for a in amounts]
    pairs = sorted(zip(dates, amounts), key=lambda x: x[0])
    dates = [p[0] for p in pairs]
    amounts = [p[1] for p in pairs]
    ref = dates[0]

    def npv(rate):
        if rate <= -1.0:
            return float("inf")
        return sum(
            cf / ((1.0 + rate) ** ((d - ref).days / 365.0))
            for d, cf in zip(dates, amounts)
        )

    try:
        return newton(npv, guess, tol=1e-12, maxiter=200)
    except Exception:
        pass
    try:
        return brentq(npv, -0.9999, 10.0, xtol=1e-12, maxiter=200)
    except Exception:
        return None


def _fetch_cams(folio_no: str, product_code: str) -> pd.DataFrame:
    with get_conn() as conn:
        return pd.read_sql(
            """
            SELECT COALESCE(txn_date_iso, traddate) AS traddate, trxntype, amount
            FROM v_cams_txn_active
            WHERE folio_no = ? AND UPPER(TRIM(prodcode)) = ?
            ORDER BY COALESCE(txn_date_iso, traddate)
            """,
            conn,
            params=(folio_no, product_code.strip().upper()),
        )


def _fetch_kfin(folio_no: str, product_code: str) -> pd.DataFrame:
    with get_conn() as conn:
        return pd.read_sql(
            """
            SELECT COALESCE(txn_date_iso, td_trdt) AS traddate, td_amt AS amount
            FROM v_kfin_txn_active
            WHERE td_acno = ? AND UPPER(TRIM(fmcode)) = ?
            ORDER BY COALESCE(txn_date_iso, td_trdt)
            """,
            conn,
            params=(folio_no, product_code.strip().upper()),
        )


def compute_xirr_for_folio(
    folio_no: str,
    product_code: str,
    rta: str,
    current_value: float,
    as_of_date=None,
    verbose: bool = False,
) -> dict:
    """
    Folio+scheme level XIRR.
    Prefer txn_date_iso when present (avoids DD/MM vs MM/DD ambiguity).
    """
    rta_clean = rta.strip().upper()
    as_of_date = pd.to_datetime(as_of_date or datetime.now())

    if rta_clean == "CAMS":
        df = _fetch_cams(folio_no, product_code)
    elif rta_clean in ("KFINTECH", "KFIN", "KARVY"):
        df = _fetch_kfin(folio_no, product_code)
    else:
        if verbose:
            log.info("[XIRR] Unknown RTA: %s", rta)
        return {"xirr": None, "xirr_pct": None, "cash_flows": []}

    if df.empty:
        if verbose:
            log.info("[XIRR] %s/%s/%s: no transactions", rta_clean, folio_no, product_code)
        return {"xirr": None, "xirr_pct": None, "cash_flows": []}

    df["traddate"] = pd.to_datetime(df["traddate"], errors="coerce", format="mixed")
    df = df.dropna(subset=["traddate"]).sort_values("traddate").reset_index(drop=True)
    df = df[df["traddate"] <= as_of_date]
    if df.empty:
        return {"xirr": None, "xirr_pct": None, "cash_flows": []}

    dates = df["traddate"].tolist()

    if rta_clean == "CAMS":
        amounts = []
        for _, row in df.iterrows():
            raw = abs(float(row["amount"])) if pd.notna(row["amount"]) else 0.0
            is_redemption = str(row.get("trxntype", "")).strip().upper() == "R1"
            amounts.append(raw if is_redemption else -raw)
    else:
        amounts = [-abs(float(a)) for a in df["amount"]]

    dates.append(as_of_date)
    amounts.append(abs(float(current_value)))

    if len(dates) < 2:
        return {"xirr": None, "xirr_pct": None, "cash_flows": []}

    xirr = calculate_xirr(dates, amounts)
    xirr_pct = round(xirr * 100, 4) if xirr is not None else None
    cash_flows = [{"date": d.strftime("%Y-%m-%d"), "amount": a} for d, a in zip(dates, amounts)]
    return {"xirr": xirr, "xirr_pct": xirr_pct, "cash_flows": cash_flows}


def compute_xirr_debug(
    folio_no: str,
    product_code: str,
    rta: str,
    current_value: float,
    as_of_date=None,
) -> dict:
    result = compute_xirr_for_folio(
        folio_no, product_code, rta, current_value, as_of_date=as_of_date, verbose=True
    )
    return {"xirr_pct": result["xirr_pct"], "cash_flows": result["cash_flows"]}


def compute_portfolio_xirr(
    holdings_df: pd.DataFrame,
    as_of_date=None,
    verbose: bool = False,
) -> dict:
    as_of_date = pd.to_datetime(as_of_date or datetime.now())
    if holdings_df.empty:
        return {"xirr": None, "xirr_pct": None, "cash_flows": []}

    portfolio_dates = []
    portfolio_amounts = []

    groups = (
        holdings_df[["folio_id", "rta", "product_code", "nav_based_aum"]]
        .drop_duplicates(subset=["folio_id", "product_code", "rta"])
    )

    for _, row in groups.iterrows():
        fid = row["folio_id"]
        rta = str(row["rta"]).strip().upper()
        pcode = str(row["product_code"]).strip().upper() if pd.notna(row["product_code"]) else ""
        current_val = float(row["nav_based_aum"]) if pd.notna(row["nav_based_aum"]) else 0.0
        if current_val <= 0 or not pcode:
            continue

        if rta == "CAMS":
            df_txn = _fetch_cams(fid, pcode)
            if df_txn.empty:
                continue
            df_txn["traddate"] = pd.to_datetime(df_txn["traddate"], errors="coerce", format="mixed")
            df_txn = df_txn[df_txn["traddate"] <= as_of_date]
            if df_txn.empty:
                continue
            df_txn = df_txn.dropna(subset=["traddate"]).sort_values("traddate")
            for _, t in df_txn.iterrows():
                amt = abs(float(t["amount"])) if pd.notna(t["amount"]) else 0.0
                is_redemption = str(t.get("trxntype", "")).strip().upper() == "R1"
                portfolio_dates.append(t["traddate"])
                portfolio_amounts.append(amt if is_redemption else -amt)
        elif rta in ("KFINTECH", "KFIN", "KARVY"):
            df_txn = _fetch_kfin(fid, pcode)
            if df_txn.empty:
                continue
            df_txn["traddate"] = pd.to_datetime(df_txn["traddate"], errors="coerce", format="mixed")
            df_txn = df_txn[df_txn["traddate"] <= as_of_date]
            if df_txn.empty:
                continue
            df_txn = df_txn.dropna(subset=["traddate"]).sort_values("traddate")
            for _, t in df_txn.iterrows():
                amt = abs(float(t["amount"])) if pd.notna(t["amount"]) else 0.0
                portfolio_dates.append(t["traddate"])
                portfolio_amounts.append(-amt)

    total_current = float(holdings_df["nav_based_aum"].sum()) if "nav_based_aum" in holdings_df.columns else 0.0
    if len(portfolio_dates) < 1 or total_current <= 0:
        return {"xirr": None, "xirr_pct": None, "cash_flows": []}

    portfolio_dates.append(as_of_date)
    portfolio_amounts.append(total_current)

    xirr = calculate_xirr(portfolio_dates, portfolio_amounts)
    xirr_pct = round(xirr * 100, 2) if xirr is not None else None
    cash_flows = [
        {"date": d.strftime("%Y-%m-%d"), "amount": a}
        for d, a in zip(portfolio_dates, portfolio_amounts)
    ]
    return {"xirr": xirr, "xirr_pct": xirr_pct, "cash_flows": cash_flows}
