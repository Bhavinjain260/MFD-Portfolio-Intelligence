from pydantic import BaseModel


class UploadResponse(BaseModel):
    ok: bool
    message: str
    rows: int = 0
    updated: int = 0
    skipped: int = 0


class JobQueued(BaseModel):
    job_id: int
    task: str
    status: str