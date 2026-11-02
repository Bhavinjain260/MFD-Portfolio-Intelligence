"""
core/email.py
=============
Email send + credential helpers. Pure (no Streamlit).
"""
from __future__ import annotations

import logging
import smtplib
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from core.db import get_conn

log = logging.getLogger(__name__)


def get_credential(key: str, default: str = "") -> str:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM sync_credentials WHERE key = ?", (key,)
        ).fetchone()
    return row[0] if row else default


def set_credential(key: str, value: str) -> None:
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO sync_credentials (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = CURRENT_TIMESTAMP
        """, (key, value))


def get_credentials() -> dict:
    return {
        "imap_user": get_credential("mailback_imap_user"),
        "imap_app_password": get_credential("mailback_imap_app_password"),
        "cams_zip_password": get_credential("cams_mailback_zip_password"),
        "kfintech_zip_password": get_credential("kfintech_mailback_zip_password"),
    }


def credentials_configured() -> bool:
    c = get_credentials()
    return bool(
        c["imap_user"] and c["imap_app_password"] and
        (c["cams_zip_password"] or c["kfintech_zip_password"])
    )


def get_client_email(client_code: str) -> Optional[str]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT email FROM bse_client_master WHERE client_code = ?",
            (client_code,),
        ).fetchone()
    return row[0] if row and row[0] else None


def send_report_email(
    to_email: str,
    subject: str,
    html_body: str,
    pdf_bytes: bytes | None = None,
    pdf_filename: str = "report.pdf",
    cc_emails: list[str] | None = None,
) -> tuple[bool, str]:
    creds = get_credentials()
    if not creds["imap_user"] or not creds["imap_app_password"]:
        return False, "Gmail credentials not configured."

    if not to_email or "@" not in to_email:
        return False, "Invalid recipient email address"

    try:
        msg = MIMEMultipart("alternative")
        msg["From"] = creds["imap_user"]
        msg["To"] = to_email
        msg["Subject"] = subject
        if cc_emails:
            msg["Cc"] = ", ".join(cc_emails)

        msg.attach(MIMEText("Please view this email in HTML mode.", "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html", "utf-8"))

        if pdf_bytes:
            pdf_part = MIMEApplication(pdf_bytes, Name=pdf_filename)
            pdf_part["Content-Disposition"] = f'attachment; filename="{pdf_filename}"'
            msg.attach(pdf_part)

        all_recipients = [to_email] + (cc_emails or [])
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
            server.login(creds["imap_user"], creds["imap_app_password"])
            server.send_message(msg, to_addrs=all_recipients)

        log.info("[EMAIL] Sent '%s' to %s", subject, to_email)
        return True, f"✅ Email sent to {to_email}"
    except smtplib.SMTPAuthenticationError:
        return False, "❌ Authentication failed. Check App Password."
    except smtplib.SMTPException as e:
        return False, f"❌ SMTP error: {e}"
    except Exception as e:
        log.exception("[EMAIL] Failed")
        return False, f"❌ Failed to send: {e}"
