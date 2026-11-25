"""Build a complete synthetic dataset: world + all transactions + ground truth."""
from __future__ import annotations

from datetime import UTC, datetime

from simulator import behaviours, typologies
from simulator.world import World

DEFAULT_START = datetime(2026, 9, 1, tzinfo=UTC)


def generate(seed: int, days: int, start: datetime = DEFAULT_START, n_customers: int = 2000) -> World:
    w = World(seed, start, n_customers=n_customers)
    # Order matters: roles that change devices/tiers come first; normal behaviour last,
    # so every transaction is stamped with the account's final device.
    behaviours.shared_phones(w)
    behaviours.committees(w, days)
    behaviours.salaries(w, days)
    behaviours.family_chains(w, days)
    typologies.plant_rings(w, days)
    eid_day = int(w.rng.integers(10, max(11, days - 5)))
    behaviours.normal_behaviour(w, days, eid_day)

    end = start.timestamp() + days * 86400
    w.txns = [t for t in w.txns if t["ts"].timestamp() < end]
    kept = {t["txn_id"] for t in w.txns}
    w.txn_truth = {k: v for k, v in w.txn_truth.items() if k in kept}
    w.txns.sort(key=lambda t: (t["ts"], t["txn_id"]))
    return w


def summary(w: World) -> dict:
    from collections import Counter
    n = len(w.txns)
    by_typ = Counter(v[1] for v in w.txn_truth.values())
    acc = Counter(v[1] for v in w.acct_truth.values())
    illicit = sum(1 for v in w.txn_truth.values() if v[0] == "illicit")
    return {"transactions": n, "illicit_txns": illicit, "illicit_pct": round(100 * illicit / max(n, 1), 2),
            "txns_by_typology": dict(by_typ), "accounts_by_typology": dict(acc),
            "types": dict(Counter(t["txn_type"] for t in w.txns))}
