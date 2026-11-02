from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from app.api.deps import current_admin
from app.core.db import SessionLocal
from app.schemas.admin import FailureRow, JobRunRow, RunJobRequest, SettingRow, SettingUpdate
from app.services import nav_service as ns
from app.services import sync_log
from app.services.cache import bump, data_version

router = APIRouter(prefix="/admin", tags=["admin"])

WRITABLE_SETTINGS = {"mailback_poll_enabled", "bse_auto_enabled"}

@router.get("/sync-status")
def sync_status(_=Depends(current_admin)):
    return {"snapshot": ns.get_snapshot_status(), "prev_nav_date": ns.get_previous_nav_date(), "data_version": data_version()}

@router.get("/jobs", response_model=list[JobRunRow])
def list_jobs(limit: int = 50, _=Depends(current_admin)):
    with SessionLocal() as s:
        rows = s.execute(text("SELECT id, task, trigger, started_at, finished_at, ok, msg FROM job_runs ORDER BY id DESC LIMIT :n"), {"n": limit}).fetchall()
    return [JobRunRow(id=r[0], task=r[1], trigger=r[2], started_at=r[3], finished_at=r[4], ok=bool(r[5]) if r[5] is not None else None, msg=r[6]) for r in rows]

@router.post("/jobs/run")
def run_job(req: RunJobRequest, _=Depends(current_admin)):
    valid = {"mailback", "nav_live", "nav_previous", "bse"}
    if req.name not in valid: raise HTTPException(400, f"Unknown task. Valid: {sorted(valid)}")
    with SessionLocal() as s:
        cur = s.execute(text("INSERT INTO job_runs (task, trigger, started_at, ok, msg) VALUES (:t, 'manual', CURRENT_TIMESTAMP, NULL, 'queued')"), {"t": req.name})
        job_id = cur.lastrowid
        s.commit()
    return {"ok": True, "job_id": job_id, "task": req.name, "status": "queued"}

@router.get("/failures", response_model=list[FailureRow])
def failures(limit: int = 200, source: str = "", _=Depends(current_admin)):
    recs = sync_log.read_failures(limit=limit, source_filter=source)
    return [FailureRow(ts=r.get("ts", ""), source=r.get("source", ""), stage=r.get("stage", ""), rta=r.get("rta"), report=r.get("report"), file=r.get("file"), msg=r.get("msg", "")) for r in recs]

@router.get("/settings", response_model=list[SettingRow])
def list_settings(_=Depends(current_admin)):
    with SessionLocal() as s:
        rows = s.execute(text("SELECT key, value, updated_at FROM admin_settings ORDER BY key")).fetchall()
    return [SettingRow(key=r[0], value=r[1], updated_at=r[2]) for r in rows]

@router.put("/settings", response_model=SettingRow)
def update_setting(req: SettingUpdate, _=Depends(current_admin)):
    if req.key not in WRITABLE_SETTINGS: raise HTTPException(400, f"Key {req.key!r} not writable. Allowed: {sorted(WRITABLE_SETTINGS)}")
    with SessionLocal() as s:
        s.execute(text("INSERT INTO admin_settings (key, value, updated_at) VALUES (:k, :v, CURRENT_TIMESTAMP) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP"), {"k": req.key, "v": req.value})
        s.commit()
        row = s.execute(text("SELECT key, value, updated_at FROM admin_settings WHERE key = :k"), {"k": req.key}).fetchone()
    return SettingRow(key=row[0], value=row[1], updated_at=row[2])

@router.post("/nav/redownload")
def redownload_nav(_=Depends(current_admin)):
    result = ns.download_and_save_nav_if_needed(force=True)
    if result.get("ok"): bump()
    return result

@router.post("/nav/sync-previous")
def sync_previous(_=Depends(current_admin)):
    result = ns.sync_previous_business_day_nav_if_needed(force=True)
    if result.get("ok"): bump()
    return result