"""
core/dashboard.py
=================
Dashboard / admin stats loaders. Pure: no Streamlit.
"""
from __future__ import annotations
import logging
import pandas as pd
from core.db import get_conn

log = logging.getLogger(__name__)

def get_sip_counts_by_source() -> dict:
    def _clean_regn(val):
        if pd.isna(val):
            return ""
        s = str(val).strip().upper()
        s = s.replace(".0", "") if s.endswith(".0") else s
        return re.sub(r'[^A-Z0-9]', '', s)

    active_statuses = ["ACTIVE", "LIVE SIP", "REGISTERED"]

    with get_conn() as conn:
        bse = pd.read_sql("SELECT status, xsip_regn_no AS regn FROM bse_sip", conn)
        cams = pd.read_sql("""
            SELECT
                CASE WHEN cease_date IS NULL OR cease_date = '' THEN 'Active' ELSE 'Ceased' END AS status,
                request_ref_no AS regn
            FROM cams_wbr49_sip
        """, conn)
        kfin = pd.read_sql("SELECT status, reg_slno AS regn FROM kfin_mfsd243_sip", conn)

    bse = bse[bse["status"].astype(str).str.strip().str.upper().isin(active_statuses)].copy()
    cams = cams[cams["status"].astype(str).str.strip().str.upper().isin(active_statuses)].copy()
    kfin = kfin[kfin["status"].astype(str).str.strip().str.upper().isin(active_statuses)].copy()

    bse["_key"] = bse["regn"].apply(_clean_regn)
    cams["_key"] = cams["regn"].apply(_clean_regn)
    kfin["_key"] = kfin["regn"].apply(_clean_regn)

    bse_keys = set(bse["_key"])
    rta_keys = set(cams["_key"]) | set(kfin["_key"])

    bse_unmatched = bse[~bse["_key"].isin(rta_keys)]
    cams_direct = cams[~cams["_key"].isin(bse_keys)]
    kfin_direct = kfin[~kfin["_key"].isin(bse_keys)]

    return {
        "active_sips_deduped": len(bse) + len(cams_direct) + len(kfin_direct),
        "bse_sips": len(bse),
        "bse_unmatched_in_rta": len(bse_unmatched),
        "cams_direct_sips": len(cams_direct),
        "kfin_direct_sips": len(kfin_direct),
    }


# ══════════════════════════════════════════════════════════════
# EMAIL REPORT BUTTON UI COMPONENT
# ══════════════════════════════════════════════════════════════

def load_table_summary(table: str) -> pd.DataFrame:
    with get_conn() as conn:
        return pd.read_sql(f"SELECT * FROM {table} LIMIT 1000", conn)


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
        summary["cams_txns"] = conn.execute("SELECT COUNT(*) FROM v_cams_txn_active").fetchone()[0]
        summary["cams_sips"] = conn.execute("SELECT COUNT(*) FROM cams_wbr49_sip").fetchone()[0]
        # Invested amount = sum of transaction amounts (same logic as KFin)
        summary["cams_aum"] = conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN trxntype = 'R1' THEN -amount ELSE amount END), 0) FROM v_cams_txn_active"
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
        summary["kfin_txns"] = conn.execute("SELECT COUNT(*) FROM v_kfin_txn_active").fetchone()[0]
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
                                           SELECT COALESCE(SUM(inner_sum), 0)
                                           FROM (SELECT td_acno, SUM(td_amt) as inner_sum
                                                 FROM v_kfin_txn_active
                                                 GROUP BY td_acno)
                                           """).fetchone()[0]
            summary["kfin_aum"] = float(kfin_aum_result) if kfin_aum_result else 0.0
        except Exception as e:
            log.warning("KFin AUM calculation failed: %s", e)
            summary["kfin_aum"] = 0.0

        # ── Totals ──
        summary["total_aum"] = summary["cams_aum"] + summary["kfin_aum"]
        summary["total_brokerage"] = summary["cams_brokerage"] + summary["kfin_brokerage"]

    return summary


def load_amc_breakdown() -> pd.DataFrame:
    """AMC-wise AUM and folio summary. KFin uses Fund column, td_acno for join."""
    with get_conn() as conn:
        # ── CAMS ──
        # ── CAMS ──
        # Folio counts from folio master
        cams_wbr9_folio_df = pd.read_sql("""
            SELECT amc_code as amc,
                   COUNT(DISTINCT foliochk) as folios,
                   COUNT(*) as records
            FROM cams_wbr9_folio 
            WHERE COALESCE(amc_code, '') != ''
            GROUP BY amc_code
        """, conn)

        # AUM from transactions (invested amount) instead of rupee_bal
        cams_txn_aum_df = pd.read_sql("""
                                      SELECT amc_code                                                                 as amc,
                                             COALESCE(SUM(CASE WHEN trxntype = 'R1' THEN -amount ELSE amount END),
                                                      0)                                                              as aum
                                      FROM v_cams_txn_active
                                      WHERE COALESCE(amc_code, '') != ''
                                      GROUP BY amc_code
                                      """, conn)

        if cams_wbr9_folio_df.empty and cams_txn_aum_df.empty:
            cams_combined = pd.DataFrame()
        elif cams_wbr9_folio_df.empty:
            cams_combined = cams_txn_aum_df.copy()
            cams_combined["folios"] = 0
            cams_combined["records"] = 0
        elif cams_txn_aum_df.empty:
            cams_combined = cams_wbr9_folio_df.copy()
            cams_combined["aum"] = 0.0
        else:
            cams_combined = cams_wbr9_folio_df.merge(cams_txn_aum_df, on="amc", how="outer").fillna(0)

        if not cams_combined.empty:
            cams_combined["rta"] = "CAMS"

        # ── KFinTech ──
        # AUM from transactions grouped by Fund (AMC) via td_acno join
        try:
            kfin_mfsd203_aum_df = pd.read_sql("""
                                              SELECT kf.Fund                     as amc,
                                                     COALESCE(SUM(kt.td_amt), 0) as aum
                                              FROM v_kfin_txn_active kt
                                                       JOIN kfin_mfsd211_folio kf ON kt.td_acno = kf.Folio
                                              WHERE COALESCE(kf.Fund, '') != ''
                                              GROUP BY kf.Fund
                                              """, conn)
        except Exception as e:
            log.warning("KFin AUM breakdown failed: %s", e)
            kfin_mfsd203_aum_df = pd.DataFrame(columns=["amc", "aum"])

        try:
            kfin_mfsd211_folio_df = pd.read_sql("""
                                                SELECT Fund                  as amc,
                                                       COUNT(DISTINCT Folio) as folios,
                                                       COUNT(*)              as records
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
aum_recon.ensure_schema()   # txn_overrides table + active-only txn views (needs base tables)
ensure_family_tables()
ensure_monthly_brokerage_schema()   # UNIQUE(amc,month,year) for the upsert


# ==================== BSE SCHEME MASTER AUTO-DOWNLOAD ====================
# Moved to background_worker.py — Streamlit no longer triggers downloads
# on startup. Use the Admin Panel button for manual runs.


# # ==================== CAMS MAILBACK AUTO-SYNC ====================
# def _auto_cams_mailback_sync():
#     """Non-blocking: starts background IMAP sync if needed, once per day."""
#     import cams_mailback_sync as cms

#     if not st.session_state.get("cams_mailback_auto_toggle", True):
#         return

#     last_run = st.session_state.get("cams_mailback_auto_last_run")
#     today = datetime.now().strftime("%Y-%m-%d")
#     if last_run == today:
#         return

#     st.session_state["cams_mailback_auto_last_run"] = today

#     status = cms.get_sync_status()
#     if status["running"]:
#         st.info("⏳ Syncing in background...")
#     elif status["done"]:
#         st.success(status["msg"]) if status["ok"] else st.error(status["msg"])

# Background workers (mailback IMAP poller, NAV schedule, BSE Selenium)