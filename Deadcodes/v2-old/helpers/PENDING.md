# What’s still pending (after this scaffold)

This file is the single checklist of **remaining work** on the live repo.
Everything marked ✅ already exists under `core/`, `jobs/`, `ui/`, etc. in the zip.

---

## Phase 0 — Apply deletes on your machine ☐

| Action | File |
|--------|------|
| Delete | `cache_functions.py` |
| Delete | `nav_queries.py` |
| Delete | `email_tempate.py` |
| Edit | `cams_mailback_sync.py` — keep **first** `_parse_and_move` (with `sflog.record_failure`); **delete second** |
| Edit | `cams_mailback_sync.py` — keep one `_mark_seen_safe`; delete duplicate |

---

## Phase 8 — `ingest/` parsers (partial) ☐

Done in zip: `ingest/common.py` (`raw_val`, `raw_num`, date helpers, `read_csv_auto`).

Still to move from `data_manager.py` → `ingest/`:

| Target | Source functions |
|--------|------------------|
| `ingest/parsers_bse.py` | `parse_bse_client_master`, `parse_bse_sip`, `parse_bse_scheme_master` |
| `ingest/parsers_cams.py` | `parse_cams_wbr4_aum` … `parse_cams_wbr77_brokerage` |
| `ingest/parsers_kfin.py` | `parse_kfin_mfsd203_aum` … `parse_kfin_mfsd205_brokerage` |
| `ingest/nav.py` | `parse_nav_file` + `nav_data_ingestion.*` |
| `ingest/mailback.py` | `cams_mailback_sync` minus UI / email |
| `ingest/bse.py` | `bse_auto._do_download`, `sync_now`, … |

Until moved, keep using `data_manager` / `cams_mailback_sync` / `bse_auto` as today.

---

## Phase 10 — Split `app.py` pages ☐

Done in zip: `ui/app.py` (router skeleton), `ui/cached.py`, `ui/pages/recon.py` stub.

Still to move (each `if mode == "..."` block):

| Page module | Mode string |
|-------------|-------------|
| `ui/pages/dashboard.py` | 📊 Dashboard |
| `ui/pages/clients.py` | 👤 Clients |
| `ui/pages/transactions.py` | 📋 Transactions |
| `ui/pages/brokerage.py` | 💰 Brokerage |
| `ui/pages/reports.py` | 📄 Reports |
| `ui/pages/cap_gains.py` | 📈 Capital Gains |
| `ui/pages/recon.py` | 🔍 AUM Reconciliation (finish: move `render_aum_recon_tab`) |
| `ui/pages/admin.py` | ⚙️ Admin Panel |
| `ui/reports/pdf.py` | capital gain / valuation / brokerage PDF generators |
| `ui/reports/html.py` | HTML report generators |
| `ui/reports/email_templates.py` | `generate_email_body`, brokerage email body |

Until then: **keep running the monolith `app.py`**.

---

## Phase 11 — Bug fixes

| Bug | Status in zip | Your action |
|-----|---------------|-------------|
| `task_bse` missing from worker loop | ✅ `jobs/worker.py` | Use scaffold worker or add `("bse", task_bse)` to your `background_worker.py` |
| Mailback duplicate `_parse_and_move` | Documented Phase 0 | Delete second def |
| KFin invested ≠ FIFO vs CAMS | ✅ `core/holdings` uses FIFO for both | Switch app callers to `core.holdings` |
| `Match.is_ltcg` vs `Holding Days >= 365` | ✅ `core/tax` exposes `is_ltcg` | PDF/HTML must use `m.is_ltcg`, not day count |
| XIRR `@st.cache_data` on core | ✅ stripped in `core/xirr` | Use `ui.cached.folio_xirr_cached` |
| Double `data_version` bump in worker | Optional | Remove final `bump()` in `run_all` if parsers already bump |
| Email send blocks UI | ☐ | Call `send_report_email_background` from `render_email_report_button` |
| `get_folio_to_client_code_map` misses minor KFin | ✅ name fallback in `core/brokerage` | Prefer core version |
| Family folio N+1 | ✅ `core/families.get_family_folios` | Prefer core version |
| `load_dedup_sip_counts` mixed IDs | ✅ renamed `get_sip_counts_by_source` in `core/dashboard` | Update callers |
| `force_reload` cache key | ☐ in monolith | Rename param to `_force_reload` on `get_all_folios_with_isin_and_nav` |
| `resolve_prev_nav_for_folios` returns tuple | ☐ | Optional dataclass later |

---

## Pure modules still only in monolith (optional extract)

These exist in `app.py` and are partially mirrored in core. Prefer core where listed:

| Monolith | Prefer |
|----------|--------|
| `get_client_identity` | `core.clients` |
| `compute_client_holdings` | `core.holdings.get_client_holdings` |
| `get_cams_invested_per_scheme` | `core.holdings.get_cams_invested` |
| `get_kfin_invested_per_scheme` | `core.holdings.get_kfin_invested` |
| `get_all_folios_with_isin_and_nav` | `core.holdings.get_all_folios_with_nav` (skeleton; full enrich still in app) |
| `get_enriched_folio_nav_df` | Still largely app-only (NAV merge) — extract when ready |
| `get_client_xirr_batch` | `ui.cached` + `core.xirr` |
| Dashboard loaders | `core.dashboard` |

---

## Suggested order on live repo

1. **Phase 0 deletes** (1 hour, real bugfix)  
2. Unzip scaffold; put `core/`, `jobs/`, `ui/`, `tools/`, `tests/` at project root  
3. Add shims from `SHIMS.md`  
4. Point one tab at `core.folios.get_folio_transactions` / `core.clients.get_client_folios`  
5. Swap worker to `jobs/worker.py` or patch `task_bse`  
6. Phase 8 parser moves when convenient  
7. Phase 10 page split last  

---

## Sanity checks after install

```bash
python -c "import sys; sys.path.insert(0,'.'); import core.folios; assert 'streamlit' not in sys.modules"
python tools/check_import_boundaries.py
```
