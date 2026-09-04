# ==================== TRADING DAY CALENDAR ====================

import json
from datetime import date as date_cls, timedelta

# NSE 2026 holidays (add/update as needed)
NSE_HOLIDAYS_2026 = {
    "2026-01-26": "Republic Day",
    "2026-03-25": "Holi",
    "2026-04-02": "Good Friday",
    "2026-04-10": "Eid ul-Fitr",
    "2026-04-21": "Ram Navami",
    "2026-05-01": "May Day",
    "2026-08-15": "Independence Day",
    "2026-08-28": "Janmashtami",
    "2026-09-16": "Milad-un-Nabi",
    "2026-10-02": "Gandhi Jayanti",
    "2026-10-24": "Dussehra",
    "2026-11-08": "Diwali",
    "2026-11-09": "Diwali (Day 2)",
    "2026-12-25": "Christmas",
}

def is_trading_day(target_date: date_cls) -> bool:
    """
    Check if NSE is open on this date.
    - Returns False for weekends (Sat/Sun)
    - Returns False for NSE holidays
    - Returns True otherwise
    """
    # Weekend check (weekday() returns 5=Sat, 6=Sun)
    if target_date.weekday() >= 5:
        return False
    
    # Holiday check
    iso_date = target_date.strftime("%Y-%m-%d")
    return iso_date not in NSE_HOLIDAYS_2026

def get_expected_nav_date(download_date: date_cls, is_domestic: bool = True) -> date_cls | None:
    """
    What NAV date should a download on `download_date` contain?
    
    For domestic funds (download at 11 PM):
      - NAV is for yesterday's closing (if yesterday was trading day)
      - If yesterday was non-trading, backtrack to last trading day
    
    For international funds (download at 11 AM next day):
      - NAV is for 2 days ago's closing (due to lag)
      - If that date was non-trading, backtrack
    """
    if is_domestic:
        # 11 PM download = yesterday's NAV
        nav_date = download_date - timedelta(days=1)
    else:
        # 11 AM next-day download = 2 days ago NAV
        nav_date = download_date - timedelta(days=2)
    
    # Backtrack if non-trading day
    while not is_trading_day(nav_date):
        nav_date -= timedelta(days=1)
    
    return nav_date