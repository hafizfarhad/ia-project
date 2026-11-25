"""Replay a generated dataset in time order and compute features exactly as serving would."""
from __future__ import annotations

from collections import defaultdict, deque
from datetime import timedelta

import numpy as np

from ml.features import CUSTOMER_IS_RECEIVER, FEATURES, Hist, compute, vector
from simulator.world import World

DAY = timedelta(hours=24)
WEEK = timedelta(days=7)


def feature_matrix(w: World):
    hist: dict[str, deque[Hist]] = defaultdict(deque)
    pairs: set[tuple[str, str]] = set()
    devices: dict[str, dict[str, object]] = defaultdict(dict)   # device -> {sender: last_ts}
    X, y, typ = [], [], []

    for t in w.txns:
        ts, s, r = t["ts"], t["sender_id"], t["receiver_id"]
        for a in (s, r):
            dq = hist[a]
            while dq and dq[0].ts < ts - DAY:
                dq.popleft()
        dev_n = 0
        if t["device_id"]:
            seen = devices[t["device_id"]]
            seen[s] = ts
            dev_n = sum(1 for v in seen.values() if v > ts - WEEK)

        cust = w.accounts[r if t["txn_type"] in CUSTOMER_IS_RECEIVER else s]
        f = compute(t, tier_limit=w.daily_limit(cust.tier), acct_opened=cust.opened_at,
                    s_hist=list(hist[s]), r_hist=list(hist[r]),
                    first_pair=(s, r) not in pairs, device_wallets=dev_n)
        X.append(vector(f))
        label = w.txn_truth.get(t["txn_id"])
        y.append(1 if label and label[0] == "illicit" else 0)
        typ.append(label[1] if label else "NORMAL")

        h = Hist(ts, s, r, float(t["amount"]))
        hist[s].append(h)
        if r != s:
            hist[r].append(h)
        pairs.add((s, r))

    return np.asarray(X, dtype=np.float32), np.asarray(y), np.asarray(typ), FEATURES
