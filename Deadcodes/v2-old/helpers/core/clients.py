"""
core/clients.py
===============
Client identity, search, and the ONE folio-matching rule for the whole app.
Pure: no Streamlit.
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

from core.db import get_conn


def get_client_identity(client_code: str) -> Optional[dict]:
    """
    Resolve name, PAN, minor status for a client_code.
    Returns None if not found.
    """
    with get_conn() as conn:
        row = conn.execute("""
            SELECT
                client_code,
                TRIM(COALESCE(primary_holder_first_name,'') || ' ' ||
                     COALESCE(primary_holder_middle_name || ' ','') ||
                     COALESCE(primary_holder_last_name,'')) AS name,
                primary_holder_pan,
                guardian_pan,
                tax_status,
                email,
                indian_mobile_no AS mobile
            FROM bse_client_master
            WHERE client_code = ?
        """, (client_code,)).fetchone()

    if not row:
        return None

    pan = (row["primary_holder_pan"] or "").strip()
    is_minor = pan == ""
    match_pan = (row["guardian_pan"] or "").strip() if is_minor else pan

    return {
        "client_code": row["client_code"],
        "name": (row["name"] or "").strip(),
        "pan": pan,
        "guardian_pan": (row["guardian_pan"] or "").strip(),
        "match_pan": match_pan,
        "is_minor": is_minor,
        "tax_status": row["tax_status"],
        "email": row["email"],
        "mobile": row["mobile"],
    }


def search_clients(query: str, limit: int = 20) -> pd.DataFrame:
    if not query or not query.strip():
        return pd.DataFrame()
    q = f"%{query.strip().upper()}%"
    q_exact = query.strip().upper()

    with get_conn() as conn:
        df = pd.read_sql("""
            SELECT
                client_code,
                COALESCE(
                    NULLIF(TRIM(primary_holder_first_name || ' ' ||
                              COALESCE(primary_holder_middle_name || ' ', '') ||
                              primary_holder_last_name), ''),
                    client_code
                ) AS display_name,
                COALESCE(
                    NULLIF(TRIM(primary_holder_pan), ''),
                    NULLIF(TRIM(guardian_pan), ''),
                    '—'
                ) AS display_pan,
                tax_status,
                CASE
                    WHEN TRIM(COALESCE(primary_holder_pan,'')) = ''
                    THEN 'Minor'
                    ELSE 'Adult'
                END AS client_category,
                email,
                indian_mobile_no AS mobile,
                guardian_first_name || ' ' || guardian_last_name AS guardian_name
            FROM bse_client_master
            WHERE
                UPPER(TRIM(client_code))                             LIKE ?
                OR UPPER(TRIM(primary_holder_first_name))             LIKE ?
                OR UPPER(TRIM(primary_holder_last_name))              LIKE ?
                OR UPPER(TRIM(primary_holder_first_name || ' ' ||
                              primary_holder_last_name))              LIKE ?
                OR UPPER(TRIM(primary_holder_pan))                    LIKE ?
                OR UPPER(TRIM(guardian_pan))                          LIKE ?
                OR UPPER(TRIM(email))                                 LIKE ?
                OR UPPER(TRIM(indian_mobile_no))                      LIKE ?
            ORDER BY
                CASE WHEN UPPER(TRIM(client_code)) = ? THEN 0
                     WHEN UPPER(TRIM(primary_holder_first_name)) = ? THEN 1
                     ELSE 2
                END,
                display_name
            LIMIT ?
        """, conn, params=(
            q, q, q, q, q, q, q, q,
            q_exact, q_exact,
            limit,
        ))
    return df


def get_client_folios(client_code: str) -> dict:
    """
    THE single folio-matching rule for the whole app.

    Returns:
        {"cams": [folio_ids...], "kfin": [folio_ids...]}

    Adults  → match by PAN (primary or guardian for minors), plus name fallback.
    Minors  → match by investor name (folio PAN is empty).
    """
    identity = get_client_identity(client_code)
    if not identity:
        return {"cams": [], "kfin": []}

    name_clean = identity["name"].strip().upper() if identity["name"] else ""
    match_pan = identity["match_pan"]
    cams: set[str] = set()
    kfin: set[str] = set()

    with get_conn() as conn:
        if identity["is_minor"]:
            if name_clean:
                for row in conn.execute(
                    "SELECT foliochk FROM cams_wbr9_folio WHERE TRIM(UPPER(inv_name)) LIKE ? || '%'",
                    (name_clean,),
                ).fetchall():
                    cams.add(row[0])
                for row in conn.execute(
                    "SELECT folio FROM kfin_mfsd211_folio WHERE TRIM(UPPER(investor_name)) LIKE ? || '%'",
                    (name_clean,),
                ).fetchall():
                    kfin.add(row[0])
        else:
            if match_pan and match_pan.strip():
                pan_up = match_pan.strip().upper()
                for row in conn.execute(
                    "SELECT foliochk FROM cams_wbr9_folio WHERE TRIM(UPPER(pan_no)) = ?",
                    (pan_up,),
                ).fetchall():
                    cams.add(row[0])
                for row in conn.execute(
                    "SELECT folio FROM kfin_mfsd211_folio WHERE TRIM(UPPER(pan_number)) = ?",
                    (pan_up,),
                ).fetchall():
                    kfin.add(row[0])
            # Name fallback (covers edge cases where PAN missing on one RTA)
            if name_clean:
                for row in conn.execute(
                    "SELECT foliochk FROM cams_wbr9_folio WHERE TRIM(UPPER(inv_name)) LIKE ? || '%'",
                    (name_clean,),
                ).fetchall():
                    cams.add(row[0])
                for row in conn.execute(
                    "SELECT folio FROM kfin_mfsd211_folio WHERE TRIM(UPPER(investor_name)) LIKE ? || '%'",
                    (name_clean,),
                ).fetchall():
                    kfin.add(row[0])

    return {"cams": sorted(cams), "kfin": sorted(kfin)}


def get_client_schemes(client_code: str) -> pd.DataFrame:
    """All schemes across both RTAs for a client."""
    from core.folios import get_folio_schemes

    folios = get_client_folios(client_code)
    frames = []
    for f in folios["cams"]:
        s = get_folio_schemes(f, rta="CAMS")
        if not s.empty:
            s = s.copy()
            s["rta"] = "CAMS"
            frames.append(s)
    for f in folios["kfin"]:
        s = get_folio_schemes(f, rta="KFinTech")
        if not s.empty:
            s = s.copy()
            s["rta"] = "KFinTech"
            frames.append(s)
    if not frames:
        return pd.DataFrame(columns=["folio_no", "product_code", "scheme_name", "rta"])
    return pd.concat(frames, ignore_index=True)


def load_all_clients_with_display() -> pd.DataFrame:
    with get_conn() as conn:
        return pd.read_sql("""
            SELECT
                client_code,
                COALESCE(
                    NULLIF(TRIM(primary_holder_first_name || ' ' ||
                              COALESCE(primary_holder_middle_name || ' ', '') ||
                              primary_holder_last_name), ''),
                    client_code
                ) AS display_name,
                COALESCE(
                    NULLIF(TRIM(primary_holder_pan), ''),
                    NULLIF(TRIM(guardian_pan), ''),
                    '—'
                ) AS display_pan,
                tax_status,
                email,
                indian_mobile_no AS mobile
            FROM bse_client_master
            ORDER BY display_name
        """, conn)


def folio_to_client_map() -> dict[str, str]:
    """
    folio_id → client_code.
    Includes name fallback for minor KFin folios (no guardian_pan column).
    """
    mapping: dict[str, str] = {}
    with get_conn() as conn:
        clients = conn.execute("""
            SELECT client_code,
                   TRIM(COALESCE(primary_holder_first_name,'') || ' ' ||
                        COALESCE(primary_holder_last_name,'')) AS name,
                   primary_holder_pan, guardian_pan
            FROM bse_client_master
        """).fetchall()

        for c in clients:
            code = c["client_code"]
            pan = (c["primary_holder_pan"] or "").strip().upper()
            gpan = (c["guardian_pan"] or "").strip().upper()
            name = (c["name"] or "").strip().upper()
            is_minor = pan == ""
            match_pan = gpan if is_minor else pan

            if match_pan:
                for row in conn.execute(
                    "SELECT foliochk FROM cams_wbr9_folio WHERE TRIM(UPPER(pan_no)) = ?",
                    (match_pan,),
                ).fetchall():
                    mapping.setdefault(row[0], code)
                for row in conn.execute(
                    "SELECT folio FROM kfin_mfsd211_folio WHERE TRIM(UPPER(pan_number)) = ?",
                    (match_pan,),
                ).fetchall():
                    mapping.setdefault(row[0], code)

            if name:
                for row in conn.execute(
                    "SELECT foliochk FROM cams_wbr9_folio WHERE TRIM(UPPER(inv_name)) LIKE ? || '%'",
                    (name,),
                ).fetchall():
                    mapping.setdefault(row[0], code)
                for row in conn.execute(
                    "SELECT folio FROM kfin_mfsd211_folio WHERE TRIM(UPPER(investor_name)) LIKE ? || '%'",
                    (name,),
                ).fetchall():
                    mapping.setdefault(row[0], code)

    return mapping

def normalize_folio(folio: str) -> str:
    if not folio:
        return ""
    try:
        if pd.isna(folio):
            return ""
    except Exception:
        pass
    return str(folio).strip().split("/")[0].strip().lower()


