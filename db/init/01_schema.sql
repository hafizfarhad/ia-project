-- WalletWatch schema. Runs once, as the Postgres superuser, on first container start.

-- ---------------------------------------------------------------- world
CREATE TABLE accounts (
    account_id  TEXT PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN ('customer','agent','biller','employer','external')),
    name        TEXT NOT NULL,
    cnic        TEXT,                       -- PII: masked by the API, never sent raw to the dashboard
    msisdn      TEXT,                       -- PII
    tier        TEXT CHECK (tier IN ('L0','L1','L2')),
    city        TEXT,
    device_id   TEXT,
    opened_at   TIMESTAMPTZ NOT NULL
);
CREATE INDEX accounts_device_idx ON accounts (device_id);

CREATE TABLE transactions (
    txn_id      TEXT PRIMARY KEY,           -- unique => ingest is idempotent
    ts          TIMESTAMPTZ NOT NULL,       -- simulated event time
    txn_type    TEXT NOT NULL CHECK (txn_type IN
                  ('P2P','CASH_IN','CASH_OUT','BILL','TOPUP','IBFT_OUT','IBFT_IN','SALARY')),
    sender_id   TEXT NOT NULL REFERENCES accounts(account_id),
    receiver_id TEXT NOT NULL REFERENCES accounts(account_id),
    amount      NUMERIC(14,2) NOT NULL CHECK (amount > 0),
    device_id   TEXT,
    city        TEXT,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX txn_sender_ts_idx   ON transactions (sender_id, ts);
CREATE INDEX txn_receiver_ts_idx ON transactions (receiver_id, ts);
CREATE INDEX txn_ts_idx          ON transactions (ts);
CREATE INDEX txn_device_ts_idx   ON transactions (device_id, ts);

-- Evaluation only. Written by the simulator role, read by the eval role.
-- The application role has NO access (see 02_roles.sh). No FK: truth may be
-- written before the API has ingested the transaction.
CREATE TABLE ground_truth (
    txn_id     TEXT PRIMARY KEY,
    label      TEXT NOT NULL CHECK (label IN ('illicit','lookalike')),
    typology   TEXT NOT NULL,   -- e.g. S2_FAN_IN_OUT, COMMITTEE, SALARY
    ring_id    TEXT
);

CREATE TABLE account_truth (
    account_id TEXT PRIMARY KEY,
    label      TEXT NOT NULL CHECK (label IN ('illicit','lookalike')),
    typology   TEXT NOT NULL,
    ring_id    TEXT
);

-- Aggregate scores written by the evaluator (eval role). The app may read these numbers,
-- but never the per-account answers above.
CREATE TABLE eval_snapshots (
    snapshot_id BIGSERIAL PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    payload     JSONB NOT NULL
);

-- ---------------------------------------------------------------- users
CREATE TABLE users (
    user_id       SERIAL PRIMARY KEY,
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,            -- bcrypt
    role          TEXT NOT NULL CHECK (role IN ('analyst','mlro','auditor')),
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- detection
CREATE TABLE cases (
    case_id            SERIAL PRIMARY KEY,
    account_id         TEXT NOT NULL REFERENCES accounts(account_id),
    status             TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN
                         ('OPEN','CLOSED_FALSE_POSITIVE','ESCALATED','STR_PENDING','STR_FILED')),
    priority           NUMERIC(4,3) NOT NULL DEFAULT 0,
    disposition_reason TEXT,
    disposed_by        INT REFERENCES users(user_id),
    opened_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (status <> 'CLOSED_FALSE_POSITIVE' OR length(coalesce(disposition_reason,'')) >= 20)
);
-- At most one active case per account; new alerts join it.
CREATE UNIQUE INDEX one_active_case_per_account ON cases (account_id)
    WHERE status IN ('OPEN','ESCALATED','STR_PENDING');

CREATE TABLE alerts (
    alert_id      SERIAL PRIMARY KEY,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    sim_ts        TIMESTAMPTZ NOT NULL,      -- simulated time the pattern completed
    account_id    TEXT NOT NULL REFERENCES accounts(account_id),
    scenario      TEXT NOT NULL CHECK (scenario IN ('S1','S2','S3','S4','S5','S6','ML')),
    score         NUMERIC(4,3) NOT NULL,
    evidence      JSONB NOT NULL,            -- frozen at creation
    evidence_hash CHAR(64) NOT NULL,         -- SHA-256 of canonical evidence JSON
    case_id       INT REFERENCES cases(case_id)
);
CREATE INDEX alerts_account_idx ON alerts (account_id);
CREATE INDEX alerts_case_idx    ON alerts (case_id);
CREATE INDEX alerts_scenario_idx ON alerts (scenario, sim_ts);

CREATE TABLE ml_scores (
    txn_id        TEXT PRIMARY KEY REFERENCES transactions(txn_id),
    score         NUMERIC(5,4) NOT NULL,
    shap          JSONB,
    model_version TEXT NOT NULL
);

CREATE TABLE strs (
    str_id         SERIAL PRIMARY KEY,
    case_id        INT NOT NULL REFERENCES cases(case_id),
    narrative      TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING','FILED','REJECTED')),
    drafted_by     INT NOT NULL REFERENCES users(user_id),
    drafted_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    reviewed_by    INT REFERENCES users(user_id),
    reviewed_at    TIMESTAMPTZ,
    review_comment TEXT,
    -- Maker-checker (separation of duties), enforced by the database itself.
    CONSTRAINT maker_checker CHECK (reviewed_by IS NULL OR reviewed_by <> drafted_by)
);

-- ---------------------------------------------------------------- audit
CREATE TABLE audit_log (
    entry_id   BIGSERIAL PRIMARY KEY,
    ts         TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor      TEXT NOT NULL,               -- username or 'system' / 'simulator'
    action     TEXT NOT NULL,
    target     TEXT,
    details    JSONB NOT NULL DEFAULT '{}'::jsonb,
    prev_hash  CHAR(64) NOT NULL,
    hash       CHAR(64) NOT NULL
);

-- Append-only: block UPDATE/DELETE even for roles that somehow get the privilege.
-- (A superuser can still disable this trigger — that is exactly what the hash chain detects.)
CREATE FUNCTION audit_log_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only';
END $$;
CREATE TRIGGER audit_log_no_update BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_immutable();
CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION audit_log_immutable();
