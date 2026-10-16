from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import current_user
from app.schemas.aum import (
    MarkInactiveRequest, OverrideRow, ReconRow, ReconSummary,
)
from app.services import recon as rec
from app.services.cache import data_version

router = APIRouter(prefix="/aum", tags=["aum"])


@router.get("", response_model=list[ReconRow])
def list_all(ignore_dates: bool = True, _=Depends(current_user)):
    df = rec.compute_recon(data_version(), ignore_dates=ignore_dates)
    return [ReconRow(**r) for r in df.to_dict(orient="records")]


@router.get("/reconciliation", response_model=dict)
def reconciliation(status: str = Query("all"),
                   rta: str = Query("all"),
                   q: str = Query(""),
                   _=Depends(current_user)):
    df = rec.compute_recon(data_version(), ignore_dates=True)

    summary = ReconSummary(
        compared=len(df),
        matched=int((df["status"] == "matched").sum()),
        short=int((df["status"] == "short").sum()),
        excess=int((df["status"] == "excess").sum()),
    )

    view = df[df["status"] != "matched"]
    if rta.lower() != "all":
        view = view[view["rta"] == rta]
    if status.lower() != "all":
        view = view[view["status"] == status.lower()]
    if q.strip():
        mask = (
            view["folio"].str.contains(q, case=False, na=False) |
            view["investor"].str.contains(q, case=False, na=False) |
            view["scheme"].str.contains(q, case=False, na=False) |
            view["amc"].str.contains(q, case=False, na=False)
        )
        view = view[mask]

    return {
        "summary": summary.model_dump(),
        "rows": [ReconRow(**r) for r in view.to_dict(orient="records")],
    }


@router.get("/overrides", response_model=list[OverrideRow])
def overrides(_=Depends(current_user)):
    df = rec.list_overrides()
    return [OverrideRow(**r) for r in df.to_dict(orient="records")]


@router.post("/mark-inactive")
def mark_inactive(req: MarkInactiveRequest, _=Depends(current_user)):
    try:
        rec.set_inactive(req.rta, req.folio, req.txn_no, req.fund, req.reason)
    except Exception as e:
        raise HTTPException(500, str(e))
    return {"ok": True}


@router.post("/overrides/{override_id}/restore")
def restore_override(override_id: int, _=Depends(current_user)):
    try:
        rec.restore(override_id)
    except Exception as e:
        raise HTTPException(500, str(e))
    return {"ok": True}