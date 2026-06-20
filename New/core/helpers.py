"""
Pure helper functions used across all pages: string cleaning, currency
formatting, AMC/folio normalization, RTA detection, date parsing.
"""
import re
from datetime import datetime, timedelta

import pandas as pd
import streamlit as st

CANCELLED_KEYWORDS = frozenset(
    ["CXL", "AUTOCXL", "AUTO CXL", "CX", "CANCEL", "CLOSED", "REDEEM", "STOPPED", "FAILED"]
)
AMC_SUFFIXES = [
    " MUTUAL FUND", " MF", " FUND", " AMC",
    " INDIA", " MANAGEMENT", " LTD", " LIMITED",
]
_WHITESPACE_RE = re.compile(r"\s+")

CAMS_AMCS = frozenset([
    "360 ONE", "ADITYA BIRLA SUN LIFE", "ANGEL ONE", "BANDHAN", "DSP",
    "FRANKLIN TEMPLETON", "HDFC", "HELIOS", "HSBC", "ICICI PRUDENTIAL",
    "JIO BLACKROCK", "KOTAK", "MAHINDRA MANULIFE", "NAVI", "PPFAS",
    "SBI", "SHRIRAM", "TATA", "UNIFI", "UNION", "WHITEOAK", "ZERODHA"
])
KFIN_AMCS = frozenset([
    "AXIS", "BARODA BNP PARIBAS", "BANK OF INDIA", "BAJAJ FINSERV",
    "CANARA ROBECO", "CAPITALMIND", "EDELWEISS", "GROWW", "INVESTECO",
    "ITI", "JM FINANCIAL", "LIC", "MIRAE ASSET", "MOTILAL OSWAL",
    "NIPPON INDIA", "OLD BRIDGE", "NJ", "PGIM", "QUANTUM", "QUANT",
    "SAMCO", "SUNDARAM", "TRUST", "TAURUS", "UTI"
])


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


def format_brokerage(val) -> str:
    try:
        amount = float(val)
        formatted = f"{amount:.8f}".rstrip("0").rstrip(".")
        return f"Rs {formatted}"
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


@st.cache_data(show_spinner=False)
def normalize_amc(name: str) -> str:
    if not name:
        return ""
    n = str(name).strip().upper()
    for suffix in AMC_SUFFIXES:
        n = n.replace(suffix, "")
    return _WHITESPACE_RE.sub(" ", n).strip()


def get_rta(amc_name: str) -> str:
    norm = normalize_amc(amc_name)
    if norm in CAMS_AMCS:
        return "CAMS"
    if norm in KFIN_AMCS:
        return "KFinTech"
    return "Unknown"


def normalize_folio(folio: str) -> str:
    if not folio:
        return ""
    try:
        if pd.isna(folio):
            return ""
    except Exception:
        pass
    return str(folio).strip().split("/")[0].strip().lower()


def is_active_status(raw_status) -> bool:
    if pd.isna(raw_status):
        return True
    status = str(raw_status).strip().upper()
    return not any(kw in status for kw in CANCELLED_KEYWORDS)


def parse_date_safe(val) -> str:
    if pd.isna(val) if isinstance(val, float) else str(val).strip() in {"", "None", "NaN"}:
        return ""
    try:
        dt = pd.to_datetime(val, dayfirst=True, errors="coerce")
        return dt.strftime("%Y-%m-%d") if pd.notna(dt) else ""
    except Exception:
        return ""


def get_next_sip_date(day) -> str:
    if pd.isna(day) or not day:
        return "N/A"
    try:
        day = int(day)
        today = datetime.now()
        try:
            candidate = today.replace(day=day)
            if candidate.date() >= today.date():
                return candidate.strftime("%d %b %Y")
        except ValueError:
            pass
        next_month_first = (today.replace(day=28) + timedelta(days=4)).replace(day=1)
        try:
            return next_month_first.replace(day=day).strftime("%d %b %Y")
        except ValueError:
            last_day = (next_month_first.replace(month=next_month_first.month % 12 + 1, day=1) - timedelta(days=1))
            return last_day.strftime("%d %b %Y")
    except Exception:
        return "N/A"


def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    df.columns = (df.columns.str.strip().str.lower().str.replace(r"[\s.\-_]+", "_", regex=True))
    return df


def clean_header(col: str) -> str:
    """Strip BOM/zero-width/nbsp chars and quotes from a raw CSV header, uppercase it."""
    return (
        col.strip()
        .replace("\u200b", "")
        .replace("\ufeff", "")
        .replace("\u00a0", "")
        .strip("'\"")
        .upper()
    )
