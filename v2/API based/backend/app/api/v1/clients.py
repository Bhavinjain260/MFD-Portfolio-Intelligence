from datetime import date as _date, datetime
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from app.api.deps import current_user
from app.core.compat import get_conn
from app.schemas.clients import ClientRow, PortfolioRow, PortfolioSummary
from app.services import nav_service as ns
from app.services import nav_queries as nq
from app.services import report_generator as rg
from app.services import folio_resolver as fr
from app.services.cache import cached, data_version

router = APIRouter(prefix="/clients", tags=["clients"])

@router.get("", response_model=dict)
def list_clients(q: str = "", skip: int = 0, limit: int = 50, _=Depends(current_user)):
    if limit == 0: raise HTTPException(400, "Limit cannot be 0")
    with get_conn() as conn:
        where, params = "", []
        if q.strip():
            where = "WHERE UPPER(client_code) LIKE ? OR UPPER(primary_holder_first_name) LIKE ? OR UPPER(primary_holder_last_name) LIKE ? OR UPPER(primary_holder_pan) LIKE ?"
            params = [f"%{q.upper()}%"] * 4
        count = conn.execute(f"SELECT COUNT(*) FROM bse_client_master {where}", params).fetchone()[0]
        rows = conn.execute(f"SELECT client_code, COALESCE(NULLIF(TRIM(primary_holder_first_name || ' ' || COALESCE(primary_holder_last_name, '')), ''), client_code) AS name, primary_holder_pan AS pan, guardian_pan, indian_mobile_no AS mobile, email, city FROM bse_client_master {where} ORDER BY name LIMIT ? OFFSET ?", (*params, limit, skip)).fetchall()
    return {"items": [ClientRow(**dict(r)) for r in rows], "total": count, "page": skip // limit + 1, "page_size": limit}

@router.get("/{client_code}", response_model=ClientRow)
def get_client(client_code: str, _=Depends(current_user)):
    with get_conn() as conn:
        row = conn.execute("SELECT client_code, COALESCE(NULLIF(TRIM(primary_holder_first_name || ' ' || COALESCE(primary_holder_last_name, '')), ''), client_code) AS name, primary_holder_pan AS pan, guardian_pan, indian_mobile_no AS mobile, email, city FROM bse_client_master WHERE client_code = ?", (client_code,)).fetchone()
    if not row: raise HTTPException(404, f"Client {client_code} not found")
    return ClientRow(**dict(row))

@router.get("/{client_code}/portfolio", response_model=PortfolioSummary)
@cached
def portfolio(client_code: str, response: Response, _=Depends(current_user)):
    with get_conn() as conn:
        row = conn.execute("SELECT primary_holder_pan, guardian_pan, primary_holder_first_name || ' ' || primary_holder_last_name AS name FROM bse_client_master WHERE client_code = ?", (client_code,)).fetchone()
    if not row: raise HTTPException(404, "Client not found")
    
    pan, guardian, name = row[0], row[1], row[2]
    is_minor = not (pan or "").strip()
    match_pan = guardian if is_minor else pan

    cams_folios, kfin_folios = fr.resolve_client_folios(match_pan, name, is_minor)
    all_folios = list(set(cams_folios + kfin_folios))
    if not all_folios: return PortfolioSummary(total_invested=0, total_current=0, total_gain_loss=0, one_day_diff=0, folios=0, prev_nav_date=None, holdings=[])

    latest_nav_str = nq.get_latest_nav_date()
    current_date = datetime.strptime(latest_nav_str, "%Y-%m-%d").date() if latest_nav_str else _date.today()
    
    try:
        _, summary_rows, _, total_inv, total_val, _ = rg._build_valuation_data(client_code, _date(2000,1,1), current_date)
    except ValueError: raise HTTPException(404, "No valuation data")

    isins = {r["ISIN"] for r in summary_rows if r.get("ISIN")}
    baseline, prev_map, _ = ns.resolve_prev_nav_for_folios(current_date.isoformat(), tuple(sorted(isins)))

    rows, total_1d = [], 0.0
    for r in summary_rows:
        inv = float(r["Invested"] or 0.0)
        cur = float(r["Value"]) if r["Value"] is not None else None
        gain = float(r["Gain/Loss"]) if r["Gain/Loss"] is not None else None
        isin, units = r.get("ISIN"), r.get("Units")
        nav, diff = None, None

        if isin and cur is not None:
            hit = ns.fetch_nav_by_isin(isin)
            if hit:
                nav = hit[0]
                prev = prev_map.get(isin) if prev_map else None
                if prev and units:
                    diff = (nav - prev) * units
                    total_1d += diff

        rows.append(PortfolioRow(
            rta=r["RTA"], folio_id=r["Folio"], scheme_name=r["Scheme"], isin=isin, 
            units=units, current_nav=nav, invested=inv, current_value=cur, 
            gain_loss=gain, one_day_diff=diff
        ))

    response.headers["ETag"] = f'W/"{client_code}-{data_version()}"'
    return PortfolioSummary(
        total_invested=round(total_inv, 2), total_current=round(total_val, 2), 
        total_gain_loss=round(total_val-total_inv, 2), one_day_diff=round(total_1d, 2), 
        folios=len(all_folios), prev_nav_date=baseline, holdings=rows
    )