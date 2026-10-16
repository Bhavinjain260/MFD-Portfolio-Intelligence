"""
seed.py — CLI to create the initial admin user.

Usage:
    python -m app.core.seed                       # uses settings defaults
    python -m app.core.seed --user X --password Y
"""
from __future__ import annotations

import argparse

from sqlalchemy import text

from app.core.config import settings
from app.core.db import SessionLocal
from app.core.security import hash_password


def upsert_user(username: str, password: str, is_admin: bool = True) -> None:
    pw_hash = hash_password(password)
    with SessionLocal() as s:
        s.execute(
            text("""
                INSERT INTO users (username, password_hash, is_admin)
                VALUES (:u, :h, :a)
                ON CONFLICT(username) DO UPDATE SET
                    password_hash = excluded.password_hash,
                    is_admin = excluded.is_admin
            """),
            {"u": username, "h": pw_hash, "a": 1 if is_admin else 0},
        )
        s.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", default=settings.admin_username)
    ap.add_argument("--password", default=settings.admin_password)
    ap.add_argument("--non-admin", action="store_true")
    args = ap.parse_args()

    upsert_user(args.user, args.password, is_admin=not args.non_admin)
    print(f"✅ User '{args.user}' seeded (admin={not args.non_admin})")


if __name__ == "__main__":
    main()