from pydantic import BaseModel


class EmailReportRequest(BaseModel):
    client_code: str
    report_type: str           # "Capital Gain" | "Valuation"
    to_email: str
    cc_emails: list[str] = []
    fy_str: str | None = None
    period_str: str | None = None


class EmailReportResponse(BaseModel):
    ok: bool
    message: str