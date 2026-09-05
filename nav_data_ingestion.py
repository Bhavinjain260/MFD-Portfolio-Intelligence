"""
NAV DB Ingestion - Plugs into download_and_save_nav() flow
Automatically imports ONLY Regular plans to DB after file is downloaded.
Dynamically detects Fund House names from the file structure.
"""

import logging
from datetime import datetime
from init_db import get_conn

log = logging.getLogger("nav_db")


def ingest_nav_file_to_db(filepath: str) -> dict:
    """
    Read downloaded NAV file and insert Regular plans only.
    
    Args:
        filepath: Full path to nav_YYYY-MM-DD.txt file
        
    Returns:
        {
            'ok': bool,
            'inserted': int,
            'skipped': int,
            'nav_date': date,
            'fund_houses': set,
            'reason': str
        }
    """
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
            'reason': f"Inserted {result['inserted']} Regular NAV records"
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
    Filters out everything except 'Regular Plan'.
    """
    rows = []
    current_fund_house = "Unknown"
    
    try:
        # utf-8-sig handles BOM characters if present in AMFI file
        with open(filepath, 'r', encoding='utf-8-sig') as f:
            for line_no, raw_line in enumerate(f, 1):
                line = raw_line.strip()
                
                # Skip empty lines
                if not line:
                    continue
                
                # Check if it's a standalone text line (no semicolons)
                if ';' not in line:
                    # Ignore category section headers
                    if 'Open Ended' in line or 'Close Ended' in line or 'Interval' in line:
                        continue
                    
                    # If it doesn't have semicolons and isn't a section header, 
                    # it MUST be the Fund House name!
                    # We strip " Mutual Fund" to keep the name clean (e.g., "Axis Mutual Fund" -> "Axis")
                    current_fund_house = line.replace(" Mutual Fund", "").strip()
                    continue
                
                parts = line.split(';')
                if len(parts) < 8:
                    continue
                
                try:
                    scheme_code = int(parts[0].strip())
                    isin_payout = parts[1].strip() or None
                    isin_reinvest = parts[2].strip() or None
                    scheme_name = parts[3].strip()
                    plan = parts[4].strip()
                    option = parts[5].strip()
                    nav_value = float(parts[6].strip())
                    date_str = parts[7].strip()
                    
                    # Parse date (e.g., 04-Sep-2026)
                    nav_date = datetime.strptime(date_str, "%d-%b-%Y").date()
                    
                    # Skip invalid NAV
                    if nav_value <= 0:
                        continue
                    
                    # ▼▼▼ REGULAR PLAN FILTER ▼▼▼
                    # Strictly insert only Regular Plans. 
                    # Direct plans, Institutional plans, and blank plans are skipped.
                    if plan.lower() != "regular plan":
                        continue

                    rows.append({
                        'scheme_code': scheme_code,
                        'isin_payout': isin_payout,
                        'isin_reinvest': isin_reinvest,
                        'scheme_name': scheme_name,
                        'fund_house': current_fund_house,  # Dynamically captured!
                        'plan': plan,
                        'option': option,
                        'nav_value': nav_value,
                        'nav_date': nav_date
                    })
                except (ValueError, IndexError):
                    # Skip malformed rows silently
                    continue
    
    except IOError as e:
        log.error("[NAV-DB] Cannot read file %s: %s", filepath, e)
        raise
    
    return rows


def _insert_nav_rows(rows: list[dict]) -> dict:
    """
    Insert parsed rows into nav_* tables.
    Returns: {'inserted': int, 'skipped': int, 'nav_date': date, 'fund_houses': set}
    """
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
                
                # 1. Insert scheme (INSERT OR IGNORE)
                # Note: isin_growth is intentionally omitted as it belongs in nav_options
                c.execute("""
                    INSERT OR IGNORE INTO nav_schemes 
                    (scheme_code, scheme_name, fund_house)
                    VALUES (?, ?, ?)
                """, (
                    row['scheme_code'],
                    row['scheme_name'],
                    row['fund_house']
                ))
                
                # 2. Insert option (INSERT OR IGNORE)
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
                
                # 3. Get nav_option_id
                c.execute("""
                    SELECT id FROM nav_options 
                    WHERE scheme_code = ? AND plan = ? AND option_name = ?
                """, (row['scheme_code'], row['plan'], row['option']))
                result = c.fetchone()
                
                if not result:
                    skipped += 1
                    continue
                
                nav_option_id = result[0]
                
                # 4. Insert/update history (REPLACE = upsert)
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