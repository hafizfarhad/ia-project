# WalletWatch — Real-time AML Transaction Monitoring for a Mobile Wallet

> Information Assurance course project. Synthetic data only. Small and scoped on purpose:
> anything not in this file is out of scope until the core is done (see §10 Stretch).

## 1. Goal

Watch a live stream of synthetic Pakistani mobile-wallet transactions, detect money-laundering
typologies in near real time, and give compliance staff a secure workflow:
**alert → case → close with reason / escalate → STR drafted → STR approved by MLRO.**

"Near real time" = every transaction is checked on arrival (rules over rolling windows);
network patterns (chains, cycles) are checked by a graph worker every few seconds.

## 2. Synthetic world (simulator)

| Entity | Count | Notes |
|---|---|---|
| Customers | 2,000 | Tier L0 / L1 / L2, city, device_id, CNIC, MSISDN, opened_at |
| Agents | 50 | Cash-in / cash-out points |
| Billers / merchants | 20 | Bill payments, top-ups |
| Employers | 5 | Pay monthly salaries (look-alike) |
| Committees (ROSCA) | 8 | 10–20 members each (look-alike) |
| Laundering rings | 10 | Each runs one typology from §4 |

**Transaction types:** `P2P`, `CASH_IN`, `CASH_OUT`, `BILL`, `TOPUP`, `IBFT_OUT`, `IBFT_IN`, `SALARY`.
All amounts are in PKR. Tier limits are in `config/limits.yaml` and are *illustrative* (based on SBP 2016 values):

| Tier | Daily | Monthly | Max balance |
|---|---|---|---|
| L0 | 25,000 | 40,000 | 200,000 |
| L1 | 50,000 | 80,000 | 400,000 |
| L2 | 500,000 | 2,000,000 | none |

**Normal behaviour:** top-ups, bills, small P2P sends to a stable set of contacts, and cash-in/out at
a home agent. There is a day/night cycle, and Eid weeks have spikes.

**Benign look-alikes:** these exist to create honest false positives.
- **Committee:** members pay a fixed amount to an organiser on the same day each month (fan-in). The organiser then pays the whole pot to one member.
- **Salary:** an employer fans out to 10–50 staff on the 1st of the month.
- **Family chain:** a son in Karachi sends to a parent in Multan, who forwards part of it to a sibling over a few days. This is a slow chain.
- **Shared family phone:** 2 or 3 wallets on one device.

**Modes:**
- `history`: generates 60 simulated days to Parquet, used for ML training and evaluation.
- `live`: streams transactions to the API. The default speed is 1 simulated day per minute, and it is adjustable.

**Ground truth:** stored in a separate `ground_truth(txn_id, typology, ring_id)` table.
The engine **never reads it**; only evaluation does.

## 3. Architecture

```
simulator ──HTTP (service token)──▶ api (FastAPI)
                                     ├─ rules engine (on ingest, rolling windows via SQL)
                                     ├─ ML scorer (XGBoost + SHAP)
                                     └─ auth · RBAC · masking · audit
graph worker ── every 5 s ──▶ postgres ◀── api
dashboard (Streamlit) ──JWT──▶ api      (dashboard never touches the DB)
```

There are four long-running containers (`postgres`, `api`, `worker`, `dashboard`) and one job container (`simulator`).
`docker compose up` starts the whole demo.

## 4. Detection: the 6 core scenarios

All thresholds live in `config/rules.yaml` (shown here as defaults).

| ID | Typology | Where | Fires when |
|---|---|---|---|
| **S1** | Structuring | rules | ≥ 3 incoming txns in 7 days, each between 85% and 100% of the account's tier *daily* limit |
| **S2** | Fan-in / fan-out (mule) | rules | Within 24 h, the account receives from ≥ 8 distinct senders **and** sends to ≥ 5 distinct receivers or cash-outs, **and** out ≥ 80% of in |
| **S3** | Rapid pass-through | rules | Incoming ≥ 20,000, and ≥ 90% of it leaves within 60 min. This happens ≥ 2 times in 7 days |
| **S4** | Layering chain | graph | A path of ≥ 4 hops within 6 h, where each hop is 85–100% of the previous hop's amount |
| **S5** | Round-trip | graph | A cycle of 3–6 accounts within 72 h, where the amount returned is ≥ 70% of the amount sent |
| **S6** | Shared device | rules | ≥ 3 wallets on the same `device_id` are active within 7 days |
| **ML** | Model risk | ml | XGBoost probability ≥ 0.80 on a transaction that no rule has flagged |

**What the look-alikes test:**
- The committee organiser resembles S2, but there is only one payee, so it fails the out-receiver count.
- Salary has a single funding source, so it fails the fan-in.
- The family chain is slow and its amounts shrink a lot, so it fails S4's 6 h window and 85% hop rule.
- A shared family phone with 3 wallets is a true false positive, and the analyst closes it with a reason.

**ML features (per transaction):**
- log amount, and amount ÷ tier daily limit
- sender txn count in 1 h / 24 h; distinct in/out counterparties in 24 h
- first-time sender→receiver pair (yes/no)
- minutes since sender's last incoming txn; share of the incoming amount forwarded
- hour of day, txn type, account age in days, wallets on the same device

**Training data:** the model is trained on `history` and evaluated on a second `history` generated with a different seed.
SHAP shows the top 5 features for each scored transaction.

## 5. Alerts and cases

- **Alert:** `(account, scenario, score, evidence JSON, evidence_hash)`. The evidence (the triggering txns and the numbers that crossed the threshold) is **frozen at creation**, and `evidence_hash = SHA-256(evidence)`.
- **Case:** one open case per account. New alerts on that account join the open case.
- **Priority:** the max alert score. Rule alerts score 0.7 and graph alerts score 0.9; when several scenarios hit the same account, the case score gets a bonus.

Case states:

```
OPEN ──analyst──▶ CLOSED_FALSE_POSITIVE   (reason required, min 20 chars)
  └──analyst──▶ ESCALATED ──analyst drafts──▶ STR_PENDING
                                                 ├─MLRO approve──▶ STR_FILED
                                                 └─MLRO reject───▶ OPEN (with comment)
```

**STR draft:** generated from a template, covering the subject (masked), the scenarios, the txn table, the timeline, and the analyst's narrative.
It is exported as text/Markdown; goAML XML is a stretch goal.

## 6. Information Assurance controls

| Control | Implementation | CIA |
|---|---|---|
| Authentication | bcrypt password hashes, JWT (30 min), and users seeded by script | C |
| RBAC | Roles: `analyst`, `mlro`, `auditor`. Checked in the API on every endpoint (matrix below) | C, I |
| Separation of duties | Maker-checker: the STR approver must differ from the drafter, enforced in the API **and** by a DB CHECK | I |
| PII masking | CNIC `35202-*****-3`, MSISDN `0300-***-4567`. `POST /pii/unmask` needs a role + reason and is audited | C |
| Tamper-evident audit log | Append-only. `mac = HMAC-SHA256_K(prev_mac ‖ entry)`, with key K held only by the API. `GET /audit/verify` recomputes the chain and reports the first broken link | I |
| Evidence integrity | The alert evidence hash is re-checked when a case is opened | I |
| Service auth | The simulator uses a separate ingest token and cannot call user endpoints | C, I |
| Availability | Compose healthchecks and `restart: unless-stopped`. Ingest is idempotent (txn_id is unique) | A |

**RBAC matrix**

| Action | analyst | mlro | auditor |
|---|---|---|---|
| View feed / alerts / cases | ✓ | ✓ | ✓ (read) |
| Close / escalate case, draft STR | ✓ | ✓ | ✗ |
| Approve / reject STR | ✗ | ✓ (not own draft) | ✗ |
| Unmask PII (with reason) | ✗ | ✓ | ✗ |
| View audit log, verify chain | ✗ | ✗ | ✓ |

**Audited actions:** login (success and failure), case view, disposition, STR draft/approve/reject, unmask, and audit verify.

## 7. Dashboard screens

1. **Login**
2. **Live monitor:** a txn feed showing the last 50, txns per minute, alerts per minute, and scenario counts
3. **Alert queue:** cases sorted by priority, filterable by scenario and status
4. **Case detail:** frozen evidence plus integrity ✓, a transaction timeline, a **money-flow network graph** (2 hops), SHAP bars, and action buttons for the user's role
5. **STR approvals** (MLRO): pending drafts, approve or reject
6. **Audit** (auditor): the log table plus a "Verify integrity" button
7. **Metrics:** alerts per scenario, false-positive rate, alert→STR conversion, and precision/recall against ground truth

## 8. Evaluation (for the slides)

- Per scenario: **precision, recall, and time-to-detect**, measured in simulated minutes from the first illicit txn.
- ML: PR-AUC on the held-out seed, compared with rules-only and with rules + ML.
- Look-alike analysis: how many committees, salaries and families were flagged, and why.
- **Stated limitation:** the data is synthetic and the rules were designed against it, so the numbers are optimistic. The look-alikes and the held-out seed reduce this but do not remove it.

## 9. Repo layout & build phases

```
aml-monitoring/
  config/         limits.yaml, rules.yaml
  simulator/      world.py, behaviours.py, typologies.py, run.py
  api/            main.py, auth.py, rbac.py, audit.py, masking.py, ingest.py, rules.py, cases.py, ml.py
  worker/         graph_worker.py
  ml/             features.py, train.py, evaluate.py
  dashboard/      app.py, pages/
  db/             schema.sql, seed_users.py
  docker-compose.yml
```

| Phase | Done when |
|---|---|
| 0 Skeleton | `docker compose up` starts postgres, api (/health) and a dashboard placeholder; the schema is applied |
| 1 Simulator | `history` produces 60 days of data plus ground truth, and the typology and look-alike counts are sane |
| 2 Ingest + rules | Live stream → S1, S2, S3 and S6 alerts appear in the DB with frozen evidence |
| 3 Security | Login, RBAC, masking, audit chain and verify all work, tested with pytest |
| 4 Graph worker | S4 and S5 alerts fire on planted rings |
| 5 Dashboard + cases | The full alert → STR → MLRO flow can be clicked through |
| 6 ML | The model is trained, scores on ingest, and SHAP shows in the case view |
| 7 Evaluation + slides | The metrics page and the slide deck are done |

## 10. Stretch (not now)

Dormant-account and agent-anomaly scenarios, watchlist/PEP screening, Redis Streams, an anomaly model,
retraining from analyst labels, goAML XML export, field-level encryption, account lockout, a user-management UI.

## 11. Context / references

- CTR is an automatic report on cash ≥ PKR 3M (SBP BPRD CL24, Nov 2026; previously 2M). It is rarely hit at wallet tiers, so it is explained on a slide rather than implemented.
- STRs and CTRs are filed with the FMU through goAML.
- The methodology follows Altman et al., *Realistic Synthetic Financial Transactions for AML Models*, NeurIPS 2023 (AMLworld).
