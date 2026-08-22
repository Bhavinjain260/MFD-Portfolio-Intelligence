"""
XIRR — folio+scheme level and portfolio level.
Matches Excel's =XIRR(amounts, dates, 0.01)*100.

CAMS: purchase = negative, redemption (trxntype == 'R1') = positive.
KFin: no confirmed redemption code yet -> everything treated as
       purchase (negative). Flip the sign logic once the real
       redemption code is confirmed.
"""
import logging
from datetime import datetime
import pandas as pd
from scipy.optimize import newton, brentq
import streamlit as st

log = logging.getLogger(__name__)

def calculate_xirr(dates, amounts, guess: float = 0.01):
    """Core solver — matches Excel's XIRR(values, dates, guess)."""
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

def compute_xirr_debug(
    folio_no: str,
    product_code: str,
    rta: str,
    current_value: float,
    get_conn,
    as_of_date=None,
) -> dict:
    """
    Prints the full cash-flow table to terminal and returns
    {"xirr_pct": float|None, "cash_flows": [...]}.
    """
    rta = rta.upper()

    if rta == "CAMS":
        df = _fetch_cams(folio_no, product_code, get_conn)
        if df.empty:
            log.info("[XIRR] %s/%s: no CAMS transactions found", folio_no, product_code)
            return {"xirr_pct": None, "cash_flows": []}
        df["traddate"] = pd.to_datetime(df["traddate"])
        df = df.sort_values("traddate")
        dates = df["traddate"].tolist()
        amounts = [
            abs(float(a)) if str(t).strip().upper() == "R1" else -abs(float(a))
            for t, a in zip(df["trxntype"], df["amount"])
        ]

    elif rta in ("KFINTECH", "KFIN", "KARVY"):
        df = _fetch_kfin(folio_no, product_code, get_conn)
        if df.empty:
            log.info("[XIRR] %s/%s: no Karvy transactions found", folio_no, product_code)
            return {"xirr_pct": None, "cash_flows": []}
        df["traddate"] = pd.to_datetime(df["traddate"])
        df = df.sort_values("traddate")
        dates = df["traddate"].tolist()
        amounts = [-abs(float(a)) for a in df["amount"]]

    else:
        log.info("[XIRR] Unknown RTA: %s", rta)
        return {"xirr_pct": None, "cash_flows": []}

    as_of_date = pd.to_datetime(as_of_date or datetime.now())
    dates.append(as_of_date)
    amounts.append(abs(float(current_value)))

    log.info("=" * 60)
    log.info("XIRR — %s | Folio: %s | Scheme: %s", rta, folio_no, product_code)
    log.info("=" * 60)
    log.info(f"{'Date':<12} {'Amount':>15}")
    log.info("-" * 60)
    for d, a in zip(dates, amounts):
        log.info(f"{d.strftime('%Y-%m-%d'):<12} {a:>15.2f}")
    log.info("-" * 60)

    xirr = calculate_xirr(dates, amounts)
    xirr_pct = round(xirr * 100, 4) if xirr is not None else None
    log.info("XIRR: %s%%", xirr_pct if xirr_pct is not None else "FAILED TO CONVERGE")
    log.info("=" * 60)

    cash_flows = [{"date": d.strftime("%Y-%m-%d"), "amount": a} for d, a in zip(dates, amounts)]
    return {"xirr_pct": xirr_pct, "cash_flows": cash_flows}

@st.cache_data(ttl=180, show_spinner=False)
def compute_xirr_for_folio(
    folio_no: str,
    product_code: str,
    rta: str,
    current_value: float,
    _get_conn,
    as_of_date=None,
    verbose: bool = False,
) -> dict:
    """
    Folio+scheme level XIRR.

    NOTE: param is _get_conn so Streamlit skips hashing the function object.
    Callers must pass it as _get_conn=get_conn (keyword) or positionally.

    Returns {"xirr": float|None, "xirr_pct": float|None, "cash_flows": [...]}
    """
    get_conn = _get_conn
    rta_clean = rta.strip().upper()
    as_of_date = pd.to_datetime(as_of_date or datetime.now())

    with get_conn() as conn:
        if rta_clean == "CAMS":
            df = pd.read_sql(
                """
                SELECT traddate, trxntype, amount
                FROM cams_wbr2_transaction
                WHERE folio_no = ? AND UPPER(TRIM(prodcode)) = ?
                """,
                conn,
                params=(folio_no, product_code.strip().upper()),
            )
        elif rta_clean in ("KFINTECH", "KFIN", "KARVY"):
            df = pd.read_sql(
                """
                SELECT td_trdt AS traddate, td_amt AS amount
                FROM kfin_mfsd201_transaction
                WHERE td_acno = ? AND UPPER(TRIM(fmcode)) = ?
                """,
                conn,
                params=(folio_no, product_code.strip().upper()),
            )
        else:
            if verbose:
                log.info("[XIRR] Unknown RTA: %s", rta)
            return {"xirr": None, "xirr_pct": None, "cash_flows": []}

    if df.empty:
        if verbose:
            log.info("[XIRR] %s/%s/%s: no transactions found", rta_clean, folio_no, product_code)
        return {"xirr": None, "xirr_pct": None, "cash_flows": []}

    df["traddate"] = pd.to_datetime(df["traddate"], errors="coerce")
    df = df.dropna(subset=["traddate"]).sort_values("traddate").reset_index(drop=True)

    if df.empty:
        if verbose:
            log.info("[XIRR] %s/%s/%s: no parseable dates", rta_clean, folio_no, product_code)
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
        if verbose:
            log.info("[XIRR] %s/%s/%s: insufficient cash flows", rta_clean, folio_no, product_code)
        return {"xirr": None, "xirr_pct": None, "cash_flows": []}

    if verbose:
        log.info("=" * 60)
        log.info("XIRR — %s | Folio: %s | Scheme: %s", rta_clean, folio_no, product_code)
        log.info("=" * 60)
        log.info(f"{'Date':<12} {'Amount':>15} {'Type':<12}")
        log.info("-" * 60)
        for d, a in zip(dates[:-1], amounts[:-1]):
            typ = "Redemption" if a > 0 else "Purchase"
            log.info(f"{d.strftime('%Y-%m-%d'):<12} {a:>15.2f} {typ:<12}")
        log.info("-" * 60)
        log.info(f"{dates[-1].strftime('%Y-%m-%d'):<12} {amounts[-1]:>15.2f} {'Current Val':<12}")
        log.info("-" * 60)

    xirr = calculate_xirr(dates, amounts)
    xirr_pct = round(xirr * 100, 4) if xirr is not None else None

    if verbose:
        log.info("XIRR: %s%%", xirr_pct if xirr_pct is not None else "FAILED TO CONVERGE")
        log.info("=" * 60)

    cash_flows = [{"date": d.strftime("%Y-%m-%d"), "amount": a} for d, a in zip(dates, amounts)]
    return {"xirr": xirr, "xirr_pct": xirr_pct, "cash_flows": cash_flows}

def compute_portfolio_xirr(
    holdings_df: pd.DataFrame,
    get_conn,
    as_of_date=None,
    verbose: bool = False,
) -> dict:
    """
    Portfolio-level XIRR across all folio+scheme combinations.
    """
    as_of_date = pd.to_datetime(as_of_date or datetime.now())

    if holdings_df.empty:
        if verbose:
            log.info("[XIRR-PORTFOLIO] Empty holdings — nothing to compute.")
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

        with get_conn() as conn:
            if rta == "CAMS":
                df_txn = pd.read_sql(
                    """
                    SELECT traddate, trxntype, amount
                    FROM cams_wbr2_transaction
                    WHERE folio_no = ? AND UPPER(TRIM(prodcode)) = ?
                    """,
                    conn,
                    params=(fid, pcode),
                )
                if df_txn.empty:
                    continue

                df_txn["traddate"] = pd.to_datetime(df_txn["traddate"], errors="coerce")
                df_txn = df_txn.dropna(subset=["traddate"]).sort_values("traddate")

                for _, t in df_txn.iterrows():
                    amt = abs(float(t["amount"])) if pd.notna(t["amount"]) else 0.0
                    is_redemption = str(t.get("trxntype", "")).strip().upper() == "R1"
                    portfolio_dates.append(t["traddate"])
                    portfolio_amounts.append(amt if is_redemption else -amt)

            elif rta in ("KFINTECH", "KFIN", "KARVY"):
                df_txn = pd.read_sql(
                    """
                    SELECT td_trdt AS traddate, td_amt AS amount
                    FROM kfin_mfsd201_transaction
                    WHERE td_acno = ? AND UPPER(TRIM(fmcode)) = ?
                    """,
                    conn,
                    params=(fid, pcode),
                )
                if df_txn.empty:
                    continue

                df_txn["traddate"] = pd.to_datetime(df_txn["traddate"], errors="coerce")
                df_txn = df_txn.dropna(subset=["traddate"]).sort_values("traddate")

                for _, t in df_txn.iterrows():
                    amt = abs(float(t["amount"])) if pd.notna(t["amount"]) else 0.0
                    portfolio_dates.append(t["traddate"])
                    portfolio_amounts.append(-amt)

            else:
                if verbose:
                    log.info("[XIRR-PORTFOLIO] Skipping unknown RTA '%s' for folio %s", rta, fid)
                continue

    total_current = float(holdings_df["nav_based_aum"].sum()) if "nav_based_aum" in holdings_df.columns else 0.0

    if len(portfolio_dates) < 1 or total_current <= 0:
        if verbose:
            log.info("[XIRR-PORTFOLIO] No transactions or zero total current value.")
        return {"xirr": None, "xirr_pct": None, "cash_flows": []}

    portfolio_dates.append(as_of_date)
    portfolio_amounts.append(total_current)

    if verbose:
        log.info("=" * 60)
        log.info("XIRR — PORTFOLIO LEVEL | %s folio+scheme groups", len(groups))
        log.info("=" * 60)
        log.info(f"{'Date':<12} {'Amount':>15} {'Type':<12}")
        log.info("-" * 60)
        for d, a in zip(portfolio_dates[:-1], portfolio_amounts[:-1]):
            typ = "Redemption" if a > 0 else "Purchase"
            log.info(f"{d.strftime('%Y-%m-%d'):<12} {a:>15.2f} {typ:<12}")
        log.info("-" * 60)
        log.info(f"{portfolio_dates[-1].strftime('%Y-%m-%d'):<12} {portfolio_amounts[-1]:>15.2f} {'Total Value':<12}")
        log.info("-" * 60)

    xirr = calculate_xirr(portfolio_dates, portfolio_amounts)
    xirr_pct = round(xirr * 100, 2) if xirr is not None else None

    if verbose:
        log.info("Portfolio XIRR: %s%%", xirr_pct if xirr_pct is not None else "FAILED TO CONVERGE")
        log.info("=" * 60)

    cash_flows = [
        {"date": d.strftime("%Y-%m-%d"), "amount": a}
        for d, a in zip(portfolio_dates, portfolio_amounts)
    ]
    return {"xirr": xirr, "xirr_pct": xirr_pct, "cash_flows": cash_flows}


def _fetch_cams(folio_no: str, product_code: str, get_conn) -> pd.DataFrame:
    with get_conn() as conn:
        return pd.read_sql(
            """
            SELECT traddate, trxntype, amount
            FROM cams_wbr2_transaction
            WHERE folio_no = ? AND UPPER(TRIM(prodcode)) = ?
            ORDER BY traddate
            """,
            conn,
            params=(folio_no, product_code.strip().upper()),
        )


def _fetch_kfin(folio_no: str, product_code: str, get_conn) -> pd.DataFrame:
    with get_conn() as conn:
        return pd.read_sql(
            """
            SELECT td_trdt AS traddate, td_amt AS amount
            FROM kfin_mfsd201_transaction
            WHERE td_acno = ? AND UPPER(TRIM(fmcode)) = ?
            ORDER BY td_trdt
            """,
            conn,
            params=(folio_no, product_code.strip().upper()),
        )