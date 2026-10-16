from datetime import date
from pydantic import BaseModel


class MatchedLot(BaseModel):
    buy_date: date
    buy_rate: float
    sell_date: date
    sell_rate: float
    units: float
    holding_days: int
    cost: float
    proceeds: float
    gain: float
    is_ltcg: bool


class TaxBreakdown(BaseModel):
    stcg_gain: float
    ltcg_gain: float
    total_gain: float
    stcg_tax: float
    ltcg_tax: float
    total_tax: float
    exemption_used: float
    ltcg_taxable: float
    exemption_limit: float
    tax_category: str


class HypotheticalRequest(BaseModel):
    folio_no: str
    prodcode: str
    rta: str
    redeem_units: float
    nav: float
    tax_category: str = "equity"
    slab_rate: float = 0.30