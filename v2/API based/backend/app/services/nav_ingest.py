from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import text

from app.core.db import SessionLocal
from app.services.cache import bump

log = logging.getLogger("nav_ingest")


def ingest_nav_file_to_db(filepath: str) -> dict:
    try:
        rows = _parse_nav_file(filepath)
        result = _insert_nav_rows(rows)
        log.info("[NAV-DB] Ingested %d records (skipped %d) for date %s",
                 result["inserted"], result["skipped"], result["nav_date"])
        return {
            "ok": True,
            "inserted": result["inserted"],
            "skipped": result["skipped"],
            "nav_date": result["nav_date"],
            "fund_houses": result["fund_houses"],
            "reason": f"Inserted {result['inserted']} NAV records",
        }
    except Exception as e:
        log.exception("[NAV-DB] Ingestion failed")
        return {"ok": False, "inserted": 0, "skipped": 0, "nav_date": None,
                "fund_houses": set(), "reason": f"Ingestion failed: {e}"}


def _parse_nav_file(filepath: str) -> list[dict]:
    rows: list[dict] = []
    current_fund_house = "Unknown"
    with open(filepath, "r", encoding="utf-8-sig") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            if ";" not in line:
                if "Open Ended" in line or "Close Ended" in line or "Interval" in line:
                    continue
                current_fund_house = line.replace(" Mutual Fund", "").strip()
                continue

            parts = line.split(";")
            if len(parts) < 6:
                continue

            try:
                scheme_code = int(parts[0].strip())
                isin_payout = parts[1].strip() or None
                isin_reinvest = parts[2].strip() or None
                scheme_name = parts[3].strip()

                if len(parts) >= 8:
                    plan, option = parts[4].strip(), parts[5].strip()
                    nav_value = float(parts[6].strip())
                    date_str = parts[7].strip()
                    if plan.lower() != "regular plan":
                        continue
                else:
                    plan, option = "Unknown", "Unknown"
                    nav_value = float(parts[4].strip())
                    date_str = parts[5].strip()

                if nav_value <= 0:
                    continue

                nav_date = datetime.strptime(date_str, "%d-%b-%Y").date()
                rows.append({
                    "scheme_code": scheme_code,
                    "isin_payout": isin_payout,
                    "isin_reinvest": isin_reinvest,
                    "scheme_name": scheme_name,
                    "fund_house": current_fund_house,
                    "plan": plan,
                    "option": option,
                    "nav_value": nav_value,
                    "nav_date": nav_date,
                })
            except (ValueError, IndexError):
                continue
    return rows


def _insert_nav_rows(rows: list[dict]) -> dict:
    inserted = skipped = 0
    fund_houses: set[str] = set()
    nav_date = None

    with SessionLocal() as s:
        for r in rows:
            try:
                nav_date = r["nav_date"]
                fund_houses.add(r["fund_house"])

                s.execute(
                    text("""
                        INSERT OR IGNORE INTO nav_schemes
                            (scheme_code, scheme_name, fund_house)
                        VALUES (:scheme_code, :scheme_name, :fund_house)
                    """),
                    {"scheme_code": r["scheme_code"],
                     "scheme_name": r["scheme_name"],
                     "fund_house": r["fund_house"]},
                )
                s.execute(
                    text("""
                        INSERT OR IGNORE INTO nav_options
                            (scheme_code, plan, option_name, isin_payout, isin_reinvest)
                        VALUES (:scheme_code, :plan, :option, :isin_payout, :isin_reinvest)
                    """),
                    {"scheme_code": r["scheme_code"], "plan": r["plan"],
                     "option": r["option"], "isin_payout": r["isin_payout"],
                     "isin_reinvest": r["isin_reinvest"]},
                )
                row = s.execute(
                    text("""SELECT id FROM nav_options
                            WHERE scheme_code = :c AND plan = :p AND option_name = :o"""),
                    {"c": r["scheme_code"], "p": r["plan"], "o": r["option"]},
                ).fetchone()
                if not row:
                    skipped += 1
                    continue
                s.execute(
                    text("""INSERT OR REPLACE INTO nav_history
                            (nav_option_id, nav_value, nav_date)
                            VALUES (:oid, :v, :d)"""),
                    {"oid": row[0], "v": r["nav_value"], "d": r["nav_date"]},
                )
                inserted += 1
            except Exception as e:
                log.warning("[NAV-DB] Row skipped (code %s): %s", r.get("scheme_code"), e)
                skipped += 1
        s.commit()

    if inserted:
        bump()

    return {"inserted": inserted, "skipped": skipped,
            "nav_date": nav_date, "fund_houses": fund_houses}