```markdown
# MFD Portfolio Intelligence — Refactor Reference

> **Purpose:** Working reference for converting `app.py` monolith into pure-Python `core/` services with thin Streamlit wrappers in `ui/`.
>
> **Status:** Design doc. Update checkboxes as phases complete.
>
> **Last revised:** 2026-10-22

---

## Table of Contents

1. [Target Architecture](#1-target-architecture)
2. [The Pattern](#2-the-pattern)
3. [Current State — Function Inventory](#3-current-state--function-inventory)
4. [Dead Code & Duplicates](#4-dead-code--duplicates)
5. [Known Bugs & Pitfalls](#5-known-bugs--pitfalls)
6. [Migration Plan](#6-migration-plan)
7. [Verification & Testing](#7-verification--testing)

---

## 1. Target Architecture

### 1.1 Directory layout

```
core/
  db.py             get_conn, DB_PATH, set_db_path, get_shared_read_conn
  clients.py        search_clients, get_client_identity, get_client_folios, get_client_schemes
  families.py       family CRUD, get_family_folios, get_family_holdings
  folios.py         get_folio_rta, get_folio_transactions, get_folio_schemes,
                    get_folio_invested, get_folio_units
  holdings.py       get_all_folios_with_nav, get_enriched_folios,
                    get_cams_invested, get_kfin_invested,
                    get_client_holdings, get_family_holdings
  nav.py            (current nav_service.py — already pure, just move)
  xirr.py           calculate_xirr, compute_xirr_for_folio, compute_portfolio_xirr
  tax.py            (current capital_gain.py)
  brokerage.py      load_brokerage_report, _attribute_brokerage,
                    compute_sub_broker_summary, sub-broker CRUD
  recon.py          (current aum_recon.py minus render_*)
  failure_log.py    (current sync_failure_log.py)
  calendar.py       (current trading_day_calender.py)
  email.py          send_report_email, get_client_email, credentials_configured

ingest/
  common.py         raw_val, raw_num, _clean_cols, _read_csv_auto, date helpers
  parsers_bse.py    parse_bse_client_master, parse_bse_sip, parse_bse_scheme_master
  parsers_cams.py   parse_cams_wbr4_aum … parse_cams_wbr77_brokerage
  parsers_kfin.py   parse_kfin_mfsd203_aum … parse_kfin_mfsd205_brokerage
  nav.py            parse_nav_file, ingest_nav_file_to_db
  mailback.py       (current cams_mailback_sync.py minus UI)
  bse_download.py   (current bse_auto.py minus status dict)

jobs/
  worker.py         (current background_worker.py)
  launcher.py       (current worker_launcher.py)
  watchdog.py       (current worker_watchdog.py)

ui/
  app.py            router only
  cached.py         all @st.cache_data wrappers
  theme.py          (current theme_patch.py)
  reports/
    pdf.py          all PDF generators
    html.py         all HTML generators
    email_templates.py  (generate_email_body, generate_brokerage_email_body)
  pages/
    dashboard.py, clients.py, transactions.py, brokerage.py,
    reports.py, cap_gains.py, recon.py, admin.py, settings.py
```

### 1.2 Layering rule (enforce with import lint)

| Layer | Can import | Cannot import |
|---|---|---|
| `core/` | stdlib, pandas, sqlite3, other `core/` | `streamlit`, `ingest/`, `ui/`, `jobs/` |
| `ingest/` | stdlib, pandas, `core/` | `streamlit`, `ui/` |
| `jobs/` | stdlib, `core/`, `ingest/` | `streamlit`, `ui/` |
| `ui/` | anything | — |

`nav_service.py` is the existing proof this works: it's pure, imported by both `app.py` and `background_worker.py` (the latter via shim only because *other* modules leak `st`).

---

## 2. The Pattern

Every `core/` function:

- **Takes plain args** — `str`, `int`, `datetime`, `list[str]`, `tuple`, optionally `conn` or `get_conn`
- **Returns plain data** — `pd.DataFrame`, `dict`, `dataclass`, `list[dict]`
- **Never imports streamlit**
- **Never reads `st.session_state`**
- **Never calls `st.*`**
- **Is fully testable** without a Streamlit runtime

Every `ui/cached.py` wrapper:

- **`@st.cache_data` only**
- **Delegates** to exactly one `core/` function
- **Keys on `data_version()`** where DB/NAV state can change
- **Uses `_v: int` parameter** (underscore) so cache key is explicit

### 2.1 Canonical example — `get_folio_transactions`

```python
# core/folios.py
from __future__ import annotations
import pandas as pd
from core.db import get_conn

EMPTY_TXN_COLS = [
    "txn_no", "date", "rta", "folio_no", "product_code", "trxn_type",
    "trxn_mode", "trxn_status", "units", "price", "amount",
    "brokerage_code", "sub_broker", "remarks", "scheme_name",
    "direction", "signed_units",
]

def _empty_txn_df() -> pd.DataFrame:
    return pd.DataFrame(columns=EMPTY_TXN_COLS)

def get_folio_rta(folio_no: str) -> str | None:
    """Return 'CAMS', 'KFinTech', or None if the folio isn't in either master."""
    with get_conn() as conn:
        if conn.execute(
            "SELECT 1 FROM cams_wbr9_folio WHERE foliochk=? LIMIT 1", (folio_no,)
        ).fetchone():
            return "CAMS"
        if conn.execute(
            "SELECT 1 FROM kfin_mfsd211_folio WHERE folio=? LIMIT 1", (folio_no,)
        ).fetchone():
            return "KFinTech"
    return None

def get_folio_transactions(
    folio_no: str,
    rta: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> pd.DataFrame:
    """
    Every transaction for a folio, one normalised shape.

    Columns (always present): see EMPTY_TXN_COLS.

    - `date` is ISO from txn_date_iso, falling back to raw traddate.
    - `direction` ∈ {"IN","OUT","NEUTRAL"} — already resolved.
    - `signed_units` negative for OUT. Use this for balances, not `units`.
    - `scheme_name` resolved via bse_scheme_master in one batch query.
    """
    rta = rta or get_folio_rta(folio_no)
    if rta is None:
        return _empty_txn_df()

    with get_conn() as conn:
        if rta == "CAMS":
            df = pd.read_sql(
                """
                SELECT trxnno             AS txn_no,
                       COALESCE(txn_date_iso, traddate) AS date,
                       folio_no,
                       UPPER(TRIM(prodcode)) AS product_code,
                       trxntype  AS trxn_type,
                       trxnmode  AS trxn_mode,
                       trxnstat  AS trxn_status,
                       units, purprice AS price, amount,
                       brokcode  AS brokerage_code,
                       subbrok   AS sub_broker,
                       remarks
                FROM v_cams_txn_active
                WHERE folio_no = ?
                ORDER BY COALESCE(txn_date_iso, traddate)
                """,
                conn, params=(folio_no,),
            )
        else:
            df = pd.read_sql(
                """
                SELECT td_trno            AS txn_no,
                       COALESCE(txn_date_iso, td_trdt) AS date,
                       td_acno            AS folio_no,
                       UPPER(TRIM(fmcode)) AS product_code,
                       td_purred          AS trxn_type,
                       trnmode            AS trxn_mode,
                       trnstat            AS trxn_status,
                       td_units           AS units,
                       td_pop             AS price,
                       td_amt             AS amount,
                       td_broker          AS brokerage_code,
                       ''                 AS sub_broker,
                       trdesc             AS remarks
                FROM v_kfin_txn_active
                WHERE td_acno = ?
                ORDER BY COALESCE(txn_date_iso, td_trdt)
                """,
                conn, params=(folio_no,),
            )

    if df.empty:
        return _empty_txn_df()

    df["rta"] = rta
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"])

    # Direction: single rule for the whole app
    if rta == "CAMS":
        is_red = df["trxn_type"].astype(str).str.strip().str.upper().eq("R1")
        df["direction"]    = is_red.map({True: "OUT", False: "IN"})
        df["signed_units"] = df["units"].where(~is_red, -df["units"])
    else:
        df["direction"]    = df["units"].apply(
            lambda u: "OUT" if u < 0 else ("IN" if u > 0 else "NEUTRAL")
        )
        df["signed_units"] = df["units"]

    # Scheme names: ONE batch query, not per-row
    pcodes = tuple(sorted(df["product_code"].dropna().unique()))
    if pcodes:
        ph = ",".join("?" * len(pcodes))
        with get_conn() as conn:
            schemes = pd.read_sql(
                f"""SELECT UPPER(TRIM(Channel_Partner_Code)) AS pc,
                           MAX(Scheme_Name) AS scheme_name
                    FROM bse_scheme_master
                    WHERE UPPER(TRIM(Channel_Partner_Code)) IN ({ph})
                    GROUP BY UPPER(TRIM(Channel_Partner_Code))""",
                conn, params=pcodes,
            )
        name_map = dict(zip(schemes["pc"], schemes["scheme_name"]))
        df["scheme_name"] = df["product_code"].map(name_map).fillna(df["product_code"])
    else:
        df["scheme_name"] = df["product_code"]

    if date_from:
        df = df[df["date"] >= date_from]
    if date_to:
        df = df[df["date"] <= date_to]

    return df[EMPTY_TXN_COLS].reset_index(drop=True)
```

```python
# ui/cached.py
import streamlit as st
from core.folios import get_folio_transactions
from core import data_version  # or data_manager.current

@st.cache_data(ttl=1800, show_spinner=False)
def folio_transactions_cached(folio_no: str, _v: int) -> "pd.DataFrame":
    return get_folio_transactions(folio_no)
```

```python
# ui/pages/clients.py
txn_df = folio_transactions_cached(selected_folio, data_version())
st.dataframe(txn_df)
```

### 2.2 Naming convention

- `core/` returns raw data: `get_folio_transactions`, `get_client_folios`
- `ui/cached.py` wraps with `_cached` suffix: `folio_transactions_cached`
- Never name a `core/` function `_cached`

---

## 3. Current State — Function Inventory

### 3.1 Tier 1 — Already pure, just move

| Function | Current file | Target |
|---|---|---|
| `calculate_xirr` | `xirr.py` | `core/xirr.py` |
| `_parse_date`, `Match`, `Lot`, `_consume_fifo` | `capital_gain.py` | `core/tax.py` |
| `replay_folio_scheme`, `hypothetical_redemption` | `capital_gain.py` | `core/tax.py` |
| `tax_for_matches`, `matches_to_df`, `classify_tax_category` | `capital_gain.py` | `core/tax.py` |
| `_cams_date_to_iso`, `_kfin_date_to_iso`, `raw_val`, `raw_num` | `data_manager.py` | `ingest/common.py` |
| `_clean_cols`, `_read_quoted_tsv`, `_read_csv_auto`, `_batch_id` | `data_manager.py` | `ingest/common.py` |
| `_resolve_columns`, `_cflex`, `_build_nominee` | `data_manager.py` | `ingest/common.py` |
| `_resolve_prev_nav_for_folios_impl` | `nav_service.py` | `core/nav.py` |
| `_parse_nav_text`, `_extract_nav_date_from_text`, `_get_file_nav_date` | `nav_service.py` | `core/nav.py` |
| `_count_isins_in_file`, `_read_snapshot_map` | `nav_service.py` | `core/nav.py` |
| `get_last_business_day` | `nav_service.py` | `core/nav.py` |
| `record_failure`, `read_failures`, `clear_log`, `get_log_path` | `sync_failure_log.py` | `core/failure_log.py` |
| `bump`, `current`, `set_db_path` | `data_manager.py` | `core/db.py` |
| `is_trading_day`, `get_expected_nav_date` | `trading_day_calender.py` | `core/calendar.py` |

### 3.2 Tier 2 — Pure computation, DB but no Streamlit

| Function | Current file | Target | Notes |
|---|---|---|---|
| `_fetch_cams`, `_fetch_kfin` | `xirr.py` | `core/xirr.py` | — |
| `compute_xirr_for_folio`, `compute_portfolio_xirr`, `compute_xirr_debug` | `xirr.py` | `core/xirr.py` | **Strip `@st.cache_data`** |
| `_load_cams_aum`, `_load_kfin_aum`, `_load_cams_txns`, `_load_kfin_txns` | `aum_recon.py` | `core/recon.py` | Already takes `conn` — good |
| `_fifo_units_cams`, `_reconcile`, `compute_recon`, `txn_detail` | `aum_recon.py` | `core/recon.py` | Extract `@st.cache_data` from `compute_recon` |
| `ensure_schema`, `set_inactive`, `restore`, `list_overrides` | `aum_recon.py` | `core/recon.py` | — |
| `send_report_email`, `get_client_email`, `credentials_configured`, `save_credentials` | `cams_mailback_sync.py` | `core/email.py` | — |
| `parse_bse_client_master`, `parse_bse_sip`, `parse_bse_scheme_master` | `data_manager.py` | `ingest/parsers_bse.py` | — |
| `parse_cams_wbr4_aum` … `parse_cams_wbr77_brokerage`, `_execute_cams_import` | `data_manager.py` | `ingest/parsers_cams.py` | — |
| `parse_kfin_mfsd203_aum` … `parse_kfin_mfsd205_brokerage`, `_execute_kfin_import` | `data_manager.py` | `ingest/parsers_kfin.py` | — |
| `parse_nav_file`, `parse_nav_filepath` | `data_manager.py` | `ingest/nav.py` | — |
| `ingest_nav_file_to_db`, `_parse_nav_file`, `_insert_nav_rows` | `nav_data_ingestion.py` | `ingest/nav.py` | — |
| `_do_download`, `sync_now`, `get_latest_file_path`, `has_todays_file` | `bse_auto.py` | `ingest/bse.py` | — |
| `_download_and_extract`, `_parse_and_move`, `parse_pending_files`, `sync_once` | `cams_mailback_sync.py` | `ingest/mailback.py` | **Fix duplicate `_parse_and_move`** |
| `get_pending_counts`, `get_done_counts` | `cams_mailback_sync.py` | `ingest/mailback.py` | — |
| `download_and_save_nav`, `download_and_save_nav_if_needed` | `nav_service.py` | `core/nav.py` | — |
| `download_business_day_nav`, `sync_previous_business_day_nav(_if_needed)` | `nav_service.py` | `core/nav.py` | — |
| `_save_nav_snapshot`, `_previous_snapshot_path`, `load_previous_nav_map` | `nav_service.py` | `core/nav.py` | — |
| `get_previous_nav_date`, `get_or_fetch_nav_for_date`, `get_nav_for_date_from_db` | `nav_service.py` | `core/nav.py` | — |
| `resolve_prev_nav_for_folios`, `diagnose_previous_day_coverage` | `nav_service.py` | `core/nav.py` | — |
| `AMFINavIndex`, `_amfi`, `fetch_nav_by_isin`, `fetch_amc_by_isin`, `load_nav_dataframe` | `nav_service.py` | `core/nav.py` | — |
| `task_mailback`, `task_nav_live`, `task_nav_previous`, `task_bse`, `run_all` | `background_worker.py` | `jobs/worker.py` | **Fix: add `task_bse` to loop** |

### 3.3 Tier 3 — Business logic trapped in `app.py`

| Function | Problem | Target |
|---|---|---|
| `get_cams_txns_raw`, `get_kfin_txns_raw` | 5 columns only; Clients tab bypasses them | Merge → `core/folios.py::get_folio_transactions` |
| `fetch_all_folio_transactions` | Re-implements loop with different columns | Merge → `get_folio_transactions` |
| `get_client_cams_schemes`, `get_client_kfin_schemes` | Two functions, same logic, split by RTA | `core/clients.py::get_client_schemes(client_code)` |
| `get_all_folios_with_isin_and_nav` | 16 columns, 5-layer merge | `core/holdings.py::get_all_folios_with_nav()` |
| `get_enriched_folio_nav_df` | Central structure, inline FIFO merge | `core/holdings.py::get_enriched_folios()` — split `_enrich_cams` / `_enrich_kfin` |
| `compute_client_holdings` | Duplicates Clients tab logic | `core/holdings.py::get_client_holdings(client_code)` |
| `get_cams_invested_per_scheme` | FIFO in app; also in `aum_recon`, also in `cache_functions` | `core/holdings.py::get_cams_invested(folio_list)` — **one impl** |
| `get_kfin_invested_per_scheme` | `SUM(td_amt)` — net cash flow, NOT FIFO | `core/holdings.py::get_kfin_invested(folio_list)` — decide FIFO vs net |
| `get_family_all_folios`, `get_family_folios_by_rta`, `get_family_members` | N+1 queries per member | `core/families.py::get_family_folios(family_id)` — single query |
| `get_family_all_folios` vs `compute_client_holdings` vs Clients tab vs Cap Gains tab | **4 different matching rules** for same client | `core/clients.py::get_client_folios(client_code)` — **one definition** |
| `get_folio_to_client_code_map` | Doesn't handle minor KFin folios | `core/clients.py::folio_to_client_map()` |
| `load_brokerage_report` | Huge SQL, hard-coded AMC fallback | `core/brokerage.py` |
| `_attribute_brokerage`, `compute_sub_broker_summary` | Business logic for brokerage split | `core/brokerage.py` |
| `calc_units_before`, `calc_units_upto`, `calc_invested_upto` | Valuation helpers | `core/valuation.py` |
| `get_client_xirr_batch` | Double-cached (outer `app`, inner `xirr`) | `ui/cached.py::client_xirr_batch_cached` |
| `_bse_scheme_lookup`, `get_isin_for_cams_product` | Shared lookups | `core/holdings.py` |
| `load_amc_breakdown_by_isin`, `load_active_amcs` | Dashboard loaders | `core/holdings.py` |
| `load_dashboard_summary`, `load_amc_breakdown`, `load_recent_uploads`, `load_db_stats`, `load_table_summary`, `load_dedup_sip_counts` | Dashboard loaders | `core/dashboard.py` |
| `ensure_family_tables`, `ensure_monthly_brokerage_schema`, `ensure_sub_broker_tables` | Schema setup | `core/db.py::ensure_schema()` |
| `search_clients` | Already pure | `core/clients.py` |
| `get_client_identity` | Already pure | `core/clients.py` |
| `load_all_clients_with_display` | Already pure | `core/clients.py` |

### 3.4 Tier 4 — Keep in `ui/`

These stay UI-coupled. Move to `ui/` only when splitting pages.

- All `render_*`: `render_client_selector`, `render_email_report_button`, `render_sub_brokers_ui`, `render_sub_broker_sharing_report`, `render_settings_ui`, `render_data_manager`, `render_aum_recon_tab`
- All PDF/HTML generators: `generate_capital_gain_pdf/html`, `generate_valuation_pdf/html`, `generate_brokerage_sharing_pdf/excel`, `generate_email_body`, `generate_brokerage_email_body`
- Formatting: `_find_font_path`, `_shorten_scheme_for_summary`, `_truncate_to_width`, `_txn_type_label`, `format_*`, `theme_plotly`, `clean_str`, `normalize_folio`
- Page blocks: `if mode == "📊 Dashboard":` … `elif mode == "⚙️ Admin Panel":` (8 blocks)

### 3.5 Tier 5 — Delete outright

| File | Why |
|---|---|
| `cache_functions.py` | **Unused** — nothing imports it. `_replay_kfin_folio_scheme_impl` has a broken `format=` kwarg to `float()`. |
| `nav_queries.py` | **Unused** — `app.py` uses `nav_service`, not `nav_queries`. Duplicate impl. |
| `email_tempate.py` | **Duplicated** — `generate_email_body` also in `app.py` (the one actually called). |
| Second `_parse_and_move` in `cams_mailback_sync.py` | Overrides the first (which logged failures). **Real bug** — mailback parse failures never reach `sync_failures.log`. |
| Second `_mark_seen_safe` in `cams_mailback_sync.py` | Identical copy. |

---

## 4. Dead Code & Duplicates

### 4.1 Dead modules

- `cache_functions.py` — no imports. Delete.
- `nav_queries.py` — no imports. Delete.
- `email_tempate.py` — no imports (typo name too). Delete.
- `trading_day_calender.py` — imported but never called? Verify, then move to `core/calendar.py` and decide keep/delete.

### 4.2 Duplicated definitions

| Thing | Locations | Fix |
|---|---|---|
| `_parse_and_move` | `cams_mailback_sync.py` × 2 | Keep first (with `sflog.record_failure`) |
| `_mark_seen_safe` | `cams_mailback_sync.py` × 2 | Keep one |
| `generate_email_body` | `app.py`, `email_tempate.py` | Keep `app.py`, delete module |
| `get_cams_invested_per_scheme` logic | `app.py`, `aum_recon._fifo_units_cams`, `cache_functions._replay_cams_folio_scheme_impl` | One impl in `core/holdings.py` |
| `get_conn` | `init_db.py`, `data_manager.py` (with different `DB_PATH`!) | One impl in `core/db.py` |
| Folio lookup by PAN/name | `app.py` × 6 (Clients tab, Families, Valuation, Cap Gains tab, compute_client_holdings, compute_client_holdings) | `core/clients.py::get_client_folios` |
| Transactions-for-folio SQL | `app.py` × 8 | `core/folios.py::get_folio_transactions` |

---

## 5. Known Bugs & Pitfalls

> **Fix these *during* the migration**, not after. The refactor is the natural moment.

### 5.1 Two different `DB_PATH` values

- `init_db.py`: `DB_PATH = "mfd_local.db"` (relative — cwd-dependent)
- `data_manager.py`: `DB_PATH = os.path.join(os.path.dirname(__file__), "mfd_local.db")` (absolute)

**Consequence:** If cwd ≠ project dir, `init_db.get_conn()` and `data_manager.get_conn()` open **different files**.

**Fix:** One `DB_PATH` in `core/db.py`. Delete `data_manager.get_conn` and `set_db_path`, or re-export from `core.db`.

**Verify:** `python -c "import init_db, data_manager; print(init_db.DB_PATH, data_manager.DB_PATH)"` should print the same resolved path.

### 5.2 `background_worker.run_all` never calls `task_bse`

```python
for name, fn in [
    ("mailback", task_mailback),
    ("nav_live", task_nav_live),
    ("nav_previous", task_nav_previous),
]:  # task_bse defined but not in loop
```

BSE scheme master only syncs when someone clicks the button.

**Fix:** Add `("bse", task_bse)` to the loop. The `has_todays_file()` guard inside `_do_download` makes it a no-op if already done today.

### 5.3 `get_kfin_invested_per_scheme` is NOT FIFO

`app.py`:
```python
COALESCE(SUM(td_amt), 0) AS invested_amount
```

This is net cash flow. For any folio that ever redeemed, it gives a wildly wrong "Invested". CAMS uses FIFO. The two are silently shown side by side.

**Fix:** Either implement FIFO for KFin using `capital_gain.replay_folio_scheme` (works on any DataFrame with the right columns), or document the difference and stop showing them as comparable.

### 5.4 `Match.is_ltcg` vs `Holding Days >= 365` disagree

`capital_gain.py`:
```python
anniversary = buy.replace(year=buy.year + 1)
return sell > anniversary  # calendar-correct
```

`app.py` (PDF generators, Cap Gains report):
```python
r.get("Holding Days") >= 365  # off-by-one on leap years
```

**Fix:** Pass `Match.is_ltcg` through to report generators. Never re-derive from `Holding Days`.

### 5.5 `xirr.compute_xirr_for_folio` imports Streamlit

`@st.cache_data(ttl=180)` on a `core/` function.

**Fix:** Strip decorator. Move to `ui/cached.py::folio_xirr_cached`.

### 5.6 `_parse_and_move` overwritten — mailback failures not logged

Two definitions in `cams_mailback_sync.py`. Second (no `sflog.record_failure`) wins. Every mailback parse failure is silently swallowed.

**Fix:** Delete second definition. Keep first.

### 5.7 `render_email_report_button` blocks UI on SMTP

Calls `send_report_email` synchronously inside `st.spinner`. A 5-email family batch freezes UI for 25–75 seconds.

**Fix:** Use `send_report_email_background` (already exists), or move to a thread in `core/email.py` and poll status from UI.

### 5.8 `get_folio_to_client_code_map` misses minor KFin folios

KFin's `kfin_mfsd211_folio` has only `pan_number` (no guardian column). Minor folios will never match a client.

**Fix:** Add name-based fallback. Document limitation.

### 5.9 `get_family_all_folios` and `get_family_folios_by_rta` are N+1

Loop `for _, member in members.iterrows()` with 2–4 SQL per member.

**Fix:** One query with `IN (member_codes)` (join on `bse_client_master`), then split by RTA in pandas.

### 5.10 `get_dedup_sip_counts` mixes different ID schemes

BSE uses `xsip_regn_no`, CAMS uses `request_ref_no`, KFin uses `reg_slno`. These are different identifiers — will never match.

**Fix:** Rename to `get_sip_counts_by_source`. Only dedupe within the same RTA.

### 5.11 `get_all_folios_with_isin_and_nav(force_reload=True)` creates new cache entry

`force_reload: bool` — no underscore, so it's part of the cache key. Calling with `True` and then `False` gives you the **stale** `False` entry.

**Fix:** `_force_reload: bool = False` (underscore).

### 5.12 `get_enriched_folio_nav_df` may raise `SettingWithCopyWarning`

```python
folio_nav_df = folio_nav_df.merge(...)  # new frame, OK
folio_nav_df.loc[has, 'file_aum'] = ...  # mutating — brittle
```

Works today because `.merge` returns a copy, but brittle.

**Fix:** Explicit `.copy()` after each merge.

### 5.13 `get_client_xirr_batch` double-caches

Outer `@st.cache_data(ttl=1800)` on `app.py::get_client_xirr_batch`, inner `@st.cache_data(ttl=180)` on `xirr.compute_xirr_for_folio`. The inner will return stale values if called directly within 3 min even when `current_value` changed.

**Fix:** Only the outer cache. Strip inner decorator.

### 5.14 `resolve_prev_nav_for_folios` returns tuple, not dict

Works because Streamlit serialises tuples, but fragile — a future change to return dict breaks silently.

**Fix:** Return a dataclass: `PreviousNavBaseline(date, isin_map, diagnostics)`.

### 5.15 `background_worker` double-bumps `data_version`

Each parser bumps, and `run_all` bumps again at the end. Harmless but noisy.

**Fix:** Only parsers bump. `run_all` doesn't need to.

### 5.16 `render_email_report_button` re-fetches even when creds missing

```python
client_email = mail_sync.get_client_email(client_code)  # always
creds_configured = mail_sync.credentials_configured()   # always
if not creds_configured:
    ...
    return False
```

**Fix:** Check creds first.

### 5.17 `aum_recon._fifo_units_cams` and `app.get_cams_invested_per_scheme` are duplicate logic

Both implement CAMS FIFO. Don't let them drift.

**Fix:** Extract one `core/holdings.py::_fifo_cams_folio_scheme(grp)`. Both call it.

---

## 6. Migration Plan

> **Rule:** Each phase is independently shippable. Run the app after each. Never leave main broken.

### Phase 0 — Delete dead code ☐

- [ ] Delete `cache_functions.py`
- [ ] Delete `nav_queries.py`
- [ ] Delete `email_tempate.py`
- [ ] Delete second `_parse_and_move` in `cams_mailback_sync.py` (keep first)
- [ ] Delete second `_mark_seen_safe` in `cams_mailback_sync.py`
- [ ] Run app. Verify mailback sync still works.

**Expected: ~800 lines gone, 2 real bugs fixed.**

### Phase 1 — Consolidate DB layer ☐

- [ ] Create `core/db.py` with `DB_PATH`, `get_conn`, `set_db_path`, `get_shared_read_conn`
- [ ] Move `bump`, `current` from `data_manager.py` → `core/db.py`
- [ ] Move `ensure_*_tables` from `app.py`, `aum_recon.py`, `init_db.py` → `core/db.py::ensure_schema()`
- [ ] Delete `data_manager.get_conn`, `data_manager.set_db_path`
- [ ] Replace all `from init_db import get_conn` → `from core.db import get_conn`
- [ ] Replace all `import data_manager; data_manager.get_conn` → `from core.db import get_conn`
- [ ] Run `python -c "import init_db, data_manager; print(init_db.DB_PATH, data_manager.DB_PATH)"` — must match
- [ ] Run app. Verify all pages load.

### Phase 2 — Extract `core/folios.py` ☐

- [ ] Create `core/folios.py`: `get_folio_rta`, `get_folio_transactions`, `get_folio_schemes`, `get_folio_invested`, `get_folio_units`
- [ ] Write `tests/test_folios.py` — at minimum: empty folio returns empty DF with columns; CAMS folio returns non-empty; KFin folio returns non-empty; direction/signed_units correct on redemption
- [ ] Add `ui/cached.py::folio_transactions_cached`
- [ ] Swap `app.py::get_cams_txns_raw` callers → `get_folio_transactions`
- [ ] Swap `app.py::get_kfin_txns_raw` callers → `get_folio_transactions`
- [ ] Swap `app.py::fetch_all_folio_transactions` → loop over `get_folio_transactions`
- [ ] Delete the three old functions
- [ ] Run app. Verify Clients tab, Transactions tab, Valuation report all show same data as before.

**Expected: ~8 SQL copies → 1 function.**

### Phase 3 — Extract `core/clients.py` ☐

- [ ] Move `search_clients`, `get_client_identity`, `load_all_clients_with_display` → `core/clients.py`
- [ ] Create `get_client_folios(client_code)` — **one** PAN/name resolution, returns `{"cams": [...], "kfin": [...]}`
- [ ] Create `get_client_schemes(client_code)` — merge CAMS + KFin
- [ ] Replace all 6 folio-lookup blocks in `app.py` → `get_client_folios`
- [ ] Run app. **Verify same client returns same folios on every tab.**

**Expected: same client, same folios on Clients / Families / Valuation / Cap Gains / Reports.**

### Phase 4 — Extract `core/holdings.py` ☐

- [ ] Move `get_all_folios_with_isin_and_nav` → `core/holdings.py::get_all_folios_with_nav`
  - [ ] Fix `_force_reload` param (underscore)
- [ ] Move `get_enriched_folio_nav_df` → `core/holdings.py::get_enriched_folios`
  - [ ] Split into `_enrich_cams(folio_nav_df)` / `_enrich_kfin(folio_nav_df)`
  - [ ] Add explicit `.copy()` after each merge
- [ ] Extract **one** `_fifo_cams_folio_scheme(grp)` used by both `get_cams_invested` and `aum_recon._fifo_units_cams`
- [ ] Move `get_cams_invested_per_scheme` → `core/holdings.py::get_cams_invested`
- [ ] Move `get_kfin_invested_per_scheme` → `core/holdings.py::get_kfin_invested`
  - [ ] **Decide:** FIFO or net cash flow? Document in docstring.
- [ ] Move `compute_client_holdings` → `core/holdings.py::get_client_holdings`
- [ ] Add `ui/cached.py` wrappers keyed on `(data_version(),)` for each
- [ ] Run app. Verify Dashboard, Clients, Families all show same totals.

**Expected: invested amounts and NAV-based AUM consistent everywhere.**

### Phase 5 — Extract `core/xirr.py` ☐

- [ ] Move `xirr.py` → `core/xirr.py`
- [ ] Strip `@st.cache_data` from `compute_xirr_for_folio`
- [ ] Fix KFin date parsing — use `txn_date_iso` when available, not `%d/%m/%Y` on `td_trdt`
- [ ] Add `ui/cached.py::folio_xirr_cached`, `portfolio_xirr_cached`
- [ ] Replace `app.py::get_client_xirr_batch` → thin wrapper
- [ ] Verify `background_worker.py` imports `core.xirr` without Streamlit shim (should work)

**Expected: XIRR consistent between UI and any script context.**

### Phase 6 — Extract `core/tax.py` ☐

- [ ] Move `capital_gain.py` → `core/tax.py`
- [ ] Fix `Match.is_ltcg` everywhere — pass through to `generate_capital_gain_pdf` and `_html`, don't re-derive from `Holding Days`
- [ ] Update PDF/HTML generators to consume `Match.is_ltcg` directly
- [ ] Verify PDF and on-screen figures agree on a leap-year boundary case

**Expected: STCG/LTCG split identical in PDF and on-screen.**

### Phase 7 — Extract `core/recon.py` ☐

- [ ] Move everything from `aum_recon.py` except `render_aum_recon_tab`, `_render_drilldown`, `_debug_fifo_detail` → `core/recon.py`
- [ ] Strip `@st.cache_data` from `compute_recon`
- [ ] Add `ui/cached.py::recon_cached` wrapper
- [ ] Keep `_debug_fifo_detail` in `ui/` (it uses `st.dataframe`)

### Phase 8 — Extract `ingest/` ☐

- [ ] `ingest/common.py` ← helpers from `data_manager.py`
- [ ] `ingest/parsers_bse.py` ← BSE parsers
- [ ] `ingest/parsers_cams.py` ← CAMS parsers
- [ ] `ingest/parsers_kfin.py` ← KFin parsers
- [ ] `ingest/nav.py` ← `nav_data_ingestion.py` + `parse_nav_file` from `data_manager.py`
- [ ] `ingest/mailback.py` ← `cams_mailback_sync.py` minus `render_settings_ui`, minus `send_report_email` (goes to `core/email.py`)
- [ ] `ingest/bse.py` ← `bse_auto.py` minus status dict (goes to `jobs/` or stays)
- [ ] Keep `ui/pages/settings.py::render_settings_ui`

### Phase 9 — Extract `core/brokerage.py` ☐

- [ ] Move `load_brokerage_report`, `_attribute_brokerage`, `compute_sub_broker_summary`
- [ ] Move sub-broker CRUD
- [ ] Move `get_available_brokerage_periods`

### Phase 10 — Split `app.py` into pages ☐

**Last.** Only after all helpers exist.

- [ ] Create `ui/app.py` router
- [ ] Move each `if mode == "..."` block to `ui/pages/*.py`
- [ ] Move report generators to `ui/reports/pdf.py`, `ui/reports/html.py`
- [ ] Move email templates to `ui/reports/email_templates.py`
- [ ] Move `ui/cached.py` wrappers alongside
- [ ] Verify theme is preserved (theme_patch watcher still works from router)

### Phase 11 — Fix remaining bugs ☐

- [ ] `background_worker.run_all`: add `("bse", task_bse)` to loop
- [ ] `render_email_report_button`: use background send
- [ ] `get_folio_to_client_code_map`: add name fallback for minor KFin folios
- [ ] `get_family_all_folios` / `get_family_folios_by_rta`: single-query rewrite
- [ ] `get_dedup_sip_counts`: rename → `get_sip_counts_by_source`
- [ ] `resolve_prev_nav_for_folios`: return dataclass
- [ ] `_parse_and_move`: ensure `sflog.record_failure` is called on every path
- [ ] `background_worker`: remove redundant `bump()` at end of `run_all`

---

## 7. Verification & Testing

### 7.1 Sanity commands

```bash
# Should print same resolved path twice
python -c "import core.db; print(core.db.DB_PATH)"

# Should NOT import streamlit
python -c "import sys; import core.folios; assert 'streamlit' not in sys.modules"

# Should NOT import streamlit
python -c "import sys; import core.xirr; assert 'streamlit' not in sys.modules"

# Should NOT import streamlit
python -c "import sys; import core.holdings; assert 'streamlit' not in sys.modules"

# Worker should still work without streamlit
python -c "import sys; import jobs.worker; assert 'streamlit' not in sys.modules"
```

### 7.2 Import boundary lint

Add to CI (or a pre-commit hook):

```python
# tools/check_import_boundaries.py
import ast, pathlib, sys

VIOLATIONS = []
LAYERS = {
    "core": {"forbidden": {"streamlit", "ui", "ingest"}},
    "ingest": {"forbidden": {"streamlit", "ui"}},
    "jobs": {"forbidden": {"streamlit", "ui"}},
}

for layer, rules in LAYERS.items():
    for path in pathlib.Path(layer).rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for n in node.names:
                    top = n.name.split(".")[0]
                    if top in rules["forbidden"]:
                        VIOLATIONS.append(f"{path}: imports {n.name}")
            elif isinstance(node, ast.ImportFrom):
                top = (node.module or "").split(".")[0]
                if top in rules["forbidden"]:
                    VIOLATIONS.append(f"{path}: from {node.module}")

if VIOLATIONS:
    print("\n".join(VIOLATIONS))
    sys.exit(1)
```

### 7.3 After each phase — manual smoke test

1. Load Dashboard — AUM cards populate
2. Pick a client with CAMS + KFin folios — Clients tab shows both
3. Open Transactions — filter by folio, dates work
4. Generate Valuation Report — PDF downloads
5. Generate Capital Gain Report — PDF downloads
6. AUM Reconciliation — Short/Excess counts stable
7. Admin → Sync Failures — loads without error
8. Background worker → Run Now — completes without error

### 7.4 Cross-tab consistency checks (add once `core/clients.py` exists)

Run this script manually after Phase 3:

```python
# tools/verify_folio_consistency.py
from core.clients import get_client_folios, get_client_identity
from core.db import get_conn

issues = []
with get_conn() as c:
    clients = c.execute("SELECT client_code FROM bse_client_master LIMIT 50").fetchall()

for (code,) in clients:
    ident = get_client_identity(code)
    if not ident:
        continue
    folios = get_client_folios(code)
    # Every folio must have a non-empty scheme list when we call get_folio_schemes
    from core.folios import get_folio_schemes
    for rta, flist in folios.items():
        for f in flist:
            schemes = get_folio_schemes(f)
            if not schemes:
                issues.append(f"{code} {rta} {f}: no schemes — likely wrong folio match")

print(f"{len(issues)} issues" if issues else "OK")
```

---

## Appendix A — Quick lookup

### Where is X?

| I want to… | Look in |
|---|---|
| Find where folio → transactions is defined | `core/folios.py::get_folio_transactions` (after Phase 2) |
| Find the one PAN-matching rule | `core/clients.py::get_client_folios` (after Phase 3) |
| Find where FIFO cost basis is computed | `core/holdings.py::get_cams_invested` (after Phase 4) |
| Find where STCG/LTCG is decided | `core/tax.py::Match.is_ltcg` |
| Find where the DB path is set | `core/db.py::DB_PATH` (after Phase 1) |
| Find the cache invalidation signal | `core/db.py::bump` / `current` — file `.data_version` |
| Find where NAV is fetched | `core/nav.py::download_and_save_nav` / `get_or_fetch_nav_for_date` |
| Find where mailback imports happen | `ingest/mailback.py::sync_once` |
| Find the worker tasks | `jobs/worker.py::run_all` |

### Files safe to delete after Phase 5

- `cache_functions.py`
- `nav_queries.py`
- `email_tempate.py`

### Files that become thin shims

- `init_db.py` → 10 lines: `from core.db import get_conn, DB_PATH; def init_db(): ...` (schema string stays)
- `data_manager.py` → imports from `ingest/parsers_*.py`, no own logic
- `nav_service.py` → `from core.nav import *`
- `aum_recon.py` → `from core.recon import *` + `render_aum_recon_tab` (or delete, move renderer to `ui/pages/recon.py`)

---

## Appendix B — Progress tracker

| Phase | Status | PR / Commit | Notes |
|---|---|---|---|
| 0 — Delete dead code | ☐ | — | — |
| 1 — Consolidate DB | ☐ | — | — |
| 2 — `core/folios.py` | ☐ | — | — |
| 3 — `core/clients.py` | ☐ | — | — |
| 4 — `core/holdings.py` | ☐ | — | — |
| 5 — `core/xirr.py` | ☐ | — | — |
| 6 — `core/tax.py` | ☐ | — | — |
| 7 — `core/recon.py` | ☐ | — | — |
| 8 — `ingest/` | ☐ | — | — |
| 9 — `core/brokerage.py` | ☐ | — | — |
| 10 — Split pages | ☐ | — | — |
| 11 — Fix remaining bugs | ☐ | — | — |

---

*End of reference.*
```

**Save this as `REFACTOR.md` in the project root.** A few notes on how to use it:

1. **Phase 0 is a 1-hour win** — delete the 3 dead files and the 2 duplicate functions. That alone fixes the "mailback failures don't get logged" bug (pitfall 5.6).

2. **Phase 2 is the one you asked about** — `get_folio_transactions(folio_no)` is spelled out completely in §2.1. Copy-paste it, verify with `python -c "from core.folios import get_folio_transactions; print(get_folio_transactions('SOME_FOLIO'))"`.

3. **Phase 3 kills the worst bug** — the same client returning different folios on different tabs is the kind of thing that erodes trust in the whole app. One `get_client_folios` fixes it permanently.

4. **The verification commands in §7.1 are CI-able** — once `core/` exists, `python -c "import sys; import core.folios; assert 'streamlit' not in sys.modules"` catches 90% of the layering mistakes immediately.

5. **The appendix A "Where is X?" table** is the most-used section long-term — add to it as you build, and it becomes your onboarding doc for whoever reads this code next.