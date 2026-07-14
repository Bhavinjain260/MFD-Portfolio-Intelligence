"""
XIRR Calculator — Folio-level, DB-aware with detailed logging for Excel verification.
Only needs: transaction date + amount from CAMS or KFinTech.
"""
import pandas as pd
from datetime import datetime
from scipy.optimize import newton, brentq


def calculate_xirr(dates, amounts, guess: float = 0.1) -> float | None:
    """Core XIRR solver. Matches Excel's XIRR."""
    dates = pd.to_datetime(dates)
    amounts = [float(a) for a in amounts]

    if len(dates) != len(amounts):
        raise ValueError("dates and amounts must have the same length")
    if len(dates) < 2:
        return None

    sorted_pairs = sorted(zip(dates, amounts), key=lambda x: x[0])
    dates = [p[0] for p in sorted_pairs]
    amounts = [p[1] for p in sorted_pairs]
    ref_date = dates[0]

    def npv(rate: float) -> float:
        if rate <= -1.0:
            return float("inf")
        total = 0.0
        for d, cf in zip(dates, amounts):
            days = (d - ref_date).days
            total += cf / ((1.0 + rate) ** (days / 365.0))
        return total

    try:
        return newton(npv, guess, tol=1e-12, maxiter=200)
    except Exception:
        pass

    try:
        return brentq(npv, -0.9999, 10.0, xtol=1e-12, maxiter=200)
    except Exception:
        return None


def _fetch_cams_txns(folio_no: str, get_conn) -> pd.DataFrame:
    """Fetch CAMS transactions: traddate + amount only."""
    with get_conn() as conn:
        return pd.read_sql("""
            SELECT traddate, amount
            FROM cams_wbr2_transaction
            WHERE folio_no = ?
            ORDER BY traddate
        """, conn, params=(folio_no,))


def _fetch_kfin_txns(folio_no: str, product_code: str, get_conn) -> pd.DataFrame:
    """Fetch KFinTech transactions: td_trdt + td_amt only."""
    with get_conn() as conn:
        return pd.read_sql("""
            SELECT td_trdt AS traddate, td_amt AS amount
            FROM kfin_mfsd201_transaction
            WHERE td_acno = ? AND UPPER(TRIM(fmcode)) = ?
            ORDER BY td_trdt
        """, conn, params=(folio_no, product_code.strip().upper()))


def compute_xirr_for_folio(
    folio_no: str,
    get_conn,
    rta: str = "CAMS",
    product_code: str | None = None,
    current_value: float | None = None,
    as_of_date=None,
    verbose: bool = True,
) -> dict:
    """
    Compute XIRR for a folio by fetching date+amount from DB.

    All historical transactions → NEGATIVE (money went out to buy).
    Final row: as_of_date + current_value → POSITIVE (current valuation).

    Parameters
    ----------
    folio_no : str
        Folio / account number.
    get_conn : callable
        Context-managed DB connection (your get_conn from init_db).
    rta : str
        'CAMS' or 'KFinTech'.
    product_code : str, optional
        Required for KFinTech (fmcode filter).
    current_value : float
        Current market value of remaining units.
    as_of_date : datetime, optional
        Valuation date. Defaults to today.
    verbose : bool
        If True, prints detailed calculation to terminal for Excel verification.

    Returns
    -------
    dict
        {
            "xirr": float | None,
            "xirr_pct": str | None,
            "transactions": int,
            "total_invested": float,
            "current_value": float,
            "cash_flows": list[dict],  # For Excel verification
            "error": str | None,
        }
    """
    result = {
        "xirr": None,
        "xirr_pct": None,
        "transactions": 0,
        "total_invested": 0.0,
        "current_value": current_value or 0.0,
        "cash_flows": [],
        "error": None,
    }

    if current_value is None or current_value <= 0:
        result["error"] = "No current value provided"
        return result

    # ── Fetch ──
    try:
        if rta.upper() == "CAMS":
            df = _fetch_cams_txns(folio_no, get_conn)
        elif rta.upper() in ("KFINTECH", "KFIN"):
            if not product_code:
                result["error"] = "product_code required for KFinTech"
                return result
            df = _fetch_kfin_txns(folio_no, product_code, get_conn)
        else:
            result["error"] = f"Unknown RTA: {rta}"
            return result
    except Exception as e:
        result["error"] = f"DB fetch failed: {e}"
        return result

    if df.empty:
        result["error"] = "No transactions found"
        return result

    result["transactions"] = len(df)

    # ── Build cash flows ──
    df["traddate"] = pd.to_datetime(df["traddate"])
    df = df.sort_values("traddate")

    dates = df["traddate"].tolist()
    amounts = [-abs(float(a)) for a in df["amount"]]
    result["total_invested"] = sum(abs(a) for a in amounts)

    # Build cash flow records for verification
    cash_flows = []
    for i, (d, a) in enumerate(zip(dates, amounts)):
        cash_flows.append({
            "row": i + 1,
            "date": d.strftime("%Y-%m-%d"),
            "amount": a,
            "type": "Purchase (outflow)"
        })

    # Final valuation
    as_of_date = as_of_date or datetime.now()
    dates.append(pd.to_datetime(as_of_date))
    amounts.append(abs(current_value))
    cash_flows.append({
        "row": len(cash_flows) + 1,
        "date": pd.to_datetime(as_of_date).strftime("%Y-%m-%d"),
        "amount": abs(current_value),
        "type": "Current Valuation (inflow)"
    })

    result["cash_flows"] = cash_flows

    # ── VERBOSE LOGGING FOR EXCEL CHECK ──
    if verbose:
        print("\n" + "=" * 70)
        print(f"XIRR CALCULATION — Folio: {folio_no} | RTA: {rta}")
        print("=" * 70)
        print(f"{'Row':<5} {'Date':<12} {'Amount':>15} {'Type'}")
        print("-" * 70)
        for cf in cash_flows:
            print(f"{cf['row']:<5} {cf['date']:<12} {cf['amount']:>15.2f} {cf['type']}")
        print("-" * 70)
        print(f"{'Total Invested:':<18} ₹ {result['total_invested']:>12.2f}")
        print(f"{'Current Value:':<18} ₹ {current_value:>12.2f}")
        print(f"{'Net Gain/Loss:':<18} ₹ {current_value - result['total_invested']:>12.2f}")

    # ── Calculate ──
    xirr = calculate_xirr(dates, amounts)
    result["xirr"] = xirr
    if xirr is not None:
        result["xirr_pct"] = f"{xirr * 100:.2f}%"
        if verbose:
            print(f"{'XIRR:':<18} {xirr * 100:>12.8f} %")
            print(f"{'XIRR (decimal):':<18} {xirr:>12.10f}")
            print("=" * 70)
            print("EXCEL FORMULA: =XIRR(D2:D{0}, B2:B{0}, 0.01)*100".format(len(cash_flows)))
            print("=" * 70)
    else:
        result["error"] = "XIRR calculation failed to converge"
        if verbose:
            print("XIRR: FAILED TO CONVERGE")
            print("=" * 70)

    return result
