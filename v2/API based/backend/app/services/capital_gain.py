from __future__ import annotations
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime
import pandas as pd

REDEMPTION_TRXNTYPE = "R1"

def get_tax_rates(sell_date: date) -> tuple[float, float, float]:
    """Returns (stcg_rate, ltcg_rate, exemption_limit) based on sale date."""
    if sell_date >= date(2024, 7, 23):
        return 0.20, 0.125, 125000
    return 0.15, 0.10, 100000

def classify_tax_category(scheme_name: str, scheme_type: str = "") -> str:
    text = f"{scheme_type} {scheme_name}".lower()
    if "hybrid" in text and "aggressive" not in text and "equity oriented" not in text:
        return "debt"
    equity_keywords = ["equity", "large cap", "mid cap", "small cap", "flexi cap", "elss", "multi cap", "focused"]
    if any(k in text for k in equity_keywords):
        return "equity"
    return "debt"

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
    tax_category: str = "equity"
    is_gap: bool = False

    @property
    def cost(self) -> float: return self.units * self.buy_rate
    @property
    def proceeds(self) -> float: return self.units * self.sell_rate
    @property
    def gain(self) -> float: return self.proceeds - self.cost
    @property
    def is_ltcg(self) -> bool:
        try: anniversary = self.buy_date.replace(year=self.buy_date.year + 1)
        except ValueError: anniversary = self.buy_date.replace(year=self.buy_date.year + 1, month=3, day=1)
        return self.sell_date > anniversary

def _get_fy(d: date) -> str:
    if d.month >= 4: return f"{d.year}-{str(d.year+1)[-2:]}"
    return f"{d.year-1}-{str(d.year)[-2:]}"

def _parse_date(val) -> date:
    if isinstance(val, (datetime, pd.Timestamp)): return val.date()
    if isinstance(val, date): return val
    if isinstance(val, str):
        s = val.strip()
        if len(s) >= 10 and s[4] == '-' and s[7] == '-':
            try: return datetime.strptime(s[:10], "%Y-%m-%d").date()
            except ValueError: pass
    dt = pd.to_datetime(val, dayfirst=True, errors='coerce')
    return dt.date() if pd.notna(dt) else None

def _consume_fifo(lots: list[Lot], sell_units: float, sell_date: date, sell_rate: float, tax_cat: str) -> list[Match]:
    matches = []
    remaining = sell_units
    for lot in lots:
        if remaining <= 1e-9: break
        if lot.remaining_units <= 1e-9: continue
        take = min(lot.remaining_units, remaining)
        matches.append(Match(
            buy_date=lot.buy_date, buy_rate=lot.rate, sell_date=sell_date, sell_rate=sell_rate,
            units=take, holding_days=(sell_date - lot.buy_date).days, tax_category=tax_cat, is_gap=False
        ))
        lot.remaining_units -= take
        remaining -= take
    if remaining > 1e-6:
        matches.append(Match(
            buy_date=sell_date, buy_rate=0.0, sell_date=sell_date, sell_rate=sell_rate,
            units=remaining, holding_days=0, tax_category=tax_cat, is_gap=True
        ))
    return matches

def replay_folio_scheme(txns: pd.DataFrame, scheme_name: str = "") -> tuple[list[Lot], list[Match]]:
    t = txns.copy()
    t["traddate"] = t["traddate"].apply(_parse_date)
    t = t.sort_values("traddate")
    
    tax_cat = classify_tax_category(scheme_name)
    lots, matches = [], []
    
    for _, row in t.iterrows():
        units = float(row.get("units") or 0)
        if units <= 0: continue
        is_red = (str(row.get("trxntype", "")).strip().upper() == REDEMPTION_TRXNTYPE or 
                  "redemption" in str(row.get("trxn_nature", "")).lower())
        rate = float(row.get("purprice") or (row.get("amount", 0) / units if units else 0))
        if is_red:
            matches.extend(_consume_fifo(lots, units, row["traddate"], rate, tax_cat))
        else:
            lots.append(Lot(buy_date=row["traddate"], rate=rate, remaining_units=units))
    return lots, matches

def hypothetical_redemption(txns: pd.DataFrame, redeem_units: float, current_nav: float, scheme_name: str, as_of: date | None = None) -> list[Match]:
    lots, _ = replay_folio_scheme(txns, scheme_name)
    tax_cat = classify_tax_category(scheme_name)
    return _consume_fifo(deepcopy(lots), redeem_units, as_of or date.today(), current_nav, tax_cat)

def tax_for_matches_by_fy(matches: list[Match], slab_rate: float = 0.30, include_gaps: bool = False) -> dict:
    by_fy = {}
    total_tax = 0.0
    gaps_count = 0
    
    for m in matches:
        if m.is_gap:
            gaps_count += 1
            if not include_gaps: continue
            
        fy = _get_fy(m.sell_date)
        if fy not in by_fy:
            by_fy[fy] = {
                "stcg_gain": 0.0, "ltcg_gain": 0.0, "stcg_tax": 0.0, "ltcg_tax": 0.0, 
                "exemption_used": 0.0, "ltcg_taxable": 0.0, "_debt_gain": 0.0 
            }
            
        if m.tax_category == "equity":
            if m.is_ltcg: by_fy[fy]["ltcg_gain"] += m.gain
            else: by_fy[fy]["stcg_gain"] += m.gain
        else:
            by_fy[fy]["_debt_gain"] += m.gain

    for fy, data in by_fy.items():
        fy_end_date = date(int(fy.split("-")[0]) + 1, 3, 31) 
        stcg_rate, ltcg_rate, exemption_limit = get_tax_rates(fy_end_date)
        
        data["stcg_tax"] += max(data["_debt_gain"], 0) * slab_rate
        del data["_debt_gain"]
        
        exemption = min(max(data["ltcg_gain"], 0), exemption_limit)
        data["ltcg_taxable"] = max(data["ltcg_gain"] - exemption_limit, 0)
        data["exemption_used"] = exemption
        
        data["stcg_tax"] += max(data["stcg_gain"], 0) * stcg_rate
        data["ltcg_tax"] = data["ltcg_taxable"] * ltcg_rate
        
        total_tax += data["stcg_tax"] + data["ltcg_tax"]
        
    return {
        "total_tax": total_tax,
        "gaps_count": gaps_count,
        "by_fy": [{"fy": k, **v} for k, v in sorted(by_fy.items())]
    }