# Rest of changes + polishes applied in this package

## Phase 0 (applied on package copies)

- Removed **second** `_parse_and_move` from `cams_mailback_sync.py` (kept first with `sflog.record_failure`)
- Dead modules remain only under `original/` (`cache_functions`, `nav_queries`, `email_tempate`) — not at project root

## Phase 8 (parsers copied into ingest/)

- `ingest/parsers_bse.py`
- `ingest/parsers_cams.py`
- `ingest/parsers_kfin.py`
- `ingest/parsers_nav.py`
- `ingest/__init__.py` re-exports all

Live app still calls `data_manager.parse_*` (safe). New code can use `from ingest import ...`.

## Phase 10 (stubs)

- `ui/pages/{dashboard,clients,transactions,brokerage,reports,cap_gains,admin}.py`
- Still need to move UI blocks from `app.py` into these files

## Phase 11 polishes (applied to `app.py` / `background_worker.py`)

| Change | File |
|--------|------|
| `force_reload` → `_force_reload` on NAV folio loaders | `app.py` |
| Prefer `send_report_email_background` when available | `app.py` |
| **`("bse", task_bse)`** added to worker task loop | `background_worker.py` |
| Documented final `data_version` bump (kept for safety) | `background_worker.py` |
| Name-fallback on folio→client map | `core/brokerage.py` |
| `get_enriched_folio_nav_df` extracted | `core/enriched_nav.py` |
| Root `shims.py` for gradual cutover | `shims.py` |

## How to use shims

At the top of `app.py` after imports (optional):

```python
# Prefer core implementations (comment out local defs once verified)
from shims import (
    get_client_folios, search_clients, get_client_identity,
    get_folio_transactions, get_folio_rta,
    load_brokerage_report, compute_sub_broker_summary,
    load_dashboard_summary, load_db_stats,
)
```

Then delete the matching local `def` bodies when ready.

## Still manual (cannot fully auto-finish)

1. Move Streamlit **page UI** blocks from `app.py` into `ui/pages/*.py` (large, visual)
2. Slim `data_manager.py` so it only re-exports `ingest.parsers_*` (after you verify parsers)
3. Confirm `send_report_email_background` exists in your mail module; otherwise send stays sync via fallback
