"""Thin API client. The JWT lives only in the server-side Streamlit session."""
from __future__ import annotations

import requests
import streamlit as st

from common.config import settings


class ApiError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def _url(path: str) -> str:
    return f"{settings().api_url}{path}"


def _headers() -> dict:
    tok = st.session_state.get("token")
    return {"Authorization": f"Bearer {tok}"} if tok else {}


def _handle(r: requests.Response):
    if r.status_code == 401 and st.session_state.get("token"):
        st.session_state.clear()          # expired / revoked: force re-login
        st.rerun()
    if not r.ok:
        try:
            detail = r.json().get("detail", r.text)
        except ValueError:
            detail = r.text
        if isinstance(detail, list):      # pydantic validation errors
            detail = "; ".join(d.get("msg", str(d)) for d in detail)
        raise ApiError(r.status_code, str(detail))
    ctype = r.headers.get("content-type", "")
    return r.json() if "json" in ctype else r.text


def get(path: str, **params):
    return _handle(requests.get(_url(path), headers=_headers(), params=params, timeout=15))


def post(path: str, body: dict | None = None):
    return _handle(requests.post(_url(path), headers=_headers(), json=body or {}, timeout=15))


def login(username: str, password: str) -> dict:
    r = requests.post(_url("/auth/login"), json={"username": username, "password": password}, timeout=15)
    return _handle(r)
