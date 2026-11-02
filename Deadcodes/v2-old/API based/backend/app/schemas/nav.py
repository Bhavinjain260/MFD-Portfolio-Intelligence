from datetime import date
from pydantic import BaseModel


class NavValue(BaseModel):
    isin: str
    nav: float
    nav_date: date


class NavHistoryPoint(BaseModel):
    nav_date: date
    nav_value: float
    prev_nav: float | None = None
    daily_change: float | None = None
    daily_pct: float | None = None


class NavSnapshotStatus(BaseModel):
    has_today: bool
    today_path: str | None
    latest_path: str | None
    latest_date: str | None
    latest_bytes: int | None


class NavSchemeInfo(BaseModel):
    scheme_code: int
    scheme_name: str
    fund_house: str
    isin_growth: str | None = None