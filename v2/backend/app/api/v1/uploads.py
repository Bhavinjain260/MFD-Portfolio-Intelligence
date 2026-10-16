from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import text

from app.api.deps import current_user
from app.core.db import SessionLocal
from app.schemas.uploads import UploadResponse
from app.services import data_manager as dm
from app.services.cache import bump

router = APIRouter(prefix="/uploads", tags=["uploads"])


PARSERS = {
    "bse/client":       dm.parse_bse_client_master,
    "bse/sip":          dm.parse_bse_sip,
    "bse/scheme":       dm.parse_bse_scheme_master,
    "cams/wbr4":        dm.parse_cams_wbr4_aum,
    "cams/wbr9":        dm.parse_cams_wbr9_folio,
    "cams/wbr2":        dm.parse_cams_wbr2_transaction,
    "cams/wbr49":       dm.parse_cams_wbr49_sip,
    "cams/wbr77":       dm.parse_cams_wbr77_brokerage,
    "kfin/mfsd203":     dm.parse_kfin_mfsd203_aum,
    "kfin/mfsd211":     dm.parse_kfin_mfsd211_folio,
    "kfin/mfsd201":     dm.parse_kfin_mfsd201_transaction,
    "kfin/mfsd243":     dm.parse_kfin_mfsd243_sip,
    "kfin/mfsd205":     dm.parse_kfin_mfsd205_brokerage,
    "amfi/nav":         dm.parse_nav_file,
}


@router.post("/{kind:path}", response_model=UploadResponse)
async def upload(
    kind: str,
    file: UploadFile = File(...),
    replace: bool = Form(False),
    _=Depends(current_user),
):
    fn = PARSERS.get(kind)
    if not fn:
        raise HTTPException(400, f"Unknown upload kind '{kind}'. "
                                 f"Valid: {sorted(PARSERS.keys())}")

    # The parsers expect a file-like object with .seek() and .read().
    # UploadFile.file is a SpooledTemporaryFile — good enough.
    result = fn(file.file, replace)
    if isinstance(result, tuple) and len(result) == 3:
        ok, msg, preview = result
    else:
        ok, msg = result
        preview = {}

    if ok:
        bump()

    return UploadResponse(
        ok=bool(ok),
        message=str(msg),
        rows=int((preview or {}).get("rows", 0) or 0),
        updated=int((preview or {}).get("updated", 0) or 0),
        skipped=int((preview or {}).get("skipped", 0) or 0),
    )