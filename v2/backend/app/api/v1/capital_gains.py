from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text

from app.api.deps import current_user
from app.core.compat import get_conn
from app.schemas.capital_gains import (
    HypotheticalRequest, MatchedLot, TaxBreakdown,
)
from app.services import capital_gain as cg

router = APIRouter(prefix="/capital-gains", tags=["capital-gains"])


def _load_txns(folio_no: str, prodcode: str, rta: str):
    import pandas as pd
    with get_conn() as conn:
        if rta.upper() == "CAMS":
            df = pd.read_sql("""
                SELECT COALESCE(txn_date_iso, traddate) AS traddate,
                       trxntype, trxn_nature, units, purprice, amount
                FROM v_cams_txn_active
                WHERE folio_no = ? AND UPPER(TRIM(prodcode)) = ?
            """, conn, params=(folio_no, prodcode.strip().upper()))
        elif rta.upper() in ("KFINTECH", "KFIN"):
            df = pd.read_sql("""
                SELECT COALESCE(txn_date_iso, td_trdt) AS traddate,
                       td_purred AS trxntype, '' AS trxn_nature,
                       td_units AS units, td_pop AS purprice, td_amt AS amount
                FROM v_kfin_txn_active
                WHERE td_acno = ? AND UPPER(TRIM(fmcode)) = ?
            """, conn, params=(folio_no, prodcode.strip().upper()))
        else:
            raise HTTPException(400, f"Unknown RTA: {rta}")
    if df.empty:
        raise HTTPException(404, "No transactions for this folio/scheme")
    df["_sort"] = pd.to_datetime(df["traddate"], errors="coerce", format="mixed")
    return df.sort_values("_sort").drop(columns="_sort")


@router.get("/{client_code}/realized", response_model=dict)
def realized(client_code: str, fy: str | None = None, _=Depends(current_user)):
    """
    Realized capital gains for all CAMS folios belonging to the client.
    KFinTech redemption tracking is not yet supported — mirrors the
    Streamlit-era behavior.
    """
    from datetime import date as date_cls

    with get_conn() as conn:
        row = conn.execute(
            "SELECT primary_holder_pan, guardian_pan, "
            "       primary_holder_first_name || ' ' || primary_holder_last_name AS name "
            "FROM bse_client_master WHERE client_code = ?",
            (client_code,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "Client not found")

    pan = (row[0] or "").strip().upper()
    guardian = (row[1] or "").strip().upper()
    name = (row[2] or "").strip().upper()
    match_pan = guardian if not pan else pan

    with get_conn() as conn:
        folios = [r[0] for r in conn.execute(
            "SELECT foliochk FROM cams_wbr9_folio "
            "WHERE TRIM(UPPER(pan_no))=? OR TRIM(UPPER(inv_name))=?",
            (match_pan, name),
        ).fetchall()]

    if not folios:
        return {"lots": [], "tax": None, "rows": 0}

    detail_rows = []
    for folio_no in folios:
        with get_conn() as conn:
            prods = [r[0] for r in conn.execute(
                "SELECT DISTINCT UPPER(TRIM(prodcode)) FROM v_cams_txn_active WHERE folio_no = ?",
                (folio_no,),
            ).fetchall()]
        for p in prods:
            txns = _load_txns(folio_no, p, "CAMS")
            lots, matches = cg.replay_folio_scheme(txns)
            for m in matches:
                if fy and fy != "All Time":
                    try:
                        y0 = int(fy.split("-")[0])
                        if not (date_cls(y0, 4, 1) <= m.sell_date <= date_cls(y0 + 1, 3, 31)):
                            continue
                    except Exception:
                        pass
                detail_rows.append({
                    "Scheme": "", "Folio": folio_no, "RTA": "CAMS",
                    "Purchase Date": m.buy_date, "Purchase Units": m.units,
                    "Purchase NAV": (m.cost / m.units) if m.units else 0,
                    "Purchase Value": m.cost,
                    "Redemption Date": m.sell_date, "Redemption Units": m.units,
                    "Redemption NAV": (m.proceeds / m.units) if m.units else 0,
                    "Redemption Value": m.proceeds,
                    "Holding Days": m.holding_days,
                    "Type": "LTCG" if m.is_ltcg else "STCG",
                    "Gain/Loss": m.gain,
                })

    return {"lots": detail_rows, "rows": len(detail_rows)}


@router.post("/hypothetical", response_model=dict)
def hypothetical(req: HypotheticalRequest, _=Depends(current_user)):
    txns = _load_txns(req.folio_no, req.prodcode, req.rta)
    matches = cg.hypothetical_redemption(txns, req.redeem_units, req.nav)
    tax = cg.tax_for_matches(matches, req.tax_category, req.slab_rate)
    return {
        "matches": [m.__dict__ | {"is_ltcg": m.is_ltcg,
                                  "cost": m.cost, "proceeds": m.proceeds,
                                  "gain": m.gain} for m in matches],
        "tax": tax,
    }