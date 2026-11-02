"""
NAV Query Functions - Replace file/memory based lookups with DB queries
All NAV data now comes from database
"""

import logging
from datetime import datetime, date, timedelta
from typing import Optional, Dict, Tuple, List
from init_db import get_conn

log = logging.getLogger("nav_queries")


# ============================================================================
# LATEST NAV LOOKUPS (replaces in-memory index)
# ============================================================================

def get_latest_nav_by_isin(isin: str) -> Optional[Tuple[float, str]]:
    """
    Get latest NAV and date for an ISIN
    
    Returns: (nav_value, nav_date_str) or None
    Replaces: _amfi.get_nav(isin) / nav_by_isin[isin]
    """
    with get_conn() as conn:
        row = conn.execute("""
            SELECT nh.nav_value, nh.nav_date
            FROM nav_history nh
            JOIN nav_options no ON nh.nav_option_id = no.id
            WHERE (no.isin_payout = ? OR no.isin_reinvest = ?)
            ORDER BY nh.nav_date DESC
            LIMIT 1
        """, (isin, isin)).fetchone()
    
    if row:
        return (float(row[0]), str(row[1]))
    return None


def get_latest_nav_by_scheme_and_plan(scheme_code: int, plan: str = "Regular Plan") -> Optional[Tuple[float, str]]:
    """
    Get latest NAV for scheme + plan combo (e.g., scheme 135759, Regular Plan)
    
    Returns: (nav_value, nav_date_str) or None
    """
    with get_conn() as conn:
        row = conn.execute("""
            SELECT nh.nav_value, nh.nav_date
            FROM nav_history nh
            JOIN nav_options no ON nh.nav_option_id = no.id
            WHERE no.scheme_code = ? AND no.plan = ?
            ORDER BY nh.nav_date DESC, no.option_name
            LIMIT 1
        """, (scheme_code, plan)).fetchone()
    
    if row:
        return (float(row[0]), str(row[1]))
    return None


def get_all_latest_navs() -> Dict[str, Tuple[float, str]]:
    """
    Get all latest NAVs by ISIN (replaces _amfi.nav_by_isin)
    
    Returns: {isin: (nav_value, nav_date_str), ...}
    """
    navs = {}
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT DISTINCT
                COALESCE(no.isin_payout, no.isin_reinvest) as isin,
                nh.nav_value,
                nh.nav_date
            FROM nav_history nh
            JOIN nav_options no ON nh.nav_option_id = no.id
            WHERE nh.nav_date = (
                SELECT MAX(nav_date) FROM nav_history
            )
        """).fetchall()
    
    for row in rows:
        if row[0]:  # If ISIN exists
            navs[row[0]] = (float(row[1]), str(row[2]))
    
    return navs


def get_nav_for_date(isin: str, nav_date: str) -> Optional[float]:
    """
    Get NAV for specific ISIN on specific date
    
    Args:
        isin: ISIN code
        nav_date: Date string (YYYY-MM-DD or DD-Mon-YYYY)
    
    Returns: nav_value or None
    """
    # Handle date format conversion
    if "-" in nav_date and len(nav_date) == 10:
        date_str = nav_date  # Already YYYY-MM-DD
    else:
        try:
            date_str = datetime.strptime(nav_date, "%d-%b-%Y").strftime("%Y-%m-%d")
        except ValueError:
            return None
    
    with get_conn() as conn:
        row = conn.execute("""
            SELECT nh.nav_value
            FROM nav_history nh
            JOIN nav_options no ON nh.nav_option_id = no.id
            WHERE (no.isin_payout = ? OR no.isin_reinvest = ?)
                AND nh.nav_date = ?
            LIMIT 1
        """, (isin, isin, date_str)).fetchone()
    
    return float(row[0]) if row else None


# ============================================================================
# PREVIOUS NAV (for comparison)
# ============================================================================

def get_previous_nav_date() -> Optional[str]:
    """
    Get the date of the previous (penultimate) NAV snapshot
    
    Returns: Date string (YYYY-MM-DD) or None
    Replaces: get_previous_nav_date()
    """
    with get_conn() as conn:
        row = conn.execute("""
            SELECT DISTINCT nav_date
            FROM nav_history
            ORDER BY nav_date DESC
            LIMIT 2
            OFFSET 1
        """).fetchone()
    
    return str(row[0]) if row else None


def get_previous_nav_by_isin(isin: str) -> Optional[Tuple[float, str]]:
    """
    Get previous business day NAV for an ISIN
    
    Returns: (nav_value, nav_date_str) or None
    """
    with get_conn() as conn:
        row = conn.execute("""
            SELECT nh.nav_value, nh.nav_date
            FROM nav_history nh
            JOIN nav_options no ON nh.nav_option_id = no.id
            WHERE (no.isin_payout = ? OR no.isin_reinvest = ?)
            ORDER BY nh.nav_date DESC
            LIMIT 2
            OFFSET 1
        """, (isin, isin)).fetchone()
    
    if row:
        return (float(row[0]), str(row[1]))
    return None


def get_previous_navs_map() -> Dict[str, Tuple[float, str]]:
    """
    Get all previous (penultimate) NAVs by ISIN
    
    Returns: {isin: (nav_value, nav_date_str), ...}
    Replaces: load_previous_nav_map()
    """
    navs = {}
    with get_conn() as conn:
        # Get second-latest date
        prev_date_row = conn.execute("""
            SELECT DISTINCT nav_date
            FROM nav_history
            ORDER BY nav_date DESC
            LIMIT 2
            OFFSET 1
        """).fetchone()
        
        if not prev_date_row:
            return navs
        
        prev_date = prev_date_row[0]
        
        # Get all NAVs for that date
        rows = conn.execute("""
            SELECT DISTINCT
                COALESCE(no.isin_payout, no.isin_reinvest) as isin,
                nh.nav_value
            FROM nav_history nh
            JOIN nav_options no ON nh.nav_option_id = no.id
            WHERE nh.nav_date = ?
        """, (prev_date,)).fetchall()
    
    for row in rows:
        if row[0]:
            navs[row[0]] = (float(row[1]), str(prev_date))
    
    return navs


# ============================================================================
# NAV HISTORY & TRENDS
# ============================================================================

def get_nav_history(isin: str, days: int = 30) -> List[Dict]:
    """
    Get NAV history for last N days
    
    Returns: [{'nav_date': '2026-09-04', 'nav_value': 26.41, 'daily_change': 0.12, ...}, ...]
    """
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT 
                nh.nav_date,
                nh.nav_value,
                LAG(nh.nav_value) OVER (ORDER BY nh.nav_date) as prev_nav
            FROM nav_history nh
            JOIN nav_options no ON nh.nav_option_id = no.id
            WHERE (no.isin_payout = ? OR no.isin_reinvest = ?)
            ORDER BY nh.nav_date DESC
            LIMIT ?
        """, (isin, isin, days)).fetchall()
    
    history = []
    for row in rows:
        nav_date, nav_value, prev_nav = row
        daily_change = round(nav_value - prev_nav, 4) if prev_nav else None
        daily_pct = round((daily_change / prev_nav * 100), 2) if prev_nav else None
        
        history.append({
            'nav_date': str(nav_date),
            'nav_value': float(nav_value),
            'prev_nav': float(prev_nav) if prev_nav else None,
            'daily_change': daily_change,
            'daily_pct': daily_pct
        })
    
    return list(reversed(history))


# ============================================================================
# SCHEME METADATA
# ============================================================================

def get_scheme_info(scheme_code: int) -> Optional[Dict]:
    """
    Get scheme metadata from DB
    
    Returns: {'scheme_code': 135759, 'scheme_name': '...', 'fund_house': '...', ...}
    """
    with get_conn() as conn:
        row = conn.execute("""
            SELECT scheme_code, scheme_name, fund_house, isin_growth
            FROM nav_schemes
            WHERE scheme_code = ?
        """, (scheme_code,)).fetchone()
    
    if row:
        return {
            'scheme_code': row[0],
            'scheme_name': row[1],
            'fund_house': row[2],
            'isin_growth': row[3]
        }
    return None


def get_scheme_options(scheme_code: int, plan: str = None) -> List[Dict]:
    """
    Get all option variants for a scheme (optionally filtered by plan)
    
    Returns: [{'option_name': 'Growth Option', 'plan': 'Regular Plan', 'isin_payout': '...', ...}, ...]
    """
    with get_conn() as conn:
        if plan:
            rows = conn.execute("""
                SELECT id, plan, option_name, isin_payout, isin_reinvest
                FROM nav_options
                WHERE scheme_code = ? AND plan = ?
                ORDER BY plan, option_name
            """, (scheme_code, plan)).fetchall()
        else:
            rows = conn.execute("""
                SELECT id, plan, option_name, isin_payout, isin_reinvest
                FROM nav_options
                WHERE scheme_code = ?
                ORDER BY plan, option_name
            """, (scheme_code,)).fetchall()
    
    options = []
    for row in rows:
        options.append({
            'id': row[0],
            'plan': row[1],
            'option_name': row[2],
            'isin_payout': row[3],
            'isin_reinvest': row[4]
        })
    
    return options


# ============================================================================
# STATISTICS
# ============================================================================

def get_latest_nav_date() -> Optional[str]:
    """Get the latest NAV date in DB"""
    with get_conn() as conn:
        row = conn.execute("""
            SELECT MAX(nav_date) FROM nav_history
        """).fetchone()
    
    return str(row[0]) if row and row[0] else None


def get_nav_count_by_date(nav_date: str) -> int:
    """Get count of NAV records for a date"""
    with get_conn() as conn:
        row = conn.execute("""
            SELECT COUNT(*) FROM nav_history WHERE nav_date = ?
        """, (nav_date,)).fetchone()
    
    return row[0] if row else 0


def get_fund_house_summary() -> List[Dict]:
    """
    Get summary: schemes and records per fund house
    
    Returns: [{'fund_house': 'Axis', 'schemes': 10, 'options': 25, 'records': 250}, ...]
    """
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT 
                s.fund_house,
                COUNT(DISTINCT s.scheme_code) as schemes,
                COUNT(DISTINCT no.id) as options,
                COUNT(*) as records
            FROM nav_schemes s
            LEFT JOIN nav_options no ON s.scheme_code = no.scheme_code
            LEFT JOIN nav_history nh ON no.id = nh.nav_option_id
            GROUP BY s.fund_house
            ORDER BY schemes DESC
        """).fetchall()
    
    summary = []
    for row in rows:
        summary.append({
            'fund_house': row[0],
            'schemes': row[1],
            'options': row[2],
            'records': row[3]
        })
    
    return summary


# ============================================================================
# REGULAR PLAN FILTERING (for reports/analysis)
# ============================================================================

def get_regular_plan_navs() -> Dict[str, Tuple[float, str]]:
    """
    Get all latest NAVs for Regular Plan only
    
    Returns: {isin: (nav_value, nav_date_str), ...}
    """
    navs = {}
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT DISTINCT
                COALESCE(no.isin_payout, no.isin_reinvest) as isin,
                nh.nav_value,
                nh.nav_date
            FROM nav_history nh
            JOIN nav_options no ON nh.nav_option_id = no.id
            WHERE no.plan = 'Regular Plan'
                AND nh.nav_date = (
                    SELECT MAX(nav_date) FROM nav_history
                )
        """).fetchall()
    
    for row in rows:
        if row[0]:
            navs[row[0]] = (float(row[1]), str(row[2]))
    
    return navs


def get_regular_plan_history(isin: str, days: int = 30) -> List[Dict]:
    """Get NAV history for Regular Plan schemes only"""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT 
                nh.nav_date,
                nh.nav_value,
                LAG(nh.nav_value) OVER (ORDER BY nh.nav_date) as prev_nav
            FROM nav_history nh
            JOIN nav_options no ON nh.nav_option_id = no.id
            WHERE (no.isin_payout = ? OR no.isin_reinvest = ?)
                AND no.plan = 'Regular Plan'
            ORDER BY nh.nav_date DESC
            LIMIT ?
        """, (isin, isin, days)).fetchall()
    
    history = []
    for row in rows:
        nav_date, nav_value, prev_nav = row
        daily_change = round(nav_value - prev_nav, 4) if prev_nav else None
        
        history.append({
            'nav_date': str(nav_date),
            'nav_value': float(nav_value),
            'daily_change': daily_change
        })
    
    return list(reversed(history))
    