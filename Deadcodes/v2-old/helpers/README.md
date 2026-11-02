# MFD Portfolio Intelligence — Full package

This zip contains:

1. **Runnable monolith** (root) — your current `app.py` + companion modules  
2. **Refactor scaffold** — pure `core/`, `ingest/`, `jobs/`, `ui/`, `tests/`, `tools/`  
3. **`original/`** — untouched copies of every source file for reference  
4. **Docs** — `REFACTOR.md`, `PENDING.md`, `PHASE0_DELETED.md`, `SHIMS.md`

## Layout

```
app.py                    ← full Streamlit app (run this today)
aum_recon.py
background_worker.py
bse_auto.py
cams_mailback_sync.py
capital_gain.py
data_manager.py
init_db.py
nav_data_ingestion.py
nav_service.py
runtime_status.py
sync_failure_log.py
xirr.py

core/                     ← pure business logic (no Streamlit)
ingest/                   ← parsers (partial)
jobs/                     ← background worker (task_bse fixed)
ui/                       ← thin Streamlit wrappers / page stubs
tests/
tools/
original/                 ← backup of sources as delivered

REFACTOR.md
PENDING.md
PHASE0_DELETED.md
SHIMS.md
README.md
```

## Run as today

```bash
streamlit run app.py
```

## Apply Phase 0 (safe first step)

```bash
rm -f cache_functions.py nav_queries.py email_tempate.py   # if present
# Then edit cams_mailback_sync.py: delete the SECOND _parse_and_move
# and the duplicate _mark_seen_safe (keep the first of each).
```

See `PHASE0_DELETED.md`.

## Use core/ without rewriting UI

At the top of `app.py` (or in a new `shims.py` imported early):

```python
from core.folios import get_folio_transactions, get_folio_rta
from core.clients import search_clients, get_client_identity, get_client_folios
from core.holdings import get_client_holdings, get_cams_invested, get_kfin_invested
from core.families import get_family_folios, get_family_for_client
from core.xirr import calculate_xirr, compute_xirr_for_folio
from core.tax import Lot, Match, match_fifo
from core.brokerage import load_brokerage_report, list_sub_brokers, compute_sub_broker_summary
from core.dashboard import load_dashboard_summary, load_db_stats
from core.db import get_conn, bump, current as data_version
```

Then delete the matching `def ...` bodies from `app.py` once imports work.

## Background worker

Prefer `jobs/worker.py` (includes `task_bse`) or patch your `background_worker.py` loop:

```python
tasks = [
    ("mailback", task_mailback),
    ("nav_live", task_nav_live),
    ("nav_prev", task_nav_prev),
    ("bse", task_bse),   # ← was missing
]
```

## Checks

```bash
python tools/check_import_boundaries.py
python -c "import sys; sys.path.insert(0,'.'); import core.folios; assert 'streamlit' not in sys.modules"
```

## What’s still pending

See **`PENDING.md`** — Phase 8 (full parsers), Phase 10 (page split), a few Phase 11 polish items.
