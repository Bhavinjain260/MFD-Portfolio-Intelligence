from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import text

from app.core.db import SessionLocal

log = logging.getLogger(__name__)


def get_latest_nav_by_isin(isin: str) -> Optional[tuple[float, str]]:
    with SessionLocal() as s:
        row = s.execute(
            text("""
                SELECT nh.nav_value, nh.nav_date
                FROM nav_history nh
                JOIN nav_options nopt ON nh.nav_option_id = nopt.id
                WHERE (nopt.isin_payout = :i OR nopt.isin_reinvest = :i)
                ORDER BY nh.nav_date DESC
                LIMIT 1
            """),
            {"i": isin},
        ).fetchone()
    return (float(row[0]), str(row[1])) if row else None


def get_latest_nav_by_scheme_and_plan(scheme_code: int, plan: str = "Regular Plan"):
    with SessionLocal() as s:
        row = s.execute(
            text("""
                SELECT nh.nav_value, nh.nav_date
                FROM nav_history nh
                JOIN nav_options nopt ON nh.nav_option_id = nopt.id
                WHERE nopt.scheme_code = :c AND nopt.plan = :p
                ORDER BY nh.nav_date DESC, nopt.option_name
                LIMIT 1
            """),
            {"c": scheme_code, "p": plan},
        ).fetchone()
    return (float(row[0]), str(row[1])) if row else None


def get_all_latest_navs() -> dict[str, tuple[float, str]]:
    with SessionLocal() as s:
        rows = s.execute(text("""
            SELECT DISTINCT
                COALESCE(nopt.isin_payout, nopt.isin_reinvest) AS isin,
                nh.nav_value, nh.nav_date
            FROM nav_history nh
            JOIN nav_options nopt ON nh.nav_option_id = nopt.id
            WHERE nh.nav_date = (SELECT MAX(nav_date) FROM nav_history)
        """)).fetchall()
    return {r[0]: (float(r[1]), str(r[2])) for r in rows if r[0]}


def get_nav_for_date(isin: str, nav_date: str) -> Optional[float]:
    if "-" in nav_date and len(nav_date) == 10:
        date_str = nav_date
    else:
        try:
            date_str = datetime.strptime(nav_date, "%d-%b-%Y").strftime("%Y-%m-%d")
        except ValueError:
            return None
    with SessionLocal() as s:
        row = s.execute(
            text("""
                SELECT nh.nav_value
                FROM nav_history nh
                JOIN nav_options nopt ON nh.nav_option_id = nopt.id
                WHERE (nopt.isin_payout = :i OR nopt.isin_reinvest = :i)
                  AND nh.nav_date = :d
                LIMIT 1
            """),
            {"i": isin, "d": date_str},
        ).fetchone()
    return float(row[0]) if row else None


def get_previous_nav_date() -> Optional[str]:
    with SessionLocal() as s:
        row = s.execute(text("""
            SELECT DISTINCT nav_date FROM nav_history
            ORDER BY nav_date DESC
            LIMIT 1 OFFSET 1
        """)).fetchone()
    return str(row[0]) if row else None


def get_previous_nav_by_isin(isin: str):
    with SessionLocal() as s:
        row = s.execute(
            text("""
                SELECT nh.nav_value, nh.nav_date
                FROM nav_history nh
                JOIN nav_options nopt ON nh.nav_option_id = nopt.id
                WHERE (nopt.isin_payout = :i OR nopt.isin_reinvest = :i)
                ORDER BY nh.nav_date DESC
                LIMIT 1 OFFSET 1
            """),
            {"i": isin},
        ).fetchone()
    return (float(row[0]), str(row[1])) if row else None


def get_previous_navs_map() -> dict[str, tuple[float, str]]:
    with SessionLocal() as s:
        prev_row = s.execute(text("""
            SELECT DISTINCT nav_date FROM nav_history
            ORDER BY nav_date DESC
            LIMIT 1 OFFSET 1
        """)).fetchone()
        if not prev_row:
            return {}
        prev_date = prev_row[0]
        rows = s.execute(
            text("""
                SELECT DISTINCT
                    COALESCE(nopt.isin_payout, nopt.isin_reinvest) AS isin,
                    nh.nav_value
                FROM nav_history nh
                JOIN nav_options nopt ON nh.nav_option_id = nopt.id
                WHERE nh.nav_date = :d
            """),
            {"d": prev_date},
        ).fetchall()
    return {r[0]: (float(r[1]), str(prev_date)) for r in rows if r[0]}


def get_nav_history(isin: str, days: int = 30) -> list[dict]:
    with SessionLocal() as s:
        rows = s.execute(
            text("""
                SELECT nh.nav_date, nh.nav_value,
                       LAG(nh.nav_value) OVER (ORDER BY nh.nav_date) AS prev_nav
                FROM nav_history nh
                JOIN nav_options nopt ON nh.nav_option_id = nopt.id
                WHERE (nopt.isin_payout = :i OR nopt.isin_reinvest = :i)
                ORDER BY nh.nav_date DESC
                LIMIT :n
            """),
            {"i": isin, "n": days},
        ).fetchall()
    out = []
    for nav_date, nav_value, prev_nav in rows:
        dc = round(float(nav_value) - float(prev_nav), 4) if prev_nav else None
        dp = round((dc / float(prev_nav)) * 100, 2) if dc and prev_nav else None
        out.append({
            "nav_date": str(nav_date), "nav_value": float(nav_value),
            "prev_nav": float(prev_nav) if prev_nav else None,
            "daily_change": dc, "daily_pct": dp,
        })
    return list(reversed(out))


def get_scheme_info(scheme_code: int) -> Optional[dict]:
    with SessionLocal() as s:
        row = s.execute(
            text("SELECT scheme_code, scheme_name, fund_house, isin_growth "
                 "FROM nav_schemes WHERE scheme_code = :c"),
            {"c": scheme_code},
        ).fetchone()
    if not row:
        return None
    return {"scheme_code": row[0], "scheme_name": row[1],
            "fund_house": row[2], "isin_growth": row[3]}


def get_scheme_options(scheme_code: int, plan: str | None = None) -> list[dict]:
    with SessionLocal() as s:
        if plan:
            rows = s.execute(
                text("SELECT id, plan, option_name, isin_payout, isin_reinvest "
                     "FROM nav_options WHERE scheme_code = :c AND plan = :p "
                     "ORDER BY plan, option_name"),
                {"c": scheme_code, "p": plan},
            ).fetchall()
        else:
            rows = s.execute(
                text("SELECT id, plan, option_name, isin_payout, isin_reinvest "
                     "FROM nav_options WHERE scheme_code = :c "
                     "ORDER BY plan, option_name"),
                {"c": scheme_code},
            ).fetchall()
    return [{"id": r[0], "plan": r[1], "option_name": r[2],
             "isin_payout": r[3], "isin_reinvest": r[4]} for r in rows]


def get_latest_nav_date() -> Optional[str]:
    with SessionLocal() as s:
        row = s.execute(text("SELECT MAX(nav_date) FROM nav_history")).fetchone()
    return str(row[0]) if row and row[0] else None


def get_nav_count_by_date(nav_date: str) -> int:
    with SessionLocal() as s:
        row = s.execute(
            text("SELECT COUNT(*) FROM nav_history WHERE nav_date = :d"),
            {"d": nav_date},
        ).fetchone()
    return row[0] if row else 0


def get_fund_house_summary() -> list[dict]:
    with SessionLocal() as s:
        rows = s.execute(text("""
            SELECT s.fund_house,
                   COUNT(DISTINCT s.scheme_code) AS schemes,
                   COUNT(DISTINCT nopt.id) AS options,
                   COUNT(*) AS records
            FROM nav_schemes s
            LEFT JOIN nav_options nopt ON s.scheme_code = nopt.scheme_code
            LEFT JOIN nav_history nh ON nopt.id = nh.nav_option_id
            GROUP BY s.fund_house
            ORDER BY schemes DESC
        """)).fetchall()
    return [{"fund_house": r[0], "schemes": r[1], "options": r[2], "records": r[3]} for r in rows]


def get_regular_plan_navs() -> dict[str, tuple[float, str]]:
    with SessionLocal() as s:
        rows = s.execute(text("""
            SELECT DISTINCT
                COALESCE(nopt.isin_payout, nopt.isin_reinvest) AS isin,
                nh.nav_value, nh.nav_date
            FROM nav_history nh
            JOIN nav_options nopt ON nh.nav_option_id = nopt.id
            WHERE nopt.plan = 'Regular Plan'
              AND nh.nav_date = (SELECT MAX(nav_date) FROM nav_history)
        """)).fetchall()
    return {r[0]: (float(r[1]), str(r[2])) for r in rows if r[0]}


def get_regular_plan_history(isin: str, days: int = 30) -> list[dict]:
    with SessionLocal() as s:
        rows = s.execute(
            text("""
                SELECT nh.nav_date, nh.nav_value,
                       LAG(nh.nav_value) OVER (ORDER BY nh.nav_date) AS prev_nav
                FROM nav_history nh
                JOIN nav_options nopt ON nh.nav_option_id = nopt.id
                WHERE (nopt.isin_payout = :i OR nopt.isin_reinvest = :i)
                  AND nopt.plan = 'Regular Plan'
                ORDER BY nh.nav_date DESC
                LIMIT :n
            """),
            {"i": isin, "n": days},
        ).fetchall()
    out = []
    for nav_date, nav_value, prev_nav in rows:
        dc = round(float(nav_value) - float(prev_nav), 4) if prev_nav else None
        out.append({"nav_date": str(nav_date),
                    "nav_value": float(nav_value), "daily_change": dc})
    return list(reversed(out))