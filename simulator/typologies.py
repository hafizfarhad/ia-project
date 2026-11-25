"""Laundering rings. Each ring runs one typology; its members are recruited ordinary customers
(they keep their normal behaviour too), so IDs and profiles give nothing away."""
from __future__ import annotations

from datetime import timedelta
from itertools import pairwise

import numpy as np

from simulator.world import World


def _hrs(h: float) -> timedelta:
    return timedelta(hours=h)


def _mins(m: float) -> timedelta:
    return timedelta(minutes=m)


def structuring(w: World, ring: str, start_day: int, days: int) -> None:
    """S1: several smurf wallets on one handset take cash-ins just under their daily limit
    at different agents, then pass it to a collector a few hours later."""
    rng = w.rng
    collector = w.take(1, tier="L2")[0]
    smurfs = w.take(int(rng.integers(3, 6)))
    dev = "D" + rng.bytes(6).hex()
    for s in smurfs:
        w.accounts[s].tier = str(rng.choice(["L0", "L1"]))
        w.accounts[s].device_id = dev
    w.mark([collector, *smurfs], "illicit", "S1_STRUCTURING", ring)
    agents = [a.account_id for a in w.of_kind("agent")]
    truth = ("illicit", "S1_STRUCTURING", ring)
    end = min(days, start_day + 12)
    for s in smurfs:
        d = start_day + float(rng.uniform(0, 1.5))
        while d < end:
            day, hour = int(d), 9 + rng.random() * 10
            amt = round(w.daily_limit(w.accounts[s].tier) * rng.uniform(0.86, 0.99), -2)
            t = w.txn(w.at(day, hour), "CASH_IN", str(rng.choice(agents)), s, amt, truth=truth)
            w.txn(t["ts"] + _hrs(rng.uniform(2, 8)), "P2P", s, collector,
                  round(amt * rng.uniform(0.95, 1.0), -2), truth=truth)
            d += float(rng.uniform(1, 3))
    for d in range(start_day + 3, end + 2, 4):
        if d < days:
            w.txn(w.at(d, 14 + rng.random() * 4), "IBFT_OUT", collector, "X-BANK-01",
                  float(round(rng.uniform(80000, 200000), -3)), truth=truth)


def fan_in_out(w: World, ring: str, start_day: int, days: int) -> None:
    """S2: a mule receives from many unrelated senders (fraud proceeds) in one day,
    then spreads it to downstream mules who cash out."""
    rng = w.rng
    mule = w.take(1, tier="L2")[0]
    downstream = w.take(int(rng.integers(5, 8)), tier="L1")
    w.mark([mule, *downstream], "illicit", "S2_FAN_IN_OUT", ring)
    truth = ("illicit", "S2_FAN_IN_OUT", ring)
    victims_pool = [a.account_id for a in w.of_kind("customer") if a.account_id not in w.acct_truth]
    d = start_day
    for _ in range(3):
        if d >= days:
            break
        victims = rng.choice(victims_pool, size=int(rng.integers(10, 19)), replace=False)
        received = 0.0
        last = w.at(d, 9)
        for v in victims:
            ts = w.at(d, 9 + rng.random() * 6)
            amt = float(round(rng.uniform(3000, 15000), -2))
            w.txn(ts, "P2P", str(v), mule, amt, truth=truth)
            received += amt
            last = max(last, ts)
        out_total = received * rng.uniform(0.85, 0.95)
        shares = rng.dirichlet(np.ones(len(downstream))) * out_total
        for i, (dn, amt) in enumerate(zip(downstream, shares, strict=True)):
            t = w.txn(last + _mins(20 + i * rng.uniform(10, 30)), "P2P", mule, dn, round(amt, -2), truth=truth)
            w.txn(t["ts"] + _hrs(rng.uniform(0.5, 3)), "CASH_OUT", dn, w.accounts[dn].home_agent,
                  round(amt * 0.97, -2), truth=truth)
        d += int(rng.integers(2, 5))


def pass_through(w: World, ring: str, start_day: int, days: int) -> None:
    """S3: large bank transfers land in a wallet and almost all of it leaves within the hour."""
    rng = w.rng
    acct = w.take(1, tier="L2")[0]
    partners = w.take(2, tier="L2")
    w.mark([acct, *partners], "illicit", "S3_PASS_THROUGH", ring)
    truth = ("illicit", "S3_PASS_THROUGH", ring)
    d = start_day + float(rng.uniform(0, 1))
    for _ in range(int(rng.integers(4, 6))):
        if d >= days:
            break
        t = w.txn(w.at(int(d), 10 + rng.random() * 8), "IBFT_IN", str(rng.choice(["X-BANK-02", "X-BANK-03"])),
                  acct, float(round(rng.uniform(50000, 250000), -3)), device=None, truth=truth)
        out = t["amount"] * rng.uniform(0.92, 0.98)
        if rng.random() < 0.5:
            w.txn(t["ts"] + _mins(rng.uniform(10, 50)), "IBFT_OUT", acct, "X-BANK-04", round(out, -2), truth=truth)
        else:
            split = rng.uniform(0.4, 0.6)
            ts = t["ts"] + _mins(rng.uniform(10, 30))
            w.txn(ts, "P2P", acct, partners[0], round(out * split, -2), truth=truth)
            w.txn(ts + _mins(rng.uniform(2, 15)), "P2P", acct, partners[1], round(out * (1 - split), -2), truth=truth)
        d += float(rng.uniform(1.5, 3))


def layering(w: World, ring: str, start_day: int, days: int) -> None:
    """S4: money hops through 4-6 accounts within a few hours, a little skimmed at each hop."""
    rng = w.rng
    members = w.take(8, tier="L2")
    w.mark(members, "illicit", "S4_LAYERING", ring)
    truth = ("illicit", "S4_LAYERING", ring)
    d = start_day
    for _ in range(3):
        if d >= days:
            break
        path = list(rng.choice(members, size=int(rng.integers(5, 8)), replace=False))
        ts = w.at(d, 9 + rng.random() * 6)
        amt = float(round(rng.uniform(100000, 300000), -3))
        w.txn(ts, "IBFT_IN", "X-BANK-05", path[0], amt, device=None, truth=truth)
        for a, b in pairwise(path):
            ts += _mins(rng.uniform(10, 45))
            amt = round(amt * rng.uniform(0.90, 0.98), -2)
            w.txn(ts, "P2P", str(a), str(b), amt, truth=truth)
        w.txn(ts + _mins(rng.uniform(10, 40)), "CASH_OUT", str(path[-1]),
              w.accounts[str(path[-1])].home_agent, round(amt * 0.98, -2), truth=truth)
        d += int(rng.integers(3, 6))


def round_trip(w: World, ring: str, start_day: int, days: int) -> None:
    """S5: money leaves an account, goes round 3-5 accounts over a day or two, and comes back."""
    rng = w.rng
    members = w.take(5, tier="L2")
    w.mark(members, "illicit", "S5_ROUND_TRIP", ring)
    truth = ("illicit", "S5_ROUND_TRIP", ring)
    d = start_day
    for _ in range(3):
        if d >= days - 2:
            break
        k = int(rng.integers(3, 6))
        path = list(rng.choice(members, size=k, replace=False))
        path.append(path[0])
        ts = w.at(d, 10 + rng.random() * 6)
        amt = float(round(rng.uniform(100000, 300000), -3))
        for a, b in pairwise(path):
            w.txn(ts, "P2P", str(a), str(b), amt, truth=truth)
            ts += _hrs(rng.uniform(4, 12))
            amt = round(amt * rng.uniform(0.88, 0.99), -2)
        d += int(rng.integers(4, 8))


TYPOLOGIES = [structuring, fan_in_out, pass_through, layering, round_trip]


def plant_rings(w: World, days: int, n_rings: int = 10, first_day: int = 3) -> None:
    """Two rings per typology, start days spread across the run."""
    rng = w.rng
    last_start = max(first_day + 1, days - 12)
    for i in range(n_rings):
        fn = TYPOLOGIES[i % len(TYPOLOGIES)]
        start = int(rng.integers(first_day, last_start))
        fn(w, f"RING-{i+1:02d}", start, days)
