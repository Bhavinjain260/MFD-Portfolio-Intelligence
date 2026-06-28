"""
MFD Portfolio Intelligence — Minimal Version
Only: Admin Panel (Upload + Raw Data View) + Dashboard (View All Details)
"""

import logging
import re
import warnings

import plotly.express as px
import streamlit as st

import data_manager
from Init import init_db, get_conn

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.WARNING, format="%(levelname)s | %(message)s")
log = logging.getLogger(__name__)

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
    if val is None: return ""
    try:
        if pd.isna(val): return ""
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


import logging
import pandas as pd
from datetime import datetime
from typing import Optional
import requests

log = logging.getLogger(__name__)
AMFI_TEXT_URL = "https://portal.amfiindia.com/spages/NAVAll.txt"


# ==================== AMFI NAV SERVICE ====================

class AMFINavService:
    """Downloads AMFI text once per session and indexes by ISIN for O(1) lookups."""

    _nav_by_isin: dict[str, tuple[float, str]] = {}
    _last_fetch: Optional[datetime] = None

    def refresh(self) -> dict[str, tuple[float, str]]:
        log.info("[AMFI] Starting NAV download from %s", AMFI_TEXT_URL)
        try:
            res = requests.get(AMFI_TEXT_URL, timeout=30)
            res.raise_for_status()
            lines = res.text.splitlines()
            log.info("[AMFI] Downloaded %s lines", len(lines))
        except Exception:
            log.exception("[AMFI] NAV download failed")
            return {}

        nav_map: dict[str, tuple[float, str]] = {}
        skipped = 0
        parsed = 0

        for line in lines:
            line = line.strip()
            if not line or ";" not in line:
                continue
            parts = line.split(";")
            if len(parts) < 6 or parts[0] == "Scheme Code":
                continue

            isin_1 = parts[1].strip()
            isin_2 = parts[2].strip()
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

            if nav > 0:
                parsed += 1
                for isin in (isin_1, isin_2):
                    if isin:
                        nav_map[isin.upper()] = (nav, nav_date)
            else:
                skipped += 1

        self._nav_by_isin = nav_map
        self._last_fetch = datetime.now()
        log.info("[AMFI] NAV indexed: %s ISINs parsed, %s skipped (N.A./0)", parsed, skipped)
        return nav_map

    def get_nav(self, isin: str) -> Optional[tuple[float, str]]:
        if not self._nav_by_isin:
            log.debug("[AMFI] Cache empty, triggering refresh")
            self.refresh()
        if not isin:
            return None
        result = self._nav_by_isin.get(isin.strip().upper())
        log.debug("[AMFI] Lookup ISIN=%s → %s", isin, "HIT" if result else "MISS")
        return result


_amfi = AMFINavService()


# ==================== PUBLIC API ====================

def fetch_nav_by_isin(isin: str) -> Optional[tuple[float, str]]:
    """Single ISIN lookup using shared AMFI index."""
    isin = isin.strip().upper()
    result = _amfi.get_nav(isin)
    if result:
        nav, nav_date = result
        print(f"✅ ISIN {isin} | NAV: ₹{nav} | Date: {nav_date}")
        return nav, nav_date
    else:
        print(f"❌ No NAV found for ISIN: {isin}")
        return None


def get_all_folios_with_isin_and_nav() -> pd.DataFrame:
    """
    Master batch function:
      1. Download AMFI NAV once → index by ISIN
      2. Query CAMS folio master JOIN deduplicated bse_scheme_master (product → Channel Partner Code → ISIN)
      3. Query KFin folio master JOIN deduplicated bse_scheme_master (product_code → Channel Partner Code → ISIN)
         + computed units from kfin_transactions
      4. Map NAV in-memory
      5. Return combined DataFrame
    """
    log.info("=" * 60)
    log.info("[NAV-FLOW] Starting get_all_folios_with_isin_and_nav()")

    # ── Step 1: AMFI NAV Download ──
    log.info("[NAV-FLOW][Step 1] Downloading AMFI NAV index...")
    nav_map = _amfi.refresh()
    log.info("[NAV-FLOW][Step 1] AMFI NAV ready: %s ISINs in index", len(nav_map))

    # ── Step 2: Deduplicated BSE Scheme Master subquery ──
    log.info("[NAV-FLOW][Step 2] Building deduplicated BSE lookup...")
    bse_dedup = """
        SELECT 
            UPPER(TRIM(Channel_Partner_Code)) AS cp_code,
            MAX(ISIN) AS ISIN,
            MAX(Scheme_Name) AS Scheme_Name
        FROM bse_scheme_master
        WHERE Channel_Partner_Code IS NOT NULL AND TRIM(Channel_Partner_Code) != ''
        GROUP BY UPPER(TRIM(Channel_Partner_Code))
    """
    log.debug("[NAV-FLOW][Step 2] BSE dedup SQL:\n%s", bse_dedup)

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
        'CAMS'              AS rta
    FROM cams_folio f
    LEFT JOIN ({bse_dedup}) bsm
        ON UPPER(TRIM(f.product)) = bsm.cp_code
    WHERE f.product IS NOT NULL AND TRIM(f.product) != ''
    """
    log.debug("[NAV-FLOW][Step 3] CAMS SQL:\n%s", cams_sql)

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
    log.debug("[NAV-FLOW][Step 4] KFin SQL:\n%s", kfin_sql)

    # ── Step 5: Execute Queries ──
    log.info("[NAV-FLOW][Step 5] Executing SQL queries...")
    with get_conn() as conn:
        cams_df = pd.read_sql(cams_sql, conn)
        log.info("[NAV-FLOW][Step 5] CAMS rows fetched: %s", len(cams_df))

        # Debug: CAMS product → ISIN mapping stats
        cams_with_product = cams_df["product_code"].notna().sum()
        cams_with_isin = cams_df["isin"].notna().sum()
        log.info("[NAV-FLOW][Step 5] CAMS: %s rows with product_code, %s rows with ISIN (%s%% match)",
                 cams_with_product, cams_with_isin,
                 round(cams_with_isin / cams_with_product * 100, 2) if cams_with_product else 0)

        # Debug: Show sample CAMS mappings
        cams_sample = cams_df.dropna(subset=["product_code"]).head(3)
        for _, row in cams_sample.iterrows():
            log.debug("[NAV-FLOW][Step 5] CAMS SAMPLE | folio=%s | product=%s | isin=%s | scheme=%s",
                      row["folio_id"], row["product_code"], row["isin"], row["scheme_name"])

        kfin_df = pd.read_sql(kfin_sql, conn)
        log.info("[NAV-FLOW][Step 5] KFin rows fetched: %s", len(kfin_df))

        # Debug: KFin product_code → ISIN mapping stats
        kfin_with_product = kfin_df["product_code"].notna().sum()
        kfin_with_isin = kfin_df["isin"].notna().sum()
        log.info("[NAV-FLOW][Step 5] KFin: %s rows with product_code, %s rows with ISIN (%s%% match)",
                 kfin_with_product, kfin_with_isin,
                 round(kfin_with_isin / kfin_with_product * 100, 2) if kfin_with_product else 0)

        # Debug: Show sample KFin mappings
        kfin_sample = kfin_df.dropna(subset=["product_code"]).head(3)
        for _, row in kfin_sample.iterrows():
            log.debug("[NAV-FLOW][Step 5] KFin SAMPLE | folio=%s | product=%s | isin=%s | scheme=%s",
                      row["folio_id"], row["product_code"], row["isin"], row["scheme_name"])

    # ── Step 6: Combine ──
    log.info("[NAV-FLOW][Step 6] Combining CAMS + KFin data...")
    combined = pd.concat([cams_df, kfin_df], ignore_index=True)
    log.info("[NAV-FLOW][Step 6] Combined rows: %s", len(combined))

    # ── Step 7: NAV Lookup ──
    log.info("[NAV-FLOW][Step 7] Mapping ISIN → NAV from AMFI index...")

    def _lookup_nav(isin):
        if pd.isna(isin) or not str(isin).strip():
            return pd.Series([None, None])
        hit = nav_map.get(str(isin).strip().upper())
        return pd.Series(hit) if hit else pd.Series([None, None])

    nav_cols = combined["isin"].apply(_lookup_nav)
    combined["current_nav"] = nav_cols[0]
    combined["nav_date"] = nav_cols[1]

    # Debug: NAV mapping stats
    total_with_isin = combined["isin"].notna().sum()
    total_with_nav = combined["current_nav"].notna().sum()
    log.info("[NAV-FLOW][Step 7] ISIN→NAV mapping: %s with ISIN, %s with NAV (%s%% coverage)",
             total_with_isin, total_with_nav,
             round(total_with_nav / total_with_isin * 100, 2) if total_with_isin else 0)

    # Debug: Show sample NAV lookups
    nav_sample = combined.dropna(subset=["isin"]).head(3)
    for _, row in nav_sample.iterrows():
        log.debug("[NAV-FLOW][Step 7] NAV SAMPLE | isin=%s | nav=%s | date=%s",
                  row["isin"], row["current_nav"], row["nav_date"])

    # ── Step 8: Calculate AUM ──
    log.info("[NAV-FLOW][Step 8] Calculating NAV-based AUM...")
    combined["nav_based_aum"] = combined.apply(
        lambda r: r["units"] * r["current_nav"]
        if pd.notna(r.get("units")) and pd.notna(r["current_nav"])
        else None,
        axis=1
    )

    # Debug: AUM stats
    total_aum = combined["nav_based_aum"].sum()
    log.info("[NAV-FLOW][Step 8] Total NAV-based AUM: ₹%s", f"{total_aum:,.2f}" if pd.notna(total_aum) else "N/A")

    # ── Step 9: Final Flags & Reorder ──
    log.info("[NAV-FLOW][Step 9] Finalizing columns...")
    combined["has_isin"] = combined["isin"].notna()
    combined["has_nav"] = combined["current_nav"].notna()

    front = [
        "rta", "folio_id", "investor_name", "product_code",
        "scheme_name", "isin", "has_isin",
        "current_nav", "nav_date", "has_nav",
        "units", "file_aum", "nav_based_aum"
    ]
    front = [c for c in front if c in combined.columns]
    back = [c for c in combined.columns if c not in front]
    result = combined[front + back]

    log.info("[NAV-FLOW] Complete. Returning %s rows x %s cols", len(result), len(result.columns))
    log.info("=" * 60)

    return result


def get_folio_nav_summary() -> dict:
    """Quick stats for Streamlit metrics."""
    log.info("[NAV-FLOW] Generating folio NAV summary...")

    df = get_all_folios_with_isin_and_nav()
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

        "total_aum": cams_nav_aum + kfin_nav_aum,  # ← ADD THIS
        "cams_aum": cams_nav_aum,
        "cams_file_aum": cams_file_aum,
        "cams_unmatched_nav": cams_unmatched,
        "kfin_aum": kfin_nav_aum,
        "kfin_unmatched_nav": kfin_unmatched,

        "cams_with_nav": int(cams_df["has_nav"].sum()),
        "cams_total": len(cams_df),
        "kfin_with_nav": int(kfin_df["has_nav"].sum()),
        "kfin_total": len(kfin_df),
    }


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
            "clients", "clients_bank", "clients_nominee", "bse_xsip", "bse_scheme_master",
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

# -------------------- THEME --------------------

if "dark_mode" not in st.session_state:
    st.session_state["dark_mode"] = False
dark = st.session_state["dark_mode"]

_bg = "#0e1117" if dark else "#ffffff"
_sbg = "#161b22" if dark else "#f6f8fa"
_text = "#e6edf3" if dark else "#1a1a2e"
_muted = "#8b949e" if dark else "#6b7280"
_border = "#30363d" if dark else "#d0d7de"
_accent = "#58a6ff" if dark else "#2563eb"

from theme_patch import render_theme

st.markdown(render_theme(dark), unsafe_allow_html=True)

# -------------------- COMPACT HEADER --------------------
hdr1, hdr2 = st.columns([6, 1])
with hdr1:
    st.markdown("#### 📊 MFD Portfolio Intelligence")
with hdr2:
    if st.button("🌙" if not dark else "☀️", help="Toggle dark/light mode", use_container_width=True):
        st.session_state["dark_mode"] = not st.session_state["dark_mode"]
        st.rerun()

# Navigation
nav_cols = st.columns([1, 1])
nav_options = ["📊 Dashboard", "⚙️ Admin Panel"]
nav_keys = ["nav_dash", "nav_admin"]
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
                folio_nav_df = get_all_folios_with_isin_and_nav()
                nav_stats = get_folio_nav_summary()
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

    # ── Top metrics row (from base_summary) ──
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("👥 Clients", summary.get("total_clients", 0))
    m2.metric("📋 BSE XSIPs", summary.get("total_xsip", 0))
    m3.metric("✅ Active XSIPs", summary.get("active_xsip", 0))
    m4.metric("🏢 CAMS AMCs", summary.get("cams_amcs", 0))
    m5.metric("🏢 KFinTech AMCs", summary.get("kfin_amcs", 0))

    # ── Second metrics row (from base_summary) ──
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

    try:

        _has_nav_module = True
    except ImportError as e:
        _has_nav_module = False
        # st.warning(f"⚠️ folio_nav_enricher.py not found: {e}")

    if _has_nav_module:
        if "folio_nav_df" in st.session_state:
            df = st.session_state["folio_nav_df"]

            st.divider()
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
                use_container_width=True,
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
    amc_df = load_amc_breakdown()
    if not amc_df.empty:
        amc_df["aum_display"] = amc_df["aum"].apply(format_aum)
        display_df = amc_df[["amc", "rta", "folios", "records", "aum_display"]].rename(
            columns={"amc": "AMC Code", "rta": "RTA", "folios": "Folios",
                     "records": "Records", "aum_display": "AUM"}
        )
        st.dataframe(display_df, use_container_width=True, hide_index=True)

        fig = px.pie(amc_df, values="aum", names="amc", hole=0.4,
                     title="AUM Distribution by AMC")
        fig = theme_plotly(fig, dark)
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("No AMC data uploaded yet. Go to Admin Panel -> Import Data.")

    # ── Recent Uploads ──
    st.divider()
    st.subheader("📤 Recent Uploads")
    uploads_df = load_recent_uploads()
    if not uploads_df.empty:
        st.dataframe(uploads_df, use_container_width=True, hide_index=True)
    else:
        st.info("No uploads yet. Go to Admin Panel to upload data.")


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
                ["Clients", "Client Banks", "Client Nominees", "XSIP", "Scheme Master"],
                horizontal=True,
                key="raw_bse_type"
            )
            table_map = {
                "Clients": "clients",
                "Client Banks": "clients_bank",
                "Client Nominees": "clients_nominee",
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

        with get_conn() as conn:
            total_rows = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

        st.metric(f"Total Records in {table}", f"{total_rows:,}")

        if total_rows > 0:
            df_raw = load_table_summary(table)
            if not df_raw.empty:
                st.dataframe(df_raw, use_container_width=True, hide_index=True)

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
            "BSE": ["clients", "clients_bank", "clients_nominee", "bse_xsip", "bse_scheme_master"],
            "CAMS": ["cams_folio", "cams_transactions", "cams_sip", "cams_aum", "cams_brokerage"],
            "KFinTech": ["kfin_folio", "kfin_transactions", "kfin_sip", "kfin_aum", "kfin_brokerage"],
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
