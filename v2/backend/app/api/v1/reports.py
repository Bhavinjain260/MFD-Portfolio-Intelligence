from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
from app.api.deps import current_user
from app.core.compat import get_conn
from app.schemas.reports import EmailReportRequest, EmailReportResponse
from app.services import mailback as mb
from app.services import report_generator as rg

router = APIRouter(prefix="/reports", tags=["reports"])

class PDFRequest(BaseModel):
    client_code: str
    fy: str | None = None
    valuation_date: str | None = None

@router.post("/valuation")
def valuation_pdf(req: PDFRequest, _=Depends(current_user)):
    try:
        pdf = rg.generate_valuation_pdf(client_code=req.client_code, fy=req.fy, valuation_date=req.valuation_date)
    except Exception as e: raise HTTPException(400, str(e))
    return Response(content=pdf, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="Valuation_{req.client_code}.pdf"'})

@router.post("/capital-gain")
def cg_pdf(req: PDFRequest, _=Depends(current_user)):
    try:
        pdf = rg.generate_capital_gain_pdf(client_code=req.client_code, fy=req.fy)
    except Exception as e: raise HTTPException(400, str(e))
    return Response(content=pdf, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="CapitalGain_{req.client_code}.pdf"'})

@router.post("/email", response_model=EmailReportResponse)
def email_report(req: EmailReportRequest, _=Depends(current_user)):
    with get_conn() as conn:
        row = conn.execute("SELECT email, primary_holder_first_name || ' ' || primary_holder_last_name FROM bse_client_master WHERE client_code = ?", (req.client_code,)).fetchone()
    if not row or not row[0]: raise HTTPException(400, "Client email not found in master.")
    
    to_email, name = row[0], row[1]
    
    safe_cc = []
    if req.cc_emails:
        for e in req.cc_emails:
            if e.endswith("@gmail.com") or e.endswith("@yourdomain.com"):
                safe_cc.append(e)

    pdf = rg.generate_valuation_pdf(client_code=req.client_code, fy=req.fy_str) if req.report_type == "Valuation" else rg.generate_capital_gain_pdf(client_code=req.client_code, fy=req.fy_str)
    html = rg.generate_email_body(name, req.report_type, req.period_str)
    
    ok, msg = mb.send_report_email(to_email=to_email, subject=f"{req.report_type} - {name}", html_body=html, pdf_bytes=pdf, cc_emails=safe_cc)
    return EmailReportResponse(ok=ok, message=msg)