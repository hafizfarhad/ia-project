"""Database-level IA controls. Needs the compose stack running (postgres on localhost:5432).

Run:  .venv/bin/pytest tests/test_db_security.py
"""
import os
from pathlib import Path

import psycopg
import pytest
from psycopg import errors


def _env() -> dict:
    env = dict(os.environ)
    p = Path(__file__).resolve().parent.parent / ".env"
    if p.exists():
        for line in p.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env.setdefault(k, v)
    return env


ENV = _env()


def connect(user: str, pw_key: str) -> psycopg.Connection:
    try:
        return psycopg.connect(host="localhost", port=5432, dbname="walletwatch",
                               user=user, password=ENV[pw_key], connect_timeout=3,
                               autocommit=True)
    except psycopg.OperationalError:
        pytest.skip("postgres not reachable — run `docker compose up` first")


@pytest.fixture
def app():
    with connect("aml_app", "APP_DB_PASSWORD") as c:
        yield c


@pytest.fixture
def admin():
    with connect("postgres", "POSTGRES_PASSWORD") as c:
        yield c


def test_app_cannot_read_ground_truth(app):
    with pytest.raises(errors.InsufficientPrivilege):
        app.execute("SELECT * FROM ground_truth")


def test_app_cannot_delete(app):
    with pytest.raises(errors.InsufficientPrivilege):
        app.execute("DELETE FROM alerts")


def test_audit_log_append_only_for_app(app):
    # Rolled back so no fake entry ends up breaking the real hash chain.
    with app.transaction(force_rollback=True):
        app.execute("INSERT INTO audit_log (actor, action, prev_hash, hash) "
                    "VALUES ('test', 'TEST', repeat('0',64), repeat('1',64))")
        with pytest.raises(errors.InsufficientPrivilege):
            with app.transaction():
                app.execute("UPDATE audit_log SET actor = 'evil'")


def test_audit_log_trigger_blocks_even_superuser(admin):
    # Row triggers only fire when a row exists, so insert one (rolled back afterwards).
    with admin.transaction(force_rollback=True):
        admin.execute("INSERT INTO audit_log (actor, action, prev_hash, hash) "
                      "VALUES ('test', 'TEST', repeat('0',64), repeat('1',64))")
        for stmt in ("UPDATE audit_log SET actor = 'evil'",
                     "DELETE FROM audit_log",
                     "TRUNCATE audit_log"):
            with pytest.raises(errors.RaiseException):
                with admin.transaction():
                    admin.execute(stmt)


def test_maker_checker_constraint(admin):
    with admin.transaction(force_rollback=True):
        uid = admin.execute("INSERT INTO users (username, password_hash, role) "
                            "VALUES ('mc_test', 'x', 'mlro') RETURNING user_id").fetchone()[0]
        admin.execute("INSERT INTO accounts (account_id, kind, name, opened_at) "
                      "VALUES ('MC-ACC', 'customer', 'x', now())")
        cid = admin.execute("INSERT INTO cases (account_id) VALUES ('MC-ACC') "
                            "RETURNING case_id").fetchone()[0]
        with pytest.raises(errors.CheckViolation):
            with admin.transaction():
                admin.execute("INSERT INTO strs (case_id, narrative, drafted_by, reviewed_by) "
                              "VALUES (%s, 'n', %s, %s)", (cid, uid, uid))


def test_false_positive_needs_reason(admin):
    with admin.transaction(force_rollback=True):
        admin.execute("INSERT INTO accounts (account_id, kind, name, opened_at) "
                      "VALUES ('FP-ACC', 'customer', 'x', now())")
        with pytest.raises(errors.CheckViolation):
            with admin.transaction():
                admin.execute("INSERT INTO cases (account_id, status, disposition_reason) "
                              "VALUES ('FP-ACC', 'CLOSED_FALSE_POSITIVE', 'too short')")


def test_one_active_case_per_account(admin):
    with admin.transaction(force_rollback=True):
        admin.execute("INSERT INTO accounts (account_id, kind, name, opened_at) "
                      "VALUES ('DUP-ACC', 'customer', 'x', now())")
        admin.execute("INSERT INTO cases (account_id) VALUES ('DUP-ACC')")
        with pytest.raises(errors.UniqueViolation):
            with admin.transaction():
                admin.execute("INSERT INTO cases (account_id) VALUES ('DUP-ACC')")
