"""Tamper-evident audit log: each entry's MAC covers the previous entry's MAC (a keyed hash chain).

Editing, deleting or reordering any past row breaks every link after it, which /audit/verify
detects. The chain is keyed (HMAC-SHA256) with a secret held only by the API process, so a
database administrator who rewrites a row cannot simply recompute the rest of the chain:
forging it needs both database access *and* the API's key (Schneier & Kelsey, 1999)."""
from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime

from psycopg import Connection

from common.config import settings
from common.integrity import canonical, normalise

GENESIS = "0" * 64
_LOCK_KEY = 7_204_501   # advisory lock so concurrent writers can't fork the chain


def _entry_hash(prev_hash: str, ts: datetime, actor: str, action: str, target: str | None,
                details: dict) -> str:
    ts_s = ts.astimezone(UTC).isoformat(timespec="microseconds")
    msg = "|".join([prev_hash, ts_s, actor, action, target or "", canonical(details)])
    return hmac.new(settings().audit_hmac_key.encode(), msg.encode(), hashlib.sha256).hexdigest()


def record(conn: Connection, actor: str, action: str, target: str | None = None,
           details: dict | None = None) -> None:
    """Append an entry. Call inside the caller's transaction so the action and its audit
    record commit (or roll back) together."""
    details = normalise(details or {})
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_KEY,))
    last = conn.execute("SELECT hash FROM audit_log ORDER BY entry_id DESC LIMIT 1").fetchone()
    prev = last["hash"] if last else GENESIS
    ts = datetime.now(UTC)
    h = _entry_hash(prev, ts, actor, action, target, details)
    conn.execute(
        "INSERT INTO audit_log (ts, actor, action, target, details, prev_hash, hash) "
        "VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s)",
        (ts, actor, action, target, canonical(details), prev, h))


def verify(conn: Connection) -> dict:
    """Recompute the whole chain. Reports the first entry whose stored hash or link is wrong."""
    prev, n = GENESIS, 0
    with conn.cursor(name="audit_verify") as cur:   # server-side cursor: streams large logs
        cur.execute("SELECT entry_id, ts, actor, action, target, details, prev_hash, hash "
                    "FROM audit_log ORDER BY entry_id")
        for row in cur:
            n += 1
            if row["prev_hash"] != prev:
                return {"ok": False, "entries_checked": n, "broken_at": row["entry_id"],
                        "reason": "link broken: prev_hash does not match previous entry (row deleted or reordered?)"}
            expect = _entry_hash(prev, row["ts"], row["actor"], row["action"], row["target"], row["details"])
            if not hmac.compare_digest(expect, row["hash"]):
                return {"ok": False, "entries_checked": n, "broken_at": row["entry_id"],
                        "reason": "content changed: stored MAC does not match entry contents"}
            prev = row["hash"]
    return {"ok": True, "entries_checked": n, "head_hash": prev}
