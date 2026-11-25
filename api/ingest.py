"""Transaction ingest: store → rules → ML score → alerts, all in one DB transaction per batch."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api import rules
from api.ml import Scorer
from api.security import ingest_token
from common.alerts import create_alert, recent_alert_exists
from common.config import load_yaml
from common.db import pool

log = logging.getLogger(__name__)
router = APIRouter(prefix="/ingest", tags=["ingest"], dependencies=[Depends(ingest_token)])

scorer: Scorer | None = None
_accounts: dict[str, dict] = {}


class TxnIn(BaseModel):
    txn_id: str = Field(min_length=4, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    ts: datetime
    txn_type: Literal["P2P", "CASH_IN", "CASH_OUT", "BILL", "TOPUP", "IBFT_OUT", "IBFT_IN", "SALARY"]
    sender_id: str = Field(max_length=32)
    receiver_id: str = Field(max_length=32)
    amount: float = Field(gt=0, le=100_000_000)
    device_id: str | None = Field(default=None, max_length=32)
    city: str | None = Field(default=None, max_length=32)


class Batch(BaseModel):
    txns: list[TxnIn] = Field(max_length=5000)


def _account(conn, account_id: str) -> dict:
    acc = _accounts.get(account_id)
    if acc is None:
        acc = conn.execute("SELECT account_id, kind, tier, opened_at FROM accounts WHERE account_id=%s",
                           (account_id,)).fetchone()
        if acc is None:
            raise HTTPException(422, f"unknown account {account_id}")
        _accounts[account_id] = acc
    return acc


@router.post("/batch")
def ingest_batch(batch: Batch) -> dict:
    ml_thr = float(load_yaml("rules")["ML"]["threshold"])
    stored = dup = n_alerts = 0
    with pool().connection() as conn, conn.transaction():
        for t in batch.txns:
            txn = t.model_dump()
            sender, receiver = _account(conn, txn["sender_id"]), _account(conn, txn["receiver_id"])
            ins = conn.execute(
                "INSERT INTO transactions (txn_id, ts, txn_type, sender_id, receiver_id, amount, device_id, city) "
                "VALUES (%(txn_id)s, %(ts)s, %(txn_type)s, %(sender_id)s, %(receiver_id)s, %(amount)s, "
                "%(device_id)s, %(city)s) ON CONFLICT (txn_id) DO NOTHING RETURNING txn_id", txn).fetchone()
            if ins is None:
                dup += 1      # idempotent: a replayed txn is ignored, not double-counted
                continue
            stored += 1

            hits = rules.evaluate(conn, txn, sender, receiver)
            for h in hits:
                create_alert(conn, account_id=h.account_id, scenario=h.scenario, score=h.score,
                             sim_ts=txn["ts"], evidence={"trigger_txn": txn["txn_id"], **h.evidence})
            n_alerts += len(hits)

            if scorer and scorer.booster is not None:
                f = scorer.features(conn, txn, sender, receiver)
                p, shap = scorer.score(f)
                conn.execute("INSERT INTO ml_scores (txn_id, score, shap, model_version) "
                             "VALUES (%s, %s, %s::jsonb, %s)",
                             (txn["txn_id"], round(p, 4), json.dumps(shap), scorer.version))
                if p >= ml_thr:
                    n_alerts += _ml_alert(conn, txn, sender, receiver, p, shap, hits)
    return {"stored": stored, "duplicates": dup, "alerts": n_alerts}


def _ml_alert(conn, txn, sender, receiver, p, shap, hits) -> int:
    """ML alert only where the rules were silent; on the customer side of the txn."""
    subject = receiver if txn["txn_type"] in ("CASH_IN", "IBFT_IN", "SALARY") else sender
    if subject["kind"] != "customer":
        return 0
    if any(h.account_id == subject["account_id"] for h in hits):
        return 0
    if recent_alert_exists(conn, subject["account_id"], "ML", txn["ts"] - timedelta(hours=24)):
        return 0
    create_alert(conn, account_id=subject["account_id"], scenario="ML", score=min(0.95, p), sim_ts=txn["ts"],
                 evidence={"trigger_txn": txn["txn_id"], "model_version": scorer.version,
                           "probability": round(p, 4), "threshold": float(load_yaml("rules")["ML"]["threshold"]),
                           "rule": "model risk ≥ threshold on a txn no rule flagged",
                           "shap_top5": shap,
                           "txns": [{"txn_id": txn["txn_id"], "ts": txn["ts"], "sender_id": txn["sender_id"],
                                     "receiver_id": txn["receiver_id"], "amount": txn["amount"]}]})
    return 1


@router.get("/status")
def ingest_status() -> dict:
    with pool().connection() as conn:
        row = conn.execute("SELECT max(ts) AS last_ts, count(*) AS n FROM transactions").fetchone()
    return {"last_ts": row["last_ts"], "count": row["n"]}
