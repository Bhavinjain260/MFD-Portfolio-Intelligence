"""
MFD Portfolio Intelligence — Minimal Version
Only: Admin Panel (Upload + Raw Data View) + Dashboard (View All Details)
"""

import logging
import re
import warnings
from contextlib import contextmanager

import pandas as pd
import requests
import plotly.express as px
import streamlit as st

import data_manager

from Init import DB_PATH, get_conn, init_db

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


# ==================== AMFI NAV FETCH ====================
AMFI_TEXT_URL = "https://portal.amfiindia.com/spages/NAVAll.txt"

@st.cache_data(ttl=3600, show_spinner=False)
def fetch_amfi_nav_index() -> dict:
    """Fetch AMFI NAV file and build two lookup indexes:
    - by_code: AMFI scheme code (numeric) -> NAV  (for CAMS)
    - by_name: Normalized scheme name -> NAV  (for KFinTech fallback)
    """
    try:
        res = requests.get(AMFI_TEXT_URL, timeout=20)
        res.raise_for_status()
        lines = res.text.splitlines()
    except Exception:
        log.exception("AMFI fetch failed")
        return {"by_code": {}, "by_name": {}}

    nav_by_code: dict[str, float] = {}
    nav_by_name: dict[str, float] = {}

    for line in lines:
        line = line.strip()
        if not line or ";" not in line:
            continue
        parts = line.split(";")
        if len(parts) < 6 or parts[0] == "Scheme Code":
            continue
        code = parts[0].strip()
        name = parts[3].strip()
        try:
            nav = float(parts[4]) if parts[4] not in ("N.A.", "") else 0.0
        except ValueError:
            nav = 0.0
        if nav > 0 and code:
            nav_by_code[code] = nav
            key = _WHITESPACE_RE.sub(" ", name.upper())
            nav_by_name[key] = nav

    return {"by_code": nav_by_code, "by_name": nav_by_name}


def extract_amfi_code(product: str) -> str:
    """Extract AMFI scheme code from CAMS product code.
    Examples: 'L689G' -> '689', 'M1234D' -> '1234', '689' -> '689'
    """
    if not product:
        return ""
    cleaned = re.sub(r"^[A-Za-z]", "", str(product).strip())
    cleaned = re.sub(r"[A-Za-z]$", "", cleaned)
    return cleaned.strip()


def find_nav_by_name(scheme_name: str, nav_by_name: dict[str, float]) -> float:
    """Fuzzy match scheme name against AMFI name index."""
    if not scheme_name:
        return 0.0

    key = _WHITESPACE_RE.sub(" ", str(scheme_name).strip().upper())

    if key in nav_by_name:
        return nav_by_name[key]

    simplified = key
    for suffix in [" GROWTH", " IDCW", " DIVIDEND", " DIRECT", " REGULAR", " PLAN", " OPTION"]:
        simplified = simplified.replace(suffix, "")
    simplified = _WHITESPACE_RE.sub(" ", simplified).strip()

    if simplified in nav_by_name:
        return nav_by_name[simplified]

    for amfi_name, nav in nav_by_name.items():
        if simplified in amfi_name or amfi_name in simplified:
            return nav

    return 0.0

def normalize_folio(folio: str) -> str:
    if not folio: return ""
    try:
        if pd.isna(folio): return ""
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
    """Load key metrics for dashboard."""
    summary = {}
    with get_conn() as conn:
        summary["total_clients"] = conn.execute("SELECT COUNT(*) FROM clients").fetchone()[0]

        summary["total_xsip"] = conn.execute("SELECT COUNT(*) FROM bse_xsip").fetchone()[0]
        summary["active_xsip"] = conn.execute(
            "SELECT COUNT(*) FROM bse_xsip WHERE LOWER(status) LIKE '%active%'"
        ).fetchone()[0]

        summary["cams_folios"] = conn.execute(
            "SELECT COUNT(DISTINCT folio_no) FROM cams_folio"
        ).fetchone()[0]
        summary["cams_txns"] = conn.execute("SELECT COUNT(*) FROM cams_transactions").fetchone()[0]
        summary["cams_sips"] = conn.execute("SELECT COUNT(*) FROM cams_sip").fetchone()[0]
        # CAMS AUM: from Folio Master (WBR9) - rupee_bal
        summary["cams_aum"] = conn.execute(
            "SELECT COALESCE(SUM(rupee_bal), 0) FROM cams_folio"
        ).fetchone()[0]
        summary["cams_brokerage"] = conn.execute(
            "SELECT COALESCE(SUM(brkage_amt), 0) FROM cams_brokerage"
        ).fetchone()[0]

        # KFinTech AUM: calculated from transactions (MFSD201) - sum of td_amt per folio
        summary["kfin_folios"] = conn.execute(
            "SELECT COUNT(DISTINCT folio_no) FROM kfin_folio"
        ).fetchone()[0]
        summary["kfin_txns"] = conn.execute("SELECT COUNT(*) FROM kfin_transactions").fetchone()[0]
        summary["kfin_sips"] = conn.execute("SELECT COUNT(*) FROM kfin_sip").fetchone()[0]
        kfin_aum_result = conn.execute("""
            SELECT COALESCE(SUM(td_amt), 0) FROM (
                SELECT folio_no, SUM(amount) as td_amt 
                FROM kfin_transactions 
                GROUP BY folio_no
            )
        """).fetchone()[0]
        summary["kfin_aum"] = float(kfin_aum_result) if kfin_aum_result else 0.0
        summary["kfin_brokerage"] = conn.execute(
            "SELECT COALESCE(SUM(brokerage), 0) FROM kfin_brokerage"
        ).fetchone()[0]

        summary["total_aum"] = summary["cams_aum"] + summary["kfin_aum"]
        summary["total_brokerage"] = summary["cams_brokerage"] + summary["kfin_brokerage"]

        summary["cams_amcs"] = conn.execute(
            "SELECT COUNT(DISTINCT amc_code) FROM cams_folio WHERE amc_code != ''"
        ).fetchone()[0]

        summary["kfin_amcs"] = conn.execute(
            "SELECT COUNT(DISTINCT fund_code) FROM kfin_folio WHERE fund_code != ''"
        ).fetchone()[0]

        summary["bse_schemes"] = conn.execute(
            "SELECT COUNT(*) FROM bse_scheme_master"
        ).fetchone()[0]

    return summary


@st.cache_data(ttl=60, show_spinner=False)
def load_amc_breakdown() -> pd.DataFrame:
    """AMC-wise AUM and folio summary."""
    with get_conn() as conn:
        cams_aum_df = pd.read_sql("""
            SELECT amc_code as amc, 
                   COALESCE(SUM(rupee_bal), 0) as aum
            FROM cams_folio 
            WHERE amc_code != ''
            GROUP BY amc_code
        """, conn)

        cams_folio_df = pd.read_sql("""
            SELECT amc_code as amc,
                   COUNT(DISTINCT folio_no) as folios,
                   COUNT(*) as records
            FROM cams_folio
            WHERE amc_code != ''
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

        kfin_aum_df = pd.read_sql("""
            SELECT fund_code as amc,
                   COALESCE(SUM(folio_aum), 0) as aum
            FROM (
                SELECT kf.fund_code, kt.folio_no, SUM(kt.amount) as folio_aum
                FROM kfin_transactions kt
                JOIN kfin_folio kf ON kt.folio_no = kf.folio_no
                WHERE kf.fund_code != ''
                GROUP BY kf.fund_code, kt.folio_no
            )
            GROUP BY fund_code
        """, conn)

        kfin_folio_df = pd.read_sql("""
            SELECT fund_code as amc,
                   COUNT(DISTINCT folio_no) as folios,
                   COUNT(*) as records
            FROM kfin_folio
            WHERE fund_code != ''
            GROUP BY fund_code
        """, conn)

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

# -------------------- AMFI NAV SYNC --------------------
if "amfi_nav" not in st.session_state:
    st.session_state["amfi_nav"] = fetch_amfi_nav_index()
    nav_data = st.session_state["amfi_nav"]
    total_nav = len(nav_data["by_code"]) + len(nav_data["by_name"])
    if total_nav == 0:
        st.warning("⚠️ AMFI NAV fetch failed. AUM calculations will show 0.")
    else:
        st.toast(f"✅ AMFI NAV synced: {len(nav_data['by_code']):,} schemes")

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

st.markdown(f"""
<style>
    .stApp {{ background-color: {_bg} !important; }}
    section[data-testid="stSidebar"] {{ background-color: {_sbg} !important; }}
    section[data-testid="stSidebar"] * {{ color: {_text} !important; }}
    .stMarkdown, p, span, div, label,
    [data-testid="stMetricLabel"], [data-testid="stMetricValue"],
    .streamlit-expanderContent, .stAlert,
    .stButton > button, .stTabs button {{ color: {_text} !important; }}
    .stTextInput > div > div > input,
    .stNumberInput > div > div > input,
    .stSelectbox > div > div > div,
    .stRadio > div {{
        background-color: {_sbg} !important;
        color: {_text} !important;
        border-color: {_border} !important;
    }}
    .stDataFrame, .dataframe {{
        color: {_text} !important;
        background: {_sbg} !important;
    }}
    .dataframe th, .dataframe td {{
        color: {_text} !important;
        background: {_sbg} !important;
        border-color: {_border} !important;
    }}
    .main .block-container {{
        padding-top: 0.75rem !important;
        padding-bottom: 1rem !important;
        max-width: 100% !important;
    }}
    .aum-card {{
        background: linear-gradient(135deg, #1a472a 0%, #2d6a4f 100%);
        border-radius: 12px; padding: 16px 20px; margin-bottom: 8px;
        border: 1px solid rgba(255,255,255,0.1);
    }}
    .aum-card .label {{ font-size: 0.85rem; color: rgba(255,255,255,0.75); margin-bottom: 4px; }}
    .aum-card .value {{ font-size: 1.4rem; font-weight: 700; color: #fff; }}
    .aum-card-kfin {{
        background: linear-gradient(135deg, #1a3a5c 0%, #2d6494 100%);
        border-radius: 12px; padding: 16px 20px; margin-bottom: 8px;
        border: 1px solid rgba(255,255,255,0.1);
    }}
    .aum-card-kfin .label {{ font-size: 0.85rem; color: rgba(255,255,255,0.75); margin-bottom: 4px; }}
    .aum-card-kfin .value {{ font-size: 1.4rem; font-weight: 700; color: #fff; }}
    .aum-card-bse {{
        background: linear-gradient(135deg, #5c1a3a 0%, #942d64 100%);
        border-radius: 12px; padding: 16px 20px; margin-bottom: 8px;
        border: 1px solid rgba(255,255,255,0.1);
    }}
    .aum-card-bse .label {{ font-size: 0.85rem; color: rgba(255,255,255,0.75); margin-bottom: 4px; }}
    .aum-card-bse .value {{ font-size: 1.4rem; font-weight: 700; color: #fff; }}
</style>
""", unsafe_allow_html=True)

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

    c_refresh, _ = st.columns([1, 5])
    with c_refresh:
        if st.button("🔄 Refresh Data", use_container_width=True):
            st.cache_data.clear()
            st.rerun()

    summary = load_dashboard_summary()

    # Top metrics row
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("👥 Clients", summary.get("total_clients", 0))
    m2.metric("📋 BSE XSIPs", summary.get("total_xsip", 0))
    m3.metric("✅ Active XSIPs", summary.get("active_xsip", 0))
    m4.metric("🏢 CAMS AMCs", summary.get("cams_amcs", 0))
    m5.metric("🏢 KFinTech AMCs", summary.get("kfin_amcs", 0))

    # AUM Cards
    st.divider()

    nav_data = st.session_state.get("amfi_nav", {})
    nav_count = len(nav_data.get("by_code", {}))
    if nav_count == 0:
        st.warning("⚠️ AMFI NAV not available. AUM shown is based on file rupee_bal only.")
    else:
        st.caption(f"📡 AMFI NAV synced: **{nav_count:,}** schemes | AUM = Units × NAV")

    aum_col1, aum_col2, aum_col3 = st.columns(3)
    with aum_col1:
        st.markdown(
            f'<div class="aum-card-bse"><div class="label">📦 Total AUM (All RTAs)</div>'
            f'<div class="value">{format_aum(summary.get("total_aum", 0))}</div></div>',
            unsafe_allow_html=True)
    with aum_col2:
        cams_unmatched = summary.get("cams_unmatched_nav", 0)
        cams_label = "📦 CAMS AUM"
        if cams_unmatched > 0:
            cams_label += f" ({cams_unmatched} unmatched)"
        st.markdown(
            f'<div class="aum-card"><div class="label">{cams_label}</div>'
            f'<div class="value">{format_aum(summary.get("cams_aum", 0))}</div></div>',
            unsafe_allow_html=True)
    with aum_col3:
        kfin_unmatched = summary.get("kfin_unmatched_nav", 0)
        kfin_label = "📦 KFinTech AUM"
        if kfin_unmatched > 0:
            kfin_label += f" ({kfin_unmatched} unmatched)"
        st.markdown(
            f'<div class="aum-card-kfin"><div class="label">{kfin_label}</div>'
            f'<div class="value">{format_aum(summary.get("kfin_aum", 0))}</div></div>',
            unsafe_allow_html=True)

    # Second metrics row
    st.divider()
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

    # Third metrics row — Scheme Master
    st.divider()
    m1, _, _, _, _ = st.columns(5)
    m1.metric("📋 BSE Schemes", summary.get("bse_schemes", 0))

    # AMC Breakdown
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

    # Recent Uploads
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

                numeric_cols = df_raw.select_dtypes(include=["number"]).columns.tolist()
                if numeric_cols:
                    st.divider()
                    st.subheader("📊 Numeric Column Summary")
                    st.dataframe(df_raw[numeric_cols].describe(), use_container_width=True)
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