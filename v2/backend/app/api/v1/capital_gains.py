from fastapi import APIRouter, Depends, HTTPException, Query
import pandas as pd
from app.api.deps import current_user
from app.core.compat import get_conn
from app.schemas.capital_gains import HypotheticalRequest
from app.services import capital_gain as cg
from app.services import folio_resolver as fr

router = APIRouter(prefix="/capital-gains", tags=["capital-gains"])

def _load_all_cams_txns(folios: list[str]) -> pd.DataFrame:
    if not folios: return pd.DataFrame()
    ph = ",".join(["?"] * len(folios))
    with get_conn() as conn:
        df = pd.read_sql(f"SELECT folio_no, UPPER(TRIM(prodcode)) AS prodcode, COALESCE(txn_date_iso, traddate) AS traddate, trxntype, trxn_nature, units, purprice, amount FROM v_cams_txn_active WHERE folio_no IN ({ph})", conn, params=folios)
    if not df.empty:
        df["_sort"] = pd.to_datetime(df["traddate"], errors="coerce", format="mixed")
        df = df.sort_values("_sort").drop(columns="_sort")
    return df

def _scheme_name_map() -> dict[str, str]:
    with get_conn() as conn:
        return {r[0]: r[1] for r in conn.execute("SELECT UPPER(TRIM(Channel_Partner_Code)), MAX(Scheme_Name) FROM bse_scheme_master GROUP BY 1").fetchall()}

@router.get("/{client_code}/realized", response_model=dict)
def realized(client_code: str, fy: str | None = Query(None), tax_category: str = Query("equity", pattern="^(equity|debt)$"), slab_rate: float = Query(0.30), include_gaps: bool = Query(False), _=Depends(current_user)):
    with get_conn() as conn:
        row = conn.execute("SELECT primary_holder_pan, guardian_pan, primary_holder_first_name || ' ' || primary_holder_last_name AS name FROM bse_client_master WHERE client_code = ?", (client_code,)).fetchone()
    if not row: raise HTTPException(404, "Client not found")

    pan, guardian, name = row[0], row[1], row[2]
    is_minor = not (pan or "").strip()
    match_pan = guardian if is_minor else pan

    cams_folios, _ = fr.resolve_client_folios(match_pan, name, is_minor)
    if not cams_folios: return {"lots": [], "tax": None, "rows": 0}

    txns = _load_all_cams_txns(cams_folios)
    scheme_names = _scheme_name_map()
    
    detail_rows = []
    all_matches = []
    
    for (folio_no, prodcode), group in txns.groupby(["folio_no", "prodcode"]):
        _, matches = cg.replay_folio_scheme(group)
        for m in matches:
            if m.sell_date is None: continue
            if fy and fy != "All Time":
                if cg._get_fy(m.sell_date) != fy: continue

            all_matches.append(m)
            detail_rows.append({
                "Scheme": scheme_names.get(prodcode, prodcode), "Folio": folio_no, "RTA": "CAMS",
                "Purchase Date": m.buy_date, "Purchase Units": m.units, "Purchase NAV": (m.cost / m.units) if m.units else 0, "Purchase Value": m.cost,
                "Redemption Date": m.sell_date, "Redemption Units": m.units, "Redemption NAV": (m.proceeds / m.units) if m.units else 0, "Redemption Value": m.proceeds,
                "Holding Days": m.holding_days, "Type": "LTCG" if m.is_ltcg else "STCG", "Gain/Loss": m.gain,
                "data_gap": m.is_gap
            })

    tax = cg.tax_for_matches_by_fy(all_matches, tax_category, slab_rate, include_gaps)
    return {"lots": detail_rows, "tax": tax, "rows": len(detail_rows)}

@router.post("/hypothetical", response_model=dict)
def hypothetical(req: HypotheticalRequest, _=Depends(current_user)):
    with get_conn() as conn:
        df = pd.read_sql("SELECT COALESCE(txn_date_iso, traddate) AS traddate, trxntype, trxn_nature, units, purprice, amount FROM v_cams_txn_active WHERE folio_no = ? AND UPPER(TRIM(prodcode)) = ?", conn, params=(req.folio_no, req.prodcode.strip().upper()))
    if df.empty: raise HTTPException(404, "No transactions")
    
    df["_s"] = pd.to_datetime(df["traddate"], errors="coerce", format="mixed")
    df = df.sort_values("_s").drop(columns="_s")

    nav = req.nav
    if not nav or nav <= 0:
        with get_conn() as conn:
            isin = conn.execute("SELECT UPPER(TRIM(ISIN)) FROM bse_scheme_master WHERE UPPER(TRIM(Channel_Partner_Code)) = ? LIMIT 1", (req.prodcode.strip().upper(),)).fetchone()[0]
        from app.services import nav_service as ns
        hit = ns.fetch_nav_by_isin(isin)
        if not hit: raise HTTPException(400, f"No NAV for {isin}")
        nav = hit[0]

    matches = cg.hypothetical_redemption(df, req.redeem_units, nav)
    tax = cg.tax_for_matches_by_fy(matches, req.tax_category, req.slab_rate)
    return {"matches": [{"buy_date": m.buy_date, "buy_rate": m.buy_rate, "sell_date": m.sell_date, "sell_rate": m.sell_rate, "units": m.units, "holding_days": m.holding_days, "cost": m.cost, "proceeds": m.proceeds, "gain": m.gain, "is_ltcg": m.is_ltcg, "data_gap": m.is_gap} for m in matches], "tax": tax, "nav_used": nav}