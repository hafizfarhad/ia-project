"""Per-transaction features. ONE implementation used by both training (replaying history in
memory) and serving (history fetched from Postgres), so the two can't drift apart."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

TXN_TYPES = ["P2P", "CASH_IN", "CASH_OUT", "BILL", "TOPUP", "IBFT_OUT", "IBFT_IN", "SALARY"]
# For these types the customer is the receiver; otherwise the sender.
CUSTOMER_IS_RECEIVER = {"CASH_IN", "IBFT_IN", "SALARY"}

FEATURES = [
    "log_amount", "amt_to_limit",
    "s_out_1h", "s_out_24h", "s_in_cp_24h", "s_out_cp_24h", "s_in_amt_24h_log",
    "r_in_cp_24h", "r_in_24h", "r_in_amt_24h_log",
    "first_pair", "mins_since_in", "fwd_ratio_1h",
    "hour", "acct_age_days", "device_wallets_7d",
    *[f"type_{t}" for t in TXN_TYPES],
]


@dataclass
class Hist:
    """A transaction as seen in an account's recent history."""
    ts: datetime
    sender_id: str
    receiver_id: str
    amount: float


def compute(txn: dict, *, tier_limit: float, acct_opened: datetime | None,
            s_hist: list[Hist], r_hist: list[Hist], first_pair: bool, device_wallets: int) -> dict:
    """s_hist / r_hist: transactions touching the sender / receiver in the 24h *before* txn
    (the txn itself excluded). device_wallets: distinct senders on txn's device in 7d incl. this one."""
    ts: datetime = txn["ts"]
    s, r, amt = txn["sender_id"], txn["receiver_id"], float(txn["amount"])
    h1 = ts - timedelta(hours=1)

    s_out = [h for h in s_hist if h.sender_id == s]
    s_in = [h for h in s_hist if h.receiver_id == s]
    r_in = [h for h in r_hist if h.receiver_id == r]
    in_1h = sum(h.amount for h in s_in if h.ts >= h1)
    out_1h = sum(h.amount for h in s_out if h.ts >= h1)
    last_in = max((h.ts for h in s_in), default=None)

    f = {
        "log_amount": math.log1p(amt),
        "amt_to_limit": amt / tier_limit,
        "s_out_1h": sum(1 for h in s_out if h.ts >= h1),
        "s_out_24h": len(s_out),
        "s_in_cp_24h": len({h.sender_id for h in s_in}),
        "s_out_cp_24h": len({h.receiver_id for h in s_out} | {r}),
        "s_in_amt_24h_log": math.log1p(sum(h.amount for h in s_in)),
        "r_in_cp_24h": len({h.sender_id for h in r_in} | {s}),
        "r_in_24h": len(r_in),
        "r_in_amt_24h_log": math.log1p(sum(h.amount for h in r_in) + amt),
        "first_pair": int(first_pair),
        "mins_since_in": min(1440.0, (ts - last_in).total_seconds() / 60) if last_in else 1440.0,
        "fwd_ratio_1h": min(3.0, (out_1h + amt) / in_1h) if in_1h > 0 else 0.0,
        "hour": ts.hour + ts.minute / 60,
        "acct_age_days": (ts - acct_opened).days if acct_opened else 3650,
        "device_wallets_7d": device_wallets,
    }
    for t in TXN_TYPES:
        f[f"type_{t}"] = int(txn["txn_type"] == t)
    return f


def vector(f: dict) -> list[float]:
    return [float(f[k]) for k in FEATURES]
