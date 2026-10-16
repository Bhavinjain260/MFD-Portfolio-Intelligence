from fastapi import APIRouter, HTTPException, Query

from app.services import nav_queries as nq
from app.services import nav_service as ns
from app.schemas.nav import (
    NavHistoryPoint, NavSchemeInfo, NavSnapshotStatus, NavValue,
)

router = APIRouter(prefix="/nav", tags=["nav"])


@router.get("/snapshot-status", response_model=NavSnapshotStatus)
def snapshot_status():
    return NavSnapshotStatus(**ns.get_snapshot_status())


@router.get("/latest", response_model=NavValue)
def latest(isin: str = Query(..., min_length=3)):
    hit = nq.get_latest_nav_by_isin(isin)
    if not hit:
        raise HTTPException(404, f"No NAV for {isin}")
    nav, date_str = hit
    return NavValue(isin=isin, nav=nav, nav_date=date_str)


@router.get("/{isin}/history", response_model=list[NavHistoryPoint])
def history(isin: str, days: int = Query(30, ge=1, le=730)):
    return [NavHistoryPoint(**p) for p in nq.get_nav_history(isin, days=days)]


@router.get("/previous")
def previous():
    return {
        "prev_date": nq.get_previous_nav_date(),
        "map": {k: {"nav": v[0], "date": v[1]} for k, v in nq.get_previous_navs_map().items()},
    }


@router.get("/scheme/{scheme_code}", response_model=NavSchemeInfo)
def scheme_info(scheme_code: int):
    info = nq.get_scheme_info(scheme_code)
    if not info:
        raise HTTPException(404, f"Scheme {scheme_code} not found")
    return NavSchemeInfo(**info)