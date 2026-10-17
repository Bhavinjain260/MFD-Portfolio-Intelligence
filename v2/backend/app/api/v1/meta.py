from fastapi import APIRouter, Depends

from app.api.deps import current_user
from app.services.cache import data_version

router = APIRouter(prefix="/meta", tags=["meta"])


@router.get("/data-version")
def get_data_version(_=Depends(current_user)):
    return {"data_version": data_version()}