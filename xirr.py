"""
XIRR — folio+scheme level, terminal debug version.
Matches Excel's =XIRR(amounts, dates, 0.01)*100 exactly.

CAMS: purchase = negative, redemption (trxntype == 'R1') = positive.
Karvy: no confirmed redemption code yet -> everything treated as
       purchase (negative), same assumption already used in
       get_kfin_txns_raw(). Flip the sign logic below once Karvy
       redemption rows + the real td_purred code are confirmed.

Final row is always today's date with the current value as a
positive inflow, exactly like your Excel sheet.
"""
import logging
from datetime import datetime
import pandas as pd
from scipy.optimize import newton, brentq

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(message)s")


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
        return sum(cf / ((1.0 + rate) ** ((d - ref).days / 365.0)) for d, cf in zip(dates, amounts))

    try:
        return newton(npv, guess, tol=1e-12, maxiter=200)
    except Exception:
        pass
    try:
        return brentq(npv, -0.9999, 10.0, xtol=1e-12, maxiter=200)
    except Exception:
        return None


def _fetch_cams(folio_no: str, product_code: str, get_conn) -> pd.DataFrame:
    """CAMS: traddate, trxntype, amount — filtered to ONE folio + ONE scheme."""
    with get_conn() as conn:
        return pd.read_sql("""
            SELECT traddate, trxntype, amount
            FROM cams_wbr2_transaction
            WHERE folio_no = ? AND UPPER(TRIM(prodcode)) = ?
            ORDER BY traddate
        """, conn, params=(folio_no, product_code.strip().upper()))


def _fetch_kfin(folio_no: str, product_code: str, get_conn) -> pd.DataFrame:
    """Karvy: td_trdt, td_amt — filtered to ONE folio + ONE scheme."""
    with get_conn() as conn:
        return pd.read_sql("""
            SELECT td_trdt AS traddate, td_amt AS amount
            FROM kfin_mfsd201_transaction
            WHERE td_acno = ? AND UPPER(TRIM(fmcode)) = ?
            ORDER BY td_trdt
        """, conn, params=(folio_no, product_code.strip().upper()))


def compute_xirr_debug(
    folio_no: str,
    product_code: str,
    rta: str,
    current_value: float,
    get_conn,
    as_of_date=None,
) -> dict:
    """
    Prints the full cash-flow table to terminal (same shape as your Excel
    sheet) and returns {"xirr_pct": float|None, "cash_flows": [...]}.
    """
    rta = rta.upper()

    if rta == "CAMS":
        df = _fetch_cams(folio_no, product_code, get_conn)
        if df.empty:
            log.info(f"[XIRR] {folio_no}/{product_code}: no CAMS transactions found")
            return {"xirr_pct": None, "cash_flows": []}
        df["traddate"] = pd.to_datetime(df["traddate"])
        df = df.sort_values("traddate")
        dates = df["traddate"].tolist()
        # redemption (R1) -> positive inflow, everything else -> negative outflow
        amounts = [
            abs(float(a)) if str(t).strip().upper() == "R1" else -abs(float(a))
            for t, a in zip(df["trxntype"], df["amount"])
        ]

    elif rta in ("KFINTECH", "KFIN", "KARVY"):
        df = _fetch_kfin(folio_no, product_code, get_conn)
        if df.empty:
            log.info(f"[XIRR] {folio_no}/{product_code}: no Karvy transactions found")
            return {"xirr_pct": None, "cash_flows": []}
        df["traddate"] = pd.to_datetime(df["traddate"])
        df = df.sort_values("traddate")
        dates = df["traddate"].tolist()
        # No confirmed redemption code yet -> treat everything as a purchase.
        amounts = [-abs(float(a)) for a in df["amount"]]

    else:
        log.info(f"[XIRR] Unknown RTA: {rta}")
        return {"xirr_pct": None, "cash_flows": []}

    as_of_date = pd.to_datetime(as_of_date or datetime.now())
    dates.append(as_of_date)
    amounts.append(abs(float(current_value)))

    # ── print table exactly like the Excel sheet ──
    log.info("=" * 60)
    log.info(f"XIRR — {rta} | Folio: {folio_no} | Scheme: {product_code}")
    log.info("=" * 60)
    log.info(f"{'Date':<12} {'Amount':>15}")
    log.info("-" * 60)
    for d, a in zip(dates, amounts):
        log.info(f"{d.strftime('%Y-%m-%d'):<12} {a:>15.2f}")
    log.info("-" * 60)

    xirr = calculate_xirr(dates, amounts)
    xirr_pct = round(xirr * 100, 4) if xirr is not None else None
    log.info(f"XIRR: {xirr_pct}%" if xirr_pct is not None else "XIRR: FAILED TO CONVERGE")
    log.info("=" * 60)

    cash_flows = [{"date": d.strftime("%Y-%m-%d"), "amount": a} for d, a in zip(dates, amounts)]
    return {"xirr_pct": xirr_pct, "cash_flows": cash_flows}


if __name__ == "__main__":
    # Quick manual test against your Excel example, no DB needed.
    dates = ["9/15/25", "11/17/25", "3/16/26", "8/18/25", "5/15/26", "4/15/26",
              "10/15/25", "12/15/25", "2/16/26", "1/16/26", "6/15/26", "7/15/26"]
    amounts = [-1499.93, -1499.93, -1499.93, -2499.88, -1499.93, -1499.93,
               -1499.93, -1499.93, -1499.93, -1499.93, -1499.93, 19217.81554]
    result = calculate_xirr(dates, amounts)
    print(f"Test XIRR (should be ~19.4985%): {result * 100:.4f}%")