"""Live evaluation against ground truth. Runs as the read-only aml_eval role and writes only
aggregate numbers to eval_snapshots — the detection side never sees per-account answers.

    python -m ml.evaluate            # one snapshot, printed
    python -m ml.evaluate --loop     # every EVAL_INTERVAL seconds (compose service)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import time
from collections import defaultdict

import psycopg
from psycopg.rows import dict_row

from common.db import conninfo
from common.integrity import canonical

logging.basicConfig(level=logging.INFO, format="%(asctime)s evaluator %(levelname)s %(message)s")
log = logging.getLogger(__name__)

RULE_SCENARIOS = ("S1", "S2", "S3", "S4", "S5", "S6")


def snapshot(conn) -> dict:
    # Illicit accounts count once their laundering has actually started in the ingested stream.
    active = conn.execute(
        "SELECT at.account_id, at.typology, at.ring_id, min(t.ts) AS first_illicit "
        "FROM account_truth at JOIN ground_truth g ON g.ring_id = at.ring_id AND g.label='illicit' "
        "JOIN transactions t ON t.txn_id = g.txn_id WHERE at.label='illicit' "
        "GROUP BY at.account_id, at.typology, at.ring_id").fetchall()
    truth = {r["account_id"]: (r["label"], r["typology"]) for r in
             conn.execute("SELECT account_id, label, typology FROM account_truth").fetchall()}
    alerts = conn.execute("SELECT account_id, scenario, sim_ts FROM alerts").fetchall()

    by_acct: dict[str, set] = defaultdict(set)
    for a in alerts:
        by_acct[a["account_id"]].add(a["scenario"])

    per_scn = {}
    for scn in (*RULE_SCENARIOS, "ML"):
        accts = {a["account_id"] for a in alerts if a["scenario"] == scn}
        tp = sum(1 for x in accts if truth.get(x, ("",))[0] == "illicit")
        la = sum(1 for x in accts if truth.get(x, ("",))[0] == "lookalike")
        per_scn[scn] = {"accounts_alerted": len(accts), "true_positive": tp, "lookalike_fp": la,
                        "other_fp": len(accts) - tp - la,
                        "precision": round(tp / len(accts), 3) if accts else None}

    per_typ: dict[str, dict] = defaultdict(lambda: {"active": 0, "caught_rules": 0, "caught_any": 0})
    for r in active:
        d = per_typ[r["typology"]]
        d["active"] += 1
        sc = by_acct.get(r["account_id"], set())
        d["caught_any"] += bool(sc)
        d["caught_rules"] += bool(sc & set(RULE_SCENARIOS))
    for d in per_typ.values():
        d["recall_rules"] = round(d["caught_rules"] / d["active"], 3)
        d["recall_rules_plus_ml"] = round(d["caught_any"] / d["active"], 3)

    rings: dict[str, dict] = {}
    for r in active:
        g = rings.setdefault(r["ring_id"], {"typology": r["typology"], "start": r["first_illicit"],
                                            "members": set(), "first_alert": None})
        g["start"] = min(g["start"], r["first_illicit"])
        g["members"].add(r["account_id"])
    for g in rings.values():
        # first alert on any ring member at or after the ring's first illicit txn
        times = [a["sim_ts"] for a in alerts if a["account_id"] in g["members"] and a["sim_ts"] >= g["start"]]
        g["first_alert"] = min(times, default=None)
    ttd = {k: {"typology": v["typology"],
               "detected": v["first_alert"] is not None,
               "minutes_to_detect": round((v["first_alert"] - v["start"]).total_seconds() / 60)
               if v["first_alert"] else None} for k, v in sorted(rings.items())}

    lookalikes: dict[str, dict] = defaultdict(lambda: {"accounts": 0, "alerted": 0})
    for acct, (label, typ) in truth.items():
        if label == "lookalike":
            lookalikes[typ]["accounts"] += 1
            lookalikes[typ]["alerted"] += acct in by_acct

    all_alerted = set(by_acct)
    tp_all = sum(1 for x in all_alerted if truth.get(x, ("",))[0] == "illicit")
    n_active = len(active)
    caught = sum(1 for r in active if r["account_id"] in by_acct)
    return {
        "overall": {"accounts_alerted": len(all_alerted), "true_positive": tp_all,
                    "precision": round(tp_all / len(all_alerted), 3) if all_alerted else None,
                    "illicit_accounts_active": n_active,
                    "recall": round(caught / n_active, 3) if n_active else None},
        "by_scenario": per_scn, "by_typology": dict(per_typ), "time_to_detect": ttd,
        "lookalikes": dict(lookalikes),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true")
    args = ap.parse_args()
    interval = int(os.getenv("EVAL_INTERVAL", "15"))
    while True:
        try:
            with psycopg.connect(conninfo(), row_factory=dict_row) as conn:
                snap = snapshot(conn)
                if args.loop:
                    conn.execute("INSERT INTO eval_snapshots (payload) VALUES (%s::jsonb)", (canonical(snap),))
                else:
                    print(json.dumps(snap, indent=2, default=str))
        except Exception:
            log.exception("evaluation failed")
        if not args.loop:
            return
        time.sleep(interval)


if __name__ == "__main__":
    main()
