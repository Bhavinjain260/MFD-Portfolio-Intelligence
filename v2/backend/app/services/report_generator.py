"""
report_generator.py
===================
PDF / HTML report generation for Valuation and Capital Gain.
"""
from __future__ import annotations

import os
import re
from datetime import date as date_cls
from datetime import datetime

import pandas as pd

from app.core.compat import get_conn
from app.services import capital_gain as cg
from app.services import nav_service as ns
from app.services import folio_resolver as fr

# ══════════════════════════════════════════════════════════════
# Low-level helpers
# ══════════════════════════════════════════════════════════════

def _find_font_path() -> tuple:
    candidates = [
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        ("/Library/Fonts/Arial Unicode.ttf", "/Library/Fonts/Arial Bold.ttf"),
        (r"C:\Windows\Fonts\arial.ttf", r"C:\Windows\Fonts\arialbd.ttf"),
        ("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
        ("/usr/share/fonts/TTF/DejaVuSans.ttf", "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"),
    ]
    for regular, bold in candidates:
        if os.path.exists(regular):
            return (regular, bold if os.path.exists(bold) else regular)
    return (None, None)


def _shorten_scheme_for_summary(name: str) -> str:
    if not name: return ""
    matches = list(re.finditer(r"\bFUND\b", name, flags=re.IGNORECASE))
    if matches: return name[:matches[-1].end()].strip()
    return name.strip()


def _truncate_to_width(pdf, text: str, max_width_mm: float) -> str:
    if not text: return ""
    text = str(text)
    if pdf.get_string_width(text) <= max_width_mm: return text
    for i in range(len(text) - 1, 0, -1):
        candidate = text[:i].rstrip() + "…"
        if pdf.get_string_width(candidate) <= max_width_mm: return candidate
    return "…"


def _txn_type_label(row) -> str:
    direction = str(row.get("direction", "")).strip().upper()
    if direction == "IN": return "Purchase"
    if direction == "OUT": return "Redemption"
    if direction == "NEUTRAL": return "Reversal"
    raw = str(row.get("trxntype", "")).strip().upper()
    if not raw: return ""
    if raw.startswith("R"): return "Redemption"
    if raw.startswith("P"): return "Purchase"
    if "SWITCH" in raw: return "Switch In" if "IN" in raw else "Switch Out"
    return raw


# ══════════════════════════════════════════════════════════════
# Low-level: Capital Gain PDF
# ══════════════════════════════════════════════════════════════
def _render_capital_gain_pdf(
    client_name: str, pan: str, client_code: str, fy_str: str,
    detail_rows: list[dict], total_buy: float, total_sale: float, total_gain: float,
) -> bytes | None:
    try:
        from fpdf import FPDF
    except ImportError:
        return None

    reg_path, bold_path = _find_font_path()
    if not reg_path: return None

    pdf = FPDF(orientation="L", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_font("Main", "", reg_path, uni=True)
    pdf.add_font("Main", "B", bold_path, uni=True)
    pdf.set_margins(12, 12, 12)

    BLUE, DARK, GREY = (41, 128, 185), (44, 62, 80), (127, 140, 141)
    LIGHT, WHITE = (236, 240, 241), (255, 255, 255)
    GREEN, RED = (39, 174, 96), (192, 57, 43)
    PAGE_W = 297 - 24

    def _fmt_inr(val):
        try: return f"\u20b9{float(val):,.2f}"
        except: return "N/A"
    def _fmt_units(val):
        try: return f"{float(val):.4f}"
        except: return "N/A"
    def _fmt_date(val):
        if val is None: return ""
        try: return val.strftime("%d-%m-%Y")
        except: return str(val)

    pdf.add_page()
    pdf.set_font("Main", "B", 16); pdf.set_text_color(*DARK)
    pdf.cell(PAGE_W, 10, "Capital Gain Report", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)
    pdf.set_font("Main", "", 9); pdf.set_text_color(*GREY)
    pdf.cell(PAGE_W, 5, f"Client: {client_name}", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(PAGE_W, 5, f'PAN: {pan or "N/A"}   |   Code: {client_code}   |   FY: {fy_str}', new_x="LMARGIN", new_y="NEXT")
    pdf.cell(PAGE_W, 5, "Scope: CAMS folios only. KFinTech redemption tracking not yet implemented.", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    pdf.set_fill_color(*LIGHT); col_w = PAGE_W / 3
    pdf.set_font("Main", "B", 8); pdf.set_text_color(*GREY)
    pdf.cell(col_w, 5, "Total Purchase", border=0, fill=True, new_x="RIGHT")
    pdf.cell(col_w, 5, "Total Redemption", border=0, fill=True, new_x="RIGHT")
    pdf.cell(col_w, 5, "Total Gain / Loss", border=0, fill=True, new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Main", "B", 11); pdf.set_text_color(*DARK)
    pdf.cell(col_w, 7, _fmt_inr(total_buy), border=0, fill=True, new_x="RIGHT")
    pdf.cell(col_w, 7, _fmt_inr(total_sale), border=0, fill=True, new_x="RIGHT")
    pdf.set_text_color(*(GREEN if total_gain >= 0 else RED))
    pdf.cell(col_w, 7, _fmt_inr(total_gain), border=0, fill=True, new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(*DARK); pdf.ln(3)

    stcg = sum(r.get("Gain/Loss", 0) for r in detail_rows if (r.get("Holding Days") or 0) < 365)
    ltcg = sum(r.get("Gain/Loss", 0) for r in detail_rows if (r.get("Holding Days") or 0) >= 365)
    LTCG_EXEMPTION = 125000
    ltcg_taxable = max(ltcg - LTCG_EXEMPTION, 0)

    pdf.set_font("Main", "B", 8); pdf.set_text_color(*GREY)
    pdf.cell(col_w, 5, "STCG (held < 12m)", border=0, new_x="RIGHT")
    pdf.cell(col_w, 5, "LTCG (held >= 12m)", border=0, new_x="RIGHT")
    pdf.cell(col_w, 5, "Taxable LTCG (after 1.25L exemption)", border=0, new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Main", "B", 10); pdf.set_text_color(*DARK)
    pdf.cell(col_w, 6, _fmt_inr(stcg), border=0, new_x="RIGHT")
    pdf.cell(col_w, 6, _fmt_inr(ltcg), border=0, new_x="RIGHT")
    pdf.cell(col_w, 6, _fmt_inr(ltcg_taxable), border=0, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)

    if any(r.get("Purchase Date") and r["Purchase Date"] < date_cls(2018, 1, 31) for r in detail_rows):
        pdf.set_font("Main", "B", 8); pdf.set_text_color(*RED)
        pdf.cell(PAGE_W, 5, "WARNING: Some purchases predate 31-Jan-2018. Grandfathering (FMV) rules NOT applied - LTCG cost basis may be understated.", new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(*DARK); pdf.ln(2)

    pdf.set_font("Main", "B", 11); pdf.set_text_color(*BLUE)
    pdf.cell(PAGE_W, 6, "Realized Gains — Transaction Detail", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    headers = ["Scheme", "Folio", "Pur. Date", "Pur. Units", "Pur. NAV", "Pur. Value", "Red. Date", "Red. Units", "Red. NAV", "Red. Value", "Days", "Type", "Gain/Loss"]
    widths = [78, 22, 15, 15, 13, 16, 15, 15, 13, 16, 12, 12, 0]
    widths[-1] = PAGE_W - sum(widths[:-1])
    if widths[-1] <= 0: raise ValueError(f"Column widths exceed page width by {-widths[-1]:.1f}mm")
    aligns = ["L", "C", "C", "R", "R", "R", "C", "R", "R", "R", "C", "C", "R"]

    row_h = 6
    pdf.set_font("Main", "B", 7); pdf.set_fill_color(*BLUE); pdf.set_text_color(*WHITE)
    for h, w in zip(headers, widths): pdf.cell(w, row_h, h, border=1, align="C", fill=True)
    pdf.ln()

    rows_sorted = sorted(detail_rows, key=lambda r: (r.get("Redemption Date") or date_cls.min))
    pdf.set_font("Main", "", 7); pdf.set_text_color(*DARK)
    for i, row in enumerate(rows_sorted):
        fill = (i % 2 == 1)
        if fill: pdf.set_fill_color(*LIGHT)
        scheme_txt = _truncate_to_width(pdf, row.get("Scheme", ""), widths[0] - 1.0)
        vals = [
            scheme_txt, str(row.get("Folio", "")), _fmt_date(row.get("Purchase Date")),
            _fmt_units(row.get("Purchase Units")), _fmt_units(row.get("Purchase NAV")), _fmt_inr(row.get("Purchase Value")),
            _fmt_date(row.get("Redemption Date")), _fmt_units(row.get("Redemption Units")), _fmt_units(row.get("Redemption NAV")), _fmt_inr(row.get("Redemption Value")),
            str(row.get("Holding Days", "")), str(row.get("Type", "")), _fmt_inr(row.get("Gain/Loss")),
        ]
        for v, w, a in zip(vals, widths, aligns): pdf.cell(w, row_h, v, border=1, align=a, fill=fill)
        pdf.ln()

    pdf.set_font("Main", "B", 7); pdf.set_fill_color(200, 200, 200)
    total_vals = ["TOTAL", "", "", "", "", _fmt_inr(total_buy), "", "", "", _fmt_inr(total_sale), "", "", _fmt_inr(total_gain)]
    for v, w, a in zip(total_vals, widths, aligns): pdf.cell(w, row_h, v, border=1, align=a, fill=True)
    pdf.ln(8)

    pdf.set_font("Main", "", 7); pdf.set_text_color(*GREY)
    pdf.cell(PAGE_W, 4, f'Report generated on {datetime.now().strftime("%d/%m/%Y %H:%M")}  |  Assumes equity taxation (STCG 20%, LTCG 12.5% above 1.25L exemption). Indicative only - consult your tax advisor.', align="C", new_x="LMARGIN", new_y="NEXT")

    return bytes(pdf.output())


# ══════════════════════════════════════════════════════════════
# Low-level: Capital Gain HTML
# ══════════════════════════════════════════════════════════════
def _render_capital_gain_html(
    client_name: str, pan: str, client_code: str, fy_str: str,
    detail_rows: list[dict], total_buy: float, total_sale: float, total_gain: float,
) -> str:
    def fi(v):
        try: return f"₹{float(v):,.2f}"
        except: return "N/A"
    def fd(v):
        if v is None: return ""
        try: return v.strftime("%d-%m-%Y")
        except: return str(v)

    gain_cls = "positive" if total_gain >= 0 else "negative"
    stcg = sum(r.get("Gain/Loss", 0) for r in detail_rows if (r.get("Holding Days") or 0) < 365)
    ltcg = sum(r.get("Gain/Loss", 0) for r in detail_rows if (r.get("Holding Days") or 0) >= 365)
    LTCG_EXEMPTION = 125000
    ltcg_taxable = max(ltcg - LTCG_EXEMPTION, 0)
    pre_2018 = [r for r in detail_rows if r.get("Purchase Date") and r["Purchase Date"] < date_cls(2018, 1, 31)]

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Capital Gain Report - {client_name}</title>
<style>
  @page {{ size: A4 landscape; margin: 12mm; }}
  body {{ font-family: Arial, Helvetica, sans-serif; font-size: 10px; color: #2c3e50; margin: 0; padding: 15px; }}
  h1 {{ font-size: 17px; text-align: center; margin: 0 0 3px 0; }}
  .meta {{ text-align: center; color: #7f8c8d; font-size: 9px; margin-bottom: 12px; }}
  .metrics {{ display: flex; gap: 10px; margin-bottom: 8px; }}
  .metric-box {{ flex: 1; background: #ecf0f1; padding: 8px; border-radius: 4px; text-align: center; }}
  .metric-box .label {{ font-size: 8px; color: #7f8c8d; text-transform: uppercase; }}
  .metric-box .value {{ font-size: 14px; font-weight: bold; margin-top: 2px; }}
  .tax-row {{ display: flex; gap: 10px; margin-bottom: 14px; }}
  .tax-box {{ flex: 1; background: #eaf2f8; padding: 6px 8px; border-radius: 4px; text-align: center; border-left: 3px solid #2980b9; }}
  .tax-box .label {{ font-size: 8px; color: #7f8c8d; text-transform: uppercase; }}
  .tax-box .value {{ font-size: 12px; font-weight: bold; margin-top: 2px; color: #2c3e50; }}
  .positive {{ color: #27ae60; }}
  .negative {{ color: #c0392b; }}
  .warn {{ background: #fdecea; border-left: 4px solid #c0392b; padding: 8px; font-size: 9px; margin-bottom: 12px; border-radius: 3px; }}
  table {{ border-collapse: collapse; width: 100%; margin-bottom: 10px; font-size: 8.5px; }}
  th {{ background: #2980b9; color: white; padding: 4px 4px; text-align: center; font-weight: bold; }}
  td {{ padding: 3px 4px; border: 1px solid #dcdcdc; }}
  tr:nth-child(even) {{ background: #f7fafc; }}
  .total-row {{ font-weight: bold; background: #e0e0e0 !important; }}
  .right {{ text-align: right; }}
  .center {{ text-align: center; }}
  .footer {{ text-align: center; color: #aaa; font-size: 8px; margin-top: 20px; border-top: 1px solid #eee; padding-top: 6px; }}
</style></head><body>
<h1>Capital Gain Report</h1>
<div class="meta">
  {client_name} &nbsp;|&nbsp; PAN: {pan or "N/A"} &nbsp;|&nbsp; Code: {client_code} &nbsp;|&nbsp; FY: {fy_str}<br>
  Scope: CAMS folios only. KFinTech redemption tracking not yet implemented.
</div>
<div class="metrics">
  <div class="metric-box"><div class="label">Total Purchase</div><div class="value">{fi(total_buy)}</div></div>
  <div class="metric-box"><div class="label">Total Redemption</div><div class="value">{fi(total_sale)}</div></div>
  <div class="metric-box"><div class="label">Gain / Loss</div><div class="value {gain_cls}">{fi(total_gain)}</div></div>
</div>
<div class="tax-row">
  <div class="tax-box"><div class="label">STCG (held &lt; 12m)</div><div class="value">{fi(stcg)}</div></div>
  <div class="tax-box"><div class="label">LTCG (held ≥ 12m)</div><div class="value">{fi(ltcg)}</div></div>
  <div class="tax-box"><div class="label">LTCG Exemption Used</div><div class="value">{fi(min(max(ltcg, 0), LTCG_EXEMPTION))}</div></div>
  <div class="tax-box"><div class="label">Taxable LTCG</div><div class="value">{fi(ltcg_taxable)}</div></div>
</div>
"""
    if pre_2018:
        html += f'<div class="warn">⚠️ <strong>{len(pre_2018)} purchase(s) predate 31-Jan-2018.</strong> Grandfathering (FMV) rules are NOT applied — LTCG cost basis may be understated. Verify before filing.</div>'

    html += """<table>
<tr>
  <th class="left">Scheme</th>
  <th class="center">Folio</th>
  <th class="center">Purchase Date</th>
  <th class="right">Purchase Units</th>
  <th class="right">Purchase NAV</th>
  <th class="right">Purchase Value</th>
  <th class="center">Redemption Date</th>
  <th class="right">Redemption Units</th>
  <th class="right">Redemption NAV</th>
  <th class="right">Redemption Value</th>
  <th class="center">Holding Days</th>
  <th class="center">Type</th>
  <th class="right">Gain/Loss</th>
</tr>
"""

    for r in sorted(detail_rows, key=lambda x: x.get("Redemption Date") or date_cls.min):
        gl = r.get("Gain/Loss")
        gl_cls = "positive" if gl and gl >= 0 else ("negative" if gl and gl < 0 else "")
        html += f"""<tr>
  <td>{r.get('Scheme', '')}</td>
  <td class="center">{r.get('Folio', '')}</td>
  <td class="center">{fd(r.get('Purchase Date'))}</td>
  <td class="right">{r.get('Purchase Units', 0):.4f}</td>
  <td class="right">{r.get('Purchase NAV', 0):.4f}</td>
  <td class="right">{fi(r.get('Purchase Value'))}</td>
  <td class="center">{fd(r.get('Redemption Date'))}</td>
  <td class="right">{r.get('Redemption Units', 0):.4f}</td>
  <td class="right">{r.get('Redemption NAV', 0):.4f}</td>
  <td class="right">{fi(r.get('Redemption Value'))}</td>
  <td class="center">{r.get('Holding Days', '')}</td>
  <td class="center">{r.get('Type', '')}</td>
  <td class="right {gl_cls}">{fi(gl)}</td>
</tr>"""

    html += f"""<tr class="total-row">
  <td colspan="5" style="text-align: left;">TOTAL</td>
  <td class="right">{fi(total_buy)}</td>
  <td colspan="3"></td>
  <td class="right">{fi(total_sale)}</td>
  <td colspan="2"></td>
  <td class="right {gain_cls}">{fi(total_gain)}</td>
</tr>
</table>
<div class="footer">
  Report generated on {datetime.now().strftime("%d/%m/%Y %H:%M")} &nbsp;|&nbsp;
  Assumes equity taxation (STCG 20%, LTCG 12.5% above ₹1.25L exemption). Indicative only — consult your tax advisor.
</div>
</body></html>"""
    return html


# ══════════════════════════════════════════════════════════════
# Low-level: Valuation HTML
# ══════════════════════════════════════════════════════════════
def _render_valuation_html(
    client_name: str, pan: str, client_code: str, mobile: str,
    val_date_str: str, period_from_str: str, summary_rows: list[dict],
    rta_txns: dict, total_invested: float, total_value: float, total_gain: float,
    show_investor: bool = False,
) -> str:
    def fi(v):
        try: return f"₹{float(v):,.2f}"
        except: return "N/A"
    def fu(v):
        try: return f"{float(v):.4f}"
        except: return "N/A"

    n_folios = len(set(r.get("Folio", "") for r in summary_rows))
    gain_cls = "positive" if total_gain >= 0 else "negative"

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Valuation Report - {client_name}</title>
<style>
  @page {{ size: A4; margin: 12mm; }}
  body {{ font-family: Arial, Helvetica, sans-serif; font-size: 10px; color: #2c3e50; margin: 0; padding: 15px; }}
  h1 {{ font-size: 17px; text-align: center; margin: 0 0 3px 0; }}
  .meta {{ text-align: center; color: #7f8c8d; font-size: 9px; margin-bottom: 12px; }}
  .metrics {{ display: flex; gap: 10px; margin-bottom: 14px; }}
  .metric-box {{ flex: 1; background: #ecf0f1; padding: 8px; border-radius: 4px; text-align: center; }}
  .metric-box .label {{ font-size: 8px; color: #7f8c8d; text-transform: uppercase; }}
  .metric-box .value {{ font-size: 14px; font-weight: bold; margin-top: 2px; }}
  .positive {{ color: #27ae60; }}
  .negative {{ color: #c0392b; }}
  h2 {{ font-size: 12px; color: #2980b9; border-bottom: 2px solid #2980b9; padding-bottom: 3px; margin: 16px 0 6px 0; }}
  h3 {{ font-size: 11px; color: #2980b9; margin: 14px 0 4px 0; }}
  .scheme-hdr {{ font-size: 9.5px; font-weight: bold; margin: 8px 0 3px 0; color: #2c3e50; }}
  .no-txn {{ font-size: 9px; color: #aaa; font-style: italic; margin-bottom: 6px; }}
  table {{ border-collapse: collapse; width: 100%; margin-bottom: 10px; font-size: 9px; table-layout: fixed; }}
  th {{ background: #2980b9; color: white; padding: 4px 5px; text-align: center; font-weight: bold; }}
  td {{ padding: 3px 5px; border: 1px solid #dcdcdc; word-wrap: break-word; overflow-wrap: break-word; white-space: normal; }}
  tr:nth-child(even) {{ background: #f7fafc; }}
  .total-row {{ font-weight: bold; background: #e0e0e0 !important; }}
  .right {{ text-align: right; }}
  .left {{ text-align: left; }}
  .center {{ text-align: center; }}
  .footer {{ text-align: center; color: #aaa; font-size: 8px; margin-top: 20px; border-top: 1px solid #eee; padding-top: 6px; }}
  @media print {{ body {{ padding: 5px; }} .no-print {{ display: none; }} }}
</style></head><body>
<h1>Portfolio Valuation Report</h1>
<div class="meta">
  {client_name} &nbsp;|&nbsp; PAN: {pan or "N/A"} &nbsp;|&nbsp; Code: {client_code} &nbsp;|&nbsp; Mobile: {mobile or "N/A"}<br>
  Period: {period_from_str} to {val_date_str}
</div>
<div class="metrics">
  <div class="metric-box"><div class="label">Total Invested</div><div class="value">{fi(total_invested)}</div></div>
  <div class="metric-box"><div class="label">Total Value</div><div class="value">{fi(total_value)}</div></div>
  <div class="metric-box"><div class="label">Gain / Loss</div><div class="value {gain_cls}">{fi(total_gain)}</div></div>
</div>
"""

    investor_th = '<th class="left" style="width:18%">Investor</th>' if show_investor else ""
    scheme_w = "32%" if show_investor else "38%"
    html += f"""<h2>Scheme Summary</h2>
<table>
<tr>{investor_th}<th class="left" style="width:{scheme_w}">Scheme</th><th class="center" style="width:12%">Folio</th><th class="right" style="width:14%">Invested</th><th class="right" style="width:14%">Value</th><th class="right" style="width:14%">Gain/Loss</th><th class="right" style="width:14%">Return %</th></tr>"""

    for r in summary_rows:
        gl = r.get("Gain/Loss")
        gl_cls = "positive" if gl and gl >= 0 else ("negative" if gl and gl < 0 else "")
        ret = f"{(gl / r['Invested'] * 100):.2f}%" if gl is not None and r.get("Invested", 0) > 0 else "N/A"
        investor_td = f'<td class="left">{r.get("Investor","")}</td>' if show_investor else ""
        scheme_display = _shorten_scheme_for_summary(r.get("Scheme", ""))
        html += f"""<tr>
{investor_td}
<td class="left">{scheme_display}</td>
<td class="center">{r.get('Folio','')}</td>
<td class="right">{fi(r.get('Invested'))}</td>
<td class="right">{fi(r.get('Value'))}</td>
<td class="right {gl_cls}">{fi(gl)}</td>
<td class="right">{ret}</td></tr>"""

    total_ret = f"{(total_gain/total_invested*100):.2f}%" if total_invested > 0 else "N/A"
    total_investor_td = '<td class="left"></td>' if show_investor else ""
    html += f"""<tr class="total-row">
{total_investor_td}<td class="left">TOTAL</td><td class="center">{n_folios} folios</td>
<td class="right">{fi(total_invested)}</td><td class="right">{fi(total_value)}</td>
<td class="right {gain_cls}">{fi(total_gain)}</td><td class="right">{total_ret}</td></tr>
</table>
"""

    missing = [r for r in summary_rows if r.get("Value") is None]
    if missing:
        html += f'<div style="background:#fdecea;border-left:4px solid #c0392b;padding:8px;font-size:9px;margin-bottom:12px;border-radius:3px;">⚠️ <strong>{len(missing)} of {len(summary_rows)} scheme(s)</strong> have no NAV available for the valuation date. Total Value is understated. Affected: {", ".join(r.get("Scheme","?") for r in missing[:5])}{"..." if len(missing) > 5 else ""}</div>'

    html += """<h2>Transactions</h2>"""
    all_entries = rta_txns.get("CAMS", []) + rta_txns.get("KFinTech", [])
    all_entries.sort(key=lambda e: e["label"])

    for entry in all_entries:
        label, opening, tdf = entry["label"], entry["opening"], entry["df"]
        html += f'<div class="scheme-hdr">{label} &mdash; Opening: {opening:.4f} units</div>'
        if tdf.empty:
            html += '<div class="no-txn">(no transactions during this period)</div>'
            continue

        html += """<table>
<tr><th class="center">Date</th><th class="left">Type</th><th class="right">Units</th><th class="right">Price</th><th class="right">Amount</th><th class="right">Balance</th></tr>"""
        for _, row in tdf.iterrows():
            d = row["_date"].strftime("%d-%m-%Y") if pd.notna(row["_date"]) else ""
            price = row.get("purprice", row.get("td_pop", None))
            html += f"""<tr>
<td class="center">{d}</td>
<td class="left">{_txn_type_label(row)}</td>
<td class="right">{fu(row.get('signed_units', 0))}</td>
<td class="right">{fu(price) if pd.notna(price) else ''}</td>
<td class="right">{fi(row.get('amount', 0))}</td>
<td class="right">{fu(row.get('Balance', 0))}</td></tr>"""
        html += "</table>"

    html += f"""<div class="footer">
Report generated on {datetime.now().strftime("%d/%m/%Y %H:%M")}<br>
Valuations are based on AMFI NAVs as of the valuation date (or latest available) and are for information only. Past performance is not indicative of future results. Consult your financial advisor before making investment decisions.
</div>
</body></html>"""
    return html


# ══════════════════════════════════════════════════════════════
# Low-level: Valuation PDF
# ══════════════════════════════════════════════════════════════
def _render_valuation_pdf(
    client_name: str, pan: str, client_code: str, mobile: str, val_date_str: str,
    period_from_str: str, summary_rows: list[dict], rta_txns: dict,
    total_invested: float, total_value: float, total_gain: float, show_investor: bool = False,
) -> bytes | None:
    try:
        from fpdf import FPDF
    except ImportError:
        return None

    reg_path, bold_path = _find_font_path()
    if not reg_path: return None

    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_font("Main", "", reg_path, uni=True)
    pdf.add_font("Main", "B", bold_path, uni=True)
    pdf.set_margins(12, 12, 12)

    BLUE, DARK, GREY = (41, 128, 185), (44, 62, 80), (127, 140, 141)
    LIGHT, WHITE = (236, 240, 241), (255, 255, 255)
    GREEN, RED = (39, 174, 96), (192, 57, 43)
    PAGE_W = 210 - 24

    def _fmt_inr(val) -> str:
        try: return f"\u20b9{float(val):,.2f}"
        except: return "N/A"
    def _fmt_pct(val) -> str:
        try: return f"{float(val):.2f}%"
        except: return "N/A"
    def _fmt_units(val) -> str:
        try: return f"{float(val):.4f}"
        except: return "N/A"
    def _truncate_for_cell(text: str, col_width_mm: float, font_size_pt: float = 7) -> str:
        if not text: return ""
        max_chars = int(col_width_mm / 0.55)
        if len(text) > max_chars: return text[: max_chars - 1].strip() + "…"
        return text

    pdf.add_page()
    pdf.set_font("Main", "B", 16); pdf.set_text_color(*DARK)
    pdf.cell(PAGE_W, 10, "Portfolio Valuation Report", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)
    pdf.set_font("Main", "", 9); pdf.set_text_color(*GREY)
    pdf.cell(PAGE_W, 5, f"Client: {client_name}", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(PAGE_W, 5, f'PAN: {pan or "N/A"}   |   Code: {client_code}   |   Mobile: {mobile or "N/A"}', new_x="LMARGIN", new_y="NEXT")
    pdf.cell(PAGE_W, 5, f"Period: {period_from_str} to {val_date_str}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    pdf.set_fill_color(*LIGHT); col_w = PAGE_W / 3
    pdf.set_font("Main", "B", 8); pdf.set_text_color(*GREY)
    pdf.cell(col_w, 5, "Total Invested", border=0, fill=True, new_x="RIGHT")
    pdf.cell(col_w, 5, "Total Value", border=0, fill=True, new_x="RIGHT")
    pdf.cell(col_w, 5, "Gain / Loss", border=0, fill=True, new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Main", "B", 11); pdf.set_text_color(*DARK)
    pdf.cell(col_w, 7, _fmt_inr(total_invested), border=0, fill=True, new_x="RIGHT")
    pdf.cell(col_w, 7, _fmt_inr(total_value), border=0, fill=True, new_x="RIGHT")
    pdf.set_text_color(*(GREEN if total_gain >= 0 else RED))
    pdf.cell(col_w, 7, _fmt_inr(total_gain), border=0, fill=True, new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(*DARK); pdf.ln(6)

    pdf.set_font("Main", "B", 11); pdf.set_text_color(*BLUE)
    pdf.cell(PAGE_W, 6, "Scheme Summary", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    if show_investor:
        s_headers = ["Investor", "Scheme", "Folio", "Invested", "Value", "Gain/Loss", "Return %"]
        s_widths = [30, 40, 16, 24, 24, 24, 28]
        scheme_col_idx = 1
    else:
        s_headers = ["Scheme", "Folio", "Invested", "Value", "Gain/Loss", "Return %"]
        s_widths = [55, 20, 28, 28, 28, 27]
        scheme_col_idx = 0
    s_widths[-1] = PAGE_W - sum(s_widths[:-1])

    row_h = 6
    pdf.set_font("Main", "B", 7); pdf.set_fill_color(*BLUE); pdf.set_text_color(*WHITE)
    for h, w in zip(s_headers, s_widths): pdf.cell(w, row_h, h, border=1, align="C", fill=True)
    pdf.ln()

    pdf.set_font("Main", "", 7); pdf.set_text_color(*DARK)
    aligns = ["L", "L", "C", "R", "R", "R", "R"] if show_investor else ["L", "C", "R", "R", "R", "R"]

    for i, row in enumerate(summary_rows):
        fill = (i % 2 == 1)
        if fill: pdf.set_fill_color(*LIGHT)
        short_scheme = _shorten_scheme_for_summary(str(row.get("Scheme", "")))
        if show_investor:
            scheme_text = _truncate_for_cell(short_scheme, s_widths[scheme_col_idx], 7)
            vals = [str(row.get("Investor", "")), scheme_text, str(row.get("Folio", ""))]
        else:
            scheme_text = _truncate_for_cell(short_scheme, s_widths[scheme_col_idx], 7)
            vals = [scheme_text, str(row.get("Folio", ""))]
        vals += [
            _fmt_inr(row.get("Invested")),
            _fmt_inr(row.get("Value")),
            _fmt_inr(row.get("Gain/Loss")),
            _fmt_pct((row.get("Gain/Loss", 0) / row.get("Invested", 1) * 100) if row.get("Gain/Loss") is not None and row.get("Invested", 0) > 0 else None),
        ]
        for v, w, a in zip(vals, s_widths, aligns): pdf.cell(w, row_h, v, border=1, align=a, fill=fill)
        pdf.ln()

    pdf.set_font("Main", "B", 7); pdf.set_fill_color(200, 200, 200)
    n_folios = len(set(r.get("Folio", "") for r in summary_rows))
    total_vals = ["", "TOTAL", f"{n_folios} folios"] if show_investor else ["TOTAL", f"{n_folios} folios"]
    total_vals += [
        _fmt_inr(total_invested), _fmt_inr(total_value), _fmt_inr(total_gain),
        _fmt_pct(total_gain / total_invested * 100) if total_invested > 0 else "N/A",
    ]
    for v, w, a in zip(total_vals, s_widths, aligns): pdf.cell(w, row_h, v, border=1, align=a, fill=True)
    pdf.ln(8)

    t_headers = ["Date", "Type", "Units", "Price", "Amount", "Balance"]
    t_widths = [22, 22, 22, 22, 28, 22]
    t_widths[-1] = PAGE_W - sum(t_widths[:-1])
    t_aligns = ["C", "L", "R", "R", "R", "R"]

    all_entries = rta_txns.get("CAMS", []) + rta_txns.get("KFinTech", [])
    all_entries.sort(key=lambda e: e["label"])

    for entry in all_entries:
        label, opening, tdf = entry["label"], entry["opening"], entry["df"]
        pdf.set_font("Main", "B", 8); pdf.set_text_color(*DARK)
        pdf.cell(PAGE_W, 5, f"{label}  —  Opening: {opening:.4f} units", new_x="LMARGIN", new_y="NEXT")
        if tdf.empty:
            pdf.set_font("Main", "", 7); pdf.set_text_color(*GREY)
            pdf.cell(PAGE_W, 5, "(no transactions during this period)", new_x="LMARGIN", new_y="NEXT")
            pdf.ln(3)
            continue

        pdf.set_font("Main", "B", 6.5); pdf.set_fill_color(*BLUE); pdf.set_text_color(*WHITE)
        for h, w in zip(t_headers, t_widths): pdf.cell(w, 5, h, border=1, align="C", fill=True)
        pdf.ln()

        pdf.set_font("Main", "", 6.5); pdf.set_text_color(*DARK)
        for r_idx, (_, row) in enumerate(tdf.iterrows()):
            fill = (r_idx % 2 == 1)
            if fill: pdf.set_fill_color(*LIGHT)
            date_str = row["_date"].strftime("%d-%m-%Y") if pd.notna(row["_date"]) else ""
            price = row.get("purprice", row.get("td_pop", None))
            amount = row.get("amount", 0)
            vals = [
                date_str,
                _truncate_to_width(pdf, _txn_type_label(row), t_widths[1] - 1.0),
                _fmt_units(row.get("signed_units", 0)),
                _fmt_units(price) if pd.notna(price) else "",
                _fmt_inr(amount),
                _fmt_units(row.get("Balance", 0)),
            ]
            for v, w, a in zip(vals, t_widths, t_aligns): pdf.cell(w, 4.5, v, border=1, align=a, fill=fill)
            pdf.ln()
        pdf.ln(4)

    pdf.set_font("Main", "", 7); pdf.set_text_color(*GREY)
    pdf.cell(PAGE_W, 4, f'Report generated on {datetime.now().strftime("%d/%m/%Y %H:%M")}', align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(1)
    pdf.set_font("Main", "", 6.5)
    pdf.multi_cell(PAGE_W, 3.5, "Valuations are based on AMFI NAVs as of the valuation date (or latest available) and are for information only. Past performance is not indicative of future results. Consult your financial advisor before making investment decisions.", align="C")

    return bytes(pdf.output())


# ══════════════════════════════════════════════════════════════
# Email body
# ══════════════════════════════════════════════════════════════
def generate_email_body(client_name: str, report_type: str, period_str: str | None = None) -> str:
    greeting = f"Dear {client_name},"
    period_line = f"<br><strong>Period:</strong> {period_str}" if period_str else ""

    if report_type == "Capital Gain":
        intro = """
<p>Please find attached your <strong>Capital Gain Report</strong>. This report details realized capital gains/losses on your mutual fund redemptions, computed using the <strong>FIFO (First In, First Out)</strong> cost-basis method.</p>
<p><strong>What's in the report:</strong></p>
<ul>
    <li>Purchase and redemption details for every matched lot</li>
    <li>STCG / LTCG classification by holding period</li>
    <li>Realized gain/loss per transaction</li>
    <li>Tax summary (equity assumption)</li>
</ul>
"""
    else:
        intro = """
<p>Please find attached your <strong>Portfolio Valuation Report</strong>. This report gives a snapshot of your mutual fund holdings, current valuations, and investment performance as of the valuation date.</p>
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
        body {{ font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; line-height: 1.6; color: #333; margin: 0; padding: 20px; background-color: #f9f9f9; }}
        .email-container {{ max-width: 700px; margin: 0 auto; background: white; padding: 30px; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }}
        .header {{ border-bottom: 3px solid #2980b9; padding-bottom: 15px; margin-bottom: 20px; }}
        .header h2 {{ color: #2980b9; margin: 0; font-size: 24px; }}
        .greeting {{ font-size: 16px; color: #2c3e50; margin-bottom: 15px; }}
        .intro-section {{ background: #ecf0f1; padding: 15px; border-left: 4px solid #2980b9; margin: 20px 0; border-radius: 4px; }}
        .intro-section p, .intro-section li {{ color: #2c3e50; font-size: 14px; }}
        .intro-section ul {{ margin: 10px 0; padding-left: 20px; }}
        .attachment-note {{ background: #d5f4e6; border-left: 4px solid #27ae60; padding: 15px; margin: 25px 0; border-radius: 4px; }}
        .attachment-note strong {{ color: #27ae60; }}
        .feedback-section {{ background: #fff3cd; border-left: 4px solid #f39c12; padding: 15px; margin: 25px 0; border-radius: 4px; }}
        .feedback-section h3 {{ color: #e67e22; margin-top: 0; font-size: 16px; }}
        .feedback-section p {{ margin: 8px 0; color: #7d6608; font-size: 14px; }}
        .footer {{ border-top: 1px solid #ddd; padding-top: 20px; margin-top: 30px; color: #7f8c8d; font-size: 12px; }}
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
            Your <strong>{report_type} Report</strong> is attached to this email as a PDF. You can download, print, or save it for your records.
        </div>
        <div class="feedback-section">
            <h3>⏰ Please Review Within 24–48 Hours</h3>
            <p>Kindly review the attached report carefully. If you notice any discrepancy — a missing transaction, incorrect folio number, or a valuation that doesn't match your records — please reply to this email within 24–48 hours so we can correct it.</p>
        </div>
        <p style="color: #2c3e50; margin: 20px 0;"><strong>What to check:</strong></p>
        <ul style="color: #2c3e50; margin: 10px 0;">
            <li>Folio numbers and scheme names</li>
            <li>Transaction dates, units, and amounts</li>
            <li>NAV values used and valuation date</li>
            <li>For Capital Gain reports: purchase and redemption dates per matched lot</li>
        </ul>
        <div class="footer">
            <p><strong>Contact:</strong> If you have any questions about this report, please reply to this email.</p>
            <p><strong>Generated:</strong> {datetime.now().strftime("%d %B %Y at %I:%M %p")}<br>This is an automated report from the Portfolio Intelligence platform.</p>
            <p style="margin-top: 20px; color: #34495e;">Thank you for your continued trust.</p>
            <div class="signature">
                <strong>Best Regards,</strong><br>Portfolio Intelligence Team<br><em>Your Investment Partner</em>
            </div>
        </div>
    </div>
</body>
</html>"""


# ══════════════════════════════════════════════════════════════
# Data-assembly helpers
# ══════════════════════════════════════════════════════════════
def _fetch_client(client_code: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("""
            SELECT client_code, primary_holder_first_name || ' ' || primary_holder_last_name AS name,
                   primary_holder_pan, guardian_pan, indian_mobile_no AS mobile, email, city
            FROM bse_client_master WHERE client_code = ?
        """, (client_code,)).fetchone()
    return dict(row) if row else None


def _find_folios(client: dict) -> tuple[list[str], list[str]]:
    """Return (cams_folios, kfin_folios) for a client dict using the secure resolver."""
    pan = (client.get("primary_holder_pan") or "").strip().upper()
    guardian = (client.get("guardian_pan") or "").strip().upper()
    name = (client.get("name") or "").strip().upper()
    is_minor = not pan
    match_pan = guardian if is_minor else pan
    return fr.resolve_client_folios(match_pan, name, is_minor)


def _cams_scheme_lookup() -> pd.DataFrame:
    with get_conn() as conn:
        return pd.read_sql("""
            SELECT UPPER(TRIM(Channel_Partner_Code)) AS product_code, MAX(Scheme_Name) AS scheme_name, MAX(ISIN) AS isin
            FROM bse_scheme_master WHERE Channel_Partner_Code IS NOT NULL AND TRIM(Channel_Partner_Code) != ''
            GROUP BY UPPER(TRIM(Channel_Partner_Code))
        """, conn)


def _get_cams_txns_raw(folio_no: str, product_code: str) -> pd.DataFrame:
    with get_conn() as conn:
        df = pd.read_sql("""
            SELECT COALESCE(txn_date_iso, traddate) AS traddate, trxntype, trxn_nature, units, purprice, amount
            FROM v_cams_txn_active WHERE folio_no = ? AND UPPER(TRIM(prodcode)) = ?
        """, conn, params=(folio_no, product_code.strip().upper()))
    if df.empty: return df
    df["_sort_date"] = pd.to_datetime(df["traddate"], errors="coerce", format="mixed")
    return df.sort_values("_sort_date", kind="stable").drop(columns=["_sort_date"]).reset_index(drop=True)


def _get_kfin_txns_raw(folio_no: str, product_code: str) -> pd.DataFrame:
    with get_conn() as conn:
        df = pd.read_sql("""
            SELECT COALESCE(txn_date_iso, td_trdt) AS traddate, td_purred AS trxntype, td_units AS units, td_pop AS purprice, td_amt AS amount
            FROM v_kfin_txn_active WHERE td_acno = ? AND UPPER(TRIM(fmcode)) = ?
        """, conn, params=(folio_no, product_code.strip().upper()))
    if df.empty: return df
    df["_sort_date"] = pd.to_datetime(df["traddate"], errors="coerce", format="mixed")
    return df.sort_values("_sort_date", kind="stable").drop(columns=["_sort_date"]).reset_index(drop=True)


def _fetch_all_folio_transactions(folio_no: str, rta: str) -> pd.DataFrame:
    if rta == "CAMS":
        with get_conn() as conn:
            codes = [r[0] for r in conn.execute("SELECT DISTINCT TRIM(UPPER(prodcode)) AS pc FROM v_cams_txn_active WHERE folio_no = ?", (folio_no,)).fetchall()]
        frames = []
        for pc in codes:
            t = _get_cams_txns_raw(folio_no, pc)
            if not t.empty:
                t["product_code"] = pc
                frames.append(t)
    else:
        with get_conn() as conn:
            codes = [r[0] for r in conn.execute("SELECT DISTINCT TRIM(UPPER(fmcode)) AS pc FROM v_kfin_txn_active WHERE td_acno = ?", (folio_no,)).fetchall()]
        frames = []
        for pc in codes:
            t = _get_kfin_txns_raw(folio_no, pc)
            if not t.empty:
                t["product_code"] = pc
                frames.append(t)

    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if df.empty: return df
    if "trxntype" not in df.columns: df["trxntype"] = ""
    df["_date"] = pd.to_datetime(df["traddate"], errors="coerce", format="mixed")
    df = df.dropna(subset=["_date"]).copy()
    df["units"] = pd.to_numeric(df["units"], errors="coerce").fillna(0)
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce").fillna(0)
    df["purprice"] = pd.to_numeric(df["purprice"], errors="coerce")

    if rta == "CAMS":
        is_red = df["trxntype"].astype(str).str.strip().str.upper() == "R1"
        df["direction"] = is_red.map({True: "OUT", False: "IN"})
        df["signed_units"] = df["units"].where(~is_red, -df["units"])
    else:
        df["direction"] = df["units"].apply(lambda u: "OUT" if u < 0 else ("IN" if u > 0 else "NEUTRAL"))
        df["signed_units"] = df["units"]

    return df.sort_values("_date", kind="stable").reset_index(drop=True)


def _calc_units_before(txn_df: pd.DataFrame, pc: str, before_date: pd.Timestamp) -> float:
    mask = (txn_df["product_code"] == pc) & (txn_df["_date"] < before_date)
    return max(float(txn_df.loc[mask, "signed_units"].sum()), 0.0)


def _calc_units_upto(txn_df: pd.DataFrame, pc: str, as_of_date: pd.Timestamp) -> float:
    mask = (txn_df["product_code"] == pc) & (txn_df["_date"] <= as_of_date)
    return max(float(txn_df.loc[mask, "signed_units"].sum()), 0.0)


def _calc_invested_upto(txn_df: pd.DataFrame, pc: str, as_of_date: pd.Timestamp) -> float:
    mask = ((txn_df["product_code"] == pc) & (txn_df["_date"] <= as_of_date))
    subset = txn_df.loc[mask].copy()
    if subset.empty: return 0.0
    try:
        lots, _ = cg.replay_folio_scheme(subset)
        return float(sum(l.remaining_units * l.rate for l in lots))
    except Exception:
        in_mask = subset["direction"] == "IN"
        out_mask = subset["direction"] == "OUT"
        return float(subset.loc[in_mask, "amount"].sum() - subset.loc[out_mask, "amount"].sum())


def _build_valuation_data(client_code: str, period_from: date_cls, val_date: date_cls) -> tuple[dict, list[dict], dict, float, float, float]:
    client = _fetch_client(client_code)
    if not client: raise ValueError(f"Client {client_code} not found")

    cams_folios, kfin_folios = _find_folios(client)
    all_folios = list({*cams_folios, *kfin_folios})
    if not all_folios: return client, [], {"CAMS": [], "KFinTech": []}, 0.0, 0.0, 0.0

    folio_rta_map = {f: "CAMS" for f in cams_folios}
    folio_rta_map.update({f: "KFinTech" for f in kfin_folios})

    scheme_lookup = _cams_scheme_lookup()
    isin_lk = dict(zip(scheme_lookup["product_code"], scheme_lookup["isin"]))
    name_lk = dict(zip(scheme_lookup["product_code"], scheme_lookup["scheme_name"]))

    val_iso = val_date.strftime("%Y-%m-%d")
    from_ts = pd.Timestamp(period_from)
    val_ts = pd.Timestamp(val_date)

    today = date_cls.today()
    if val_date >= today:
        ns.download_and_save_nav_if_needed()
        raw = ns._amfi.load()
        nav_map = {k: v[0] for k, v in raw.items() if v[0] > 0}
    else:
        nav_map = ns.get_or_fetch_nav_for_date(val_iso)

    summary_rows: list[dict] = []
    rta_scheme_txns: dict = {"CAMS": [], "KFinTech": []}

    for folio_no in sorted(all_folios):
        rta = folio_rta_map.get(folio_no)
        if not rta: continue
        all_txn = _fetch_all_folio_transactions(folio_no, rta)
        if all_txn.empty: continue

        for pc in all_txn["product_code"].unique():
            opening = _calc_units_before(all_txn, pc, from_ts)
            closing = _calc_units_upto(all_txn, pc, val_ts)
            invested = _calc_invested_upto(all_txn, pc, val_ts)
            if closing <= 0 and opening <= 0: continue

            isin = isin_lk.get(pc)
            nav = nav_map.get(str(isin).strip().upper()) if isin else None
            value = closing * nav if nav else None
            gain = (value - invested) if value is not None else None
            sname = name_lk.get(pc, pc)

            with get_conn() as conn:
                inv_row = conn.execute("SELECT inv_name FROM cams_wbr9_folio WHERE foliochk = ? UNION ALL SELECT investor_name FROM kfin_mfsd211_folio WHERE folio = ? LIMIT 1", (folio_no, folio_no)).fetchone()
            investor_name = (inv_row[0] or "").strip() if inv_row else ""

            summary_rows.append({
                "Scheme": sname, "Folio": folio_no, "RTA": rta, "Investor": investor_name,
                "ProductCode": pc, "ISIN": isin, "Units": closing, "Invested": invested,
                "Value": value, "Gain/Loss": gain,
            })

            mask = ((all_txn["product_code"] == pc) & (all_txn["_date"] >= from_ts) & (all_txn["_date"] <= val_ts))
            pt = all_txn[mask].copy()
            if not pt.empty: pt["Balance"] = opening + pt["signed_units"].cumsum()

            rta_key = rta if rta in rta_scheme_txns else "CAMS"
            rta_scheme_txns[rta_key].append({"label": f"{sname} ({folio_no})", "opening": opening, "df": pt})

    total_invested = sum(r["Invested"] for r in summary_rows)
    total_value = sum(r["Value"] or 0 for r in summary_rows)
    total_gain = total_value - total_invested
    return client, summary_rows, rta_scheme_txns, total_invested, total_value, total_gain


def _build_capital_gain_rows(client_code: str, fy: str | None) -> tuple[dict, list[dict]]:
    client = _fetch_client(client_code)
    if not client: raise ValueError(f"Client {client_code} not found")

    cams_folios, _ = _find_folios(client)
    if not cams_folios: return client, []

    cams_schemes = _cams_scheme_lookup()
    detail_rows: list[dict] = []

    with get_conn() as conn:
        ph = ",".join(["?"] * len(cams_folios))
        rows = pd.read_sql(f"SELECT DISTINCT folio_no, UPPER(TRIM(prodcode)) AS prodcode FROM v_cams_txn_active WHERE folio_no IN ({ph})", conn, params=cams_folios)
    rows = rows.merge(cams_schemes, left_on="prodcode", right_on="product_code", how="left")
    rows["scheme_name"] = rows["scheme_name"].fillna(rows["prodcode"])

    fy_start = fy_end = None
    if fy and fy != "All Time":
        try:
            y0 = int(fy.split("-")[0])
            fy_start = date_cls(y0, 4, 1)
            fy_end = date_cls(y0 + 1, 3, 31)
        except Exception:
            fy_start = fy_end = None

    for _, srow in rows.iterrows():
        folio_no, prodcode, scheme_name = srow["folio_no"], srow["prodcode"], srow["scheme_name"]
        txns = _get_cams_txns_raw(folio_no, prodcode)
        if txns.empty: continue

        try:
            _, matches = cg.replay_folio_scheme(txns, scheme_name=scheme_name)
        except Exception:
            continue

        for m in matches:
            if m.is_gap:  # Exclude data gaps
                continue
            if fy_start and m.sell_date and not (fy_start <= m.sell_date <= fy_end):
                continue
            detail_rows.append({
                "Scheme": scheme_name, "Folio": folio_no, "Purchase Date": m.buy_date,
                "Purchase Units": m.units, "Purchase NAV": (m.cost / m.units) if m.units else 0,
                "Purchase Value": m.cost, "Redemption Date": m.sell_date,
                "Redemption Units": m.units, "Redemption NAV": (m.proceeds / m.units) if m.units else 0,
                "Redemption Value": m.proceeds, "Holding Days": m.holding_days,
                "Type": "LTCG" if m.is_ltcg else "STCG", "Gain/Loss": m.gain,
            })

    return client, detail_rows


# ══════════════════════════════════════════════════════════════
# Public API
# ══════════════════════════════════════════════════════════════
def generate_valuation_pdf(*, client_code: str, fy: str | None = None, valuation_date: str | None = None) -> bytes:
    today = date_cls.today()
    if fy:
        try: y0 = int(fy.split("-")[0])
        except: y0 = today.year if today.month >= 4 else today.year - 1
    else:
        y0 = today.year if today.month >= 4 else today.year - 1
        fy = f"{y0}-{str(y0 + 1)[-2:]}"

    period_from = date_cls(y0, 4, 1)
    fy_end = date_cls(y0 + 1, 3, 31)
    default_val = min(fy_end, today)
    val_date = datetime.strptime(valuation_date, "%Y-%m-%d").date() if valuation_date else default_val

    client, summary_rows, rta_txns, t_inv, t_val, t_gain = _build_valuation_data(client_code, period_from, val_date)
    pdf = _render_valuation_pdf(
        client_name=client["name"], pan=client.get("primary_holder_pan") or "",
        client_code=client_code, mobile=client.get("mobile") or "",
        val_date_str=val_date.strftime("%Y-%m-%d"), period_from_str=period_from.strftime("%Y-%m-%d"),
        summary_rows=summary_rows, rta_txns=rta_txns, total_invested=t_inv, total_value=t_val, total_gain=t_gain,
    )
    if pdf is None: raise RuntimeError("PDF generation failed")
    return pdf


def generate_valuation_html(*, client_code: str, fy: str | None = None, valuation_date: str | None = None) -> str:
    today = date_cls.today()
    if fy:
        try: y0 = int(fy.split("-")[0])
        except: y0 = today.year if today.month >= 4 else today.year - 1
    else:
        y0 = today.year if today.month >= 4 else today.year - 1
        fy = f"{y0}-{str(y0 + 1)[-2:]}"

    period_from = date_cls(y0, 4, 1)
    val_date = datetime.strptime(valuation_date, "%Y-%m-%d").date() if valuation_date else min(date_cls(y0 + 1, 3, 31), today)
    client, summary_rows, rta_txns, t_inv, t_val, t_gain = _build_valuation_data(client_code, period_from, val_date)

    return _render_valuation_html(
        client_name=client["name"], pan=client.get("primary_holder_pan") or "",
        client_code=client_code, mobile=client.get("mobile") or "",
        val_date_str=val_date.strftime("%Y-%m-%d"), period_from_str=period_from.strftime("%Y-%m-%d"),
        summary_rows=summary_rows, rta_txns=rta_txns, total_invested=t_inv, total_value=t_val, total_gain=t_gain,
    )


def generate_capital_gain_pdf(*, client_code: str, fy: str | None = None) -> bytes:
    client, detail_rows = _build_capital_gain_rows(client_code, fy)
    if not detail_rows: raise ValueError(f"No realized capital gains found for {client_code} in FY {fy or 'All Time'}")
    total_buy = sum(r["Purchase Value"] for r in detail_rows)
    total_sale = sum(r["Redemption Value"] for r in detail_rows)
    total_gain = total_sale - total_buy
    pdf = _render_capital_gain_pdf(
        client_name=client["name"], pan=client.get("primary_holder_pan") or "",
        client_code=client_code, fy_str=fy or "All Time", detail_rows=detail_rows,
        total_buy=total_buy, total_sale=total_sale, total_gain=total_gain,
    )
    if pdf is None: raise RuntimeError("PDF generation failed")
    return pdf


def generate_capital_gain_html(*, client_code: str, fy: str | None = None) -> str:
    client, detail_rows = _build_capital_gain_rows(client_code, fy)
    total_buy = sum(r["Purchase Value"] for r in detail_rows)
    total_sale = sum(r["Redemption Value"] for r in detail_rows)
    total_gain = total_sale - total_buy
    return _render_capital_gain_html(
        client_name=client["name"], pan=client.get("primary_holder_pan") or "",
        client_code=client_code, fy_str=fy or "All Time", detail_rows=detail_rows,
        total_buy=total_buy, total_sale=total_sale, total_gain=total_gain,
    )