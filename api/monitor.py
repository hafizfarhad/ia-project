"""Live monitoring, metrics, and audit endpoints."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from api import audit
from api.security import ANY_ROLE, AUDITOR
from common.db import pool

router = APIRouter(tags=["monitor"])
User = Annotated[dict, Depends(ANY_ROLE)]
Auditor = Annotated[dict, Depends(AUDITOR)]


@router.get("/monitor/feed")
def feed(user: User, limit: int = Query(50, le=500)) -> list[dict]:
    with pool().connection() as conn:
        return conn.execute(
            "SELECT t.txn_id, t.ts, t.txn_type, t.sender_id, t.receiver_id, t.amount, t.city, "
            "m.score AS ml_score FROM transactions t LEFT JOIN ml_scores m USING (txn_id) "
            "ORDER BY t.ts DESC LIMIT %s", (limit,)).fetchall()


@router.get("/monitor/stats")
def stats(user: User) -> dict:
    with pool().connection() as conn:
        t = conn.execute(
            "SELECT count(*) AS txns, max(ts) AS sim_now, "
            "count(*) FILTER (WHERE ingested_at > now() - interval '60 seconds') AS txns_last_60s "
            "FROM transactions").fetchone()
        a = conn.execute(
            "SELECT count(*) AS alerts, "
            "count(*) FILTER (WHERE created_at > now() - interval '60 seconds') AS alerts_last_60s "
            "FROM alerts").fetchone()
        by_scn = conn.execute(
            "SELECT scenario, count(*) AS n FROM alerts GROUP BY scenario ORDER BY scenario").fetchall()
        cases = conn.execute("SELECT status, count(*) AS n FROM cases GROUP BY status").fetchall()
        hourly = conn.execute(
            "SELECT date_trunc('hour', ts) AS hour, count(*) AS n FROM transactions "
            "WHERE ts > (SELECT max(ts) FROM transactions) - interval '48 hours' GROUP BY 1 ORDER BY 1").fetchall()
        alerts_hourly = conn.execute(
            "SELECT date_trunc('hour', sim_ts) AS hour, scenario, count(*) AS n FROM alerts "
            "WHERE sim_ts > (SELECT max(ts) FROM transactions) - interval '48 hours' "
            "GROUP BY 1, 2 ORDER BY 1").fetchall()
    return {**t, **a, "alerts_by_scenario": {r["scenario"]: r["n"] for r in by_scn},
            "cases_by_status": {r["status"]: r["n"] for r in cases},
            "txns_hourly": hourly, "alerts_hourly": alerts_hourly}


@router.get("/metrics")
def metrics(user: User) -> dict:
    from api.ingest import scorer
    with pool().connection() as conn:
        cases = {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, count(*) AS n FROM cases GROUP BY status").fetchall()}
        by_scn = conn.execute(
            "SELECT al.scenario, count(*) AS alerts, "
            "count(*) FILTER (WHERE c.status='CLOSED_FALSE_POSITIVE') AS closed_fp, "
            "count(*) FILTER (WHERE c.status IN ('STR_PENDING','STR_FILED')) AS to_str "
            "FROM alerts al JOIN cases c USING (case_id) GROUP BY al.scenario ORDER BY al.scenario").fetchall()
        snap = conn.execute("SELECT ts, payload FROM eval_snapshots ORDER BY snapshot_id DESC LIMIT 1").fetchone()
    decided = cases.get("CLOSED_FALSE_POSITIVE", 0) + cases.get("STR_FILED", 0)
    total = sum(cases.values())
    return {
        "operations": {
            "cases_total": total, "cases_by_status": cases,
            "analyst_fp_rate": round(cases.get("CLOSED_FALSE_POSITIVE", 0) / decided, 3) if decided else None,
            "case_to_str_conversion": round(cases.get("STR_FILED", 0) / total, 3) if total else None,
            "by_scenario": by_scn,
        },
        "ground_truth_eval": snap["payload"] if snap else None,
        "ground_truth_eval_at": snap["ts"] if snap else None,
        "model": scorer.meta if scorer else None,
    }


@router.get("/audit")
def audit_log(user: Auditor, limit: int = Query(200, le=2000), actor: str | None = None,
              action: str | None = None) -> list[dict]:
    with pool().connection() as conn:
        rows = conn.execute(
            "SELECT entry_id, ts, actor, action, target, details, prev_hash, hash FROM audit_log "
            "WHERE (%(a)s::text IS NULL OR actor=%(a)s) AND (%(ac)s::text IS NULL OR action=%(ac)s) "
            "ORDER BY entry_id DESC LIMIT %(l)s", {"a": actor, "ac": action, "l": limit}).fetchall()
        with conn.transaction():
            audit.record(conn, user["username"], "AUDIT_VIEW")
    return rows


@router.get("/audit/verify")
def audit_verify(user: Auditor) -> dict:
    with pool().connection() as conn:
        with conn.transaction():
            result = audit.verify(conn)
        with conn.transaction():
            audit.record(conn, user["username"], "AUDIT_VERIFY", None,
                         {"ok": result["ok"], "entries_checked": result["entries_checked"]})
    return result
