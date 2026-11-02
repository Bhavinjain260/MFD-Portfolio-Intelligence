"""
ingest/common.py
================
Shared parsing helpers for BSE / CAMS / KFin file imports.
"""
from __future__ import annotations

import re
from datetime import datetime

import pandas as pd


def raw_val(val) -> str:
    if val is None:
        return ""
    try:
        if pd.isna(val):
            return ""
    except (TypeError, ValueError):
        pass
    return str(val).strip()


def raw_num(val, default: float = 0.0) -> float:
    if val is None:
        return default
    try:
        if pd.isna(val):
            return default
    except (TypeError, ValueError):
        pass
    s = str(val).strip()
    s = s.replace("₹", "").replace("Rs", "").replace("RS", "").replace("rs", "")
    s = s.replace("INR", "").replace("inr", "")
    s = s.replace("Dr", "").replace("CR", "").replace("Cr", "").replace("dr", "").replace("cr", "")
    s = s.replace('"', "").replace("'", "").replace(",", "").replace(" ", "").strip()
    if not s or s == "-":
        return default
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    try:
        return float(s)
    except (ValueError, TypeError):
        return default


def cams_date_to_iso(raw) -> str | None:
    """CAMS 'M/D/YYYY  HH:MM:SS AM' → YYYY-MM-DD."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or s.lower() in ("nan", "none"):
        return None
    date_part = re.split(r"\s+", s, maxsplit=1)[0]
    try:
        return datetime.strptime(date_part, "%m/%d/%Y").strftime("%Y-%m-%d")
    except ValueError:
        return None


def kfin_date_to_iso(raw) -> str | None:
    """KFin 'DD/MM/YYYY' → YYYY-MM-DD."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or s.lower() in ("nan", "none"):
        return None
    try:
        return datetime.strptime(s, "%d/%m/%Y").strftime("%Y-%m-%d")
    except ValueError:
        return None


def clean_cols(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [
        re.sub(
            r"[^A-Z0-9]+", "_",
            c.strip().replace("\u200b", "").replace("\ufeff", "")
            .replace("\u00a0", "").strip("'\"").upper(),
        ).strip("_")
        for c in df.columns
    ]
    return df


def batch_id(prefix: str, name: str = "") -> str:
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    return f"{prefix}_{name}_{ts}" if name else f"{prefix}_{ts}"


def read_csv_auto(file) -> pd.DataFrame | None:
    """Tolerant CSV/TSV reader for RTA mailback files."""
    import csv
    import io

    ENCODINGS = ("utf-8-sig", "utf-16", "latin-1", "utf-8")

    # Quoted TSV pass
    for enc in ENCODINGS:
        try:
            file.seek(0)
            raw = file.read()
            text = raw.decode(enc) if isinstance(raw, bytes) else raw
        except Exception:
            continue
        first = text.lstrip().split("\n", 1)[0]
        if first.startswith("'"):
            sep = "\t" if first.count("\t") > first.count(",") else ","
            rows = [
                r for r in csv.reader(io.StringIO(text, newline=""), delimiter=sep, quotechar="'")
                if any(c.strip() for c in r)
            ]
            if rows and len(rows[0]) > 5:
                hdr, n = [c.strip() for c in rows[0]], len(rows[0])
                data = [(r + [""] * n)[:n] for r in rows[1:]]
                return pd.DataFrame(data, columns=hdr, dtype=str)

    for encoding in ENCODINGS:
        for sep in ("\t", ",", ";"):
            try:
                file.seek(0)
                df = pd.read_csv(
                    file, sep=sep, dtype=str, quotechar='"',
                    encoding=encoding, encoding_errors="replace",
                    skip_blank_lines=True, on_bad_lines="skip",
                )
                if len(df.columns) > 5:
                    return df
            except Exception:
                continue

    for encoding in ENCODINGS:
        try:
            file.seek(0)
            df = pd.read_csv(
                file, sep=None, engine="python", dtype=str,
                encoding=encoding, encoding_errors="replace",
                skip_blank_lines=True, on_bad_lines="skip",
            )
            if len(df.columns) > 5:
                return df
        except Exception:
            continue
    return None
