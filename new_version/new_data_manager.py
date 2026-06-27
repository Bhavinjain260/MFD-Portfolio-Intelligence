
import logging
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime

import pandas as pd
import streamlit as st

log = logging.getLogger(__name__)

# ── re-exported from main app (pass DB_PATH at init) ──────────────────────────
_DB_PATH = "mfd_local.db"


def set_db_path(path: str):
    global _DB_PATH
    _DB_PATH = path


@contextmanager
def get_conn():
    conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _sync_rta(amc_codes: list[str], rta: str) -> None:
    """After a successful upload, upsert RTA into amc_config for every AMC code seen."""
    if not amc_codes:
        return
    with get_conn() as conn:
        conn.executemany(
            "INSERT INTO amc_config (amc, rta, is_enabled) VALUES (?,?,1) "
            "ON CONFLICT(amc) DO UPDATE SET rta=excluded.rta",
            [(code, rta) for code in amc_codes if code],
        )


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