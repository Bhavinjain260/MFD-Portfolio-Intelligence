from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.v1 import api_router
from app.core.config import settings
from app.services import recon as rec

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s | %(message)s",
)
log = logging.getLogger("app")

app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    docs_url="/docs",
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],  # Vite dev
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)


@app.on_event("startup")
def _startup() -> None:
    # Ensure the reconciliation views exist — idempotent.
    try:
        rec.ensure_schema()
    except Exception:
        log.exception("ensure_schema() failed at startup")


@app.get("/health")
def health():
    return {"ok": True, "version": app.version}


# Mount the built React frontend last so it doesn't shadow /api or /docs.
_frontend_dist = Path(settings.reports_dir).parent / "frontend" / "dist"
if _frontend_dist.exists():
    app.mount("/", StaticFiles(directory=str(_frontend_dist), html=True), name="frontend")
else:
    @app.get("/")
    def _root():
        return {
            "message": "API running. Frontend not built yet.",
            "docs": "/docs",
        }