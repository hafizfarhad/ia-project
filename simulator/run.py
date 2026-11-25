"""Simulator entry point.

  python -m simulator.run history --seed 1 --days 60      # write Parquet for offline work
  python -m simulator.run live                             # seed DB + stream to the API

Live-mode env: SIM_SEED, SIM_DAYS, SIM_SPEED (simulated seconds per real second; 1440 = 1 day/min),
SIM_WARMUP_DAYS (streamed unpaced at start so there is history to compare against).
"""
from __future__ import annotations

import argparse
import logging
import os
import time
from datetime import datetime
from pathlib import Path

import psycopg
import requests

from common.config import settings
from common.db import conninfo
from simulator.generate import generate, summary

logging.basicConfig(level=logging.INFO, format="%(asctime)s simulator %(levelname)s %(message)s")
log = logging.getLogger(__name__)
BATCH = 200


def history(seed: int, days: int, out: Path) -> None:
    import pandas as pd
    w = generate(seed, days)
    out.mkdir(parents=True, exist_ok=True)
    tx = pd.DataFrame(w.txns)
    tx["label"] = tx["txn_id"].map(lambda t: w.txn_truth.get(t, ("normal",))[0])
    tx["typology"] = tx["txn_id"].map(lambda t: w.txn_truth.get(t, (None, None))[1])
    tx.to_parquet(out / f"txns_seed{seed}.parquet", index=False)
    accounts = pd.DataFrame([a.db_row() for a in w.accounts.values()])
    accounts.to_parquet(out / f"accounts_seed{seed}.parquet", index=False)
    log.info("wrote %s: %s", out, summary(w))


def seed_db(w) -> None:
    """Accounts + ground truth go straight to Postgres as the restricted aml_sim role."""
    with psycopg.connect(conninfo()) as conn:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO accounts (account_id, kind, name, cnic, msisdn, tier, city, device_id, opened_at) "
                "VALUES (%(account_id)s, %(kind)s, %(name)s, %(cnic)s, %(msisdn)s, %(tier)s, %(city)s, "
                "%(device_id)s, %(opened_at)s) ON CONFLICT DO NOTHING",
                [a.db_row() for a in w.accounts.values()])
            cur.executemany("INSERT INTO ground_truth VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                            [(k, *v) for k, v in w.txn_truth.items()])
            cur.executemany("INSERT INTO account_truth VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                            [(k, *v) for k, v in w.acct_truth.items()])
    log.info("seeded %d accounts, %d truth labels", len(w.accounts), len(w.txn_truth))


def _post(session: requests.Session, url: str, txns: list[dict]) -> dict:
    body = {"txns": [{**t, "ts": t["ts"].isoformat()} for t in txns]}
    for attempt in range(10):
        try:
            r = session.post(url, json=body, timeout=60)
            if 400 <= r.status_code < 500:
                # The API rejected our data: retrying won't help, and hiding it would be worse.
                raise SystemExit(f"ingest rejected ({r.status_code}): {r.text[:500]}")
            r.raise_for_status()
            return r.json()
        except requests.RequestException as exc:
            wait = min(30, 2 ** attempt)
            log.warning("ingest failed (%s); retrying in %ss", type(exc).__name__, wait)
            time.sleep(wait)
    raise SystemExit("API unavailable; giving up")


def live() -> None:
    seed = int(os.getenv("SIM_SEED", "42"))
    days = int(os.getenv("SIM_DAYS", "30"))
    speed = float(os.getenv("SIM_SPEED", "1440"))
    warmup = float(os.getenv("SIM_WARMUP_DAYS", "3"))
    s = settings()

    w = generate(seed, days)
    log.info("world ready: %s", summary(w))
    seed_db(w)

    session = requests.Session()
    session.headers["X-Ingest-Token"] = s.ingest_token
    status = session.get(f"{s.api_url}/ingest/status", timeout=10).json()
    resume = datetime.fromisoformat(status["last_ts"]) if status["last_ts"] else None
    txns = [t for t in w.txns if resume is None or t["ts"] > resume]
    if resume:
        log.info("resuming after %s (%d txns already ingested)", resume, status["count"])
    if not txns:
        log.info("nothing left to stream")
        return

    url = f"{s.api_url}/ingest/batch"
    warm_end = w.start.timestamp() + warmup * 86400
    sim0, real0 = None, None
    buf: list[dict] = []
    totals = {"stored": 0, "alerts": 0}

    def flush() -> None:
        if buf:
            r = _post(session, url, buf)
            totals["stored"] += r["stored"]
            totals["alerts"] += r["alerts"]
            buf.clear()

    for t in txns:
        ts = t["ts"].timestamp()
        if ts > warm_end:
            if sim0 is None:
                log.info("warm-up done (%d txns); streaming at %gx real time", totals["stored"], speed)
                sim0, real0 = ts, time.time()
            due = real0 + (ts - sim0) / speed
            delay = due - time.time()
            if delay > 0.25:
                flush()
                while (left := due - time.time()) > 0:
                    time.sleep(min(left, 1.0))
        buf.append(t)
        if len(buf) >= BATCH:
            flush()
            if totals["stored"] % 5000 < BATCH:
                log.info("sim time %s · %d txns · %d alerts", t["ts"].strftime("%Y-%m-%d %H:%M"),
                         totals["stored"], totals["alerts"])
    flush()
    log.info("stream finished: %d txns, %d alerts", totals["stored"], totals["alerts"])


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="mode", required=True)
    h = sub.add_parser("history")
    h.add_argument("--seed", type=int, default=1)
    h.add_argument("--days", type=int, default=60)
    h.add_argument("--out", type=Path, default=Path("data"))
    sub.add_parser("live")
    args = ap.parse_args()
    if args.mode == "history":
        history(args.seed, args.days, args.out)
    else:
        live()


if __name__ == "__main__":
    main()
