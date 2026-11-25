"""Online ML scoring: fetch the same history the training replay sees, compute features,
score with XGBoost, explain with SHAP values (XGBoost's built-in TreeSHAP, pred_contribs)."""
from __future__ import annotations

import json
import logging
from datetime import timedelta
from pathlib import Path

import numpy as np
import xgboost as xgb
from psycopg import Connection

from common.config import load_yaml, settings
from ml.features import CUSTOMER_IS_RECEIVER, FEATURES, Hist, compute, vector

log = logging.getLogger(__name__)


class Scorer:
    def __init__(self) -> None:
        self.booster: xgb.Booster | None = None
        self.meta: dict = {}
        d = Path(settings().model_dir)
        if (d / "model.json").exists():
            self.booster = xgb.Booster()
            self.booster.load_model(d / "model.json")
            self.meta = json.loads((d / "meta.json").read_text())
            log.info("loaded model %s", self.meta.get("version"))
        else:
            log.warning("no model at %s; ML scoring disabled", d)

    @property
    def version(self) -> str | None:
        return self.meta.get("version")

    def features(self, conn: Connection, txn: dict, sender: dict, receiver: dict) -> dict:
        ts, s, r = txn["ts"], txn["sender_id"], txn["receiver_id"]
        since = ts - timedelta(hours=24)

        def hist(a: str) -> list[Hist]:
            rows = conn.execute(
                "SELECT ts, sender_id, receiver_id, amount FROM transactions "
                "WHERE (sender_id=%(a)s OR receiver_id=%(a)s) AND ts >= %(s)s AND ts <= %(t)s "
                "AND txn_id <> %(id)s", {"a": a, "s": since, "t": ts, "id": txn["txn_id"]}).fetchall()
            return [Hist(x["ts"], x["sender_id"], x["receiver_id"], float(x["amount"])) for x in rows]

        first_pair = conn.execute(
            "SELECT 1 FROM transactions WHERE sender_id=%s AND receiver_id=%s AND ts <= %s "
            "AND txn_id <> %s LIMIT 1", (s, r, ts, txn["txn_id"])).fetchone() is None
        dev_n = 0
        if txn.get("device_id"):
            dev_n = conn.execute(
                "SELECT count(DISTINCT sender_id) AS n FROM transactions WHERE device_id=%s "
                "AND ts > %s AND ts <= %s", (txn["device_id"], ts - timedelta(days=7), ts)).fetchone()["n"]
        cust = receiver if txn["txn_type"] in CUSTOMER_IS_RECEIVER else sender
        limits = load_yaml("limits")["tiers"]
        return compute(txn, tier_limit=float(limits[cust["tier"] or "L2"]["daily"]),
                       acct_opened=cust["opened_at"], s_hist=hist(s), r_hist=hist(r),
                       first_pair=first_pair, device_wallets=dev_n)

    def score(self, f: dict) -> tuple[float, dict]:
        """Returns (probability, top-5 SHAP contributions in log-odds)."""
        dm = xgb.DMatrix(np.asarray([vector(f)], dtype=np.float32), feature_names=FEATURES)
        p = float(self.booster.predict(dm)[0])
        contrib = self.booster.predict(dm, pred_contribs=True)[0]   # last column = bias
        pairs = sorted(zip(FEATURES, contrib[:-1], strict=False), key=lambda kv: -abs(kv[1]))[:5]
        return p, {k: {"shap": round(float(v), 3), "value": round(float(f[k]), 3)} for k, v in pairs}
