#!/usr/bin/env python3
"""
NAV Download + DB Ingest - Standalone Cron Job
Ready to use - just verify function names match YOUR app.py

Schedule in crontab:
  0 11 * * * /usr/bin/python3 /path/to/nav_cron_job_FINAL.py >> ~/nav_cron.log 2>&1
  0 15 * * * /usr/bin/python3 /path/to/nav_cron_job_FINAL.py >> ~/nav_cron.log 2>&1
"""

import logging
import sys
import os
from pathlib import Path
from datetime import datetime

# Add project path
PROJECT_DIR = Path(__file__).parent
sys.path.insert(0, str(PROJECT_DIR))

# ═══════════════════════════════════════════════════════════════════════════════
# VERIFY THESE IMPORTS MATCH YOUR APP.PY
# ═══════════════════════════════════════════════════════════════════════════════

# From app.py line 2447:
try:
    from app import download_and_save_nav_if_needed
except ImportError:
    print("ERROR: Cannot import download_and_save_nav_if_needed from app.py")
    print("Check: Does your app.py have this function?")
    sys.exit(1)

# From nav_data_ingestion.py:
try:
    from nav_data_ingestion import ingest_nav_file_to_db
except ImportError:
    print("ERROR: Cannot import ingest_nav_file_to_db from nav_data_ingestion.py")
    sys.exit(1)

# ═══════════════════════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════════════════════

LOG_FILE = os.path.expanduser("~/nav_cron.log")  # Home directory

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("nav_cron")


def main():
    """Download NAV → Insert to DB → Log results"""
    log.info("=" * 80)
    log.info("NAV CRON JOB STARTED at %s", datetime.now().isoformat())
    
    exit_code = 0
    
    try:
        # Step 1: Download NAV file
        log.info("Step 1: Downloading NAV...")
        download_result = download_and_save_nav_if_needed(force=True)
        
        if not download_result.get('ok'):
            log.error("❌ Download failed: %s", download_result.get('reason', 'Unknown error'))
            exit_code = 1
        else:
            filepath = download_result.get('filepath')
            log.info("✅ Download successful: %s", filepath)
            
            # Step 2: Ingest to DB
            log.info("Step 2: Ingesting NAV to DB...")
            ingest_result = ingest_nav_file_to_db(filepath)
            
            if not ingest_result.get('ok'):
                log.error("❌ Ingestion failed: %s", ingest_result.get('reason', 'Unknown error'))
                exit_code = 1
            else:
                log.info(
                    "✅ Ingestion successful: %d inserted, %d skipped (date: %s, fund houses: %s)",
                    ingest_result['inserted'],
                    ingest_result['skipped'],
                    ingest_result['nav_date'],
                    ', '.join(ingest_result.get('fund_houses', []))
                )
        
        if exit_code == 0:
            log.info("✅ NAV CRON JOB COMPLETED SUCCESSFULLY")
        else:
            log.warning("⚠️  NAV CRON JOB COMPLETED WITH ERRORS")
        
        return exit_code
        
    except ImportError as e:
        log.exception("❌ IMPORT ERROR - Check that all modules exist")
        return 1
    except Exception as e:
        log.exception("❌ FATAL ERROR in NAV cron job")
        return 1
    finally:
        log.info("=" * 80)


if __name__ == "__main__":
    sys.exit(main())
