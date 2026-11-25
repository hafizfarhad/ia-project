# WalletWatch

WalletWatch does near-real-time **anti-money-laundering (AML) transaction monitoring** for a synthetic
Pakistani mobile wallet. It has a secure analyst workflow:
**alert → case → close or escalate → STR drafted → STR approved by the MLRO (Money Laundering Reporting Officer)**.

This is an Information Assurance course project. Scope and design are in [SPEC.md](SPEC.md), and the viva
walkthrough is in [DEMO.md](DEMO.md).

## Quick start

```bash
./scripts/make_env.sh          # random secrets into .env (prints the demo login password)
docker compose up --build      # first run trains the model (~1 min), then streams live
```

| | URL | Notes |
|---|---|---|
| Dashboard | http://localhost:8501 | users `analyst1`, `analyst2`, `mlro1`, `mlro2`, `auditor1`. The password is `DEMO_PASSWORD` in `.env` |
| API docs | http://localhost:8000/docs | |

- **Reset everything:** `docker compose down -v`.
- **Simulation speed:** set it in `.env`. For example, `SIM_SPEED=2880` gives 2 simulated days per real minute. You can also change `SIM_DAYS` and `SIM_SEED` there.

## How it works

```
simulator ──ingest token───▶ api ── rules S1 S2 S3 S6 ──┐
 (2,000 customers,                │  ── XGBoost + SHAP ──────┤──▶ alerts ──▶ cases ──▶ STRs
  10 laundering rings,            │                          │
  benign look-alikes)       graph worker ── S4 chains, S5 round-trips
                                  │
             postgres ◀───────────┘           dashboard (Streamlit) ──JWT──▶ api only
             evaluator (read-only role) ──▶ precision / recall vs ground truth
```

| Scenario | Detects | Engine |
|---|---|---|
| S1 Structuring | Repeated incoming amounts just under the tier daily limit | rules |
| S2 Fan-in / fan-out | Mule account: many senders in, many receivers out, same day | rules |
| S3 Rapid pass-through | ≥ 90% of a large credit leaves within an hour, repeatedly | rules |
| S4 Layering chain | Money hops through ≥ 4 accounts within 6 h, with a small amount skimmed at each hop | graph |
| S5 Round-trip | Money goes round 3–6 accounts and comes back within 72 h | graph |
| S6 Shared device | ≥ 3 wallets operated from one handset | rules |
| ML | XGBoost risk ≥ 0.8 on a transaction no rule caught, explained with SHAP | ml |

**Benign look-alikes** create honest false positives, so the results aren't circular: committees (ROSCA),
salary runs, family remittance chains, and shared family phones.

## Information Assurance controls

| Control | Where |
|---|---|
| Least-privilege DB roles (`aml_app` can't read ground truth or delete anything) | `db/init/02_roles.sh` |
| bcrypt passwords, short-lived JWT, uniform login errors | `api/security.py` |
| RBAC: analyst / mlro / auditor; denials are audited | `api/security.py`, `api/cases.py` |
| Maker-checker on STRs (API **and** DB constraint) | `api/cases.py`, `db/init/01_schema.sql` |
| PII masking; unmasking is MLRO-only, needs a reason, and is audited | `api/masking.py`, `/pii/unmask` |
| Append-only audit log (trigger) + HMAC-SHA256 hash chain keyed by the API + verify | `api/audit.py` |
| Alert evidence frozen and hashed at creation, then re-verified on view | `common/alerts.py` |
| Separate ingest token for the simulator; secrets never reach other containers | `docker-compose.yml` |
| Ports bound to localhost; containers run as non-root | `docker-compose.yml`, `Dockerfile` |

## Tests and lint

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt ruff==0.8.4
.venv/bin/pytest -q            # 38 tests: unit always; DB, API, ingest and dashboard when the stack is up
.venv/bin/ruff check .         # style and bug lint (config in pyproject.toml)
```

The security tests attack each control on purpose: forged and missing tokens, cross-role calls,
self-approval of an STR, reading ground truth as the app role, editing the audit log, and rewriting
the whole audit chain without the API's key. Everything they change is cleaned up afterwards.

## Report, proposal and slides

```bash
./report/build.sh --refresh    # re-export results from the running system, then build both PDFs
```

This produces `report/WalletWatch-Report.pdf` and `report/WalletWatch-Proposal.pdf` (LaTeX with
IEEEtran; TikZ figures in `report/figures/`). Every number in the report's evaluation comes from
`scripts/export_results.py`, which reads the evaluator's latest snapshot.

## Layout

```
simulator/   world, normal behaviour + look-alikes, laundering typologies, live/history runner
api/         FastAPI: ingest, rules, ML scoring, cases/STRs, monitor, audit, security
worker/      graph worker (S4, S5)
ml/          shared features, training, live evaluator
dashboard/   Streamlit UI (API client only)
db/          schema, roles, user seeding
config/      tier limits and every detection threshold
scripts/     secret generation, tamper demo, results export
report/      LaTeX report and proposal, TikZ figures, bibliography
tests/       unit, database, API security, ingest and dashboard tests
```

## Limitations

- The data is synthetic, and the rules were designed alongside the simulator, so the scores are optimistic. Look-alikes and a held-out seed reduce this but do not remove it.
- The tier limits are illustrative (based on SBP 2016). The CTR threshold (PKR 3M, SBP BPRD CL24 2026) is documented but not implemented.
- The STR export is Markdown, not goAML XML.
