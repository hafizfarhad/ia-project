"""Alert creation and case linking. Used by the API (rules, ML) and the graph worker."""
from __future__ import annotations

from datetime import datetime, timedelta

from psycopg import Connection

from common.config import load_yaml
from common.integrity import evidence_hash, normalise

ACTIVE = ("OPEN", "ESCALATED", "STR_PENDING")


def recent_alert_exists(conn: Connection, account_id: str, scenario: str,
                        since: datetime) -> bool:
    return conn.execute(
        "SELECT 1 FROM alerts WHERE account_id=%s AND scenario=%s AND sim_ts > %s LIMIT 1",
        (account_id, scenario, since)).fetchone() is not None


def signature_seen(conn: Connection, scenario: str, account_id: str, signature: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM alerts WHERE scenario=%s AND account_id=%s AND evidence->>'signature'=%s LIMIT 1",
        (scenario, account_id, signature)).fetchone() is not None


def create_alert(conn: Connection, *, account_id: str, scenario: str, score: float,
                 sim_ts: datetime, evidence: dict) -> int:
    """Freeze evidence + hash, attach to the account's active case (creating it if needed),
    and refresh the case priority. Returns alert_id."""
    evidence = normalise({"scenario": scenario, "account_id": account_id,
                          "sim_ts": sim_ts, **evidence})
    h = evidence_hash(evidence)

    row = conn.execute(
        "INSERT INTO cases (account_id, priority) VALUES (%s, %s) "
        "ON CONFLICT (account_id) WHERE status IN ('OPEN','ESCALATED','STR_PENDING') DO NOTHING "
        "RETURNING case_id", (account_id, score)).fetchone()
    if row is None:
        row = conn.execute("SELECT case_id FROM cases WHERE account_id=%s AND status = ANY(%s)",
                           (account_id, list(ACTIVE))).fetchone()
    case_id = row["case_id"] if isinstance(row, dict) else row[0]

    alert = conn.execute(
        "INSERT INTO alerts (sim_ts, account_id, scenario, score, evidence, evidence_hash, case_id) "
        "VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s) RETURNING alert_id",
        (sim_ts, account_id, scenario, round(score, 3), _json(evidence), h, case_id)).fetchone()

    bonus = float(load_yaml("rules")["case_priority"]["multi_scenario_bonus"])
    conn.execute(
        "UPDATE cases SET updated_at = now(), priority = LEAST(1.0, "
        " (SELECT max(score) FROM alerts WHERE case_id=%(c)s) + "
        " %(b)s * ((SELECT count(DISTINCT scenario) FROM alerts WHERE case_id=%(c)s) - 1)) "
        "WHERE case_id=%(c)s", {"c": case_id, "b": bonus})
    return alert["alert_id"] if isinstance(alert, dict) else alert[0]


def _json(obj) -> str:
    import json
    return json.dumps(obj)


def window(cfg: dict) -> timedelta:
    if "window_days" in cfg:
        return timedelta(days=cfg["window_days"])
    if "occurrence_window_days" in cfg:
        return timedelta(days=cfg["occurrence_window_days"])
    return timedelta(hours=cfg.get("window_hours", 24))
