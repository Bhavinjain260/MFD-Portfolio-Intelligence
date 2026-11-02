from fastapi import APIRouter

from . import (
    admin, auth, aum, capital_gains, clients, meta, nav, reports, uploads,
)

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router)
api_router.include_router(meta.router)
api_router.include_router(nav.router)
api_router.include_router(clients.router)
api_router.include_router(capital_gains.router)
api_router.include_router(aum.router)
api_router.include_router(uploads.router)
api_router.include_router(reports.router)
api_router.include_router(admin.router)