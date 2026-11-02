"""
core/valuation.py
=================
As-of unit/invested helpers for valuation reports.
"""
from __future__ import annotations
import pandas as pd

def calc_units_before(txn_df: pd.DataFrame, product_code: str,
                      before_date: pd.Timestamp) -> float:
    mask = (txn_df['product_code'] == product_code) & (txn_df['_date'] < before_date)
    return max(float(txn_df.loc[mask, 'signed_units'].sum()), 0.0)


def calc_units_upto(txn_df: pd.DataFrame, product_code: str,
                    as_of_date: pd.Timestamp) -> float:
    mask = (txn_df['product_code'] == product_code) & (txn_df['_date'] <= as_of_date)
    return max(float(txn_df.loc[mask, 'signed_units'].sum()), 0.0)


def calc_invested_upto(txn_df: pd.DataFrame, product_code: str,
                       as_of_date: pd.Timestamp) -> float:
    """
    FIFO cost basis of units STILL HELD as of as_of_date.

    Matches the 'Invested' figure shown in the Client tab, which the previous
    implementation did not — it summed only purchases and ignored redemptions,
    so any folio that had ever redeemed showed a wildly wrong gain/loss.

    Falls back to net cash flow (IN sum minus OUT sum) if FIFO replay fails.
    """
    mask = (
        (txn_df['product_code'] == product_code)
        & (txn_df['_date'] <= as_of_date)
    )
    subset = txn_df.loc[mask].copy()
    if subset.empty:
        return 0.0

    try:
        lots, _ = cg_row.replay_folio_scheme(subset)
        return float(sum(l.remaining_units * l.rate for l in lots))
    except Exception as e:
        log.warning("[VALUATION] FIFO replay failed for %s: %s — "
                    "falling back to net cash flow", product_code, e)
        in_mask  = subset['direction'] == 'IN'