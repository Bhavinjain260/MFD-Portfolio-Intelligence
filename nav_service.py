"""
nav_service.py
==============
All AMFI NAV fetching, parsing, snapshot-file management, and
previous-day NAV sync live here.

This file is the single source of truth for:
  • Downloading the live NAVAll.txt
  • Fetching historical business-day NAVs from AMFI's history endpoint
  • Parsing both 6-col and 8-col formats into an ISIN-keyed index
  • Keeping the disk snapshot directory coherent
  • Exposing a request-scoped in-memory index (AMFINavIndex)
  • Wrapping Streamlit session state for the "already synced today" gate

DB ingestion is done via nav_data_ingestion.ingest_nav_file_to_db — this
module calls it, but the actual DB write lives there.
"""

import logging
import os
import time as _time
from datetime import datetime, timedelta, date as date_cls, time as time_cls
from typing import Optional

import pandas as pd
import requests
import streamlit as st

import nav_data_ingestion
import sync_failure_log as sflog

log = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════
# CONSTANTS
# ══════════════════════════════════════════════════════════════
AMFI_TEXT_URL = "https://portal.amfiindia.com/spages/NAVAll.txt"
NAV_HISTORY_URL = "https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx"
NAV_TEXT_DIR = os.environ.get("NAV_TEXT_DIR", "nav_data")

LOOKBACK_DELAY_SECONDS = 1.5

# NAV re-publish cutoffs during the day. Domestic NAVs settle ~3 PM,
# foreign/international scheme NAVs land later, ~11 PM.
NAV_REDOWNLOAD_TIMES = [time_cls(15, 0), time_cls(23, 0)]

_AMFI_SESSION = requests.Session()
_AMFI_SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/plain,text/html,application/xhtml+xml,*/*",
    "Referer": "https://www.amfiindia.com/",
})


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


# ══════════════════════════════════════════════════════════════
# PARSER (shared by live + history files)
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
    Parses AMFI's raw text format. Returns:
      nav_map:  {isin: (nav, nav_date)}
      amc_map:  {isin: amc_name}
      records:  list of dicts
    """
    nav_map: dict[str, tuple[float, str]] = {}
    amc_map: dict[str, str] = {}
    records: list[dict] = []
    current_amc = ""
    current_category = ""
    format_detected = None

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

        parts = line.split(";")
        if len(parts) < 6 or parts[0] == "Scheme Code":
            continue

        scheme_code = parts[0].strip()
        isin_1 = parts[1].strip()
        isin_2 = parts[2].strip()
        scheme_name = parts[3].strip()

        if len(parts) >= 8:
            nav_str = parts[6].strip()
            date_str = parts[7].strip()
            if format_detected is None:
                format_detected = "8-col"
        else:
            nav_str = parts[4].strip()
            date_str = parts[5].strip()
            if format_detected is None:
                format_detected = "6-col"

        try:
            nav = float(nav_str) if nav_str not in ("N.A.", "") else 0.0
        except ValueError:
            nav = 0.0

        try:
            nav_date = datetime.strptime(date_str, "%d-%b-%Y").strftime("%Y-%m-%d")
        except ValueError:
            nav_date = date_str

        if nav <= 0:
            continue

        isin_clean = isin_1 if isin_1 and isin_1 != "-" else None
        isin_payout_clean = isin_2 if isin_2 and isin_2 != "-" else None
        primary_isin = isin_clean or isin_payout_clean

        if primary_isin:
            records.append({
                "isin": primary_isin.upper(),
                "scheme_code": scheme_code,
                "isin_payout": isin_payout_clean.upper() if isin_payout_clean else None,
                "scheme_name": scheme_name,
                "amc_name": current_amc or None,
                "category": current_category or None,
                "nav": nav,
                "nav_date": nav_date,
            })

        for isin in (isin_1, isin_2):
            if isin and isin != "-":
                isin_u = isin.upper()
                nav_map[isin_u] = (nav, nav_date)
                if current_amc:
                    amc_map[isin_u] = current_amc

    log.info("[NAV-PARSE] Parsed %d records using %s format",
             len(records), format_detected or "unknown")
    return nav_map, amc_map, records


# ══════════════════════════════════════════════════════════════
# LIVE NAV DOWNLOAD
# ══════════════════════════════════════════════════════════════
def download_and_save_nav(timeout: int = 30) -> dict:
    """
    Downloads NAVAll.txt and saves under the ACTUAL NAV date (from file content).
    Overwrites if the same-date snapshot already exists.
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
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
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
    Otherwise skips.
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
            log.info("[NAV-DB] File ready. Starting database ingestion for %s...", result["path"])
            try:
                ingest_result = nav_data_ingestion.ingest_nav_file_to_db(result["path"])
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
            log.error("[NAV-DB] File path %s does not exist after download. Skipping ingestion.",
                      result["path"])

        return {"ran": True, "ok": True,
                "reason": "downloaded and ingested", "bytes": result.get("bytes")}

    except Exception as e:
        log.exception("[AMFI] Download failed with exception")
        return {"ran": True, "ok": False, "reason": f"download failed: {e}", "bytes": None}


# ══════════════════════════════════════════════════════════════
# BUSINESS DAY + HISTORICAL FETCH
# ══════════════════════════════════════════════════════════════
def get_last_business_day(from_date: date_cls | None = None) -> date_cls:
    """Most recent business day BEFORE from_date. Weekend-only rollback."""
    from_date = from_date or datetime.now().date()
    candidate = from_date - timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def _fmt_amfi_date(d) -> str:
    return d.strftime("%d-%b-%Y")


def _get_file_nav_date(path: str) -> Optional[str]:
    """Actual NAV date embedded in a saved file's data (not filename)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if ";" not in line:
                    continue
                parts = line.split(";")
                if len(parts) < 6 or parts[0] == "Scheme Code":
                    continue
                date_str = parts[7].strip() if len(parts) >= 8 else parts[5].strip()
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
    Rewrites DownloadNAVHistoryReport_Po.aspx's 8-column rows into the same
    6-column layout as the live NAVAll.txt file, so every downstream reader
    stays untouched.
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


def download_business_day_nav(target_date, timeout: int = 30) -> dict:
    """
    Fetches AMFI history for one exact date and reports what date the
    response actually contains (holidays return the last working day's data).
    Raises ValueError if the response is not AMFI-shaped at all (blocked page).
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

    log.info("[AMFI-HIST] Requested %s, file actually dated %s", date_str, actual_date)

    return {
        "text": text,
        "requested_date": target_date.strftime("%Y-%m-%d"),
        "actual_date": actual_date,
    }


def _save_nav_snapshot(text: str, iso_date: str) -> dict:
    _ensure_text_dir()
    path = _snapshot_path(iso_date)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    nav_map, _, _ = _parse_nav_text(text)
    size = os.path.getsize(path)
    log.info("[AMFI-HIST] Saved %s: %s bytes, %s ISINs", path, size, len(nav_map))
    return {"path": path, "bytes": size, "date": iso_date,
            "record_count": len(nav_map)}


def sync_previous_business_day_nav(force: bool = False, timeout: int = 30,
                                    max_lookback: int = 10) -> dict:
    """
    Reads the latest snapshot's NAV date, computes the prior business day,
    fetches it from AMFI's history endpoint, and saves it if AMFI confirms
    the date (or confirms it's a holiday by returning an earlier date).
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
            return {"ran": True, "ok": False, "reason": "network error", "date": iso_date}

        actual_date = result["actual_date"]

        if actual_date == iso_date:
            saved = _save_nav_snapshot(result["text"], actual_date)
            try:
                nav_data_ingestion.ingest_nav_file_to_db(saved["path"])
            except Exception as e:
                log.exception("[NAV-DB] Ingestion error: %s", e)
                sflog.record_failure(
                    source="nav", stage="db_insert",
                    file=saved["path"],
                    msg=f"NAV DB ingestion failed: {e}",
                )
            return {"ran": True, "ok": True, "reason": "downloaded", **saved}

        log.info(
            "[AMFI-HIST] Requested %s but AMFI returned data dated %s instead (holiday/weekend)",
            iso_date, actual_date
        )
        if not force and _have_snapshot_for_date(actual_date):
            log.info("[AMFI-HIST] Already have %s — skipping save", actual_date)
            return {"ran": False, "ok": True,
                    "reason": f"already have {actual_date}", "date": actual_date}

        saved = _save_nav_snapshot(result["text"], actual_date)
        try:
            nav_data_ingestion.ingest_nav_file_to_db(saved["path"])
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


def sync_previous_business_day_nav_if_needed(force: bool = False) -> dict:
    """
    Runs at most once per calendar day — but only sets the "done" flag when
    the previous attempt actually succeeded, so failed attempts retry on the
    next rerun.
    """
    key = "prev_nav_done_for"
    today = datetime.now().strftime("%Y-%m-%d")

    if not force and st.session_state.get(key) == today:
        return {"ran": False, "ok": True, "reason": "already synced successfully today"}

    result = sync_previous_business_day_nav(force=force)

    if result.get("ok"):
        st.session_state[key] = today
    else:
        log.warning(
            "[AMFI-HIST] Previous-day sync did not succeed (%s) — will retry on next rerun",
            result.get("reason")
        )

    return result


# ══════════════════════════════════════════════════════════════
# PREVIOUS SNAPSHOT LOOKUP
# ══════════════════════════════════════════════════════════════
def _previous_snapshot_path(current_nav_date: Optional[str] = None):
    _ensure_text_dir()
    available = sorted(
        f for f in os.listdir(NAV_TEXT_DIR)
        if f.startswith("nav_") and f.endswith(".txt")
    )
    if not available:
        return None, None

    if current_nav_date is None:
        latest_path = os.path.join(NAV_TEXT_DIR, available[-1])
        current_nav_date = _get_file_nav_date(latest_path)
    if current_nav_date is None:
        return None, None

    for fname in reversed(available):
        path = os.path.join(NAV_TEXT_DIR, fname)
        file_nav_date = _get_file_nav_date(path)
        if file_nav_date and file_nav_date < current_nav_date:
            return path, file_nav_date
    return None, None


@st.cache_data(ttl=86400, show_spinner=False)
def load_previous_nav_map() -> dict:
    path, _ = _previous_snapshot_path()
    if not path:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except Exception:
        return {}
    nav_map, _, _ = _parse_nav_text(text)
    # _parse_nav_text returns {isin: (nav, nav_date)}. Flatten to {isin: nav_float}
    # so callers can do arithmetic directly — returning the tuple forces any
    # downstream column into object dtype and breaks .nlargest()/.nsmallest().
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
# HISTORICAL NAV LOOKUP (used by Valuation Report "as-of" date)
# ══════════════════════════════════════════════════════════════
def get_or_fetch_nav_for_date(target_iso: str) -> dict:
    """
    Returns {isin: nav} for the given date. Uses a local snapshot if one
    exists, otherwise fetches from AMFI's history endpoint.
    """
    if _have_snapshot_for_date(target_iso):
        path = _snapshot_path(target_iso)
        with open(path, 'r', encoding='utf-8') as f:
            nav_map, _, _ = _parse_nav_text(f.read())
        out = {}
        for k, v in nav_map.items():
            try:
                nav = float(v[0]) if isinstance(v, (tuple, list)) else float(v)
                if nav > 0:
                    out[k] = nav
            except (TypeError, ValueError, IndexError):
                continue
        return out

    try:
        target_d = datetime.strptime(target_iso, "%Y-%m-%d").date()
        resp = download_business_day_nav(target_d, timeout=30)
        actual = resp.get("actual_date")
        if actual and resp.get("text"):
            saved_res = _save_nav_snapshot(resp["text"], actual)
            try:
                nav_data_ingestion.ingest_nav_file_to_db(saved_res["path"])
            except Exception as e:
                log.exception("[NAV-DB] Ingestion error: %s", e)
            with open(_snapshot_path(actual), 'r', encoding='utf-8') as f:
                nav_map, _, _ = _parse_nav_text(f.read())
            return {k: v[0] for k, v in nav_map.items() if v[0] > 0}
    except Exception as e:
        log.warning("[VALUATION] NAV fetch failed for %s: %s", target_iso, e)

    return {}


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
            log.error("[AMFI] No saved NAV file found in '%s' (abs: %s). "
                      "Run download_and_save_nav_if_needed() first, or check NAV_TEXT_DIR.",
                      NAV_TEXT_DIR, os.path.abspath(NAV_TEXT_DIR))
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

        log.info("[AMFI] Loaded index from '%s': %s ISINs (NAV), %s ISINs (AMC), %s records",
                 path, len(nav_map), len(amc_map), len(records))
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


# Singleton instance
_amfi = AMFINavIndex()


# ══════════════════════════════════════════════════════════════
# PUBLIC LOOKUPS
# ══════════════════════════════════════════════════════════════
def fetch_nav_by_isin(isin: str) -> Optional[tuple[float, str]]:
    isin = isin.strip().upper()
    return _amfi.get_nav(isin)


def fetch_amc_by_isin(isin: str) -> str:
    return _amfi.get_amc(isin)


def load_nav_dataframe() -> pd.DataFrame:
    _amfi.load()
    records = _amfi.get_records()
    return pd.DataFrame(records, columns=["isin", "scheme_code", "isin_payout",
                                          "scheme_name", "amc_name", "category",
                                          "nav", "nav_date"])