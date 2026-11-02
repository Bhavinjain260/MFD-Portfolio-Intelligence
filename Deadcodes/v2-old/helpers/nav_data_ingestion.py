"""
NAV DB Ingestion - Plugs into download_and_save_nav() flow
Automatically imports NAV data to DB after file is downloaded.
Dynamically detects Fund House names from the file structure.
"""

import logging
from datetime import datetime
from init_db import get_conn

log = logging.getLogger("nav_db")


def ingest_nav_file_to_db(filepath: str) -> dict:
    try:
        rows = _parse_nav_file(filepath)
        result = _insert_nav_rows(rows)
        log.info(
            "[NAV-DB] Ingested %d records (skipped %d) for date %s",
            result['inserted'], result['skipped'], result['nav_date']
        )
        return {
            'ok': True,
            'inserted': result['inserted'],
            'skipped': result['skipped'],
            'nav_date': result['nav_date'],
            'fund_houses': result['fund_houses'],
            'reason': f"Inserted {result['inserted']} NAV records"
        }
    except Exception as e:
        log.exception("[NAV-DB] Ingestion failed")
        return {
            'ok': False,
            'inserted': 0,
            'skipped': 0,
            'nav_date': None,
            'fund_houses': set(),
            'reason': f"Ingestion failed: {e}"
        }


def _parse_nav_file(filepath: str) -> list[dict]:
    """
    Parse AMFI text file dynamically. 
    Reads the AMC name from the file structure, skipping hardcoded keyword matching.
    Handles BOTH 8-column (Plan/Option present) and 6-column (historical/fallback) formats.
    """
    rows = []
    current_fund_house = "Unknown"
    
    try:
        with open(filepath, 'r', encoding='utf-8-sig') as f:
            for line_no, raw_line in enumerate(f, 1):
                line = raw_line.strip()
                if not line:
                    continue
                
                if ';' not in line:
                    if 'Open Ended' in line or 'Close Ended' in line or 'Interval' in line:
                        continue
                    current_fund_house = line.replace(" Mutual Fund", "").strip()
                    continue
                
                parts = line.split(';')
                if len(parts) < 6:
                    continue
                
                try:
                    scheme_code = int(parts[0].strip())
                    isin_payout = parts[1].strip() or None
                    isin_reinvest = parts[2].strip() or None
                    scheme_name = parts[3].strip()
                    
                    if len(parts) >= 8:
                        plan = parts[4].strip()
                        option = parts[5].strip()
                        nav_value = float(parts[6].strip())
                        date_str = parts[7].strip()
                        
                        # ▼▼▼ REGULAR PLAN FILTER ▼▼▼
                        # Strictly insert only Regular Plans for 8-col format.
                        if plan.lower() != "regular plan":
                            continue
                    else:
                        # 6-col format fallback (no Plan/Option columns)
                        plan = "Unknown"
                        option = "Unknown"
                        nav_value = float(parts[4].strip())
                        date_str = parts[5].strip()
                    
                    nav_date = datetime.strptime(date_str, "%d-%b-%Y").date()
                    
                    if nav_value <= 0:
                        continue

                    rows.append({
                        'scheme_code': scheme_code,
                        'isin_payout': isin_payout,
                        'isin_reinvest': isin_reinvest,
                        'scheme_name': scheme_name,
                        'fund_house': current_fund_house,
                        'plan': plan,
                        'option': option,
                        'nav_value': nav_value,
                        'nav_date': nav_date
                    })
                except (ValueError, IndexError):
                    continue
    
    except IOError as e:
        log.error("[NAV-DB] Cannot read file %s: %s", filepath, e)
        raise
    
    return rows


def _insert_nav_rows(rows: list[dict]) -> dict:
    inserted = 0
    skipped = 0
    fund_houses = set()
    nav_date = None
    
    with get_conn() as conn:
        c = conn.cursor()
        
        for row in rows:
            try:
                nav_date = row['nav_date']
                fund_houses.add(row['fund_house'])
                
                c.execute("""
                    INSERT OR IGNORE INTO nav_schemes 
                    (scheme_code, scheme_name, fund_house)
                    VALUES (?, ?, ?)
                """, (
                    row['scheme_code'],
                    row['scheme_name'],
                    row['fund_house']
                ))
                
                c.execute("""
                    INSERT OR IGNORE INTO nav_options
                    (scheme_code, plan, option_name, isin_payout, isin_reinvest)
                    VALUES (?, ?, ?, ?, ?)
                """, (
                    row['scheme_code'],
                    row['plan'],
                    row['option'],
                    row['isin_payout'],
                    row['isin_reinvest']
                ))
                
                c.execute("""
                    SELECT id FROM nav_options 
                    WHERE scheme_code = ? AND plan = ? AND option_name = ?
                """, (row['scheme_code'], row['plan'], row['option']))
                result = c.fetchone()
                
                if not result:
                    skipped += 1
                    continue
                
                nav_option_id = result[0]
                
                c.execute("""
                    INSERT OR REPLACE INTO nav_history
                    (nav_option_id, nav_value, nav_date)
                    VALUES (?, ?, ?)
                """, (nav_option_id, row['nav_value'], row['nav_date']))
                
                inserted += 1
                
            except Exception as e:
                log.warning("[NAV-DB] Row skipped (code %s): %s", row.get('scheme_code'), e)
                skipped += 1
                continue
        
        conn.commit()
    
    return {
        'inserted': inserted,
        'skipped': skipped,
        'nav_date': nav_date,
        'fund_houses': fund_houses
    }