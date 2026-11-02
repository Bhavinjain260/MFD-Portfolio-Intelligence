from __future__ import annotations

import json
from datetime import date as date_cls
from datetime import timedelta
from pathlib import Path

_HOLIDAYS_FILE = Path(__file__).with_name("holidays.json")
_HOLIDAYS: dict[str, str] = json.loads(_HOLIDAYS_FILE.read_text(encoding="utf-8"))


def is_trading_day(target: date_cls) -> bool:
    if target.weekday() >= 5:
        return False
    return target.strftime("%Y-%m-%d") not in _HOLIDAYS


def get_expected_nav_date(download_date: date_cls, is_domestic: bool = True) -> date_cls | None:
    nav_date = download_date - timedelta(days=1 if is_domestic else 2)
    while not is_trading_day(nav_date):
        nav_date -= timedelta(days=1)
    return nav_date