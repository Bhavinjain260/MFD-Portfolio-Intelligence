"""
nav_engine.py
=============
Single source of truth for ALL NAV-related logic. Pure Python — no Streamlit.

Covers:
  • AMFI live download + history-endpoint fetch
  • Snapshot file management (save / load / parse)
  • In-memory ISIN index (AMFINavIndex)
  • Previous-day sync + skip-back baseline resolver
  • DB queries (latest, previous, history, scheme metadata)
  • DB ingestion (parsing AMFI text → nav_schemes/nav_options/nav_history)
  • Business-day + trading-calendar helpers
  • Diagnostics

The functions here use NO Streamlit APIs — no @st.cache_data, no
st.session_state, no st.toast. Cached Streamlit wrappers live in app.py
and call these functions directly.

Replaces:
    nav_service.py
    nav_queries.py
    nav_data_ingestion.py
    trading_day_calender.py

Migration:
    app.py              : import nav_engine as nav_service
    background_worker.py: import nav_engine

    For sync_previous_business_day_nav_if_needed(), pass
    `state=st.session_state` from app.py to keep the once-per-day
    behaviour. Headless callers (background_worker, cron scripts) can
    pass state=None — the underlying sync is already idempotent via
    file-existence checks.

    Summary of everything in this final file
Section
Lines
Contents
Module docstring + imports	1–49	Pure Python, no Streamlit
Constants	52–79	URLs, dirs, thresholds, holidays, HTTP session
Trading Calendar	82–135	is_trading_day, get_last_business_day, get_expected_nav_date
Snapshot helpers	138–206	_ensure_text_dir, _snapshot_path, _count_isins_in_file, _read_snapshot_map
Parser	209–401	_parse_nav_text (column-agnostic), _extract_nav_date_from_text, _normalize_history_text_to_live_format
Snapshot save	404–448	_save_nav_snapshot (with richer-file-kept guard)
Live download	451–575	download_and_save_nav, download_and_save_nav_if_needed
Historical download	578–718	download_business_day_nav, sync_previous_business_day_nav
Sync wrapper	721–763	sync_previous_business_day_nav_if_needed (with state= param — migration fix)
Previous snapshot lookup	766–833	_previous_snapshot_path, load_previous_nav_map, get_previous_nav_date
Skip-back resolver	836–939	_resolve_prev_nav_for_folios_impl, resolve_prev_nav_for_folios
DB date lookup	942–975	get_nav_for_date_from_db
Historical orchestrator	978–1070	get_or_fetch_nav_for_date (3-source fallback)
NEW: High-level orchestrators	1073–1266	get_previous_nav_baseline + get_week_ago_nav
In-memory index	1269–1369	AMFINavIndex class, _amfi singleton, fetch_nav_by_isin, fetch_amc_by_isin, load_nav_dataframe
DB queries	1372–1685	15 query functions + get_nav_for_date_by_isin (rename fix + alias)
DB ingestion	1688–1883	ingest_nav_file_to_db, ingest_nav_bytes_to_db (new), _parse_nav_file, _insert_nav_rows
Diagnostics	1886–1927	diagnose_previous_day_coverage
"""

import logging
import os
import time as _time
from datetime import datetime, timedelta, date as date_cls, time as time_cls
from typing import Optional

import requests

import sync_failure_log as sflog
from init_db import get_conn


log = logging.getLogger("nav_engine")


# ══════════════════════════════════════════════════════════════
# CONSTANTS
# ══════════════════════════════════════════════════════════════
AMFI_TEXT_URL = "https://portal.amfiindia.com/spages/NAVAll.txt"
NAV_HISTORY_URL = "https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx"
NAV_TEXT_DIR = os.environ.get("NAV_TEXT_DIR", "nav_data")

LOOKBACK_DELAY_SECONDS = 1.5

# NAV re-publish cutoffs during the day.
# Domestic NAVs settle ~3 PM; foreign/international scheme NAVs land
# later, ~11 PM.
NAV_REDOWNLOAD_TIMES = [time_cls(15, 0), time_cls(23, 0)]

# A full AMFI NAVAll.txt has ~18,000+ data rows. AMFI's history
# endpoint (DownloadNAVHistoryReport_Po.aspx) returns only ~900-1000.
# Anything below this threshold is treated as "partial" and triggers
# fallback to other sources (history endpoint, then DB).
_MIN_ACCEPTABLE_ISIN_COUNT = 3000


_AMFI_SESSION = requests.Session()
_AMFI_SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/plain,text/html,application/xhtml+xml,*/*",
    "Referer": "https://www.amfiindia.com/",
})


# ══════════════════════════════════════════════════════════════
# TRADING CALENDAR
# ══════════════════════════════════════════════════════════════
NSE_HOLIDAYS_2026 = {
    "2026-01-26": "Republic Day",
    "2026-03-25": "Holi",
    "2026-04-02": "Good Friday",
    "2026-04-10": "Eid ul-Fitr",
    "2026-04-21": "Ram Navami",
    "2026-05-01": "May Day",
    "2026-08-15": "Independence Day",
    "2026-08-28": "Janmashtami",
    "2026-09-16": "Milad-un-Nabi",
    "2026-10-02": "Gandhi Jayanti",
    "2026-10-24": "Dussehra",
    "2026-11-08": "Diwali",
    "2026-11-09": "Diwali (Day 2)",
    "2026-12-25": "Christmas",
}


def is_trading_day(target_date: date_cls) -> bool:
    """Weekend- and holiday-aware trading day check (NSE calendar)."""
    if target_date.weekday() >= 5:
        return False
    return target_date.strftime("%Y-%m-%d") not in NSE_HOLIDAYS_2026


def get_expected_nav_date(download_date: date_cls, is_domestic: bool = True) -> date_cls:
    """
    What NAV date should a download on `download_date` contain?

    Domestic (11 PM download): yesterday's closing
    International (11 AM next-day): 2 days ago's closing
    Backtracks over non-trading days.
    """
    lag = 1 if is_domestic else 2
    nav_date = download_date - timedelta(days=lag)
    while not is_trading_day(nav_date):
        nav_date -= timedelta(days=1)
    return nav_date


def get_last_business_day(from_date: date_cls | None = None) -> date_cls:
    """
    Most recent business day BEFORE from_date.
    Weekend-only rollback — holidays are handled by AMFI returning the
    last trading day's data, and the caller checking `actual_date`.
    """
    from_date = from_date or datetime.now().date()
    candidate = from_date - timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


# ══════════════════════════════════════════════════════════════
# SNAPSHOT DIRECTORY HELPERS
# ══════════════════════════════════════════════════════════════
def _ensure_text_dir() -> None:
    os.makedirs(NAV_TEXT_DIR, exist_ok=True)


def _snapshot_path(date_str: str) -> str:
    return os.path.join(NAV_TEXT_DIR, f"nav_{date_str}.txt")


def _latest_snapshot_path() -> Optional[str]:
    _ensure_text_dir()
    available = sorted(
        f for f in os.listdir(NAV_TEXT_DIR)
        if f.startswith("nav_") and f.endswith(".txt")
    )
    return os.path.join(NAV_TEXT_DIR, available[-1]) if available else None


def get_snapshot_status() -> dict:
    _ensure_text_dir()
    today = datetime.now().strftime("%Y-%m-%d")
    today_path = _snapshot_path(today)
    latest_path = _latest_snapshot_path()
    status = {
        "has_today": os.path.exists(today_path),
        "today_path": today_path if os.path.exists(today_path) else None,
        "latest_path": latest_path,
        "latest_date": None,
        "latest_bytes": None,
    }
    if latest_path:
        fname = os.path.basename(latest_path)
        status["latest_date"] = fname.replace("nav_", "").replace(".txt", "")
        status["latest_bytes"] = os.path.getsize(latest_path)
    return status


def _count_isins_in_file(path: str) -> int:
    """Fast row-count of data lines in a snapshot file (coverage checks)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            count = 0
            for line in f:
                line = line.strip()
                if ";" in line and not line.startswith("Scheme Code"):
                    count += 1
            return count
    except Exception:
        return 0


def _read_snapshot_map(path: str) -> dict:
    """Read a snapshot file and return {isin: nav_float}."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            nav_map, _, _ = _parse_nav_text(f.read())
    except Exception:
        return {}
    out = {}
    for k, v in nav_map.items():
        try:
            nav = float(v[0]) if isinstance(v, (tuple, list)) else float(v)
            if nav > 0:
                out[str(k).strip().upper()] = nav
        except (TypeError, ValueError, IndexError):
            continue
    return out


# ══════════════════════════════════════════════════════════════
# PARSING
# ══════════════════════════════════════════════════════════════
def _extract_nav_date_from_text(text: str) -> Optional[str]:
    """
    Extract NAV date from file. Handles both 6-col and 8-col formats.
    Date is always the last column.
    """
    for line in text.splitlines():
        line = line.strip()
        if not line or ";" not in line:
            continue
        parts = line.split(";")
        if len(parts) < 6 or parts[0].strip() == "Scheme Code":
            continue
        date_field = parts[-1].strip()
        try:
            parsed_date = datetime.strptime(date_field, "%d-%b-%Y")
            return parsed_date.strftime("%Y-%m-%d")
        except ValueError:
            continue
    log.error("[NAV-DATE-EXTRACT] Could not extract NAV date from text")
    return None


def _parse_nav_text(text: str) -> tuple[dict, dict, list[dict]]:
    """
    Parses AMFI's raw text format — COLUMN-COUNT AGNOSTIC.

    Handles any of these layouts:
      6-col: Code;ISIN1;ISIN2;Name;NAV;Date
      7-col: Code;ISIN1;ISIN2;Name;Option;NAV;Date
      8-col: Code;ISIN1;ISIN2;Name;Plan;Option;NAV;Date

    Strategy: the DATE is always the LAST column. The NAV is always the
    SECOND-TO-LAST column.

    Returns:
      nav_map:  {isin: (nav, nav_date)}
      amc_map:  {isin: amc_name}
      records:  list of dicts with scheme metadata
    """
    nav_map: dict[str, tuple[float, str]] = {}
    amc_map: dict[str, str] = {}
    records: list[dict] = []
    current_amc = ""
    current_category = ""
    format_samples: dict[int, int] = {}

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if ";" not in line:
            if line.lower().endswith("mutual fund"):
                current_amc = line
            elif "(" in line and ")" in line:
                current_category = line
            continue

        parts = [p.strip() for p in line.split(";")]
        if len(parts) < 6 or parts[0] == "Scheme Code":
            continue

        scheme_code = parts[0]
        isin_1 = parts[1]
        isin_2 = parts[2]
        scheme_name = parts[3]

        nav_str = parts[-2]
        date_str = parts[-1]

        try:
            nav_date_obj = datetime.strptime(date_str, "%d-%b-%Y")
        except ValueError:
            log.debug("[NAV-PARSE] Skipping row — trailing field is not a date: %r",
                      date_str[:20])
            continue

        nav_date = nav_date_obj.strftime("%Y-%m-%d")

        try:
            nav = float(nav_str) if nav_str not in ("N.A.", "") else 0.0
        except ValueError:
            nav = 0.0
        if nav <= 0:
            continue

        format_samples[len(parts)] = format_samples.get(len(parts), 0) + 1

        isin_clean = isin_1 if isin_1 and isin_1 != "-" else None
        isin_payout_clean = isin_2 if isin_2 and isin_2 != "-" else None
        primary_isin = isin_clean or isin_payout_clean

        if primary_isin:
            records.append({
                "isin":         primary_isin.upper(),
                "scheme_code":  scheme_code,
                "isin_payout":  isin_payout_clean.upper() if isin_payout_clean else None,
                "scheme_name":  scheme_name,
                "amc_name":     current_amc or None,
                "category":     current_category or None,
                "nav":          nav,
                "nav_date":     nav_date,
            })

        for isin in (isin_1, isin_2):
            if isin and isin != "-":
                isin_u = isin.upper()
                nav_map[isin_u] = (nav, nav_date)
                if current_amc:
                    amc_map[isin_u] = current_amc

    log.info(
        "[NAV-PARSE] Parsed %d records | column counts seen: %s",
        len(records),
        {k: v for k, v in sorted(format_samples.items())},
    )
    return nav_map, amc_map, records


def _get_file_nav_date(path: str) -> Optional[str]:
    """Date is ALWAYS the last semicolon-delimited field."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if ";" not in line:
                    continue
                parts = line.split(";")
                if len(parts) < 6 or parts[0].strip() == "Scheme Code":
                    continue
                date_str = parts[-1].strip()
                try:
                    return datetime.strptime(date_str, "%d-%b-%Y").strftime("%Y-%m-%d")
                except ValueError:
                    continue
    except Exception:
        pass
    return None


def _have_snapshot_for_date(iso_date: str) -> bool:
    _ensure_text_dir()
    for fname in os.listdir(NAV_TEXT_DIR):
        if not (fname.startswith("nav_") and fname.endswith(".txt")):
            continue
        if _get_file_nav_date(os.path.join(NAV_TEXT_DIR, fname)) == iso_date:
            return True
    return False


def _looks_like_amfi_format(text: str) -> bool:
    if not text or not text.strip():
        return False
    lowered = text.lower()
    if "<html" in lowered or "captcha" in lowered:
        return False
    return "scheme code" in lowered and ";" in text


def _normalize_history_text_to_live_format(text: str) -> str:
    """
    Rewrites DownloadNAVHistoryReport_Po.aspx's 8-column rows into the
    6-column layout of the live NAVAll.txt file, so downstream readers
    stay untouched.
    """
    out_lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if ";" not in stripped:
            out_lines.append(line)
            continue
        parts = stripped.split(";")
        if len(parts) < 8 or parts[0].strip() == "Scheme Code":
            if parts and parts[0].strip() == "Scheme Code":
                out_lines.append(
                    "Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;"
                    "Scheme Name;Net Asset Value;Date"
                )
            else:
                out_lines.append(line)
            continue
        scheme_code, scheme_name, _plan, _option, isin_payout, isin_reinvest, nav, date_field = parts[:8]
        out_lines.append(
            f"{scheme_code};{isin_payout};{isin_reinvest};{scheme_name};{nav};{date_field}"
        )
    return "\n".join(out_lines)


def _fmt_amfi_date(d) -> str:
    return d.strftime("%d-%b-%Y")


# ══════════════════════════════════════════════════════════════
# SNAPSHOT SAVE
# ══════════════════════════════════════════════════════════════
def _save_nav_snapshot(text: str, iso_date: str, *,
                       allow_overwrite: bool = True,
                       source: str = "unknown") -> dict:
    """
    Save a NAV snapshot to disk.

    By default, if a snapshot for the same date already exists AND has
    MORE data rows than the incoming content, the existing (richer) file
    is KEPT rather than overwritten. This prevents the
    "partial-history clobbers full-live" bug.

    source: short tag for the log line — "live", "history", "fallback"
    """
    _ensure_text_dir()
    path = _snapshot_path(iso_date)

    new_map, _, _ = _parse_nav_text(text)
    new_count = len(new_map)

    if allow_overwrite and os.path.exists(path):
        existing_count = _count_isins_in_file(path)
        if existing_count > new_count:
            log.warning(
                "[NAV-SAVE] Refusing to overwrite richer snapshot %s "
                "(existing=%d rows, incoming=%d rows, source=%s). Keeping existing file.",
                path, existing_count, new_count, source,
            )
            return {
                "path": path,
                "bytes": os.path.getsize(path),
                "date": iso_date,
                "record_count": existing_count,
                "kept_existing": True,
            }

    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    size = os.path.getsize(path)
    log.info("[NAV-SAVE] Saved %s: %s bytes, %d rows (source=%s)",
             path, size, new_count, source)
    return {"path": path, "bytes": size, "date": iso_date,
            "record_count": new_count, "kept_existing": False}

# ══════════════════════════════════════════════════════════════
# LIVE DOWNLOAD
# ══════════════════════════════════════════════════════════════
def download_and_save_nav(timeout: int = 30) -> dict:
    """
    Downloads NAVAll.txt and saves under the ACTUAL NAV date
    (from file content). Overwrites if the same-date snapshot exists.
    """
    log.info("[AMFI] Downloading NAV file from %s", AMFI_TEXT_URL)

    try:
        res = requests.get(AMFI_TEXT_URL, timeout=timeout)
        res.raise_for_status()
    except requests.RequestException as e:
        log.error("[AMFI] Download failed: %s", e)
        return {"path": None, "bytes": 0, "date": None}

    text = res.text
    if not text or not text.strip():
        log.error("[AMFI] Downloaded file is empty")
        return {"path": None, "bytes": 0, "date": None}

    _ensure_text_dir()

    actual_nav_date = _extract_nav_date_from_text(text)
    if not actual_nav_date:
        log.error("[AMFI] Could not extract NAV date from downloaded file")
        return {"path": None, "bytes": 0, "date": None}

    path = _snapshot_path(actual_nav_date)
    file_exists = os.path.exists(path)

    try:
        saved = _save_nav_snapshot(text, actual_nav_date,
                                    allow_overwrite=True, source="live")
        path = saved["path"]
    except IOError as e:
        log.error("[AMFI] Failed to write file %s: %s", path, e)
        return {"path": None, "bytes": 0, "date": None}

    size = os.path.getsize(path)
    status = "overwritten" if file_exists else "created"
    line_count = text.count("\n") + 1
    log.info("[AMFI] NAV file %s: %s (%s bytes, %s lines) | NAV Date: %s",
             status, path, size, line_count, actual_nav_date)

    return {"path": path, "bytes": size, "date": actual_nav_date}


def _last_passed_cutoff_today(now: datetime) -> Optional[time_cls]:
    passed = [t for t in NAV_REDOWNLOAD_TIMES if now.time() >= t]
    return max(passed) if passed else None


def download_and_save_nav_if_needed(force: bool = False) -> dict:
    """
    Re-fetches if:
      - no file exists for today, OR
      - today's file was saved BEFORE the most recent cutoff that has
        already passed (stale for the current day's NAV cycle).

    On successful download, auto-ingests to DB.
    """
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    today_path = _snapshot_path(today)

    if not force and os.path.exists(today_path):
        cutoff = _last_passed_cutoff_today(now)
        if cutoff is None:
            size = os.path.getsize(today_path)
            log.info("[AMFI] Before first cutoff — keeping existing file for %s", today)
            return {"ran": False, "ok": True,
                    "reason": "before first cutoff, file is current", "bytes": size}

        file_mtime = datetime.fromtimestamp(os.path.getmtime(today_path))
        cutoff_dt = datetime.combine(now.date(), cutoff)
        if file_mtime >= cutoff_dt:
            size = os.path.getsize(today_path)
            log.info("[AMFI] File already fresh for cutoff %s (saved %s)",
                     cutoff, file_mtime.time())
            return {"ran": False, "ok": True,
                    "reason": f"already fresh past {cutoff} cutoff", "bytes": size}

        log.info("[AMFI] File saved at %s predates %s cutoff — redownloading",
                 file_mtime.time(), cutoff)

    try:
        result = download_and_save_nav()
        if result.get("date") is None:
            return {"ran": True, "ok": False,
                    "reason": "NAV date extraction failed", "bytes": None}

        if os.path.exists(result["path"]):
            log.info("[NAV-DB] File ready. Starting database ingestion for %s...",
                     result["path"])
            try:
                ingest_result = ingest_nav_file_to_db(result["path"])
                if ingest_result.get('ok'):
                    log.info("[NAV-DB] Success: %s", ingest_result.get('reason'))
                else:
                    log.error("[NAV-DB] Failed: %s", ingest_result.get('reason'))
                    sflog.record_failure(
                        source="nav", stage="db_insert",
                        file=result["path"],
                        msg=f"NAV ingestion returned failure: {ingest_result.get('reason')}",
                    )
            except Exception as e:
                log.exception("[NAV-DB] Ingestion error: %s", e)
                sflog.record_failure(
                    source="nav", stage="db_insert",
                    file=result["path"],
                    msg=f"NAV DB ingestion exception: {e}",
                )
        else:
            log.error("[NAV-DB] File path %s does not exist after download. "
                      "Skipping ingestion.", result["path"])

        return {"ran": True, "ok": True,
                "reason": "downloaded and ingested", "bytes": result.get("bytes")}

    except Exception as e:
        log.exception("[AMFI] Download failed with exception")
        return {"ran": True, "ok": False, "reason": f"download failed: {e}",
                "bytes": None}


# ══════════════════════════════════════════════════════════════
# HISTORICAL DOWNLOAD
# ══════════════════════════════════════════════════════════════
def download_business_day_nav(target_date, timeout: int = 30) -> dict:
    """
    Fetches AMFI history for one exact date and reports what date the
    response actually contains (holidays return the last working day's
    data). Raises ValueError if the response is not AMFI-shaped at all
    (blocked page).
    """
    date_str = _fmt_amfi_date(target_date)
    log.info("[AMFI-HIST] Requesting NAV history for %s", date_str)

    res = _AMFI_SESSION.get(
        NAV_HISTORY_URL,
        params={"tp": 1, "frmdt": date_str, "todt": date_str},
        timeout=timeout,
    )
    log.info("[AMFI-HIST] Request URL: %s", res.url)
    res.raise_for_status()
    text = res.text

    if not _looks_like_amfi_format(text):
        snippet = text.strip()[:200].replace("\n", " ")
        log.error(
            "[AMFI-HIST] Response for %s doesn't look like AMFI data at all "
            "(status=%s, len=%s). Snippet: %r — likely blocked/error page.",
            date_str, res.status_code, len(text), snippet
        )
        raise ValueError(f"blocked_or_invalid_response for {date_str}: {snippet!r}")

    text = _normalize_history_text_to_live_format(text)

    actual_date = _extract_nav_date_from_text(text)
    if actual_date is None:
        snippet = text.strip()[:200].replace("\n", " ")
        log.error(
            "[AMFI-HIST] %s: response has AMFI's shape but zero parseable "
            "date rows — unexpected. Snippet: %r", date_str, snippet
        )
        raise ValueError(f"no_date_in_response for {date_str}: {snippet!r}")

    log.info("[AMFI-HIST] Requested %s, file actually dated %s",
             date_str, actual_date)

    return {
        "text": text,
        "requested_date": target_date.strftime("%Y-%m-%d"),
        "actual_date": actual_date,
    }


def sync_previous_business_day_nav(force: bool = False, timeout: int = 30,
                                    max_lookback: int = 10) -> dict:
    """
    Reads the latest snapshot's NAV date, computes the prior business day,
    fetches it from AMFI's history endpoint, saves + ingests if AMFI
    confirms the date (or confirms a holiday by returning an earlier date).
    """
    latest_path = _latest_snapshot_path()
    if latest_path is None:
        return {"ran": False, "ok": False, "reason": "no live snapshot yet"}

    latest_nav_date_str = _get_file_nav_date(latest_path)
    if not latest_nav_date_str:
        return {"ran": False, "ok": False,
                "reason": "could not read NAV date from latest snapshot"}

    latest_nav_date = datetime.strptime(latest_nav_date_str, "%Y-%m-%d").date()
    target = get_last_business_day(latest_nav_date)

    for i in range(max_lookback):
        iso_date = target.strftime("%Y-%m-%d")

        if not force and _have_snapshot_for_date(iso_date):
            log.info("[AMFI-HIST] Already have %s — skipping fetch", iso_date)
            return {"ran": False, "ok": True,
                    "reason": f"already have {iso_date}", "date": iso_date}

        if i > 0:
            _time.sleep(LOOKBACK_DELAY_SECONDS)

        try:
            result = download_business_day_nav(target, timeout=timeout)
        except ValueError as e:
            log.error("[AMFI-HIST] Stopping lookback — %s", e)
            sflog.record_failure(
                source="nav", stage="fetch",
                msg=str(e),
                context={"requested_date": iso_date},
            )
            return {"ran": True, "ok": False, "reason": str(e), "date": iso_date}
        except requests.RequestException:
            log.exception("[AMFI-HIST] Network error for %s", iso_date)
            sflog.record_failure(
                source="nav", stage="fetch",
                msg="Network error",
                context={"requested_date": iso_date},
            )
            return {"ran": True, "ok": False, "reason": "network error",
                    "date": iso_date}

        actual_date = result["actual_date"]

        if actual_date == iso_date:
            saved = _save_nav_snapshot(result["text"], actual_date)
            try:
                ingest_nav_file_to_db(saved["path"])
            except Exception as e:
                log.exception("[NAV-DB] Ingestion error: %s", e)
                sflog.record_failure(
                    source="nav", stage="db_insert",
                    file=saved["path"],
                    msg=f"NAV DB ingestion failed: {e}",
                )
            return {"ran": True, "ok": True, "reason": "downloaded", **saved}

        log.info(
            "[AMFI-HIST] Requested %s but AMFI returned data dated %s "
            "instead (holiday/weekend)", iso_date, actual_date
        )
        if not force and _have_snapshot_for_date(actual_date):
            log.info("[AMFI-HIST] Already have %s — skipping save", actual_date)
            return {"ran": False, "ok": True,
                    "reason": f"already have {actual_date}", "date": actual_date}

        saved = _save_nav_snapshot(result["text"], actual_date)
        try:
            ingest_nav_file_to_db(saved["path"])
        except Exception as e:
            log.exception("[NAV-DB] Ingestion error: %s", e)
            sflog.record_failure(
                source="nav", stage="db_insert",
                file=saved["path"],
                msg=f"NAV DB ingestion failed: {e}",
            )
        return {"ran": True, "ok": True,
                "reason": f"requested {iso_date}, saved actual {actual_date}", **saved}

    return {"ran": True, "ok": False,
            "reason": f"no business day found within {max_lookback} days"}


def sync_previous_business_day_nav_if_needed(
    force: bool = False,
    state: dict | None = None,
) -> dict:
    """
    Runs at most once per calendar day IF `state` is provided.

    Parameters
    ----------
    force : bool
        Force re-fetch even if today's snapshot exists.
    state : dict-like, optional
        Pass `st.session_state` from app.py to get the once-per-day gate.
        Pass None from headless callers (background_worker, scripts) —
        the underlying sync_previous_business_day_nav() is already
        idempotent via file-existence checks, so this just skips the
        state shortcut.

    Returns
    -------
    dict  — same shape as sync_previous_business_day_nav().
    """
    today = datetime.now().strftime("%Y-%m-%d")

    if not force and state is not None and state.get("prev_nav_done_for") == today:
        return {"ran": False, "ok": True,
                "reason": "already synced successfully today"}

    result = sync_previous_business_day_nav(force=force)

    if result.get("ok"):
        if state is not None:
            try:
                state["prev_nav_done_for"] = today
            except Exception:
                pass
    else:
        log.warning(
            "[NAV-SYNC] Previous-day sync did not succeed (%s) — will retry next call",
            result.get("reason")
        )

    return result


# ══════════════════════════════════════════════════════════════
# PREVIOUS SNAPSHOT LOOKUP
# ══════════════════════════════════════════════════════════════
def _previous_snapshot_path(current_nav_date: Optional[str] = None):
    """
    Find the previous snapshot by ACTUAL NAV date inside file content,
    not by filename.
    """
    _ensure_text_dir()
    available = sorted(
        f for f in os.listdir(NAV_TEXT_DIR)
        if f.startswith("nav_") and f.endswith(".txt")
    )
    if not available:
        return None, None

    pairs: list[tuple[str, str]] = []
    for fname in available:
        path = os.path.join(NAV_TEXT_DIR, fname)
        nav_date = _get_file_nav_date(path)
        if nav_date:
            pairs.append((path, nav_date))

    if not pairs:
        return None, None

    pairs.sort(key=lambda p: p[1])

    if current_nav_date is None:
        current_nav_date = pairs[-1][1]

    for path, nav_date in reversed(pairs):
        if nav_date < current_nav_date:
            return path, nav_date

    return None, None


def load_previous_nav_map() -> dict:
    """
    Returns {isin: nav_float} for the previous snapshot, or {} if none.
    Flattened to floats so downstream arithmetic stays float64.
    """
    path, _ = _previous_snapshot_path()
    if not path:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except Exception:
        return {}
    nav_map, _, _ = _parse_nav_text(text)
    flat = {}
    for isin, val in nav_map.items():
        try:
            if isinstance(val, (tuple, list)):
                nav = float(val[0])
            else:
                nav = float(val)
            flat[isin] = nav
        except (TypeError, ValueError, IndexError):
            continue
    return flat


def get_previous_nav_date() -> Optional[str]:
    _, date_str = _previous_snapshot_path()
    return date_str


# ══════════════════════════════════════════════════════════════
# SKIP-BACK BASELINE RESOLVER
# ══════════════════════════════════════════════════════════════
def _resolve_prev_nav_for_folios_impl(
    current_date: date_cls,
    our_isins: set,
    max_lookback: int = 10,
    min_overlap_pct: float = 0.5,
) -> tuple[Optional[str], dict, dict]:
    """Pure implementation. Do not call directly from app.py — use
    resolve_prev_nav_for_folios() instead (which app.py caches)."""
    if not our_isins:
        return None, {}, {"tried": [], "chosen": None, "overlap": 0,
                          "needed": 0, "candidates": []}

    our_isins = {str(i).strip().upper() for i in our_isins if i}
    needed = max(1, int(len(our_isins) * min_overlap_pct))

    tried: list[str] = []
    candidates: list[dict] = []
    cursor = current_date

    for _ in range(max_lookback):
        cursor = get_last_business_day(cursor)
        iso = cursor.strftime("%Y-%m-%d")
        tried.append(iso)

        try:
            nav_map = get_or_fetch_nav_for_date(iso)
        except Exception:
            log.exception("[NAV-SKIPBACK] get_or_fetch failed for %s", iso)
            nav_map = {}

        overlap = len(our_isins & set(nav_map.keys()))

        candidates.append({
            "date": iso,
            "rows": len(nav_map),
            "folio_overlap": overlap,
            "needed": needed,
        })

        log.info(
            "[NAV-SKIPBACK] %s → %d rows, %d/%d folio ISINs (need %d)",
            iso, len(nav_map), overlap, len(our_isins), needed,
        )

        if overlap >= needed:
            log.info(
                "[NAV-SKIPBACK] Using %s as baseline (%d/%d folio ISINs)",
                iso, overlap, len(our_isins),
            )
            return iso, nav_map, {
                "tried": tried,
                "chosen": iso,
                "overlap": overlap,
                "needed": needed,
                "candidates": candidates,
            }

    log.warning(
        "[NAV-SKIPBACK] No date within %d business days covered ≥ %d "
        "folio ISINs. Tried: %s", max_lookback, needed, tried,
    )
    return None, {}, {
        "tried": tried,
        "chosen": None,
        "overlap": 0,
        "needed": needed,
        "candidates": candidates,
    }


def resolve_prev_nav_for_folios(
    current_date_iso,
    isins_tuple: tuple,
    max_lookback: int = 10,
    min_overlap_pct: float = 0.5,
) -> tuple:
    """
    Walk backward through prior business days until a date covers ≥
    min_overlap_pct of the given folios' ISINs.

    Returns
    -------
    (iso_date_or_None, nav_map, diagnostics)

    app.py wraps this with @st.cache_data — the pure version here has
    no caching.
    """
    if isinstance(current_date_iso, str):
        current_date = datetime.strptime(current_date_iso, "%Y-%m-%d").date()
    else:
        current_date = (
            current_date_iso.date() if hasattr(current_date_iso, "date")
            else current_date_iso
        )

    return _resolve_prev_nav_for_folios_impl(
        current_date,
        set(isins_tuple),
        max_lookback=max_lookback,
        min_overlap_pct=min_overlap_pct,
    )

# ══════════════════════════════════════════════════════════════
# DB LOOKUP FOR A SPECIFIC DATE
# ══════════════════════════════════════════════════════════════
def get_nav_for_date_from_db(target_iso: str) -> dict:
    """
    Fetch {isin: nav_value} for a specific date from the nav_history DB
    table. Returns {} if the date has no records — caller decides.
    """
    try:
        with get_conn() as conn:
            rows = conn.execute("""
                SELECT no.isin_payout, no.isin_reinvest, nh.nav_value
                FROM nav_history nh
                JOIN nav_options no ON nh.nav_option_id = no.id
                WHERE nh.nav_date = ?
            """, (target_iso,)).fetchall()
    except Exception as e:
        log.exception("[NAV-DB-LOOKUP] Query failed for %s: %s", target_iso, e)
        return {}

    out: dict[str, float] = {}
    for isin_payout, isin_reinvest, nav_val in rows:
        try:
            v = float(nav_val)
            if v <= 0:
                continue
        except (TypeError, ValueError):
            continue
        for isin in (isin_payout, isin_reinvest):
            if isin and str(isin).strip() and str(isin) != "-":
                out[str(isin).strip().upper()] = v

    log.info("[NAV-DB-LOOKUP] %s → %d ISINs from DB", target_iso, len(out))
    return out


# ══════════════════════════════════════════════════════════════
# HISTORICAL LOOKUP ORCHESTRATOR
# ══════════════════════════════════════════════════════════════
def get_or_fetch_nav_for_date(target_iso: str) -> dict:
    """
    Returns {isin: nav} for the given date, trying sources in priority:

      1. On-disk snapshot — trusted ONLY if it passes the coverage
                            threshold (_MIN_ACCEPTABLE_ISIN_COUNT).
      2. AMFI history endpoint — fetched live for that exact date.
      3. DB fallback — reads nav_history table for that date.

    Stops at the first source that meets the threshold. If none do, the
    best-coverage partial result is returned (so callers still see
    *something*), with a warning.
    """
    candidates: list[tuple[str, dict]] = []

    # ── Source 1: disk snapshot ──
    if _have_snapshot_for_date(target_iso):
        try:
            disk_map = _read_snapshot_map(_snapshot_path(target_iso))
            if disk_map:
                candidates.append(("disk", disk_map))
                if len(disk_map) >= _MIN_ACCEPTABLE_ISIN_COUNT:
                    log.info(
                        "[NAV-LOOKUP] %s → %d ISINs from disk snapshot (OK)",
                        target_iso, len(disk_map),
                    )
                    return disk_map
                log.warning(
                    "[NAV-LOOKUP] Disk snapshot for %s is partial "
                    "(%d rows < %d threshold) — trying AMFI history endpoint",
                    target_iso, len(disk_map), _MIN_ACCEPTABLE_ISIN_COUNT,
                )
        except Exception:
            log.exception("[NAV-LOOKUP] Failed to read snapshot for %s", target_iso)

    # ── Source 2: AMFI history endpoint ──
    try:
        target_d = datetime.strptime(target_iso, "%Y-%m-%d").date()
        resp = download_business_day_nav(target_d, timeout=30)
        actual = resp.get("actual_date")
        if actual and resp.get("text"):
            saved_res = _save_nav_snapshot(
                resp["text"], actual,
                allow_overwrite=True, source="history",
            )
            try:
                ingest_nav_file_to_db(saved_res["path"])
            except Exception as e:
                log.exception("[NAV-DB] Ingestion error: %s", e)

            hist_map = _read_snapshot_map(_snapshot_path(actual))
            if hist_map:
                candidates.append(("history", hist_map))
                if len(hist_map) >= _MIN_ACCEPTABLE_ISIN_COUNT:
                    log.info(
                        "[NAV-LOOKUP] %s → %d ISINs from history endpoint (OK)",
                        target_iso, len(hist_map),
                    )
                    return hist_map
                log.warning(
                    "[NAV-LOOKUP] History endpoint for %s also partial "
                    "(%d rows) — trying DB fallback",
                    target_iso, len(hist_map),
                )
    except Exception as e:
        log.warning("[NAV-LOOKUP] History fetch failed for %s: %s", target_iso, e)

    # ── Source 3: DB fallback ──
    # The DB is authoritative. If it has data for this date, use it
    # directly — no threshold comparison.
    db_map = get_nav_for_date_from_db(target_iso)
    if db_map:
        log.info(
            "[NAV-LOOKUP] %s → %d ISINs from DB (authoritative — using as-is)",
            target_iso, len(db_map),
        )
        return db_map

    # ── Nothing met threshold — return best partial ──
    if candidates:
        best_source, best_map = max(candidates, key=lambda c: len(c[1]))
        log.warning(
            "[NAV-LOOKUP] %s — no source met threshold; returning best "
            "partial (%s, %d rows)",
            target_iso, best_source, len(best_map),
        )
        return best_map

    log.error("[NAV-LOOKUP] %s — no data available from any source", target_iso)
    return {}


# ══════════════════════════════════════════════════════════════
# HIGH-LEVEL BASELINE ORCHESTRATORS
# ─────────────────────────────────────────────────────────────
# What app.py used to do inline — moved here so background_worker,
# cron scripts, and any headless caller gets the same behaviour as
# the Streamlit UI. Pure Python: no Streamlit, no caching.
#
# Boundary:
#   - These functions return (iso, nav_map, diagnostics). Pure NAV
#     answers — no pandas, no folio dataframe coupling.
#   - The @st.cache_data wrappers live in app.py.
#   - The per-row diff arithmetic ((current_nav - prev_nav) * units)
#     also stays in app.py, because it depends on the folio
#     dataframe schema built by get_enriched_folio_nav_df().
# ══════════════════════════════════════════════════════════════

def get_previous_nav_baseline(
    current_date_iso,
    our_isins: tuple,
    max_lookback: int = 10,
    min_overlap_pct: float = 0.5,
) -> tuple:
    """
    High-level "previous baseline" resolver.

    Wraps resolve_prev_nav_for_folios() with a fallback that the
    app.py block intended but never actually applied: if the skip-back
    loop exhausts max_lookback without finding a date with sufficient
    ISIN overlap, fall back to whatever the immediate previous snapshot
    on disk contains (load_previous_nav_map()).

    This fixes the dead-code bug where app.py called
    load_previous_nav_map_cached() and get_previous_nav_date() but
    threw their results away before they could serve as fallbacks.

    Parameters
    ----------
    current_date_iso : str or date
        The "today" NAV date. Accepts ISO string ('YYYY-MM-DD') or
        a date object — same flexibility as resolve_prev_nav_for_folios.
    our_isins : tuple of str
        ISINs the caller's folios hold. Tuple (not set) so the
        @st.cache_data wrapper in app.py can hash it.
    max_lookback : int
        How many business days to walk backward before giving up.
    min_overlap_pct : float
        Fraction of our_isins that must be present in a candidate
        date's NAV map for it to be accepted as the baseline.

    Returns
    -------
    (iso_or_None, nav_map, diagnostics)
        iso_or_None : str | None
            The baseline date that was used ('YYYY-MM-DD'), or None
            if both skip-back and fallback failed.
        nav_map : dict
            {isin: nav_float} for the baseline date. Empty dict if
            nothing could be resolved.
        diagnostics : dict
            Same shape as resolve_prev_nav_for_folios() returns, plus:
              - fallback_used : bool
              - fallback_date : str | None
              - first_tried_date : str | None  (the immediate previous
                business day — useful for the "snapshot was incomplete"
                warning in app.py)
    """
    iso, nav_map, diag = resolve_prev_nav_for_folios(
        current_date_iso, our_isins,
        max_lookback=max_lookback,
        min_overlap_pct=min_overlap_pct,
    )

    # Surface the immediate previous business day for UI messaging.
    # Saves app.py from having to recompute it just to print "using X
    # instead of Y because Y was incomplete".
    if isinstance(current_date_iso, str):
        _cd = datetime.strptime(current_date_iso, "%Y-%m-%d").date()
    else:
        _cd = (
            current_date_iso.date() if hasattr(current_date_iso, "date")
            else current_date_iso
        )
    _first_tried = get_last_business_day(_cd).strftime("%Y-%m-%d")
    diag["first_tried_date"] = _first_tried

    if iso and nav_map:
        diag["fallback_used"] = False
        diag["fallback_date"] = None
        return iso, nav_map, diag

    # Skip-back exhausted — fall back to the file-system previous
    # snapshot. This is the rescue path for the failure mode where
    # AMFI's history endpoint is unavailable AND no recent disk
    # snapshot covers enough of our folios.
    fallback_map = load_previous_nav_map()
    fallback_date = get_previous_nav_date()
    if fallback_map:
        diag["fallback_used"] = True
        diag["fallback_date"] = fallback_date
        log.warning(
            "[NAV-BASELINE] Skip-back found no sufficient overlap in "
            "%d business days; falling back to load_previous_nav_map() "
            "(date=%s, %d ISINs)",
            max_lookback, fallback_date, len(fallback_map),
        )
        return fallback_date, fallback_map, diag

    diag["fallback_used"] = False
    diag["fallback_date"] = None
    log.error(
        "[NAV-BASELINE] Skip-back exhausted AND no previous snapshot "
        "available. Returning empty nav_map; 1-day diff will show 0."
    )
    return None, {}, diag


def get_week_ago_nav(
    current_date_iso,
    days_back: int = 7,
    use_business_days: bool = True,
) -> tuple:
    """
    Fetch NAV for `days_back` trading (or calendar) days before
    current_date.

    Replaces the inline `timedelta(days=7)` arithmetic that lived in
    app.py's 1-Week Movement block. Moving it here means the policy
    (how to interpret "1 week ago") is owned by nav_engine, not the
    UI layer — so background_worker and any headless caller get the
    same behaviour.

    Parameters
    ----------
    current_date_iso : str or date
        The "today" NAV date. Accepts ISO string ('YYYY-MM-DD') or
        a date object.
    days_back : int
        How many days to walk back. Default 7.
    use_business_days : bool
        True (default)  — count only NSE trading days, skipping
                          weekends AND holidays. "1 week ago" becomes
                          "5 trading days ago" (previous Friday for a
                          Friday start). Consistent with the 1-day
                          block's semantics.
        False           — legacy calendar-day arithmetic. Still walks
                          back to the nearest preceding trading day
                          before fetching, so the returned map is
                          always valid even if `days_back` lands on a
                          weekend/holiday.

    Returns
    -------
    (target_iso_or_None, nav_map)
        target_iso : str | None
            The exact date the fetch was resolved for, after any
            holiday/weekend walk-back. 'YYYY-MM-DD' format.
        nav_map : dict
            {isin: nav_float} for the resolved date. Empty dict if
            get_or_fetch_nav_for_date() returned nothing.
    """
    if isinstance(current_date_iso, str):
        current_date = datetime.strptime(current_date_iso, "%Y-%m-%d").date()
    else:
        current_date = (
            current_date_iso.date() if hasattr(current_date_iso, "date")
            else current_date_iso
        )

    if use_business_days:
        # Count backward over NSE trading days only.
        cursor = current_date
        counted = 0
        while counted < days_back:
            cursor -= timedelta(days=1)
            if is_trading_day(cursor):
                counted += 1
        target = cursor
    else:
        # Legacy calendar-day arithmetic, then walk back to the
        # nearest preceding trading day so the fetch is valid.
        target = current_date - timedelta(days=days_back)
        while not is_trading_day(target):
            target -= timedelta(days=1)

    target_iso = target.strftime("%Y-%m-%d")
    log.info(
        "[NAV-WEEKAGO] current=%s, days_back=%s, mode=%s → target=%s",
        current_date.isoformat(), days_back,
        "business_days" if use_business_days else "calendar_days",
        target_iso,
    )

    nav_map = get_or_fetch_nav_for_date(target_iso)
    return target_iso, nav_map

# ══════════════════════════════════════════════════════════════
# IN-MEMORY INDEX
# ══════════════════════════════════════════════════════════════
class AMFINavIndex:
    """
    In-memory ISIN-keyed index loaded from the latest saved file.
    Refreshes at most once per TTL. Never touches the network.
    """

    _nav_by_isin: dict[str, tuple[float, str]] = {}
    _amc_by_isin: dict[str, str] = {}
    _records: list[dict] = []
    _loaded_from: Optional[str] = None
    _last_load: Optional[datetime] = None
    _ttl_seconds: int = 3600

    def _is_fresh(self) -> bool:
        if not self._nav_by_isin or self._last_load is None:
            return False
        return (datetime.now() - self._last_load).total_seconds() < self._ttl_seconds

    def load(self, force: bool = False) -> dict[str, tuple[float, str]]:
        if not force and self._is_fresh():
            log.debug("[AMFI] In-memory index fresh (loaded from %s) — reusing",
                      self._loaded_from)
            return self._nav_by_isin

        path = _latest_snapshot_path()
        if path is None:
            log.error(
                "[AMFI] No saved NAV file found in '%s' (abs: %s). "
                "Run download_and_save_nav_if_needed() first, or check NAV_TEXT_DIR.",
                NAV_TEXT_DIR, os.path.abspath(NAV_TEXT_DIR),
            )
            return {}

        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except Exception:
            log.exception("[AMFI] Failed to read saved NAV file '%s'", path)
            return {}

        if not text.strip():
            log.error("[AMFI] Saved NAV file '%s' is empty", path)
            return {}

        nav_map, amc_map, records = _parse_nav_text(text)
        self._nav_by_isin = nav_map
        self._amc_by_isin = amc_map
        self._records = records
        self._loaded_from = path
        self._last_load = datetime.now()

        log.info("[AMFI] Loaded index from '%s': %s ISINs (NAV), %s ISINs (AMC), "
                 "%s records", path, len(nav_map), len(amc_map), len(records))
        return nav_map

    def get_nav(self, isin: str) -> Optional[tuple[float, str]]:
        if not self._nav_by_isin:
            self.load()
        if not isin:
            return None
        return self._nav_by_isin.get(isin.strip().upper())

    def get_amc(self, isin: str) -> str:
        if not self._amc_by_isin:
            self.load()
        if not isin:
            return ""
        return self._amc_by_isin.get(isin.strip().upper(), "")

    def get_records(self) -> list[dict]:
        if not self._records:
            self.load()
        return self._records


_amfi = AMFINavIndex()


def fetch_nav_by_isin(isin: str) -> Optional[tuple[float, str]]:
    isin = isin.strip().upper()
    return _amfi.get_nav(isin)


def fetch_amc_by_isin(isin: str) -> str:
    return _amfi.get_amc(isin)


def load_nav_dataframe():
    """Returns the AMFI NAV data as a DataFrame. Imports pandas locally
    so this module stays dependency-free for non-DataFrame use cases."""
    import pandas as pd
    _amfi.load()
    records = _amfi.get_records()
    return pd.DataFrame(records, columns=[
        "isin", "scheme_code", "isin_payout",
        "scheme_name", "amc_name", "category",
        "nav", "nav_date",
    ])


# ══════════════════════════════════════════════════════════════
# DB QUERIES (from nav_queries.py)
# ══════════════════════════════════════════════════════════════
def get_latest_nav_by_isin(isin: str) -> Optional[tuple[float, str]]:
    """Latest NAV + date for an ISIN, from DB. Returns (nav, date_str) or None."""
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


def get_latest_nav_by_scheme_and_plan(scheme_code: int,
                                       plan: str = "Regular Plan"
                                       ) -> Optional[tuple[float, str]]:
    """Latest NAV for scheme+plan combo, from DB."""
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


def get_all_latest_navs() -> dict[str, tuple[float, str]]:
    """All latest NAVs by ISIN (latest date across all schemes)."""
    navs = {}
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT DISTINCT
                COALESCE(no.isin_payout, no.isin_reinvest) as isin,
                nh.nav_value,
                nh.nav_date
            FROM nav_history nh
            JOIN nav_options no ON nh.nav_option_id = no.id
            WHERE nh.nav_date = (SELECT MAX(nav_date) FROM nav_history)
        """).fetchall()
    for row in rows:
        if row[0]:
            navs[row[0]] = (float(row[1]), str(row[2]))
    return navs


def get_nav_for_date_by_isin(isin: str, nav_date: str) -> Optional[float]:
    """
    Specific ISIN on specific date, from DB. Returns nav_value or None.

    Renamed from `get_nav_for_date` to avoid confusion with
    `get_or_fetch_nav_for_date` (which returns a dict, not a float).
    The old name is preserved as an alias below for backwards compat.
    """
    if "-" in nav_date and len(nav_date) == 10:
        date_str = nav_date
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


# Backwards-compat alias — old nav_queries.py exposed this as get_nav_for_date.
# Deprecated; new code should use get_nav_for_date_by_isin instead.
get_nav_for_date = get_nav_for_date_by_isin


def get_previous_nav_date_db() -> Optional[str]:
    """
    Previous DB date (penultimate distinct nav_date).
    Renamed from nav_queries.get_previous_nav_date() to avoid conflict
    with the file-based get_previous_nav_date() above.
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


def get_previous_nav_by_isin(isin: str) -> Optional[tuple[float, str]]:
    """Penultimate NAV for an ISIN, from DB."""
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


def get_previous_navs_map() -> dict[str, tuple[float, str]]:
    """All penultimate NAVs by ISIN, from DB."""
    navs = {}
    with get_conn() as conn:
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


def get_nav_history(isin: str, days: int = 30) -> list[dict]:
    """NAV history (last N days) for an ISIN, from DB, with daily change."""
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
            "nav_date": str(nav_date),
            "nav_value": float(nav_value),
            "prev_nav": float(prev_nav) if prev_nav else None,
            "daily_change": daily_change,
            "daily_pct": daily_pct,
        })
    return list(reversed(history))


def get_scheme_info(scheme_code: int) -> Optional[dict]:
    """Scheme metadata (name, fund_house, isin_growth) from DB."""
    with get_conn() as conn:
        row = conn.execute("""
            SELECT scheme_code, scheme_name, fund_house, isin_growth
            FROM nav_schemes
            WHERE scheme_code = ?
        """, (scheme_code,)).fetchone()
    if row:
        return {
            "scheme_code": row[0],
            "scheme_name": row[1],
            "fund_house": row[2],
            "isin_growth": row[3],
        }
    return None


def get_scheme_options(scheme_code: int, plan: str = None) -> list[dict]:
    """All option variants (plan+option) for a scheme, from DB."""
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

    return [{
        "id": row[0],
        "plan": row[1],
        "option_name": row[2],
        "isin_payout": row[3],
        "isin_reinvest": row[4],
    } for row in rows]


def get_latest_nav_date() -> Optional[str]:
    """Latest NAV date across all schemes in DB."""
    with get_conn() as conn:
        row = conn.execute("SELECT MAX(nav_date) FROM nav_history").fetchone()
    return str(row[0]) if row and row[0] else None


def get_nav_count_by_date(nav_date: str) -> int:
    """Count of NAV records for a date."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM nav_history WHERE nav_date = ?",
            (nav_date,)
        ).fetchone()
    return row[0] if row else 0


def get_fund_house_summary() -> list[dict]:
    """Schemes/options/records per fund house."""
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
    return [{
        "fund_house": row[0],
        "schemes": row[1],
        "options": row[2],
        "records": row[3],
    } for row in rows]


def get_regular_plan_navs() -> dict[str, tuple[float, str]]:
    """All latest NAVs restricted to Regular Plan."""
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
                AND nh.nav_date = (SELECT MAX(nav_date) FROM nav_history)
        """).fetchall()
    for row in rows:
        if row[0]:
            navs[row[0]] = (float(row[1]), str(row[2]))
    return navs


def get_regular_plan_history(isin: str, days: int = 30) -> list[dict]:
    """NAV history for Regular Plan schemes only."""
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
            "nav_date": str(nav_date),
            "nav_value": float(nav_value),
            "daily_change": daily_change,
        })
    return list(reversed(history))

# ══════════════════════════════════════════════════════════════
# DB INGESTION (from nav_data_ingestion.py)
# ══════════════════════════════════════════════════════════════
def ingest_nav_file_to_db(filepath: str) -> dict:
    """
    Parse an AMFI NAV text file and insert to the normalized DB schema.
    Filters to Regular Plan only (business requirement).
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
            'reason': f"Inserted {result['inserted']} NAV records",
        }
    except Exception as e:
        log.exception("[NAV-DB] Ingestion failed")
        return {
            'ok': False,
            'inserted': 0,
            'skipped': 0,
            'nav_date': None,
            'fund_houses': set(),
            'reason': f"Ingestion failed: {e}",
        }


def ingest_nav_bytes_to_db(data: bytes) -> dict:
    """
    Convenience wrapper: save raw bytes to a temp file, then call
    ingest_nav_file_to_db(). Useful for Streamlit UploadedFile objects
    (call .getvalue() to get bytes, then pass here) and any other
    in-memory NAV content that doesn't have a filesystem path.

    Usage in data_manager.py:
        from nav_engine import ingest_nav_bytes_to_db
        result = ingest_nav_bytes_to_db(file.getvalue())

    Returns: same dict shape as ingest_nav_file_to_db().
    """
    import tempfile
    with tempfile.NamedTemporaryFile(delete=False, suffix=".txt", mode='wb') as tmp:
        tmp.write(data)
        tmp_path = tmp.name
    try:
        return ingest_nav_file_to_db(tmp_path)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def _parse_nav_file(filepath: str) -> list[dict]:
    """
    Parse AMFI text file for DB insertion.
    Reads the AMC name from the file structure, skips hardcoded keyword
    matching. Handles BOTH 8-column (Plan/Option present) and 6-column
    (historical/fallback) formats.

    Filters to "Regular Plan" rows only for 8-column format.
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
                        if plan.lower() != "regular plan":
                            continue
                    else:
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
                        'nav_date': nav_date,
                    })
                except (ValueError, IndexError):
                    continue

    except IOError as e:
        log.error("[NAV-DB] Cannot read file %s: %s", filepath, e)
        raise

    return rows


def _insert_nav_rows(rows: list[dict]) -> dict:
    """Insert parsed NAV rows into the 3-table normalized schema."""
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
                """, (row['scheme_code'], row['scheme_name'], row['fund_house']))

                c.execute("""
                    INSERT OR IGNORE INTO nav_options
                    (scheme_code, plan, option_name, isin_payout, isin_reinvest)
                    VALUES (?, ?, ?, ?, ?)
                """, (row['scheme_code'], row['plan'], row['option'],
                      row['isin_payout'], row['isin_reinvest']))

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
                log.warning("[NAV-DB] Row skipped (code %s): %s",
                            row.get('scheme_code'), e)
                skipped += 1
                continue

        conn.commit()

    return {
        'inserted': inserted,
        'skipped': skipped,
        'nav_date': nav_date,
        'fund_houses': fund_houses,
    }


# ══════════════════════════════════════════════════════════════
# DIAGNOSTICS
# ══════════════════════════════════════════════════════════════
def diagnose_previous_day_coverage(current_iso: str, previous_iso: str) -> dict:
    """
    Compares today's and yesterday's snapshots for row counts and ISIN
    overlap. Renders as a table in the debug toggle / Admin Panel.
    """
    today_path = _snapshot_path(current_iso)
    prev_path = _snapshot_path(previous_iso)

    today_exists = os.path.exists(today_path)
    prev_exists = os.path.exists(prev_path)

    today_count = _count_isins_in_file(today_path) if today_exists else 0
    prev_count = _count_isins_in_file(prev_path) if prev_exists else 0

    today_map = _read_snapshot_map(today_path) if today_exists else {}
    prev_map = _read_snapshot_map(prev_path) if prev_exists else {}

    today_isins = set(today_map.keys())
    prev_isins = set(prev_map.keys())
    overlap = today_isins & prev_isins
    missing = today_isins - prev_isins

    return {
        "current_date": current_iso,
        "previous_date": previous_iso,
        "today_file_exists": today_exists,
        "prev_file_exists": prev_exists,
        "today_count": today_count,
        "prev_count": prev_count,
        "coverage_threshold": _MIN_ACCEPTABLE_ISIN_COUNT,
        "prev_is_partial": prev_count > 0 and prev_count < _MIN_ACCEPTABLE_ISIN_COUNT,
        "today_isins": len(today_isins),
        "prev_isins": len(prev_isins),
        "overlap": len(overlap),
        "missing_from_prev": len(missing),
        "missing_isins_sample": sorted(missing)[:10],
        "has_db_fallback": bool(get_nav_for_date_from_db(previous_iso)),
    }