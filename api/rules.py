"""Per-transaction typology rules S1, S2, S3, S6 (SPEC §4). Run inside the ingest transaction,
after the transaction row is inserted. Windows are in *simulated* time (txn.ts)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from psycopg import Connection

from common.alerts import recent_alert_exists, window
from common.config import load_yaml


@dataclass
class Hit:
    account_id: str
    scenario: str
    score: float
    evidence: dict


def _f(x) -> float:
    return round(float(x), 2)


def _txns(rows, keys=("txn_id", "ts", "sender_id", "receiver_id", "amount")) -> list[dict]:
    return [{k: (_f(r[k]) if k == "amount" else r[k]) for k in keys if k in r} for r in rows]


def s1_structuring(conn, txn, receiver, limits, cfg) -> Hit | None:
    if receiver["kind"] != "customer" or not receiver["tier"]:
        return None
    daily = float(limits[receiver["tier"]]["daily"])
    lo, hi = daily * cfg["band_low"], daily * cfg["band_high"]
    if not (lo <= float(txn["amount"]) <= hi):
        return None
    rows = conn.execute(
        "SELECT txn_id, ts, sender_id, receiver_id, amount FROM transactions "
        "WHERE receiver_id=%s AND ts > %s AND ts <= %s AND amount BETWEEN %s AND %s ORDER BY ts",
        (receiver["account_id"], txn["ts"] - timedelta(days=cfg["window_days"]), txn["ts"], lo, hi)).fetchall()
    if len(rows) < cfg["min_count"]:
        return None
    return Hit(receiver["account_id"], "S1", cfg["score"], {
        "rule": f"≥{cfg['min_count']} incoming txns in {cfg['window_days']}d each "
                f"{int(cfg['band_low']*100)}–{int(cfg['band_high']*100)}% of tier daily limit",
        "tier": receiver["tier"], "daily_limit": daily, "band": [_f(lo), _f(hi)],
        "count": len(rows), "txns": _txns(rows)})


def s2_fan_in_out(conn, txn, acct, cfg) -> Hit | None:
    if acct["kind"] != "customer":
        return None
    a, since = acct["account_id"], txn["ts"] - timedelta(hours=cfg["window_hours"])
    agg = conn.execute(
        "SELECT count(DISTINCT sender_id) FILTER (WHERE receiver_id=%(a)s) AS in_cp, "
        "       count(DISTINCT receiver_id) FILTER (WHERE sender_id=%(a)s) AS out_cp, "
        "       coalesce(sum(amount) FILTER (WHERE receiver_id=%(a)s), 0) AS in_amt, "
        "       coalesce(sum(amount) FILTER (WHERE sender_id=%(a)s), 0) AS out_amt "
        "FROM transactions WHERE (sender_id=%(a)s OR receiver_id=%(a)s) AND ts > %(s)s AND ts <= %(t)s",
        {"a": a, "s": since, "t": txn["ts"]}).fetchone()
    in_amt, out_amt = float(agg["in_amt"]), float(agg["out_amt"])   # NUMERIC arrives as Decimal
    if not (agg["in_cp"] >= cfg["min_distinct_senders"] and agg["out_cp"] >= cfg["min_distinct_receivers"]
            and in_amt > 0 and out_amt >= cfg["min_out_in_ratio"] * in_amt):
        return None
    rows = conn.execute(
        "SELECT txn_id, ts, sender_id, receiver_id, amount FROM transactions "
        "WHERE (sender_id=%(a)s OR receiver_id=%(a)s) AND ts > %(s)s AND ts <= %(t)s ORDER BY ts LIMIT 60",
        {"a": a, "s": since, "t": txn["ts"]}).fetchall()
    return Hit(a, "S2", cfg["score"], {
        "rule": f"in 24h: ≥{cfg['min_distinct_senders']} distinct senders, "
                f"≥{cfg['min_distinct_receivers']} distinct receivers, out ≥ {int(cfg['min_out_in_ratio']*100)}% of in",
        "distinct_senders": agg["in_cp"], "distinct_receivers": agg["out_cp"],
        "in_amount": _f(in_amt), "out_amount": _f(out_amt),
        "out_in_ratio": round(out_amt / in_amt, 3), "txns": _txns(rows)})


def s3_pass_through(conn, txn, sender, cfg) -> Hit | None:
    if sender["kind"] != "customer":
        return None
    a, mins = sender["account_id"], cfg["forward_within_minutes"]
    rows = conn.execute(
        "SELECT i.txn_id, i.ts, i.sender_id, i.receiver_id, i.amount, "
        "  (SELECT coalesce(sum(o.amount), 0) FROM transactions o "
        "    WHERE o.sender_id=%(a)s AND o.ts >= i.ts AND o.ts <= i.ts + make_interval(mins => %(m)s) "
        "      AND o.txn_id <> i.txn_id) AS forwarded "
        "FROM transactions i WHERE i.receiver_id=%(a)s AND i.amount >= %(min)s "
        "  AND i.ts > %(since)s AND i.ts <= %(t)s ORDER BY i.ts",
        {"a": a, "m": mins, "min": cfg["min_incoming"],
         "since": txn["ts"] - timedelta(days=cfg["occurrence_window_days"]), "t": txn["ts"]}).fetchall()
    occ = [r for r in rows if float(r["forwarded"]) >= cfg["forward_ratio"] * float(r["amount"])]
    if len(occ) < cfg["min_occurrences"]:
        return None
    # Only fire when *this* outgoing txn completes an occurrence.
    if occ[-1]["ts"] < txn["ts"] - timedelta(minutes=mins):
        return None
    return Hit(a, "S3", cfg["score"], {
        "rule": f"incoming ≥ {cfg['min_incoming']:,} with ≥{int(cfg['forward_ratio']*100)}% forwarded "
                f"within {mins} min, ≥{cfg['min_occurrences']}× in {cfg['occurrence_window_days']}d",
        "occurrences": [{"txn_id": r["txn_id"], "ts": r["ts"], "incoming": _f(r["amount"]),
                         "forwarded": _f(r["forwarded"]),
                         "forward_ratio": round(float(r["forwarded"]) / float(r["amount"]), 3)} for r in occ]})


def s6_shared_device(conn, txn, sender, cfg) -> Hit | None:
    if sender["kind"] != "customer" or not txn.get("device_id"):
        return None
    rows = conn.execute(
        "SELECT sender_id, count(*) AS txns, max(ts) AS last_seen FROM transactions "
        "WHERE device_id=%s AND ts > %s AND ts <= %s GROUP BY sender_id ORDER BY sender_id",
        (txn["device_id"], txn["ts"] - timedelta(days=cfg["window_days"]), txn["ts"])).fetchall()
    if len(rows) < cfg["min_wallets"]:
        return None
    return Hit(sender["account_id"], "S6", cfg["score"], {
        "rule": f"≥{cfg['min_wallets']} wallets active on one device in {cfg['window_days']}d",
        "device_id": txn["device_id"],
        "wallets": [{"account_id": r["sender_id"], "txns": r["txns"], "last_seen": r["last_seen"]} for r in rows]})


CFG_KEYS = {"S1": "S1_structuring", "S2": "S2_fan_in_out", "S3": "S3_pass_through", "S6": "S6_shared_device"}


def evaluate(conn: Connection, txn: dict, sender: dict, receiver: dict) -> list[Hit]:
    rules = load_yaml("rules")
    limits = load_yaml("limits")["tiers"]
    c = {k: rules[v] for k, v in CFG_KEYS.items()}
    candidates = [
        s1_structuring(conn, txn, receiver, limits, c["S1"]),
        s2_fan_in_out(conn, txn, receiver, c["S2"]),
        s2_fan_in_out(conn, txn, sender, c["S2"]),
        s3_pass_through(conn, txn, sender, c["S3"]),
        s6_shared_device(conn, txn, sender, c["S6"]),
    ]
    hits, seen = [], set()
    for h in candidates:
        if h is None or (h.account_id, h.scenario) in seen:
            continue
        # De-duplicate: one alert per account+scenario per rule window.
        if recent_alert_exists(conn, h.account_id, h.scenario, txn["ts"] - window(c[h.scenario])):
            continue
        seen.add((h.account_id, h.scenario))
        hits.append(h)
    return hits
