"""
Upload Utilities — import center for BSE, CAMS, and KFinTech data files.
Mirrors legacy "Upload Utilities" nav tab.
"""
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.append(str(Path(__file__).resolve().parent.parent))

from core.db import get_conn, init_db
from core.helpers import format_aum, format_brokerage
from core.parsers_bse import parse_client_master, parse_sip_report
from core.parsers_cams import (
    parse_cams_aum,
    parse_cams_brokerage,
    parse_cams_folio_master,
    parse_cams_sip_master,
    parse_cams_transactions,
)
from core.parsers_kfintech import parse_kfintech_aum, parse_kfintech_brokerage

if not st.session_state.get("db_initialized"):
    init_db()
    st.session_state["db_initialized"] = True

st.header("📥 Upload Utilities")
st.caption("Import data from BSE StAR MF, CAMS, and KFinTech (Karvy).")

rta_tab_bse, rta_tab_cams, rta_tab_kfin = st.tabs(["🟠 BSE", "🟢 CAMS", "🔵 KFinTech"])

# ============================================================
# BSE
# ============================================================
with rta_tab_bse:
    st.subheader("Client Master")
    f1 = st.file_uploader("Client Master Excel", type=["xlsx"], key="client_file")
    replace1 = st.checkbox("Replace existing clients", key="replace_clients")
    if replace1:
        st.warning("Replace mode: ALL existing clients will be deleted and reimported.", icon="⚠️")
    else:
        st.info("Append mode: only new client codes inserted; existing ones skipped.", icon="ℹ️")
    if st.button("Import Clients") and f1:
        with st.spinner("Importing…"):
            ok, msg = parse_client_master(f1, replace1)
            (st.success if ok else st.error)(msg)
            if ok:
                st.cache_data.clear()

    st.divider()

    st.subheader("SIP Report")
    f2 = st.file_uploader("SIP Report Excel", type=["xlsx"], key="sip_file")
    replace2 = st.checkbox("Replace existing holdings", key="replace_holdings")
    if replace2:
        st.warning("Replace mode: ALL existing holdings will be deleted and reimported.", icon="⚠️")
    else:
        st.info("Append mode: new SIPs inserted; duplicates (folio + scheme + start_date) skipped.", icon="ℹ️")
    if st.button("Import SIPs") and f2:
        with st.spinner("Importing…"):
            ok, msg, preview = parse_sip_report(f2, replace2)
            (st.success if ok else st.error)(msg)
            if preview:
                cols = st.columns(3)
                if "active" in preview:
                    cols[0].metric("Active in file", preview["active"])
                if "cancelled" in preview:
                    cols[1].metric("Cancelled (skipped)", preview["cancelled"])
                if "first_order" in preview:
                    cols[2].metric("First Order = Y", preview["first_order"])
            if ok:
                st.cache_data.clear()

# ============================================================
# CAMS
# ============================================================
with rta_tab_cams:
    cams_report_type = st.radio(
        "Report type",
        ["📦 WBR4 — AUM Report", "🔁 WBR2 — Transaction Report", "🗂️ WBR9 — Folio Master",
         "💵 WBR49 — SIP Master", "💰 Brokerage Report"],
        horizontal=True, key="cams_report_toggle",
    )
    st.divider()

    # ---------- WBR4 — AUM ----------
    if cams_report_type == "📦 WBR4 — AUM Report":
        st.markdown("#### WBR4 — AUM Report")
        st.caption("Required columns: `FOLIOCHK`, `RUPEE_BAL`, `SCH_NAME`, `AMC_CODE`, `CLOS_BAL`.")
        aum_file = st.file_uploader("CAMS AUM CSV / TSV", type=["csv", "txt", "tsv"], key="aum_file")
        replace_aum = st.checkbox(
            "Replace existing CAMS AUM", key="replace_aum",
            help="Checked: deletes all CAMS AUM rows, reinserts.\nUnchecked: skips rows already present "
                 "(folio + scheme + rep_date).")
        if replace_aum:
            st.warning("Replace mode: ALL existing CAMS AUM data will be deleted.", icon="⚠️")
        else:
            st.info("Append mode: duplicate (folio + scheme + date) rows skipped.", icon="ℹ️")
        if st.button("📤 Upload AUM") and aum_file:
            with st.spinner("Parsing CAMS AUM…"):
                ok, msg, preview = parse_cams_aum(aum_file, replace_aum)
                (st.success if ok else st.error)(msg)
                if ok and preview:
                    c1, c2, c3, c4 = st.columns(4)
                    c1.metric("📄 Inserted", preview.get("rows", 0))
                    c2.metric("📦 Total AUM", format_aum(preview.get("total_aum", 0)))
                    c3.metric("🔁 Duplicates", preview.get("duplicates", 0))
                    c4.metric("⏭️ Skipped", preview.get("skipped", 0))
                st.cache_data.clear()
        st.divider()
        st.markdown("#### Existing CAMS AUM Summary")
        with get_conn() as conn:
            aum_summary = pd.read_sql(
                """SELECT rep_date AS "Report Date", amc_code AS "AMC Code", COUNT(*) AS "Folios",
                   ROUND(SUM(rupee_bal), 2) AS "Total AUM (Rs)"
                   FROM cams_aum GROUP BY rep_date, amc_code ORDER BY rep_date DESC, 4 DESC""", conn)
            grand_total = conn.execute("SELECT COALESCE(SUM(rupee_bal),0) FROM cams_aum").fetchone()[0]
        if aum_summary.empty:
            st.info("No CAMS AUM data uploaded yet.")
        else:
            aum_summary["Total AUM (Rs)"] = aum_summary["Total AUM (Rs)"].apply(format_aum)
            st.dataframe(aum_summary, use_container_width=True, hide_index=True)
        st.metric("Grand Total AUM", format_aum(grand_total))
        if st.button("⚠️ Clear CAMS AUM"):
            with get_conn() as conn:
                conn.execute("DELETE FROM cams_aum")
            st.warning("CAMS AUM data deleted.")
            st.cache_data.clear()
            st.rerun()

    # ---------- WBR2 — Transactions ----------
    elif cams_report_type == "🔁 WBR2 — Transaction Report":
        st.markdown("#### WBR2 — Transaction Report")
        st.caption(
            "Required columns: `FOLIO_NO`, `TRXNNO`, `AMOUNT`, `UNITS`, `PURPRICE`, `TRADDATE`.")
        r2_file = st.file_uploader("R2 CSV file", type=["csv", "txt", "tsv"], key="r2_file")
        replace_r2 = st.checkbox(
            "Replace ALL existing transaction data", key="replace_r2",
            help="Checked: deletes all cams_transactions rows, then reinserts.\n"
                 "Unchecked: inserts only new transactions (matched on trxn_no + folio).")
        if replace_r2:
            st.warning("Replace mode: ALL existing CAMS transaction data will be deleted.", icon="⚠️")
        else:
            st.info("Append mode: duplicate (trxn_no + folio) rows are silently skipped.", icon="ℹ️")
        if st.button("📤 Upload Transactions") and r2_file:
            with st.spinner("Parsing transactions…"):
                ok, msg, preview = parse_cams_transactions(r2_file, replace_r2)
                (st.success if ok else st.error)(msg)
                if ok and preview:
                    c1, c2, c3, c4 = st.columns(4)
                    c1.metric("Inserted", preview.get("rows", 0))
                    c2.metric("Total Amount", format_aum(preview.get("total_amount", 0)))
                    c3.metric("Folios", preview.get("folios", 0))
                    c4.metric("Schemes", preview.get("schemes", 0))
                st.cache_data.clear()
        st.divider()
        st.markdown("#### Existing Transaction Summary")
        with get_conn() as conn:
            txn_summary = pd.read_sql(
                """SELECT trade_date AS "Date", amc_code AS "AMC", COUNT(*) AS "Transactions",
                   COUNT(DISTINCT folio_no) AS "Folios", ROUND(SUM(amount), 2) AS "Total Amount (Rs)"
                   FROM cams_transactions GROUP BY trade_date, amc_code ORDER BY trade_date DESC LIMIT 50""", conn)
        if txn_summary.empty:
            st.info("No CAMS transaction data uploaded yet.")
        else:
            st.dataframe(txn_summary, use_container_width=True, hide_index=True)
        if st.button("⚠️ Clear All Transactions"):
            with get_conn() as conn:
                conn.execute("DELETE FROM cams_transactions")
            st.warning("All CAMS transaction data deleted.")
            st.cache_data.clear()
            st.rerun()

    # ---------- WBR9 — Folio Master ----------
    elif cams_report_type == "🗂️ WBR9 — Folio Master":
        st.markdown("#### WBR9 — Investor Folio Master")
        st.caption("Required columns: `FOLIOCHK`, `INV_NAME`, `SCH_NAME`, `RUPEE_BAL`, `PAN_NO`.")
        r9_file = st.file_uploader("R9 CSV file", type=["csv", "txt", "tsv"], key="r9_file")
        replace_r9 = st.checkbox(
            "Replace ALL existing folio master data", key="replace_r9",
            help="Checked: deletes all cams_folio_master rows, then reinserts.\n"
                 "Unchecked: inserts only new (folio + scheme) combos.")
        if replace_r9:
            st.warning("Replace mode: ALL existing folio master data will be deleted.", icon="⚠️")
        else:
            st.info("Append mode: duplicate (folio + scheme_name) rows are silently skipped.", icon="ℹ️")
        if st.button("📤 Upload Folio Master") and r9_file:
            with st.spinner("Parsing folio master…"):
                ok, msg, preview = parse_cams_folio_master(r9_file, replace_r9)
                (st.success if ok else st.error)(msg)
                if ok and preview:
                    c1, c2, c3, c4 = st.columns(4)
                    c1.metric("Inserted", preview.get("rows", 0))
                    c2.metric("Total AUM", format_aum(preview.get("total_aum", 0)))
                    c3.metric("Unique Folios", preview.get("unique_folios", 0))
                    c4.metric("Investors", preview.get("unique_investors", 0))
                st.cache_data.clear()
        st.divider()
        st.markdown("#### Existing Folio Master Summary")
        with get_conn() as conn:
            folio_summary = pd.read_sql(
                """SELECT amc_code AS "AMC", COUNT(DISTINCT folio_no) AS "Folios",
                   COUNT(DISTINCT pan_no) AS "Investors", COUNT(*) AS "Scheme Records",
                   ROUND(SUM(rupee_bal), 2) AS "Total AUM (Rs)"
                   FROM cams_folio_master GROUP BY amc_code ORDER BY 5 DESC""", conn)
            total_fm_aum = conn.execute("SELECT COALESCE(SUM(rupee_bal),0) FROM cams_folio_master").fetchone()[0]
        if folio_summary.empty:
            st.info("No CAMS folio master data uploaded yet.")
        else:
            folio_summary["Total AUM (Rs)"] = folio_summary["Total AUM (Rs)"].apply(format_aum)
            st.dataframe(folio_summary, use_container_width=True, hide_index=True)
            st.metric("Grand Total AUM (Folio Master)", format_aum(total_fm_aum))
        if st.button("⚠️ Clear Folio Master Data"):
            with get_conn() as conn:
                conn.execute("DELETE FROM cams_folio_master")
            st.warning("All CAMS folio master data deleted.")
            st.cache_data.clear()
            st.rerun()

    # ---------- WBR49 — SIP Master ----------
    elif cams_report_type == "💵 WBR49 — SIP Master":
        st.markdown("#### WBR49 — SIP Details / Master")
        st.caption("Required columns: `FOLIO_NO`, `AUTO_TRNO`, `AUTO_AMOUNT`, `FROM_DATE`, `TO_DATE`.")
        r49_file = st.file_uploader("R49 CSV file", type=["csv", "txt", "tsv"], key="r49_file")
        replace_r49 = st.checkbox(
            "Replace ALL existing SIP master data", key="replace_r49",
            help="Checked: deletes all cams_sip_master rows, then reinserts.\n"
                 "Unchecked: inserts only new (sip_reg_no + folio) combos.")
        if replace_r49:
            st.warning("Replace mode: ALL existing CAMS SIP master data will be deleted.", icon="⚠️")
        else:
            st.info("Append mode: duplicate (sip_reg_no + folio) rows are silently skipped.", icon="ℹ️")
        if st.button("📤 Upload SIP Master") and r49_file:
            with st.spinner("Parsing SIP master…"):
                ok, msg, preview = parse_cams_sip_master(r49_file, replace_r49)
                (st.success if ok else st.error)(msg)
                if ok and preview:
                    c1, c2, c3, c4 = st.columns(4)
                    c1.metric("Inserted", preview.get("rows", 0))
                    c2.metric("Active SIPs", preview.get("active", 0))
                    c3.metric("Ceased", preview.get("ceased", 0))
                    c4.metric("Completed", preview.get("completed", 0))
                st.cache_data.clear()
        st.divider()
        st.markdown("#### Existing SIP Master Summary")
        with get_conn() as conn:
            sip_summary = pd.read_sql(
                """SELECT amc_code AS "AMC", status AS "Status", COUNT(*) AS "SIPs",
                   COUNT(DISTINCT folio_no) AS "Folios", ROUND(SUM(sip_amount), 2) AS "Total SIP Amount (Rs)"
                   FROM cams_sip_master GROUP BY amc_code, status ORDER BY amc_code, status""", conn)
        if sip_summary.empty:
            st.info("No CAMS SIP master data uploaded yet.")
        else:
            st.dataframe(sip_summary, use_container_width=True, hide_index=True)
        if st.button("⚠️ Clear SIP Master Data"):
            with get_conn() as conn:
                conn.execute("DELETE FROM cams_sip_master")
            st.warning("All CAMS SIP master data deleted.")
            st.cache_data.clear()
            st.rerun()

    # ---------- Brokerage ----------
    else:
        st.markdown("#### CAMS Brokerage File")
        st.caption("Required columns: `FOLIO_NO`, `BRKAGE_AMT`, `AMC_CODE`.")
        cams_file = st.file_uploader("CAMS Brokerage CSV / TSV", type=["csv", "txt", "tsv"], key="cams_brok_file")
        replace_cams = st.checkbox(
            "Replace ALL existing CAMS brokerage data", key="replace_cams_brok",
            help="Checked: deletes all existing CAMS brokerage rows, then reinserts.\n"
                 "Unchecked: only inserts transactions not already present "
                 "(matched on trxn_no + folio + accrual_month).")
        if replace_cams:
            st.warning("Replace mode: ALL existing CAMS brokerage data will be deleted and reimported.", icon="⚠️")
        else:
            st.info("Append mode: duplicate transactions (same trxn_no + folio + month) are silently skipped.",
                    icon="ℹ️")
        if st.button("📤 Upload Brokerage") and cams_file:
            with st.spinner("Parsing brokerage…"):
                ok, msg, preview = parse_cams_brokerage(cams_file, replace_cams)
                (st.success if ok else st.error)(msg)
                if ok and preview:
                    pm1, pm2, pm3, pm4 = st.columns(4)
                    pm1.metric("📄 Inserted", preview.get("rows", 0))
                    pm2.metric("💰 Total Brokerage", format_brokerage(preview.get("total_brokerage", 0)))
                    pm3.metric("🔁 Duplicates skipped", preview.get("duplicates", 0))
                    pm4.metric("⏭️ Invalid skipped", preview.get("skipped", 0))
                    if preview.get("months"):
                        st.info(f"Accrual months in file: **{', '.join(preview['months'])}**")
                st.cache_data.clear()
        st.divider()
        st.markdown("#### Existing CAMS Brokerage Summary")
        with get_conn() as conn:
            cams_summary = pd.read_sql(
                """SELECT accrual_month AS "Month", COUNT(*) AS "Rows", COUNT(DISTINCT folio_no) AS "Folios",
                   ROUND(SUM(brkage_amt), 2) AS "Total Brokerage (Rs)", upload_batch AS "Batch"
                   FROM cams_brokerage GROUP BY accrual_month, upload_batch ORDER BY accrual_month DESC""", conn)
        if cams_summary.empty:
            st.info("No CAMS brokerage data uploaded yet.")
        else:
            st.dataframe(cams_summary, use_container_width=True, hide_index=True)
        if st.button("⚠️ Clear CAMS Brokerage Data"):
            with get_conn() as conn:
                conn.execute("DELETE FROM cams_brokerage")
            st.warning("All CAMS brokerage data deleted.")
            st.cache_data.clear()
            st.rerun()

# ============================================================
# KFINTECH
# ============================================================
with rta_tab_kfin:
    kfin_report_type = st.radio(
        "Report type", ["📦 AUM Report", "💰 Brokerage Report"], horizontal=True, key="kfin_report_toggle",
    )
    st.divider()

    if kfin_report_type == "📦 AUM Report":
        st.markdown("#### KFinTech AUM Report")
        st.caption("Expected columns: `Folio Number`, `Fund Description`, `AUM`, `Balance`, `NAV`, `Report Date`.")
        kfin_file = st.file_uploader("KFinTech AUM CSV / TSV", type=["csv", "txt", "tsv"], key="kfin_aum_file")
        replace_kfin = st.checkbox(
            "Replace existing KFinTech AUM", key="replace_kfin",
            help="Checked: deletes all KFinTech AUM rows, reinserts.\n"
                 "Unchecked: skips duplicate (folio + scheme + date) rows.")
        if replace_kfin:
            st.warning("Replace mode: ALL existing KFinTech AUM data will be deleted.", icon="⚠️")
        else:
            st.info("Append mode: duplicate (folio + scheme + date) rows skipped.", icon="ℹ️")
        if st.button("📤 Upload KFinTech AUM") and kfin_file:
            with st.spinner("Parsing KFinTech AUM…"):
                ok, msg, preview = parse_kfintech_aum(kfin_file, replace_kfin)
                (st.success if ok else st.error)(msg)
                if ok and preview:
                    km1, km2, km3, km4 = st.columns(4)
                    km1.metric("📄 Inserted", preview.get("rows", 0))
                    km2.metric("📦 Total AUM", format_aum(preview.get("total_aum", 0)))
                    km3.metric("🔁 Duplicates", preview.get("duplicates", 0))
                    km4.metric("⏭️ Skipped", preview.get("skipped", 0))
                st.cache_data.clear()
        st.divider()
        st.markdown("#### Existing KFinTech AUM Summary")
        with get_conn() as conn:
            kfin_summary = pd.read_sql(
                """SELECT rep_date AS "Report Date", amc_code AS "AMC Code", COUNT(*) AS "Folios",
                   ROUND(SUM(rupee_bal), 2) AS "Total AUM (Rs)"
                   FROM kfintech_aum GROUP BY rep_date, amc_code ORDER BY rep_date DESC, 4 DESC""", conn)
            grand_total_kf = conn.execute("SELECT COALESCE(SUM(rupee_bal),0) FROM kfintech_aum").fetchone()[0]
        if kfin_summary.empty:
            st.info("No KFinTech AUM data uploaded yet.")
        else:
            kfin_summary["Total AUM (Rs)"] = kfin_summary["Total AUM (Rs)"].apply(format_aum)
            st.dataframe(kfin_summary, use_container_width=True, hide_index=True)
        st.metric("Grand Total AUM", format_aum(grand_total_kf))
        if st.button("⚠️ Clear KFinTech AUM"):
            with get_conn() as conn:
                conn.execute("DELETE FROM kfintech_aum")
            st.warning("KFinTech AUM data deleted.")
            st.cache_data.clear()
            st.rerun()

    else:
        st.markdown("#### KFinTech (Karvy) Brokerage File")
        st.caption("Required columns: `Account Number`, `Brokerage (in Rs.)`, `Fund`.")
        kf_brok_file = st.file_uploader("KFinTech Brokerage CSV / TSV", type=["csv", "txt", "tsv"],
                                        key="kf_brok_file")
        replace_kf_brok = st.checkbox(
            "Replace ALL existing KFinTech brokerage data", key="replace_kf_brok",
            help="Checked: deletes all existing KFinTech brokerage rows, then reinserts.\n"
                 "Unchecked: only inserts transactions not already present "
                 "(matched on trxn_no + folio + accrual_month).")
        if replace_kf_brok:
            st.warning("Replace mode: ALL existing KFinTech brokerage data will be deleted and reimported.",
                       icon="⚠️")
        else:
            st.info("Append mode: duplicate transactions (same trxn_no + folio + month) are silently skipped.",
                    icon="ℹ️")
        if st.button("📤 Upload KFinTech Brokerage") and kf_brok_file:
            with st.spinner("Parsing KFinTech brokerage…"):
                ok, msg, preview = parse_kfintech_brokerage(kf_brok_file, replace_kf_brok)
                (st.success if ok else st.error)(msg)
                if ok and preview:
                    pm1, pm2, pm3, pm4 = st.columns(4)
                    pm1.metric("📄 Inserted", preview.get("rows", 0))
                    pm2.metric("💰 Total Brokerage", format_brokerage(preview.get("total_brokerage", 0)))
                    pm3.metric("🔁 Duplicates skipped", preview.get("duplicates", 0))
                    pm4.metric("⏭️ Invalid skipped", preview.get("skipped", 0))
                    if preview.get("months"):
                        st.info(f"Accrual months in file: **{', '.join(preview['months'])}**")
                st.cache_data.clear()
        st.divider()
        st.markdown("#### Existing KFinTech Brokerage Summary")
        with get_conn() as conn:
            kf_summary = pd.read_sql(
                """SELECT accrual_month AS "Month", COUNT(*) AS "Rows", COUNT(DISTINCT folio_no) AS "Folios",
                   ROUND(SUM(brkage_amt), 2) AS "Total Brokerage (Rs)", upload_batch AS "Batch"
                   FROM kfintech_brokerage GROUP BY accrual_month, upload_batch ORDER BY accrual_month DESC""", conn)
        if kf_summary.empty:
            st.info("No KFinTech brokerage data uploaded yet.")
        else:
            st.dataframe(kf_summary, use_container_width=True, hide_index=True)
        if st.button("⚠️ Clear KFinTech Brokerage Data"):
            with get_conn() as conn:
                conn.execute("DELETE FROM kfintech_brokerage")
            st.warning("All KFinTech brokerage data deleted.")
            st.cache_data.clear()
            st.rerun()
