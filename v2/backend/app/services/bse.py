from __future__ import annotations

import logging
import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path

from app.core.config import settings

log = logging.getLogger(__name__)

BSE_SCHEME_URL = "https://www.bsestarmf.in/RptSchemeMaster.aspx"
REPORT_VALUE = "SCHEMEMASTERPHYSICAL"
DOWNLOAD_TIMEOUT = 350
PAGE_LOAD_TIMEOUT = 120


def get_download_dir() -> Path:
    p = Path(os.environ.get("BSE_SCHEME_DIR", settings.bse_scheme_dir))
    p.mkdir(parents=True, exist_ok=True)
    return p


def _today_stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _today_filename() -> str:
    return f"bse_scheme_master_physical_{_today_stamp()}.txt"


def _today_done_filename() -> str:
    return f"bse_scheme_master_physical_{_today_stamp()}_done.txt"


def _find_latest_download(d: Path, pattern: str = r"SCHMSTRPHY.*\.txt") -> Path | None:
    files = [f for f in d.iterdir() if f.is_file() and re.search(pattern, f.name, re.I)]
    return max(files, key=lambda p: p.stat().st_mtime) if files else None


def is_uploaded() -> bool:
    return (get_download_dir() / _today_done_filename()).exists()


def has_todays_file() -> bool:
    d = get_download_dir()
    return (d / _today_filename()).exists() or (d / _today_done_filename()).exists()


def get_latest_file_path() -> str | None:
    d = get_download_dir()
    for name in (_today_done_filename(), _today_filename()):
        p = d / name
        if p.exists():
            return str(p)
    latest = _find_latest_download(d)
    return str(latest) if latest else None


def _wait_for_download(out: Path, before: set, timeout: int) -> Path | None:
    waited = 0
    while waited < timeout:
        time.sleep(1)
        waited += 1
        added = set(f.name for f in out.iterdir()) - before
        if any(f.endswith(".crdownload") for f in added):
            continue
        txt = [f for f in added if f.endswith(".txt")]
        if txt:
            return out / txt[0]
    return None


def _do_download() -> dict:
    out = get_download_dir()
    pending = out / _today_filename()
    done = out / _today_done_filename()

    if done.exists():
        return {"ok": True, "path": str(done), "msg": f"Already uploaded: {done.name}"}
    if pending.exists():
        return {"ok": True, "path": str(pending), "msg": f"Already have today's file: {pending.name}"}

    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import Select, WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
    except ImportError as e:
        return {"ok": False, "path": None,
                "msg": f"Missing selenium: {e}. apt install python3-selenium chromium-driver"}

    chromedriver = shutil.which("chromedriver")
    if not chromedriver:
        return {"ok": False, "path": None,
                "msg": "chromedriver not found on PATH — apt install chromium-driver"}

    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--log-level=3")
    opts.add_experimental_option("excludeSwitches", ["enable-logging"])
    opts.add_experimental_option("prefs", {
        "download.default_directory": str(out.resolve()),
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "safebrowsing.enabled": True,
    })

    driver = None
    try:
        driver = webdriver.Chrome(service=Service(chromedriver), options=opts)
        driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
        wait = WebDriverWait(driver, 30)

        log.info("[BSE] Opening %s", BSE_SCHEME_URL)
        driver.get(BSE_SCHEME_URL)

        ddl = wait.until(EC.presence_of_element_located((By.ID, "ddlTypeOption")))
        Select(ddl).select_by_value(REPORT_VALUE)
        log.info("[BSE] Selected %s", REPORT_VALUE)

        time.sleep(1)
        before = set(f.name for f in out.iterdir())

        btn = wait.until(EC.element_to_be_clickable((By.ID, "btnText")))
        try:
            driver.execute_script("arguments[0].click();", btn)
        except Exception:
            btn.click()
        log.info("[BSE] Download clicked")

        downloaded = _wait_for_download(out, before, DOWNLOAD_TIMEOUT)
        if not downloaded:
            return {"ok": False, "path": None, "msg": "Download timed out"}

        if pending.exists():
            pending.unlink()
        downloaded.rename(pending)
        log.info("[BSE] Saved %s (%s bytes)", pending.name, pending.stat().st_size)
        return {"ok": True, "path": str(pending), "msg": f"Downloaded {pending.name}"}

    except Exception as e:
        log.exception("[BSE] Selenium failed")
        return {"ok": False, "path": None, "msg": f"Selenium error: {e}"}
    finally:
        if driver:
            driver.quit()


def sync_now(parse_func) -> dict:
    out = get_download_dir()
    archive = out / "_archive"
    archive.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    for f in (out / _today_filename(), out / _today_done_filename()):
        if f.exists():
            try:
                shutil.move(str(f), str(archive / f"{f.stem}_{stamp}{f.suffix}"))
            except Exception as e:
                log.warning("[BSE] Could not archive %s: %s", f.name, e)

    result = _do_download()
    if not result.get("ok"):
        return {"ok": False, "msg": result.get("msg", "Download failed"),
                "path": None, "rows": None, "updated": None, "skipped": None}

    path = Path(result["path"])
    if not path.exists():
        return {"ok": False, "msg": f"File not found at {path}",
                "path": None, "rows": None, "updated": None, "skipped": None}

    try:
        with open(path, "rb") as f:
            db_ok, db_msg, preview = parse_func(f, replace=False)
    except Exception as e:
        log.exception("[BSE] Parse crashed")
        return {"ok": False, "msg": f"Parse failed: {e}",
                "path": str(path), "rows": None, "updated": None, "skipped": None}

    if not db_ok:
        return {"ok": False, "msg": f"DB import failed: {db_msg}",
                "path": str(path), "rows": None, "updated": None, "skipped": None}

    done_path = path.with_name(_today_done_filename())
    try:
        path.rename(done_path)
    except Exception:
        done_path = path

    pv = preview or {}
    return {
        "ok": True,
        "msg": f"Synced — {pv.get('rows', 0)} new, {pv.get('updated', 0)} updated, {pv.get('skipped', 0)} skipped",
        "path": str(done_path),
        "rows": pv.get("rows"),
        "updated": pv.get("updated"),
        "skipped": pv.get("skipped"),
    }