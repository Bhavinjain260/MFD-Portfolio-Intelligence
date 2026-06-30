"""
MFD Portfolio Intelligence — Minimal Version
Only: Admin Panel (Upload + Raw Data View) + Dashboard (View All Details)
"""

import logging
import os
import re
import warnings
from datetime import datetime
from typing import Optional
import plotly.express as px

import pandas as pd
import requests
import streamlit as st

import data_manager
from init_db import init_db, get_conn
from theme_patch import THEME_WATCHER_JS, render_theme

log = logging.getLogger(__name__)

warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

# ==================== CONSTANTS ====================
PAGE_SIZE = 20

_WHITESPACE_RE = re.compile(r"\s+")


# ==================== DB INIT (cached per session) ====================
def ensure_db() -> None:
    """Run init_db() once per session. Schema lives in init.py — single source of truth."""
    if not st.session_state.get("db_initialized"):
        init_db()
        st.session_state["db_initialized"] = True


# ==================== PURE HELPERS ====================
def clean_str(val) -> str:
    if val is None:
        return ""
    try:
        if pd.isna(val):
            return ""
    except (TypeError, ValueError):
        pass
    s = str(val).strip()
    return "" if s.lower() in {"nan", "none", "null", "na", ""} else s


def format_currency(val, decimals: int = 2) -> str:
    try:
        return f"Rs {float(val):,.{decimals}f}"
    except (TypeError, ValueError):
        return "Rs -"


def format_aum(val) -> str:
    try:
        amount = float(val)
        if amount >= 1_00_00_000:
            return f"Rs {amount / 1_00_00_000:.2f} Cr"
        elif amount >= 1_00_000:
            return f"Rs {amount / 1_00_000:.2f} L"
        else:
            return f"Rs {amount:,.0f}"
    except (TypeError, ValueError):
        return "Rs -"


def format_brokerage(val) -> str:
    try:
        amount = float(val)
        formatted = f"{amount:.8f}".rstrip('0').rstrip('.')
        return f"Rs {formatted}"
    except (TypeError, ValueError):
        return "Rs -"


# ==================== Cams, Karvy and Manual entir Brokerage Data HELPERS ====================
def _resolve_amc_via_isin(get_conn, scheme_code_col_sql: str, table: str, scheme_code_value_alias: str):
    """
    Not used directly — kept as documentation of the join shape.
    Actual resolution happens inline in the loader below via a single
    bse_scheme_master join, exactly like get_all_folios_with_isin_and_nav().
    """
    pass


@st.cache_data(ttl=60, show_spinner=False)
def load_brokerage_report(_get_conn) -> dict:
    """
    Returns {
        "merged":     DataFrame[amc, month, file_amount, manual_amount, variance, status]
        "detail":     DataFrame[amc, rta, client, folio, scheme_code, isin, txn_date,
                                 txn_amount, brokerage_pct, brokerage_amount, brokerage_type]
        "manual_raw": DataFrame[amc, month, manual_amount]
    }

    AMC resolution path (matches Dashboard's proven join):
        brokerage.folio_no / account_number
            -> cams_wbr9_folio.foliochk / kfin_mfsd211_folio.folio   (get product code)
            -> Channel_Partner_Code (bse_scheme_master) -> ISIN
            -> _amfi.get_amc(isin)                       (canonical AMC name)

    Date handling: CAMS proc_date is DD-MM-YYYY, KFin proc_date is
    YYYY-MM-DD. Each is parsed in its OWN format BEFORE concatenation —
    parsing a combined column once with a single dayfirst= flag lets
    pandas' format inference lock onto whichever format it sees first
    and silently return NaT for every row in the other format.
    """
    # ---- Resolve BSE's AMC fallback column FIRST, on its own connection,
    #      BEFORE opening the connection used for the real reads ----
    bse_amc_col = _get_bse_amc_column(_get_conn)
    bse_amc_select = f"MAX({bse_amc_col}) AS bse_amc_name" if bse_amc_col else "NULL AS bse_amc_name"
    bse_dedup = f"""
        SELECT
            UPPER(TRIM(Channel_Partner_Code)) AS cp_code,
            MAX(ISIN) AS ISIN,
            {bse_amc_select}
        FROM bse_scheme_master
        WHERE Channel_Partner_Code IS NOT NULL AND TRIM(Channel_Partner_Code) != ''
        GROUP BY UPPER(TRIM(Channel_Partner_Code))
    """

    # ---- CAMS brokerage -> cams_wbr9_folio (get product code) -> Channel_Partner_Code -> ISIN ----
    cams_sql = f"""
        SELECT
            cb.proc_date              AS proc_date,
            cb.brokerage_accrual_month AS accrual_month,
            cb.inv_name               AS client,
            cb.folio_no               AS folio,
            cb.scheme_code            AS scheme_code,
            cb.trxn_no                AS txn_no,
            cb.plot_amount            AS txn_amount,
            cb.brkage_rate            AS brokerage_pct,
            cb.brkage_amt             AS brokerage_amount,
            cb.brkage_type            AS brokerage_type,
            cf.product                AS folio_product_code,
            sm.ISIN                   AS isin,
            sm.bse_amc_name           AS bse_amc_name,
            'CAMS'                    AS rta
        FROM cams_wbr77_brokerage cb
        LEFT JOIN cams_wbr9_folio cf
            ON UPPER(TRIM(cb.folio_no)) = UPPER(TRIM(cf.foliochk))
        LEFT JOIN ({bse_dedup}) sm
            ON UPPER(TRIM(cf.product)) = sm.cp_code
    """

    # ---- KFin brokerage -> kfin_mfsd211_folio (get product_code) -> Channel_Partner_Code -> ISIN ----
    kfin_sql = f"""
        SELECT
            kb.process_date        AS proc_date,
            NULL                   AS accrual_month,
            kb.investor_name       AS client,
            kb.account_number      AS folio,
            kb.scheme_code         AS scheme_code,
            kb.transaction_number  AS txn_no,
            kb.amount           AS txn_amount,
            kb.percentage          AS brokerage_pct,
            kb.brokerage        AS brokerage_amount,
            kb.brokerage_type      AS brokerage_type,
            kf.product_code        AS folio_product_code,
            sm.ISIN                AS isin,
            sm.bse_amc_name        AS bse_amc_name,
            'KFinTech'             AS rta
        FROM kfin_mfsd205_brokerage kb
        LEFT JOIN kfin_mfsd211_folio kf
            ON UPPER(TRIM(kb.account_number)) = UPPER(TRIM(kf.folio))
        LEFT JOIN ({bse_dedup}) sm
            ON UPPER(TRIM(kf.product_code)) = sm.cp_code
    """

    # ---- Open the connection once, run both reads on it, then close ----
    with _get_conn() as conn:
        cams_df = pd.read_sql(cams_sql, conn)
        kfin_df = pd.read_sql(kfin_sql, conn)
        manual_df = pd.read_sql("SELECT amc, month, year, amount FROM monthly_brokerage", conn)

    # ---- Parse each RTA's date format SEPARATELY, before combining ----
    # NOTE: both CAMS and KFin proc_date are YYYY-MM-DD in this dataset —
    # confirmed via direct query (e.g. CAMS: '2026-02-06', '2026-05-06').
    # dayfirst=True on an already year-first string SILENTLY SWAPS month
    # and day (e.g. '2026-02-06' -> June 2nd instead of Feb 6th) instead
    # of raising an error, which is what caused older-month CAMS rows to
    # disappear (they got miscategorized into the current month). Use
    # dayfirst=False for both — if a future upload genuinely uses
    # DD-MM-YYYY, change that side's flag back to True at that time.
    if not cams_df.empty:
        cams_df["proc_date"] = pd.to_datetime(cams_df["proc_date"], errors="coerce", dayfirst=False)
    if not kfin_df.empty:
        kfin_df["proc_date"] = pd.to_datetime(kfin_df["proc_date"], errors="coerce", dayfirst=False)

    detail = pd.concat([cams_df, kfin_df], ignore_index=True)

    # ---- AMC name: AMFI-canonical (via ISIN) first, then BSE fallback, then raw product code ----
    detail["amfi_amc_name"] = detail["isin"].apply(
        lambda i: _amfi.get_amc(i) if pd.notna(i) and str(i).strip() else None
    )
    detail["amc"] = detail["amfi_amc_name"]
    detail["amc"] = detail["amc"].fillna(detail["bse_amc_name"])
    detail["amc"] = detail["amc"].fillna(detail["folio_product_code"])
    detail["amc"] = detail["amc"].fillna(detail["scheme_code"])
    detail["amc"] = detail["amc"].fillna("⚠️ Unresolved")

    # ---- Month key: prefer explicit accrual_month (CAMS), else derive from
    #      the already-parsed proc_date. Anything still unresolved falls
    #      into an explicit "Unknown" bucket instead of silently vanishing
    #      from downstream filters (which never match NaN). ----
    month_from_accrual = detail["accrual_month"].astype(str).str.strip()
    month_from_proc = detail["proc_date"].dt.strftime("%Y-%m")
    detail["month"] = month_from_accrual.where(
        month_from_accrual.str.match(r"^\d{4}-\d{2}$", na=False), month_from_proc
    )
    detail["month"] = detail["month"].fillna("Unknown")

    detail["txn_amount"] = pd.to_numeric(detail["txn_amount"], errors="coerce")
    detail["brokerage_pct"] = pd.to_numeric(detail["brokerage_pct"], errors="coerce")
    detail["brokerage_amount"] = pd.to_numeric(detail["brokerage_amount"], errors="coerce").fillna(0.0)
    detail["txn_date"] = detail["proc_date"].dt.strftime("%Y-%m-%d")

    # ---- File-side grouped by AMC + month ----
    file_grouped = (
        detail.dropna(subset=["month"])
        .groupby(["amc", "month"], dropna=False)["brokerage_amount"]
        .sum()
        .reset_index()
        .rename(columns={"brokerage_amount": "file_amount"})
    )

    # ---- Manual entries ----
    if not manual_df.empty:
        manual_df["month"] = manual_df["year"].astype(str) + "-" + manual_df["month"].astype(str).str.zfill(2)
        manual_grouped = (
            manual_df.groupby(["amc", "month"], dropna=False)["amount"]
            .sum()
            .reset_index()
            .rename(columns={"amount": "manual_amount"})
        )
    else:
        manual_grouped = pd.DataFrame(columns=["amc", "month", "manual_amount"])

    merged = pd.merge(file_grouped, manual_grouped, on=["amc", "month"], how="outer")
    merged["file_amount"] = merged["file_amount"].fillna(0.0)
    merged["manual_amount"] = merged["manual_amount"].fillna(0.0)
    merged["variance"] = merged["file_amount"] - merged["manual_amount"]

    def _status(row):
        if row["manual_amount"] == 0 and row["file_amount"] > 0:
            return "⚠️ Not yet received"
        if row["file_amount"] == 0 and row["manual_amount"] > 0:
            return "❓ Received, no file match"
        if abs(row["variance"]) < 1:
            return "✅ Matched"
        return "🔶 Mismatch"

    if not merged.empty:
        merged["status"] = merged.apply(_status, axis=1)
        merged = merged.sort_values(["month", "amc"], ascending=[False, True])
    else:
        merged["status"] = pd.Series(dtype="object")

    return {"merged": merged, "detail": detail, "manual_raw": manual_grouped}


def format_brokerage_inr(val) -> str:
    try:
        return f"Rs {float(val):,.2f}"
    except (TypeError, ValueError):
        return "Rs -"


# ==================== AMFI NAV SERVICE ====================


AMFI_TEXT_URL = "https://portal.amfiindia.com/spages/NAVAll.txt"

# ── File-based fallback paths (used when live AMFI fetch fails, e.g. no internet) ──
NAV_TEXT_DIR = os.environ.get("NAV_TEXT_DIR", "nav_data")


def _ensure_text_dir() -> None:
    os.makedirs(NAV_TEXT_DIR, exist_ok=True)


def _snapshot_path(date_str: str) -> str:
    return os.path.join(NAV_TEXT_DIR, f"nav_{date_str}.txt")


def _latest_snapshot_path() -> Optional[str]:
    """Most recent saved snapshot file, regardless of date, or None if none exist."""
    _ensure_text_dir()
    available = sorted(
        f for f in os.listdir(NAV_TEXT_DIR) if f.startswith("nav_") and f.endswith(".txt")
    )
    if not available:
        return None
    return os.path.join(NAV_TEXT_DIR, available[-1])


def get_snapshot_status() -> dict:
    """
    Info for an admin panel: does today's file exist, what's the latest
    available file, how old is it. No network call.
    """
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


# ==================== THE ONLY FUNCTION THAT CALLS AMFI ====================

def download_and_save_nav(timeout: int = 30) -> dict:
    """
    Hits AMFI's server, saves the raw response verbatim to nav_data/nav_<today>.txt.
    This is the ONLY function in this module that makes a network call.
    Call this from: (a) app startup, via download_and_save_nav_if_needed(), and
    (b) an explicit admin "Redownload NAV" button.

    Raises on failure — caller decides how to handle/log it.
    Returns {"path": str, "bytes": int, "date": str}.
    """
    log.info("[AMFI] Downloading NAV file from %s", AMFI_TEXT_URL)
    res = requests.get(AMFI_TEXT_URL, timeout=timeout)
    res.raise_for_status()
    text = res.text

    _ensure_text_dir()
    today = datetime.now().strftime("%Y-%m-%d")
    path = _snapshot_path(today)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)

    size = os.path.getsize(path)
    log.info("[AMFI] Saved NAV file: %s (%s bytes, %s lines)",
             path, size, text.count("\n") + 1)
    return {"path": path, "bytes": size, "date": today}


def download_and_save_nav_if_needed(force: bool = False) -> dict:
    """
    Calls download_and_save_nav() only if today's file doesn't already exist.
    Safe to call on every app start/restart — only actually hits AMFI once
    per calendar day unless force=True (admin button).

    Returns {"ran": bool, "ok": bool, "reason": str, "bytes": int|None}.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    today_path = _snapshot_path(today)

    if not force and os.path.exists(today_path):
        size = os.path.getsize(today_path)
        log.info("[AMFI] Today's NAV file already exists (%s, %s bytes) — not hitting AMFI", today, size)
        return {"ran": False, "ok": True, "reason": f"already have today's file ({today})", "bytes": size}

    try:
        result = download_and_save_nav()
        return {"ran": True, "ok": True, "reason": "downloaded", "bytes": result["bytes"]}
    except Exception as e:
        log.exception("[AMFI] Download failed")
        return {"ran": True, "ok": False, "reason": f"download failed: {e}", "bytes": None}


# ==================== PARSER (shared by file load) ====================

def _parse_nav_text(text: str) -> tuple[dict, dict, list[dict]]:
    """
    Parses AMFI's raw text format. Returns:
      nav_map:  {isin: (nav, nav_date)}
      amc_map:  {isin: amc_name}
      records:  list of {isin, scheme_code, isin_payout, scheme_name,
                          amc_name, category, nav, nav_date}
    """
    nav_map: dict[str, tuple[float, str]] = {}
    amc_map: dict[str, str] = {}
    records: list[dict] = []
    current_amc = ""
    current_category = ""

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        # Non-data lines: AMC header or category header (no semicolons)
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
        nav_str = parts[4].strip()
        date_str = parts[5].strip()

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

    return nav_map, amc_map, records


# ==================== IN-MEMORY INDEX (loaded from file, not network) ====================

class AMFINavIndex:
    """
    In-memory ISIN-keyed index, loaded from the saved file. Loaded once per
    process (TTL-cached so repeated calls within the same run are free) and
    NEVER triggers a network call itself — only reads NAV_TEXT_DIR.
    """

    _nav_by_isin: dict[str, tuple[float, str]] = {}
    _amc_by_isin: dict[str, str] = {}
    _records: list[dict] = []
    _loaded_from: Optional[str] = None
    _last_load: Optional[datetime] = None
    _ttl_seconds: int = 3600  # re-read the file at most once/hour within a run

    def _is_fresh(self) -> bool:
        if not self._nav_by_isin or self._last_load is None:
            return False
        age = (datetime.now() - self._last_load).total_seconds()
        return age < self._ttl_seconds

    def load(self, force: bool = False) -> dict[str, tuple[float, str]]:
        """Loads (or reloads) the index from the latest saved file on disk."""
        if not force and self._is_fresh():
            log.debug("[AMFI] In-memory index fresh (loaded from %s) — reusing", self._loaded_from)
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


_amfi = AMFINavIndex()


# ==================== PUBLIC LOOKUP API ====================

def fetch_nav_by_isin(isin: str) -> Optional[tuple[float, str]]:
    """Single ISIN → (nav, nav_date) lookup, reading from the file-backed index."""
    isin = isin.strip().upper()
    result = _amfi.get_nav(isin)
    if result:
        nav, nav_date = result
        log.debug("ISIN %s | NAV: %s | Date: %s", isin, nav, nav_date)
        return nav, nav_date
    log.debug("No NAV found for ISIN: %s", isin)
    return None


def fetch_amc_by_isin(isin: str) -> str:
    """Single ISIN → canonical AMC name lookup, reading from the file-backed index."""
    return _amfi.get_amc(isin)


def load_nav_dataframe() -> pd.DataFrame:
    """
    Full NAV+AMC table as a DataFrame with just isin, nav, nav_date, amc_name —
    reads from the saved file via the in-memory index, no network call.
    """
    _amfi.load()
    records = _amfi.get_records()
    df = pd.DataFrame(records, columns=["isin", "scheme_code", "isin_payout",
                                        "scheme_name", "amc_name", "category",
                                        "nav", "nav_date"])
    return df


# ==================== FOLIO JOIN (CAMS/KFin × BSE × AMFI) ====================

def _get_bse_amc_column(get_conn) -> Optional[str]:
    """
    Probe bse_scheme_master's actual schema to find whichever column holds
    the AMC/fund-house name string. Used only as a fallback label for ISINs
    that don't resolve via AMFI.
    """
    candidates = [
        "amc_code", "amc_ind", "amc_name",
        "AMC", "AMC_Name", "AMC_NAME", "Amc_Name",
        "Fund_Name", "FUND_NAME", "Fund", "FUND",
        "AMC_Code", "AMC_CODE",
    ]
    with get_conn() as conn:
        cols = [row[1] for row in conn.execute("PRAGMA table_info(bse_scheme_master)").fetchall()]
    log.info("[NAV-FLOW] bse_scheme_master columns: %s", cols)
    for c in candidates:
        if c in cols:
            log.info("[NAV-FLOW] Using '%s' as BSE AMC-name fallback column", c)
            return c
    log.warning("[NAV-FLOW] No AMC-name column found in bse_scheme_master (checked %s). "
                "BSE fallback name disabled — rows will rely on AMFI ISIN match only.", candidates)
    return None


def get_all_folios_with_isin_and_nav(get_conn, force_reload: bool = False) -> pd.DataFrame:
    """
    Master batch function — reads NAV/AMC from the saved file (NOT live AMFI):
      1. Load AMFI NAV + AMC index from disk (in-memory cached per process)
      2. Query CAMS folio master JOIN deduplicated bse_scheme_master
      3. Query KFin folio master JOIN deduplicated bse_scheme_master + units
      4. Map NAV + AMC name (canonical, from saved file) in-memory by ISIN
      5. Return combined DataFrame

    force_reload=True re-reads the saved file from disk (not the network) —
    use this if you just ran download_and_save_nav() and want fresh numbers
    without restarting the app.
    """
    log.info("=" * 60)
    log.info("[NAV-FLOW] Starting get_all_folios_with_isin_and_nav()")

    # ── Step 1: Load NAV + AMC index from saved file ──
    log.info("[NAV-FLOW][Step 1] Loading AMFI NAV + AMC index from saved file (force_reload=%s)...",
             force_reload)
    nav_map = _amfi.load(force=force_reload)
    log.info("[NAV-FLOW][Step 1] Index ready: %s ISINs (NAV), %s ISINs (AMC)",
             len(nav_map), len(_amfi._amc_by_isin))
    if not nav_map:
        log.error("[NAV-FLOW][Step 1] nav_map is EMPTY — no saved NAV file found or it's unreadable. "
                  "Every row downstream will have current_nav=None, so total AUM will be 0. "
                  "Fix: call download_and_save_nav_if_needed() at startup, or use the admin "
                  "'Redownload NAV' button. Checked dir: %s", os.path.abspath(NAV_TEXT_DIR))

    # ── Step 2: Deduplicated BSE Scheme Master subquery ──
    log.info("[NAV-FLOW][Step 2] Building deduplicated BSE lookup...")
    bse_amc_col = _get_bse_amc_column(get_conn)
    bse_amc_select = f"MAX({bse_amc_col}) AS bse_amc_name" if bse_amc_col else "NULL AS bse_amc_name"
    bse_dedup = f"""
        SELECT 
            UPPER(TRIM(Channel_Partner_Code)) AS cp_code,
            MAX(ISIN) AS ISIN,
            MAX(Scheme_Name) AS Scheme_Name,
            {bse_amc_select}
        FROM bse_scheme_master
        WHERE Channel_Partner_Code IS NOT NULL AND TRIM(Channel_Partner_Code) != ''
        GROUP BY UPPER(TRIM(Channel_Partner_Code))
    """

    # ── Step 3: CAMS Query ──
    log.info("[NAV-FLOW][Step 3] Building CAMS SQL...")
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

    # ── Step 4: KFin Query ──
    log.info("[NAV-FLOW][Step 4] Building KFin SQL...")
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
        FROM kfin_mfsd201_transaction
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
    log.info("[NAV-FLOW][Step 8] Total NAV-based AUM: ₹%s", f"{total_aum:,.2f}" if pd.notna(total_aum) else "N/A")

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

    log.info("[NAV-FLOW] Complete. Returning %s rows x %s cols", len(result), len(result.columns))
    log.info("=" * 60)
    return result


# def get_folio_nav_summary(get_conn, force_reload: bool = False) -> dict:
#     """Quick stats for Streamlit metrics. Reads from saved file via get_all_folios_with_isin_and_nav."""
#     log.info("[NAV-FLOW] Generating folio NAV summary...")
#
#     df = get_all_folios_with_isin_and_nav(get_conn, force_reload=force_reload)
#     cams_df = df[df["rta"] == "CAMS"]
#     kfin_df = df[df["rta"] == "KFinTech"]
#
#     cams_nav_aum = float(cams_df["nav_based_aum"].sum()) if "nav_based_aum" in cams_df.columns else 0.0
#     cams_file_aum = float(cams_df["file_aum"].sum()) if "file_aum" in cams_df.columns else 0.0
#     kfin_nav_aum = float(kfin_df["nav_based_aum"].sum()) if "nav_based_aum" in kfin_df.columns else 0.0
#
#     cams_unmatched = int((cams_df["has_isin"] & ~cams_df["has_nav"]).sum())
#     kfin_unmatched = int((kfin_df["has_isin"] & ~kfin_df["has_nav"]).sum())
#
#     return {
#         "total_folios": len(df),
#         "cams_wbr9_folios": len(cams_df),
#         "kfin_mfsd211_folios": len(kfin_df),
#         "with_isin": int(df["has_isin"].sum()),
#         "isin_coverage_pct": round(df["has_isin"].mean() * 100, 2) if len(df) else 0,
#         "with_nav": int(df["has_nav"].sum()),
#         "nav_coverage_pct": round(df["has_nav"].mean() * 100, 2) if len(df) else 0,
#         "with_amc": int(df["has_amc"].sum()),
#         "amc_coverage_pct": round(df["has_amc"].mean() * 100, 2) if len(df) else 0,
#         "amc_resolved_via_amfi": int((df["amc_name_source"] == "AMFI").sum()),
#
#         "total_aum": cams_nav_aum + kfin_nav_aum,
#         "cams_wbr4_aum": cams_nav_aum,
#         "cams_file_aum": cams_file_aum,
#         "cams_unmatched_nav": cams_unmatched,
#         "kfin_mfsd203_aum": kfin_nav_aum,
#         "kfin_unmatched_nav": kfin_unmatched,
#
#         "cams_with_nav": int(cams_df["has_nav"].sum()),
#         "cams_total": len(cams_df),
#         "kfin_with_nav": int(kfin_df["has_nav"].sum()),
#         "kfin_total": len(kfin_df),
#
#         "df": df,
#     }

def get_folio_nav_summary(get_conn, force_reload: bool = False) -> dict:
    """Quick stats for Streamlit metrics."""
    log.info("[NAV-FLOW] Generating folio NAV summary...")

    df = get_all_folios_with_isin_and_nav(get_conn, force_reload=force_reload)
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


def load_amc_breakdown_by_isin(get_conn) -> pd.DataFrame:
    """AMC-wise AUM + folio breakdown, grouped by canonical AMFI AMC name (via ISIN)."""
    df = get_all_folios_with_isin_and_nav(get_conn)
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


def normalize_folio(folio: str) -> str:
    if not folio:
        return ""
    try:
        if pd.isna(folio):
            return ""
    except Exception:
        pass
    return str(folio).strip().split("/")[0].strip().lower()


def theme_plotly(fig, dark: bool):
    text_c = "#e6edf3" if dark else "#1a1a2e"
    grid_c = "#30363d" if dark else "#e2e8f0"
    fig.update_layout(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font_color=text_c,
        title_font_color=text_c,
        legend_font_color=text_c,
        xaxis=dict(gridcolor=grid_c, linecolor=grid_c),
        yaxis=dict(gridcolor=grid_c, linecolor=grid_c),
    )
    return fig


# Calcute the Invested Amount for Karvy Schemes
@st.cache_data(ttl=180, show_spinner=False)
def get_kfin_invested_amount(folio_list):
    if not folio_list:
        return 0.0

    # ⬇️ TEMPORARY DEBUG: Remove after testing ⬇️
    print(f"DEBUG KFIN INVESTED: Received {len(folio_list)} folios: {folio_list}")
    st.write(f"**DEBUG:** Calculating invested for {len(folio_list)} folios:", folio_list)
    # ⬆️ TEMPORARY DEBUG: Remove after testing ⬆️

    with get_conn() as conn:
        placeholders = ','.join(['?'] * len(folio_list))
        query = f"""
            SELECT COALESCE(SUM(td_amt), 0) as total_invested
            FROM kfin_mfsd201_transaction 
            WHERE td_acno IN ({placeholders})
        """
        result = conn.execute(query, folio_list).fetchone()[0]
        return float(result) if result is not None else 0.0


@st.cache_data(ttl=180, show_spinner=False)
def get_kfin_invested_per_scheme(folio_list: list) -> pd.DataFrame:
    """
    Return invested amount PER SCHEME for KFin folios.
    Groups by folio + product_code so each scheme gets its own total.
    """
    if not folio_list:
        return pd.DataFrame(columns=["folio_id", "product_code", "invested_amount"])

    placeholders = ",".join(["?"] * len(folio_list))
    with get_conn() as conn:
        query = f"""
            SELECT
                td_acno   AS folio_id,
                fmcode    AS product_code,
                COALESCE(SUM(td_amt), 0) AS invested_amount
            FROM kfin_mfsd201_transaction
            WHERE td_acno IN ({placeholders})
            GROUP BY td_acno, fmcode
        """
        return pd.read_sql(query, conn, params=folio_list)


# ==================== DATA LOADERS ====================
@st.cache_data(ttl=60, show_spinner=False)
def load_table_summary(table: str) -> pd.DataFrame:
    with get_conn() as conn:
        return pd.read_sql(f"SELECT * FROM {table} LIMIT 1000", conn)


@st.cache_data(ttl=60, show_spinner=False)
def load_db_stats() -> dict:
    stats = {}
    with get_conn() as conn:
        tables = [
            "bse_client_master", "bse_sip", "bse_scheme_master",
            "cams_wbr4_aum", "cams_wbr9_folio", "cams_wbr2_transaction",
            "cams_wbr49_sip", "cams_wbr77_brokerage",
            "kfin_mfsd203_aum", "kfin_mfsd211_folio", "kfin_mfsd201_transaction",
            "kfin_mfsd243_sip", "kfin_mfsd205_brokerage",
            "monthly_brokerage", "amc_code_map"
        ]
        for t in tables:
            try:
                stats[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except:
                stats[t] = 0
    return stats


# @st.cache_data(ttl=60, show_spinner=False)
# def load_dashboard_summary() -> dict:
#     """Load key metrics for dashboard."""
#     summary = {}
#     with get_conn() as conn:
#         # ── BSE ──
#         summary["total_clients"] = conn.execute("SELECT COUNT(*) FROM bse_client_master").fetchone()[0]
#         summary["total_xsip"] = conn.execute("SELECT COUNT(*) FROM bse_sip").fetchone()[0]
#         summary["active_xsip"] = conn.execute(
#             "SELECT COUNT(*) FROM bse_sip WHERE LOWER(COALESCE(status, '')) LIKE '%active%'"
#         ).fetchone()[0]
#         summary["bse_schemes"] = conn.execute("SELECT COUNT(*) FROM bse_scheme_master").fetchone()[0]
#
#         # ── CAMS ──
#         summary["cams_wbr9_folios"] = conn.execute(
#             "SELECT COUNT(DISTINCT foliochk) FROM cams_wbr9_folio"
#         ).fetchone()[0]
#         summary["cams_txns"] = conn.execute("SELECT COUNT(*) FROM cams_wbr2_transaction").fetchone()[0]
#         summary["cams_wbr49_sips"] = conn.execute("SELECT COUNT(*) FROM cams_wbr49_sip").fetchone()[0]
#         summary["cams_wbr4_aum"] = conn.execute(
#             "SELECT COALESCE(SUM(rupee_bal), 0) FROM cams_wbr4_aum"
#         ).fetchone()[0]
#         summary["cams_wbr77_brokerage"] = conn.execute(
#             "SELECT COALESCE(SUM(brkage_amt), 0) FROM cams_wbr77_brokerage"
#         ).fetchone()[0]
#         summary["cams_amcs"] = conn.execute(
#             "SELECT COUNT(DISTINCT amc_code) FROM cams_wbr9_folio WHERE COALESCE(amc_code, '') != ''"
#         ).fetchone()[0]
#
#         # ── KFinTech ──
#         summary["kfin_mfsd211_folios"] = conn.execute(
#             "SELECT COUNT(DISTINCT Folio) FROM kfin_mfsd211_folio"
#         ).fetchone()[0]
#         summary["kfin_txns"] = conn.execute("SELECT COUNT(*) FROM kfin_mfsd201_transaction").fetchone()[0]
#         summary["kfin_mfsd243_sips"] = conn.execute("SELECT COUNT(*) FROM kfin_mfsd243_sip").fetchone()[0]
#         summary["kfin_mfsd205_brokerage"] = conn.execute(
#             "SELECT COALESCE(SUM(brokerage_rs), 0) FROM kfin_mfsd205_brokerage"
#         ).fetchone()[0]
#         summary["kfin_amcs"] = conn.execute(
#             "SELECT COUNT(DISTINCT Fund) FROM kfin_mfsd211_folio WHERE COALESCE(Fund, '') != ''"
#         ).fetchone()[0]
#
#         # KFin AUM: sum td_amt grouped by td_acno from MFSD201
#         try:
#             kfin_mfsd203_aum_result = conn.execute("""
#                 SELECT COALESCE(SUM(inner_sum), 0) FROM (
#                     SELECT td_acno, SUM(td_amt) as inner_sum
#                     FROM kfin_mfsd201_transaction
#                     GROUP BY td_acno
#                 )
#             """).fetchone()[0]
#             summary["kfin_mfsd203_aum"] = float(kfin_mfsd203_aum_result) if kfin_mfsd203_aum_result else 0.0
#         except Exception as e:
#             log.warning("KFin AUM calculation failed: %s", e)
#             summary["kfin_mfsd203_aum"] = 0.0
#
#         # ── Totals ──
#         summary["total_aum"] = summary["cams_wbr4_aum"] + summary["kfin_mfsd203_aum"]
#         summary["total_brokerage"] = summary["cams_wbr77_brokerage"] + summary["kfin_mfsd205_brokerage"]
#
#     return summary

@st.cache_data(ttl=60, show_spinner=False)
def load_dashboard_summary() -> dict:
    """Load key metrics for dashboard."""
    summary = {}
    with get_conn() as conn:
        # ── BSE ──
        summary["total_clients"] = conn.execute("SELECT COUNT(*) FROM bse_client_master").fetchone()[0]
        summary["total_xsip"] = conn.execute("SELECT COUNT(*) FROM bse_sip").fetchone()[0]
        summary["active_xsip"] = conn.execute(
            "SELECT COUNT(*) FROM bse_sip WHERE LOWER(COALESCE(status, '')) LIKE '%active%'"
        ).fetchone()[0]
        summary["bse_schemes"] = conn.execute("SELECT COUNT(*) FROM bse_scheme_master").fetchone()[0]

        # ── CAMS ──
        summary["cams_folios"] = conn.execute(
            "SELECT COUNT(DISTINCT foliochk) FROM cams_wbr9_folio"
        ).fetchone()[0]
        summary["cams_txns"] = conn.execute("SELECT COUNT(*) FROM cams_wbr2_transaction").fetchone()[0]
        summary["cams_sips"] = conn.execute("SELECT COUNT(*) FROM cams_wbr49_sip").fetchone()[0]
        summary["cams_aum"] = conn.execute(
            "SELECT COALESCE(SUM(rupee_bal), 0) FROM cams_wbr4_aum"
        ).fetchone()[0]
        summary["cams_brokerage"] = conn.execute(
            "SELECT COALESCE(SUM(brkage_amt), 0) FROM cams_wbr77_brokerage"
        ).fetchone()[0]
        summary["cams_amcs"] = conn.execute(
            "SELECT COUNT(DISTINCT amc_code) FROM cams_wbr9_folio WHERE COALESCE(amc_code, '') != ''"
        ).fetchone()[0]

        # ── KFinTech ──
        summary["kfin_folios"] = conn.execute(
            "SELECT COUNT(DISTINCT Folio) FROM kfin_mfsd211_folio"
        ).fetchone()[0]
        summary["kfin_txns"] = conn.execute("SELECT COUNT(*) FROM kfin_mfsd201_transaction").fetchone()[0]
        summary["kfin_sips"] = conn.execute("SELECT COUNT(*) FROM kfin_mfsd243_sip").fetchone()[0]
        summary["kfin_brokerage"] = conn.execute(
            "SELECT COALESCE(SUM(brokerage), 0) FROM kfin_mfsd205_brokerage"
        ).fetchone()[0]
        summary["kfin_amcs"] = conn.execute(
            "SELECT COUNT(DISTINCT Fund) FROM kfin_mfsd211_folio WHERE COALESCE(Fund, '') != ''"
        ).fetchone()[0]

        # KFin AUM: sum td_amt grouped by td_acno from MFSD201
        try:
            kfin_aum_result = conn.execute("""
                SELECT COALESCE(SUM(inner_sum), 0) FROM (
                    SELECT td_acno, SUM(td_amt) as inner_sum 
                    FROM kfin_mfsd201_transaction 
                    GROUP BY td_acno
                )
            """).fetchone()[0]
            summary["kfin_aum"] = float(kfin_aum_result) if kfin_aum_result else 0.0
        except Exception as e:
            log.warning("KFin AUM calculation failed: %s", e)
            summary["kfin_aum"] = 0.0

        # ── Totals ──
        summary["total_aum"] = summary["cams_aum"] + summary["kfin_aum"]
        summary["total_brokerage"] = summary["cams_brokerage"] + summary["kfin_brokerage"]

    return summary

@st.cache_data(ttl=60, show_spinner=False)
def load_amc_breakdown() -> pd.DataFrame:
    """AMC-wise AUM and folio summary. KFin uses Fund column, td_acno for join."""
    with get_conn() as conn:
        # ── CAMS ──
        cams_wbr4_aum_df = pd.read_sql("""
            SELECT amc_code as amc, 
                   COALESCE(SUM(rupee_bal), 0) as aum
            FROM cams_wbr9_folio  
            WHERE COALESCE(amc_code, '') != ''
            GROUP BY amc_code
        """, conn)

        cams_wbr9_folio_df = pd.read_sql("""
            SELECT amc_code as amc,
                   COUNT(DISTINCT foliochk) as folios,
                   COUNT(*) as records
            FROM cams_wbr9_folio 
            WHERE COALESCE(amc_code, '') != ''
            GROUP BY amc_code
        """, conn)

        if cams_wbr4_aum_df.empty and cams_wbr9_folio_df.empty:
            cams_combined = pd.DataFrame()
        elif cams_wbr4_aum_df.empty:
            cams_combined = cams_wbr9_folio_df.copy()
            cams_combined["aum"] = 0.0
        elif cams_wbr9_folio_df.empty:
            cams_combined = cams_wbr4_aum_df.copy()
            cams_combined["folios"] = 0
            cams_combined["records"] = 0
        else:
            cams_combined = cams_wbr9_folio_df.merge(cams_wbr4_aum_df, on="amc", how="outer").fillna(0)

        if not cams_combined.empty:
            cams_combined["rta"] = "CAMS"

        # ── KFinTech ──
        # AUM from transactions grouped by Fund (AMC) via td_acno join
        try:
            kfin_mfsd203_aum_df = pd.read_sql("""
                SELECT kf.Fund as amc,
                       COALESCE(SUM(kt.td_amt), 0) as aum
                FROM kfin_mfsd201_transaction kt
                JOIN kfin_mfsd211_folio kf ON kt.td_acno = kf.Folio
                WHERE COALESCE(kf.Fund, '') != ''
                GROUP BY kf.Fund
            """, conn)
        except Exception as e:
            log.warning("KFin AUM breakdown failed: %s", e)
            kfin_mfsd203_aum_df = pd.DataFrame(columns=["amc", "aum"])

        try:
            kfin_mfsd211_folio_df = pd.read_sql("""
                SELECT Fund as amc,
                       COUNT(DISTINCT Folio) as folios,
                       COUNT(*) as records
                FROM kfin_mfsd211_folio
                WHERE COALESCE(Fund, '') != ''
                GROUP BY Fund
            """, conn)
        except Exception as e:
            log.warning("KFin folio breakdown failed: %s", e)
            kfin_mfsd211_folio_df = pd.DataFrame(columns=["amc", "folios", "records"])

        if kfin_mfsd203_aum_df.empty and kfin_mfsd211_folio_df.empty:
            kfin_combined = pd.DataFrame()
        elif kfin_mfsd203_aum_df.empty:
            kfin_combined = kfin_mfsd211_folio_df.copy()
            kfin_combined["aum"] = 0.0
        elif kfin_mfsd211_folio_df.empty:
            kfin_combined = kfin_mfsd203_aum_df.copy()
            kfin_combined["folios"] = 0
            kfin_combined["records"] = 0
        else:
            kfin_combined = kfin_mfsd211_folio_df.merge(kfin_mfsd203_aum_df, on="amc", how="outer").fillna(0)

        if not kfin_combined.empty:
            kfin_combined["rta"] = "KFinTech"

        # ── Combine ──
        if cams_combined.empty and kfin_combined.empty:
            return pd.DataFrame()

        combined = pd.concat([cams_combined, kfin_combined], ignore_index=True)
        for col in ["amc", "folios", "records", "aum", "rta"]:
            if col not in combined.columns:
                combined[col] = 0 if col != "rta" else ""
        combined = combined[["amc", "folios", "records", "aum", "rta"]]
        combined = combined.sort_values("aum", ascending=False)
        return combined


@st.cache_data(ttl=60, show_spinner=False)
def load_recent_uploads(limit: int = 10) -> pd.DataFrame:
    """Show recent upload batches."""
    with get_conn() as conn:
        batches = []
        for table, batch_col in [
            ("bse_client_master", "upload_batch"),
            ("bse_sip", "upload_batch"),
            ("bse_scheme_master", "upload_batch"),
            ("cams_wbr4_aum", "upload_batch"),
            ("cams_wbr9_folio", "upload_batch"),
            ("cams_wbr2_transaction", "upload_batch"),
            ("cams_wbr49_sip", "upload_batch"),
            ("cams_wbr77_brokerage", "upload_batch"),
            ("kfin_mfsd203_aum", "upload_batch"),
            ("kfin_mfsd211_folio", "upload_batch"),
            ("kfin_mfsd201_transaction", "upload_batch"),
            ("kfin_mfsd243_sip", "upload_batch"),
            ("kfin_mfsd205_brokerage", "upload_batch"),
        ]:
            try:
                rows = conn.execute(f"""
                    SELECT '{table}' as source_table, 
                           {batch_col} as batch_id,
                           COUNT(*) as row_count,
                           MAX(id) as max_id
                    FROM {table}
                    WHERE {batch_col} IS NOT NULL
                    GROUP BY {batch_col}
                    ORDER BY max_id DESC
                    LIMIT {limit}
                """).fetchall()
                batches.extend(rows)
            except:
                pass

        if not batches:
            return pd.DataFrame()

        df = pd.DataFrame(batches, columns=["Table", "Batch ID", "Rows", "_max_id"])
        df = df.sort_values("_max_id", ascending=False).head(limit)
        return df.drop(columns=["_max_id"])


# ==================== APP INIT ====================
st.set_page_config(page_title="MFD Portfolio Intelligence", layout="wide", page_icon="📊")

ensure_db()

# -------------------- THEME (native Streamlit System/Light/Dark) --------------------
current_theme = st.context.theme.type

dark = st.context.theme.type == "dark"

st.html(THEME_WATCHER_JS)

if "last_theme" not in st.session_state:
    st.session_state["last_theme"] = current_theme
elif st.session_state["last_theme"] != current_theme:
    st.session_state["last_theme"] = current_theme
    st.rerun()

dark = current_theme == "dark"

st.markdown(render_theme(dark), unsafe_allow_html=True)

# ==================== Navigation ====================
nav_cols = st.columns([1, 1, 1, 1])  # Changed to 4 columns
nav_options = ["📊 Dashboard", "👥 Clients", "💰 Brokerage Report", "⚙️ Admin Panel"]
nav_keys = ["nav_dash", "nav_clients", "nav_brokerage", "nav_admin"]

if "nav_mode" not in st.session_state:
    st.session_state["nav_mode"] = "📊 Dashboard"

for i, (opt, key) in enumerate(zip(nav_options, nav_keys)):
    with nav_cols[i]:
        btn_type = "primary" if st.session_state["nav_mode"] == opt else "secondary"
        label = opt.split(" ")[1]
        if st.button(label, key=key, type=btn_type, use_container_width=True):
            st.session_state["nav_mode"] = opt
            st.rerun()

mode = st.session_state.get("nav_mode", "📊 Dashboard")

# ==================== 📊 DASHBOARD ====================

if mode == "📊 Dashboard":
    st.header("📊 Portfolio Overview")

    # ── Auto-fetch NAV on first load ──
    nav_ready = False
    folio_nav_df = pd.DataFrame()
    nav_stats = {}

    if "folio_nav_df" not in st.session_state:
        with st.spinner("⏳ Fetching ISIN mappings & latest NAVs from AMFI... (5–10s)"):
            try:
                download_and_save_nav_if_needed()

                folio_nav_df = get_all_folios_with_isin_and_nav(get_conn)
                nav_stats = get_folio_nav_summary(get_conn)
                st.session_state["folio_nav_df"] = folio_nav_df
                st.session_state["folio_nav_summary"] = nav_stats
                nav_ready = True
                st.toast("✅ NAV data synced!")
            except Exception as e:
                st.error(f"Failed to fetch NAV: {e}")
                log.exception("Auto NAV fetch failed")
    else:
        folio_nav_df = st.session_state["folio_nav_df"]
        nav_stats = st.session_state["folio_nav_summary"]
        nav_ready = True

    # ── Skeleton loading if NAV not ready ──
    if not nav_ready:
        st.info("⏳ Loading portfolio data... Please wait.")
        skel1, skel2, skel3 = st.columns(3)
        with skel1:
            st.markdown(
                '<div style="background:#1e1e2e;border-radius:12px;padding:20px;height:100px;">'
                '<div style="background:#30363d;height:14px;width:60%;border-radius:4px;margin-bottom:12px;"></div>'
                '<div style="background:#30363d;height:28px;width:80%;border-radius:4px;"></div></div>',
                unsafe_allow_html=True)
        with skel2:
            st.markdown(
                '<div style="background:#1e1e2e;border-radius:12px;padding:20px;height:100px;">'
                '<div style="background:#30363d;height:14px;width:60%;border-radius:4px;margin-bottom:12px;"></div>'
                '<div style="background:#30363d;height:28px;width:80%;border-radius:4px;"></div></div>',
                unsafe_allow_html=True)
        with skel3:
            st.markdown(
                '<div style="background:#1e1e2e;border-radius:12px;padding:20px;height:100px;">'
                '<div style="background:#30363d;height:14px;width:60%;border-radius:4px;margin-bottom:12px;"></div>'
                '<div style="background:#30363d;height:28px;width:80%;border-radius:4px;"></div></div>',
                unsafe_allow_html=True)
        st.stop()

    # ── Merge base stats + NAV stats ──
    base_summary = load_dashboard_summary()
    summary = {**base_summary, **nav_stats}

    # ── Refresh button ──
    c_refresh, _ = st.columns([1, 5])
    with c_refresh:
        if st.button("🔄 Refresh Data", use_container_width=True):
            st.cache_data.clear()
            st.session_state.pop("folio_nav_df", None)
            st.session_state.pop("folio_nav_summary", None)
            _amfi.load(force=True)
            st.rerun()

    # ── AUM Cards (NAV-based only) ──
    nav_coverage = summary.get("nav_coverage_pct", 0)
    with_nav = summary.get("with_nav", 0)
    total = summary.get("total_folios", 0)

    if nav_coverage == 0:
        st.warning("⚠️ AMFI NAV not available. Showing file-based AUM as fallback.")
    else:
        st.caption(f"📡 AMFI NAV synced: **{with_nav}/{total}** folios ({nav_coverage}%) | AUM = Units × NAV")

    aum_col1, aum_col2, aum_col3 = st.columns(3)

    with aum_col1:
        st.markdown(
            f'<div class="aum-card-bse"><div class="label">📦 Total AUM (All RTAs)</div>'
            f'<div class="value">{format_aum(summary.get("total_aum", 0))}</div></div>',
            unsafe_allow_html=True)

    with aum_col2:
        cams_unmatched = summary.get("cams_unmatched_nav", 0)
        cams_label = "🟢 CAMS Current Value"
        if cams_unmatched > 0:
            cams_label += f" ({cams_unmatched} unmatched)"
        st.markdown(
            f'<div class="aum-card"><div class="label">{cams_label}</div>'
            f'<div class="value">{format_aum(summary.get("cams_aum", 0))}</div></div>',
            unsafe_allow_html=True)

    with aum_col3:
        kfin_unmatched = summary.get("kfin_unmatched_nav", 0)
        kfin_label = "🔵 KFinTech Current Value"
        if kfin_unmatched > 0:
            kfin_label += f" ({kfin_unmatched} unmatched)"
        st.markdown(
            f'<div class="aum-card-kfin"><div class="label">{kfin_label}</div>'
            f'<div class="value">{format_aum(summary.get("kfin_aum", 0))}</div></div>',
            unsafe_allow_html=True)

    st.divider()

    # ── Top metrics row ──
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("👥 Clients", summary.get("total_clients", 0))
    m2.metric("📋 BSE SIPs", summary.get("total_xsip", 0))
    m3.metric("✅ Active SIPs", summary.get("active_xsip", 0))
    m4.metric("🏢 CAMS AMCs", summary.get("cams_amcs", 0))
    m5.metric("🏢 KFinTech AMCs", summary.get("kfin_amcs", 0))

    # ── Second metrics row ──
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("📂 CAMS Folios", summary.get("cams_folios", 0))
    m2.metric("💱 CAMS Txns", summary.get("cams_txns", 0))
    m3.metric("🔄 CAMS SIPs", summary.get("cams_sips", 0))
    m4.metric("💰 CAMS Brokerage", format_brokerage(summary.get("cams_brokerage", 0)))
    m5.metric("💰 KFin Brokerage", format_brokerage(summary.get("kfin_brokerage", 0)))

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("📂 KFin Folios", summary.get("kfin_folios", 0))
    m2.metric("💱 KFin Txns", summary.get("kfin_txns", 0))
    m3.metric("🔄 KFin SIPs", summary.get("kfin_sips", 0))
    m4.metric("💰 Total Brokerage", format_brokerage(summary.get("total_brokerage", 0)))
    m5.metric("📊 Total Records", sum(summary.get(k, 0) for k in [
        "total_clients", "total_xsip", "cams_txns", "cams_sips",
        "kfin_txns", "kfin_sips"
    ]))

    # ── ISIN + Current NAV Section ──
    st.divider()
    st.subheader("📈 Folio-Level ISIN & Current NAV")

    if "folio_nav_df" in st.session_state:
        df = st.session_state["folio_nav_df"]

        f1, f2, f3 = st.columns([2, 2, 2])
        with f1:
            rta_filter = st.multiselect(
                "RTA", df["rta"].unique(), default=df["rta"].unique(), key="nav_rta_filter"
            )
        with f2:
            show_only = st.radio(
                "Show", ["All", "With ISIN only", "With NAV only", "Missing ISIN", "Missing NAV"],
                horizontal=True, key="nav_show_filter"
            )
        with f3:
            search_folio = st.text_input("🔍 Search Folio / Investor", "", key="nav_search")

        view = df[df["rta"].isin(rta_filter)]

        if show_only == "With ISIN only":
            view = view[view["has_isin"]]
        elif show_only == "With NAV only":
            view = view[view["has_nav"]]
        elif show_only == "Missing ISIN":
            view = view[~view["has_isin"]]
        elif show_only == "Missing NAV":
            view = view[view["has_isin"] & ~view["has_nav"]]

        if search_folio.strip():
            mask = (
                    view["folio_id"].astype(str).str.contains(search_folio, case=False, na=False) |
                    view["investor_name"].astype(str).str.contains(search_folio, case=False, na=False)
            )
            view = view[mask]

        display_cols = [
            "rta", "folio_id", "investor_name", "product_code",
            "scheme_name", "isin", "current_nav", "nav_date",
            "units", "file_aum", "nav_based_aum"
        ]
        display_cols = [c for c in display_cols if c in view.columns]

        st.dataframe(
            view[display_cols],
            width='stretch',
            hide_index=True,
            column_config={
                "current_nav": st.column_config.NumberColumn("Current NAV", format="₹ %.4f"),
                "nav_based_aum": st.column_config.NumberColumn("NAV-based AUM", format="₹ %.2f"),
                "file_aum": st.column_config.NumberColumn("File AUM", format="₹ %.2f"),
                "units": st.column_config.NumberColumn("Units", format="%.4f"),
            }
        )

        st.caption(f"Showing {len(view):,} of {len(df):,} folios")

        if not view.empty:
            csv = view.to_csv(index=False).encode("utf-8")
            st.download_button(
                "⬇️ Download NAV Report (CSV)",
                csv,
                "folio_nav_report.csv",
                "text/csv",
            )

    # ── AMC Breakdown ──
    st.divider()
    st.subheader("🏢 AMC-wise Breakdown")

    if nav_ready and not folio_nav_df.empty:
        amc_breakdown_df = folio_nav_df.copy()
        amc_breakdown_df["amc_name"] = amc_breakdown_df["amc_name"].fillna("⚠️ Unresolved (no ISIN match)")

        amc_df = (
            amc_breakdown_df.groupby(["amc_name", "rta"], dropna=False)
            .agg(
                folios=("folio_id", "nunique"),
                records=("folio_id", "count"),
                aum=("nav_based_aum", "sum"),
            )
            .reset_index()
            .sort_values("aum", ascending=False)
        )

        if not amc_df.empty:
            amc_df["aum_display"] = amc_df["aum"].apply(format_aum)
            display_df = amc_df[["amc_name", "rta", "folios", "records", "aum_display"]].rename(
                columns={"amc_name": "AMC Name", "rta": "RTA", "folios": "Folios",
                         "records": "Records", "aum_display": "AUM"}
            )
            st.dataframe(display_df, use_container_width=True, hide_index=True)

            fig = px.pie(amc_df, values="aum", names="amc_name", hole=0.4,
                         title="AUM Distribution by AMC")
            fig = theme_plotly(fig, dark)
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("No AMC breakdown data available.")
    else:
        st.info("AMC breakdown requires NAV data. Refresh the page if NAV is still loading.")

    # ── Recent Uploads ──
    st.divider()
    st.subheader("📤 Recent Uploads")
    uploads_df = load_recent_uploads()
    if not uploads_df.empty:
        st.dataframe(uploads_df, width='stretch', hide_index=True)
    else:
        st.info("No uploads yet. Go to Admin Panel to upload data.")



# ==================== 👥 CLIENTS ====================
elif mode == "👥 Clients":
    st.header("👤 Client Portfolio & Analytics")


    # Client Search
    @st.cache_data(ttl=300)
    def load_clients_search():
        with get_conn() as conn:
            return pd.read_sql("""
                SELECT client_code, 
                primary_holder_first_name || ' ' || primary_holder_last_name AS name, 
                primary_holder_pan AS pan, 
                indian_mobile_no AS mobile,  -- ← FIXED
                email, 
                city
                    FROM bse_client_master             -- ← FIXED
                    WHERE primary_holder_pan IS NOT NULL
            """, conn)


    clients_df = load_clients_search()
    if clients_df.empty:
        st.warning("No clients found. Upload client master.")
        st.stop()

    col1, col2 = st.columns([3, 1])
    with col1:
        search_term = st.text_input("🔍 Search Client", key="client_search")
    with col2:
        max_show = st.slider("Show", 10, 100, 30)

    if search_term:
        mask = clients_df.apply(lambda x: x.astype(str).str.contains(search_term, case=False)).any(axis=1)
        search_results = clients_df[mask].head(max_show)
    else:
        search_results = clients_df.head(max_show)

    search_results['display'] = search_results.apply(
        lambda r: f"{r['name']} | PAN: {r['pan']} | {r['client_code']}", axis=1)

    selected_display = st.selectbox("Select Client", search_results['display'].tolist(), key="client_select")
    if not selected_display: st.stop()

    selected_client = search_results[search_results['display'] == selected_display].iloc[0]
    print(selected_client)
    client_code = selected_client['client_code']
    pan = selected_client['pan']
    name = selected_client['name']

    st.divider()
    st.subheader(f"👤 {name}")
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Client Code", client_code)
    c2.metric("PAN", pan)
    c3.metric("Mobile", selected_client.get('mobile') or 'N/A')
    c4.metric("Email", selected_client.get('email') or 'N/A')
    c5.metric("City", selected_client.get('city') or 'N/A')
    st.divider()

    # NAV Data
    if "folio_nav_df" not in st.session_state:
        with st.spinner("Loading NAV..."):
            download_and_save_nav_if_needed()
            st.session_state["folio_nav_df"] = get_all_folios_with_isin_and_nav(get_conn)

    folio_nav_df = st.session_state["folio_nav_df"]

    # Get Folios
    with get_conn() as conn:
        cams_f = pd.read_sql("SELECT foliochk FROM cams_wbr9_folio  WHERE TRIM(UPPER(pan_no))=? OR TRIM(UPPER(inv_name))=?",
                             conn, params=(pan, name))
        kfin_f = pd.read_sql(
            "SELECT folio FROM kfin_mfsd211_folio WHERE TRIM(UPPER(pan_number))=? OR TRIM(UPPER(investor_name))=?",
            conn, params=(pan, name))

    all_folios = set(cams_f['foliochk'].tolist() + kfin_f['folio'].tolist())

    if not all_folios:
        st.info("No folios found.")
        st.stop()


    # =====================================================================
    # FIX 1: KFIN INVESTED AMOUNT (PER SCHEME, NOT TOTAL FOLIO)
    # =====================================================================
    @st.cache_data(ttl=180)
    def get_kfin_invested_per_scheme(folios):
        if not folios:
            return pd.DataFrame(columns=["folio_id", "product_code", "invested_amount"])
        with get_conn() as conn:
            ph = ','.join(['?'] * len(folios))
            # Group by folio AND product code so each scheme gets its own sum
            return pd.read_sql(f"""
                SELECT 
                    td_acno AS folio_id, 
                    fmcode AS product_code,
                    COALESCE(SUM(td_amt), 0) AS invested_amount
                FROM kfin_mfsd201_transaction 
                WHERE td_acno IN ({ph})
                GROUP BY td_acno, fmcode
            """, conn, params=folios)


    # Holdings
    holdings = folio_nav_df[folio_nav_df['folio_id'].isin(all_folios)].copy()

    if not holdings.empty:
        # Apply KFin fix: Merge the per-scheme DataFrame instead of painting one total
        if 'KFinTech' in holdings['rta'].values:
            kfin_invested_df = get_kfin_invested_per_scheme(kfin_f['folio'].tolist())
            holdings = holdings.merge(kfin_invested_df, on=["folio_id", "product_code"], how="left")

            # Update only KFin rows with their specific scheme invested amount
            kfin_mask = holdings['rta'] == 'KFinTech'
            holdings.loc[kfin_mask, 'file_aum'] = holdings.loc[kfin_mask, 'invested_amount']
            holdings = holdings.drop(columns=['invested_amount'], errors='ignore')

        total_invested = holdings['file_aum'].sum()
        total_current = holdings['nav_based_aum'].sum() or 0
        total_gain_loss = total_current - total_invested

        h1, h2, h3, h4 = st.columns(4)
        h1.metric("Total Invested", format_aum(total_invested))
        h2.metric("Current Value", format_aum(total_current))
        h3.metric("Gain / Loss", format_aum(total_gain_loss),
                  delta=f"{(total_gain_loss / total_invested * 100):.2f}%" if total_invested > 0 else "0%")
        h4.metric("Total Folios", len(all_folios))

        # Display Holdings
        holdings["gain_loss"] = holdings["nav_based_aum"] - holdings["file_aum"]
        holdings["portfolio_pct"] = (holdings["nav_based_aum"] / total_current * 100).fillna(
            0) if total_current > 0 else 0

        display_holdings = holdings[[
            'rta', 'folio_id', 'amc_name', 'scheme_name', 'units', 'file_aum',
            'current_nav', 'nav_based_aum', 'gain_loss', 'portfolio_pct'
        ]].rename(columns={
            'rta': 'RTA', 'folio_id': 'Folio', 'amc_name': 'AMC', 'scheme_name': 'Scheme',
            'file_aum': 'Invested', 'nav_based_aum': 'Current Value',
            'gain_loss': 'Gain/Loss', 'portfolio_pct': '% Portfolio'
        })

        selected = st.dataframe(
            display_holdings.sort_values("Current Value", ascending=False),
            use_container_width=True, hide_index=True, on_select="rerun", selection_mode="single-row",
            column_config={
                "Units": st.column_config.NumberColumn(format="%.4f"),
                "Invested": st.column_config.NumberColumn(format="₹ %.2f"),
                "Current Value": st.column_config.NumberColumn(format="₹ %.2f"),
                "Gain/Loss": st.column_config.NumberColumn(format="₹ %.2f"),
                "% Portfolio": st.column_config.NumberColumn(format="%.2f%%"),
            }
        )

        # Transaction View
        if selected and len(selected["selection"]["rows"]) > 0:
            idx = selected["selection"]["rows"][0]
            row = display_holdings.iloc[idx]
            st.divider()
            st.subheader(f"Transactions → {row['Scheme']} ({row['Folio']})")
            # ... (keep your existing transaction query logic here)

    st.divider()

    # =====================================================================
    # FIX 2: SIP DEDUPLICATION (Using only columns that exist in your DB)
    # =====================================================================
    st.subheader("🔄 All SIPs (Deduplicated)")

    with get_conn() as conn:
        # 1. Load BSE XSIP (The Master)
        bse_sip = pd.read_sql("""
                SELECT amc_name, scheme_name, installments_amt, status, frequency_type, 
                       'BSE' as source 
                FROM bse_sip WHERE client_code = ?
            """, conn, params=(client_code,))

        # 2. Load CAMS SIP
        cams_wbr49_sip = pd.read_sql("""
                SELECT scheme as scheme_name, auto_amount as installments_amt, 
                       periodicity as frequency_type, 
                       CASE WHEN cease_date IS NULL OR cease_date = '' THEN 'Active' ELSE 'Ceased' END as status,
                       'CAMS' as source
                FROM cams_wbr49_sip WHERE folio_no IN (SELECT foliochk FROM cams_wbr9_folio  WHERE pan_no = ?)
            """, conn, params=(pan,))

        # 3. Load KFin SIP
        kfin_mfsd243_sip = pd.read_sql("""
                SELECT scheme_name, amount as installments_amt, frequency as frequency_type, 
                       status, 'KFin' as source
                FROM kfin_mfsd243_sip WHERE folio IN (SELECT folio FROM kfin_mfsd211_folio WHERE pan_number = ?)
            """, conn, params=(pan,))


    # --- Deduplication Logic (BSE Priority) ---
    # Since we don't have scheme_code/folio_no reliably in all tables,
    # we match on Scheme Name + Amount + Frequency
    def _make_safe_key(df):
        return (
                df["scheme_name"].fillna("").str.strip().str.upper() + "|" +
                df["installments_amt"].astype(str) + "|" +
                df["frequency_type"].fillna("").str.strip().str.upper()
        )


    frames_to_keep = []
    bse_keys = set()

    if not bse_sip.empty:
        bse_sip["_match_key"] = _make_safe_key(bse_sip)
        bse_keys = set(bse_sip["_match_key"])
        frames_to_keep.append(bse_sip)

    if not cams_wbr49_sip.empty:
        cams_wbr49_sip["_match_key"] = _make_safe_key(cams_wbr49_sip)
        cams_direct = cams_wbr49_sip[~cams_wbr49_sip["_match_key"].isin(bse_keys)].copy()
        if not cams_direct.empty:
            cams_direct["source"] = "CAMS (Direct)"
            frames_to_keep.append(cams_direct)

    if not kfin_mfsd243_sip.empty:
        kfin_mfsd243_sip["_match_key"] = _make_safe_key(kfin_mfsd243_sip)
        kfin_direct = kfin_mfsd243_sip[~kfin_mfsd243_sip["_match_key"].isin(bse_keys)].copy()
        if not kfin_direct.empty:
            kfin_direct["source"] = "KFin (Direct)"
            frames_to_keep.append(kfin_direct)

    if frames_to_keep:
        final_sips = pd.concat(frames_to_keep, ignore_index=True)
        final_sips = final_sips.drop(columns=["_match_key"], errors='ignore')

        # Summary
        active = len(final_sips[final_sips['status'].str.contains('Active', na=False, case=False)])
        total_monthly = final_sips['installments_amt'].sum()

        s1, s2, s3 = st.columns(3)
        s1.metric("Total SIPs", len(final_sips))
        s2.metric("Active SIPs", active)
        s3.metric("Monthly Commitment", format_currency(total_monthly))

        st.dataframe(
            final_sips[['source', 'scheme_name', 'installments_amt', 'frequency_type', 'status']].sort_values('source'),
            use_container_width=True,
            hide_index=True,
            column_config={
                "installments_amt": st.column_config.NumberColumn("Amount", format="₹ %.2f"),
                "source": st.column_config.TextColumn("Source")
            }
        )
    else:
        st.info("No SIP records found.")

# ==================== 💰 Brokerage Report ====================

elif mode == "💰 Brokerage Report":
    st.header("💰 Brokerage Report")
    st.caption(
        "File-reported brokerage (CAMS + KFin), AMC names resolved the same way as your Dashboard (AMFI-canonical via "
        "ISIN)."
    )

    data = load_brokerage_report(get_conn)
    merged = data["merged"]
    detail = data["detail"]

    if merged.empty and detail.empty:
        st.info("No brokerage data yet. Upload CAMS/KFin brokerage files and log manual entries first.")
        st.stop()

    # ── Top summary cards ──
    total_file = merged["file_amount"].sum() if not merged.empty else 0.0
    total_manual = merged["manual_amount"].sum() if not merged.empty else 0.0
    total_variance = total_file - total_manual
    pending_count = (merged["status"] == "⚠️ Not yet received").sum() if not merged.empty else 0

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("📄 Total File Brokerage", format_brokerage_inr(total_file))
    c2.metric("🏦 Total Received (Manual)", format_brokerage_inr(total_manual))
    c3.metric("📊 Variance", format_brokerage_inr(total_variance))
    c4.metric("⚠️ Pending AMC-Months", int(pending_count))

    st.divider()

    # ════════════════════════════════════════════════════════════
    # SECTION 3 — Record a manual brokerage receipt (AMC dropdown)
    # ════════════════════════════════════════════════════════════
    st.subheader("✍️ Record Manual Brokerage Receipt")

    # AMC options = every AMC seen in resolved file data, so manual entries
    # always line up with bifurcation/drilldown — no typos creating AMC
    # names that never match a file row. "Add new AMC" covers the case
    # where you've received brokerage for an AMC with no file data yet.
    known_amcs = sorted(detail["amc"].dropna().unique()) if not detail.empty else []
    amc_dropdown_options = known_amcs + ["➕ Add new AMC..."]

    with st.form("manual_brokerage_form", clear_on_submit=True):
        mc1, mc2, mc3, mc4 = st.columns(4)
        with mc1:
            m_amc_choice = st.selectbox("AMC Name", amc_dropdown_options)
        with mc2:
            m_month = st.selectbox("Month", [f"{i:02d}" for i in range(1, 13)])
        with mc3:
            m_year = st.number_input("Year", min_value=2015, max_value=2100, value=pd.Timestamp.now().year)
        with mc4:
            m_amount = st.number_input("Amount (Rs)", min_value=0.0, step=100.0)

        m_amc_new = ""
        if m_amc_choice == "➕ Add new AMC...":
            m_amc_new = st.text_input("New AMC Name")

        m_notes = st.text_input("Notes (optional)")
        submitted = st.form_submit_button("➕ Add Entry")

        if submitted:
            m_amc_final = m_amc_new.strip() if m_amc_choice == "➕ Add new AMC..." else m_amc_choice
            if not m_amc_final:
                st.error("AMC name is required.")
            else:
                with get_conn() as conn:
                    conn.execute('''
                            INSERT INTO monthly_brokerage (amc, month, year, amount, notes)
                            VALUES (?, ?, ?, ?, ?)
                            ON CONFLICT(amc, month, year) DO UPDATE SET
                                amount = excluded.amount,
                                notes = excluded.notes,
                                timestamp = CURRENT_TIMESTAMP
                        ''', (m_amc_final, m_month, int(m_year), float(m_amount), m_notes.strip() or None))
                st.cache_data.clear()
                st.success(f"Logged {format_brokerage_inr(m_amount)} for {m_amc_final} ({m_month}-{m_year}).")
                st.rerun()

    # ── Existing manual log (view + delete) ──
    with st.expander("📜 View / Delete Manual Entry Log", expanded=False):
        with get_conn() as conn:
            log_df = pd.read_sql(
                "SELECT amc, month, year, amount, notes, timestamp FROM monthly_brokerage ORDER BY year DESC, month DESC",
                conn
            )

        if log_df.empty:
            st.info("No manual entries logged yet.")
        else:
            st.dataframe(
                log_df, width='stretch', hide_index=True,
                column_config={"amount": st.column_config.NumberColumn(format="₹ %.2f")}
            )

            st.divider()
            st.caption("Delete an entry")
            del1, del2, del3 = st.columns([3, 2, 2])

            log_df["_label"] = (
                    log_df["amc"] + " — " + log_df["month"].astype(str) + "/" + log_df["year"].astype(str)
                    + " (" + log_df["amount"].apply(format_brokerage_inr) + ")"
            )
            with del1:
                entry_to_delete = st.selectbox(
                    "Select entry to delete", log_df["_label"].tolist(), key="brok_delete_select"
                )
            with del2:
                confirm_delete = st.checkbox("Confirm", key="brok_delete_confirm")
            with del3:
                if st.button("🗑️ Delete Entry", key="brok_delete_btn", disabled=not confirm_delete):
                    row = log_df[log_df["_label"] == entry_to_delete].iloc[0]
                    with get_conn() as conn:
                        conn.execute(
                            "DELETE FROM monthly_brokerage WHERE amc = ? AND month = ? AND year = ?",
                            (row["amc"], row["month"], int(row["year"]))
                        )
                    st.cache_data.clear()
                    st.success(f"Deleted entry: {entry_to_delete}")
                    st.rerun()

    # ════════════════════════════════════════════════════════════
    # SECTION 2 — AMC-wise bifurcation (with month filter)
    # ════════════════════════════════════════════════════════════
    st.subheader("🏢 AMC-wise Bifurcation")

    if not merged.empty:
        available_months = sorted(merged["month"].dropna().unique(), reverse=True)
        bif_month_filter = st.multiselect(
            "📅 Month(s)", available_months, default=available_months,
            key="brok_bif_month_filter"
        )

        merged_view = merged[merged["month"].isin(bif_month_filter)]

        amc_summary = (
            merged_view.groupby("amc")[["file_amount", "manual_amount", "variance"]]
            .sum()
            .reset_index()
            .sort_values("file_amount", ascending=False)
        )
        amc_summary["status"] = amc_summary.apply(
            lambda r: "⚠️ Pending" if r["manual_amount"] == 0 and r["file_amount"] > 0
            else ("✅ Matched" if abs(r["variance"]) < 1 else "🔶 Mismatch"),
            axis=1
        )

        display_amc = amc_summary.rename(columns={
            "amc": "AMC", "file_amount": "File Brokerage",
            "manual_amount": "Received (Manual)", "variance": "Variance", "status": "Status"
        })
        st.dataframe(
            display_amc, width='stretch', hide_index=True,
            column_config={
                "File Brokerage": st.column_config.NumberColumn(format="₹ %.2f"),
                "Received (Manual)": st.column_config.NumberColumn(format="₹ %.2f"),
                "Variance": st.column_config.NumberColumn(format="₹ %.2f"),
            }
        )

        if not amc_summary.empty:
            chart_df = amc_summary.copy()
            chart_df["file_amount"] = pd.to_numeric(chart_df["file_amount"], errors="coerce").fillna(0.0)
            chart_df["manual_amount"] = pd.to_numeric(chart_df["manual_amount"], errors="coerce").fillna(0.0)

            chart_long = chart_df.melt(
                id_vars="amc",
                value_vars=["file_amount", "manual_amount"],
                var_name="type",
                value_name="amount"
            )
            chart_long["type"] = chart_long["type"].map({
                "file_amount": "File Brokerage",
                "manual_amount": "Received (Manual)"
            })

            fig = px.bar(
                chart_long, x="amc", y="amount", color="type",
                barmode="group", title="File vs Received, by AMC",
                labels={"amount": "Amount (₹)", "amc": "AMC", "type": "Type"}
            )
            fig = theme_plotly(fig, dark)
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("No data for the selected month(s).")
    else:
        st.info("No file brokerage data parsed yet.")

    st.divider()

    # ════════════════════════════════════════════════════════════
    # SECTION 3 — AMC Drilldown: client-level detail
    #   - "All" AMC option
    #   - month filter
    #   - RTA filter REMOVED (RTA still shown as a column in the table)
    # ════════════════════════════════════════════════════════════
    st.subheader("🔍 AMC Drilldown — Client-level Detail")

    all_amcs = sorted(detail["amc"].dropna().unique()) if not detail.empty else []
    if not all_amcs:
        st.info("No detail rows available.")
    else:
        amc_options = ["All"] + all_amcs
        selected_amc = st.selectbox("Select AMC", amc_options, key="brok_drilldown_amc")

        if selected_amc == "All":
            amc_detail = detail.copy()
        else:
            amc_detail = detail[detail["amc"] == selected_amc].copy()

        dc1, dc2 = st.columns([2, 2])
        with dc1:
            available_drilldown_months = sorted(amc_detail["month"].dropna().unique(), reverse=True)
            month_f = st.multiselect(
                "📅 Month(s)", available_drilldown_months, default=available_drilldown_months,
                key="brok_drilldown_month"
            )
        with dc2:
            client_search = st.text_input("🔍 Search Client / Folio", "", key="brok_drilldown_search")

        amc_detail = amc_detail[amc_detail["month"].isin(month_f)]
        if client_search.strip():
            mask = (
                    amc_detail["client"].astype(str).str.contains(client_search, case=False, na=False)
                    | amc_detail["folio"].astype(str).str.contains(client_search, case=False, na=False)
            )
            amc_detail = amc_detail[mask]

        amc_total = amc_detail["brokerage_amount"].sum()
        label = "All AMCs" if selected_amc == "All" else selected_amc
        st.metric(f"💰 Total Brokerage — {label}", format_brokerage_inr(amc_total))

        detail_cols = [
            "rta", "client", "folio", "scheme_code", "txn_date",
            "txn_amount", "brokerage_pct", "brokerage_amount", "brokerage_type"
        ]
        detail_cols = [c for c in detail_cols if c in amc_detail.columns]
        display_detail = amc_detail[detail_cols].rename(columns={
            "rta": "RTA", "client": "Client", "folio": "Folio", "scheme_code": "Scheme Code",
            "txn_date": "Date", "txn_amount": "Txn Amount", "brokerage_pct": "Brokerage %",
            "brokerage_amount": "Brokerage Amount", "brokerage_type": "Type",
        })
        st.dataframe(
            display_detail.sort_values("Date", ascending=False) if "Date" in display_detail.columns else display_detail,
            width='stretch', hide_index=True,
            column_config={
                "Txn Amount": st.column_config.NumberColumn(format="₹ %.2f"),
                "Brokerage %": st.column_config.NumberColumn(format="%.4f"),
                "Brokerage Amount": st.column_config.NumberColumn(format="₹ %.2f"),
            }
        )
        st.caption(f"Showing {len(amc_detail):,} brokerage records for {label}")

        if not amc_detail.empty:
            csv = display_detail.to_csv(index=False).encode("utf-8")
            st.download_button(
                f"⬇️ Download {label} Brokerage Detail (CSV)",
                csv, f"brokerage_{label.replace(' ', '_')}.csv", "text/csv",
                key="brok_drilldown_download"
            )

    st.divider()





# ==================== ⚙️ ADMIN PANEL ====================
elif mode == "⚙️ Admin Panel":
    st.header("⚙️ Admin Panel")

    tab_upload, tab_raw = st.tabs(["📤 Upload Data", "📄 View Raw Data"])

    # ---------- UPLOAD TAB ----------
    with tab_upload:
        data_manager.render_data_manager()

    # ---------- RAW DATA TAB ----------
    with tab_raw:
        st.subheader("📄 Raw Data Explorer")
        st.caption("View raw uploaded data directly from the database.")

        source = st.radio(
            "Select Data Source",
            ["BSE", "CAMS", "KFinTech"],
            horizontal=True,
            key="raw_source"
        )

        if source == "BSE":
            data_type = st.radio(
                "Select Data Type",
                ["Bse Client Master", "BSE SiP", "BSE Scheme Master"],
                horizontal=True,
                key="raw_bse_type"
            )
            table_map = {
                "Bse Client Master": "bse_client_master",
                "BSE SiP": "bse_sip",
                "BSE Scheme Master": "bse_scheme_master"
            }
            table = table_map[data_type]

        elif source == "CAMS":
            data_type = st.radio(
                "Select Data Type",
                ["Folio Master", "Transactions", "SIP Master", "AUM", "Brokerage"],
                horizontal=True,
                key="raw_cams_type"
            )
            table_map = {
                "Folio Master": "cams_wbr9_folio",
                "Transactions": "cams_wbr2_transaction",
                "SIP Master": "cams_wbr49_sip",
                "AUM": "cams_wbr4_aum",
                "Brokerage": "cams_wbr77_brokerage"
            }
            table = table_map[data_type]

        else:
            data_type = st.radio(
                "Select Data Type",
                ["Folio Master", "Transactions", "SIP Master", "AUM", "Brokerage"],
                horizontal=True,
                key="raw_kfin_type"
            )
            table_map = {
                "Folio Master": "kfin_mfsd211_folio",
                "Transactions": "kfin_mfsd201_transaction",
                "SIP Master": "kfin_mfsd243_sip",
                "AUM": "kfin_mfsd203_aum",
                "Brokerage": "kfin_mfsd205_brokerage"
            }
            table = table_map[data_type]

        st.divider()

        with get_conn() as conn:
            total_rows = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

        st.metric(f"Total Records in {table}", f"{total_rows:,}")

        if total_rows > 0:
            df_raw = load_table_summary(table)
            if not df_raw.empty:
                st.dataframe(df_raw, width='stretch', hide_index=True)

                csv = df_raw.to_csv(index=False).encode("utf-8")
                st.download_button(
                    label="⬇️ Download Raw Data (CSV)",
                    data=csv,
                    file_name=f"{source}_{data_type.replace(' ', '_')}_raw.csv",
                    mime="text/csv",
                )
            else:
                st.info("No data to display.")
        else:
            st.info(f"No data in `{table}` yet. Upload data in the Upload Data tab.")

        # DB Stats at bottom
        st.divider()
        st.subheader("🗄️ Database Stats")
        stats = load_db_stats()

        cols = st.columns(3)
        categories = {
            "BSE": ["bse_client_master", "bse_sip", "bse_scheme_master"],
            "CAMS": ["cams_wbr4_aum", "cams_wbr9_folio", "cams_wbr2_transaction", "cams_wbr49_sip",
                     "cams_wbr77_brokerage"],
            "KFinTech": ["kfin_mfsd203_aum", "kfin_mfsd211_folio", "kfin_mfsd201_transaction", "kfin_mfsd243_sip",
                         "kfin_mfsd205_brokerage"],
        }
        for i, (cat, tables) in enumerate(categories.items()):
            with cols[i]:
                st.markdown(f"**{cat}**")
                for t in tables:
                    st.caption(f"{t}: **{stats.get(t, 0):,}**")

        with cols[0]:
            st.markdown("**Other**")
            st.caption(f"monthly_brokerage: **{stats.get('monthly_brokerage', 0):,}**")
            st.caption(f"amc_code_map: **{stats.get('amc_code_map', 0):,}**")
