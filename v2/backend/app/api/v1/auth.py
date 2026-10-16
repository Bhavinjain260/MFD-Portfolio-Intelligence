from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import text

from app.api.deps import current_user
from app.core.db import SessionLocal
from app.core.security import (
    create_access_token, get_user, record_login, verify_password,
)
from app.schemas.auth import TokenResponse, UserMe

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse)
def login(form: OAuth2PasswordRequestForm = Depends()):
    user = get_user(form.username)
    if not user or not verify_password(form.password, user["password_hash"]):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials")
    record_login(user["username"])
    return TokenResponse(access_token=create_access_token(user["username"]))


@router.get("/me", response_model=UserMe)
def me(user: dict = Depends(current_user)):
    with SessionLocal() as s:
        row = s.execute(
            text("SELECT id, username, is_admin, last_login "
                 "FROM users WHERE username = :u"),
            {"u": user["username"]},
        ).fetchone()
    if not row:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found")

    last_login = row[3]
    if last_login is not None and not hasattr(last_login, "year"):
        try:
            from datetime import datetime
            last_login = datetime.fromisoformat(str(last_login))
        except Exception:
            last_login = None

    return UserMe(
        id=row[0],
        username=row[1],
        is_admin=bool(row[2]),
        last_login=last_login,
    )