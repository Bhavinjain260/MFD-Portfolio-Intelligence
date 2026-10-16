from datetime import datetime
from pydantic import BaseModel


class JobRunRow(BaseModel):
    id: int
    task: str
    trigger: str
    started_at: datetime
    finished_at: datetime | None
    ok: bool | None
    msg: str | None


class FailureRow(BaseModel):
    ts: str
    source: str
    stage: str
    rta: str | None
    report: str | None
    file: str | None
    msg: str


class SettingRow(BaseModel):
    key: str
    value: str | None
    updated_at: datetime | None


class SettingUpdate(BaseModel):
    key: str
    value: str


class RunJobRequest(BaseModel):
    name: str