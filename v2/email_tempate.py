"""
email_tempate.py
================
HTML email body generators for client-facing reports.

Streamlit-free. Only stdlib dependency is `datetime`.

Note on the filename: `email_tempate` (sic) — intentional, matches the
import statements scattered through main.py, mailback_sync.py, and the
sub-broker sharing UI. Do not rename without updating every import site.

Functions
---------
generate_email_body             — client report emails (Valuation / Capital Gain)
generate_brokerage_email_body   — sub-broker sharing statement emails
"""
from datetime import datetime


# ══════════════════════════════════════════════════════════════
# CLIENT REPORT EMAIL (Valuation / Capital Gain)
# ══════════════════════════════════════════════════════════════
def generate_email_body(
    client_name: str,
    report_type: str,                   # "Capital Gain" or "Valuation"
    period_str: str | None = None,
) -> str:
    """
    Professional HTML email wrapper — used for both Capital Gain & Valuation
    emails sent from the Reports tab.

    period_str : human-readable period label, e.g. "FY 2025-26" or
                 "01-Apr-2026 to 23-Sep-2026". Shown in the header so the
                 recipient knows which report they're looking at without
                 opening the attachment.
    """
    greeting = f"Dear {client_name},"
    period_line = f"<br><strong>Period:</strong> {period_str}" if period_str else ""

    if report_type == "Capital Gain":
        intro = """
<p>Please find attached your <strong>Capital Gain Report</strong>. This report
details realized capital gains/losses on your mutual fund redemptions, computed
using the <strong>FIFO (First In, First Out)</strong> cost-basis method.</p>

<p><strong>What's in the report:</strong></p>
<ul>
    <li>Purchase and redemption details for every matched lot</li>
    <li>STCG / LTCG classification by holding period</li>
    <li>Realized gain/loss per transaction</li>
    <li>Tax summary (equity assumption)</li>
</ul>
"""
    else:  # Valuation
        intro = """
<p>Please find attached your <strong>Portfolio Valuation Report</strong>. This
report gives a snapshot of your mutual fund holdings, current valuations, and
investment performance as of the valuation date.</p>

<p><strong>What's in the report:</strong></p>
<ul>
    <li>Scheme-wise investment summary with current valuations</li>
    <li>Gain/loss analysis across holdings</li>
    <li>Detailed transaction history for the selected period</li>
</ul>
"""

    return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <style>
        body {{
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            line-height: 1.6; color: #333; margin: 0; padding: 20px;
            background-color: #f9f9f9;
        }}
        .email-container {{
            max-width: 700px; margin: 0 auto; background: white;
            padding: 30px; border-radius: 8px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        }}
        .header {{ border-bottom: 3px solid #2980b9; padding-bottom: 15px; margin-bottom: 20px; }}
        .header h2 {{ color: #2980b9; margin: 0; font-size: 24px; }}
        .greeting {{ font-size: 16px; color: #2c3e50; margin-bottom: 15px; }}
        .intro-section {{
            background: #ecf0f1; padding: 15px;
            border-left: 4px solid #2980b9; margin: 20px 0; border-radius: 4px;
        }}
        .intro-section p, .intro-section li {{ color: #2c3e50; font-size: 14px; }}
        .intro-section ul {{ margin: 10px 0; padding-left: 20px; }}
        .attachment-note {{
            background: #d5f4e6; border-left: 4px solid #27ae60;
            padding: 15px; margin: 25px 0; border-radius: 4px;
        }}
        .attachment-note strong {{ color: #27ae60; }}
        .feedback-section {{
            background: #fff3cd; border-left: 4px solid #f39c12;
            padding: 15px; margin: 25px 0; border-radius: 4px;
        }}
        .feedback-section h3 {{ color: #e67e22; margin-top: 0; font-size: 16px; }}
        .feedback-section p {{ margin: 8px 0; color: #7d6608; font-size: 14px; }}
        .footer {{
            border-top: 1px solid #ddd; padding-top: 20px; margin-top: 30px;
            color: #7f8c8d; font-size: 12px;
        }}
        .signature {{ margin-top: 20px; color: #2c3e50; }}
    </style>
</head>
<body>
    <div class="email-container">
        <div class="header">
            <h2>📊 {report_type} Report</h2>
        </div>

        <div class="greeting">
            {greeting}{period_line}
        </div>

        <div class="intro-section">
            {intro}
        </div>

        <div class="attachment-note">
            <strong>✓ Report Attached</strong><br>
            Your <strong>{report_type} Report</strong> is attached to this email
            as a PDF. You can download, print, or save it for your records.
        </div>

        <div class="feedback-section">
            <h3>⏰ Please Review Within 24–48 Hours</h3>
            <p>
                Kindly review the attached report carefully. If you notice any
                discrepancy — a missing transaction, incorrect folio number, or
                a valuation that doesn't match your records — please reply to
                this email within 24–48 hours so we can correct it.
            </p>
        </div>

        <p style="color: #2c3e50; margin: 20px 0;"><strong>What to check:</strong></p>
        <ul style="color: #2c3e50; margin: 10px 0;">
            <li>Folio numbers and scheme names</li>
            <li>Transaction dates, units, and amounts</li>
            <li>NAV values used and valuation date</li>
            <li>For Capital Gain reports: purchase and redemption dates per matched lot</li>
        </ul>

        <div class="footer">
            <p>
                <strong>Contact:</strong> If you have any questions about this
                report, please reply to this email.
            </p>
            <p>
                <strong>Generated:</strong> {datetime.now().strftime("%d %B %Y at %I:%M %p")}<br>
                This is an automated report from the Portfolio Intelligence platform.
            </p>
            <p style="margin-top: 20px; color: #34495e;">
                Thank you for your continued trust.
            </p>
            <div class="signature">
                <strong>Best Regards,</strong><br>
                Portfolio Intelligence Team<br>
                <em>Your Investment Partner</em>
            </div>
        </div>
    </div>
</body>
</html>"""


# ══════════════════════════════════════════════════════════════
# SUB-BROKER SHARING STATEMENT EMAIL
# ══════════════════════════════════════════════════════════════
def generate_brokerage_email_body(
    sub_broker_name: str,
    period_label: str,
    total_brokerage: float,
    sub_broker_share: float,
    my_share: float,
    transfer: dict | None = None,
) -> str:
    """
    HTML body for the sub-broker sharing statement email.

    transfer : optional dict from sub_broker_payouts — expected keys:
               status, transferred_amount, transfer_date,
               transfer_reference, transfer_notes.
               When present, a colour-coded transfer block is rendered.
    """
    def fi(v):
        try:    return f"₹{float(v):,.2f}"
        except Exception: return "—"

    transfer_block = ""
    if transfer:
        status = (transfer.get("status") or "pending").lower()
        if status == "transferred":
            colour, label = "#27ae60", "✅ Transferred"
        elif status == "partial":
            colour, label = "#f39c12", "⏳ Partially Transferred"
        else:
            colour, label = "#c0392b", "⏰ Pending"
        transfer_block = f"""
        <div style="background:#f8f9fa;border-left:4px solid {colour};padding:14px 18px;margin:20px 0;border-radius:4px;">
          <div style="font-size:13px;font-weight:bold;color:{colour};margin-bottom:8px;">{label}</div>
          <table style="width:100%;font-size:13px;color:#2c3e50;">
            <tr><td style="padding:3px 0;">Amount Transferred</td>
                <td style="text-align:right;font-weight:bold;">{fi(transfer.get('transferred_amount', 0))}</td></tr>
            <tr><td style="padding:3px 0;">Transfer Date</td>
                <td style="text-align:right;">{transfer.get('transfer_date') or '—'}</td></tr>
            <tr><td style="padding:3px 0;">Reference</td>
                <td style="text-align:right;">{transfer.get('transfer_reference') or '—'}</td></tr>
            <tr><td style="padding:3px 0;">Notes</td>
                <td style="text-align:right;">{transfer.get('transfer_notes') or '—'}</td></tr>
          </table>
        </div>
        """

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family:'Segoe UI',Tahoma,sans-serif;color:#333;background:#f9f9f9;padding:20px;margin:0;">
  <div style="max-width:680px;margin:0 auto;background:#fff;padding:30px;border-radius:8px;box-shadow:0 2px 8px rgba(0,0,0,0.08);">
    <div style="border-bottom:3px solid #2980b9;padding-bottom:14px;margin-bottom:20px;">
      <h2 style="color:#2980b9;margin:0;font-size:22px;">💹 Brokerage Sharing Statement</h2>
      <div style="color:#7f8c8d;font-size:13px;margin-top:6px;">Period: <strong>{period_label}</strong></div>
    </div>

    <p style="font-size:15px;color:#2c3e50;">Dear <strong>{sub_broker_name}</strong>,</p>

    <p style="font-size:14px;color:#2c3e50;line-height:1.6;">
      Please find below the brokerage generated on your assigned clients for <strong>{period_label}</strong>,
      along with your share and the transfer status.
    </p>

    <table style="width:100%;border-collapse:collapse;margin:22px 0;font-size:14px;">
      <tr style="background:#ecf0f1;">
        <td style="padding:12px 16px;color:#7f8c8d;font-size:12px;text-transform:uppercase;">Total Brokerage</td>
        <td style="padding:12px 16px;text-align:right;font-weight:bold;color:#2c3e50;">{fi(total_brokerage)}</td>
      </tr>
      <tr style="background:#eaf2f8;">
        <td style="padding:12px 16px;color:#7f8c8d;font-size:12px;text-transform:uppercase;">Your Share</td>
        <td style="padding:12px 16px;text-align:right;font-weight:bold;color:#27ae60;">{fi(sub_broker_share)}</td>
      </tr>
      <tr>
        <td style="padding:12px 16px;color:#7f8c8d;font-size:12px;text-transform:uppercase;">Firm's Share</td>
        <td style="padding:12px 16px;text-align:right;font-weight:bold;color:#2c3e50;">{fi(my_share)}</td>
      </tr>
    </table>

    {transfer_block}

    <div style="background:#d5f4e6;border-left:4px solid #27ae60;padding:12px 16px;margin:20px 0;border-radius:4px;font-size:13px;color:#1e7e46;">
      <strong>✓ Detailed report attached</strong><br>
      Please review the attached PDF for the full client-wise breakdown.
    </div>

    <p style="font-size:13px;color:#7f8c8d;line-height:1.6;">
      If you notice any discrepancy in the amounts, client attribution, or sharing percentage,
      please reply to this email within 24–48 hours so we can correct it.
    </p>

    <div style="border-top:1px solid #ddd;padding-top:16px;margin-top:26px;color:#7f8c8d;font-size:12px;">
      Generated: {datetime.now().strftime('%d %B %Y at %I:%M %p')}<br>
      This is an automated statement from the Portfolio Intelligence platform.
    </div>
  </div>
</body></html>"""