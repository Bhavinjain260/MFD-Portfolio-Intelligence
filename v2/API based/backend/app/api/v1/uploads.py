import time
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import text
from app.api.deps import current_admin
from app.core.db import SessionLocal
from app.schemas.uploads import UploadResponse
from app.services import data_manager as dm
from app.services.cache import bump

router = APIRouter(prefix="/uploads", tags=["uploads"])

MAX_UPLOAD_BYTES = 200 * 1024 * 1024

PARSERS = {
    "bse/client":   dm.parse_bse_client_master, "bse/sip": dm.parse_bse_sip, "bse/scheme": dm.parse_bse_scheme_master,
    "cams/wbr4":    dm.parse_cams_wbr4_aum, "cams/wbr9": dm.parse_cams_wbr9_folio, "cams/wbr2": dm.parse_cams_wbr2_transaction,
    "cams/wbr49":   dm.parse_cams_wbr49_sip, "cams/wbr77": dm.parse_cams_wbr77_brokerage,
    "kfin/mfsd203": dm.parse_kfin_mfsd203_aum, "kfin/mfsd211": dm.parse_kfin_mfsd211_folio, "kfin/mfsd201": dm.parse_kfin_mfsd201_transaction,
    "kfin/mfsd243": dm.parse_kfin_mfsd243_sip, "kfin/mfsd205": dm.parse_kfin_mfsd205_brokerage, "amfi/nav": dm.parse_nav_file,
}

def _file_size(file: UploadFile) -> int:
    size = getattr(file, "size", None)
    if size is not None: return int(size)
    pos = file.file.tell()
    file.file.seek(0, 2)
    size = file.file.tell()
    file.file.seek(pos)
    return int(size)

@router.post("/{kind:path}", response_model=UploadResponse)
def upload(kind: str, file: UploadFile = File(...), replace: bool = Form(False), confirm: bool = Form(False), _=Depends(current_admin)):
    if replace and not confirm: raise HTTPException(400, "Pass confirm=true to replace data.")
    fn = PARSERS.get(kind)
    if not fn: raise HTTPException(400, f"Unknown kind {kind}")
    
    size = _file_size(file)
    if size > MAX_UPLOAD_BYTES: raise HTTPException(413, f"File too large: {size/1024/1024:.1f}MB")

    start = time.time()
    try:
        result = fn(file.file, replace)
    except Exception as e:
        _log_upload(kind, file.filename, size, 0, False, str(e), start)
        raise HTTPException(422, f"Parser failed: {e}")

    ok, msg, preview = result if len(result) == 3 else (*result, {})
    if ok: bump()
    
    _log_upload(kind, file.filename, size, preview.get("rows",0), ok, msg, start)
    return UploadResponse(ok=ok, message=msg, rows=preview.get("rows",0), updated=preview.get("updated",0), skipped=preview.get("skipped",0))

def _log_upload(kind: str, filename: str, size: int, rows: int, ok: bool, msg: str, start: float):
    try:
        with SessionLocal() as s:
            s.execute(text("""
                INSERT INTO upload_history (kind, filename, size_bytes, rows, success, message, duration_ms)
                VALUES (:k, :f, :s, :r, :ok, :m, :d)
            """), {"k": kind, "f": filename, "s": size, "r": rows, "ok": 1 if ok else 0, "m": msg[:500], "d": int((time.time()-start)*1000)})
            s.commit()
    except Exception:
        pass