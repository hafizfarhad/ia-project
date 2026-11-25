"""Authentication (bcrypt + JWT), RBAC dependencies, and the simulator's ingest token."""
from __future__ import annotations

import hmac
from datetime import UTC, datetime, timedelta
from typing import Annotated

import bcrypt
import jwt
from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from api import audit
from common.config import settings
from common.db import pool

ALGO = "HS256"
_bearer = HTTPBearer(auto_error=False)
# Verified against when the username doesn't exist, so response time doesn't reveal valid usernames.
_DUMMY_HASH = bcrypt.hashpw(b"not-a-real-password", bcrypt.gensalt()).decode()


def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt(rounds=12)).decode()


def check_password(pw: str, hashed: str) -> bool:
    return bcrypt.checkpw(pw.encode(), hashed.encode())


def issue_token(user: dict) -> tuple[str, int]:
    s = settings()
    exp = datetime.now(UTC) + timedelta(minutes=s.jwt_minutes)
    token = jwt.encode({"sub": user["username"], "uid": user["user_id"], "role": user["role"],
                        "exp": exp, "iat": datetime.now(UTC)}, s.jwt_secret, algorithm=ALGO)
    return token, s.jwt_minutes * 60


def authenticate(username: str, password: str) -> dict | None:
    with pool().connection() as conn:
        user = conn.execute("SELECT user_id, username, password_hash, role, is_active FROM users "
                            "WHERE username=%s", (username,)).fetchone()
        ok = check_password(password, user["password_hash"] if user else _DUMMY_HASH)
        ok = ok and user is not None and user["is_active"]
        with conn.transaction():
            audit.record(conn, username[:64], "LOGIN_SUCCESS" if ok else "LOGIN_FAILED")
    return user if ok else None


def current_user(creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]) -> dict:
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token",
                            headers={"WWW-Authenticate": "Bearer"})
    try:
        claims = jwt.decode(creds.credentials, settings().jwt_secret, algorithms=[ALGO],
                            options={"require": ["exp", "sub", "role"]})
    except jwt.PyJWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or expired token",
                            headers={"WWW-Authenticate": "Bearer"}) from exc
    with pool().connection() as conn:
        row = conn.execute("SELECT user_id, username, role, is_active FROM users WHERE username=%s",
                           (claims["sub"],)).fetchone()
    # Role comes from the DB, not the token, so a demoted/disabled user loses access immediately.
    if not row or not row["is_active"]:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "user disabled")
    return dict(row)


def require(*roles: str):
    """RBAC dependency. Denials are audited."""
    def dep(request: Request, user: Annotated[dict, Depends(current_user)]) -> dict:
        if user["role"] not in roles:
            with pool().connection() as conn, conn.transaction():
                audit.record(conn, user["username"], "ACCESS_DENIED", request.url.path,
                             {"method": request.method, "role": user["role"], "needed": list(roles)})
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"requires role: {', '.join(roles)}")
        return user
    return dep


ANY_ROLE = require("analyst", "mlro", "auditor")
WORKER = require("analyst", "mlro")
MLRO = require("mlro")
AUDITOR = require("auditor")


def ingest_token(x_ingest_token: Annotated[str | None, Header()] = None) -> None:
    expected = settings().ingest_token
    if not expected or not x_ingest_token or not hmac.compare_digest(x_ingest_token, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad ingest token")
