import logging
from app.core.compat import get_conn

log = logging.getLogger(__name__)

def resolve_client_folios(pan: str, name: str, is_minor: bool) -> tuple[list[str], list[str]]:
    """
    Securely resolve folios for a client.
    Returns (cams_folios, kfin_folios).
    """
    pan = (pan or "").strip().upper()
    name = (name or "").strip().upper()

    if not pan and not name:
        return [], []

    cams = set()
    kfin = set()

    with get_conn() as conn:
        if pan:
            # Exact PAN match is safe and fast
            for r in conn.execute("SELECT foliochk FROM cams_wbr9_folio WHERE TRIM(UPPER(pan_no)) = ?", (pan,)):
                cams.add(r[0])
            for r in conn.execute("SELECT folio FROM kfin_mfsd211_folio WHERE TRIM(UPPER(pan_number)) = ?", (pan,)):
                kfin.add(r[0])

        if name and not is_minor:
            # Exact name match as a fallback (prevents 'RAJ' matching 'RAJESH')
            for r in conn.execute("SELECT foliochk FROM cams_wbr9_folio WHERE TRIM(UPPER(inv_name)) = ?", (name,)):
                cams.add(r[0])
            for r in conn.execute("SELECT folio FROM kfin_mfsd211_folio WHERE TRIM(UPPER(investor_name)) = ?", (name,)):
                kfin.add(r[0])

        # Minors usually have blank PANs, match strictly by exact name
        if name and is_minor:
             for r in conn.execute("SELECT foliochk FROM cams_wbr9_folio WHERE TRIM(UPPER(inv_name)) = ?", (name,)):
                cams.add(r[0])
             for r in conn.execute("SELECT folio FROM kfin_mfsd211_folio WHERE TRIM(UPPER(investor_name)) = ?", (name,)):
                kfin.add(r[0])

    return sorted(cams), sorted(kfin)