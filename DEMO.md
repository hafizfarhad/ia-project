# Viva demo script (about 10 minutes)

**Before the viva:** run `docker compose up` at least 15 minutes ahead, so the stream has built up cases.
Get the password with `grep DEMO_PASSWORD .env`. Use two browser windows: a normal one and a private one,
so you can be logged in as two users at once.

## 1. The problem (30 s)
Money launderers use mobile wallets for three things: **placement** (cash-in at agents), **layering** (moving the
money quickly through mule wallets) and **integration** (getting it back out). Banks and wallets must monitor
transactions and file Suspicious Transaction Reports (STRs) to the FMU through goAML.
WalletWatch does that monitoring on synthetic data.

## 2. Live monitor — `analyst1` (1 min)
- Point out the simulated clock moving, the transaction rate, and the alerts arriving.
- **Alerts by scenario:** six typologies, from three engines (rules, graph and ML).
- Say: "Every transaction is checked within milliseconds of arriving. Network patterns are checked every 5 seconds."

## 3. Investigate a case (3 min)
1. On the **Alert queue**, pick a high-priority case. Ones with several scenarios score highest. An S4 or S5 case is the best to show.
2. **Alerts & evidence:** show the rule that fired and the frozen evidence, marked **✅ evidence intact**.
   Say: "The evidence is hashed with SHA-256 when the alert is created. If anyone alters it later, this check turns red."
3. **Money-flow graph:** follow the chain or cycle. The red node is the subject and the orange nodes are other alerted accounts.
4. **ML alert:** show the SHAP bars. Say: "The model doesn't just give a score; it shows which features pushed the transaction towards suspicious."
5. Point at the CNIC and MSISDN: they're **masked**, because an analyst doesn't need them.

## 4. A false positive (1 min)
Open an **S6 shared-device** case that turns out to be a family. Close it with a reason.
Then try a short reason, which is rejected because at least 20 characters are needed.
Say: "We planted innocent look-alikes on purpose: committees, salary runs, families sharing a phone.
Real systems drown in false positives, and our evaluation shows that honestly."

## 5. Escalate → STR → maker-checker (2 min)
1. As **analyst1**, escalate a real case and draft the STR. The template is pre-filled from the evidence.
2. In the private window, log in as **mlro1** and open **STRs**, then approve it.
   - Optionally, show the block: draft one as `mlro1` and try to approve it as `mlro1`. You get **403, "maker-checker"**.
     The database CHECK constraint would also reject it.
3. Export the filed STR as Markdown, ready for goAML.
4. As **mlro1**, use **Unmask PII**. A reason is required, and the access is logged.

## 6. Audit and tamper evidence (2 min) — `auditor1`
1. **Audit log:** every login, view, decision, unmask and access denial is there.
2. Click **Verify integrity**. It shows ✅ chain intact.
3. In a terminal, run `./scripts/tamper_demo.sh tamper`.
   - First, a normal UPDATE is **blocked by the append-only trigger**.
   - Then a "malicious DBA" disables the trigger and rewrites entry #3.
4. Click **Verify integrity** again. It shows ❌ **TAMPERING DETECTED at entry #3**.
   Say: "We can't stop a superuser editing the table, but we can make sure they can't do it without being detected."
5. Run `./scripts/tamper_demo.sh restore`, and verification passes again.

## 7. Results (1 min) — Metrics page
Show precision and recall against the ground truth, recall by typology (rules vs rules + ML), time to detect,
and which look-alikes were flagged.
Stress: "The evaluator runs under a separate read-only database role. The detection system itself can never
read the answers."

## Likely questions
| Question | Answer |
|---|---|
| Isn't synthetic data circular? | Partly, and we say so. We reduce it with benign look-alikes, a held-out seed for the ML model, and by measuring the false positives instead of hiding them. |
| Why not Kafka? | It's overkill for ~35 transactions per second. HTTP ingest is idempotent (the transaction ID is unique), so replays are safe. Redis Streams or Kafka is the scaling path. |
| Why rules *and* ML? | Rules are explainable and auditable, which regulators require. ML catches variants the rules miss. SHAP keeps the ML explainable too. |
| CIA mapping? | **Confidentiality:** RBAC, masking, least-privilege roles. **Integrity:** keyed (HMAC) audit chain, evidence hashes, maker-checker, DB constraints. **Availability:** health checks, restart policies, idempotent replay. |
| What would production need? | TLS, an SSO/MFA identity provider, key management for field encryption, goAML XML, rule back-testing, and a feedback loop that retrains on analyst labels. |
