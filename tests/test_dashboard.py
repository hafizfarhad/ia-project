"""Render every dashboard page headlessly for each role (needs the stack running)."""
import os

import pytest
import requests
from streamlit.testing.v1 import AppTest

from tests.test_api_security import API, PW, _up

pytestmark = pytest.mark.skipif(not _up(), reason="API not running")
os.environ["API_URL"] = API

PAGES = {"analyst1": ["live_monitor", "alert_queue", "case_detail", "str_page", "metrics_page"],
         "mlro1": ["alert_queue", "case_detail", "str_page", "metrics_page"],
         "auditor1": ["case_detail", "metrics_page", "audit_page"]}


def _run(user: str, view: str, case_id: int | None) -> AppTest:
    from common.config import settings
    settings.cache_clear()
    at = AppTest.from_string(f"from dashboard import views\nviews.{view}()", default_timeout=60)
    r = requests.post(f"{API}/auth/login", json={"username": user, "password": PW}, timeout=10).json()
    at.session_state["token"] = r["access_token"]
    at.session_state["username"] = r["username"]
    at.session_state["role"] = r["role"]
    if case_id:
        at.session_state["case_id"] = case_id
    return at.run()


@pytest.fixture(scope="module")
def case_id():
    tok = requests.post(f"{API}/auth/login", json={"username": "analyst1", "password": PW}, timeout=10).json()
    cases = requests.get(f"{API}/cases", headers={"Authorization": f"Bearer {tok['access_token']}"}, timeout=10).json()
    return cases[0]["case_id"] if cases else None


@pytest.mark.parametrize("user,view", [(u, v) for u, vs in PAGES.items() for v in vs])
def test_page_renders(user, view, case_id):
    at = _run(user, view, case_id)
    errs = [e.value for e in at.exception] + [e.value for e in at.error]
    assert not errs, errs


def test_login_page_shows_form():
    at = AppTest.from_file("dashboard/app.py", default_timeout=30).run()
    assert len(at.text_input) == 2 and not at.exception
