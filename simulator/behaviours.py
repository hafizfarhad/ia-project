"""Normal wallet behaviour and benign look-alikes (committees, salaries, family chains, shared phones)."""
from __future__ import annotations

from datetime import timedelta

import numpy as np

from simulator.world import World

# per-tier daily Poisson rates and median amounts (PKR)
P2P_RATE = {"L0": 0.25, "L1": 0.40, "L2": 0.60}
P2P_MEDIAN = {"L0": 2500, "L1": 5000, "L2": 12000}
CASHIN_RATE = {"L0": 0.12, "L1": 0.12, "L2": 0.10}
CASHIN_MEDIAN = {"L0": 4000, "L1": 9000, "L2": 25000}
CASHOUT_RATE = {"L0": 0.08, "L1": 0.10, "L2": 0.10}
TOPUP_RATE = 0.25
IBFT_RATE = {"L0": 0.0, "L1": 0.04, "L2": 0.08}


def _amount(rng: np.random.Generator, median: float, cap: float, sigma: float = 0.8) -> float:
    a = float(rng.lognormal(np.log(median), sigma))
    a = min(a, cap)
    return max(100.0, round(a / 50) * 50)


def normal_behaviour(w: World, days: int, eid_day: int | None) -> None:
    rng = w.rng
    customers = w.of_kind("customer")
    billers = [b.account_id for b in w.of_kind("biller")]
    telcos = [b for b in billers if "Telco" in w.accounts[b].name] or billers
    banks = [x.account_id for x in w.of_kind("external")]
    bills = {c.account_id: [(int(rng.integers(5, 16)), str(rng.choice(billers)))
                            for _ in range(int(rng.integers(1, 4)))] for c in customers}

    for d in range(days):
        date = w.start + timedelta(days=d)
        eid = eid_day is not None and eid_day <= d < eid_day + 3
        for c in customers:
            cid, tier = c.account_id, c.tier
            cap = w.daily_limit(tier) * 0.8
            # P2P to contacts (Eid: more sends, bigger "Eidi")
            rate = P2P_RATE[tier] * (3 if eid else 1)
            for _ in range(rng.poisson(rate)):
                if not c.contacts:
                    break
                amt = _amount(rng, P2P_MEDIAN[tier] * (1.5 if eid else 1), cap)
                w.txn(w.at(d, w.rand_hour()), "P2P", cid, str(rng.choice(c.contacts)), amt)
            for _ in range(rng.poisson(TOPUP_RATE)):
                w.txn(w.at(d, w.rand_hour()), "TOPUP", cid, str(rng.choice(telcos)),
                      float(rng.choice([100, 200, 300, 500, 1000])))
            for day_of_month, biller in bills[cid]:
                if date.day == day_of_month:
                    w.txn(w.at(d, w.rand_hour()), "BILL", cid, biller,
                          _amount(rng, {"L0": 2500, "L1": 5000, "L2": 12000}[tier], cap, 0.5))
            for _ in range(rng.poisson(CASHIN_RATE[tier])):
                w.txn(w.at(d, w.rand_hour()), "CASH_IN", c.home_agent, cid,
                      _amount(rng, CASHIN_MEDIAN[tier], cap, 0.7), device="sender")
            for _ in range(rng.poisson(CASHOUT_RATE[tier])):
                w.txn(w.at(d, w.rand_hour()), "CASH_OUT", cid, c.home_agent,
                      _amount(rng, CASHIN_MEDIAN[tier] * 0.8, cap, 0.7))
            for _ in range(rng.poisson(IBFT_RATE[tier])):
                bank = str(rng.choice(banks))
                if rng.random() < 0.5:
                    w.txn(w.at(d, w.rand_hour()), "IBFT_OUT", cid, bank, _amount(rng, P2P_MEDIAN[tier] * 2, cap))
                else:
                    w.txn(w.at(d, w.rand_hour()), "IBFT_IN", bank, cid,
                          _amount(rng, P2P_MEDIAN[tier] * 2, cap), device=None)


def committees(w: World, days: int, n: int = 8) -> None:
    """ROSCA: members pay a fixed amount to the organiser early each month; the pot goes to one member."""
    rng = w.rng
    for k in range(n):
        ring = f"COMMITTEE-{k+1}"
        organiser = w.take(1, tier="L2")[0]
        members = w.take(int(rng.integers(10, 21)))
        share = float(rng.choice([5000, 10000, 20000]))
        w.mark([organiser], "lookalike", "COMMITTEE", ring)
        turn = 0
        for d in range(days):
            date = w.start + timedelta(days=d)
            if date.day == 1:
                for m in members:
                    pay_day = d + int(rng.choice([0, 0, 0, 1, 2]))
                    if pay_day < days:
                        w.txn(w.at(pay_day, w.rand_hour()), "P2P", m, organiser, share,
                              truth=("lookalike", "COMMITTEE", ring))
                if d + 5 < days:
                    w.txn(w.at(d + 5, 11 + rng.random() * 6), "P2P", organiser, members[turn % len(members)],
                          share * len(members), truth=("lookalike", "COMMITTEE", ring))
                    turn += 1


def salaries(w: World, days: int) -> None:
    """Employers are funded by one bank transfer, then fan out salaries on the 1st."""
    rng = w.rng
    banks = [x.account_id for x in w.of_kind("external")]
    for emp in w.of_kind("employer"):
        ring = f"SALARY-{emp.account_id}"
        staff = w.take(int(rng.integers(10, 51)))
        for s in staff:
            if w.accounts[s].tier == "L0":
                w.accounts[s].tier = "L1"
        pay = {s: float(round(rng.uniform(25000, 80000), -3)) for s in staff}
        w.mark([emp.account_id], "lookalike", "SALARY", ring)
        for d in range(days):
            date = w.start + timedelta(days=d)
            if date.day == 1:
                if d >= 1:
                    w.txn(w.at(d - 1, 15 + rng.random() * 3), "IBFT_IN", str(rng.choice(banks)),
                          emp.account_id, sum(pay.values()) * 1.02, device=None,
                          truth=("lookalike", "SALARY", ring))
                for s in staff:
                    t = w.txn(w.at(d, 9 + rng.random() * 3), "SALARY", emp.account_id, s, pay[s],
                              truth=("lookalike", "SALARY", ring))
                    if rng.random() < 0.3:   # some staff withdraw part of it the same day
                        w.txn(t["ts"] + timedelta(hours=float(rng.uniform(1, 8))), "CASH_OUT", s,
                              w.accounts[s].home_agent, round(pay[s] * rng.uniform(0.4, 0.6), -2))


def family_chains(w: World, days: int, n: int = 30) -> None:
    """Son sends money home; the parent forwards part of it to a sibling a few days later."""
    rng = w.rng
    for k in range(n):
        ring = f"FAMILY-{k+1}"
        son, parent, sibling = w.take(3)
        w.accounts[son].tier = "L2"
        w.accounts[parent].tier = "L1"
        w.mark([son, parent, sibling], "lookalike", "FAMILY", ring)
        send_day = int(rng.integers(1, 8))
        for d in range(days):
            if (w.start + timedelta(days=d)).day == send_day:
                amt = float(round(rng.uniform(20000, 40000), -3))
                t = w.txn(w.at(d, w.rand_hour()), "P2P", son, parent, amt, truth=("lookalike", "FAMILY", ring))
                lag = timedelta(days=float(rng.uniform(1, 3)))
                w.txn(t["ts"] + lag, "P2P", parent, sibling, round(amt * rng.uniform(0.3, 0.6), -2),
                      truth=("lookalike", "FAMILY", ring))


def shared_phones(w: World, n_pairs: int = 30, n_triples: int = 10) -> None:
    """Family members sharing one handset. Triples will (legitimately) trip S6."""
    for k in range(n_pairs + n_triples):
        size = 2 if k < n_pairs else 3
        members = w.take(size)
        dev = w.accounts[members[0]].device_id
        city = w.accounts[members[0]].city
        for m in members[1:]:
            w.accounts[m].device_id = dev
            w.accounts[m].city = city
        if size == 3:
            w.mark(members, "lookalike", "SHARED_PHONE", f"PHONE-{k+1}")
