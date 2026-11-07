"""
nav_engine.py
=============
Single source of truth for ALL NAV-related logic. Pure Python — no Streamlit.

Covers:
  • AMFI live download + history-endpoint fetch (retrying session)
  • Snapshot file management (atomic save / indexed lookup / parse)
  • In-memory ISIN index (AMFINavIndex, thread-safe, auto-reload on change)
  • Previous-day sync + skip-back baseline resolver (local-first)
  • DB queries (latest, previous, history, scheme metadata)
  • DB ingestion (batched, single transaction)
  • Business-day + trading-calendar helpers (holiday-aware, JSON-extendable,
    self-learning from AMFI responses)
  • Diagnostics

No Streamlit APIs. Cached wrappers live in app.py.

Time zone
---------
All "now" / "today" logic runs in IST (Asia/Kolkata), independent of the
server's local zone.

Holidays
--------
Built-in: NSE_HOLIDAYS_2026 (verified against the NSE list). To add years,
create `nse_holidays.json` (path via env NSE_HOLIDAYS_FILE):
    {"2027": {"2027-01-26": "Republic Day", "...": "..."}}
Years with no data are treated as weekend-only (a warning is logged once).
Dates AMFI proves to be non-trading (it answers a history request with the
previous trading day's data) are learned automatically and stored under the
"learned" key of the same file. Learned dates never count as curated year data.

Ingestion scope
---------------
By default only "Regular Plan" rows go into the DB. Set env
NAV_INGEST_PLANS="Regular Plan,Direct Plan" (or "all") to ingest more.
Disk snapshots always hold every plan.

Changelog (this revision)
-------------------------
  • IST-aware clock everywhere; cutoffs compared in IST.
  • NSE 2026 holiday list corrected; learned-holiday mechanism added.
  • _network_nav_map never relabels data: if AMFI returns a different date
    than requested, that data is saved under its real date and the requested
    date gets only local data.
  • get_week_ago_nav counts 5 trading days by default (calendar mode: 7).
  • nav_history writes use ON CONFLICT DO UPDATE; duplicates are removed
    before the UNIQUE index is created.
  • Thread-safe module caches; AMFINavIndex swaps state atomically and
    rate-limits its error logs.
  • Live-file validation: >= max(_MIN_ACCEPTABLE_ISIN_COUNT, 90% of the
    latest snapshot's rows).
  • Keeping a thicker existing snapshot refreshes its mtime (no 10-minute
    re-download loop).
  • Baseline fallback is relative to the requested date.
  • ISIN lookups use two indexed lookups (CTE + UNION) instead of OR.
  • download_and_save_nav_if_needed returns `stale` when AMFI has not yet
    published the expected NAV date.
  • Removed: get_expected_nav_date, dead 8-col plan branch,
    sync_previous_business_day_nav(max_lookback), AMFINavIndex(ttl_seconds),
    _count_isins_in_file / _get_file_nav_date aliases.

Migration notes
---------------
  • Public API otherwise unchanged. NAV values returned from DB / index are
    `NavPoint` NamedTuples (nav, date) — still index-able like old tuples.
  • `get_nav_for_date` kept as a deprecated alias.
  • Run `cleanup_unknown_plan_rows()` once (dry-run first) to purge rows
    ingested under plan='Unknown' by the old Regular-Plan-filter bug, then
    re-ingest.
"""

import json
import logging
import os
import re
import threading
import time as _time
import warnings
from collections import OrderedDict
from datetime import (datetime, timedelta, timezone,
                      date as date_cls, time as time_cls)
from typing import NamedTuple, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import sync_failure_log as sflog
from init_db import get_conn


import pandas as pd      # used by folio+NAV batch loaders + load_nav_dataframe
import bse_scheme         # used by get_all_folios_with_isin_and_nav for the dedup subquery

try:
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
except Exception:  # tzdata missing (e.g. some Windows installs)
    IST = timezone(timedelta(hours=5, minutes=30), "IST")


log = logging.getLogger("nav_engine")


def _now() -> datetime:
    """Current time in IST (timezone-aware)."""
    return datetime.now(IST)


def _as_date(d) -> date_cls:
    """Accept ISO string, datetime or date."""
    if isinstance(d, str):
        return datetime.strptime(d, "%Y-%m-%d").date()
    if isinstance(d, datetime):
        return d.date()
    return d


# ── throttled logging (avoid per-call error floods) ───────────
_LOG_THROTTLE: dict[str, float] = {}
_LOG_THROTTLE_LOCK = threading.Lock()


def _log_throttled(key: str, level: int, msg: str, *args, interval: float = 300.0) -> None:
    now = _time.monotonic()
    with _LOG_THROTTLE_LOCK:
        last = _LOG_THROTTLE.get(key)
        if last is not None and now - last < interval:
            return
        _LOG_THROTTLE[key] = now
    log.log(level, msg, *args)


# ══════════════════════════════════════════════════════════════
# TYPES
# ══════════════════════════════════════════════════════════════
class NavPoint(NamedTuple):
    nav: float
    date: str          # ISO YYYY-MM-DD


# ══════════════════════════════════════════════════════════════
# CONSTANTS
# ══════════════════════════════════════════════════════════════
AMFI_TEXT_URL = "https://portal.amfiindia.com/spages/NAVAll.txt"
NAV_HISTORY_URL = "https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx"
NAV_TEXT_DIR = os.environ.get("NAV_TEXT_DIR", "nav_data")
NSE_HOLIDAYS_FILE = os.environ.get("NSE_HOLIDAYS_FILE", "nse_holidays.json")

# Re-publish cutoffs during the day (IST). Domestic NAVs settle ~3 PM;
# foreign/international scheme NAVs land ~11 PM.
NAV_REDOWNLOAD_TIMES = [time_cls(15, 0), time_cls(23, 0)]

# Minimum wait between download attempts when the snapshot is still stale
# (e.g. AMFI hasn't published yet). Prevents hammering on every rerun.
RETRY_BACKOFF_SECONDS = 600

# A full NAVAll.txt has ~18,000+ data rows. The history endpoint returns
# only ~900-1000. Anything below this is "partial".
_MIN_ACCEPTABLE_ISIN_COUNT = 10000
_LIVE_MIN_FRACTION_OF_PREV = 0.9

_SIG_CHECK_INTERVAL = 30  # seconds between AMFINavIndex file-change checks

# Which plans go into the DB. "all" disables the filter.
_INGEST_PLANS_RAW = os.environ.get("NAV_INGEST_PLANS", "Regular Plan")
_INGEST_ALL = _INGEST_PLANS_RAW.strip().lower() == "all"
_INGEST_PLANS = {p.strip().lower() for p in _INGEST_PLANS_RAW.split(",") if p.strip()}


def _build_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/plain,text/html,application/xhtml+xml,*/*",
        "Referer": "https://www.amfiindia.com/",
    })
    retry = Retry(
        total=3, connect=3, read=3,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_maxsize=4)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


_AMFI_SESSION = _build_session()


def _decode_response(res: requests.Response) -> str:
    """Decode as UTF-8 (strip BOM) regardless of what headers claim."""
    return res.content.decode("utf-8-sig", errors="replace")


# ══════════════════════════════════════════════════════════════
# TRADING CALENDAR
# ══════════════════════════════════════════════════════════════
# NSE equity segment weekday holidays, 2026 (weekend holidays omitted —
# they are non-trading anyway; Muhurat session 2026-11-08 is a Sunday).
NSE_HOLIDAYS_2026 = {
    "2026-01-15": "Municipal Corporation Election - Maharashtra",
    "2026-01-26": "Republic Day",
    "2026-03-03": "Holi",
    "2026-03-26": "Shri Ram Navami",
    "2026-03-31": "Shri Mahavir Jayanti",
    "2026-04-03": "Good Friday",
    "2026-04-14": "Dr. Baba Saheb Ambedkar Jayanti",
    "2026-05-01": "Maharashtra Day",
    "2026-05-28": "Bakri Id",
    "2026-06-26": "Muharram",
    "2026-09-14": "Ganesh Chaturthi",
    "2026-10-02": "Mahatma Gandhi Jayanti",
    "2026-10-20": "Dussehra",
    "2026-11-10": "Diwali - Balipratipada",
    "2026-11-24": "Prakash Gurpurb Sri Guru Nanak Dev",
    "2026-12-25": "Christmas",
}

_HOLIDAY_CACHE: dict = {"sig": "unset", "data": None}
_HOLIDAY_WARNED_YEARS: set = set()
_HOLIDAY_LOCK = threading.RLock()


def _load_holidays() -> tuple[dict[str, dict[str, str]], dict[str, str]]:
    """
    (curated, learned):
      curated: {year_str: {iso_date: name}} — built-in merged with JSON file
      learned: {iso_date: note} — dates AMFI showed to be non-trading
    """
    with _HOLIDAY_LOCK:
        try:
            sig = os.stat(NSE_HOLIDAYS_FILE).st_mtime_ns
        except OSError:
            sig = None
        if _HOLIDAY_CACHE["sig"] == sig and _HOLIDAY_CACHE["data"] is not None:
            return _HOLIDAY_CACHE["data"]

        curated: dict[str, dict[str, str]] = {"2026": dict(NSE_HOLIDAYS_2026)}
        learned: dict[str, str] = {}
        if sig is not None:
            try:
                with open(NSE_HOLIDAYS_FILE, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                for key, mapping in raw.items():
                    if key == "learned":
                        learned.update(mapping)
                    elif str(key).isdigit() and isinstance(mapping, dict):
                        curated.setdefault(str(key), {}).update(mapping)
            except Exception:
                log.exception("[CAL] Failed to read %s — using built-in holidays",
                              NSE_HOLIDAYS_FILE)
        _HOLIDAY_CACHE["sig"] = sig
        _HOLIDAY_CACHE["data"] = (curated, learned)
        return curated, learned


def _holidays() -> dict[str, dict[str, str]]:
    """Curated holidays only: {year_str: {iso_date: name}}."""
    return _load_holidays()[0]


def is_trading_day(target_date: date_cls) -> bool:
    """Weekend- and holiday-aware trading day check (NSE calendar)."""
    if target_date.weekday() >= 5:
        return False
    iso = target_date.strftime("%Y-%m-%d")
    curated, learned = _load_holidays()
    if iso in learned:
        return False
    year = str(target_date.year)
    year_data = curated.get(year)
    if not year_data:
        if year not in _HOLIDAY_WARNED_YEARS:
            _HOLIDAY_WARNED_YEARS.add(year)
            log.warning("[CAL] No holiday data for %s — treating as weekend-only. "
                        "Add it to %s.", year, NSE_HOLIDAYS_FILE)
        return True
    return iso not in year_data


def _learn_holiday(iso_date: str, note: str) -> None:
    """Persist a learned non-trading date (atomic write, merged with file)."""
    with _HOLIDAY_LOCK:
        try:
            raw: dict = {}
            if os.path.exists(NSE_HOLIDAYS_FILE):
                with open(NSE_HOLIDAYS_FILE, "r", encoding="utf-8") as f:
                    raw = json.load(f)
            learned = raw.setdefault("learned", {})
            if iso_date in learned:
                return
            learned[iso_date] = note
            tmp = f"{NSE_HOLIDAYS_FILE}.{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(raw, f, indent=2, sort_keys=True)
            os.replace(tmp, NSE_HOLIDAYS_FILE)
            _HOLIDAY_CACHE["sig"] = "unset"
            log.warning("[CAL] Learned non-trading day %s (%s)", iso_date, note)
        except Exception:
            log.exception("[CAL] Could not record learned holiday %s", iso_date)


def _maybe_learn_holiday(requested_iso: str, actual_iso: str) -> None:
    """
    AMFI answered a history request for `requested_iso` with `actual_iso`
    data. Learn the requested date as non-trading ONLY if it is a past
    weekday our calendar thought was open, and AMFI's answer is exactly the
    previous trading day (anything older looks like a publishing gap).
    """
    try:
        req = datetime.strptime(requested_iso, "%Y-%m-%d").date()
    except ValueError:
        return
    if req >= _now().date() or not is_trading_day(req):
        return
    if actual_iso != get_last_business_day(req).strftime("%Y-%m-%d"):
        return
    _learn_holiday(requested_iso, f"Learned: AMFI returned {actual_iso} data")


def get_last_business_day(from_date: date_cls | None = None) -> date_cls:
    """Most recent trading day strictly BEFORE from_date (holiday-aware)."""
    from_date = from_date or _now().date()
    candidate = from_date - timedelta(days=1)
    for _ in range(30):
        if is_trading_day(candidate):
            break
        candidate -= timedelta(days=1)
    return candidate


# ══════════════════════════════════════════════════════════════
# SNAPSHOT DIRECTORY: indexed + cached
# ══════════════════════════════════════════════════════════════
_INDEX_LOCK = threading.RLock()
_WRITE_LOCK = threading.Lock()
_CACHE_LOCK = threading.Lock()   # guards _DATE_CACHE / _ROWS_CACHE / _MAP_CACHE
_INDEX: dict = {"dir": None, "sig": None, "map": {}}
_DATE_CACHE: dict[str, tuple[tuple[int, int], Optional[str]]] = {}
_ROWS_CACHE: dict[str, tuple[tuple[int, int], int]] = {}
_MAP_CACHE: "OrderedDict[tuple, dict]" = OrderedDict()
_MAP_CACHE_MAX = 6


def _ensure_text_dir() -> None:
    os.makedirs(NAV_TEXT_DIR, exist_ok=True)


def _snapshot_path(date_str: str) -> str:
    return os.path.join(NAV_TEXT_DIR, f"nav_{date_str}.txt")


def _file_sig(path: str) -> tuple[int, int]:
    st = os.stat(path)
    return (st.st_mtime_ns, st.st_size)


def _file_nav_date(path: str) -> Optional[str]:
    """NAV date from file content (last column of first data row). Cached."""
    try:
        sig = _file_sig(path)
    except OSError:
        return None
    with _CACHE_LOCK:
        cached = _DATE_CACHE.get(path)
    if cached and cached[0] == sig:
        return cached[1]

    nav_date = None
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            for line in f:
                line = line.strip()
                if ";" not in line:
                    continue
                parts = line.split(";")
                if len(parts) < 6 or parts[0].strip() == "Scheme Code":
                    continue
                try:
                    nav_date = datetime.strptime(
                        parts[-1].strip(), "%d-%b-%Y").strftime("%Y-%m-%d")
                    break
                except ValueError:
                    continue
    except OSError:
        return None
    with _CACHE_LOCK:
        _DATE_CACHE[path] = (sig, nav_date)
    return nav_date


def _row_count(path: str) -> int:
    """Number of data rows in a snapshot file. Cached by (mtime, size)."""
    try:
        sig = _file_sig(path)
    except OSError:
        return 0
    with _CACHE_LOCK:
        cached = _ROWS_CACHE.get(path)
    if cached and cached[0] == sig:
        return cached[1]
    count = 0
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            for line in f:
                if line.count(";") >= 5 and not line.startswith("Scheme Code"):
                    count += 1
    except OSError:
        return 0
    with _CACHE_LOCK:
        _ROWS_CACHE[path] = (sig, count)
    return count


def _count_data_lines(text: str) -> int:
    return sum(1 for l in text.splitlines()
               if l.count(";") >= 5 and not l.startswith("Scheme Code"))


def _invalidate_index() -> None:
    with _INDEX_LOCK:
        _INDEX["sig"] = None


def _snapshot_index() -> dict[str, str]:
    """
    {nav_date_iso: path}. Built once, re-scanned only when the directory
    changes. If two files claim the same NAV date, the larger one wins.
    """
    with _INDEX_LOCK:
        _ensure_text_dir()
        try:
            sig = os.stat(NAV_TEXT_DIR).st_mtime_ns
        except OSError:
            return {}
        if (_INDEX["dir"] == NAV_TEXT_DIR and _INDEX["sig"] == sig):
            return _INDEX["map"]

        idx: dict[str, str] = {}
        for fname in os.listdir(NAV_TEXT_DIR):
            if not (fname.startswith("nav_") and fname.endswith(".txt")):
                continue
            path = os.path.join(NAV_TEXT_DIR, fname)
            nd = _file_nav_date(path)
            if not nd:
                continue
            if nd in idx:
                try:
                    if os.path.getsize(path) <= os.path.getsize(idx[nd]):
                        continue
                except OSError:
                    continue
            idx[nd] = path
        _INDEX.update({"dir": NAV_TEXT_DIR, "sig": sig, "map": idx})
        return idx


def _latest_snapshot_path() -> Optional[str]:
    idx = _snapshot_index()
    return idx[max(idx)] if idx else None


def _have_snapshot_for_date(iso_date: str) -> bool:
    return iso_date in _snapshot_index()


def get_snapshot_status() -> dict:
    idx = _snapshot_index()
    today = _now().strftime("%Y-%m-%d")
    latest_date = max(idx) if idx else None
    latest_path = idx[latest_date] if latest_date else None
    return {
        "has_today": today in idx,
        "today_path": idx.get(today),
        "latest_path": latest_path,
        "latest_date": latest_date,
        "latest_bytes": os.path.getsize(latest_path) if latest_path else None,
    }


def _previous_snapshot_path(current_nav_date: Optional[str] = None):
    """Previous snapshot by ACTUAL NAV date inside file content."""
    idx = _snapshot_index()
    if not idx:
        return None, None
    dates = sorted(idx)
    if current_nav_date is None:
        current_nav_date = dates[-1]
    for d in reversed(dates):
        if d < current_nav_date:
            return idx[d], d
    return None, None


def _read_snapshot_map(path: str) -> dict:
    """{isin: nav_float} for a snapshot file. Small LRU, returns a copy."""
    try:
        key = (path,) + _file_sig(path)
    except OSError:
        return {}
    with _CACHE_LOCK:
        cached = _MAP_CACHE.get(key)
        if cached is not None:
            _MAP_CACHE.move_to_end(key)
            return dict(cached)
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            nav_map, _, _ = _parse_nav_text(f.read())
    except OSError:
        return {}
    out = {isin: pt.nav for isin, pt in nav_map.items() if pt.nav > 0}
    with _CACHE_LOCK:
        _MAP_CACHE[key] = out
        while len(_MAP_CACHE) > _MAP_CACHE_MAX:
            _MAP_CACHE.popitem(last=False)
    return dict(out)


# ══════════════════════════════════════════════════════════════
# PARSING (single parser, everything else builds on it)
# ══════════════════════════════════════════════════════════════
def _clean_isin(v: Optional[str]) -> Optional[str]:
    v = (v or "").strip().upper()
    return None if v in ("", "-") else v


def _iter_rows(text: str):
    """
    Yield one dict per valid data row. COLUMN-COUNT AGNOSTIC: date is the
    last column, NAV is second-to-last. Tracks AMC / category headers.
    """
    amc = ""
    category = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if ";" not in line:
            if line.lower().endswith("mutual fund"):
                amc = line
            elif "(" in line and ")" in line:
                category = line
            continue

        parts = [p.strip() for p in line.split(";")]
        if len(parts) < 6 or parts[0] == "Scheme Code":
            continue
        try:
            nav_date = datetime.strptime(parts[-1], "%d-%b-%Y").strftime("%Y-%m-%d")
        except ValueError:
            continue
        try:
            nav = float(parts[-2])
        except ValueError:
            continue
        if nav <= 0:
            continue

        yield {
            "scheme_code": parts[0],
            "isin_1": _clean_isin(parts[1]),
            "isin_2": _clean_isin(parts[2]),
            "scheme_name": parts[3],
            "nav": nav,
            "nav_date": nav_date,
            "amc": amc,
            "category": category,
            "parts": parts,
        }


def _parse_nav_text(text: str) -> tuple[dict, dict, list[dict]]:
    """
    Returns:
      nav_map:  {isin: NavPoint(nav, nav_date)}
      amc_map:  {isin: amc_name}
      records:  list of scheme-metadata dicts
    """
    nav_map: dict[str, NavPoint] = {}
    amc_map: dict[str, str] = {}
    records: list[dict] = []

    for r in _iter_rows(text):
        primary = r["isin_1"] or r["isin_2"]
        point = NavPoint(r["nav"], r["nav_date"])
        if primary:
            records.append({
                "isin":        primary,
                "scheme_code": r["scheme_code"],
                "isin_payout": r["isin_2"],
                "scheme_name": r["scheme_name"],
                "amc_name":    r["amc"] or None,
                "category":    r["category"] or None,
                "nav":         r["nav"],
                "nav_date":    r["nav_date"],
            })
        for isin in (r["isin_1"], r["isin_2"]):
            if isin:
                nav_map[isin] = point
                if r["amc"]:
                    amc_map[isin] = r["amc"]

    log.debug("[NAV-PARSE] Parsed %d records, %d ISINs", len(records), len(nav_map))
    return nav_map, amc_map, records


def _extract_nav_date_from_text(text: str) -> Optional[str]:
    for line in text.splitlines():
        line = line.strip()
        if not line or ";" not in line:
            continue
        parts = line.split(";")
        if len(parts) < 6 or parts[0].strip() == "Scheme Code":
            continue
        try:
            return datetime.strptime(parts[-1].strip(), "%d-%b-%Y").strftime("%Y-%m-%d")
        except ValueError:
            continue
    log.error("[NAV-DATE-EXTRACT] Could not extract NAV date from text")
    return None


def _looks_like_amfi_format(text: str) -> bool:
    if not text or not text.strip():
        return False
    lowered = text.lower()
    if "<html" in lowered or "captcha" in lowered:
        return False
    return "scheme code" in lowered and ";" in text


_LIVE_HEADER = ("Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;"
                "Scheme Name;Net Asset Value;Date")


def _layout_from_header(header: str) -> Optional[dict]:
    cols = [c.strip().lower() for c in header.split(";")]
    try:
        code = next(i for i, c in enumerate(cols) if c.startswith("scheme code"))
        name = next(i for i, c in enumerate(cols) if c.startswith("scheme name"))
        nav = next(i for i, c in enumerate(cols) if "net asset value" in c)
        date = next(i for i, c in enumerate(cols) if c == "date")
    except StopIteration:
        return None
    isins = [i for i, c in enumerate(cols) if "isin" in c]
    if len(isins) < 2:
        return None
    return {"code": code, "name": name, "isin1": isins[0], "isin2": isins[1],
            "nav": nav, "date": date, "width": len(cols)}


# Layout the old code assumed for header-less history files.
_LEGACY_HISTORY_LAYOUT = {"code": 0, "name": 1, "isin1": 4, "isin2": 5,
                          "nav": 6, "date": 7, "width": 8}


def _normalize_history_text_to_live_format(text: str) -> str:
    """
    Rewrite any AMFI layout into the 6-col live layout
    (Code;ISIN1;ISIN2;Name;NAV;Date). Column positions come from the header
    row when present. Idempotent: live-format text is returned unchanged.
    """
    lines = text.splitlines()
    layout = None
    for line in lines:
        if line.strip().lower().startswith("scheme code"):
            layout = _layout_from_header(line)
            break
    if layout is None:
        layout = _LEGACY_HISTORY_LAYOUT

    if layout["name"] == 3 and layout["nav"] == layout["width"] - 2:
        return text  # already live layout

    need = max(layout[k] for k in ("code", "name", "isin1", "isin2", "nav", "date")) + 1
    out = []
    for line in lines:
        stripped = line.strip()
        if ";" not in stripped:
            out.append(line)
            continue
        parts = [p.strip() for p in stripped.split(";")]
        if parts[0].lower().startswith("scheme code"):
            out.append(_LIVE_HEADER)
            continue
        if len(parts) < need:
            out.append(line)
            continue
        out.append(";".join([
            parts[layout["code"]], parts[layout["isin1"]], parts[layout["isin2"]],
            parts[layout["name"]], parts[layout["nav"]], parts[layout["date"]],
        ]))
    return "\n".join(out)


def _fmt_amfi_date(d) -> str:
    return d.strftime("%d-%b-%Y")


# ══════════════════════════════════════════════════════════════
# SNAPSHOT SAVE (atomic)
# ══════════════════════════════════════════════════════════════
def _save_nav_snapshot(text: str, iso_date: str, *,
                       allow_overwrite: bool = True,
                       source: str = "unknown") -> dict:
    """
    Atomically save a snapshot. An existing file with MORE rows is never
    clobbered by a thinner one (partial-history vs full-live protection).
    When a thicker file is kept, its mtime is refreshed so the freshness
    gate does not keep re-downloading.
    """
    _ensure_text_dir()
    path = _snapshot_path(iso_date)
    new_count = _count_data_lines(text)

    with _WRITE_LOCK:
        if os.path.exists(path):
            existing = _row_count(path)
            if not allow_overwrite or existing > new_count:
                log.warning(
                    "[NAV-SAVE] Keeping existing %s (existing=%d rows, incoming=%d, "
                    "source=%s, allow_overwrite=%s).",
                    path, existing, new_count, source, allow_overwrite)
                if allow_overwrite and existing > new_count:
                    try:
                        os.utime(path, None)
                    except OSError:
                        pass
                return {"path": path, "bytes": os.path.getsize(path),
                        "date": iso_date, "record_count": existing,
                        "kept_existing": True}

        tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    _invalidate_index()
    size = os.path.getsize(path)
    log.info("[NAV-SAVE] Saved %s: %s bytes, %d rows (source=%s)",
             path, size, new_count, source)
    return {"path": path, "bytes": size, "date": iso_date,
            "record_count": new_count, "kept_existing": False}


# ══════════════════════════════════════════════════════════════
# ingestion helper used by every save path
# ══════════════════════════════════════════════════════════════
def _ingest_logged(path: str) -> dict:
    try:
        res = ingest_nav_file_to_db(path)
    except Exception as e:  # ingest_nav_file_to_db already catches; belt & braces
        res = {"ok": False, "reason": str(e)}
    if res.get("ok"):
        log.info("[NAV-DB] %s", res.get("reason"))
    else:
        log.error("[NAV-DB] Failed: %s", res.get("reason"))
        sflog.record_failure(
            source="nav", stage="db_insert", file=path,
            msg=f"NAV ingestion failed: {res.get('reason')}",
        )
    return res


def _save_and_ingest(text: str, iso_date: str, source: str) -> tuple[dict, dict]:
    saved = _save_nav_snapshot(text, iso_date, allow_overwrite=True, source=source)
    ing = _ingest_logged(saved["path"])
    return saved, ing


# ══════════════════════════════════════════════════════════════
# LIVE DOWNLOAD
# ══════════════════════════════════════════════════════════════
def _min_live_rows() -> int:
    """Row floor for a live file: absolute minimum, or 90% of the latest snapshot."""
    path = _latest_snapshot_path()
    prev = _row_count(path) if path else 0
    return max(_MIN_ACCEPTABLE_ISIN_COUNT, int(prev * _LIVE_MIN_FRACTION_OF_PREV))


def download_and_save_nav(timeout: int = 30) -> dict:
    """
    Download NAVAll.txt and save under the ACTUAL NAV date (from content).
    Rejects empty / non-AMFI / partial responses before touching disk.
    """
    log.info("[AMFI] Downloading NAV file from %s", AMFI_TEXT_URL)
    fail = {"path": None, "bytes": 0, "date": None}

    try:
        res = _AMFI_SESSION.get(AMFI_TEXT_URL, timeout=timeout)
        res.raise_for_status()
    except requests.RequestException as e:
        log.error("[AMFI] Download failed: %s", e)
        return {**fail, "reason": f"download failed: {e}"}

    text = _decode_response(res)
    if not _looks_like_amfi_format(text):
        log.error("[AMFI] Response is not AMFI NAV data (len=%d)", len(text))
        return {**fail, "reason": "response is not AMFI NAV data"}

    rows = _count_data_lines(text)
    floor = _min_live_rows()
    if rows < floor:
        log.error("[AMFI] Live file has only %d rows (< %d) — rejecting", rows, floor)
        return {**fail, "reason": f"partial live file ({rows} rows, need {floor})"}

    actual_nav_date = _extract_nav_date_from_text(text)
    if not actual_nav_date:
        return {**fail, "reason": "NAV date extraction failed"}

    try:
        saved = _save_nav_snapshot(text, actual_nav_date,
                                   allow_overwrite=True, source="live")
    except OSError as e:
        log.error("[AMFI] Failed to write snapshot: %s", e)
        return {**fail, "reason": f"write failed: {e}"}

    log.info("[AMFI] NAV file %s: %s (%s bytes, %d rows) | NAV Date: %s",
             "kept" if saved["kept_existing"] else "saved",
             saved["path"], saved["bytes"], rows, actual_nav_date)
    return {"path": saved["path"], "bytes": saved["bytes"],
            "date": actual_nav_date, "rows": rows,
            "kept_existing": saved["kept_existing"]}


# ── freshness gate ────────────────────────────────────────────
def _last_passed_cutoff_today(now: datetime) -> Optional[time_cls]:
    passed = [t for t in NAV_REDOWNLOAD_TIMES if now.time() >= t]
    return max(passed) if passed else None


def _attempt_file() -> str:
    return os.path.join(NAV_TEXT_DIR, ".last_attempt.json")


def _read_attempt() -> dict:
    try:
        with open(_attempt_file(), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write_attempt(ok: bool, reason: str) -> None:
    try:
        _ensure_text_dir()
        path = _attempt_file()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"ts": _time.time(), "ok": ok, "reason": reason}, f)
        os.replace(tmp, path)
    except OSError:
        log.exception("[AMFI] Could not record download attempt")


def _required_snapshot(now: datetime) -> tuple[date_cls, datetime]:
    """
    (expected NAV date, minimum file mtime — IST-aware) for the snapshot to
    be current.

      trading day, a cutoff has passed → today's NAV, saved after that cutoff
      otherwise (before first cutoff, weekend, holiday) → previous trading
        day's NAV, saved after its last (late-night) cutoff
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=IST)
    else:
        now = now.astimezone(IST)
    today = now.date()
    cutoff = _last_passed_cutoff_today(now)
    if is_trading_day(today) and cutoff is not None:
        return today, datetime.combine(today, cutoff, tzinfo=IST)
    prev = get_last_business_day(today)
    return prev, datetime.combine(prev, max(NAV_REDOWNLOAD_TIMES), tzinfo=IST)


def _snapshot_is_fresh(expected: date_cls, min_mtime: datetime) -> tuple[bool, Optional[str]]:
    expected_iso = expected.strftime("%Y-%m-%d")
    idx = _snapshot_index()
    for d in sorted(idx, reverse=True):
        if d < expected_iso:
            break
        path = idx[d]
        if _row_count(path) < _MIN_ACCEPTABLE_ISIN_COUNT:
            continue
        if d > expected_iso:
            return True, path
        try:
            if datetime.fromtimestamp(os.path.getmtime(path), IST) >= min_mtime:
                return True, path
        except OSError:
            continue
    return False, None


def download_and_save_nav_if_needed(force: bool = False) -> dict:
    """
    Download + ingest only when the on-disk snapshot is stale for the current
    NAV cycle (see _required_snapshot). Works on weekends/holidays (compares
    NAV dates, not download dates) and backs off RETRY_BACKOFF_SECONDS after
    any attempt. `ok` is False if the download OR the DB ingestion failed.
    `stale` is True when we still do not hold the expected NAV date
    (e.g. AMFI has not published yet).
    """
    now = _now()
    expected, min_mtime = _required_snapshot(now)
    expected_iso = expected.strftime("%Y-%m-%d")

    if not force:
        fresh, path = _snapshot_is_fresh(expected, min_mtime)
        if fresh:
            return {"ran": False, "ok": True, "stale": False,
                    "reason": f"snapshot {os.path.basename(path)} is current",
                    "bytes": os.path.getsize(path)}

        last = _read_attempt()
        age = _time.time() - float(last.get("ts", 0) or 0)
        if last and age < RETRY_BACKOFF_SECONDS:
            return {"ran": False, "ok": bool(last.get("ok")), "stale": True,
                    "reason": f"backoff: last attempt {int(age)}s ago "
                              f"({last.get('reason', '')}); retry in "
                              f"{int(RETRY_BACKOFF_SECONDS - age)}s",
                    "bytes": None}

    try:
        result = download_and_save_nav()
    except Exception as e:
        log.exception("[AMFI] Download failed with exception")
        _write_attempt(False, f"exception: {e}")
        return {"ran": True, "ok": False, "stale": True,
                "reason": f"download failed: {e}", "bytes": None}

    if result.get("date") is None:
        reason = result.get("reason", "download failed")
        _write_attempt(False, reason)
        sflog.record_failure(source="nav", stage="fetch", msg=reason)
        return {"ran": True, "ok": False, "stale": True, "reason": reason, "bytes": None}

    ing = _ingest_logged(result["path"])
    ok = bool(ing.get("ok"))
    stale = result["date"] < expected_iso
    reason = ("downloaded and ingested" if ok
              else f"downloaded; DB ingestion failed: {ing.get('reason')}")
    if stale:
        reason += f" (AMFI still at {result['date']}, expected {expected_iso})"
    _write_attempt(ok, reason)
    return {"ran": True, "ok": ok, "stale": stale, "reason": reason,
            "bytes": result.get("bytes"), "date": result.get("date")}


# ══════════════════════════════════════════════════════════════
# HISTORICAL DOWNLOAD
# ══════════════════════════════════════════════════════════════
def download_business_day_nav(target_date, timeout: int = 30) -> dict:
    """
    Fetch AMFI history for one exact date and report what date the response
    actually contains (holidays return the last working day's data).
    Raises ValueError if the response is not AMFI-shaped (blocked page).
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
    text = _decode_response(res)

    if not _looks_like_amfi_format(text):
        snippet = text.strip()[:200].replace("\n", " ")
        log.error("[AMFI-HIST] Response for %s isn't AMFI data (status=%s, len=%s). "
                  "Snippet: %r", date_str, res.status_code, len(text), snippet)
        raise ValueError(f"blocked_or_invalid_response for {date_str}: {snippet!r}")

    text = _normalize_history_text_to_live_format(text)

    actual_date = _extract_nav_date_from_text(text)
    if actual_date is None:
        snippet = text.strip()[:200].replace("\n", " ")
        log.error("[AMFI-HIST] %s: AMFI-shaped but zero parseable date rows. "
                  "Snippet: %r", date_str, snippet)
        raise ValueError(f"no_date_in_response for {date_str}: {snippet!r}")

    log.info("[AMFI-HIST] Requested %s, file actually dated %s", date_str, actual_date)
    return {
        "text": text,
        "requested_date": target_date.strftime("%Y-%m-%d"),
        "actual_date": actual_date,
    }


def sync_previous_business_day_nav(force: bool = False, timeout: int = 30) -> dict:
    """
    Fetch the trading day before the latest snapshot's NAV date, save and
    ingest it. The calendar is holiday-aware, and if AMFI answers with an
    earlier date anyway, that actual date is saved (and the requested date
    is learned as non-trading when it is safe to do so).
    """
    latest_path = _latest_snapshot_path()
    if latest_path is None:
        return {"ran": False, "ok": False, "reason": "no live snapshot yet"}

    latest_str = _file_nav_date(latest_path)
    if not latest_str:
        return {"ran": False, "ok": False,
                "reason": "could not read NAV date from latest snapshot"}

    target = get_last_business_day(datetime.strptime(latest_str, "%Y-%m-%d").date())
    iso_date = target.strftime("%Y-%m-%d")

    if not force and _have_snapshot_for_date(iso_date):
        log.info("[AMFI-HIST] Already have %s — skipping fetch", iso_date)
        return {"ran": False, "ok": True,
                "reason": f"already have {iso_date}", "date": iso_date}

    try:
        result = download_business_day_nav(target, timeout=timeout)
    except ValueError as e:
        log.error("[AMFI-HIST] %s", e)
        sflog.record_failure(source="nav", stage="fetch", msg=str(e),
                             context={"requested_date": iso_date})
        return {"ran": True, "ok": False, "reason": str(e), "date": iso_date}
    except requests.RequestException:
        log.exception("[AMFI-HIST] Network error for %s", iso_date)
        sflog.record_failure(source="nav", stage="fetch", msg="Network error",
                             context={"requested_date": iso_date})
        return {"ran": True, "ok": False, "reason": "network error", "date": iso_date}

    actual = result["actual_date"]
    if actual != iso_date:
        log.info("[AMFI-HIST] Requested %s but AMFI returned %s", iso_date, actual)
        _maybe_learn_holiday(iso_date, actual)
        if not force and _have_snapshot_for_date(actual):
            return {"ran": False, "ok": True,
                    "reason": f"already have {actual}", "date": actual}

    saved, ing = _save_and_ingest(result["text"], actual, source="history")
    reason = "downloaded" if actual == iso_date else \
        f"requested {iso_date}, saved actual {actual}"
    return {"ran": True, "ok": bool(ing.get("ok")), "reason": reason, **saved}


def sync_previous_business_day_nav_if_needed(
    force: bool = False,
    state: dict | None = None,
) -> dict:
    """
    Runs at most once per calendar day IF `state` is provided (pass
    st.session_state from app.py). Headless callers pass None — the
    underlying sync is idempotent via snapshot checks.
    """
    today = _now().strftime("%Y-%m-%d")

    if not force and state is not None and state.get("prev_nav_done_for") == today:
        return {"ran": False, "ok": True, "reason": "already synced successfully today"}

    result = sync_previous_business_day_nav(force=force)

    if result.get("ok"):
        if state is not None:
            try:
                state["prev_nav_done_for"] = today
            except Exception:
                pass
    else:
        log.warning("[NAV-SYNC] Previous-day sync did not succeed (%s) — will retry next call",
                    result.get("reason"))
    return result


# ══════════════════════════════════════════════════════════════
# PREVIOUS SNAPSHOT
# ══════════════════════════════════════════════════════════════
def load_previous_nav_map(current_date_iso=None) -> dict:
    """
    {isin: nav_float} for the snapshot before `current_date_iso`
    (default: before the latest snapshot), or {} if none.
    """
    cur = _as_date(current_date_iso).strftime("%Y-%m-%d") if current_date_iso else None
    path, _ = _previous_snapshot_path(cur)
    return _read_snapshot_map(path) if path else {}


def get_previous_nav_date(current_date_iso=None) -> Optional[str]:
    cur = _as_date(current_date_iso).strftime("%Y-%m-%d") if current_date_iso else None
    _, date_str = _previous_snapshot_path(cur)
    return date_str


# ══════════════════════════════════════════════════════════════
# DB LOOKUP FOR A SPECIFIC DATE
# ══════════════════════════════════════════════════════════════
def get_nav_for_date_from_db(target_iso: str) -> dict:
    """{isin: nav_value} for a specific date from nav_history. {} if none."""
    try:
        with get_conn() as conn:
            rows = conn.execute("""
                SELECT o.isin_payout, o.isin_reinvest, nh.nav_value
                FROM nav_history nh
                JOIN nav_options o ON nh.nav_option_id = o.id
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
# HISTORICAL LOOKUP ORCHESTRATOR (local-first, network last)
# ══════════════════════════════════════════════════════════════
def _local_nav_map(iso: str) -> tuple[dict, bool]:
    """
    Disk + DB only — NO network. Returns (map, is_full).
      1. disk snapshot if it passes the coverage threshold
      2. DB (authoritative if non-empty)
      3. otherwise the partial disk map (is_full=False)
    """
    partial: dict = {}
    path = _snapshot_index().get(iso)
    if path:
        partial = _read_snapshot_map(path)
        if len(partial) >= _MIN_ACCEPTABLE_ISIN_COUNT:
            log.info("[NAV-LOOKUP] %s → %d ISINs from disk (OK)", iso, len(partial))
            return partial, True
        if partial:
            log.warning("[NAV-LOOKUP] Disk snapshot for %s is partial (%d < %d)",
                        iso, len(partial), _MIN_ACCEPTABLE_ISIN_COUNT)

    db_map = get_nav_for_date_from_db(iso)
    if db_map:
        log.info("[NAV-LOOKUP] %s → %d ISINs from DB (authoritative)", iso, len(db_map))
        return db_map, True

    return partial, False


def _network_nav_map(target_iso: str, fallback: dict) -> dict:
    """
    AMFI history fetch for one date; saves + ingests; returns best map FOR
    THAT DATE. If AMFI answers with a different date, that data is stored
    under its real date and is never returned as `target_iso`'s data.
    """
    best = fallback
    try:
        target_d = datetime.strptime(target_iso, "%Y-%m-%d").date()
        resp = download_business_day_nav(target_d, timeout=30)
        actual = resp.get("actual_date")
        if actual and resp.get("text"):
            if actual != target_iso:
                log.warning("[NAV-LOOKUP] Asked for %s, AMFI returned %s data — "
                            "not using it for %s", target_iso, actual, target_iso)
                _maybe_learn_holiday(target_iso, actual)
                if not _have_snapshot_for_date(actual):
                    _save_and_ingest(resp["text"], actual, source="history")
                return best

            saved, _ = _save_and_ingest(resp["text"], actual, source="history")
            hist = _read_snapshot_map(saved["path"])
            if len(hist) > len(best):
                best = hist
            if len(hist) >= _MIN_ACCEPTABLE_ISIN_COUNT:
                log.info("[NAV-LOOKUP] %s → %d ISINs from history endpoint (OK)",
                         target_iso, len(hist))
            else:
                log.warning("[NAV-LOOKUP] History endpoint for %s also partial (%d rows)",
                            target_iso, len(hist))
    except Exception as e:
        log.warning("[NAV-LOOKUP] History fetch failed for %s: %s", target_iso, e)

    if not best:
        log.error("[NAV-LOOKUP] %s — no data available from any source", target_iso)
    return best


def get_or_fetch_nav_for_date(target_iso: str, allow_network: bool = True) -> dict:
    """
    {isin: nav} for a date. Order: disk (full) → DB → AMFI history endpoint →
    best partial. Network is touched only when local sources can't give a
    full map (and never if allow_network=False).
    """
    local, full = _local_nav_map(target_iso)
    if full or not allow_network:
        return local
    return _network_nav_map(target_iso, local)


# ══════════════════════════════════════════════════════════════
# SKIP-BACK BASELINE RESOLVER
# ══════════════════════════════════════════════════════════════
def _resolve_prev_nav_for_folios_impl(
    current_date: date_cls,
    our_isins: set,
    max_lookback: int = 10,
    min_overlap_pct: float = 0.5,
    network_budget: int = 1,
) -> tuple[Optional[str], dict, dict]:
    """
    Pure implementation. Walks back over trading days using LOCAL data
    (disk, DB); spends at most `network_budget` AMFI fetches, on the first
    date that has no full local data.
    """
    empty_diag = {"tried": [], "chosen": None, "overlap": 0, "needed": 0,
                  "candidates": []}
    if not our_isins:
        return None, {}, empty_diag

    our_isins = {str(i).strip().upper() for i in our_isins if i}
    needed = max(1, int(len(our_isins) * min_overlap_pct))

    tried: list[str] = []
    candidates: list[dict] = []
    cursor = current_date
    budget = network_budget

    for _ in range(max_lookback):
        cursor = get_last_business_day(cursor)
        iso = cursor.strftime("%Y-%m-%d")
        tried.append(iso)

        nav_map, full = _local_nav_map(iso)
        fetched = False
        if not full and budget > 0:
            budget -= 1
            fetched = True
            nav_map = _network_nav_map(iso, nav_map)

        overlap = len(our_isins & set(nav_map.keys()))
        candidates.append({
            "date": iso, "rows": len(nav_map), "folio_overlap": overlap,
            "needed": needed, "network": fetched,
        })
        log.info("[NAV-SKIPBACK] %s → %d rows, %d/%d folio ISINs (need %d)%s",
                 iso, len(nav_map), overlap, len(our_isins), needed,
                 " [fetched]" if fetched else "")

        if overlap >= needed:
            return iso, nav_map, {"tried": tried, "chosen": iso, "overlap": overlap,
                                  "needed": needed, "candidates": candidates}

    log.warning("[NAV-SKIPBACK] No date within %d business days covered ≥ %d "
                "folio ISINs. Tried: %s", max_lookback, needed, tried)
    return None, {}, {"tried": tried, "chosen": None, "overlap": 0,
                      "needed": needed, "candidates": candidates}


def resolve_prev_nav_for_folios(
    current_date_iso,
    isins_tuple: tuple,
    max_lookback: int = 10,
    min_overlap_pct: float = 0.5,
) -> tuple:
    """
    Walk backward through prior trading days until a date covers ≥
    min_overlap_pct of the given ISINs.
    Returns (iso_date_or_None, nav_map, diagnostics).
    app.py wraps this with @st.cache_data.
    """
    return _resolve_prev_nav_for_folios_impl(
        _as_date(current_date_iso), set(isins_tuple),
        max_lookback=max_lookback, min_overlap_pct=min_overlap_pct,
    )


# ══════════════════════════════════════════════════════════════
# HIGH-LEVEL BASELINE ORCHESTRATORS
# ─────────────────────────────────────────────────────────────
# Pure NAV answers — no pandas, no folio dataframe coupling. The
# @st.cache_data wrappers and per-row diff arithmetic stay in app.py.
# ══════════════════════════════════════════════════════════════

def get_previous_nav_baseline(
    current_date_iso,
    our_isins: tuple,
    max_lookback: int = 10,
    min_overlap_pct: float = 0.5,
) -> tuple:
    """
    High-level "previous baseline" resolver.

    Wraps resolve_prev_nav_for_folios() with a fallback: if the skip-back
    loop exhausts max_lookback without finding a date with sufficient ISIN
    overlap, fall back to the newest snapshot on disk that is strictly
    BEFORE current_date_iso.

    Returns (iso_or_None, nav_map, diagnostics). diagnostics is the
    skip-back diagnostics plus:
      - fallback_used : bool
      - fallback_date : str | None
      - first_tried_date : str | None  (immediate previous trading day)
    """
    iso, nav_map, diag = resolve_prev_nav_for_folios(
        current_date_iso, our_isins,
        max_lookback=max_lookback,
        min_overlap_pct=min_overlap_pct,
    )

    current = _as_date(current_date_iso)
    current_iso = current.strftime("%Y-%m-%d")
    diag["first_tried_date"] = get_last_business_day(current).strftime("%Y-%m-%d")

    if iso and nav_map:
        diag["fallback_used"] = False
        diag["fallback_date"] = None
        return iso, nav_map, diag

    # Skip-back exhausted — fall back to the newest snapshot older than the
    # requested date (NOT older than the latest file on disk).
    fallback_map = load_previous_nav_map(current_iso)
    fallback_date = get_previous_nav_date(current_iso)
    if fallback_map:
        diag["fallback_used"] = True
        diag["fallback_date"] = fallback_date
        log.warning(
            "[NAV-BASELINE] Skip-back found no sufficient overlap in "
            "%d trading days; falling back to previous snapshot "
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
    days_back: Optional[int] = None,
    use_business_days: bool = True,
) -> tuple:
    """
    Fetch NAV for roughly "one week before" current_date.

    use_business_days=True  (default) — `days_back` counts NSE TRADING days,
        default 5 (= the same weekday last week, barring holidays).
    use_business_days=False — `days_back` is calendar days, default 7; the
        result is walked back to the nearest preceding trading day.

    Returns (target_iso_or_None, nav_map).
    """
    current_date = _as_date(current_date_iso)
    if days_back is None:
        days_back = 5 if use_business_days else 7

    if use_business_days:
        cursor = current_date
        counted = 0
        while counted < days_back:
            cursor -= timedelta(days=1)
            if is_trading_day(cursor):
                counted += 1
        target = cursor
    else:
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

    nav_map = get_or_fetch_nav_for_date(target_iso, allow_network=True)
    return target_iso, nav_map


# ══════════════════════════════════════════════════════════════
# IN-MEMORY INDEX
# ══════════════════════════════════════════════════════════════
class _IndexState(NamedTuple):
    nav: dict
    amc: dict
    records: list
    path: Optional[str]
    sig: Optional[tuple]


_EMPTY_STATE = _IndexState({}, {}, [], None, None)


class AMFINavIndex:
    """
    In-memory ISIN-keyed index of the latest snapshot. Thread-safe, never
    touches the network, reloads automatically when a newer / changed
    snapshot appears (checked at most every _SIG_CHECK_INTERVAL seconds).
    State is swapped atomically as one immutable tuple.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._state: _IndexState = _EMPTY_STATE
        self._checked_at: float = 0.0
        self._failed_at: Optional[float] = None

    @staticmethod
    def _current_sig() -> Optional[tuple]:
        path = _latest_snapshot_path()
        if not path:
            return None
        try:
            return (path,) + _file_sig(path)
        except OSError:
            return None

    def _recently_failed(self) -> bool:
        return (self._failed_at is not None
                and (_time.monotonic() - self._failed_at) < _SIG_CHECK_INTERVAL)

    def _ensure_loaded(self) -> None:
        st = self._state
        if st.nav and (_time.monotonic() - self._checked_at) < _SIG_CHECK_INTERVAL:
            return
        if not st.nav and self._recently_failed():
            return
        with self._lock:
            st = self._state
            now = _time.monotonic()
            if st.nav and (now - self._checked_at) < _SIG_CHECK_INTERVAL:
                return
            if not st.nav and self._recently_failed():
                return
            self._checked_at = now
            if st.nav and self._current_sig() == st.sig:
                return
            self._load_locked()

    def _load_locked(self) -> dict[str, NavPoint]:
        path = _latest_snapshot_path()
        if path is None:
            self._failed_at = _time.monotonic()
            _log_throttled("amfi-no-file", logging.ERROR,
                           "[AMFI] No saved NAV file in '%s' (abs: %s). Run "
                           "download_and_save_nav_if_needed() first, or check NAV_TEXT_DIR.",
                           NAV_TEXT_DIR, os.path.abspath(NAV_TEXT_DIR))
            return {}
        try:
            sig = (path,) + _file_sig(path)
            with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
                text = f.read()
        except OSError:
            self._failed_at = _time.monotonic()
            _log_throttled("amfi-read-fail", logging.ERROR,
                           "[AMFI] Failed to read saved NAV file '%s'", path)
            return {}
        if not text.strip():
            self._failed_at = _time.monotonic()
            _log_throttled("amfi-empty-file", logging.ERROR,
                           "[AMFI] Saved NAV file '%s' is empty", path)
            return {}

        nav_map, amc_map, records = _parse_nav_text(text)
        self._state = _IndexState(nav_map, amc_map, records, path, sig)
        self._checked_at = _time.monotonic()
        self._failed_at = None
        log.info("[AMFI] Loaded index from '%s': %s ISINs (NAV), %s ISINs (AMC), %s records",
                 path, len(nav_map), len(amc_map), len(records))
        return nav_map

    def load(self, force: bool = False) -> dict[str, NavPoint]:
        with self._lock:
            if force or (not self._state.nav and not self._recently_failed()):
                return self._load_locked() or self._state.nav
            self._ensure_loaded()
            return self._state.nav

    def get_nav(self, isin: str) -> Optional[NavPoint]:
        if not isin:
            return None
        self._ensure_loaded()
        st = self._state
        if not st.nav:
            self.load()
            st = self._state
        return st.nav.get(isin.strip().upper())

    def get_amc(self, isin: str) -> str:
        if not isin:
            return ""
        self._ensure_loaded()
        st = self._state
        if not st.amc:
            self.load()
            st = self._state
        return st.amc.get(isin.strip().upper(), "")

    def get_records(self) -> list[dict]:
        self._ensure_loaded()
        st = self._state
        if not st.records:
            self.load()
            st = self._state
        return st.records


_amfi = AMFINavIndex()


def fetch_nav_by_isin(isin: str) -> Optional[NavPoint]:
    return _amfi.get_nav(isin)


def fetch_amc_by_isin(isin: str) -> str:
    return _amfi.get_amc(isin)


def load_nav_dataframe():
    """AMFI NAV data as a DataFrame."""
    _amfi.load()
    return pd.DataFrame(_amfi.get_records(), columns=[
        "isin", "scheme_code", "isin_payout",
        "scheme_name", "amc_name", "category",
        "nav", "nav_date",
    ])


# ══════════════════════════════════════════════════════════════
# DB INDEXES
# ══════════════════════════════════════════════════════════════
_indexes_ready = False
_UNIQUE_OK = False          # True once UNIQUE(nav_option_id, nav_date) exists
_INDEX_LOCK_DB = threading.Lock()


def _create_unique_history_index(conn) -> bool:
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_nav_history_opt_date "
                 "ON nav_history(nav_option_id, nav_date)")
    return True


def ensure_db_indexes() -> None:
    """Idempotent. Called automatically before the first ingestion."""
    global _indexes_ready, _UNIQUE_OK
    if _indexes_ready:
        return
    with _INDEX_LOCK_DB:
        if _indexes_ready:
            return
        try:
            with get_conn() as conn:
                try:
                    _UNIQUE_OK = _create_unique_history_index(conn)
                except Exception:
                    log.warning("[NAV-DB] UNIQUE (nav_option_id, nav_date) failed — "
                                "removing duplicate rows and retrying")
                    try:
                        conn.execute(
                            "DELETE FROM nav_history WHERE rowid NOT IN ("
                            "SELECT MAX(rowid) FROM nav_history "
                            "GROUP BY nav_option_id, nav_date)")
                        _UNIQUE_OK = _create_unique_history_index(conn)
                    except Exception:
                        log.exception("[NAV-DB] Could not enforce UNIQUE index — "
                                      "using plain index and INSERT OR REPLACE")
                        _UNIQUE_OK = False
                        conn.execute("CREATE INDEX IF NOT EXISTS ix_nav_history_opt_date "
                                     "ON nav_history(nav_option_id, nav_date)")
                for stmt in (
                    "CREATE INDEX IF NOT EXISTS ix_nav_history_date ON nav_history(nav_date)",
                    "CREATE INDEX IF NOT EXISTS ix_nav_options_isin_payout ON nav_options(isin_payout)",
                    "CREATE INDEX IF NOT EXISTS ix_nav_options_isin_reinvest ON nav_options(isin_reinvest)",
                    "CREATE INDEX IF NOT EXISTS ix_nav_options_scheme ON nav_options(scheme_code, plan, option_name)",
                ):
                    conn.execute(stmt)
                conn.commit()
            _indexes_ready = True
        except Exception:
            log.exception("[NAV-DB] Index creation failed (non-fatal)")


# ══════════════════════════════════════════════════════════════
# DB QUERIES
# ══════════════════════════════════════════════════════════════
# Two indexed lookups instead of `isin_payout = ? OR isin_reinvest = ?`
# (an OR defeats both indexes). Bind the ISIN twice.
_OPT_CTE = ("WITH opts AS ("
            "SELECT id FROM nav_options WHERE isin_payout = ? "
            "UNION "
            "SELECT id FROM nav_options WHERE isin_reinvest = ?) ")


def _points_from_rows(rows) -> dict[str, NavPoint]:
    """rows of (isin_payout, isin_reinvest, nav_value, nav_date) → {isin: NavPoint}.
    Emits BOTH ISINs of every option."""
    out: dict[str, NavPoint] = {}
    for payout, reinvest, nav, d in rows:
        pt = NavPoint(float(nav), str(d))
        for isin in (payout, reinvest):
            if isin and str(isin).strip() and str(isin) != "-":
                out[str(isin).strip().upper()] = pt
    return out


def get_latest_nav_by_isin(isin: str) -> Optional[NavPoint]:
    """Latest NAV + date for an ISIN, from DB."""
    isin = (isin or "").strip().upper()
    with get_conn() as conn:
        row = conn.execute(_OPT_CTE + """
            SELECT nh.nav_value, nh.nav_date
            FROM nav_history nh
            JOIN opts ON nh.nav_option_id = opts.id
            ORDER BY nh.nav_date DESC
            LIMIT 1
        """, (isin, isin)).fetchone()
    return NavPoint(float(row[0]), str(row[1])) if row else None


def get_latest_nav_by_scheme_and_plan(scheme_code: int,
                                      plan: str = "Regular Plan"
                                      ) -> Optional[NavPoint]:
    with get_conn() as conn:
        row = conn.execute("""
            SELECT nh.nav_value, nh.nav_date
            FROM nav_history nh
            JOIN nav_options o ON nh.nav_option_id = o.id
            WHERE o.scheme_code = ? AND o.plan = ?
            ORDER BY nh.nav_date DESC, o.option_name
            LIMIT 1
        """, (scheme_code, plan)).fetchone()
    return NavPoint(float(row[0]), str(row[1])) if row else None


def get_all_latest_navs() -> dict[str, NavPoint]:
    """All latest NAVs by ISIN (both payout and reinvest ISINs)."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT o.isin_payout, o.isin_reinvest, nh.nav_value, nh.nav_date
            FROM nav_history nh
            JOIN nav_options o ON nh.nav_option_id = o.id
            WHERE nh.nav_date = (SELECT MAX(nav_date) FROM nav_history)
        """).fetchall()
    return _points_from_rows(rows)


def get_nav_for_date_by_isin(isin: str, nav_date: str) -> Optional[float]:
    """Specific ISIN on specific date, from DB. Accepts YYYY-MM-DD or DD-Mon-YYYY."""
    if "-" in nav_date and len(nav_date) == 10 and nav_date[4] == "-":
        date_str = nav_date
    else:
        try:
            date_str = datetime.strptime(nav_date, "%d-%b-%Y").strftime("%Y-%m-%d")
        except ValueError:
            return None
    isin = (isin or "").strip().upper()

    with get_conn() as conn:
        row = conn.execute(_OPT_CTE + """
            SELECT nh.nav_value
            FROM nav_history nh
            JOIN opts ON nh.nav_option_id = opts.id
            WHERE nh.nav_date = ?
            LIMIT 1
        """, (isin, isin, date_str)).fetchone()
    return float(row[0]) if row else None


def get_nav_for_date(isin: str, nav_date: str) -> Optional[float]:
    """Deprecated alias of get_nav_for_date_by_isin."""
    warnings.warn("get_nav_for_date is deprecated; use get_nav_for_date_by_isin",
                  DeprecationWarning, stacklevel=2)
    return get_nav_for_date_by_isin(isin, nav_date)


def get_previous_nav_date_db() -> Optional[str]:
    """Penultimate distinct nav_date in DB."""
    with get_conn() as conn:
        row = conn.execute("""
            SELECT DISTINCT nav_date FROM nav_history
            ORDER BY nav_date DESC LIMIT 1 OFFSET 1
        """).fetchone()
    return str(row[0]) if row else None


def get_previous_nav_by_isin(isin: str) -> Optional[NavPoint]:
    """Penultimate NAV for an ISIN, from DB."""
    isin = (isin or "").strip().upper()
    with get_conn() as conn:
        row = conn.execute(_OPT_CTE + """
            SELECT nh.nav_value, nh.nav_date
            FROM nav_history nh
            JOIN opts ON nh.nav_option_id = opts.id
            ORDER BY nh.nav_date DESC
            LIMIT 1 OFFSET 1
        """, (isin, isin)).fetchone()
    return NavPoint(float(row[0]), str(row[1])) if row else None


def get_previous_navs_map() -> dict[str, NavPoint]:
    """All penultimate NAVs by ISIN (both ISINs), from DB."""
    prev_date = get_previous_nav_date_db()
    if not prev_date:
        return {}
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT o.isin_payout, o.isin_reinvest, nh.nav_value, nh.nav_date
            FROM nav_history nh
            JOIN nav_options o ON nh.nav_option_id = o.id
            WHERE nh.nav_date = ?
        """, (prev_date,)).fetchall()
    return _points_from_rows(rows)


def _history(isin: str, days: int, regular_only: bool) -> list[dict]:
    isin = (isin or "").strip().upper()
    plan_join = ("JOIN nav_options o ON o.id = nh.nav_option_id "
                 "AND o.plan = 'Regular Plan' ") if regular_only else ""
    with get_conn() as conn:
        rows = conn.execute(_OPT_CTE + f"""
            SELECT
                nh.nav_date,
                nh.nav_value,
                LAG(nh.nav_value) OVER (
                    PARTITION BY nh.nav_option_id ORDER BY nh.nav_date
                ) AS prev_nav
            FROM nav_history nh
            JOIN opts ON nh.nav_option_id = opts.id
            {plan_join}
            ORDER BY nh.nav_date DESC
            LIMIT ?
        """, (isin, isin, days)).fetchall()

    history = []
    for nav_date, nav_value, prev_nav in rows:
        change = round(nav_value - prev_nav, 4) if prev_nav else None
        pct = round(change / prev_nav * 100, 2) if prev_nav else None
        history.append({
            "nav_date": str(nav_date),
            "nav_value": float(nav_value),
            "prev_nav": float(prev_nav) if prev_nav else None,
            "daily_change": change,
            "daily_pct": pct,
        })
    return list(reversed(history))


def get_nav_history(isin: str, days: int = 30) -> list[dict]:
    """NAV history (last N points) for an ISIN with daily change."""
    return _history(isin, days, regular_only=False)


def get_regular_plan_history(isin: str, days: int = 30) -> list[dict]:
    """NAV history restricted to Regular Plan."""
    return _history(isin, days, regular_only=True)


def get_scheme_info(scheme_code: int) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute("""
            SELECT scheme_code, scheme_name, fund_house, isin_growth
            FROM nav_schemes WHERE scheme_code = ?
        """, (scheme_code,)).fetchone()
    if row:
        return {"scheme_code": row[0], "scheme_name": row[1],
                "fund_house": row[2], "isin_growth": row[3]}
    return None


def get_scheme_options(scheme_code: int, plan: str = None) -> list[dict]:
    sql = ("SELECT id, plan, option_name, isin_payout, isin_reinvest "
           "FROM nav_options WHERE scheme_code = ?")
    args: list = [scheme_code]
    if plan:
        sql += " AND plan = ?"
        args.append(plan)
    sql += " ORDER BY plan, option_name"
    with get_conn() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [{"id": r[0], "plan": r[1], "option_name": r[2],
             "isin_payout": r[3], "isin_reinvest": r[4]} for r in rows]


def get_latest_nav_date() -> Optional[str]:
    with get_conn() as conn:
        row = conn.execute("SELECT MAX(nav_date) FROM nav_history").fetchone()
    return str(row[0]) if row and row[0] else None


def get_nav_count_by_date(nav_date: str) -> int:
    with get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) FROM nav_history WHERE nav_date = ?",
                           (nav_date,)).fetchone()
    return row[0] if row else 0


def get_fund_house_summary() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT s.fund_house,
                   COUNT(DISTINCT s.scheme_code) AS schemes,
                   COUNT(DISTINCT o.id) AS options,
                   COUNT(nh.nav_option_id) AS records
            FROM nav_schemes s
            LEFT JOIN nav_options o ON s.scheme_code = o.scheme_code
            LEFT JOIN nav_history nh ON o.id = nh.nav_option_id
            GROUP BY s.fund_house
            ORDER BY schemes DESC
        """).fetchall()
    return [{"fund_house": r[0], "schemes": r[1], "options": r[2], "records": r[3]}
            for r in rows]


def get_regular_plan_navs() -> dict[str, NavPoint]:
    """All latest NAVs restricted to Regular Plan (both ISINs)."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT o.isin_payout, o.isin_reinvest, nh.nav_value, nh.nav_date
            FROM nav_history nh
            JOIN nav_options o ON nh.nav_option_id = o.id
            WHERE o.plan = 'Regular Plan'
                AND nh.nav_date = (SELECT MAX(nav_date) FROM nav_history)
        """).fetchall()
    return _points_from_rows(rows)


# ══════════════════════════════════════════════════════════════
# DB INGESTION
# ══════════════════════════════════════════════════════════════
_PLAN_DIRECT_RE = re.compile(r"\bdirect\b", re.I)
_AMC_SUFFIX_RE = re.compile(r"\s+mutual fund$", re.I)


def _derive_plan_option(scheme_name: str) -> tuple[str, str]:
    """
    Plan/option derived from the scheme name (every layout is normalized to
    the 6-col form, which carries no plan/option columns).
    """
    n = scheme_name.lower()
    plan = "Direct Plan" if _PLAN_DIRECT_RE.search(scheme_name) else "Regular Plan"
    if "growth" in n:
        option = "Growth"
    elif any(k in n for k in ("idcw", "dividend", "payout", "reinvest")):
        option = "IDCW"
    elif "bonus" in n:
        option = "Bonus"
    else:
        option = "Other"
    return plan, option


def _parse_nav_file(filepath: str) -> list[dict]:
    """
    Parse an AMFI text file (any layout) into DB-ready rows, filtered to the
    plans in NAV_INGEST_PLANS (default: Regular Plan). Plan is derived per
    row from the scheme name.
    """
    try:
        with open(filepath, "r", encoding="utf-8-sig", errors="replace") as f:
            text = f.read()
    except IOError as e:
        log.error("[NAV-DB] Cannot read file %s: %s", filepath, e)
        raise

    text = _normalize_history_text_to_live_format(text)
    rows = []
    for r in _iter_rows(text):
        try:
            code = int(r["scheme_code"])
        except ValueError:
            continue
        plan, option = _derive_plan_option(r["scheme_name"])
        if not _INGEST_ALL and plan.lower() not in _INGEST_PLANS:
            continue
        rows.append({
            "scheme_code": code,
            "isin_payout": r["isin_1"],
            "isin_reinvest": r["isin_2"],
            "scheme_name": r["scheme_name"],
            "fund_house": _AMC_SUFFIX_RE.sub("", r["amc"]).strip() or "Unknown",
            "plan": plan,
            "option": option,
            "nav_value": r["nav"],
            "nav_date": r["nav_date"],          # ISO string
        })
    return rows


_HISTORY_UPSERT = (
    "INSERT INTO nav_history (nav_option_id, nav_value, nav_date) VALUES (?, ?, ?) "
    "ON CONFLICT(nav_option_id, nav_date) DO UPDATE SET nav_value = excluded.nav_value")
_HISTORY_REPLACE = (
    "INSERT OR REPLACE INTO nav_history (nav_option_id, nav_value, nav_date) "
    "VALUES (?, ?, ?)")


def _insert_nav_rows(rows: list[dict]) -> dict:
    """
    Batched insert into the 3-table schema in ONE transaction:
    preload existing schemes/options, insert only what's missing, update
    changed ISINs, bulk-upsert history.
    """
    if not rows:
        return {"inserted": 0, "skipped": 0, "nav_date": None, "fund_houses": set()}

    ensure_db_indexes()
    fund_houses = {r["fund_house"] for r in rows}
    nav_date = max(r["nav_date"] for r in rows)
    skipped = 0

    with get_conn() as conn:
        try:
            cur = conn.cursor()

            # schemes
            existing_schemes = {r[0] for r in cur.execute("SELECT scheme_code FROM nav_schemes")}
            new_schemes = {}
            for r in rows:
                if r["scheme_code"] not in existing_schemes:
                    new_schemes.setdefault(
                        r["scheme_code"],
                        (r["scheme_code"], r["scheme_name"], r["fund_house"]))
            if new_schemes:
                cur.executemany(
                    "INSERT OR IGNORE INTO nav_schemes (scheme_code, scheme_name, fund_house) "
                    "VALUES (?, ?, ?)", list(new_schemes.values()))

            # options
            opt_ids: dict[tuple, int] = {}
            opt_isins: dict[int, tuple] = {}
            for id_, code, plan, opt, p, rv in cur.execute(
                    "SELECT id, scheme_code, plan, option_name, isin_payout, isin_reinvest "
                    "FROM nav_options"):
                opt_ids[(code, plan, opt)] = id_
                opt_isins[id_] = (p, rv)

            isin_updates = []
            seen_opts = set()
            for r in rows:
                key = (r["scheme_code"], r["plan"], r["option"])
                if key in seen_opts:
                    continue
                seen_opts.add(key)
                oid = opt_ids.get(key)
                if oid is None:
                    try:
                        cur.execute(
                            "INSERT INTO nav_options "
                            "(scheme_code, plan, option_name, isin_payout, isin_reinvest) "
                            "VALUES (?, ?, ?, ?, ?)",
                            (r["scheme_code"], r["plan"], r["option"],
                             r["isin_payout"], r["isin_reinvest"]))
                        opt_ids[key] = cur.lastrowid
                    except Exception as e:
                        log.warning("[NAV-DB] Option insert skipped (code %s): %s",
                                    r["scheme_code"], e)
                else:
                    old_p, old_r = opt_isins[oid]
                    new_p = r["isin_payout"] or old_p
                    new_r = r["isin_reinvest"] or old_r
                    if (new_p, new_r) != (old_p, old_r):
                        isin_updates.append((new_p, new_r, oid))
            if isin_updates:
                cur.executemany(
                    "UPDATE nav_options SET isin_payout = ?, isin_reinvest = ? WHERE id = ?",
                    isin_updates)

            # history (dedupe on option+date; last row wins)
            hist: dict[tuple, float] = {}
            for r in rows:
                oid = opt_ids.get((r["scheme_code"], r["plan"], r["option"]))
                if oid is None:
                    skipped += 1
                    continue
                k = (oid, r["nav_date"])
                if k in hist:
                    skipped += 1
                hist[k] = r["nav_value"]

            cur.executemany(
                _HISTORY_UPSERT if _UNIQUE_OK else _HISTORY_REPLACE,
                [(oid, v, d) for (oid, d), v in hist.items()])
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    return {"inserted": len(hist), "skipped": skipped,
            "nav_date": nav_date, "fund_houses": fund_houses}


def ingest_nav_file_to_db(filepath: str) -> dict:
    """Parse an AMFI NAV text file (any layout) and insert rows (see NAV_INGEST_PLANS)."""
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


def ingest_nav_bytes_to_db(data: bytes) -> dict:
    """
    Ingest in-memory NAV content (e.g. Streamlit UploadedFile.getvalue()).
    Same return shape as ingest_nav_file_to_db().
    """
    import tempfile
    with tempfile.NamedTemporaryFile(delete=False, suffix=".txt", mode="wb") as tmp:
        tmp.write(data)
        tmp_path = tmp.name
    try:
        return ingest_nav_file_to_db(tmp_path)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def cleanup_unknown_plan_rows(dry_run: bool = True) -> dict:
    """
    One-off cleanup of rows ingested with plan='Unknown' by the old
    Regular-Plan-filter bug (it let every plan through). dry_run=True only
    counts. After deleting, re-ingest snapshots to rebuild proper rows.
    """
    with get_conn() as conn:
        opt_ids = [r[0] for r in conn.execute(
            "SELECT id FROM nav_options WHERE plan = 'Unknown'").fetchall()]
        hist_count = 0
        if opt_ids:
            hist_count = conn.execute(
                "SELECT COUNT(*) FROM nav_history WHERE nav_option_id IN "
                "(SELECT id FROM nav_options WHERE plan = 'Unknown')").fetchone()[0]
        if not dry_run and opt_ids:
            conn.execute("DELETE FROM nav_history WHERE nav_option_id IN "
                         "(SELECT id FROM nav_options WHERE plan = 'Unknown')")
            conn.execute("DELETE FROM nav_options WHERE plan = 'Unknown'")
            conn.commit()
    log.info("[NAV-DB] cleanup_unknown_plan_rows(dry_run=%s): %d options, %d history rows",
             dry_run, len(opt_ids), hist_count)
    return {"dry_run": dry_run, "options": len(opt_ids), "history_rows": hist_count}


# ══════════════════════════════════════════════════════════════
# DIAGNOSTICS
# ══════════════════════════════════════════════════════════════
def diagnose_previous_day_coverage(current_iso: str, previous_iso: str) -> dict:
    """Row counts and ISIN overlap between two snapshots (Admin Panel)."""
    idx = _snapshot_index()
    today_path = idx.get(current_iso) or _snapshot_path(current_iso)
    prev_path = idx.get(previous_iso) or _snapshot_path(previous_iso)

    today_exists = os.path.exists(today_path)
    prev_exists = os.path.exists(prev_path)

    today_count = _row_count(today_path) if today_exists else 0
    prev_count = _row_count(prev_path) if prev_exists else 0

    today_isins = set(_read_snapshot_map(today_path)) if today_exists else set()
    prev_isins = set(_read_snapshot_map(prev_path)) if prev_exists else set()
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
        "prev_is_partial": 0 < prev_count < _MIN_ACCEPTABLE_ISIN_COUNT,
        "today_isins": len(today_isins),
        "prev_isins": len(prev_isins),
        "overlap": len(overlap),
        "missing_from_prev": len(missing),
        "missing_isins_sample": sorted(missing)[:10],
        "has_db_fallback": bool(get_nav_for_date_from_db(previous_iso)),
    }


# ══════════════════════════════════════════════════════════════
# FOLIO + NAV BATCH LOADERS  (append to end of nav_engine.py)
# ══════════════════════════════════════════════════════════════
# Combine the AMFI in-memory index with CAMS/KFin folio master tables
# and the BSE scheme master dedup to produce the enriched folio+NAV
# DataFrame that the dashboard, client tab, and reports all consume.
#
# No Streamlit imports. @st.cache_data wrappers live in main.py.
#
# These read from the saved NAV file (not live AMFI). The standalone
# background_worker.py is responsible for keeping the snapshot fresh.

def get_all_folios_with_isin_and_nav(force_reload: bool = False) -> pd.DataFrame:
    """
    Master batch function — reads NAV/AMC from the saved file (NOT live AMFI):
      1. Load AMFI NAV + AMC index from disk (in-memory cached per process)
      2. Build deduplicated BSE scheme master subquery (via bse_scheme)
      3. Query CAMS folio master JOIN deduplicated bse_scheme_master
      4. Query KFin folio master JOIN deduplicated bse_scheme_master + units
      5. Map NAV + AMC name (canonical, from saved file) in-memory by ISIN
      6. Return combined DataFrame

    force_reload=True re-reads the saved file from disk (not the network).

    No @st.cache_data here — the wrapper in main.py handles caching.
    """
    log.info("=" * 60)
    log.info("[NAV-FLOW] Starting get_all_folios_with_isin_and_nav()")

    # ── Step 1: Load NAV + AMC index from saved file ──
    log.info("[NAV-FLOW][Step 1] Loading AMFI NAV + AMC index from saved file (force_reload=%s)...",
             force_reload)
    nav_map = _amfi.load(force=force_reload)
    log.info("[NAV-FLOW][Step 1] Index ready: %s ISINs (NAV), %s ISINs (AMC)",
             len(nav_map), len(_amfi._state.amc))
    if not nav_map:
        log.error("[NAV-FLOW][Step 1] nav_map is EMPTY — no saved NAV file found or it's unreadable. "
                  "Every row downstream will have current_nav=None, so total AUM will be 0. "
                  "Fix: call download_and_save_nav_if_needed() at startup, or use the admin "
                  "'Redownload NAV' button. Checked dir: %s", os.path.abspath(NAV_TEXT_DIR))

    # ── Step 2: Deduplicated BSE Scheme Master subquery ──
    log.info("[NAV-FLOW][Step 2] Building deduplicated BSE lookup...")
    bse_dedup = bse_scheme.build_dedup_subquery(
        include_scheme_name=True, include_amc=True,
    )

    # ── Step 3: CAMS SQL ──
    cams_sql = f"""
    SELECT
        f.foliochk          AS folio_id,
        f.product           AS product_code,
        f.inv_name          AS investor_name,
        f.rupee_bal         AS file_aum,
        f.clos_bal          AS units,
        bsm.ISIN            AS isin,
        bsm.Scheme_Name     AS scheme_name,
        bsm.bse_amc_name    AS bse_amc_name,
        'CAMS'              AS rta
    FROM cams_wbr9_folio f
    LEFT JOIN ({bse_dedup}) bsm
        ON UPPER(TRIM(f.product)) = bsm.cp_code
    WHERE f.product IS NOT NULL AND TRIM(f.product) != ''
    """

    # ── Step 4: KFin SQL ──
    kfin_sql = f"""
    SELECT
        f.folio             AS folio_id,
        f.product_code      AS product_code,
        f.investor_name     AS investor_name,
        NULL                AS file_aum,
        u.total_units       AS units,
        bsm.ISIN            AS isin,
        bsm.Scheme_Name     AS scheme_name,
        bsm.bse_amc_name    AS bse_amc_name,
        'KFinTech'          AS rta
    FROM kfin_mfsd211_folio f
    INNER JOIN (
        SELECT
            td_acno AS folio_id,
            fmcode  AS product_code,
            SUM(td_units) AS total_units
        FROM v_kfin_txn_active
        WHERE td_units IS NOT NULL
        GROUP BY td_acno, fmcode
        HAVING total_units != 0
    ) u
        ON f.folio = u.folio_id
        AND UPPER(TRIM(f.product_code)) = UPPER(TRIM(u.product_code))
    LEFT JOIN ({bse_dedup}) bsm
        ON UPPER(TRIM(f.product_code)) = bsm.cp_code
    WHERE f.product_code IS NOT NULL AND TRIM(f.product_code) != ''
    """

    # ── Step 5: Execute Queries ──
    log.info("[NAV-FLOW][Step 5] Executing SQL queries...")
    with get_conn() as conn:
        cams_df = pd.read_sql(cams_sql, conn)
        log.info("[NAV-FLOW][Step 5] CAMS rows fetched: %s", len(cams_df))
        kfin_df = pd.read_sql(kfin_sql, conn)
        log.info("[NAV-FLOW][Step 5] KFin rows fetched: %s", len(kfin_df))

    # ── Step 6: Combine ──
    combined = pd.concat([cams_df, kfin_df], ignore_index=True)
    log.info("[NAV-FLOW][Step 6] Combined rows: %s", len(combined))

    # ── Step 7: NAV + AMC Lookup ──
    log.info("[NAV-FLOW][Step 7] Mapping ISIN → NAV and ISIN → AMC name...")

    def _lookup_nav(isin):
        if pd.isna(isin) or not str(isin).strip():
            return pd.Series([None, None])
        hit = nav_map.get(str(isin).strip().upper())
        return pd.Series(hit) if hit else pd.Series([None, None])

    nav_cols = combined["isin"].apply(_lookup_nav)
    combined["current_nav"] = nav_cols[0]
    combined["nav_date"] = nav_cols[1]

    combined["amfi_amc_name"] = combined["isin"].apply(
        lambda i: _amfi.get_amc(i) if pd.notna(i) and str(i).strip() else None
    )

    combined["amc_name"] = combined["amfi_amc_name"].fillna(combined["bse_amc_name"])
    combined["amc_name_source"] = combined["amfi_amc_name"].apply(
        lambda x: "AMFI" if pd.notna(x) and str(x).strip() else None
    )
    combined.loc[combined["amc_name_source"].isna() & combined["bse_amc_name"].notna(),
    "amc_name_source"] = "BSE (unresolved)"
    combined["amc_name_source"] = combined["amc_name_source"].fillna("Unknown")

    total_with_isin = combined["isin"].notna().sum()
    total_with_nav = combined["current_nav"].notna().sum()
    total_with_amc = combined["amc_name"].notna().sum()
    total_amfi_amc = (combined["amc_name_source"] == "AMFI").sum()
    log.info("[NAV-FLOW][Step 7] ISIN→NAV: %s with ISIN, %s with NAV (%s%% coverage)",
             total_with_isin, total_with_nav,
             round(total_with_nav / total_with_isin * 100, 2) if total_with_isin else 0)
    log.info("[NAV-FLOW][Step 7] AMC name resolved: %s/%s rows (%s via AMFI-canonical, %s via BSE fallback)",
             total_with_amc, len(combined), total_amfi_amc, total_with_amc - total_amfi_amc)

    # ── Step 8: Calculate AUM ──
    combined["nav_based_aum"] = combined.apply(
        lambda r: r["units"] * r["current_nav"]
        if pd.notna(r.get("units")) and pd.notna(r["current_nav"])
        else None,
        axis=1
    )
    total_aum = combined["nav_based_aum"].sum()
    log.info("[NAV-FLOW][Step 8] Total NAV-based AUM: ₹%s",
             f"{total_aum:,.2f}" if pd.notna(total_aum) else "N/A")

    # ── Step 9: Final Flags & Reorder ──
    combined["has_isin"] = combined["isin"].notna()
    combined["has_nav"] = combined["current_nav"].notna()
    combined["has_amc"] = combined["amc_name"].notna()

    front = [
        "rta", "folio_id", "investor_name", "product_code",
        "scheme_name", "isin", "has_isin",
        "amc_name", "amc_name_source", "has_amc",
        "current_nav", "nav_date", "has_nav",
        "units", "file_aum", "nav_based_aum"
    ]
    front = [c for c in front if c in combined.columns]
    back = [c for c in combined.columns if c not in front]
    result = combined[front + back]

    log.info("[NAV-FLOW] Complete. Returning %s rows x %s cols",
             len(result), len(result.columns))
    log.info("=" * 60)
    return result


def get_folio_nav_summary(force_reload: bool = False) -> dict:
    """Quick stats for Streamlit metrics. Calls get_all_folios_with_isin_and_nav."""
    log.info("[NAV-FLOW] Generating folio NAV summary...")

    df = get_all_folios_with_isin_and_nav(force_reload=force_reload)
    cams_df = df[df["rta"] == "CAMS"]
    kfin_df = df[df["rta"] == "KFinTech"]

    cams_nav_aum = float(cams_df["nav_based_aum"].sum()) if "nav_based_aum" in cams_df.columns else 0.0
    cams_file_aum = float(cams_df["file_aum"].sum()) if "file_aum" in cams_df.columns else 0.0
    kfin_nav_aum = float(kfin_df["nav_based_aum"].sum()) if "nav_based_aum" in kfin_df.columns else 0.0

    cams_unmatched = int((cams_df["has_isin"] & ~cams_df["has_nav"]).sum())
    kfin_unmatched = int((kfin_df["has_isin"] & ~kfin_df["has_nav"]).sum())

    return {
        "total_folios": len(df),
        "cams_folios": len(cams_df),
        "kfin_folios": len(kfin_df),
        "with_isin": int(df["has_isin"].sum()),
        "isin_coverage_pct": round(df["has_isin"].mean() * 100, 2) if len(df) else 0,
        "with_nav": int(df["has_nav"].sum()),
        "nav_coverage_pct": round(df["has_nav"].mean() * 100, 2) if len(df) else 0,
        "with_amc": int(df["has_amc"].sum()),
        "amc_coverage_pct": round(df["has_amc"].mean() * 100, 2) if len(df) else 0,
        "amc_resolved_via_amfi": int((df["amc_name_source"] == "AMFI").sum()),

        "total_aum": cams_nav_aum + kfin_nav_aum,
        "cams_aum": cams_nav_aum,
        "cams_file_aum": cams_file_aum,
        "cams_unmatched_nav": cams_unmatched,
        "kfin_aum": kfin_nav_aum,
        "kfin_unmatched_nav": kfin_unmatched,

        "cams_with_nav": int(cams_df["has_nav"].sum()),
        "cams_total": len(cams_df),
        "kfin_with_nav": int(kfin_df["has_nav"].sum()),
        "kfin_total": len(kfin_df),

        "df": df,
    }


def load_amc_breakdown_by_isin() -> pd.DataFrame:
    """AMC-wise AUM + folio breakdown, grouped by canonical AMFI AMC name (via ISIN)."""
    df = get_all_folios_with_isin_and_nav()
    if df.empty:
        return pd.DataFrame()

    df = df.copy()
    df["amc_name"] = df["amc_name"].fillna("⚠️ Unresolved (no ISIN match)")

    grouped = (
        df.groupby(["amc_name", "rta"], dropna=False)
        .agg(
            folios=("folio_id", "nunique"),
            records=("folio_id", "count"),
            aum=("nav_based_aum", "sum"),
        )
        .reset_index()
        .sort_values("aum", ascending=False)
    )
    return grouped


def load_active_amcs() -> list:
    """AMCs you currently have business with (from folio holdings, not brokerage files)."""
    df = get_all_folios_with_isin_and_nav()
    if df.empty:
        return []
    return sorted(df["amc_name"].dropna().unique().tolist())
