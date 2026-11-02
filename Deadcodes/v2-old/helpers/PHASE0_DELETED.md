# Phase 0 — Dead code to delete from the live project

When applying this refactor onto the real repo (not just this scaffold), delete:

| File | Reason |
|------|--------|
| `cache_functions.py` | Unused; broken `format=` kwarg |
| `nav_queries.py` | Unused; app uses `nav_service` / now `core.nav` |
| `email_tempate.py` | Typo name; duplicate of email body helper |

In `cams_mailback_sync.py`:

1. Keep the **first** `_parse_and_move` (the one that calls `sflog.record_failure`).
2. Delete the **second** `_parse_and_move` that overrides it.
3. Keep one `_mark_seen_safe`; delete the identical duplicate.

These deletions fix the “mailback parse failures never reach sync_failures.log” bug.

In this scaffold the dead modules were never copied into `artifacts/` — they remain only under `attachments/` for reference.
