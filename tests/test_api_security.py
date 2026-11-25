"""End-to-end IA controls through the real API. Needs the compose stack running.

    .venv/bin/pytest tests/test_api_security.py
"""
from datetime import UTC, datetime

import psycopg
import pytest
import requests
from psycopg.rows import dict_row

from common.alerts import create_alert
from tests.test_db_security import ENV

API = "http://localhost:8000"
PW = ENV.get("DEMO_PASSWORD", "")
ACC = "ZZTEST-0001"


def _up() -> bool:
    try:
        return requests.get(f"{API}/health", timeout=2).ok
    except requests.RequestException:
        return False


pytestmark = pytest.mark.skipif(not _up(), reason="API not running — `docker compose up` first")


def login(user: str) -> dict:
    r = requests.post(f"{API}/auth/login", json={"username": user, "password": PW}, timeout=10)
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture(scope="module")
def admin():
    with psycopg.connect(host="localhost", dbname="walletwatch", user="postgres",
                         password=ENV["POSTGRES_PASSWORD"], row_factory=dict_row, autocommit=True) as c:
        yield c


@pytest.fixture(scope="module")
def case_id(admin):
    """A throwaway case on a test account, removed afterwards (audit entries intentionally stay)."""
    admin.execute("INSERT INTO accounts (account_id, kind, name, cnic, msisdn, tier, city, device_id, opened_at) "
                  "VALUES (%s, 'customer', 'Test Subject', '35202-7654321-9', '0300-7654321', 'L1', 'Lahore', "
                  "'DTEST', now()) ON CONFLICT DO NOTHING", (ACC,))
    with admin.transaction():
        create_alert(admin, account_id=ACC, scenario="S1", score=0.7, sim_ts=datetime.now(UTC),
                     evidence={"rule": "test", "txns": []})
    cid = admin.execute("SELECT case_id FROM cases WHERE account_id=%s AND status='OPEN'", (ACC,)).fetchone()["case_id"]
    yield cid
    admin.execute("DELETE FROM strs WHERE case_id IN (SELECT case_id FROM cases WHERE account_id=%s)", (ACC,))
    admin.execute("DELETE FROM alerts WHERE account_id=%s", (ACC,))
    admin.execute("DELETE FROM cases WHERE account_id=%s", (ACC,))
    admin.execute("DELETE FROM accounts WHERE account_id=%s", (ACC,))


# ------------------------------------------------------------------ authentication
def test_bad_password_and_unknown_user_look_the_same():
    a = requests.post(f"{API}/auth/login", json={"username": "analyst1", "password": "wrong"}, timeout=10)
    b = requests.post(f"{API}/auth/login", json={"username": "nobody", "password": "wrong"}, timeout=10)
    assert a.status_code == b.status_code == 401 and a.json() == b.json()


def test_no_or_forged_token_rejected():
    assert requests.get(f"{API}/cases", timeout=10).status_code == 401
    forged = {"Authorization": "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJtbHJvMSIsInJvbGUiOiJtbHJvIn0.x"}
    assert requests.get(f"{API}/cases", headers=forged, timeout=10).status_code == 401


def test_ingest_needs_service_token():
    assert requests.post(f"{API}/ingest/batch", json={"txns": []}, timeout=10).status_code == 401
    user_token = login("mlro1")      # a user token is not an ingest token
    assert requests.post(f"{API}/ingest/batch", json={"txns": []}, headers=user_token, timeout=10).status_code == 401


# ------------------------------------------------------------------ RBAC
def test_rbac_matrix(case_id):
    an, ml, au = login("analyst1"), login("mlro1"), login("auditor1")
    assert requests.get(f"{API}/audit", headers=an, timeout=10).status_code == 403
    assert requests.get(f"{API}/audit", headers=au, timeout=10).status_code == 200
    assert requests.post(f"{API}/cases/{case_id}/escalate", json={"reason": "x" * 25}, headers=au,
                         timeout=10).status_code == 403
    assert requests.post(f"{API}/pii/unmask", json={"account_id": ACC, "reason": "investigation"}, headers=an,
                         timeout=10).status_code == 403
    assert requests.post(f"{API}/pii/unmask", json={"account_id": ACC, "reason": "investigation"}, headers=ml,
                         timeout=10).json()["cnic"] == "35202-7654321-9"


def test_pii_masked_in_case_view(case_id):
    d = requests.get(f"{API}/cases/{case_id}", headers=login("analyst1"), timeout=10).json()
    assert d["account"]["cnic"] == "35202-*******-9" and d["account"]["msisdn"] == "0300-***4321"
    assert all(a["integrity_ok"] for a in d["alerts"])


# ------------------------------------------------------------------ workflow + maker-checker
def test_case_workflow_and_maker_checker(case_id):
    an, ml1, ml2 = login("analyst1"), login("mlro1"), login("mlro2")
    assert requests.post(f"{API}/cases/{case_id}/close", json={"reason": "short"}, headers=an,
                         timeout=10).status_code == 422          # reason too short
    assert requests.post(f"{API}/cases/{case_id}/escalate", json={"reason": "Repeated near-limit cash-ins"},
                         headers=an, timeout=10).status_code == 200
    r = requests.post(f"{API}/cases/{case_id}/str", json={"narrative": "Subject received repeated cash-ins just "
                      "under the tier limit at different agents, inconsistent with profile."}, headers=ml1, timeout=10)
    assert r.status_code == 200
    sid = r.json()["str_id"]
    # mlro1 drafted it, so mlro1 may not approve it
    approve = f"{API}/strs/{sid}/approve"
    assert requests.post(approve, json={"comment": "ok"}, headers=ml1, timeout=10).status_code == 403
    assert requests.post(f"{API}/strs/{sid}/approve", json={"comment": "ok"}, headers=an, timeout=10).status_code == 403
    assert requests.post(f"{API}/strs/{sid}/approve", json={"comment": "agreed"}, headers=ml2,
                         timeout=10).json()["status"] == "FILED"
    assert "STR-" in requests.get(f"{API}/strs/{sid}/export", headers=ml2, timeout=10).text


# ------------------------------------------------------------------ audit trail
def test_actions_are_audited(admin, case_id):
    actions = {r["action"] for r in admin.execute(
        "SELECT action FROM audit_log WHERE target IN (%s, %s) OR action IN ('ACCESS_DENIED','LOGIN_FAILED')",
        (f"case:{case_id}", f"account:{ACC}")).fetchall()}
    assert {"CASE_VIEW", "CASE_ESCALATE", "PII_UNMASK", "ACCESS_DENIED", "LOGIN_FAILED"} <= actions


def test_tampering_is_detected(admin):
    au = login("auditor1")
    assert requests.get(f"{API}/audit/verify", headers=au, timeout=30).json()["ok"] is True
    victim = admin.execute("SELECT entry_id, actor FROM audit_log ORDER BY entry_id LIMIT 1 OFFSET 2").fetchone()
    # An insider with superuser rights disables the trigger and rewrites history...
    admin.execute("ALTER TABLE audit_log DISABLE TRIGGER audit_log_no_update")
    try:
        admin.execute("UPDATE audit_log SET actor='someone_else' WHERE entry_id=%s", (victim["entry_id"],))
        r = requests.get(f"{API}/audit/verify", headers=au, timeout=30).json()
        assert r["ok"] is False and r["broken_at"] == victim["entry_id"]
    finally:
        admin.execute("UPDATE audit_log SET actor=%s WHERE entry_id=%s", (victim["actor"], victim["entry_id"]))
        admin.execute("ALTER TABLE audit_log ENABLE TRIGGER audit_log_no_update")
    assert requests.get(f"{API}/audit/verify", headers=au, timeout=30).json()["ok"] is True


def test_rewritten_chain_without_key_is_detected(admin):
    """A DB superuser edits an entry AND recomputes every later link. Without the API's HMAC
    key (they try plain SHA-256), verification still fails at the edited entry.

    Verified in-process with the API's own code and key: calling /audit/verify over HTTP here
    would append a genuine entry onto the forged chain and corrupt the real log."""
    import hashlib
    import os

    from api import audit
    from common.config import settings
    from common.integrity import canonical

    os.environ["AUDIT_HMAC_KEY"] = ENV["AUDIT_HMAC_KEY"]
    settings.cache_clear()

    def verify():
        with admin.transaction():
            return audit.verify(admin)

    assert verify()["ok"] is True
    rows = admin.execute("SELECT * FROM audit_log ORDER BY entry_id").fetchall()
    victim = rows[2]
    admin.execute("ALTER TABLE audit_log DISABLE TRIGGER audit_log_no_update")
    try:
        prev = rows[1]["hash"]
        for r in rows[2:]:
            actor = "someone_else" if r["entry_id"] == victim["entry_id"] else r["actor"]
            msg = "|".join([prev, r["ts"].isoformat(timespec="microseconds"), actor, r["action"],
                            r["target"] or "", canonical(r["details"])])
            h = hashlib.sha256(msg.encode()).hexdigest()
            admin.execute("UPDATE audit_log SET actor=%s, prev_hash=%s, hash=%s WHERE entry_id=%s",
                          (actor, prev, h, r["entry_id"]))
            prev = h
        res = verify()
        assert res["ok"] is False and res["broken_at"] == victim["entry_id"]
    finally:
        for r in rows[2:]:
            admin.execute("UPDATE audit_log SET actor=%s, prev_hash=%s, hash=%s WHERE entry_id=%s",
                          (r["actor"], r["prev_hash"], r["hash"], r["entry_id"]))
        admin.execute("ALTER TABLE audit_log ENABLE TRIGGER audit_log_no_update")
    assert verify()["ok"] is True
    au = login("auditor1")
    assert requests.get(f"{API}/audit/verify", headers=au, timeout=60).json()["ok"] is True
