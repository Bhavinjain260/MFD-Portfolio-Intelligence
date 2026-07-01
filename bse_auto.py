"""
BSE Star MF — Scheme Code Master Physical
Background-threaded auto-downloader using Selenium.
Non-blocking: UI stays responsive while download runs.
"""

import os
import re
import time
import threading
import logging
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════
# CUSTOM DOWNLOAD PATH
#   Override: set environment variable BSE_SCHEME_DIR=/absolute/path
# ═══════════════════════════════════════════════════════════
DEFAULT_DOWNLOAD_DIR = "Reports/Bse/scheme_master_auto_download"

BSE_SCHEME_URL = "https://www.bsestarmf.in/RptSchemeMaster.aspx"
REPORT_LABEL = "Scheme Code Master Physical"
DOWNLOAD_TIMEOUT = 120  # Increased for slow site
PAGE_LOAD_TIMEOUT = 45


def _today_stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _find_latest_download(download_dir: Path, pattern: str = r"Scheme.*\.txt|Scheme.*\.csv|bse_scheme.*\.txt") -> Path | None:
    candidates = [f for f in download_dir.iterdir() if f.is_file() and re.search(pattern, f.name, re.I)]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _is_today_file(path: Path) -> bool:
    if not path or not path.exists():
        return False
    mtime = datetime.fromtimestamp(path.stat().st_mtime)
    return mtime.strftime("%Y-%m-%d") == _today_stamp()


def get_download_dir() -> Path:
    dir_path = os.environ.get("BSE_SCHEME_DIR", DEFAULT_DOWNLOAD_DIR)
    p = Path(dir_path)
    p.mkdir(parents=True, exist_ok=True)
    return p


# ═══════════════════════════════════════════════════════════
# BACKGROUND THREAD STATE
# ═══════════════════════════════════════════════════════════
_download_status = {
    "running": False,
    "done": False,
    "ok": False,
    "path": None,
    "msg": "",
    "started_at": None,
    "finished_at": None,
}


def get_download_status() -> dict:
    """Thread-safe read of current download status."""
    return _download_status.copy()


def _set_status(**kwargs):
    _download_status.update(kwargs)


def _reset_status():
    _download_status.update({
        "running": False,
        "done": False,
        "ok": False,
        "path": None,
        "msg": "",
        "started_at": None,
        "finished_at": None,
    })


def _do_download() -> dict:
    """The actual blocking Selenium work. Runs in background thread."""
    out = get_download_dir()

    # Already have today's file?
    existing = _find_latest_download(out)
    if existing and _is_today_file(existing):
        return {
            "ok": True,
            "path": str(existing),
            "msg": f"Already have today's file: {existing.name}",
            "source": "cache"
        }

    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import Select, WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        from webdriver_manager.chrome import ChromeDriverManager
    except ImportError as e:
        return {
            "ok": False,
            "path": None,
            "msg": f"Missing dependency: {e}. Run: pip install selenium webdriver-manager",
            "source": "auto"
        }

    chrome_opts = Options()
    chrome_opts.add_argument("--headless=new")
    chrome_opts.add_argument("--no-sandbox")
    chrome_opts.add_argument("--disable-dev-shm-usage")
    chrome_opts.add_argument("--disable-gpu")
    chrome_opts.add_argument("--window-size=1920,1080")
    # Reduce noise
    chrome_opts.add_argument("--log-level=3")
    chrome_opts.add_experimental_option("excludeSwitches", ["enable-logging"])

    prefs = {
        "download.default_directory": str(out.resolve()),
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "safebrowsing.enabled": True,
    }
    chrome_opts.add_experimental_option("prefs", prefs)

    driver = None
    try:
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=chrome_opts)
        driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
        wait = WebDriverWait(driver, 30)

        log.info("[BSE-AUTO] Opening %s", BSE_SCHEME_URL)
        driver.get(BSE_SCHEME_URL)

        # Wait for dropdown
        ddl = wait.until(
            EC.presence_of_element_located((By.ID, "ContentPlaceHolder1_ddlschememaster"))
        )
        select = Select(ddl)
        select.select_by_visible_text(REPORT_LABEL)
        log.info("[BSE-AUTO] Selected '%s'", REPORT_LABEL)

        # Click download
        btn = wait.until(
            EC.element_to_be_clickable((By.ID, "ContentPlaceHolder1_btnTextDownload"))
        )
        btn.click()
        log.info("[BSE-AUTO] Download clicked")

        # Wait for file
        deadline = time.time() + DOWNLOAD_TIMEOUT
        downloaded = None
        while time.time() < deadline:
            latest = _find_latest_download(out)
            if latest and latest.stat().st_size > 0 and (time.time() - latest.stat().st_mtime) > 2:
                downloaded = latest
                break
            time.sleep(1)

        if not downloaded:
            return {
                "ok": False,
                "path": None,
                "msg": "Download timed out — file did not appear.",
                "source": "auto"
            }

        # Rename to canonical name
        today_file = out / f"bse_scheme_master_physical_{_today_stamp()}.txt"
        if today_file.exists():
            today_file.unlink()
        downloaded.rename(today_file)

        log.info("[BSE-AUTO] Saved %s (%s bytes)", today_file.name, today_file.stat().st_size)
        return {
            "ok": True,
            "path": str(today_file),
            "msg": f"Downloaded {today_file.name}",
            "source": "auto"
        }

    except Exception as e:
        log.exception("[BSE-AUTO] Selenium failed")
        return {
            "ok": False,
            "path": None,
            "msg": f"Selenium error: {e}",
            "source": "auto"
        }

    finally:
        if driver:
            driver.quit()


def _download_worker():
    """Runs _do_download() in background and updates shared status."""
    _set_status(running=True, done=False, started_at=datetime.now().isoformat())
    result = _do_download()
    _set_status(
        running=False,
        done=True,
        ok=result["ok"],
        path=result.get("path"),
        msg=result["msg"],
        finished_at=datetime.now().isoformat(),
    )


def start_background_download() -> None:
    """Kick off a background thread to do the download. Non-blocking."""
    if _download_status["running"]:
        return  # Already running

    _reset_status()
    t = threading.Thread(target=_download_worker, daemon=True)
    t.start()


def should_auto_download() -> bool:
    """True if today's file does not yet exist."""
    out = get_download_dir()
    existing = _find_latest_download(out)
    return not (existing and _is_today_file(existing))


def get_latest_file_path() -> str | None:
    out = get_download_dir()
    latest = _find_latest_download(out)
    return str(latest) if latest else None


def parse_and_import_latest(parse_func) -> dict:
    """
    One-shot: download (or use cache) → parse → return result dict.
    Blocking — use only from explicit button clicks, not startup.
    """
    result = _do_download()
    if not result["ok"]:
        return {"ok": False, "db_ok": False, "msg": result["msg"]}

    path = result["path"]
    if not path or not Path(path).exists():
        return {"ok": False, "db_ok": False, "msg": "Download succeeded but file not found."}

    with open(path, "rb") as f:
        db_ok, db_msg, preview = parse_func(f, replace=False)

    return {
        "ok": True,
        "db_ok": db_ok,
        "msg": f"{result['msg']} | DB: {db_msg}",
        "path": path,
        "preview": preview,
    }