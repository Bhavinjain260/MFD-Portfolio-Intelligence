#!/usr/bin/env python3
"""
Mailback Auto-Downloader + Auto-Importer (CAMS + KFinTech)

Polls Gmail via IMAP for both CAMS and KFinTech mailback reports,
downloads + extracts zips, auto-parses into DB, moves to done/.

Each RTA (CAMS, KFinTech) has separate:
- Email sender
- Report codes
- Zip password
- Folder structure
- Parsers

Settings stored in DB app_settings table.
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
log = logging.getLogger("mailback_sync")

# ══════════════════════════════════════════════════════════════
# CONFIG — CAMS & KFinTech RTAs
# ══════════════════════════════════════════════════════════════
IMAP_HOST = "imap.gmail.com"

# ── RTA Configs (sender, report codes, parsers) ──
RTA_CONFIG = {
    "CAMS": {
        "sender": "donotreply@camsonline.com",
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
        "reports": [
            "MFSD201", "MFSD211", "MFSD205", "MFSD243", "MFSD203",
            "MFSD307", "MFSD313", "MFSD311",   # ← Karvy aliases
        ],
        "parsers": {
            "MFSD201": dm.parse_kfin_mfsd201_transaction,
            "MFSD211": dm.parse_kfin_mfsd211_folio,
            "MFSD205": dm.parse_kfin_mfsd205_brokerage,
            "MFSD243": dm.parse_kfin_mfsd243_sip,
            "MFSD203": dm.parse_kfin_mfsd203_aum,
        },
        "aliases": {   # ← NEW: map aliases → canonical codes
            "MFSD307": "MFSD201",
            "MFSD313": "MFSD243",
            "MFSD311": "MFSD211",
        },
        "password_key": "kfintech_mailback_zip_password",
    },
}

# Extract all report codes from RTA_CONFIG
REPORT_CODES = []
for rta_config in RTA_CONFIG.values():
    REPORT_CODES.extend(rta_config["reports"])

BASE_DIR = Path("mailback_sync")
POLL_INTERVAL_SECONDS = 7200




def _poll_loop(interval_seconds: int = POLL_INTERVAL_SECONDS):
    while True:
        try:
            if is_polling_enabled() and credentials_configured() and not _sync_status["running"]:
                log.info("[MAILBACK-POLL] Checking for new mailback files...")
                result = sync_once()
                log.info(
                    "[MAILBACK-POLL] %s downloaded, %s parsed, %s failed",
                    len(result["downloaded"]), len(result["parsed"]), len(result["parse_failed"])
                )
                dm.set_credential("mailback_last_sync_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        except Exception:
            log.exception("[MAILBACK-POLL] Poll cycle failed")
        time.sleep(interval_seconds)


def ensure_poller_started(interval_seconds: int = POLL_INTERVAL_SECONDS) -> None:
    """Idempotent — safe to call on every Streamlit rerun."""
    global _poller_started
    with _poller_lock:
        if _poller_started:
            return
        t = threading.Thread(target=_poll_loop, args=(interval_seconds,), daemon=True)
        t.start()
        _poller_started = True
        log.info("[MAILBACK-POLL] Background poller started (every %ss)", interval_seconds)


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
# BACKGROUND SYNC STATUS
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

# ══════════════════════════════════════════════════════════════
# POLLING LOOP
# ══════════════════════════════════════════════════════════════
POLL_ENABLED_KEY = "mailback_poll_enabled"

_poller_started = False
_poller_lock = threading.Lock()


def is_polling_enabled() -> bool:
    return dm.get_credential(POLL_ENABLED_KEY, "1") == "1"


def set_polling_enabled(enabled: bool) -> None:
    dm.set_credential(POLL_ENABLED_KEY, "1" if enabled else "0")


# ══════════════════════════════════════════════════════════════
# SETTINGS (Uses unified sync_credentials table from data_manager)
# ══════════════════════════════════════════════════════════════
def get_credentials() -> dict:
    return {
        "imap_user": dm.get_credential("mailback_imap_user"),
        "imap_app_password": dm.get_credential("mailback_imap_app_password"),
        "cams_zip_password": dm.get_credential("cams_mailback_zip_password"),
        "kfintech_zip_password": dm.get_credential("kfintech_mailback_zip_password"),
    }


def save_credentials(imap_user: str, imap_app_password: str, cams_zip_password: str, kfintech_zip_password: str) -> None:
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
        (c["cams_zip_password"] or c["kfintech_zip_password"])  # ← at least one RTA
    )

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


def _detect_rta(sender: str) -> str | None:
    """Detect which RTA based on sender email."""
    sender_lower = sender.lower()
    for rta, config in RTA_CONFIG.items():
        if config["sender"].lower() in sender_lower:
            return rta
    return None


# def _extract_report_code(subject: str, rta: str) -> str | None:
#     """Extract report code for the given RTA."""
#     reports = RTA_CONFIG[rta]["reports"]
#     for code in reports:
#         if re.search(rf"\b{code}\b", subject, re.I):
#             return code
#     return None

# def _resolve_report_code(rta: str, code: str) -> str:
#     """Resolve alias report codes (e.g. MFSD307) to canonical codes (e.g. MFSD201)."""
#     aliases = RTA_CONFIG[rta].get("aliases", {})
#     return aliases.get(code, code)


# def _extract_download_url(body: str) -> str | None:
#     """
#     Extract download URL from email body.
#     Handles HTML hrefs, quotes, and query parameters.
#     """
#     # Pattern 1: Direct URL ending in .zip with optional query params
#     # Allow .zip followed by ?, &, =, /, or typical URL chars, then a boundary
#     m = re.search(r'https?://[^\s"<>]+\.zip(?:\?[^\s"<>]*)?', body, re.I)
#     if m:
#         url = m.group(0)
#         # Clean trailing punctuation that might be captured
#         url = url.rstrip('"\'<>);,')
#         return url

#     # Pattern 2: Generic .zip link (fallback)
#     m = re.search(r'https?://\S+\.zip', body, re.I)
#     if m:
#         url = m.group(0).rstrip('"\'<>);,')
#         return url

#     return None


def _is_no_data(body: str) -> bool:
    lowered = body.lower()
    return bool(re.search(r'no\s*data|no\s*records?\s*found', lowered))


# ══════════════════════════════════════════════════════════════
# DOWNLOAD + EXTRACT
# ══════════════════════════════════════════════════════════════
def _download_and_extract(url: str, rta: str, report_code: str, zip_password: str) -> list[str]:
    """Download zip, extract, return list of file paths."""
    log.info("[%s-%s] Downloading %s", rta, report_code, url)
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()

    out_dir = BASE_DIR / rta.lower() / report_code.lower()
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
                log.info("[%s-%s] Extracted %s (%d bytes)", rta, report_code, out_path.name, len(data))
    except RuntimeError as e:
        log.error("[%s-%s] Zip extraction failed (bad password?): %s", rta, report_code, e)
        raise
    except pyzipper.zipfile.BadZipFile as e:
        log.error("[%s-%s] Not a valid zip file: %s", rta, report_code, e)
        raise

    return saved_files


# ══════════════════════════════════════════════════════════════
# AUTO-PARSE + AUTO-MOVE
# ══════════════════════════════════════════════════════════════
def _parse_and_move(path_str: str, rta: str, report_code: str) -> dict:
    """Parse one extracted file into the DB. On success: move to done/"""
    path = Path(path_str)
    rta_config = RTA_CONFIG[rta]
    parser = rta_config["parsers"].get(report_code)
    
    if parser is None:
        return {"path": path_str, "ok": False, "msg": f"No parser mapped for {rta}-{report_code}"}

    try:
        with open(path, "rb") as f:
            ok, msg, _preview = parser(f, replace=False)
    except Exception as e:
        log.exception("[%s-%s] Parse failed for %s", rta, report_code, path.name)
        return {"path": path_str, "ok": False, "msg": f"Exception: {e}"}

    if not ok:
        log.warning("[%s-%s] Parser returned error: %s", rta, report_code, msg)
        return {"path": path_str, "ok": False, "msg": msg}

    # ── SUCCESS: Move to done/ ──
    done_dir = path.parent / "done"
    done_dir.mkdir(parents=True, exist_ok=True)
    dest = done_dir / path.name
    if dest.exists():
        dest = done_dir / f"{path.stem}_{int(time.time())}{path.suffix}"
    path.rename(dest)
    log.info("[%s-%s] Parsed and moved to %s", rta, report_code, dest)
    return {"path": str(dest), "ok": True, "msg": msg}


def parse_pending_files() -> dict:
    """Scan all RTA folders for unparsed files, parse + move each."""
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
    """Files sitting in folders (not yet parsed)."""
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
    """Files successfully imported into DB."""
    counts = {}
    for rta in RTA_CONFIG.keys():
        done_count = 0
        for report_code in RTA_CONFIG[rta]["reports"]:
            done_dir = BASE_DIR / rta.lower() / report_code.lower() / "done"
            done_count += len(list(done_dir.glob("*"))) if done_dir.exists() else 0
        counts[rta] = done_count
    return counts


# ══════════════════════════════════════════════════════════════
# MAIN SYNC (supports both CAMS and KFinTech)
# ══════════════════════════════════════════════════════════════
def _extract_report_code(subject: str, body: str, rta: str) -> str | None:
    """Extract report code from subject first, then fall back to body."""
    reports = RTA_CONFIG[rta]["reports"]
    for code in reports:
        if re.search(rf"\b{code}\b", subject, re.I):
            return code
    # Fallback: search body if not found in subject
    for code in reports:
        if re.search(rf"\b{code}\b", body, re.I):
            return code
    return None


def _resolve_report_code(rta: str, code: str) -> str:
    """Resolve alias report codes (e.g. MFSD307) to canonical codes (e.g. MFSD201)."""
    aliases = RTA_CONFIG[rta].get("aliases", {})
    return aliases.get(code, code)


def _extract_download_url(body: str) -> str | None:
    """Extract download URL from email body.
    
    Handles:
    - CAMS direct .zip links
    - KFinTech scdelivery redirect links (no .zip in URL)
    - KFinTech mfs portal links
    """
    # Pattern 1: Direct .zip URL (CAMS and some KFinTech)
    m = re.search(r'https?://[^\s"<>]+\.zip(?:\?[^\s"<>]*)?', body, re.I)
    if m:
        url = m.group(0).rstrip('"\'<>);,')
        return url

    # Pattern 2: KFinTech scdelivery redirect URLs 
    # Example: https://scdelivery.kfintech.com/c/?u=...&p=...&e=...
    m = re.search(r'https?://scdelivery\.kfintech\.com/c/\?u=[^\s"<>]+', body, re.I)
    if m:
        url = m.group(0).rstrip('"\'<>);,')
        return url

    # Pattern 3: Generic KFinTech mfs links (fallback)
    m = re.search(r'https?://mfs\.kfintech\.com/[^\s"<>]+', body, re.I)
    if m:
        url = m.group(0).rstrip('"\'<>);,')
        return url

    return None


def sync_once() -> dict:
    """
    One sync run:
    1. Check Gmail for unread mailback emails from BOTH RTAs
    2. Download + extract each zip
    3. Auto-parse each file into DB
    4. Move successful imports to done/
    """
    creds = get_credentials()
    if not creds["imap_user"] or not creds["imap_app_password"]:
        raise RuntimeError("IMAP user / app password not set.")

    results = {
        "checked": 0,
        "no_data": [],
        "errors": [],
        "fetch_skipped": [],
        "downloaded": [],  # [{rta, report, subject, files: [...]}]
        "parsed": [],
        "parse_failed": [],
    }

    with imaplib.IMAP4_SSL(IMAP_HOST) as imap:
        imap.login(creds["imap_user"], creds["imap_app_password"])
        imap.select("INBOX")

        # ── Loop through each RTA ──
        for rta, config in RTA_CONFIG.items():
            zip_password = creds.get(f"{rta.lower()}_zip_password")
            if not zip_password:
                log.info("[%s] Password not configured, skipping", rta)
                continue

            sender = config["sender"]
            status, data = imap.search(None, f'(UNSEEN FROM "{sender}")')
            if status != "OK":
                results["errors"].append(f"{rta}: IMAP search failed")
                continue

            msg_ids = data[0].split()
            log.info("[%s] Found %d unread mailback emails", rta, len(msg_ids))

            for mid in msg_ids:
                results["checked"] += 1
                status, msg_data = imap.fetch(mid, "(BODY.PEEK[])")
                if status != "OK":
                    results["errors"].append(f"{rta}-{mid.decode()}: fetch failed")
                    continue

                msg = email.message_from_bytes(msg_data[0][1])
                subject = _decode_subject(msg.get("Subject"))
                body = _get_body_text(msg)

                # ── TRACE: log what we see ──
                log.info("[%s] Processing email: %s", rta, subject[:100])

                report_code = _extract_report_code(subject, body, rta)
                if not report_code:
                    log.warning("[%s] No report code found in subject/body for: %s", rta, subject[:80])
                    continue

                report_code = _resolve_report_code(rta, report_code)
                log.info("[%s-%s] Report code resolved", rta, report_code)

                if _is_no_data(body):
                    log.info("[%s-%s] No data — marking read, skipping", rta, report_code)
                    results["no_data"].append(f"{rta}: {subject}")
                    imap.store(mid, "+FLAGS", "\\Seen")
                    continue

                url = _extract_download_url(body)
                if not url:
                    log.error("[%s-%s] No download URL found in body", rta, report_code)
                    log.debug("[%s-%s] Body snippet: %s", rta, report_code, body[:500].replace('\n', ' '))
                    results["errors"].append(f"{rta}: {subject} — no URL found")
                    # Mark read so broken emails don't poll forever
                    imap.store(mid, "+FLAGS", "\\Seen")
                    continue

                log.info("[%s-%s] Download URL found: %s", rta, report_code, url[:120])

                try:
                    saved = _download_and_extract(url, rta, report_code, zip_password)
                except Exception as e:
                    results["errors"].append(f"{rta}: {subject} — {e}")
                    log.exception("[%s-%s] Download/extract failed", rta, report_code)
                    continue

                results["downloaded"].append({
                    "rta": rta,
                    "report": report_code,
                    "subject": subject,
                    "files": saved
                })
                imap.store(mid, "+FLAGS", "\\Seen")

                # ── Auto-parse + auto-move immediately after download ──
                for path_str in saved:
                    res = _parse_and_move(path_str, rta, report_code)
                    res["rta"] = rta
                    res["report"] = report_code
                    res["file"] = Path(path_str).name
                    (results["parsed"] if res["ok"] else results["parse_failed"]).append(res)

        # ── Sweep any pending files from previous runs (retry) ──
        leftover = parse_pending_files()
        results["parsed"].extend(leftover["parsed"])
        results["parse_failed"].extend(leftover["failed"])

    dm.set_credential("mailback_last_sync_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    return results
    
# ══════════════════════════════════════════════════════════════
# BACKGROUND WORKER
# ══════════════════════════════════════════════════════════════
def _sync_worker():
    """Background thread worker."""
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
        log.info("[MAILBACK-SYNC] %s", msg)
    except Exception as e:
        log.exception("[MAILBACK-SYNC] Sync failed")
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
        log.info("[MAILBACK-SYNC] Already running, skipping")
        return
    _reset_sync_status()
    t = threading.Thread(target=_sync_worker, daemon=True)
    t.start()
    log.info("[MAILBACK-SYNC] Started background sync thread")


def should_auto_sync() -> bool:
    """Only if credentials are set and haven't synced today."""
    if not credentials_configured():
        return False
    last = dm.get_credential("mailback_last_sync_at")
    today = datetime.now().strftime("%Y-%m-%d")
    return not (last and last.startswith(today))


# ══════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════
# ══════════════════════════════════════════════════════════════
# STREAMLIT UI
# ══════════════════════════════════════════════════════════════
def render_settings_ui():
    """Render mailback sync settings in Streamlit (call from Admin Panel)."""
    try:
        import streamlit as st
    except ImportError:
        log.warning("Streamlit not available for UI rendering")
        return

    creds = get_credentials()
    is_configured = credentials_configured()

    # ── Header & Status Bar ──
    col_title, col_status_badge = st.columns([3, 1])
    with col_title:
        st.subheader("📬 Mailback Auto-Sync")
    with col_status_badge:
        if is_configured:
            st.success("✅ Ready", icon="✅")
        else:
            st.warning("⚠️ Setup Needed", icon="⚠️")

    # ── Auto-Sync Toggle ──
    auto_enabled = is_polling_enabled()
    
    toggle_col1, toggle_col2 = st.columns([4, 1])
    with toggle_col1:
        st.caption("Background polling checks Gmail every 2 hours for new reports.")
    with toggle_col2:
        new_state = st.toggle(
            "Auto-sync",
            value=auto_enabled,
            key="mailback_auto_toggle"
        )
    
    if new_state != auto_enabled:
        set_polling_enabled(new_state)
        st.rerun()

    st.divider()

    # ── Credentials Configuration ──
    with st.container(border=True):
        st.markdown("#### 🔐 Gmail IMAP Credentials")
        st.caption("Used to connect to Gmail and fetch CAMS/KFinTech mailback ZIPs. [Create an App Password here](https://myaccount.google.com/apppasswords)")
        
        g_col1, g_col2 = st.columns(2)
        with g_col1:
            imap_user = st.text_input(
                "Gmail Address",
                value=creds["imap_user"],
                placeholder="you@example.com",
                key="imap_user_input"
            )
        with g_col2:
            imap_app_password = st.text_input(
                "App Password",
                value=creds["imap_app_password"],
                type="password",
                placeholder="xxxx xxxx xxxx xxxx",
                key="imap_app_password_input"
            )

    st.markdown("<div style='height: 5px'></div>", unsafe_allow_html=True)

    # ── RTA Passwords ──
    rta_col1, rta_col2 = st.columns(2)
    
    with rta_col1:
        with st.container(border=True):
            st.markdown("#### 🟢 CAMS Mailback")
            st.caption("Protects WBR2, WBR9, WBR49, WBR77, WBR4 ZIPs")
            
            cams_pw_status = "✅ Set" if creds["cams_zip_password"] else "❌ Missing"
            st.markdown(f"**Status:** {cams_pw_status}")
            
            cams_zip_password = st.text_input(
                "ZIP Password",
                value=creds["cams_zip_password"],
                type="password",
                placeholder="Enter CAMS password",
                key="cams_zip_password_input",
                label_visibility="collapsed"
            )
            cams_enabled = st.checkbox("Enable CAMS auto-download", value=bool(creds["cams_zip_password"]), key="cams_enabled_check")

    with rta_col2:
        with st.container(border=True):
            st.markdown("#### 🔵 KFinTech Mailback")
            st.caption("Protects MFSD201, MFSD211, MFSD205, MFSD243, MFSD203 ZIPs")
            
            kfin_pw_status = "✅ Set" if creds["kfintech_zip_password"] else "❌ Missing"
            st.markdown(f"**Status:** {kfin_pw_status}")
            
            kfintech_zip_password = st.text_input(
                "ZIP Password",
                value=creds["kfintech_zip_password"],
                type="password",
                placeholder="Enter KFinTech password",
                key="kfintech_zip_password_input",
                label_visibility="collapsed"
            )
            kfintech_enabled = st.checkbox("Enable KFinTech auto-download", value=bool(creds["kfintech_zip_password"]), key="kfintech_enabled_check")

    st.markdown("<div style='height: 10px'></div>", unsafe_allow_html=True)

    # ── Save Button ──
    save_col1, save_col2, save_col3 = st.columns([1, 1, 1])
    with save_col2:
        if st.button("💾 Save All Credentials", type="primary", use_container_width=True, key="save_mailback_creds"):
            save_credentials(
                imap_user=imap_user if imap_user else None,
                imap_app_password=imap_app_password if imap_app_password else None,
                cams_zip_password=cams_zip_password if cams_enabled and cams_zip_password else None,
                kfintech_zip_password=kfintech_zip_password if kfintech_enabled and kfintech_zip_password else None
            )
            st.cache_data.clear()
            st.success("✅ Credentials saved successfully!")
            st.rerun()

    st.divider()

    # ── Manual Sync & Status ──
    sync_col1, sync_col2 = st.columns([1, 2])
    
    with sync_col1:
        st.markdown("#### 🔄 Manual Sync")
        if st.button("▶️ Sync Now", type="primary", use_container_width=True, key="manual_mailback_sync"):
            if not is_configured:
                st.error("❌ Configure credentials first!")
            else:
                start_background_sync()
                st.info("⏳ Sync started in background...")

    with sync_col2:
        st.markdown("#### 📊 Last Sync Status")
        status = get_sync_status()

        if status["running"]:
            st.warning(f"⏳ **Running** since {status['started_at']}")
        elif status["done"]:
            if status["ok"]:
                st.success(status["msg"])
                if status["result"]:
                    with st.expander("View Details"):
                        res = status["result"]
                        c1, c2, c3, c4 = st.columns(4)
                        c1.metric("Checked", res['checked'])
                        c2.metric("Downloaded", len(res['downloaded']))
                        c3.metric("Imported", len(res['parsed']))
                        c4.metric("Failed", len(res['parse_failed']), delta_color="inverse")
                        
                        if res["parse_failed"]:
                            st.markdown("**❌ Failed Files:**")
                            for item in res["parse_failed"]:
                                st.caption(f"  • `{item.get('file')}` — {item.get('msg')}")
                        if res["errors"]:
                            st.markdown("**⚠️ Errors:**")
                            for err in res["errors"]:
                                st.caption(f"  • {err}")
            else:
                st.error(f"❌ {status['msg']}")
        else:
            last_sync = dm.get_credential("mailback_last_sync_at")
            if last_sync:
                st.info(f"🕐 **Last sync:** {last_sync}")
            else:
                st.info("💤 No sync yet — configure credentials and click Sync Now")

    st.divider()

    # ── File Pipeline Status ──
    with st.expander("📁 File Pipeline Status", expanded=False):
        pending = get_pending_counts()
        done = get_done_counts()
        
        pipe_col1, pipe_col2 = st.columns(2)
        
        with pipe_col1:
            st.markdown("**⏳ Pending (Waiting to Parse)**")
            if any(pending.values()):
                for rta, count in pending.items():
                    if count > 0:
                        st.warning(f"{rta}: {count} files")
                    else:
                        st.caption(f"{rta}: 0")
            else:
                st.success("No pending files")
        
        with pipe_col2:
            st.markdown("**✅ Imported (Done)**")
            for rta, count in done.items():
                st.caption(f"{rta}: {count} files")


# ══════════════════════════════════════════════════════════════
# EMAIL SENDING (Reuses Gmail IMAP credentials)
# ══════════════════════════════════════════════════════════════

def get_client_email(client_code: str) -> str | None:
    """Get email address for a client from bse_client_master."""
    from init_db import get_conn
    with get_conn() as conn:
        row = conn.execute(
            "SELECT email FROM bse_client_master WHERE client_code = ?",
            (client_code,)
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
    """
    Send an email with optional PDF attachment using configured Gmail credentials.
    
    Uses SMTP (not IMAP) for sending - same credentials work for both.
    
    Args:
        to_email: Recipient email address
        subject: Email subject line
        html_body: HTML content for the email body
        pdf_bytes: Optional PDF bytes to attach
        pdf_filename: Name for the PDF attachment
        cc_emails: Optional list of CC recipients
    
    Returns:
        (success, message) tuple
    """
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.mime.application import MIMEApplication
    
    creds = get_credentials()
    if not creds["imap_user"] or not creds["imap_app_password"]:
        return False, "Gmail credentials not configured. Please set up in Admin > Mailback Sync."
    
    gmail_user = creds["imap_user"]
    gmail_app_password = creds["imap_app_password"]
    
    # Validate recipient
    if not to_email or "@" not in to_email:
        return False, "Invalid recipient email address"
    
    try:
        # Create message
        msg = MIMEMultipart('alternative')
        msg['From'] = gmail_user
        msg['To'] = to_email
        msg['Subject'] = subject
        
        if cc_emails:
            msg['Cc'] = ", ".join(cc_emails)
        
        # Plain text fallback (minimal)
        plain_text = "Please view this email in HTML mode to see the report properly."
        plain_part = MIMEText(plain_text, 'plain', 'utf-8')
        msg.attach(plain_part)
        
        # Attach HTML body
        html_part = MIMEText(html_body, 'html', 'utf-8')
        msg.attach(html_part)
        
        # Attach PDF if provided
        if pdf_bytes:
            pdf_part = MIMEApplication(pdf_bytes, Name=pdf_filename)
            pdf_part['Content-Disposition'] = f'attachment; filename="{pdf_filename}"'
            msg.attach(pdf_part)
        
        # Build recipient list (to + cc)
        all_recipients = [to_email]
        if cc_emails:
            all_recipients.extend(cc_emails)
        
        # Send via Gmail SMTP
        with smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=30) as server:
            server.login(gmail_user, gmail_app_password)
            server.send_message(msg, to_addrs=all_recipients)
        
        log.info("[EMAIL] Sent '%s' to %s", subject, to_email)
        return True, f"✅ Email sent to {to_email}"
    
    except smtplib.SMTPAuthenticationError:
        return False, "❌ Authentication failed. Check your App Password in Admin settings."
    except smtplib.SMTPException as e:
        return False, f"❌ SMTP error: {e}"
    except Exception as e:
        log.exception("[EMAIL] Failed to send report")
        return False, f"❌ Failed to send: {e}"


# ══════════════════════════════════════════════════════════════
# EMAIL STATUS TRACKING
# ══════════════════════════════════════════════════════════════
_email_status = {
    "sending": False,
    "done": False,
    "ok": False,
    "msg": "",
    "sent_at": None,
}

def get_email_status() -> dict:
    return _email_status.copy()

def _reset_email_status():
    _email_status.update({
        "sending": False,
        "done": False,
        "ok": False,
        "msg": "",
        "sent_at": None,
    })

def _email_worker(to_email: str, subject: str, html_body: str, 
                  pdf_bytes: bytes | None, pdf_filename: str,
                  cc_emails: list[str] | None = None):
    """Background thread worker for sending emails."""
    _email_status.update({
        "sending": True,
        "done": False,
    })
    try:
        success, msg = send_report_email(
            to_email=to_email,
            subject=subject,
            html_body=html_body,
            pdf_bytes=pdf_bytes,
            pdf_filename=pdf_filename,
            cc_emails=cc_emails,
        )
        _email_status.update({
            "sending": False,
            "done": True,
            "ok": success,
            "msg": msg,
            "sent_at": datetime.now().isoformat() if success else None,
        })
    except Exception as e:
        _email_status.update({
            "sending": False,
            "done": True,
            "ok": False,
            "msg": f"Error: {e}",
        })

def send_report_email_background(
    to_email: str,
    subject: str,
    html_body: str,
    pdf_bytes: bytes | None = None,
    pdf_filename: str = "report.pdf",
    cc_emails: list[str] | None = None,
) -> None:
    """Start email send in background thread (non-blocking)."""
    if _email_status["sending"]:
        log.info("[EMAIL] Already sending, skipping")
        return
    _reset_email_status()
    t = threading.Thread(
        target=_email_worker, 
        args=(to_email, subject, html_body, pdf_bytes, pdf_filename, cc_emails),
        daemon=True
    )
    t.start()
    log.info("[EMAIL] Started background email send")