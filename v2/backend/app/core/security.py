from __future__ import annotations

from datetime import datetime, timedelta, timezone

from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy import text

from app.core.config import settings
from app.core.db import SessionLocal

_pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=12)


def hash_password(plain: str) -> str:
    return _pwd_ctx.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return _pwd_ctx.verify(plain, hashed)
    except Exception:
        return False


def create_access_token(sub: str, expires_min: int | None = None) -> str:
    exp = datetime.now(timezone.utc) + timedelta(minutes=expires_min or settings.jwt_expires_minutes)
    payload = {"sub": sub, "exp": exp, "iat": datetime.now(timezone.utc)}
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except JWTError:
        return None


def get_user(username: str) -> dict | None:
    with SessionLocal() as s:
        row = s.execute(
            text("SELECT id, username, password_hash, is_admin FROM users WHERE username = :u"),
            {"u": username},
        ).fetchone()
    if not row:
        return None
    return {"id": row[0], "username": row[1], "password_hash": row[2], "is_admin": bool(row[3])}


def record_login(username: str) -> None:
    with SessionLocal() as s:
        s.execute(text("UPDATE users SET last_login = CURRENT_TIMESTAMP WHERE username = :u"),
                  {"u": username})
        s.commit()