"""Ingest → rules → alerts → cases, end to end through the API (needs the stack running).

Test transactions are dated before the simulation starts so they never fall inside the
graph worker's or evaluator's live windows, and everything is deleted afterwards.
"""
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
import requests
from psycopg.rows import dict_row

from tests.test_api_security import API, _up
from tests.test_db_security import ENV

pytestmark = pytest.mark.skipif(not _up(), reason="API not running")
T0 = datetime(2026, 8, 1, 10, tzinfo=UTC)
HDR = {"X-Ingest-Token": ENV.get("INGEST_TOKEN", "")}


@pytest.fixture(scope="module")
def admin():
    with psycopg.connect(host="localhost", dbname="walletwatch", user="postgres",
                         password=ENV["POSTGRES_PASSWORD"], row_factory=dict_row, autocommit=True) as c:
        ids = [f"ZZING-{i:02d}" for i in range(20)]
        for i, a in enumerate(ids):
            c.execute("INSERT INTO accounts (account_id, kind, name, tier, city, device_id, opened_at) "
                      "VALUES (%s, 'customer', 'Ingest Test', 'L2', 'Lahore', %s, %s) ON CONFLICT DO NOTHING",
                      (a, f"DZZ{i:02d}", T0 - timedelta(days=400)))
        yield c
        like = "ZZING-%"
        c.execute("DELETE FROM strs WHERE case_id IN (SELECT case_id FROM cases WHERE account_id LIKE %s)", (like,))
        c.execute("DELETE FROM alerts WHERE account_id LIKE %s", (like,))
        c.execute("DELETE FROM cases WHERE account_id LIKE %s", (like,))
        c.execute("DELETE FROM ml_scores WHERE txn_id LIKE 'ZZT%%'")
        c.execute("DELETE FROM transactions WHERE txn_id LIKE 'ZZT%%'")
        c.execute("DELETE FROM accounts WHERE account_id LIKE %s", (like,))


def tx(n, minutes, s, r, amt, typ="P2P"):
    return {"txn_id": f"ZZT{n:05d}", "ts": (T0 + timedelta(minutes=minutes)).isoformat(), "txn_type": typ,
            "sender_id": s, "receiver_id": r, "amount": amt, "device_id": None}


def post(txns):
    r = requests.post(f"{API}/ingest/batch", json={"txns": txns}, headers=HDR, timeout=30)
    assert r.status_code == 200, r.text
    return r.json()


def test_fan_in_fan_out_raises_s2_and_opens_case(admin):
    mule = "ZZING-00"
    batch = [tx(i, i * 10, f"ZZING-{i:02d}", mule, 9000) for i in range(1, 10)]           # 9 senders in
    batch += [tx(100 + i, 120 + i * 5, mule, f"ZZING-{10 + i:02d}", 13000) for i in range(6)]  # 6 out, 78k of 81k
    res = post(batch)
    assert res["stored"] == len(batch)
    a = admin.execute("SELECT * FROM alerts WHERE account_id=%s AND scenario='S2'", (mule,)).fetchone()
    assert a and a["case_id"] and a["evidence"]["distinct_senders"] == 9
    # replaying the same batch is idempotent
    assert post(batch) == {"stored": 0, "duplicates": len(batch), "alerts": 0}


def test_pass_through_raises_s3(admin):
    acct = "ZZING-19"
    batch = []
    for k in range(2):                      # twice: big credit, 95% out within 30 min
        base = 2000 + k * 600
        batch.append(tx(500 + k * 2, base, "ZZING-18", acct, 100000))
        batch.append(tx(501 + k * 2, base + 30, acct, "ZZING-17", 95000))
    post(batch)
    assert admin.execute("SELECT 1 FROM alerts WHERE account_id=%s AND scenario='S3'", (acct,)).fetchone()


def test_ingest_rejects_bad_input():
    bad = tx(900, 0, "ZZING-01", "ZZING-02", -5)
    r = requests.post(f"{API}/ingest/batch", json={"txns": [bad]}, headers=HDR, timeout=10)
    assert r.status_code == 422
    bad = dict(tx(901, 0, "ZZING-01", "ZZING-02", 5), txn_id="x'; DROP TABLE transactions;--")
    r = requests.post(f"{API}/ingest/batch", json={"txns": [bad]}, headers=HDR, timeout=10)
    assert r.status_code == 422
