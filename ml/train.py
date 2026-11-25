"""Train the transaction risk model on synthetic history; evaluate on a world from a different seed.

    python -m ml.train            # skips if models/model.json exists
    python -m ml.train --force
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import xgboost as xgb
from sklearn.metrics import average_precision_score, precision_score, recall_score, roc_auc_score

from common.config import load_yaml
from ml.replay import feature_matrix
from simulator.generate import generate

MODEL_DIR = Path(__file__).resolve().parent.parent / "models"
TRAIN_SEEDS = [11, 12]
TEST_SEED = 13
DAYS = 60


def build(seeds: list[int]):
    Xs, ys, ts = [], [], []
    for s in seeds:
        t0 = time.time()
        w = generate(s, DAYS)
        X, y, typ, names = feature_matrix(w)
        print(f"  seed {s}: {len(y):,} txns, {int(y.sum())} illicit ({time.time()-t0:.0f}s)", flush=True)
        Xs.append(X), ys.append(y), ts.append(typ)
    return np.vstack(Xs), np.concatenate(ys), np.concatenate(ts), names


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    MODEL_DIR.mkdir(exist_ok=True)
    if (MODEL_DIR / "model.json").exists() and not args.force:
        print("model exists; skipping training (use --force to retrain)")
        return

    print("generating training worlds…", flush=True)
    X, y, _, names = build(TRAIN_SEEDS)
    print("generating held-out test world…", flush=True)
    Xt, yt, typ_t, _ = build([TEST_SEED])

    pos = max(int(y.sum()), 1)
    clf = xgb.XGBClassifier(
        n_estimators=400, max_depth=5, learning_rate=0.08, subsample=0.8, colsample_bytree=0.8,
        min_child_weight=2, scale_pos_weight=(len(y) - pos) / pos, eval_metric="aucpr",
        tree_method="hist", random_state=7)
    clf.fit(X, y)
    booster = clf.get_booster()
    booster.feature_names = names

    p = booster.predict(xgb.DMatrix(Xt, feature_names=names))
    thr = float(load_yaml("rules")["ML"]["threshold"])
    pred = p >= thr
    per_typ = {}
    for t in sorted(set(typ_t)):
        m = typ_t == t
        per_typ[t] = {"txns": int(m.sum()), "flagged": int(pred[m].sum()),
                      "flag_rate": round(float(pred[m].mean()), 3)}
    gain = booster.get_score(importance_type="gain")
    meta = {
        "version": datetime.now(UTC).strftime("xgb-%Y%m%d-%H%M%S"),
        "features": names, "threshold": thr,
        "train": {"seeds": TRAIN_SEEDS, "txns": int(len(y)), "illicit": int(y.sum())},
        "test": {
            "seed": TEST_SEED, "txns": int(len(yt)), "illicit": int(yt.sum()),
            "pr_auc": round(float(average_precision_score(yt, p)), 4),
            "roc_auc": round(float(roc_auc_score(yt, p)), 4),
            "precision_at_thr": round(float(precision_score(yt, pred, zero_division=0)), 4),
            "recall_at_thr": round(float(recall_score(yt, pred)), 4),
            "flags_at_thr": int(pred.sum()),
            "by_typology": per_typ,
        },
        "top_features": dict(sorted(((k, round(v, 2)) for k, v in gain.items()),
                                    key=lambda kv: -kv[1])[:10]),
    }
    booster.save_model(MODEL_DIR / "model.json")
    (MODEL_DIR / "meta.json").write_text(json.dumps(meta, indent=2))
    print(json.dumps(meta["test"], indent=2))
    print("top features:", meta["top_features"])


if __name__ == "__main__":
    main()
