"""
reconcile_db.py — Bring an old Streamlit-era mfd_local.db up to the new schema.

Does three things:
  1. Compares every ORM-defined table against the DB.
  2. Creates any missing tables (users, job_runs, alembic_version will be
     handled by `alembic stamp head` after this runs).
  3. Alerts on any table where columns differ between ORM and DB.

By default it only REPORTS. Pass --apply to actually mutate.

Usage:
    python scripts/reconcile_db.py --old-db /path/to/old/mfd_local.db
    python scripts/reconcile_db.py --old-db /path/to/old/mfd_local.db --apply
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from pathlib import Path

# Add project root to sys.path so `app` is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import settings
from app.models import Base


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--old-db", required=True,
                    help="Path to the Streamlit-era mfd_local.db")
    ap.add_argument("--apply", action="store_true",
                    help="Actually copy and create tables (default: dry-run)")
    args = ap.parse_args()

    old_db = Path(args.old_db).expanduser().resolve()
    new_db = Path(settings.db_path).resolve()

    if not old_db.exists():
        sys.exit(f"❌ Old DB not found: {old_db}")
    if old_db == new_db:
        sys.exit("❌ Old DB and new DB are the same path. Aborting.")

    print(f"Old DB:  {old_db}")
    print(f"New DB:  {new_db}")
    print(f"Mode:    {'APPLY' if args.apply else 'DRY-RUN'}")
    print()

    # ── Backup new DB ──
    if args.apply and new_db.exists():
        backup = new_db.with_suffix(".db.pre-reconcile")
        shutil.copy2(new_db, backup)
        print(f"✅ Backed up existing new DB → {backup}")

    # ── Copy old DB into place ──
    if args.apply:
        shutil.copy2(old_db, new_db)
        print(f"✅ Copied {old_db.name} → {new_db}")

    # ── Inspect the DB (either the copy, or the old one in dry-run) ──
    inspect_path = new_db if args.apply else old_db
    conn = sqlite3.connect(str(inspect_path))

    existing_tables = {
        r[0]: r[1] for r in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    existing_views = {
        r[0]: r[1] for r in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='view'"
        ).fetchall()
    }
    print(f"\nExisting tables: {len(existing_tables)}")
    print(f"Existing views:  {len(existing_views)}")
    if existing_views:
        print(f"  views: {sorted(existing_views.keys())}")

    # ── Compare ORM tables against DB ──
    missing: list[str] = []
    drift: list[tuple[str, set[str], set[str]]] = []

    for name, table_obj in Base.metadata.tables.items():
        if name == "alembic_version":
            continue  # managed by alembic, not in ORM
        if name not in existing_tables:
            missing.append(name)
            continue
        cols_in_db = {r[1] for r in conn.execute(f"PRAGMA table_info({name})")}
        cols_in_orm = {c.name for c in table_obj.columns}
        if cols_in_db != cols_in_orm:
            drift.append((name, cols_in_db - cols_in_orm, cols_in_orm - cols_in_db))

    print(f"\nMissing ORM tables: {len(missing)}")
    for m in missing:
        print(f"  + {m}")

    print(f"\nTables with column drift: {len(drift)}")
    for name, only_db, only_orm in drift:
        print(f"  {name}:")
        if only_db:
            print(f"    only in DB   : {sorted(only_db)}")
        if only_orm:
            print(f"    only in ORM  : {sorted(only_orm)}  ← READ ERRORS LIKELY")

    # ── Apply fixes ──

    if args.apply:
        if missing:
            from app.core.db import engine
            print(f"\nCreating {len(missing)} missing tables...")
            Base.metadata.create_all(
                engine,
                tables=[Base.metadata.tables[n] for n in missing],
                checkfirst=True,
            )
            for name in missing:
                print(f"  created {name}")

        if drift:
            print(f"\n⚠️  Column drift detected on {len(drift)} table(s).")
            print("    Do NOT proceed until you review the output above.")
            print("    For a column that exists only in the DB — harmless (ignored).")
            print("    For a column that exists only in the ORM — add it manually or")
            print("    the ORM's SELECT will fail with 'no such column'.")

    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()