
from datetime import date as _date

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import current_user
from app.core.compat import get_conn
from app.schemas.clients import ClientRow, PortfolioRow, PortfolioSummary
from app.services import nav_service as ns
from app.services import report_generator as rg

router = APIRouter(prefix="/clients", tags=["clients"])


def _client_row(cc: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("""
            SELECT client_code,
                   COALESCE(NULLIF(TRIM(primary_holder_first_name || ' ' ||
                                       COALESCE(primary_holder_last_name, '')), ''),
                            client_code) AS name,
                   primary_holder_pan, guardian_pan,
                   indian_mobile_no, email, city
            FROM bse_client_master WHERE client_code = ?
        """, (cc,)).fetchone()
    if not row:
        return None
    return dict(row)


@router.get("", response_model=list[ClientRow])
def list_clients(q: str = Query("", description="substring search"),
                 limit: int = Query(50, ge=1, le=500),
                 _=Depends(current_user)):
    if not q.strip():
        with get_conn() as conn:
            rows = conn.execute("""
                SELECT client_code,
                       COALESCE(NULLIF(TRIM(primary_holder_first_name || ' ' ||
                                           COALESCE(primary_holder_last_name, '')), ''),
                                client_code) AS name,
                       primary_holder_pan AS pan,
                       guardian_pan,
                       indian_mobile_no AS mobile,
                       email, city
                FROM bse_client_master
                ORDER BY name LIMIT ?
            """, (limit,)).fetchall()
        return [ClientRow(**dict(r)) for r in rows]

    like = f"%{q.strip().upper()}%"
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT client_code,
                   COALESCE(NULLIF(TRIM(primary_holder_first_name || ' ' ||
                                       COALESCE(primary_holder_last_name, '')), ''),
                            client_code) AS name,
                   primary_holder_pan AS pan,
                   guardian_pan,
                   indian_mobile_no AS mobile,
                   email, city
            FROM bse_client_master
            WHERE UPPER(client_code) LIKE ? OR UPPER(primary_holder_first_name) LIKE ?
               OR UPPER(primary_holder_last_name) LIKE ?
               OR UPPER(primary_holder_pan) LIKE ? OR UPPER(guardian_pan) LIKE ?
               OR UPPER(email) LIKE ? OR UPPER(indian_mobile_no) LIKE ?
            ORDER BY name LIMIT ?
        """, (like, like, like, like, like, like, like, limit)).fetchall()
    return [ClientRow(**dict(r)) for r in rows]


@router.get("/{client_code}", response_model=ClientRow)
def get_client(client_code: str, _=Depends(current_user)):
    r = _client_row(client_code)
    if not r:
        raise HTTPException(404, f"Client {client_code} not found")
    return ClientRow(**r)


def _folio_to_isin_map(folios: list[str]) -> dict[str, str]:
    """Batch-resolve folio_id -> ISIN via BSE scheme master. Two queries."""
    if not folios:
        return {}
    out: dict[str, str] = {}
    ph = ",".join(["?"] * len(folios))
    with get_conn() as conn:
        rows = conn.execute(f"""
            SELECT f.foliochk AS folio,
                   UPPER(TRIM(bsm.ISIN)) AS isin
            FROM cams_wbr9_folio f
            JOIN bse_scheme_master bsm
              ON UPPER(TRIM(bsm.Channel_Partner_Code)) = UPPER(TRIM(f.product))
            WHERE f.foliochk IN ({ph})
              AND bsm.ISIN IS NOT NULL AND TRIM(bsm.ISIN) != ''
        """, folios).fetchall()
        for fid, isin in rows:
            out.setdefault(fid, isin)

        rows = conn.execute(f"""
            SELECT f.folio AS folio,
                   UPPER(TRIM(bsm.ISIN)) AS isin
            FROM kfin_mfsd211_folio f
            JOIN bse_scheme_master bsm
              ON UPPER(TRIM(bsm.Channel_Partner_Code)) = UPPER(TRIM(f.product_code))
            WHERE f.folio IN ({ph})
              AND bsm.ISIN IS NOT NULL AND TRIM(bsm.ISIN) != ''
        """, folios).fetchall()
        for fid, isin in rows:
            out.setdefault(fid, isin)
    return out


@router.get("/{client_code}/portfolio", response_model=PortfolioSummary)
def portfolio(client_code: str, _=Depends(current_user)):
    """
    Portfolio for a client.

    Invested amount and current value come from the same FIFO / txn-derived
    pipeline the Valuation Report uses (report_generator._build_valuation_data),
    so the numbers match across the UI and the PDF.
    """
    row = _client_row(client_code)
    if not row:
        raise HTTPException(404, f"Client {client_code} not found")

    # Current snapshot date
    snap = ns.get_snapshot_status()
    current_date_iso = snap.get("latest_date") or "1970-01-01"
    try:
        from datetime import datetime as _dt
        current_date = _dt.strptime(current_date_iso, "%Y-%m-%d").date()
    except Exception:
        current_date = _date.today()

    # Period start: wide-open (client inception) so cost basis covers all
    # units held today.
    period_from = _date(2000, 1, 1)

    try:
        _, summary_rows, _, total_inv, total_val, _ = rg._build_valuation_data(
            client_code, period_from, current_date,
        )
    except ValueError as e:
        raise HTTPException(404, str(e))

    # Batch-resolve folio -> ISIN for all folios in the response
    cams_folios, kfin_folios = rg._find_folios(row)
    all_folios = list({*cams_folios, *kfin_folios})
    folio_isin = _folio_to_isin_map(all_folios)

    # Skip-back baseline for 1-day diff
    our_isins = {v for v in folio_isin.values() if v}
    baseline_iso, prev_map, _ = ns.resolve_prev_nav_for_folios(
        current_date_iso, tuple(sorted(our_isins)),
    )

    # Per-scheme rows
    rows_out: list[PortfolioRow] = []
    total_1d = 0.0
    for r in summary_rows:
        invested = float(r["Invested"] or 0.0)
        current_value = float(r["Value"]) if r["Value"] is not None else None
        gain = float(r["Gain/Loss"]) if r["Gain/Loss"] is not None else None

        isin = folio_isin.get(r["Folio"])
        scheme_units = None
        current_nav = None
        diff_1d = None

        if isin and current_value is not None:
            nav_hit = ns.fetch_nav_by_isin(isin)
            if nav_hit:
                current_nav = nav_hit[0]
                if current_nav:
                    scheme_units = current_value / current_nav
                prev_nav = prev_map.get(isin) if prev_map else None
                if prev_nav and scheme_units is not None:
                    diff_1d = (current_nav - prev_nav) * scheme_units
                    total_1d += diff_1d

        rows_out.append(PortfolioRow(
            rta=r["RTA"],
            folio_id=r["Folio"],
            scheme_name=r["Scheme"],
            isin=isin,
            units=scheme_units,
            current_nav=current_nav,
            invested=invested,
            current_value=current_value,
            gain_loss=gain,
            one_day_diff=diff_1d,
        ))

    return PortfolioSummary(
        total_invested=round(total_inv, 2),
        total_current=round(total_val, 2),
        total_gain_loss=round(total_val - total_inv, 2),
        one_day_diff=round(total_1d, 2),
        folios=len(all_folios),
        prev_nav_date=baseline_iso,
        holdings=rows_out,
    )