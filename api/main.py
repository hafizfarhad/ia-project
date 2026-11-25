"""WalletWatch API."""
import logging
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from api import cases, ingest, monitor
from api.ml import Scorer
from api.security import ANY_ROLE, authenticate, issue_token
from common.config import settings
from common.db import close_pool, pool

logging.basicConfig(level=logging.INFO, format="%(asctime)s api %(levelname)s %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = settings()
    if len(s.jwt_secret) < 32 or len(s.ingest_token) < 16 or len(s.audit_hmac_key) < 32:
        raise RuntimeError("JWT_SECRET / INGEST_TOKEN / AUDIT_HMAC_KEY missing or too short — "
                           "run scripts/make_env.sh")
    pool()
    ingest.scorer = Scorer()
    yield
    close_pool()


app = FastAPI(title="WalletWatch API", version="1.0.0", lifespan=lifespan)
app.include_router(ingest.router)
app.include_router(cases.router)
app.include_router(monitor.router)


@app.middleware("http")
async def security_headers(request, call_next):
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Cache-Control"] = "no-store"
    return resp


class Login(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


@app.post("/auth/login", tags=["auth"])
def login(body: Login) -> dict:
    user = authenticate(body.username, body.password)
    if not user:
        raise HTTPException(401, "invalid username or password")   # same message either way
    token, ttl = issue_token(user)
    return {"access_token": token, "token_type": "bearer", "expires_in": ttl,
            "username": user["username"], "role": user["role"]}


@app.get("/auth/me", tags=["auth"])
def me(user: Annotated[dict, Depends(ANY_ROLE)]) -> dict:
    return user


@app.get("/health", tags=["ops"])
def health():
    try:
        with pool().connection(timeout=2) as conn:
            row = conn.execute(
                "SELECT current_user AS db_user, (SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema = 'public') AS tables").fetchone()
        return {"status": "ok", "db": "ok", "model": ingest.scorer.version if ingest.scorer else None, **row}
    except Exception as exc:
        return JSONResponse(status_code=503, content={"status": "degraded", "db": "error",
                                                      "error": type(exc).__name__})
