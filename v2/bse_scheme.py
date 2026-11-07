"""
bse_scheme.py
=============
Single source of truth for ALL bse_scheme_master lookups. Pure Python —
no Streamlit.

Covers:
  • Schema probe: which column holds the AMC / fund-house name string
    (cached per process — the PRAGMA table_info used to run 3× per page load)
  • Deduplicated scheme lookup (product_code → ISIN, scheme_name, bse_amc_name)
  • Single-row lookups: ISIN / scheme_name for one product code
  • SQL subquery builder for callers that need to inline the dedup block
    inside a larger JOIN (brokerage load, folio-NAV load)

Cached @st.cache_data wrappers live in main.py — same pattern as
nav_engine.py: pure logic here, decorators there.

Why this exists
---------------
Before this module, the same `UPPER(TRIM(Channel_Partner_Code)) … GROUP BY`
dedup query was copy-pasted in 5+ places in main.py:
  - _bse_scheme_lookup          (cached, scheme_name + isin only)
  - load_brokerage_report       (inline subquery, amc_name only)
  - get_all_folios_with_isin_and_nav (inline subquery, all 3 cols)
  - Valuation Report            (inline read_sql, isin + scheme_name)
  - get_isin_for_cams_product   (one-off SELECT ISIN)
  - _get_bse_amc_column         (PRAGMA probe)

Each rebuilt its own SQL + its own AMC-column probe. Now there is one
query, one probe, one cache entry.
"""

import logging
import threading
from typing import Optional

import pandas as pd

from init_db import get_conn

log = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════
# AMC COLUMN PROBE (cached per process)
# ══════════════════════════════════════════════════════════════
# bse_scheme_master's AMC-name column has a different physical name across
# BSE downloads (amc_code, AMC_Name, Fund_Name, ...). We probe once and
# cache the result for the process lifetime.

_AMC_COL_LOCK = threading.Lock()
_AMC_COL_CACHE: Optional[str] = None
_AMC_COL_PROBED: bool = False

_AMC_COLUMN_CANDIDATES = [
    "amc_code", "amc_ind", "amc_name",
    "AMC", "AMC_Name", "AMC_NAME", "Amc_Name",
    "Fund_Name", "FUND_NAME", "Fund", "FUND",
    "AMC_Code", "AMC_CODE",
]


def get_amc_column() -> Optional[str]:
    """
    Probe bse_scheme_master's actual schema to find whichever column holds
    the AMC / fund-house name string. Cached per process — the PRAGMA scan
    runs exactly once even if many callers ask.

    Returns the column name, or None if no candidate column exists (in which
    case callers should fall back to AMFI-canonical AMC names via ISIN).
    """
    global _AMC_COL_CACHE, _AMC_COL_PROBED
    with _AMC_COL_LOCK:
        if _AMC_COL_PROBED:
            return _AMC_COL_CACHE
        with get_conn() as conn:
            cols = [row[1] for row in conn.execute(
                "PRAGMA table_info(bse_scheme_master)").fetchall()]
        for c in _AMC_COLUMN_CANDIDATES:
            if c in cols:
                log.info("[BSE-SCHEME] Using '%s' as AMC-name fallback column", c)
                _AMC_COL_CACHE = c
                break
        else:
            log.warning(
                "[BSE-SCHEME] No AMC-name column found in bse_scheme_master "
                "(checked %s). BSE fallback name disabled — rows will rely on "
                "AMFI ISIN match only.",
                _AMC_COLUMN_CANDIDATES,
            )
            _AMC_COL_CACHE = None
        _AMC_COL_PROBED = True
        return _AMC_COL_CACHE


def _amc_select_expr(alias: str = "bse_amc_name") -> str:
    """SQL fragment for the AMC-name column, or NULL if none exists."""
    col = get_amc_column()
    return f"MAX({col}) AS {alias}" if col else f"NULL AS {alias}"


# ══════════════════════════════════════════════════════════════
# DEDUPLICATED SCHEME LOOKUP (DataFrame)
# ══════════════════════════════════════════════════════════════
def get_scheme_lookup() -> pd.DataFrame:
    """
    Deduplicated bse_scheme_master, one row per Channel_Partner_Code.

    Columns:
        product_code   — UPPER(TRIM(Channel_Partner_Code))
        isin           — MAX(ISIN)
        scheme_name    — MAX(Scheme_Name)
        bse_amc_name   — MAX(<amc_col>) or NULL if no AMC column exists

    Wraps with @st.cache_data in main.py — do NOT cache here, so the
    module stays importable from plain-Python contexts (background_worker).
    """
    amc_select = _amc_select_expr()
    with get_conn() as conn:
        return pd.read_sql(f"""
            SELECT UPPER(TRIM(Channel_Partner_Code)) AS product_code,
                   MAX(ISIN)        AS isin,
                   MAX(Scheme_Name) AS scheme_name,
                   {amc_select}
            FROM bse_scheme_master
            WHERE Channel_Partner_Code IS NOT NULL
              AND TRIM(Channel_Partner_Code) != ''
            GROUP BY UPPER(TRIM(Channel_Partner_Code))
        """, conn)


# ══════════════════════════════════════════════════════════════
# SINGLE-PRODUCT LOOKUPS (no DataFrame, no caching at this layer)
# ══════════════════════════════════════════════════════════════
def get_isin_for_product(prodcode: str) -> Optional[str]:
    """ISIN for one Channel_Partner_Code, or None."""
    if not prodcode:
        return None
    with get_conn() as conn:
        row = conn.execute("""
            SELECT ISIN FROM bse_scheme_master
            WHERE UPPER(TRIM(Channel_Partner_Code)) = ?
            LIMIT 1
        """, (prodcode.strip().upper(),)).fetchone()
    return row[0] if row else None


def get_scheme_name_for_product(prodcode: str) -> Optional[str]:
    """Scheme name for one Channel_Partner_Code, or None."""
    if not prodcode:
        return None
    with get_conn() as conn:
        row = conn.execute("""
            SELECT Scheme_Name FROM bse_scheme_master
            WHERE UPPER(TRIM(Channel_Partner_Code)) = ?
            LIMIT 1
        """, (prodcode.strip().upper(),)).fetchone()
    return row[0] if row else None


# ══════════════════════════════════════════════════════════════
# SUBQUERY BUILDER (for callers that inline the dedup inside a JOIN)
# ══════════════════════════════════════════════════════════════
# load_brokerage_report() and get_all_folios_with_isin_and_nav() both
# embed the dedup as a subquery inside a larger SQL JOIN. We can't replace
# that with a DataFrame merge without rewriting their whole query flow, so
# this builder keeps the same shape but centralizes the SQL + AMC probe.

def build_dedup_subquery(include_scheme_name: bool = False,
                         include_amc: bool = False) -> str:
    """
    Return the dedup SQL block as a string, suitable for embedding inside a
    larger query's JOIN, e.g.:

        LEFT JOIN (<bse_dedup>) sm
            ON UPPER(TRIM(cf.product)) = sm.cp_code

    Always emits:    cp_code, ISIN
    Opt-in columns:
        include_scheme_name  → adds MAX(Scheme_Name) AS Scheme_Name
        include_amc          → adds MAX(<amc_col>)  AS bse_amc_name
                               (NULL AS bse_amc_name if no AMC column exists)

    Callers should reference the emitted column names directly: cp_code,
    ISIN, Scheme_Name, bse_amc_name.
    """
    cols = [
        "UPPER(TRIM(Channel_Partner_Code)) AS cp_code",
        "MAX(ISIN) AS ISIN",
    ]
    if include_scheme_name:
        cols.append("MAX(Scheme_Name) AS Scheme_Name")
    if include_amc:
        cols.append(_amc_select_expr())

    select_list = ",\n                   ".join(cols)
    return f"""
        SELECT
                   {select_list}
        FROM bse_scheme_master
        WHERE Channel_Partner_Code IS NOT NULL AND TRIM(Channel_Partner_Code) != ''
        GROUP BY UPPER(TRIM(Channel_Partner_Code))
    """