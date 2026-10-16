from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

from app.api.deps import current_user
from app.schemas.reports import EmailReportRequest, EmailReportResponse
from app.services import mailback as mb

router = APIRouter(prefix="/reports", tags=["reports"])


# Valuation / capital-gain PDF generation lives in the app-level module.
# Wire those generators in once Step 3.9's glue module is created.
# For now, expose a stub endpoint that returns 501 so the frontend can
# be built against the correct shape.
@router.post("/valuation")
def valuation_pdf(client_code: str, fy: str | None = None,
                  valuation_date: str | None = None,
                  _=Depends(current_user)):
    from app.services import report_generator as rg
    try:
        pdf = rg.generate_valuation_pdf(
            client_code=client_code, fy=fy, valuation_date=valuation_date,
        )
    except NotImplementedError:
        raise HTTPException(501, "Valuation PDF generation pending (Step 3.9)")
    if not pdf:
        raise HTTPException(500, "PDF generation failed")
    return Response(
        content=pdf, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="Valuation_{client_code}.pdf"'},
    )


@router.post("/capital-gain")
def cg_pdf(client_code: str, fy: str | None = None,
           _=Depends(current_user)):
    from app.services import report_generator as rg
    try:
        pdf = rg.generate_capital_gain_pdf(client_code=client_code, fy=fy)
    except NotImplementedError:
        raise HTTPException(501, "Capital-gain PDF generation pending (Step 3.9)")
    if not pdf:
        raise HTTPException(500, "PDF generation failed")
    return Response(
        content=pdf, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="CapitalGain_{client_code}.pdf"'},
    )


@router.post("/email", response_model=EmailReportResponse)
def email_report(req: EmailReportRequest, _=Depends(current_user)):
    # Placeholder: real implementation generates a PDF and calls
    # mb.send_report_email(). Kept minimal for now so the endpoint exists.
    html = f"<p>{req.report_type} report for {req.client_code}</p>"
    ok, msg = mb.send_report_email(
        to_email=req.to_email,
        subject=f"{req.report_type} — {req.client_code}",
        html_body=html,
        pdf_bytes=None,
        pdf_filename="report.pdf",
        cc_emails=req.cc_emails or None,
    )
    return EmailReportResponse(ok=ok, message=msg)