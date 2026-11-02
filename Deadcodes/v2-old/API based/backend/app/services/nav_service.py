"""
nav_service.py
==============
AMFI NAV fetching, parsing, snapshot-file management, previous-day sync.
No Streamlit — state lives in admin_settings or on disk.
"""
from __future__ import annotations

import logging
import os
import time as _time
from datetime import datetime, timedelta, date as date_cls, time as time_cls
from typing import Optional

import pandas as pd
import requests
from sqlalchemy import text

from app.core.config import settings
from app.core.db import SessionLocal
from app.services.cache import cached
from app.services import nav_ingest
from app.services import sync_log as sflog

log = logging.getLogger(__name__)

AMFI_TEXT_URL = "https://portal.amfiindia.com/spages/NAVAll.txt"
NAV_HISTORY_URL = "https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx"
NAV_TEXT_DIR = os.environ.get("NAV_TEXT_DIR", settings.nav_text_dir)
LOOKBACK_DELAY_SECONDS = 1.5
NAV_REDOWNLOAD_TIMES = [time_cls(15, 0), time_cls(23, 0)]

_AMFI_SESSION = requests.Session()
_AMFI_SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/plain,text/html,application/xhtml+xml,*/*",
    "Referer": "https://www.amfiindia.com/",
})

_MIN_ACCEPTABLE_ISIN_COUNT = 3000


def _ensure_text_dir() -> None:
    os.makedirs(NAV_TEXT_DIR, exist_ok=True)


def _count_isins_in_file(path: str) -> int:
    try:
        with open(path, "r", encoding="utf-8") as f:
            n = 0
            for line in f:
                line = line.strip()
                if ";" in line and not line.startswith("Scheme Code"):
                    n += 1
            return n
    except Exception:
        return 0


def _read_snapshot_map(path: str) -> dict:
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


def _snapshot_path(date_str: str) -> str:
    return os.path.join(NAV_TEXT_DIR, f"nav_{date_str}.txt")


def _latest_snapshot_path() -> Optional[str]:
    _ensure_text_dir()
    files = sorted(f for f in os.listdir(NAV_TEXT_DIR)
                   if f.startswith("nav_") and f.endswith(".txt"))
    return os.path.join(NAV_TEXT_DIR, files[-1]) if files else None


def get_snapshot_status() -> dict:
    _ensure_text_dir()
    today = datetime.now().strftime("%Y-%m-%d")
    today_path = _snapshot_path(today)
    latest = _latest_snapshot_path()
    out = {"has_today": os.path.exists(today_path),
           "today_path": today_path if os.path.exists(today_path) else None,
           "latest_path": latest, "latest_date": None, "latest_bytes": None}
    if latest:
        out["latest_date"] = os.path.basename(latest).replace("nav_", "").replace(".txt", "")
        out["latest_bytes"] = os.path.getsize(latest)
    return out


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
    log.error("[NAV-DATE-EXTRACT] Could not extract NAV date")
    return None


def _parse_nav_text(text: str) -> tuple[dict, dict, list[dict]]:
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

        scheme_code, isin_1, isin_2, scheme_name = parts[0], parts[1], parts[2], parts[3]
        nav_str, date_str = parts[-2], parts[-1]

        try:
            nav_date = datetime.strptime(date_str, "%d-%b-%Y").strftime("%Y-%m-%d")
        except ValueError:
            continue
        try:
            nav = float(nav_str) if nav_str not in ("N.A.", "") else 0.0
        except ValueError:
            nav = 0.0
        if nav <= 0:
            continue

        format_samples[len(parts)] = format_samples.get(len(parts), 0) + 1

        ip = isin_1 if isin_1 and isin_1 != "-" else None
        ir = isin_2 if isin_2 and isin_2 != "-" else None
        primary = ip or ir
        if primary:
            records.append({
                "isin": primary.upper(), "scheme_code": scheme_code,
                "isin_payout": ir.upper() if ir else None,
                "scheme_name": scheme_name, "amc_name": current_amc or None,
                "category": current_category or None,
                "nav": nav, "nav_date": nav_date,
            })
        for isin in (isin_1, isin_2):
            if isin and isin != "-":
                nav_map[isin.upper()] = (nav, nav_date)
                if current_amc:
                    amc_map[isin.upper()] = current_amc

    log.info("[NAV-PARSE] Parsed %d records | columns seen: %s",
             len(records), dict(sorted(format_samples.items())))
    return nav_map, amc_map, records


def download_and_save_nav(timeout: int = 30) -> dict:
    log.info("[AMFI] Downloading NAV file from %s", AMFI_TEXT_URL)
    try:
        res = requests.get(AMFI_TEXT_URL, timeout=timeout)
        res.raise_for_status()
    except requests.RequestException as e:
        log.error("[AMFI] Download failed: %s", e)
        return {"path": None, "bytes": 0, "date": None}

    text = res.text
    if not text or not text.strip():
        log.error("[AMFI] Downloaded file empty")
        return {"path": None, "bytes": 0, "date": None}

    _ensure_text_dir()
    actual = _extract_nav_date_from_text(text)
    if not actual:
        log.error("[AMFI] Could not extract NAV date")
        return {"path": None, "bytes": 0, "date": None}

    saved = _save_nav_snapshot(text, actual, allow_overwrite=True, source="live")
    size = os.path.getsize(saved["path"])
    log.info("[AMFI] NAV file saved: %s (%s bytes) | NAV Date: %s",
             saved["path"], size, actual)
    return {"path": saved["path"], "bytes": size, "date": actual}


def _last_passed_cutoff_today(now: datetime) -> Optional[time_cls]:
    passed = [t for t in NAV_REDOWNLOAD_TIMES if now.time() >= t]
    return max(passed) if passed else None


def download_and_save_nav_if_needed(force: bool = False) -> dict:
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    today_path = _snapshot_path(today)

    if not force and os.path.exists(today_path):
        cutoff = _last_passed_cutoff_today(now)
        if cutoff is None:
            return {"ran": False, "ok": True,
                    "reason": "before first cutoff", "bytes": os.path.getsize(today_path)}
        file_mtime = datetime.fromtimestamp(os.path.getmtime(today_path))
        cutoff_dt = datetime.combine(now.date(), cutoff)
        if file_mtime >= cutoff_dt:
            return {"ran": False, "ok": True,
                    "reason": f"already fresh past {cutoff}",
                    "bytes": os.path.getsize(today_path)}
        log.info("[AMFI] File predates %s cutoff — redownloading", cutoff)

    try:
        result = download_and_save_nav()
        if result.get("date") is None:
            return {"ran": True, "ok": False,
                    "reason": "NAV date extraction failed", "bytes": None}

        if os.path.exists(result["path"]):
            log.info("[NAV-DB] Ingesting %s", result["path"])
            try:
                ing = nav_ingest.ingest_nav_file_to_db(result["path"])
                if ing.get("ok"):
                    log.info("[NAV-DB] Success: %s", ing.get("reason"))
                else:
                    log.error("[NAV-DB] Failed: %s", ing.get("reason"))
                    sflog.record_failure(source="nav", stage="db_insert",
                                         file=result["path"],
                                         msg=f"NAV ingestion failed: {ing.get('reason')}")
            except Exception as e:
                log.exception("[NAV-DB] Ingestion error")
                sflog.record_failure(source="nav", stage="db_insert",
                                     file=result["path"],
                                     msg=f"NAV DB ingestion exception: {e}")

        return {"ran": True, "ok": True,
                "reason": "downloaded and ingested", "bytes": result.get("bytes")}
    except Exception as e:
        log.exception("[AMFI] Download failed")
        return {"ran": True, "ok": False, "reason": f"download failed: {e}", "bytes": None}


def get_last_business_day(from_date: date_cls | None = None) -> date_cls:
    from_date = from_date or datetime.now().date()
    c = from_date - timedelta(days=1)

    try:
        from app.services.trading_calendar import is_trading_day
    except Exception:
        is_trading_day = None

    if is_trading_day is not None:
        for _ in range(60):
            if is_trading_day(c):
                return c
            c -= timedelta(days=1)
        c = from_date - timedelta(days=1)

    while c.weekday() >= 5:
        c -= timedelta(days=1)
    return c


def _fmt_amfi_date(d) -> str:
    return d.strftime("%d-%b-%Y")


def _get_file_nav_date(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if ";" not in line:
                    continue
                parts = line.split(";")
                if len(parts) < 6 or parts[0].strip() == "Scheme Code":
                    continue
                try:
                    return datetime.strptime(parts[-1].strip(), "%d-%b-%Y").strftime("%Y-%m-%d")
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
    low = text.lower()
    if "<html" in low or "captcha" in low:
        return False
    return "scheme code" in low and ";" in text


def _normalize_history_text_to_live_format(text: str) -> str:
    out = []
    for line in text.splitlines():
        s = line.strip()
        if ";" not in s:
            out.append(line)
            continue
        parts = s.split(";")
        if len(parts) < 8 or parts[0].strip() == "Scheme Code":
            if parts and parts[0].strip() == "Scheme Code":
                out.append("Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;"
                           "Scheme Name;Net Asset Value;Date")
            else:
                out.append(line)
            continue
        code, name, _plan, _opt, ip, ir, nav, dt = parts[:8]
        out.append(f"{code};{ip};{ir};{name};{nav};{dt}")
    return "\n".join(out)


def download_business_day_nav(target_date, timeout: int = 30) -> dict:
    date_str = _fmt_amfi_date(target_date)
    log.info("[AMFI-HIST] Requesting %s", date_str)
    res = _AMFI_SESSION.get(NAV_HISTORY_URL,
                            params={"tp": 1, "frmdt": date_str, "todt": date_str},
                            timeout=timeout)
    log.info("[AMFI-HIST] URL: %s", res.url)
    res.raise_for_status()
    text = res.text

    if not _looks_like_amfi_format(text):
        snippet = text.strip()[:200].replace("\n", " ")
        raise ValueError(f"blocked_or_invalid_response for {date_str}: {snippet!r}")

    text = _normalize_history_text_to_live_format(text)
    actual = _extract_nav_date_from_text(text)
    if actual is None:
        snippet = text.strip()[:200].replace("\n", " ")
        raise ValueError(f"no_date_in_response for {date_str}: {snippet!r}")

    log.info("[AMFI-HIST] Requested %s, actual %s", date_str, actual)
    return {"text": text,
            "requested_date": target_date.strftime("%Y-%m-%d"),
            "actual_date": actual}


def _save_nav_snapshot(text: str, iso_date: str, *,
                       allow_overwrite: bool = True,
                       source: str = "unknown") -> dict:
    _ensure_text_dir()
    path = _snapshot_path(iso_date)
    new_map, _, _ = _parse_nav_text(text)
    new_count = len(new_map)

    if allow_overwrite and os.path.exists(path):
        existing = _count_isins_in_file(path)
        if existing > new_count:
            log.warning("[NAV-SAVE] Refusing to overwrite richer %s "
                        "(existing=%d, incoming=%d, source=%s)",
                        path, existing, new_count, source)
            return {"path": path, "bytes": os.path.getsize(path),
                    "date": iso_date, "record_count": existing,
                    "kept_existing": True}

    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    size = os.path.getsize(path)
    log.info("[NAV-SAVE] Saved %s: %s bytes, %d rows (source=%s)",
             path, size, new_count, source)
    return {"path": path, "bytes": size, "date": iso_date,
            "record_count": new_count, "kept_existing": False}


def sync_previous_business_day_nav(force: bool = False, timeout: int = 30,
                                   max_lookback: int = 10) -> dict:
    latest = _latest_snapshot_path()
    if latest is None:
        return {"ran": False, "ok": False, "reason": "no live snapshot yet"}

    latest_iso = _get_file_nav_date(latest)
    if not latest_iso:
        return {"ran": False, "ok": False, "reason": "could not read NAV date"}

    target = get_last_business_day(datetime.strptime(latest_iso, "%Y-%m-%d").date())

    for i in range(max_lookback):
        iso = target.strftime("%Y-%m-%d")

        if not force and _have_snapshot_for_date(iso):
            log.info("[AMFI-HIST] Already have %s — skipping", iso)
            return {"ran": False, "ok": True,
                    "reason": f"already have {iso}", "date": iso}

        if i > 0:
            _time.sleep(LOOKBACK_DELAY_SECONDS)

        try:
            result = download_business_day_nav(target, timeout=timeout)
        except ValueError as e:
            sflog.record_failure(source="nav", stage="fetch", msg=str(e),
                                 context={"requested_date": iso})
            return {"ran": True, "ok": False, "reason": str(e), "date": iso}
        except requests.RequestException:
            log.exception("[AMFI-HIST] Network error for %s", iso)
            sflog.record_failure(source="nav", stage="fetch", msg="Network error",
                                 context={"requested_date": iso})
            return {"ran": True, "ok": False, "reason": "network error", "date": iso}

        actual = result["actual_date"]
        if actual == iso:
            saved = _save_nav_snapshot(result["text"], actual)
            try:
                nav_ingest.ingest_nav_file_to_db(saved["path"])
            except Exception as e:
                sflog.record_failure(source="nav", stage="db_insert",
                                     file=saved["path"], msg=f"Ingest: {e}")
            return {"ran": True, "ok": True, "reason": "downloaded", **saved}

        log.info("[AMFI-HIST] Holiday/weekend: requested %s, got %s", iso, actual)
        if not force and _have_snapshot_for_date(actual):
            return {"ran": False, "ok": True,
                    "reason": f"already have {actual}", "date": actual}
        saved = _save_nav_snapshot(result["text"], actual)
        try:
            nav_ingest.ingest_nav_file_to_db(saved["path"])
        except Exception as e:
            sflog.record_failure(source="nav", stage="db_insert",
                                 file=saved["path"], msg=f"Ingest: {e}")
        return {"ran": True, "ok": True,
                "reason": f"requested {iso}, saved actual {actual}", **saved}

    return {"ran": True, "ok": False,
            "reason": f"no business day within {max_lookback} days"}


def _prev_nav_done_today() -> str | None:
    with SessionLocal() as s:
        row = s.execute(text(
            "SELECT value FROM admin_settings WHERE key='prev_nav_done_for'"
        )).fetchone()
    return row[0] if row else None


def _mark_prev_nav_done(iso_date: str) -> None:
    with SessionLocal() as s:
        s.execute(text("""
            INSERT INTO admin_settings(key, value, updated_at)
            VALUES ('prev_nav_done_for', :v, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                                          updated_at=CURRENT_TIMESTAMP
        """), {"v": iso_date})
        s.commit()


def sync_previous_business_day_nav_if_needed(force: bool = False) -> dict:
    today = datetime.now().strftime("%Y-%m-%d")
    if not force and _prev_nav_done_today() == today:
        return {"ran": False, "ok": True, "reason": "already synced today"}

    result = sync_previous_business_day_nav(force=force)
    if result.get("ok"):
        _mark_prev_nav_done(today)
    else:
        log.warning("[AMFI-HIST] Prev-day sync failed (%s) — will retry",
                    result.get("reason"))
    return result


def _previous_snapshot_path(current_nav_date: Optional[str] = None):
    _ensure_text_dir()
    files = sorted(f for f in os.listdir(NAV_TEXT_DIR)
                   if f.startswith("nav_") and f.endswith(".txt"))
    if not files:
        return None, None

    pairs: list[tuple[str, str]] = []
    for fname in files:
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


@cached
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
    flat = {}
    for isin, val in nav_map.items():
        try:
            flat[isin] = float(val[0]) if isinstance(val, (tuple, list)) else float(val)
        except (TypeError, ValueError, IndexError):
            continue
    return flat


def get_previous_nav_date() -> Optional[str]:
    _, date_str = _previous_snapshot_path()
    return date_str


def _resolve_prev_nav_for_folios_impl(
    current_date: date_cls,
    our_isins: set,
    max_lookback: int = 10,
    min_overlap_pct: float = 0.5,
) -> tuple[Optional[str], dict, dict]:
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
        candidates.append({"date": iso, "rows": len(nav_map),
                           "folio_overlap": overlap, "needed": needed})
        log.info("[NAV-SKIPBACK] %s → %d rows, %d/%d folio ISINs (need %d)",
                 iso, len(nav_map), overlap, len(our_isins), needed)

        if overlap >= needed:
            log.info("[NAV-SKIPBACK] Using %s (%d/%d)", iso, overlap, len(our_isins))
            return iso, nav_map, {"tried": tried, "chosen": iso,
                                  "overlap": overlap, "needed": needed,
                                  "candidates": candidates}

    log.warning("[NAV-SKIPBACK] No date covered ≥ %d ISINs. Tried: %s",
                needed, tried)
    return None, {}, {"tried": tried, "chosen": None, "overlap": 0,
                      "needed": needed, "candidates": candidates}


@cached
def resolve_prev_nav_for_folios(current_date_iso: str, isins_tuple: tuple,
                                max_lookback: int = 10,
                                min_overlap_pct: float = 0.5) -> tuple:
    if isinstance(current_date_iso, str):
        current_date = datetime.strptime(current_date_iso, "%Y-%m-%d").date()
    else:
        current_date = (current_date_iso.date() if hasattr(current_date_iso, "date")
                        else current_date_iso)
    return _resolve_prev_nav_for_folios_impl(
        current_date, set(isins_tuple),
        max_lookback=max_lookback, min_overlap_pct=min_overlap_pct,
    )


def get_nav_for_date_from_db(target_iso: str) -> dict:
    try:
        with SessionLocal() as s:
            rows = s.execute(
                text("""
                    SELECT nopt.isin_payout, nopt.isin_reinvest, nh.nav_value
                    FROM nav_history nh
                    JOIN nav_options nopt ON nh.nav_option_id = nopt.id
                    WHERE nh.nav_date = :d
                """),
                {"d": target_iso},
            ).fetchall()
    except Exception as e:
        log.exception("[NAV-DB-LOOKUP] Query failed for %s: %s", target_iso, e)
        return {}

    out: dict[str, float] = {}
    for ip, ir, val in rows:
        try:
            v = float(val)
            if v <= 0:
                continue
        except (TypeError, ValueError):
            continue
        for isin in (ip, ir):
            if isin and str(isin).strip() and str(isin) != "-":
                out[str(isin).strip().upper()] = v
    log.info("[NAV-DB-LOOKUP] %s → %d ISINs", target_iso, len(out))
    return out


def get_or_fetch_nav_for_date(target_iso: str) -> dict:
    candidates: list[tuple[str, dict]] = []

    if _have_snapshot_for_date(target_iso):
        try:
            disk_map = _read_snapshot_map(_snapshot_path(target_iso))
            if disk_map:
                candidates.append(("disk", disk_map))
                if len(disk_map) >= _MIN_ACCEPTABLE_ISIN_COUNT:
                    log.info("[NAV-LOOKUP] %s → %d ISINs (disk)",
                             target_iso, len(disk_map))
                    return disk_map
                log.warning("[NAV-LOOKUP] Disk partial (%d < %d) — trying history",
                            len(disk_map), _MIN_ACCEPTABLE_ISIN_COUNT)
        except Exception:
            log.exception("[NAV-LOOKUP] Read snapshot failed for %s", target_iso)

    try:
        target_d = datetime.strptime(target_iso, "%Y-%m-%d").date()
        resp = download_business_day_nav(target_d, timeout=30)
        actual = resp.get("actual_date")
        if actual and resp.get("text"):
            saved = _save_nav_snapshot(resp["text"], actual,
                                       allow_overwrite=True, source="history")
            try:
                nav_ingest.ingest_nav_file_to_db(saved["path"])
            except Exception as e:
                log.exception("[NAV-DB] Ingest error: %s", e)
            hist_map = _read_snapshot_map(_snapshot_path(actual))
            if hist_map:
                candidates.append(("history", hist_map))
                if len(hist_map) >= _MIN_ACCEPTABLE_ISIN_COUNT:
                    log.info("[NAV-LOOKUP] %s → %d ISINs (history)",
                             target_iso, len(hist_map))
                    return hist_map
                log.warning("[NAV-LOOKUP] History partial (%d) — trying DB",
                            len(hist_map))
    except Exception as e:
        log.warning("[NAV-LOOKUP] History fetch failed for %s: %s", target_iso, e)

    db_map = get_nav_for_date_from_db(target_iso)
    if db_map:
        log.info("[NAV-LOOKUP] %s → %d ISINs (DB, authoritative)",
                 target_iso, len(db_map))
        return db_map

    if candidates:
        src, best = max(candidates, key=lambda c: len(c[1]))
        log.warning("[NAV-LOOKUP] %s — no source met threshold; "
                    "returning best partial (%s, %d)", target_iso, src, len(best))
        return best
    log.error("[NAV-LOOKUP] %s — no data anywhere", target_iso)
    return {}


class AMFINavIndex:
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
            return self._nav_by_isin
        path = _latest_snapshot_path()
        if path is None:
            log.error("[AMFI] No saved NAV file in '%s'", os.path.abspath(NAV_TEXT_DIR))
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except Exception:
            log.exception("[AMFI] Failed to read %s", path)
            return {}
        if not text.strip():
            log.error("[AMFI] File %s empty", path)
            return {}
        nav_map, amc_map, records = _parse_nav_text(text)
        self._nav_by_isin = nav_map
        self._amc_by_isin = amc_map
        self._records = records
        self._loaded_from = path
        self._last_load = datetime.now()
        log.info("[AMFI] Index loaded from %s: %d ISINs / %d AMC / %d records",
                 path, len(nav_map), len(amc_map), len(records))
        return nav_map

    def get_nav(self, isin: str):
        if not self._nav_by_isin:
            self.load()
        return self._nav_by_isin.get(isin.strip().upper()) if isin else None

    def get_amc(self, isin: str) -> str:
        if not self._amc_by_isin:
            self.load()
        return self._amc_by_isin.get(isin.strip().upper(), "") if isin else ""

    def get_records(self) -> list[dict]:
        if not self._records:
            self.load()
        return self._records


_amfi = AMFINavIndex()


def fetch_nav_by_isin(isin: str):
    return _amfi.get_nav(isin.strip().upper()) if isin else None


def fetch_amc_by_isin(isin: str) -> str:
    return _amfi.get_amc(isin)


def load_nav_dataframe() -> pd.DataFrame:
    _amfi.load()
    return pd.DataFrame(_amfi.get_records(),
                        columns=["isin", "scheme_code", "isin_payout",
                                 "scheme_name", "amc_name", "category",
                                 "nav", "nav_date"])


def diagnose_previous_day_coverage(current_iso: str, previous_iso: str) -> dict:
    today_path = _snapshot_path(current_iso)
    prev_path = _snapshot_path(previous_iso)
    te, pe = os.path.exists(today_path), os.path.exists(prev_path)
    tc = _count_isins_in_file(today_path) if te else 0
    pc = _count_isins_in_file(prev_path) if pe else 0
    tm = _read_snapshot_map(today_path) if te else {}
    pm = _read_snapshot_map(prev_path) if pe else {}
    tis, pis = set(tm.keys()), set(pm.keys())
    overlap = tis & pis
    missing = tis - pis
    return {
        "current_date": current_iso, "previous_date": previous_iso,
        "today_file_exists": te, "prev_file_exists": pe,
        "today_count": tc, "prev_count": pc,
        "coverage_threshold": _MIN_ACCEPTABLE_ISIN_COUNT,
        "prev_is_partial": 0 < pc < _MIN_ACCEPTABLE_ISIN_COUNT,
        "today_isins": len(tis), "prev_isins": len(pis),
        "overlap": len(overlap), "missing_from_prev": len(missing),
        "missing_isins_sample": sorted(missing)[:10],
        "has_db_fallback": bool(get_nav_for_date_from_db(previous_iso)),
    }