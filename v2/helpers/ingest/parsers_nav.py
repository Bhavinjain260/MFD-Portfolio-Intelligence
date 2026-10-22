"""Auto-extracted parsers (Phase 8). Uses data_manager helpers where still shared."""
from __future__ import annotations
import logging
from typing import Optional, Any
import pandas as pd

log = logging.getLogger(__name__)

try:
    from core.db import get_conn, bump
except Exception:
    from init_db import get_conn
    def bump():
        try:
            from data_manager import bump as _b
            _b()
        except Exception:
            pass

# Shared helpers still live in data_manager until fully moved
try:
    from data_manager import (
        raw_val, raw_num, read_csv_auto, _normalize_col,
        clean_str,  # may not exist
    )
except Exception:
    from ingest.common import raw_val, raw_num, read_csv_auto
    def _normalize_col(c):
        return str(c).strip().lower().replace(" ", "_")


def parse_nav_file(file, replace: bool) -> tuple[bool, str, dict]:
    """
    Parses AMFI NAV text file uploaded via Streamlit UI.
    Saves to a temporary file and delegates to nav_data_ingestion 
    to ensure both 6-col and 8-col formats are handled identically.
    """
    import tempfile
    import os
    
    try:
        # Save the Streamlit UploadedFile to a temporary file path
        with tempfile.NamedTemporaryFile(delete=False, suffix=".txt", mode='wb') as tmp:
            tmp.write(file.getvalue())
            tmp_path = tmp.name
            
        # Use the unified ingestion module
        result = nav_data_ingestion.ingest_nav_file_to_db(tmp_path)
        
        # Clean up the temp file
        os.remove(tmp_path)
        
        if result.get('ok'):
            msg = f"Imported {result['inserted']} NAV records | Skipped: {result['skipped']}"
            if result['inserted']:
                bump()
            return True, msg, {
                "rows": result['inserted'], 
                "skipped": result['skipped'], 
                "fund_houses": len(result.get('fund_houses', set()))
            }
        else:
            return False, result.get('reason', 'Unknown error'), {}
            
    except Exception as e:
        return False, f"File read error: {e}", {}


def parse_nav_filepath(filepath: str) -> dict:
    """
    Wrapper for auto-fetch scheduler to pass a file path instead of a Streamlit object.
    """
    try:
        with open(filepath, 'r', encoding='utf-8-sig') as f:
            rows = _parse_nav_text(f)
            if not rows:
                return {'ok': False, 'reason': 'No valid Regular Plan rows found'}
            
            result = _insert_nav_rows(rows)
            return {
                'ok': True,
                'inserted': result['inserted'],
                'skipped': result['skipped'],
                'nav_date': result['nav_date'],
                'fund_houses': result['fund_houses'],
                'reason': f"Inserted {result['inserted']} Regular NAV records"
            }
    except Exception as e:
        return {'ok': False, 'reason': f"Ingestion failed: {e}"}


