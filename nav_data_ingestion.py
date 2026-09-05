"""
NAV DB Ingestion - Plugs into download_and_save_nav() flow
Automatically imports ALL plans (not filtered) to DB after file is downloaded
Filtering by Regular plan happens at query time via nav_queries.py
"""

import logging
from datetime import datetime
from init_db import get_conn

log = logging.getLogger("nav_db")


def ingest_nav_file_to_db(filepath: str) -> dict:
    """
    Read downloaded NAV file and insert Regular plans only
    
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
    Parse AMFI text file (ALL plans - filtering done at query time)
    Format (8-col, Aug 2026+):
        Code;ISIN1;ISIN2;Name;Plan;Option;NAV;Date
    """
    rows = []
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            for line_no, raw_line in enumerate(f, 1):
                line = raw_line.strip()
                
                # Skip empty/header rows
                if not line or len(line.split(';')) < 8:
                    continue
                if line.startswith('Scheme Code') or line.startswith('Open Ended'):
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
                    
                    # Parse date (04-Sep-2026)
                    nav_date = datetime.strptime(date_str, "%d-%b-%Y").date()
                    
                    # Skip invalid NAV only
                    if nav_value <= 0:
                        continue
                    
                    # Store ALL plans (Regular, Direct, Institutional, etc)
                    # Filtering by plan type happens at query time in nav_queries.py
                    rows.append({
                        'scheme_code': scheme_code,
                        'isin_payout': isin_payout,
                        'isin_reinvest': isin_reinvest,
                        'scheme_name': scheme_name,
                        'fund_house': _extract_fund_house(scheme_name),
                        'plan': plan,
                        'option': option,
                        'nav_value': nav_value,
                        'nav_date': nav_date
                    })
                except (ValueError, IndexError) as e:
                    # Skip malformed rows silently
                    continue
    
    except IOError as e:
        log.error("[NAV-DB] Cannot read file %s: %s", filepath, e)
        raise
    
    return rows


def _extract_fund_house(scheme_name: str) -> str:
    """Extract AMC name from scheme name"""
    keywords = {
        'axis': 'Axis',
        'hdfc': 'HDFC',
        'icici': 'ICICI',
        'sbi': 'SBI',
        'franklin': 'Franklin',
        'l&t': 'L&T',
        'dsp': 'DSP',
        'kotak': 'Kotak',
        'uti': 'UTI',
        'aditya birla': 'Aditya Birla',
        'birla': 'Aditya Birla',
        'motilal': 'Motilal',
        'edelweiss': 'Edelweiss',
        'nippon': 'Nippon',
        'idbi': 'IDBI',
        'canara': 'Canara',
        'bandhan': 'Bandhan',
        'baroda': 'Baroda BNP',
        'tata': 'Tata',
        'pgim': 'PGIM',
        'invesco': 'Invesco',
        'itnf': 'ITI'
    }
    
    lower = scheme_name.lower()
    for kw, amc in keywords.items():
        if kw in lower:
            return amc
    return 'Unknown'


def _insert_nav_rows(rows: list[dict]) -> dict:
    """
    Insert parsed rows into nav_* tables
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
                c.execute("""
                    INSERT OR IGNORE INTO nav_schemes 
                    (scheme_code, isin_growth, scheme_name, fund_house)
                    VALUES (?, ?, ?, ?)
                """, (
                    row['scheme_code'],
                    row['isin_reinvest'],
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