#!/usr/bin/env python3
"""
CAMS Mailback Auto-Downloader + Auto-Importer (Enhanced with Background Threading)

Polls Gmail via IMAP for CAMS Mailback Server report emails (WBR2, WBR9, WBR49, WBR77, WBR4),
downloads + extracts the zip, auto-parses each file into the DB via data_manager's
parsers, then moves successfully-parsed files into <report>/done/.

KEY CHANGES FROM ORIGINAL:
- Background thread for sync (non-blocking, like BSE Scheme Master)
- Auto-parse IMMEDIATELY after download + extraction
- Move to done/ ONLY after successful DB import
- Status tracking in _sync_status (visible in UI)
- Auto-retry on parsing errors (leave file in folder)
- Once-per-day gate to avoid excessive polling

Credentials are stored in app_settings table (same DB as everything else).
"""

import imaplib
import email
import re
import io
import json
import time
import logging
import threading
from pathlib import Path
from email.header import decode_header
from datetime import datetime

import pyzipper
import requests

import data_manager as dm

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("cams_sync")

# ══════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════
IMAP_HOST = "imap.gmail.com"
CAMS_SENDER = "donotreply@camsonline.com"
BASE_DIR = Path("mailback_sync/cams_data")
POLL_INTERVAL_SECONDS = 300

REPORT_CODES = ["WBR2", "WBR9", "WBR49", "WBR77", "WBR4"]

REPORT_PARSER_MAP = {
    "WBR2": dm.parse_cams_wbr2_transaction,
    "WBR9": dm.parse_cams_wbr9_folio,
    "WBR49": dm.parse_cams_wbr49_sip,
    "WBR77": dm.parse_cams_wbr77_brokerage,
    "WBR4": dm.parse_cams_wbr4_aum,
}

SETTINGS_KEYS = {
    "imap_user": "cams_mailback_imap_user",
    "imap_app_password": "cams_mailback_imap_app_password",
    "zip_password": "cams_mailback_zip_password",
    "last_sync_at": "cams_mailback_last_sync_at",
}


# ══════════════════════════════════════════════════════════════
# BACKGROUND SYNC STATUS (mirrors BSE pattern)
# ══════════════════════════════════════════════════════════════
_sync_status = {
    "running": False,
    "done": False,
    "ok": False,
    "msg": "",
    "result": None,
    "started_at": None,
    "finished_at": None,
}


def get_sync_status() -> dict:
    """Return copy of current sync status — safe for UI display."""
    return _sync_status.copy()


def _reset_sync_status():
    _sync_status.update({
        "running": False,
        "done": False,
        "ok": False,
        "msg": "",
        "result": None,
        "started_at": None,
        "finished_at": None,
    })


def _set_sync_status(**kwargs):
    _sync_status.update(kwargs)


# ══════════════════════════════════════════════════════════════
# SETTINGS (stored in DB, same as everything else)
# ══════════════════════════════════════════════════════════════
def _ensure_settings_table():
    with dm.get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)


def get_setting(key: str, default: str = "") -> str:
    _ensure_settings_table()
    with dm.get_conn() as conn:
        row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row and row[0] is not None else default


def set_setting(key: str, value: str) -> None:
    _ensure_settings_table()
    with dm.get_conn() as conn:
        conn.execute("""
            INSERT INTO app_settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """, (key, value))


def get_credentials() -> dict:
    return {
        "imap_user": get_setting(SETTINGS_KEYS["imap_user"]),
        "imap_app_password": get_setting(SETTINGS_KEYS["imap_app_password"]),
        "zip_password": get_setting(SETTINGS_KEYS["zip_password"]),
    }


def save_credentials(imap_user: str, imap_app_password: str, zip_password: str) -> None:
    if imap_user:
        set_setting(SETTINGS_KEYS["imap_user"], imap_user.strip())
    if imap_app_password:
        set_setting(SETTINGS_KEYS["imap_app_password"], imap_app_password.strip())
    if zip_password:
        set_setting(SETTINGS_KEYS["zip_password"], zip_password.strip())


def credentials_configured() -> bool:
    c = get_credentials()
    return bool(c["imap_user"] and c["imap_app_password"] and c["zip_password"])


# ══════════════════════════════════════════════════════════════
# EMAIL PARSING
# ══════════════════════════════════════════════════════════════
def _decode_subject(raw_subject) -> str:
    parts = decode_header(raw_subject or "")
    out = ""
    for text, enc in parts:
        if isinstance(text, bytes):
            out += text.decode(enc or "utf-8", errors="replace")
        else:
            out += text
    return out


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
            charset = chosen.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace")
        return ""
    else:
        payload = msg.get_payload(decode=True)
        charset = msg.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace") if payload else ""


def _extract_report_code(subject: str) -> str | None:
    for code in REPORT_CODES:
        if re.search(rf"\b{code}\b", subject, re.I):
            return code
    return None


def _extract_download_url(body: str) -> str | None:
    m = re.search(r'https://mailback\d*\.camsonline\.com/mailback_result/\S+?\.zip', body)
    return m.group(0) if m else None


def _is_no_data(body: str) -> bool:
    lowered = body.lower()
    return bool(re.search(r'no\s*data|no\s*records?\s*found', lowered))


# ══════════════════════════════════════════════════════════════
# DOWNLOAD + EXTRACT (no parsing here — parse happens after)
# ══════════════════════════════════════════════════════════════
def _download_and_extract(url: str, report_code: str, zip_password: str) -> list[str]:
    """Download zip, extract, return list of file paths. Do NOT parse yet."""
    log.info("[%s] Downloading %s", report_code, url)
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()

    out_dir = BASE_DIR / report_code.lower()
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "done").mkdir(parents=True, exist_ok=True)

    saved_files = []
    try:
        with pyzipper.AESZipFile(io.BytesIO(resp.content)) as zf:
            zf.setpassword(zip_password.encode())
            for name in zf.namelist():
                data = zf.read(name)
                out_path = out_dir / Path(name).name
                out_path.write_bytes(data)
                saved_files.append(str(out_path))
                log.info("[%s] Extracted %s (%d bytes)", report_code, out_path.name, len(data))
    except RuntimeError as e:
        log.error("[%s] Zip extraction failed (bad password?): %s", report_code, e)
        raise
    except pyzipper.zipfile.BadZipFile as e:
        log.error("[%s] Not a valid zip file: %s", report_code, e)
        raise

    return saved_files


# ══════════════════════════════════════════════════════════════
# AUTO-PARSE + AUTO-MOVE (happens IMMEDIATELY after download)
# ══════════════════════════════════════════════════════════════
def _parse_and_move(path_str: str, report_code: str) -> dict:
    """
    Parse one extracted file into the DB via data_manager.
    On success: move to done/
    On failure: leave in folder for retry
    """
    path = Path(path_str)
    parser = REPORT_PARSER_MAP.get(report_code)
    if parser is None:
        return {"path": path_str, "ok": False, "msg": f"No parser mapped for {report_code}"}

    try:
        with open(path, "rb") as f:
            ok, msg, _preview = parser(f, replace=False)
    except Exception as e:
        log.exception("[%s] Parse failed for %s", report_code, path.name)
        return {"path": path_str, "ok": False, "msg": f"Exception: {e}"}

    if not ok:
        log.warning("[%s] Parser returned error: %s", report_code, msg)
        return {"path": path_str, "ok": False, "msg": msg}

    # ── SUCCESS: Move to done/ folder ──
    done_dir = path.parent / "done"
    done_dir.mkdir(parents=True, exist_ok=True)
    dest = done_dir / path.name
    if dest.exists():
        dest = done_dir / f"{path.stem}_{int(time.time())}{path.suffix}"
    path.rename(dest)
    log.info("[%s] Parsed and moved to %s", report_code, dest)
    return {"path": str(dest), "ok": True, "msg": msg}


def parse_pending_files() -> dict:
    """
    Scan every report's folder for files NOT yet in done/, parse + move each.
    Safe to call any time (manual retry, or auto-sweep).
    """
    results = {"parsed": [], "failed": []}
    for report_code in REPORT_CODES:
        out_dir = BASE_DIR / report_code.lower()
        if not out_dir.exists():
            continue
        for f in sorted(out_dir.iterdir()):
            if not f.is_file():
                continue
            res = _parse_and_move(str(f), report_code)
            res["report"] = report_code
            res["file"] = f.name
            (results["parsed"] if res["ok"] else results["failed"]).append(res)
    return results


def get_pending_counts() -> dict:
    """Files sitting in folders (not yet parsed)."""
    counts = {}
    for report_code in REPORT_CODES:
        out_dir = BASE_DIR / report_code.lower()
        if not out_dir.exists():
            counts[report_code] = 0
            continue
        counts[report_code] = sum(1 for f in out_dir.iterdir() if f.is_file())
    return counts


def get_done_counts() -> dict:
    """Files successfully imported into DB."""
    counts = {}
    for report_code in REPORT_CODES:
        done_dir = BASE_DIR / report_code.lower() / "done"
        counts[report_code] = len(list(done_dir.glob("*"))) if done_dir.exists() else 0
    return counts


# ══════════════════════════════════════════════════════════════
# MAIN SYNC (with auto-parse after each download)
# ══════════════════════════════════════════════════════════════
def sync_once() -> dict:
    """
    One sync run:
    1. Check Gmail for unread CAMS mailback emails
    2. Download + extract each zip
    3. Auto-parse each file into DB (happens here now)
    4. Move successful imports to done/
    5. Return summary

    Follows same flow as BSE auto-download.
    """
    creds = get_credentials()
    if not creds["imap_user"] or not creds["imap_app_password"] or not creds["zip_password"]:
        raise RuntimeError(
            "IMAP user / app password / zip password not set. "
            "Set them in Admin Panel → CAMS Mailback Auto-Sync first."
        )

    results = {
        "checked": 0,
        "no_data": [],
        "errors": [],
        "downloaded": [],   # [{report, subject, files: [...]}]
        "parsed": [],
        "parse_failed": [],
    }

    imap = imaplib.IMAP4_SSL(IMAP_HOST)
    imap.login(creds["imap_user"], creds["imap_app_password"])
    imap.select("INBOX")

    status, data = imap.search(None, f'(UNSEEN FROM "{CAMS_SENDER}")')
    if status != "OK":
        imap.logout()
        raise RuntimeError(f"IMAP search failed: {status}")

    msg_ids = data[0].split()
    log.info("Found %d unread CAMS mailback emails", len(msg_ids))

    for mid in msg_ids:
        results["checked"] += 1
        status, msg_data = imap.fetch(mid, "(BODY.PEEK[])")
        if status != "OK":
            results["errors"].append(f"{mid.decode()}: fetch failed")
            continue

        msg = email.message_from_bytes(msg_data[0][1])
        subject = _decode_subject(msg.get("Subject"))
        report_code = _extract_report_code(subject)
        if not report_code:
            continue

        body = _get_body_text(msg)

        if _is_no_data(body):
            log.info("[%s] No data — marking read, skipping", report_code)
            results["no_data"].append(subject)
            imap.store(mid, "+FLAGS", "\\Seen")
            continue

        url = _extract_download_url(body)
        if not url:
            log.warning("[%s] No DownloadURL found: %s", report_code, subject)
            results["errors"].append(f"{subject}: no URL found")
            continue

        try:
            saved = _download_and_extract(url, report_code, creds["zip_password"])
        except Exception as e:
            results["errors"].append(f"{subject}: {e}")
            log.exception("[%s] Download/extract failed: %s", report_code, e)
            continue

        results["downloaded"].append({"report": report_code, "subject": subject, "files": saved})
        imap.store(mid, "+FLAGS", "\\Seen")  # Mark read after successful download

        # ══════════════════════════════════════════════════════════════
        # KEY CHANGE: Auto-parse + auto-move IMMEDIATELY after download
        # ══════════════════════════════════════════════════════════════
        for path_str in saved:
            res = _parse_and_move(path_str, report_code)
            res["report"] = report_code
            res["file"] = Path(path_str).name
            (results["parsed"] if res["ok"] else results["parse_failed"]).append(res)

    imap.logout()

    # Also sweep any pending files from previous runs (retry)
    leftover = parse_pending_files()
    results["parsed"].extend(leftover["parsed"])
    results["parse_failed"].extend(leftover["failed"])

    set_setting(SETTINGS_KEYS["last_sync_at"], datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    return results


# ══════════════════════════════════════════════════════════════
# BACKGROUND THREAD WORKER (non-blocking)
# ══════════════════════════════════════════════════════════════
def _sync_worker():
    """Background thread worker — runs sync_once() and updates status."""
    _set_sync_status(
        running=True,
        done=False,
        started_at=datetime.now().isoformat()
    )
    try:
        result = sync_once()
        msg = (
            f"Downloaded {len(result['downloaded'])} | "
            f"Imported {len(result['parsed'])} | "
            f"Failed {len(result['parse_failed'])} | "
            f"No data {len(result['no_data'])}"
        )
        _set_sync_status(
            running=False,
            done=True,
            ok=True,
            result=result,
            msg=msg,
            finished_at=datetime.now().isoformat()
        )
        log.info("[CAMS-MAILBACK-SYNC] %s", msg)
    except Exception as e:
        log.exception("[CAMS-MAILBACK-SYNC] Sync failed")
        _set_sync_status(
            running=False,
            done=True,
            ok=False,
            msg=str(e),
            finished_at=datetime.now().isoformat()
        )


def start_background_sync() -> None:
    """Start sync in background thread (non-blocking)."""
    if _sync_status["running"]:
        log.info("[CAMS-MAILBACK-SYNC] Already running, skipping")
        return
    _reset_sync_status()
    t = threading.Thread(target=_sync_worker, daemon=True)
    t.start()
    log.info("[CAMS-MAILBACK-SYNC] Started background sync thread")


def should_auto_sync() -> bool:
    """Only if credentials are set and haven't synced today."""
    if not credentials_configured():
        return False
    last = get_setting(SETTINGS_KEYS["last_sync_at"])
    today = datetime.now().strftime("%Y-%m-%d")
    return not (last and last.startswith(today))


# ══════════════════════════════════════════════════════════════
# ENTRY POINT (for standalone script mode)
# ══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "loop":
        log.info("Running continuous sync loop (Ctrl+C to stop)")
        while True:
            try:
                result = sync_once()
                log.info(
                    "Sync complete: %s downloaded, %s parsed, %s parse-failed, %s no-data, %s errors",
                    len(result["downloaded"]), len(result["parsed"]), len(result["parse_failed"]),
                    len(result["no_data"]), len(result["errors"])
                )
            except Exception as e:
                log.exception("Sync run failed")
            time.sleep(POLL_INTERVAL_SECONDS)
    else:
        result = sync_once()
        print(json.dumps(result, indent=2, default=str))