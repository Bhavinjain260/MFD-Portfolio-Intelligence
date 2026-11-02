from pydantic import BaseModel


class ReconRow(BaseModel):
    rta: str
    folio: str
    product: str
    scheme: str
    investor: str
    amc: str
    aum_date: str | None
    aum_units: float
    our_units: float
    diff: float
    folio_master_units: float | None
    last_txn_date: str | None
    status: str


class ReconSummary(BaseModel):
    compared: int
    matched: int
    short: int
    excess: int


class MarkInactiveRequest(BaseModel):
    rta: str
    folio: str
    txn_no: str
    fund: str = ""
    reason: str = ""


class OverrideRow(BaseModel):
    id: int
    rta: str
    folio: str
    txn_no: str
    fund: str
    reason: str | None
    created_at: str