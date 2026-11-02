from pydantic import BaseModel


class ClientRow(BaseModel):
    client_code: str
    name: str
    pan: str | None
    guardian_pan: str | None = None
    mobile: str | None = None
    email: str | None = None
    city: str | None = None


class PortfolioRow(BaseModel):
    rta: str
    folio_id: str
    scheme_name: str
    isin: str | None
    units: float | None
    current_nav: float | None
    invested: float | None
    current_value: float | None
    gain_loss: float | None
    one_day_diff: float | None


class PortfolioSummary(BaseModel):
    total_invested: float
    total_current: float
    total_gain_loss: float
    one_day_diff: float
    folios: int
    prev_nav_date: str | None
    holdings: list[PortfolioRow]