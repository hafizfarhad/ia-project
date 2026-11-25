"""Graph worker: S4 layering chains and S5 round-trips over the customer money-flow graph.

Every few seconds it loads the recent window of customer-to-customer transfers and runs a
*temporal* path search: each hop must happen after the previous one, which plain graph cycle
algorithms ignore. Alerts are de-duplicated by a signature of the pattern's transactions.
"""
from __future__ import annotations

import hashlib
import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import pairwise

from common.alerts import create_alert, signature_seen
from common.config import load_yaml
from common.db import pool

logging.basicConfig(level=logging.INFO, format="%(asctime)s worker %(levelname)s %(message)s")
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Edge:
    txn_id: str
    ts: datetime
    src: str
    dst: str
    amount: float


def load_edges(conn, since: datetime, until: datetime) -> list[Edge]:
    rows = conn.execute(
        "SELECT t.txn_id, t.ts, t.sender_id, t.receiver_id, t.amount FROM transactions t "
        "JOIN accounts s ON s.account_id=t.sender_id JOIN accounts r ON r.account_id=t.receiver_id "
        "WHERE t.ts > %s AND t.ts <= %s AND s.kind='customer' AND r.kind='customer' ORDER BY t.ts, t.txn_id",
        (since, until)).fetchall()
    return [Edge(r["txn_id"], r["ts"], r["sender_id"], r["receiver_id"], float(r["amount"])) for r in rows]


def index(edges: list[Edge]) -> tuple[dict[str, list[Edge]], dict[str, list[Edge]]]:
    out, inc = defaultdict(list), defaultdict(list)
    for e in edges:
        out[e.src].append(e)
        inc[e.dst].append(e)
    return out, inc


def find_chains(edges: list[Edge], out: dict, inc: dict, cfg: dict, since: datetime) -> list[list[Edge]]:
    """S4: maximal time-ordered paths of ≥ min_hops hops, all within window_hours of the first hop,
    each hop 85–100% of the previous one, no account visited twice. Only paths starting at a
    'head' (no qualifying predecessor) are returned, so a growing chain keeps one identity."""
    lo, hi = cfg["hop_ratio_low"], cfg["hop_ratio_high"]
    win, min_hops = timedelta(hours=cfg["window_hours"]), cfg["min_hops"]

    def ok(prev: Edge, nxt: Edge, t0: datetime) -> bool:
        return prev.ts < nxt.ts <= t0 + win and lo <= nxt.amount / prev.amount <= hi

    chains = []
    for e0 in edges:
        if e0.ts <= since:
            continue
        if any(ok(p, e0, p.ts) for p in inc.get(e0.src, ()) if p.ts >= e0.ts - win):
            continue     # not a head: e0 continues an earlier hop
        stack = [[e0]]
        while stack:
            path = stack.pop()
            last, seen = path[-1], {path[0].src, *(x.dst for x in path)}
            nxts = [n for n in out.get(last.dst, ()) if n.dst not in seen and ok(last, n, e0.ts)]
            if not nxts:
                if len(path) >= min_hops:
                    chains.append(path)
                continue
            stack.extend(path + [n] for n in nxts[:8])
    return chains


def find_cycles(edges: list[Edge], out: dict, cfg: dict, since: datetime) -> list[list[Edge]]:
    """S5: time-ordered cycles of 3..6 hops back to the origin within window_hours,
    returning ≥ min_return_ratio of the amount sent."""
    win = timedelta(hours=cfg["window_hours"])
    lmin, lmax, ret = cfg["min_cycle_len"], cfg["max_cycle_len"], cfg["min_return_ratio"]
    cycles = []
    for e0 in edges:
        if e0.ts <= since:
            continue
        origin = e0.src
        stack = [[e0]]
        while stack:
            path = stack.pop()
            last = path[-1]
            if last.dst == origin:
                if len(path) >= lmin and last.amount / e0.amount >= ret:
                    cycles.append(path)
                continue
            if len(path) >= lmax:
                continue
            seen = {x.src for x in path}
            for n in out.get(last.dst, ()):
                # prune: time-ordered, inside window, amounts stay in a plausible band
                if last.ts < n.ts <= e0.ts + win and (n.dst == origin or n.dst not in seen) \
                        and 0.5 <= n.amount / last.amount <= 1.05:
                    stack.append(path + [n])
    return cycles


def signature(path: list[Edge], kind: str) -> str:
    """S4 chains are identified by their head hop, so a chain that grows by a hop between
    ticks is still the same chain. S5 cycles by their full set of transactions."""
    key = path[0].txn_id if kind == "S4" else "|".join(sorted(e.txn_id for e in path))
    return hashlib.sha256(f"{kind}:{key}".encode()).hexdigest()[:24]


def evidence(path: list[Edge], kind: str, cfg: dict) -> dict:
    hops = [{"txn_id": e.txn_id, "ts": e.ts, "sender_id": e.src, "receiver_id": e.dst, "amount": round(e.amount, 2)}
            for e in path]
    ratios = [round(b.amount / a.amount, 3) for a, b in pairwise(path)]
    accounts = list(dict.fromkeys([path[0].src, *(e.dst for e in path)]))
    ev = {"signature": signature(path, kind), "hops": len(path), "accounts": accounts, "txns": hops,
          "hop_ratios": ratios, "duration_minutes": round((path[-1].ts - path[0].ts).total_seconds() / 60, 1)}
    if kind == "S4":
        ev["rule"] = (f"≥{cfg['min_hops']} time-ordered hops within {cfg['window_hours']}h, each hop "
                      f"{int(cfg['hop_ratio_low']*100)}–{int(cfg['hop_ratio_high']*100)}% of the previous")
    else:
        ev["rule"] = (f"money returns to origin through {cfg['min_cycle_len']}–{cfg['max_cycle_len']} accounts "
                      f"within {cfg['window_hours']}h, ≥{int(cfg['min_return_ratio']*100)}% returned")
        ev["return_ratio"] = round(path[-1].amount / path[0].amount, 3)
    return ev


def tick() -> int:
    rules = load_yaml("rules")
    c4, c5 = rules["S4_layering_chain"], rules["S5_round_trip"]
    created = 0
    with pool().connection() as conn:
        now = conn.execute("SELECT max(ts) AS t FROM transactions").fetchone()["t"]
        if now is None:
            return 0
        horizon = timedelta(hours=max(c4["window_hours"], c5["window_hours"]))
        edges = load_edges(conn, now - 2 * horizon, now)
        out, inc = index(edges)
        chain_since = now - horizon - timedelta(hours=c4["window_hours"])
        found = [("S4", p, c4) for p in find_chains(edges, out, inc, c4, chain_since)]
        found += [("S5", p, c5) for p in find_cycles(edges, out, c5, now - 2 * horizon)]
        with conn.transaction():
            for scn, path, cfg in found:
                ev = evidence(path, scn, cfg)
                for acct in ev["accounts"]:
                    if signature_seen(conn, scn, acct, ev["signature"]):
                        continue
                    create_alert(conn, account_id=acct, scenario=scn, score=cfg["score"],
                                 sim_ts=path[-1].ts, evidence=ev)
                    created += 1
    return created


def main() -> None:
    interval = load_yaml("rules")["graph_worker"]["interval_seconds"]
    while True:
        t0 = time.time()
        try:
            n = tick()
            if n:
                log.info("created %d graph alerts (%.2fs)", n, time.time() - t0)
        except Exception:
            log.exception("tick failed; retrying")
        time.sleep(interval)


if __name__ == "__main__":
    main()
