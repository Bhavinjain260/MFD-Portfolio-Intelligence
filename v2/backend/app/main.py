from __future__ import annotations
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.api.v1 import api_router
from app.core.config import settings
from app.core.db import engine, SessionLocal
from app.models import Base
from app.services import recon as rec

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s | %(message)s")
log = logging.getLogger("app")

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Starting up: initializing database schema & views...")
    try:
        # 1. Create ORM tables if they don't exist (e.g. users, job_runs, upload_history)
        Base.metadata.create_all(engine)
        
        # 2. Create specific views (v_cams_txn_active, v_kfin_txn_active)
        rec.ensure_schema()
        
        # 3. Seed default settings
        with SessionLocal() as s:
            s.execute(text("""
                INSERT OR IGNORE INTO admin_settings (key, value) 
                VALUES ('data_version', '0'), 
                       ('mailback_poll_enabled', '1'), 
                       ('bse_auto_enabled', '1')
            """))
            s.commit()
    except Exception:
        log.exception("Startup DB init failed")
    
    yield
    
    log.info("Shutting down...")

app = FastAPI(title=settings.app_name, version="0.1.0", docs_url="/docs", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
)

app.include_router(api_router)

@app.get("/health")
def health():
    return {"ok": True, "version": app.version}

_frontend_dist = Path(settings.reports_dir).parent / "frontend" / "dist"
if _frontend_dist.exists():
    app.mount("/", StaticFiles(directory=str(_frontend_dist), html=True), name="frontend")
else:
    @app.get("/")
    def _root(): return {"message": "API running", "docs": "/docs"}