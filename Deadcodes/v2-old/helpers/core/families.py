"""
core/families.py
================
Family CRUD and folio aggregation (single-query, not N+1).
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

from core.db import get_conn
from core.clients import get_client_identity, get_client_folios


def get_family_for_client(client_code: str) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute("""
            SELECT f.family_id, f.family_name, f.head_client_code
            FROM client_family_member m
            JOIN client_family f ON m.family_id = f.family_id
            WHERE m.client_code = ?
        """, (client_code,)).fetchone()
    if not row:
        return None
    return {
        "family_id": row[0],
        "family_name": row[1],
        "head_client_code": row[2],
    }


def get_family_members(family_id: int) -> pd.DataFrame:
    with get_conn() as conn:
        return pd.read_sql("""
            SELECT m.client_code, m.is_head,
                   COALESCE(
                       NULLIF(TRIM(
                           COALESCE(b.primary_holder_first_name,'') || ' ' ||
                           COALESCE(b.primary_holder_last_name,'')
                       ), ''),
                       m.client_code
                   ) AS name
            FROM client_family_member m
            LEFT JOIN bse_client_master b ON m.client_code = b.client_code
            WHERE m.family_id = ?
        """, conn, params=(family_id,))


def create_family(head_client_code: str, family_name: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO client_family (family_name, head_client_code) VALUES (?, ?)",
            (family_name, head_client_code),
        )
        family_id = cur.lastrowid
        conn.execute(
            "INSERT INTO client_family_member (family_id, client_code, is_head) VALUES (?, ?, 1)",
            (family_id, head_client_code),
        )
    return family_id


def add_family_member(family_id: int, client_code: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO client_family_member (family_id, client_code, is_head) VALUES (?, ?, 0)",
            (family_id, client_code),
        )


def remove_family_member(family_id: int, client_code: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM client_family_member WHERE family_id = ? AND client_code = ?",
            (family_id, client_code),
        )


def delete_family(family_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM client_family_member WHERE family_id = ?", (family_id,))
        conn.execute("DELETE FROM client_family WHERE family_id = ?", (family_id,))


def get_family_folios(family_id: int) -> dict:
    """
    All folios for all family members — uses get_client_folios (one rule).
    Returns {"cams": [...], "kfin": [...]}.
    """
    members = get_family_members(family_id)
    cams: set[str] = set()
    kfin: set[str] = set()
    for _, m in members.iterrows():
        folios = get_client_folios(m["client_code"])
        cams.update(folios["cams"])
        kfin.update(folios["kfin"])
    return {"cams": sorted(cams), "kfin": sorted(kfin)}


def get_family_folios_by_rta(family_id: int) -> tuple[list, list]:
    """Back-compat: (cams_list, kfin_list)."""
    f = get_family_folios(family_id)
    return f["cams"], f["kfin"]


def get_family_all_folios(family_id: int) -> set:
    f = get_family_folios(family_id)
    return set(f["cams"]) | set(f["kfin"])


def get_investor_names_for_folios(cams_folios: list, kfin_folios: list) -> dict:
    name_map: dict = {}
    with get_conn() as conn:
        if cams_folios:
            ph = ",".join(["?"] * len(cams_folios))
            rows = conn.execute(
                f"SELECT foliochk, inv_name FROM cams_wbr9_folio WHERE foliochk IN ({ph})",
                cams_folios,
            ).fetchall()
            for folio_id, name in rows:
                name_map[folio_id] = (name or "").strip()
        if kfin_folios:
            ph = ",".join(["?"] * len(kfin_folios))
            rows = conn.execute(
                f"SELECT folio, investor_name FROM kfin_mfsd211_folio WHERE folio IN ({ph})",
                kfin_folios,
            ).fetchall()
            for folio_id, name in rows:
                name_map.setdefault(folio_id, (name or "").strip())
    return name_map
