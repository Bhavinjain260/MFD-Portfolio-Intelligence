"""
THEME PATCH — replace your existing <style> block with this one.
Covers: stDataFrame grid (glide canvas), metrics, selectbox popovers,
multiselect/radio pills, buttons, tabs, expander, dividers, captions.
"""

THEME_CSS = """
<style>
    .stApp {{ background-color: {bg} !important; }}

    section[data-testid="stSidebar"] {{ background-color: {sbg} !important; }}
    section[data-testid="stSidebar"] * {{ color: {text} !important; }}

    .stMarkdown, p, span, div, label, h1, h2, h3, h4, h5, h6,
    .streamlit-expanderContent, .stAlert, .stCaption,
    [data-testid="stCaptionContainer"] {{ color: {text} !important; }}

    [data-testid="stMetric"] {{
        background: {sbg} !important;
        border: 1px solid {border} !important;
        border-radius: 10px !important;
        padding: 12px 16px !important;
    }}
    [data-testid="stMetricLabel"] {{ color: {muted} !important; }}
    [data-testid="stMetricValue"] {{ color: {text} !important; }}
    [data-testid="stMetricDelta"] {{ color: {accent} !important; }}

    .stButton > button, .stDownloadButton > button {{
        background-color: {sbg} !important;
        color: {text} !important;
        border: 1px solid {border} !important;
    }}
    .stButton > button:hover, .stDownloadButton > button:hover {{
        border-color: {accent} !important;
        color: {accent} !important;
    }}
    .stButton > button[kind="primary"] {{
        background-color: {accent} !important;
        color: #ffffff !important;
        border-color: {accent} !important;
    }}

    .stTextInput input, .stNumberInput input,
    .stSelectbox div[data-baseweb="select"] > div,
    .stMultiSelect div[data-baseweb="select"] > div,
    .stTextArea textarea {{
        background-color: {sbg} !important;
        color: {text} !important;
        border-color: {border} !important;
    }}

    /* dropdown popover menus (selectbox / multiselect options) */
    div[data-baseweb="popover"] ul,
    div[data-baseweb="menu"],
    div[data-baseweb="select"] ul li {{
        background-color: {sbg} !important;
        color: {text} !important;
    }}
    div[data-baseweb="select"] ul li:hover {{
        background-color: {hover} !important;
    }}

    /* multiselect chips */
    span[data-baseweb="tag"] {{
        background-color: {accent} !important;
        color: #ffffff !important;
    }}

    /* radio / pill buttons */
    .stRadio label, .stRadio div, [role="radiogroup"] label {{
        color: {text} !important;
    }}
    [data-baseweb="radio"] {{ color: {text} !important; }}

    .stTabs [data-baseweb="tab-list"] {{ background-color: transparent !important; }}
    .stTabs [data-baseweb="tab"] {{ color: {muted} !important; }}
    .stTabs [aria-selected="true"] {{ color: {accent} !important; border-color: {accent} !important; }}

    hr, [data-testid="stDivider"] {{ border-color: {border} !important; }}

    .streamlit-expanderHeader {{
        background-color: {sbg} !important;
        color: {text} !important;
    }}

    /* ---- DATAFRAME GRID (glide-data-grid canvas) ---- */
    [data-testid="stDataFrame"], [data-testid="stDataFrameResizable"] {{
        background-color: {sbg} !important;
        border: 1px solid {border} !important;
        border-radius: 8px !important;
    }}
    [data-testid="stDataFrame"] [data-testid="stElementToolbar"] {{
        background-color: {sbg} !important;
    }}
    .gdg-cell {{
        background-color: {sbg} !important;
        color: {text} !important;
    }}
    .glideDataEditor, .gdg-canvas, canvas[data-testid] {{
        background-color: {sbg} !important;
    }}

    /* fallback legacy st.table */
    .dataframe th, .dataframe td {{
        color: {text} !important;
        background: {sbg} !important;
        border-color: {border} !important;
    }}

    .main .block-container {{
        padding-top: 0.75rem !important;
        padding-bottom: 1rem !important;
        max-width: 100% !important;
    }}

    .aum-card, .aum-card-kfin, .aum-card-bse {{
        border-radius: 12px; padding: 16px 20px; margin-bottom: 8px;
        border: 1px solid rgba(255,255,255,0.1);
    }}
    .aum-card {{ background: linear-gradient(135deg, #1a472a 0%, #2d6a4f 100%); }}
    .aum-card-kfin {{ background: linear-gradient(135deg, #1a3a5c 0%, #2d6494 100%); }}
    .aum-card-bse {{ background: linear-gradient(135deg, #5c1a3a 0%, #942d64 100%); }}
    .aum-card .label, .aum-card-kfin .label, .aum-card-bse .label {{
        font-size: 0.85rem; color: rgba(255,255,255,0.75) !important; margin-bottom: 4px;
    }}
    .aum-card .value, .aum-card-kfin .value, .aum-card-bse .value {{
        font-size: 1.4rem; font-weight: 700; color: #fff !important;
    }}
</style>
"""


def render_theme(dark: bool) -> str:
    if dark:
        return THEME_CSS.format(
            bg="#0e1117", sbg="#161b22", text="#e6edf3",
            muted="#8b949e", border="#30363d", accent="#58a6ff",
            hover="#21262d",
        )
    return THEME_CSS.format(
        bg="#ffffff", sbg="#f6f8fa", text="#1a1a2e",
        muted="#6b7280", border="#d0d7de", accent="#2563eb",
        hover="#eef2f7",
    )


# Watches Streamlit's actual rendered background color (changes instantly
# when user picks Light/Dark/System in the ⋮ menu, even though the Python
# script doesn't know yet -- this is a known Streamlit limitation: theme
# menu changes are frontend-only and don't trigger a script rerun on their
# own). On a detected color change, reloads the page so app.py re-runs with
# the correct st.context.theme.type -- no manual refresh needed by the user.
THEME_WATCHER_JS = """
<script>
(function() {
    function getBg() {
        const doc = window.parent.document;
        const appEl = doc.querySelector('[data-testid="stApp"]') || doc.body;
        return window.parent.getComputedStyle(appEl).backgroundColor;
    }

    function isDarkColor(rgbStr) {
        const m = rgbStr && rgbStr.match(/\\d+/g);
        if (!m || m.length < 3) return null;
        const [r, g, b] = m.map(Number);
        return (r + g + b) / 3 < 128;
    }

    const STORE_KEY = "__last_theme_is_dark__";
    let stored = window.parent.sessionStorage.getItem(STORE_KEY);
    let lastIsDark = stored === null ? isDarkColor(getBg()) : stored === "true";

    setInterval(function() {
        const nowDark = isDarkColor(getBg());
        if (nowDark === null) return;
        if (nowDark !== lastIsDark) {
            window.parent.sessionStorage.setItem(STORE_KEY, String(nowDark));
            window.parent.location.reload();
        }
    }, 350);
})();
</script>
"""