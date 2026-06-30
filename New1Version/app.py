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

import pandas as pd
import requests
import streamlit as st

import data_manager
from init_db import init_db, get_conn
from theme_patch import THEME_WATCHER_JS

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
        print("Creating the DB.")
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
            -> cams_folio.foliochk / kfin_folio.folio   (get product code)
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

    # ---- CAMS brokerage -> cams_folio (get product code) -> Channel_Partner_Code -> ISIN ----
    cams_sql = f"""
        SELECT
            cb.proc_date              AS proc_date,
            cb.brokerage_acrual_month AS accrual_month,
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
        FROM cams_brokerage cb
        LEFT JOIN cams_folio cf
            ON UPPER(TRIM(cb.folio_no)) = UPPER(TRIM(cf.foliochk))
        LEFT JOIN ({bse_dedup}) sm
            ON UPPER(TRIM(cf.product)) = sm.cp_code
    """

    # ---- KFin brokerage -> kfin_folio (get product_code) -> Channel_Partner_Code -> ISIN ----
    kfin_sql = f"""
        SELECT
            kb.process_date        AS proc_date,
            NULL                   AS accrual_month,
            kb.investor_name       AS client,
            kb.account_number      AS folio,
            kb.scheme_code         AS scheme_code,
            kb.transaction_number  AS txn_no,
            kb.amount_rs           AS txn_amount,
            kb.percentage          AS brokerage_pct,
            kb.brokerage_rs        AS brokerage_amount,
            kb.brokerage_type      AS brokerage_type,
            kf.product_code        AS folio_product_code,
            sm.ISIN                AS isin,
            sm.bse_amc_name        AS bse_amc_name,
            'KFinTech'             AS rta
        FROM kfin_brokerage kb
        LEFT JOIN kfin_folio kf
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
    FROM cams_folio f
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
    FROM kfin_folio f
    INNER JOIN (
        SELECT 
            td_acno AS folio_id,
            fmcode  AS product_code,
            SUM(td_units) AS total_units
        FROM kfin_transactions
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


def get_folio_nav_summary(get_conn, force_reload: bool = False) -> dict:
    """Quick stats for Streamlit metrics. Reads from saved file via get_all_folios_with_isin_and_nav."""
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
            FROM kfin_transactions 
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
            FROM kfin_transactions
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
            "clients", "bse_xsip", "bse_scheme_master",
            "cams_folio", "cams_transactions", "cams_sip", "cams_aum", "cams_brokerage",
            "kfin_folio", "kfin_transactions", "kfin_sip", "kfin_aum", "kfin_brokerage",
            "monthly_brokerage", "amc_code_map"
        ]
        for t in tables:
            try:
                stats[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except:
                stats[t] = 0
    return stats


@st.cache_data(ttl=60, show_spinner=False)
def load_dashboard_summary() -> dict:
    """Load key metrics for dashboard. Uses exact KFin schema: Folio, td_acno, td_amt, Fund."""
    summary = {}
    with get_conn() as conn:
        # ── Clients ──
        summary["total_clients"] = conn.execute("SELECT COUNT(*) FROM clients").fetchone()[0]

        # ── BSE XSIP ──
        summary["total_xsip"] = conn.execute("SELECT COUNT(*) FROM bse_xsip").fetchone()[0]
        summary["active_xsip"] = conn.execute(
            "SELECT COUNT(*) FROM bse_xsip WHERE LOWER(COALESCE(status, '')) LIKE '%active%'"
        ).fetchone()[0]

        # ── CAMS ──
        summary["cams_folios"] = conn.execute(
            "SELECT COUNT(DISTINCT foliochk) FROM cams_folio"
        ).fetchone()[0]
        summary["cams_txns"] = conn.execute("SELECT COUNT(*) FROM cams_transactions").fetchone()[0]
        summary["cams_sips"] = conn.execute("SELECT COUNT(*) FROM cams_sip").fetchone()[0]
        summary["cams_aum"] = conn.execute(
            "SELECT COALESCE(SUM(rupee_bal), 0) FROM cams_folio"
        ).fetchone()[0]
        summary["cams_brokerage"] = conn.execute(
            "SELECT COALESCE(SUM(brkage_amt), 0) FROM cams_brokerage"
        ).fetchone()[0]

        # ── KFinTech ──
        # Folio Master: Folio, Fund, Product Code, Investor Name
        summary["kfin_folios"] = conn.execute(
            "SELECT COUNT(DISTINCT Folio) FROM kfin_folio"
        ).fetchone()[0]
        summary["kfin_txns"] = conn.execute("SELECT COUNT(*) FROM kfin_transactions").fetchone()[0]
        summary["kfin_sips"] = conn.execute("SELECT COUNT(*) FROM kfin_sip").fetchone()[0]

        # KFin AUM: sum td_amt grouped by td_acno (folio) from MFSD201
        try:
            kfin_aum_result = conn.execute("""
                SELECT COALESCE(SUM(inner_sum), 0) FROM (
                    SELECT td_acno, SUM(td_amt) as inner_sum 
                    FROM kfin_transactions 
                    GROUP BY td_acno
                )
            """).fetchone()[0]
            summary["kfin_aum"] = float(kfin_aum_result) if kfin_aum_result else 0.0
        except Exception as e:
            log.warning("KFin AUM calculation failed: %s", e)
            summary["kfin_aum"] = 0.0

        summary["kfin_brokerage"] = conn.execute(
            "SELECT COALESCE(SUM(brokerage_rs), 0) FROM kfin_brokerage"
        ).fetchone()[0]

        # ── Totals ──
        summary["total_aum"] = summary["cams_aum"] + summary["kfin_aum"]
        summary["total_brokerage"] = summary["cams_brokerage"] + summary["kfin_brokerage"]

        # ── AMC counts ──
        summary["cams_amcs"] = conn.execute(
            "SELECT COUNT(DISTINCT amc_code) FROM cams_folio WHERE COALESCE(amc_code, '') != ''"
        ).fetchone()[0]

        # KFin AMCs: count distinct Fund from kfin_folio
        summary["kfin_amcs"] = conn.execute(
            "SELECT COUNT(DISTINCT Fund) FROM kfin_folio WHERE COALESCE(Fund, '') != ''"
        ).fetchone()[0]

        summary["bse_schemes"] = conn.execute(
            "SELECT COUNT(*) FROM bse_scheme_master"
        ).fetchone()[0]

    return summary


@st.cache_data(ttl=60, show_spinner=False)
def load_amc_breakdown() -> pd.DataFrame:
    """AMC-wise AUM and folio summary. KFin uses Fund column, td_acno for join."""
    with get_conn() as conn:
        # ── CAMS ──
        cams_aum_df = pd.read_sql("""
            SELECT amc_code as amc, 
                   COALESCE(SUM(rupee_bal), 0) as aum
            FROM cams_folio 
            WHERE COALESCE(amc_code, '') != ''
            GROUP BY amc_code
        """, conn)

        cams_folio_df = pd.read_sql("""
            SELECT amc_code as amc,
                   COUNT(DISTINCT foliochk) as folios,
                   COUNT(*) as records
            FROM cams_folio
            WHERE COALESCE(amc_code, '') != ''
            GROUP BY amc_code
        """, conn)

        if cams_aum_df.empty and cams_folio_df.empty:
            cams_combined = pd.DataFrame()
        elif cams_aum_df.empty:
            cams_combined = cams_folio_df.copy()
            cams_combined["aum"] = 0.0
        elif cams_folio_df.empty:
            cams_combined = cams_aum_df.copy()
            cams_combined["folios"] = 0
            cams_combined["records"] = 0
        else:
            cams_combined = cams_folio_df.merge(cams_aum_df, on="amc", how="outer").fillna(0)

        if not cams_combined.empty:
            cams_combined["rta"] = "CAMS"

        # ── KFinTech ──
        # AUM from transactions grouped by Fund (AMC) via td_acno join
        try:
            kfin_aum_df = pd.read_sql("""
                SELECT kf.Fund as amc,
                       COALESCE(SUM(kt.td_amt), 0) as aum
                FROM kfin_transactions kt
                JOIN kfin_folio kf ON kt.td_acno = kf.Folio
                WHERE COALESCE(kf.Fund, '') != ''
                GROUP BY kf.Fund
            """, conn)
        except Exception as e:
            log.warning("KFin AUM breakdown failed: %s", e)
            kfin_aum_df = pd.DataFrame(columns=["amc", "aum"])

        try:
            kfin_folio_df = pd.read_sql("""
                SELECT Fund as amc,
                       COUNT(DISTINCT Folio) as folios,
                       COUNT(*) as records
                FROM kfin_folio
                WHERE COALESCE(Fund, '') != ''
                GROUP BY Fund
            """, conn)
        except Exception as e:
            log.warning("KFin folio breakdown failed: %s", e)
            kfin_folio_df = pd.DataFrame(columns=["amc", "folios", "records"])

        if kfin_aum_df.empty and kfin_folio_df.empty:
            kfin_combined = pd.DataFrame()
        elif kfin_aum_df.empty:
            kfin_combined = kfin_folio_df.copy()
            kfin_combined["aum"] = 0.0
        elif kfin_folio_df.empty:
            kfin_combined = kfin_aum_df.copy()
            kfin_combined["folios"] = 0
            kfin_combined["records"] = 0
        else:
            kfin_combined = kfin_folio_df.merge(kfin_aum_df, on="amc", how="outer").fillna(0)

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
            ("bse_xsip", "upload_batch"),
            ("bse_scheme_master", "upload_batch"),
            ("cams_folio", "upload_batch"),
            ("cams_transactions", "upload_batch"),
            ("cams_sip", "upload_batch"),
            ("cams_aum", "upload_batch"),
            ("cams_brokerage", "upload_batch"),
            ("kfin_folio", "upload_batch"),
            ("kfin_transactions", "upload_batch"),
            ("kfin_sip", "upload_batch"),
            ("kfin_aum", "upload_batch"),
            ("kfin_brokerage", "upload_batch"),
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

from theme_patch import render_theme

st.markdown(render_theme(dark), unsafe_allow_html=True)

# ==================== Navigation ====================
nav_cols = st.columns([1])  # Changed to 4 columns
nav_options = ["⚙️ Admin Panel"]
nav_keys = ["nav_admin"]

if "nav_mode" not in st.session_state:
    st.session_state["nav_mode"] = "📊 Admin Panel"

for i, (opt, key) in enumerate(zip(nav_options, nav_keys)):
    with nav_cols[i]:
        btn_type = "primary" if st.session_state["nav_mode"] == opt else "secondary"
        label = opt.split(" ")[1]
        if st.button(label, key=key, type=btn_type, use_container_width=True):
            st.session_state["nav_mode"] = opt
            st.rerun()

mode = st.session_state.get("nav_mode", "📊 Admin Panel")

if mode == "⚙️ Admin Panel":
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
                ["Clients", "XSIP", "Scheme Master"],
                horizontal=True,
                key="raw_bse_type"
            )
            table_map = {
                "Clients": "clients",
                "XSIP": "bse_xsip",
                "Scheme Master": "bse_scheme_master"
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
                "Folio Master": "cams_folio",
                "Transactions": "cams_transactions",
                "SIP Master": "cams_sip",
                "AUM": "cams_aum",
                "Brokerage": "cams_brokerage"
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
                "Folio Master": "kfin_folio",
                "Transactions": "kfin_transactions",
                "SIP Master": "kfin_sip",
                "AUM": "kfin_aum",
                "Brokerage": "kfin_brokerage"
            }
            table = table_map[data_type]

        st.divider()

        # with get_conn() as conn:
        #     total_rows = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        #
        # st.metric(f"Total Records in {table}", f"{total_rows:,}")
        #
        # if total_rows > 0:
        #     df_raw = load_table_summary(table)
        #     if not df_raw.empty:
        #         st.dataframe(df_raw, width='stretch', hide_index=True)
        #
        #         csv = df_raw.to_csv(index=False).encode("utf-8")
        #         st.download_button(
        #             label="⬇️ Download Raw Data (CSV)",
        #             data=csv,
        #             file_name=f"{source}_{data_type.replace(' ', '_')}_raw.csv",
        #             mime="text/csv",
        #         )
        #     else:
        #         st.info("No data to display.")
        # else:
        #     st.info(f"No data in `{table}` yet. Upload data in the Upload Data tab.")

        # DB Stats at bottom
        st.divider()
        # st.subheader("🗄️ Database Stats")
        # stats = load_db_stats()
        #
        # cols = st.columns(3)
        # categories = {
        #     "BSE": ["clients", "bse_xsip", "bse_scheme_master"],
        #     "CAMS": ["cams_folio", "cams_transactions", "cams_sip", "cams_aum", "cams_brokerage"],
        #     "KFinTech": ["kfin_folio", "kfin_transactions", "kfin_sip", "kfin_aum", "kfin_brokerage"],
        # }
        # for i, (cat, tables) in enumerate(categories.items()):
        #     with cols[i]:
        #         st.markdown(f"**{cat}**")
        #         for t in tables:
        #             st.caption(f"{t}: **{stats.get(t, 0):,}**")
        #
        # with cols[0]:
        #     st.markdown("**Other**")
        #     st.caption(f"monthly_brokerage: **{stats.get('monthly_brokerage', 0):,}**")
        #     st.caption(f"amc_code_map: **{stats.get('amc_code_map', 0):,}**")
