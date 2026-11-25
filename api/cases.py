"""Case management: queue, detail, disposition, STR maker-checker workflow, PII unmask."""
from __future__ import annotations

from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from api import audit
from api.masking import mask_account
from api.security import ANY_ROLE, MLRO, WORKER
from common.alerts import ACTIVE
from common.db import pool
from common.integrity import evidence_hash

router = APIRouter(tags=["cases"])
User = Annotated[dict, Depends(ANY_ROLE)]
Worker = Annotated[dict, Depends(WORKER)]
Mlro = Annotated[dict, Depends(MLRO)]

SCENARIO_NAMES = {"S1": "Structuring", "S2": "Fan-in / fan-out (mule)", "S3": "Rapid pass-through",
                  "S4": "Layering chain", "S5": "Round-trip", "S6": "Shared device", "ML": "Model risk"}


class Reason(BaseModel):
    reason: str = Field(min_length=20, max_length=2000)


class Narrative(BaseModel):
    narrative: str = Field(min_length=50, max_length=20000)


class Review(BaseModel):
    comment: str = Field(default="", max_length=2000)


class Unmask(BaseModel):
    account_id: str = Field(max_length=32)
    reason: str = Field(min_length=10, max_length=500)


# ------------------------------------------------------------------ queue & detail
@router.get("/cases")
def list_cases(user: User, status: str | None = None, scenario: str | None = None,
               limit: int = Query(200, le=1000)) -> list[dict]:
    q = ("SELECT c.case_id, c.account_id, a.name, a.tier, a.city, c.status, c.priority, c.opened_at, "
         "c.updated_at, array_agg(DISTINCT al.scenario ORDER BY al.scenario) AS scenarios, "
         "count(al.alert_id) AS alerts, max(al.sim_ts) AS last_alert_sim_ts "
         "FROM cases c JOIN accounts a USING (account_id) LEFT JOIN alerts al ON al.case_id = c.case_id "
         "WHERE (%(st)s::text IS NULL OR c.status = %(st)s) "
         "GROUP BY c.case_id, a.name, a.tier, a.city "
         "HAVING (%(sc)s::text IS NULL OR %(sc)s = ANY(array_agg(al.scenario))) "
         "ORDER BY (c.status = ANY(%(active)s)) DESC, c.priority DESC, c.updated_at DESC LIMIT %(lim)s")
    with pool().connection() as conn:
        return conn.execute(q, {"st": status, "sc": scenario, "active": list(ACTIVE), "lim": limit}).fetchall()


def _case(conn, case_id: int) -> dict:
    c = conn.execute("SELECT c.*, u.username AS disposed_by_name FROM cases c "
                     "LEFT JOIN users u ON u.user_id = c.disposed_by WHERE case_id=%s", (case_id,)).fetchone()
    if not c:
        raise HTTPException(404, "case not found")
    return c


@router.get("/cases/{case_id}")
def case_detail(case_id: int, user: User) -> dict:
    with pool().connection() as conn:
        c = _case(conn, case_id)
        acc = conn.execute("SELECT * FROM accounts WHERE account_id=%s", (c["account_id"],)).fetchone()
        alerts = conn.execute("SELECT * FROM alerts WHERE case_id=%s ORDER BY sim_ts", (case_id,)).fetchall()
        for a in alerts:
            # Evidence integrity: re-hash what's stored and compare with the hash frozen at creation.
            a["integrity_ok"] = evidence_hash(a["evidence"]) == a["evidence_hash"]
            a["scenario_name"] = SCENARIO_NAMES.get(a["scenario"], a["scenario"])
        last = max((a["sim_ts"] for a in alerts), default=None)
        txns = []
        if last:
            txns = conn.execute(
                "SELECT t.txn_id, t.ts, t.txn_type, t.sender_id, t.receiver_id, t.amount, t.device_id, "
                "m.score AS ml_score, m.shap FROM transactions t LEFT JOIN ml_scores m USING (txn_id) "
                "WHERE (t.sender_id=%(a)s OR t.receiver_id=%(a)s) AND t.ts BETWEEN %(s)s AND %(e)s "
                "ORDER BY t.ts DESC LIMIT 200",
                {"a": c["account_id"], "s": last - timedelta(days=7), "e": last + timedelta(days=1)}).fetchall()
        strs = conn.execute(
            "SELECT s.str_id, s.status, s.drafted_at, d.username AS drafted_by, s.reviewed_at, "
            "r.username AS reviewed_by, s.review_comment FROM strs s JOIN users d ON d.user_id=s.drafted_by "
            "LEFT JOIN users r ON r.user_id=s.reviewed_by WHERE case_id=%s ORDER BY str_id", (case_id,)).fetchall()
        with conn.transaction():
            audit.record(conn, user["username"], "CASE_VIEW", f"case:{case_id}")
    return {"case": c, "account": mask_account(acc), "alerts": alerts, "transactions": txns, "strs": strs}


@router.get("/cases/{case_id}/graph")
def case_graph(case_id: int, user: User, hours: int = Query(72, le=24 * 14)) -> dict:
    """Money flow around the subject: their counterparties, plus flows among those counterparties."""
    with pool().connection() as conn:
        c = _case(conn, case_id)
        last = conn.execute("SELECT max(sim_ts) AS t FROM alerts WHERE case_id=%s", (case_id,)).fetchone()["t"]
        if last is None:
            return {"nodes": [], "edges": []}
        a, s, e = c["account_id"], last - timedelta(hours=hours), last + timedelta(hours=6)
        hop1 = conn.execute(
            "SELECT sender_id, receiver_id, sum(amount) AS total, count(*) AS n FROM transactions "
            "WHERE (sender_id=%(a)s OR receiver_id=%(a)s) AND ts BETWEEN %(s)s AND %(e)s "
            "GROUP BY sender_id, receiver_id", {"a": a, "s": s, "e": e}).fetchall()
        nbrs = list({r["sender_id"] for r in hop1} | {r["receiver_id"] for r in hop1})
        hop2 = conn.execute(
            "SELECT t.sender_id, t.receiver_id, sum(t.amount) AS total, count(*) AS n FROM transactions t "
            "JOIN accounts sa ON sa.account_id=t.sender_id JOIN accounts ra ON ra.account_id=t.receiver_id "
            "WHERE t.sender_id = ANY(%(n)s) AND t.receiver_id = ANY(%(n)s) AND t.ts BETWEEN %(s)s AND %(e)s "
            "AND sa.kind='customer' AND ra.kind='customer' AND t.sender_id<>%(a)s AND t.receiver_id<>%(a)s "
            "GROUP BY t.sender_id, t.receiver_id LIMIT 150", {"n": nbrs, "a": a, "s": s, "e": e}).fetchall()
        ids = list({a, *nbrs})
        accs = conn.execute(
            "SELECT a.account_id, a.kind, a.name, a.tier, "
            "EXISTS (SELECT 1 FROM alerts al WHERE al.account_id=a.account_id) AS alerted "
            "FROM accounts a WHERE account_id = ANY(%s)", (ids,)).fetchall()
    nodes = [{**x, "subject": x["account_id"] == a} for x in accs]
    edges = [{"source": r["sender_id"], "target": r["receiver_id"], "total": float(r["total"]), "count": r["n"]}
             for r in [*hop1, *hop2]]
    return {"nodes": nodes, "edges": edges, "window": {"from": s, "to": e}}


# ------------------------------------------------------------------ disposition
def _transition(conn, case_id: int, frm: tuple[str, ...], to: str, user: dict, reason: str | None) -> None:
    row = conn.execute(
        "UPDATE cases SET status=%s, disposition_reason=coalesce(%s, disposition_reason), disposed_by=%s, "
        "updated_at=now() WHERE case_id=%s AND status = ANY(%s) RETURNING case_id",
        (to, reason, user["user_id"], case_id, list(frm))).fetchone()
    if not row:
        _case(conn, case_id)
        raise HTTPException(409, f"case is not in state {' / '.join(frm)}")


@router.post("/cases/{case_id}/close")
def close_case(case_id: int, body: Reason, user: Worker) -> dict:
    with pool().connection() as conn, conn.transaction():
        _transition(conn, case_id, ("OPEN",), "CLOSED_FALSE_POSITIVE", user, body.reason)
        audit.record(conn, user["username"], "CASE_CLOSE_FP", f"case:{case_id}", {"reason": body.reason})
    return {"case_id": case_id, "status": "CLOSED_FALSE_POSITIVE"}


@router.post("/cases/{case_id}/escalate")
def escalate_case(case_id: int, body: Reason, user: Worker) -> dict:
    with pool().connection() as conn, conn.transaction():
        _transition(conn, case_id, ("OPEN",), "ESCALATED", user, body.reason)
        audit.record(conn, user["username"], "CASE_ESCALATE", f"case:{case_id}", {"reason": body.reason})
    return {"case_id": case_id, "status": "ESCALATED"}


# ------------------------------------------------------------------ STR (maker-checker)
@router.get("/cases/{case_id}/str-template")
def str_template(case_id: int, user: Worker) -> dict:
    with pool().connection() as conn:
        c = _case(conn, case_id)
        acc = mask_account(conn.execute("SELECT * FROM accounts WHERE account_id=%s",
                                        (c["account_id"],)).fetchone())
        alerts = conn.execute("SELECT scenario, sim_ts, evidence FROM alerts WHERE case_id=%s ORDER BY sim_ts",
                              (case_id,)).fetchall()
    lines = [
        f"Subject: {acc['name']} — wallet {acc['account_id']} (tier {acc['tier']}, {acc['city']}), "
        f"CNIC {acc['cnic']}, MSISDN {acc['msisdn']}.",
        "",
        "Grounds for suspicion:",
    ]
    for a in alerts:
        ev = a["evidence"]
        when = f"{a['sim_ts']:%Y-%m-%d %H:%M}"
        lines.append(f"- {SCENARIO_NAMES.get(a['scenario'])} detected at {when} — {ev.get('rule', '')}.")
    lines += ["", f"Escalation note: {c['disposition_reason'] or ''}", "",
              "Analyst assessment: <describe the pattern, the counterparties involved, and why it is "
              "inconsistent with the customer's profile>"]
    return {"narrative": "\n".join(lines)}


@router.post("/cases/{case_id}/str")
def draft_str(case_id: int, body: Narrative, user: Worker) -> dict:
    with pool().connection() as conn, conn.transaction():
        _transition(conn, case_id, ("ESCALATED",), "STR_PENDING", user, None)
        sid = conn.execute("INSERT INTO strs (case_id, narrative, drafted_by) VALUES (%s, %s, %s) RETURNING str_id",
                           (case_id, body.narrative, user["user_id"])).fetchone()["str_id"]
        audit.record(conn, user["username"], "STR_DRAFT", f"str:{sid}", {"case_id": case_id})
    return {"str_id": sid, "status": "PENDING"}


@router.get("/strs")
def list_strs(user: User, status: str | None = None) -> list[dict]:
    with pool().connection() as conn:
        return conn.execute(
            "SELECT s.str_id, s.case_id, c.account_id, s.status, s.narrative, s.drafted_at, "
            "d.username AS drafted_by, s.reviewed_at, r.username AS reviewed_by, s.review_comment "
            "FROM strs s JOIN cases c USING (case_id) JOIN users d ON d.user_id=s.drafted_by "
            "LEFT JOIN users r ON r.user_id=s.reviewed_by "
            "WHERE (%(st)s::text IS NULL OR s.status=%(st)s) ORDER BY s.str_id DESC LIMIT 200",
            {"st": status}).fetchall()


def _review(str_id: int, user: dict, approve: bool, comment: str) -> dict:
    with pool().connection() as conn, conn.transaction():
        s = conn.execute("SELECT * FROM strs WHERE str_id=%s FOR UPDATE", (str_id,)).fetchone()
        if not s:
            raise HTTPException(404, "STR not found")
        if s["status"] != "PENDING":
            raise HTTPException(409, "STR already reviewed")
        self_review = s["drafted_by"] == user["user_id"]
        if self_review:
            # Separation of duties (the DB CHECK constraint would also reject it). Commit the
            # audit record, then refuse — raising here would roll the audit entry back.
            audit.record(conn, user["username"], "STR_SELF_REVIEW_BLOCKED", f"str:{str_id}")
    if self_review:
        raise HTTPException(403, "maker-checker: you cannot review an STR you drafted")
    with pool().connection() as conn, conn.transaction():
        s = conn.execute("SELECT * FROM strs WHERE str_id=%s FOR UPDATE", (str_id,)).fetchone()
        if s["status"] != "PENDING":
            raise HTTPException(409, "STR already reviewed")
        new = "FILED" if approve else "REJECTED"
        conn.execute("UPDATE strs SET status=%s, reviewed_by=%s, reviewed_at=now(), review_comment=%s "
                     "WHERE str_id=%s", (new, user["user_id"], comment, str_id))
        conn.execute("UPDATE cases SET status=%s, updated_at=now() WHERE case_id=%s",
                     ("STR_FILED" if approve else "OPEN", s["case_id"]))
        audit.record(conn, user["username"], "STR_APPROVE" if approve else "STR_REJECT", f"str:{str_id}",
                     {"case_id": s["case_id"], "comment": comment})
    return {"str_id": str_id, "status": new}


@router.post("/strs/{str_id}/approve")
def approve_str(str_id: int, body: Review, user: Mlro) -> dict:
    return _review(str_id, user, True, body.comment)


@router.post("/strs/{str_id}/reject")
def reject_str(str_id: int, body: Review, user: Mlro) -> dict:
    if len(body.comment) < 10:
        raise HTTPException(422, "rejection needs a comment of at least 10 characters")
    return _review(str_id, user, False, body.comment)


@router.get("/strs/{str_id}/export", response_class=PlainTextResponse)
def export_str(str_id: int, user: Mlro) -> str:
    with pool().connection() as conn:
        s = conn.execute(
            "SELECT s.*, d.username AS drafter, r.username AS reviewer, c.account_id FROM strs s "
            "JOIN cases c USING (case_id) JOIN users d ON d.user_id=s.drafted_by "
            "LEFT JOIN users r ON r.user_id=s.reviewed_by WHERE str_id=%s", (str_id,)).fetchone()
        if not s:
            raise HTTPException(404, "STR not found")
        txns = conn.execute(
            "SELECT DISTINCT jsonb_array_elements(evidence->'txns') AS t FROM alerts WHERE case_id=%s",
            (s["case_id"],)).fetchall()
        with conn.transaction():
            audit.record(conn, user["username"], "STR_EXPORT", f"str:{str_id}")
    rows = sorted((r["t"] for r in txns), key=lambda t: t.get("ts", ""))
    table = "\n".join(f"| {t.get('ts','')[:16]} | {t.get('txn_id')} | {t.get('sender_id')} | "
                      f"{t.get('receiver_id')} | {t.get('amount'):,.2f} |" for t in rows[:100])
    return (f"# SUSPICIOUS TRANSACTION REPORT — STR-{str_id:05d}\n\n"
            f"Prepared for: Financial Monitoring Unit (via goAML)  \n"
            f"Status: **{s['status']}**  \nCase: {s['case_id']} — subject wallet {s['account_id']}  \n"
            f"Drafted by: {s['drafter']} on {s['drafted_at']:%Y-%m-%d %H:%M} UTC  \n"
            f"Reviewed by (MLRO): {s['reviewer'] or '—'}"
            f"{' on ' + format(s['reviewed_at'], '%Y-%m-%d %H:%M') + ' UTC' if s['reviewed_at'] else ''}  \n\n"
            f"## Narrative\n\n{s['narrative']}\n\n"
            f"## Transactions referenced\n\n| Time (sim) | Txn | From | To | Amount (PKR) |\n|---|---|---|---|---|\n"
            f"{table}\n\n_Synthetic data — training system, not a real report._\n")


# ------------------------------------------------------------------ PII
@router.post("/pii/unmask")
def unmask(body: Unmask, user: Mlro) -> dict:
    with pool().connection() as conn, conn.transaction():
        acc = conn.execute("SELECT account_id, cnic, msisdn FROM accounts WHERE account_id=%s",
                           (body.account_id,)).fetchone()
        if not acc:
            raise HTTPException(404, "account not found")
        audit.record(conn, user["username"], "PII_UNMASK", f"account:{body.account_id}", {"reason": body.reason})
    return acc
