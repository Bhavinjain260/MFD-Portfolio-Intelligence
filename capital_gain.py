"""
Capital Gains Engine — FIFO cost-basis matching for CAMS transactions.
Two modes: realized (already-redeemed) and hypothetical (what-if redemption).
"""
from __future__ import annotations
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime

import pandas as pd

REDEMPTION_TRXNTYPE = "R1"  # extend if more redemption codes show up
STCG_EQUITY_RATE = 0.20
LTCG_EQUITY_RATE = 0.125
LTCG_EQUITY_EXEMPTION = 125000  # per FY, aggregate across ALL equity LTCG — applied here as if this is the only one
EQUITY_HOLDING_DAYS = 365  # approx for "12 months"; real rule is calendar month-based


def classify_tax_category(category: str = "", scheme_name: str = "") -> str:
    """
    Best-guess only — always let the user confirm/override in the UI.
    'equity' -> STCG/LTCG equity rules. 'debt' -> slab-rate rules (covers debt,
    FoF, gold/silver ETF FoF, international funds — "specified mutual funds").
    """
    text = f"{category or ''} {scheme_name or ''}".lower()
    if "equity" in text and "hybrid" not in text:
        return "equity"
    if "aggressive hybrid" in text or "equity oriented" in text:
        return "equity"
    return "debt"


def _parse_date(val) -> date:
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val
    return pd.to_datetime(val).date()


@dataclass
class Lot:
    buy_date: date
    rate: float
    remaining_units: float


@dataclass
class Match:
    buy_date: date
    buy_rate: float
    sell_date: date
    sell_rate: float
    units: float
    holding_days: int

    @property
    def cost(self) -> float:
        return self.units * self.buy_rate

    @property
    def proceeds(self) -> float:
        return self.units * self.sell_rate

    @property
    def gain(self) -> float:
        return self.proceeds - self.cost

    @property
    def is_ltcg(self) -> bool:
        return self.holding_days >= EQUITY_HOLDING_DAYS


def _consume_fifo(lots: list[Lot], sell_units: float, sell_date: date, sell_rate: float) -> list[Match]:
    """Consumes sell_units from lots (mutated, oldest first). Returns matches."""
    matches: list[Match] = []
    remaining = sell_units
    for lot in lots:
        if remaining <= 1e-9:
            break
        if lot.remaining_units <= 1e-9:
            continue
        take = min(lot.remaining_units, remaining)
        matches.append(Match(
            buy_date=lot.buy_date, buy_rate=lot.rate,
            sell_date=sell_date, sell_rate=sell_rate,
            units=take, holding_days=(sell_date - lot.buy_date).days,
        ))
        lot.remaining_units -= take
        remaining -= take
    if remaining > 1e-6:
        # Sold more than known purchases cover — missing history / data gap. Flagged with 0-day holding.
        matches.append(Match(
            buy_date=sell_date, buy_rate=0.0,
            sell_date=sell_date, sell_rate=sell_rate,
            units=remaining, holding_days=0,
        ))
    return matches


def replay_folio_scheme(txns: pd.DataFrame) -> tuple[list[Lot], list[Match]]:
    """
    txns columns required: traddate, trxntype, trxn_nature, units, purprice, amount.
    Purchases build lots; anything flagged as redemption consumes lots FIFO.
    Returns (remaining_lots, realized_matches).
    """
    t = txns.copy()
    t["traddate"] = t["traddate"].apply(_parse_date)
    t = t.sort_values("traddate")

    lots: list[Lot] = []
    matches: list[Match] = []

    for _, row in t.iterrows():
        units = float(row.get("units") or 0)
        if units <= 0:
            continue
        is_redemption = (
            str(row.get("trxntype", "")).strip().upper() == REDEMPTION_TRXNTYPE
            or "redemption" in str(row.get("trxn_nature", "")).lower()
        )
        if is_redemption:
            rate = float(row.get("purprice") or (row.get("amount", 0) / units if units else 0))
            matches.extend(_consume_fifo(lots, units, row["traddate"], rate))
        else:
            rate = float(row.get("purprice") or 0)
            lots.append(Lot(buy_date=row["traddate"], rate=rate, remaining_units=units))

    return lots, matches


def hypothetical_redemption(txns: pd.DataFrame, redeem_units: float, current_nav: float,
                             as_of: date | None = None) -> list[Match]:
    """FIFO-consumes redeem_units from units currently outstanding, at current_nav, dated as_of (default today)."""
    lots, _ = replay_folio_scheme(txns)
    lots = deepcopy(lots)
    as_of = as_of or date.today()
    return _consume_fifo(lots, redeem_units, as_of, current_nav)


def tax_for_matches(matches: list[Match], tax_category: str, slab_rate: float = 0.30) -> dict:
    """
    tax_category: 'equity' or 'debt'. slab_rate used only for 'debt'.
    LTCG_EQUITY_EXEMPTION applied as if this is your only equity LTCG for the FY —
    subtract other equity LTCG first if you have any, before trusting this number.
    """
    stcg_gain = sum(m.gain for m in matches if not m.is_ltcg)
    ltcg_gain = sum(m.gain for m in matches if m.is_ltcg)
    total_gain = stcg_gain + ltcg_gain

    if tax_category == "equity":
        stcg_tax = max(stcg_gain, 0) * STCG_EQUITY_RATE
        ltcg_tax = max(ltcg_gain - LTCG_EQUITY_EXEMPTION, 0) * LTCG_EQUITY_RATE
        total_tax = stcg_tax + ltcg_tax
    else:  # debt / specified mutual fund — slab rate, no LTCG benefit, no holding-period distinction
        total_tax = max(total_gain, 0) * slab_rate
        stcg_tax = max(stcg_gain, 0) * slab_rate
        ltcg_tax = max(ltcg_gain, 0) * slab_rate

    return {
        "stcg_gain": stcg_gain, "ltcg_gain": ltcg_gain, "total_gain": total_gain,
        "stcg_tax": stcg_tax, "ltcg_tax": ltcg_tax, "total_tax": total_tax,
        "tax_category": tax_category,
    }


def matches_to_df(matches: list[Match]) -> pd.DataFrame:
    if not matches:
        return pd.DataFrame(columns=[
            "Buy Date", "Buy Rate", "Sell Date", "Sell Rate", "Units",
            "Holding Days", "Cost", "Proceeds", "Gain", "STCG/LTCG"
        ])
    return pd.DataFrame([{
        "Buy Date": m.buy_date, "Buy Rate": round(m.buy_rate, 4),
        "Sell Date": m.sell_date, "Sell Rate": round(m.sell_rate, 4),
        "Units": round(m.units, 4), "Holding Days": m.holding_days,
        "Cost": round(m.cost, 2), "Proceeds": round(m.proceeds, 2), "Gain": round(m.gain, 2),
        "STCG/LTCG": "LTCG" if m.is_ltcg else "STCG",
    } for m in matches])