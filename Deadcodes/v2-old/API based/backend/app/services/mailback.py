#!/usr/bin/env python3
"""
mailback.py
===========
Polls Gmail via IMAP for CAMS and KFinTech mailback reports, downloads
and extracts zips, auto-parses into the DB, and moves successful files
to done/.
"""
import imaplib
import email
import re
import io
import logging
import time
from pathlib import Path
from email.header import decode_header
from datetime import datetime

import pyzipper
import requests

from app.services import data_manager as dm
from app.services import sync_log as sflog
from app.core.config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("mailback_sync")


IMAP_HOST = "imap.gmail.com"
IMAP_TIMEOUT = 30
IMAP_KEEPALIVE_INTERVAL = 300  # 5 min

RTA_CONFIG = {
    "CAMS": {
        "sender": "donotreply@camsonline.com",
        "subject_filter": "WBR",
        "reports": ["WBR2", "WBR9", "WBR49", "WBR77", "WBR4"],
        "parsers": {
            "WBR2": dm.parse_cams_wbr2_transaction,
            "WBR9": dm.parse_cams_wbr9_folio,
            "WBR49": dm.parse_cams_wbr49_sip,
            "WBR77": dm.parse_cams_wbr77_brokerage,
            "WBR4": dm.parse_cams_wbr4_aum,
        },
        "password_key": "cams_mailback_zip_password",
    },
    "KFinTech": {
        "sender": "distributorcare@kfintech.com",
        "subject_filter": "Subscribed",
        "reports": [
            "MFSD201", "MFSD211", "MFSD205", "MFSD243", "MFSD203",
            "MFSD307", "MFSD313", "MFSD311",
        ],
        "parsers": {
            "MFSD201": dm.parse_kfin_mfsd201_transaction,
            "MFSD211": dm.parse_kfin_mfsd211_folio,
            "MFSD205": dm.parse_kfin_mfsd205_brokerage,
            "MFSD243": dm.parse_kfin_mfsd243_sip,
            "MFSD203": dm.parse_kfin_mfsd203_aum,
        },
        "aliases": {
            "MFSD307": "MFSD201",
            "MFSD313": "MFSD243",
            "MFSD311": "MFSD211",
        },
        "password_key": "kfintech_mailback_zip_password",
    },
}

REPORT_CODES = []
for rta_config in RTA_CONFIG.values():
    REPORT_CODES.extend(rta_config["reports"])

BASE_DIR = Path(settings.mailback_base_dir)
POLL_ENABLED_KEY = "mailback_poll_enabled"


def is_polling_enabled() -> bool:
    return dm.get_credential(POLL_ENABLED_KEY, "1") == "1"


def set_polling_enabled(enabled: bool) -> None:
    dm.set_credential(POLL_ENABLED_KEY, "1" if enabled else "0")


def get_credentials() -> dict:
    return {
        "imap_user": dm.get_credential("mailback_imap_user"),
        "imap_app_password": dm.get_credential("mailback_imap_app_password"),
        "cams_zip_password": dm.get_credential("cams_mailback_zip_password"),
        "kfintech_zip_password": dm.get_credential("kfintech_mailback_zip_password"),
    }


def save_credentials(imap_user: str, imap_app_password: str,
                     cams_zip_password: str, kfintech_zip_password: str) -> None:
    if imap_user:
        dm.set_credential("mailback_imap_user", imap_user.strip())
    if imap_app_password:
        dm.set_credential("mailback_imap_app_password", imap_app_password.strip())
    if cams_zip_password:
        dm.set_credential("cams_mailback_zip_password", cams_zip_password.strip())
    if kfintech_zip_password:
        dm.set_credential("kfintech_mailback_zip_password", kfintech_zip_password.strip())


def credentials_configured() -> bool:
    c = get_credentials()
    return bool(
        c["imap_user"] and c["imap_app_password"] and
        (c["cams_zip_password"] or c["kfintech_zip_password"])
    )


def _decode_subject(raw_subject) -> str:
    parts = decode_header(raw_subject or "")
    out = ""
    for text, enc in parts:
        if isinstance(text, bytes):
            try:
                out += text.decode(enc or "utf-8", errors="replace")
            except (LookupError, UnicodeDecodeError):
                out += text.decode("utf-8", errors="replace")
        else:
            out += text
    return out


def _safe_decode(payload: bytes, charset: str | None) -> str:
    if not payload:
        return ""
    for enc in (charset, "utf-8", "latin-1"):
        if not enc:
            continue
        try:
            return payload.decode(enc, errors="replace")
        except (LookupError, UnicodeDecodeError):
            continue
    return payload.decode("utf-8", errors="replace")


def _get_body_text(msg) -> str:
    if msg.is_multipart():
        html_part = None
        text_part = None
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype == "text/html" and html_part is None:
                html_part = part
            elif ctype == "text/plain" and text_part is None:
                text_part = part
        chosen = html_part or text_part
        if chosen:
            payload = chosen.get_payload(decode=True)
            charset = chosen.get_content_charset()
            return _safe_decode(payload, charset)
        return ""
    else:
        payload = msg.get_payload(decode=True)
        charset = msg.get_content_charset()
        return _safe_decode(payload, charset)


def _detect_rta(sender: str) -> str | None:
    sender_lower = sender.lower()
    for rta, config in RTA_CONFIG.items():
        if config["sender"].lower() in sender_lower:
            return rta
    return None


def _is_no_data(body: str) -> bool:
    if not body:
        return True
    lowered = body.lower()
    patterns = [
        r"no\s+data\s+available", r"no\s+data", r"no\s+records?\s+found",
        r"no\s+records?\s+to\s+report", r"no\s+transactions?\s+during\s+this\s+period",
        r"no\s+new\s+transactions?", r"no\s+transactions?", r"nil\s+report",
        r"there\s+are\s+no\s+records", r"there\s+is\s+no\s+data",
    ]
    return any(re.search(p, lowered) for p in patterns)


def _get_imap_connection():
    creds = get_credentials()
    imap = imaplib.IMAP4_SSL(IMAP_HOST, timeout=IMAP_TIMEOUT)
    imap.login(creds["imap_user"], creds["imap_app_password"])
    imap.select("INBOX")
    return imap


def _fetch_email_safe(imap, mid: bytes, max_retries: int = 2) -> tuple[bool, bytes | None]:
    for attempt in range(max_retries):
        try:
            status, msg_data = imap.fetch(mid, "(BODY.PEEK[])")
            if status == "OK":
                return True, msg_data[0][1]
            return False, None
        except (imaplib.IMAP4.abort, OSError, EOFError, BrokenPipeError) as e:
            log.warning("[IMAP-FETCH] Attempt %d/%d failed: %s", attempt + 1, max_retries, type(e).__name__)
            if attempt < max_retries - 1:
                time.sleep(0.5)
                try: imap.noop()
                except Exception: raise
            else:
                return False, None
    return False, None


def _search_safe(imap, query: str, max_retries: int = 2) -> tuple[bool, list]:
    for attempt in range(max_retries):
        try:
            status, data = imap.search(None, query)
            if status == "OK":
                return True, data[0].split()
            return False, []
        except (imaplib.IMAP4.abort, OSError, EOFError, BrokenPipeError) as e:
            log.warning("[IMAP-SEARCH] Attempt %d/%d failed: %s", attempt + 1, max_retries, type(e).__name__)
            if attempt < max_retries - 1:
                time.sleep(0.5)
                try: imap.noop()
                except Exception: raise
            else:
                return False, []
    return False, []


def _mark_seen_safe(imap, mid: bytes, rta: str = "", report: str = "",
                    subject: str = "", max_retries: int = 3) -> bool:
    for attempt in range(max_retries):
        try:
            status, _ = imap.store(mid, "+FLAGS", "\\Seen")
            if status == "OK":
                return True
            log.warning("[IMAP-MARK-SEEN] attempt %d/%d: unexpected status %r for %s", attempt + 1, max_retries, status, mid.decode(errors="replace"))
        except (imaplib.IMAP4.abort, OSError, EOFError, BrokenPipeError) as e:
            log.warning("[IMAP-MARK-SEEN] attempt %d/%d socket error for %s: %s", attempt + 1, max_retries, mid.decode(errors="replace"), type(e).__name__)
            if attempt < max_retries - 1:
                time.sleep(0.5)
                try: imap.noop()
                except Exception: pass
        except Exception as e:
            log.warning("[IMAP-MARK-SEEN] attempt %d/%d unexpected error for %s: %s", attempt + 1, max_retries, mid.decode(errors="replace"), e)

    log.error("[IMAP-MARK-SEEN] Could not mark %s as read after %d attempts (subject: %r)", mid.decode(errors="replace"), max_retries, subject[:100])
    sflog.record_failure(source="mailback", stage="mark_seen", rta=rta, report=report, msg=f"Failed to mark email as read after {max_retries} attempts: {subject[:200]}", context={"msg_id": mid.decode(errors="replace")})
    return False


def _download_and_extract(url: str, rta: str, report_code: str, zip_password: str) -> list[str]:
    log.info("[%s-%s] Downloading %s", rta, report_code, url)
    try:
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
    except Exception as e:
        sflog.record_failure(source="mailback", stage="fetch", rta=rta, report=report_code, msg=f"Download failed: {e}", context={"url": url})
        raise

    out_dir = BASE_DIR / rta.lower() / report_code.lower()
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "done").mkdir(parents=True, exist_ok=True)

    saved_files = []
    try:
        with pyzipper.AESZipFile(io.BytesIO(resp.content)) as zf:
            zf.setpassword(zip_password.encode())
            names = zf.namelist()
            if not names:
                log.info("[%s-%s] ZIP is empty (0 entries) — nothing to import", rta, report_code)
                return []
            for name in names:
                data = zf.read(name)
                out_path = out_dir / Path(name).name
                out_path.write_bytes(data)
                saved_files.append(str(out_path))
                log.info("[%s-%s] Extracted %s (%d bytes)", rta, report_code, out_path.name, len(data))
    except RuntimeError as e:
        log.error("[%s-%s] Zip extraction failed (bad password?): %s", rta, report_code, e)
        sflog.record_failure(source="mailback", stage="extract", rta=rta, report=report_code, msg=f"Zip extraction failed (bad password?): {e}", context={"url": url})
        raise
    except Exception as e:
        log.error("[%s-%s] Zip extract error: %s", rta, report_code, e)
        sflog.record_failure(source="mailback", stage="extract", rta=rta, report=report_code, msg=f"Zip extract error: {e}", context={"url": url})
        raise

    return saved_files


def _parse_and_move(path_str: str, rta: str, report_code: str) -> dict:
    path = Path(path_str)
    rta_config = RTA_CONFIG[rta]
    parser = rta_config["parsers"].get(report_code)

    if parser is None:
        sflog.record_failure(source="mailback", stage="parse", rta=rta, report=report_code, file=path.name, msg=f"No parser mapped for {rta}-{report_code}")
        return {"path": path_str, "ok": False, "msg": f"No parser mapped for {rta}-{report_code}"}

    try:
        with open(path, "rb") as f:
            ok, msg, _preview = parser(f, replace=False)
    except Exception as e:
        log.exception("[%s-%s] Parse failed for %s", rta, report_code, path.name)
        sflog.record_failure(source="mailback", stage="parse", rta=rta, report=report_code, file=path.name, msg=f"Exception during parse: {e}")
        return {"path": path_str, "ok": False, "msg": f"Exception: {e}"}

    if not ok and "0 rows found" in str(msg):
        log.info("[%s-%s] %s contains 0 data rows — moving to done (no-op)", rta, report_code, path.name)
        done_dir = path.parent / "done"
        done_dir.mkdir(parents=True, exist_ok=True)
        dest = done_dir / path.name
        if dest.exists():
            dest = done_dir / f"{path.stem}_{int(time.time())}{path.suffix}"
        path.rename(dest)
        return {"path": str(dest), "ok": True, "msg": "empty (0 rows) — ignored"}

    if not ok:
        log.warning("[%s-%s] Parser returned error: %s", rta, report_code, msg)
        sflog.record_failure(source="mailback", stage="db_insert", rta=rta, report=report_code, file=path.name, msg=str(msg))
        return {"path": path_str, "ok": False, "msg": msg}

    done_dir = path.parent / "done"
    done_dir.mkdir(parents=True, exist_ok=True)
    dest = done_dir / path.name
    if dest.exists():
        dest = done_dir / f"{path.stem}_{int(time.time())}{path.suffix}"
    path.rename(dest)
    log.info("[%s-%s] Parsed and moved to %s", rta, report_code, dest)
    return {"path": str(dest), "ok": True, "msg": msg}


def parse_pending_files() -> dict:
    results = {"parsed": [], "failed": []}
    for rta in RTA_CONFIG.keys():
        rta_dir = BASE_DIR / rta.lower()
        if not rta_dir.exists():
            continue
        for report_dir in rta_dir.iterdir():
            if not report_dir.is_dir() or report_dir.name == "done":
                continue
            report_code = report_dir.name.upper()
            for f in sorted(report_dir.iterdir()):
                if not f.is_file():
                    continue
                res = _parse_and_move(str(f), rta, report_code)
                res["rta"] = rta
                res["report"] = report_code
                res["file"] = f.name
                (results["parsed"] if res["ok"] else results["failed"]).append(res)
    return results


def get_pending_counts() -> dict:
    counts = {}
    for rta in RTA_CONFIG.keys():
        rta_dir = BASE_DIR / rta.lower()
        if not rta_dir.exists():
            counts[rta] = 0
            continue
        count = 0
        for report_dir in rta_dir.iterdir():
            if report_dir.is_dir() and report_dir.name != "done":
                count += sum(1 for f in report_dir.iterdir() if f.is_file())
        counts[rta] = count
    return counts


def get_done_counts() -> dict:
    counts = {}
    for rta in RTA_CONFIG.keys():
        done_count = 0
        for report_code in RTA_CONFIG[rta]["reports"]:
            done_dir = BASE_DIR / rta.lower() / report_code.lower() / "done"
            done_count += len(list(done_dir.glob("*"))) if done_dir.exists() else 0
        counts[rta] = done_count
    return counts


def _extract_report_code(subject: str, body: str, rta: str) -> str | None:
    reports = RTA_CONFIG[rta]["reports"]
    for code in reports:
        if re.search(rf"\b{code}\b", subject, re.I):
            return code
    for code in reports:
        if re.search(rf"\b{code}\b", body, re.I):
            return code
    return None


def _resolve_report_code(rta: str, code: str) -> str:
    aliases = RTA_CONFIG[rta].get("aliases", {})
    return aliases.get(code, code)


def _extract_download_url(body: str) -> str | None:
    m = re.search(r'https?://[^\s"<>]+\.zip(?:\?[^\s"<>]*)?', body, re.I)
    if m: return m.group(0).rstrip('"\'<>);,')
    m = re.search(r'https?://scdelivery\.kfintech\.com/c/\?u=[^\s"<>]+', body, re.I)
    if m: return m.group(0).rstrip('"\'<>);,')
    m = re.search(r'https?://mfs\.kfintech\.com/[^\s"<>]+', body, re.I)
    if m: return m.group(0).rstrip('"\'<>);,')
    return None


import os as _os
import fcntl as _fcntl

_MAILBACK_LOCK_PATH = _os.environ.get("MAILBACK_LOCK", "/tmp/mailback_sync.lock")
_EMPTY_RESULT = {"checked": 0, "no_data": [], "errors": [], "fetch_skipped": [], "downloaded": [], "parsed": [], "parse_failed": []}


def sync_once() -> dict:
    try:
        lock_fp = open(_MAILBACK_LOCK_PATH, "a")
    except OSError as e:
        log.warning("[MAILBACK] Cannot open lock file %s: %s — proceeding unlocked", _MAILBACK_LOCK_PATH, e)
        return _sync_once_impl()

    try:
        _fcntl.flock(lock_fp, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
    except BlockingIOError:
        log.info("[MAILBACK] Another sync is already running — skipping this call")
        lock_fp.close()
        return dict(_EMPTY_RESULT)

    try:
        return _sync_once_impl()
    finally:
        try: _fcntl.flock(lock_fp, _fcntl.LOCK_UN)
        finally: lock_fp.close()


def _sync_once_impl() -> dict:
    creds = get_credentials()
    if not creds["imap_user"] or not creds["imap_app_password"]:
        raise RuntimeError("IMAP user / app password not set.")

    results = {"checked": 0, "no_data": [], "errors": [], "fetch_skipped": [], "downloaded": [], "parsed": [], "parse_failed": []}
    imap = None
    try:
        imap = _get_imap_connection()
        log.info("[IMAP] Connected")
        for rta, config in RTA_CONFIG.items():
            zip_password = creds.get(f"{rta.lower()}_zip_password")
            if not zip_password:
                log.info("[%s] Password not configured, skipping", rta)
                continue

            sender = config["sender"]
            try:
                ok, msg_ids = _search_safe(imap, f'(UNSEEN FROM "{sender}")')
            except (imaplib.IMAP4.abort, OSError, EOFError):
                log.warning("[%s] Search failed, reconnecting...", rta)
                try: imap.close()
                except: pass
                imap = _get_imap_connection()
                ok, msg_ids = _search_safe(imap, f'(UNSEEN FROM "{sender}")')

            if not ok:
                results["errors"].append(f"{rta}: IMAP search failed")
                continue

            log.info("[%s] Found %d unread mailback emails", rta, len(msg_ids))
            for mid in msg_ids:
                results["checked"] += 1
                ok, msg_data = _fetch_email_safe(imap, mid)
                if not ok:
                    log.warning("[%s] Fetch failed for %s, reconnecting...", rta, mid.decode())
                    try: imap.close()
                    except: pass
                    imap = _get_imap_connection()
                    results["errors"].append(f"{rta}-{mid.decode()}: fetch failed after retry")
                    continue

                msg = email.message_from_bytes(msg_data)
                subject = _decode_subject(msg.get("Subject"))
                body = _get_body_text(msg)
                log.info("[%s] Processing email: %s", rta, subject[:100])

                report_code = _extract_report_code(subject, body, rta)
                if not report_code:
                    log.info("[%s] Skipping non-report email (marking read): %s", rta, subject[:80])
                    _mark_seen_safe(imap, mid, rta=rta, subject=subject)
                    continue

                report_code = _resolve_report_code(rta, report_code)
                log.info("[%s-%s] Report code resolved", rta, report_code)

                if _is_no_data(body):
                    log.info("[%s-%s] No data — marking read, skipping", rta, report_code)
                    results["no_data"].append(f"{rta}: {subject}")
                    _mark_seen_safe(imap, mid, rta=rta, report=report_code, subject=subject)
                    continue

                url = _extract_download_url(body)
                if not url:
                    log.info("[%s-%s] No download URL — treating as empty report, marking read", rta, report_code)
                    results["no_data"].append(f"{rta}: {subject}")
                    _mark_seen_safe(imap, mid, rta=rta, report=report_code, subject=subject)
                    continue

                log.info("[%s-%s] Download URL found", rta, report_code)
                try:
                    saved = _download_and_extract(url, rta, report_code, zip_password)
                except Exception as e:
                    results["errors"].append(f"{rta}: {subject} — {e}")
                    log.exception("[%s-%s] Download/extract failed", rta, report_code)
                    continue

                _mark_seen_safe(imap, mid, rta=rta, report=report_code, subject=subject)
                if not saved:
                    results["no_data"].append(f"{rta}: {subject}")
                    continue

                results["downloaded"].append({"rta": rta, "report": report_code, "subject": subject, "files": saved})
                for path_str in saved:
                    res = _parse_and_move(path_str, rta, report_code)
                    res["rta"] = rta
                    res["report"] = report_code
                    res["file"] = Path(path_str).name
                    (results["parsed"] if res["ok"] else results["parse_failed"]).append(res)

        leftover = parse_pending_files()
        results["parsed"].extend(leftover["parsed"])
        results["parse_failed"].extend(leftover["failed"])
    except Exception as e:
        log.exception("[MAILBACK-SYNC] Fatal error")
        sflog.record_failure(source="mailback", stage="unknown", msg=f"Fatal error during sync: {e}")
        results["errors"].append(f"Fatal: {e}")
    finally:
        if imap:
            try: imap.close(); imap.logout()
            except: pass
    dm.set_credential("mailback_last_sync_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    return results


def get_client_email(client_code: str) -> str | None:
    from app.core.compat import get_conn
    with get_conn() as conn:
        row = conn.execute("SELECT email FROM bse_client_master WHERE client_code = ?", (client_code,)).fetchone()
    return row[0] if row and row[0] else None


def send_report_email(to_email: str, subject: str, html_body: str, pdf_bytes: bytes | None = None, pdf_filename: str = "report.pdf", cc_emails: list[str] | None = None) -> tuple[bool, str]:
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.mime.application import MIMEApplication

    creds = get_credentials()
    if not creds["imap_user"] or not creds["imap_app_password"]:
        return False, "Gmail credentials not configured."
    if not to_email or "@" not in to_email:
        return False, "Invalid recipient email address"

    try:
        msg = MIMEMultipart('alternative')
        msg['From'] = creds["imap_user"]
        msg['To'] = to_email
        msg['Subject'] = subject
        if cc_emails: msg['Cc'] = ", ".join(cc_emails)
        msg.attach(MIMEText("Please view in HTML mode.", 'plain', 'utf-8'))
        msg.attach(MIMEText(html_body, 'html', 'utf-8'))
        if pdf_bytes:
            part = MIMEApplication(pdf_bytes, Name=pdf_filename)
            part['Content-Disposition'] = f'attachment; filename="{pdf_filename}"'
            msg.attach(part)

        with smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=30) as server:
            server.login(creds["imap_user"], creds["imap_app_password"])
            server.send_message(msg, to_addrs=[to_email] + (cc_emails or []))
        log.info("[EMAIL] Sent '%s' to %s", subject, to_email)
        return True, f"✅ Email sent to {to_email}"
    except smtplib.SMTPAuthenticationError:
        return False, "❌ Authentication failed. Check your App Password in Admin settings."
    except Exception as e:
        log.exception("[EMAIL] Failed to send")
        return False, f"❌ Failed: {e}"


def fetch_from_gmail() -> dict:
    import fcntl
    import os
    if not credentials_configured():
        return {"ok": False, "msg": "Gmail credentials not configured."}
    lock_path = os.environ.get("WORKER_LOCK", "/tmp/background_worker.lock")
    try:
        fp = open(lock_path, "a")
    except OSError as e:
        return {"ok": False, "msg": f"Cannot open lock: {e}"}
    try:
        fcntl.flock(fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fp.close()
        return {"ok": False, "msg": "Worker running. Try again."}
    try:
        res = sync_once()
    except Exception as e:
        log.exception("[MAILBACK] Gmail fetch failed")
        return {"ok": False, "msg": str(e)}
    finally:
        try: fcntl.flock(fp, fcntl.LOCK_UN)
        finally: fp.close()
    from app.services.cache import bump as _bump
    _bump()
    msg = f"checked {res['checked']} · downloaded {len(res['downloaded'])} · imported {len(res['parsed'])} · failed {len(res['parse_failed'])} · no data {len(res['no_data'])} · errors {len(res['errors'])}"
    return {"ok": not res["errors"] and not res["parse_failed"], "msg": msg, "result": res}