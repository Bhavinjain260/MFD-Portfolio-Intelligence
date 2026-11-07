from datetime import datetime

def generate_email_body(
    client_name: str,
    report_type: str,  # "Capital Gain" or "Valuation"
) -> str:
    """
    Generate professional email body with intro, report details, and feedback request.
    
    Args:
        client_name: Client's full name
        report_type: "Capital Gain" or "Valuation"
    
    Returns:
        Complete HTML email body
    """
    
    greeting = f"Dear {client_name},"
    
    if report_type == "Capital Gain":
        intro = f"""
<p>We are pleased to share your <strong>Capital Gain Report</strong> for review. This report details all realized 
capital gains/losses from your mutual fund investments, calculated using the FIFO (First In First Out) method.</p>

<p><strong>Report Highlights:</strong></p>
<ul>
    <li>Summary of all buy and sell transactions</li>
    <li>Cost basis and realized gains/losses per transaction</li>
    <li>Useful for income tax filing and investment tracking</li>
</ul>
"""
    else:  # Valuation
        intro = f"""
<p>We are pleased to share your <strong>Portfolio Valuation Report</strong> for review. This report provides a 
comprehensive snapshot of your mutual fund holdings, current valuations, and investment performance as of the 
valuation date mentioned in the report.</p>

<p><strong>Report Highlights:</strong></p>
<ul>
    <li>Scheme-wise investment summary and current NAV-based valuations</li>
    <li>Gain/Loss analysis across all holdings</li>
    <li>Detailed transaction history for each holding</li>
</ul>
"""
    
    email_body = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <style>
        body {{
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            line-height: 1.6;
            color: #333;
            margin: 0;
            padding: 20px;
            background-color: #f9f9f9;
        }}
        .email-container {{
            max-width: 700px;
            margin: 0 auto;
            background: white;
            padding: 30px;
            border-radius: 8px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        }}
        .header {{
            border-bottom: 3px solid #2980b9;
            padding-bottom: 15px;
            margin-bottom: 20px;
        }}
        .header h2 {{
            color: #2980b9;
            margin: 0;
            font-size: 24px;
        }}
        .greeting {{
            font-size: 16px;
            color: #2c3e50;
            margin-bottom: 15px;
        }}
        .intro-section {{
            background: #ecf0f1;
            padding: 15px;
            border-left: 4px solid #2980b9;
            margin: 20px 0;
            border-radius: 4px;
        }}
        .intro-section p {{
            margin: 10px 0;
            color: #2c3e50;
            font-size: 14px;
        }}
        .intro-section ul {{
            margin: 10px 0;
            padding-left: 20px;
            color: #2c3e50;
            font-size: 14px;
        }}
        .intro-section li {{
            margin: 8px 0;
        }}
        .attachment-note {{
            background: #d5f4e6;
            border-left: 4px solid #27ae60;
            padding: 15px;
            margin: 25px 0;
            border-radius: 4px;
        }}
        .attachment-note strong {{
            color: #27ae60;
        }}
        .feedback-section {{
            background: #fff3cd;
            border-left: 4px solid #f39c12;
            padding: 15px;
            margin: 25px 0;
            border-radius: 4px;
        }}
        .feedback-section h3 {{
            color: #e67e22;
            margin-top: 0;
            font-size: 16px;
        }}
        .feedback-section p {{
            margin: 8px 0;
            color: #7d6608;
            font-size: 14px;
        }}
        .footer {{
            border-top: 1px solid #ddd;
            padding-top: 20px;
            margin-top: 30px;
            color: #7f8c8d;
            font-size: 12px;
        }}
        .footer-link {{
            color: #2980b9;
            text-decoration: none;
        }}
        .footer-link:hover {{
            text-decoration: underline;
        }}
        .signature {{
            margin-top: 20px;
            color: #2c3e50;
        }}
        .report-attached {{
            color: #27ae60;
            font-weight: bold;
        }}
    </style>
</head>
<body>
    <div class="email-container">
        <!-- Header -->
        <div class="header">
            <h2>📊 {report_type} Report</h2>
        </div>
        
        <!-- Greeting -->
        <div class="greeting">
            {greeting}
        </div>
        
        <!-- Introduction -->
        <div class="intro-section">
            {intro}
        </div>
        
        <!-- Attachment Note -->
        <div class="attachment-note">
            <strong>✓ Report Attached</strong><br>
            Your <strong>{report_type} Report</strong> is attached below as a PDF file. 
            You can download, print, or save it for your records.
        </div>
        
        <!-- Feedback Request -->
        <div class="feedback-section">
            <h3>⏰ Action Required</h3>
            <p>
                Please review the attached report carefully. If you notice any discrepancies, 
                errors in transaction details, or have any questions, <strong>please revert within 24-48 hours</strong>.
            </p>
            <p>
                This will help us ensure accuracy in your portfolio records and make any necessary corrections 
                at the earliest.
            </p>
        </div>
        
        <!-- Key Points -->
        <p style="color: #2c3e50; margin: 20px 0;">
            <strong>What to Look For:</strong>
        </p>
        <ul style="color: #2c3e50; margin: 10px 0;">
            <li>Verify all folio numbers and scheme names</li>
            <li>Check transaction dates and amounts</li>
            <li>Confirm NAV values and current holdings</li>
            <li>Review valuation dates and calculation methods</li>
        </ul>
        
        <!-- Footer -->
        <div class="footer">
            <p>
                <strong>Contact Information:</strong><br>
                If you have any questions or need clarification on any aspect of this report, 
                please don't hesitate to reach out to us.
            </p>
            <p>
                <strong>Report Generated:</strong> {datetime.now().strftime("%d %B %Y at %I:%M %p")}<br>
                This is an automated report. For support, contact our team.
            </p>
            <p style="margin-top: 20px; color: #34495e;">
                Thank you for your trust in our services.
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
    
    return email_body