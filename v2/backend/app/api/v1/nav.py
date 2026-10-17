from fastapi import APIRouter, Depends, HTTPException, Query
from app.api.deps import current_user
from app.services import nav_queries as nq
from app.services import nav_service as ns
from app.schemas.nav import NavHistoryPoint, NavSchemeInfo, NavSnapshotStatus, NavValue

router = APIRouter(prefix="/nav", tags=["nav"])

@router.get("/snapshot-status", response_model=NavSnapshotStatus)
def snapshot_status(_=Depends(current_user)):
    return NavSnapshotStatus(**ns.get_snapshot_status())

@router.get("/latest", response_model=NavValue)
def latest(isin: str = Query(..., min_length=3), _=Depends(current_user)):
    hit = nq.get_latest_nav_by_isin(isin)
    if not hit: raise HTTPException(404, f"No NAV for {isin}")
    return NavValue(isin=isin, nav=hit[0], nav_date=hit[1])

@router.get("/latest-batch", response_model=dict)
def latest_batch(isins: str = Query(...), _=Depends(current_user)):
    isin_list = [i.strip().upper() for i in isins.split(",") if i.strip()]
    if not isin_list: return {}
    if len(isin_list) > 500: raise HTTPException(400, "Max 500 ISINs")
    return {i: {"nav": v[0], "date": v[1]} for i in isin_list if (v := nq.get_latest_nav_by_isin(i))}

@router.get("/{isin}/history", response_model=list[NavHistoryPoint])
def history(isin: str, days: int = Query(30, ge=1, le=730), _=Depends(current_user)):
    return [NavHistoryPoint(**p) for p in nq.get_nav_history(isin, days=days)]

@router.get("/previous", response_model=dict)
def previous(isins: str = Query(None), _=Depends(current_user)):
    if isins:
        isin_list = [i.strip().upper() for i in isins.split(",") if i.strip()]
        if len(isin_list) > 500: raise HTTPException(400, "Max 500 ISINs")
        prev_date = nq.get_previous_nav_date()
        return {"prev_date": prev_date, "map": {i: {"nav": v[0], "date": v[1]} for i in isin_list if (v := nq.get_previous_nav_by_isin(i))}}
    return {"prev_date": nq.get_previous_nav_date(), "map": {}}

@router.get("/scheme/{scheme_code}", response_model=NavSchemeInfo)
def scheme_info(scheme_code: int, _=Depends(current_user)):
    info = nq.get_scheme_info(scheme_code)
    if not info: raise HTTPException(404, f"Scheme {scheme_code} not found")
    return NavSchemeInfo(**info)