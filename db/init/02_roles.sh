#!/bin/bash
# Least privilege: the app connects as a non-superuser role that
#  - cannot read ground_truth (detection must not peek at the answers)
#  - can only INSERT/SELECT on audit_log (no UPDATE/DELETE)
#  - cannot DELETE anything
# The simulator role can only seed accounts and write ground truth.
# The evaluation role can read everything but write nothing.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
     -v app_pw="$APP_DB_PASSWORD" -v eval_pw="$EVAL_DB_PASSWORD" -v sim_pw="$SIM_DB_PASSWORD" <<'SQL'
CREATE ROLE aml_app  LOGIN PASSWORD :'app_pw';
CREATE ROLE aml_eval LOGIN PASSWORD :'eval_pw';
CREATE ROLE aml_sim  LOGIN PASSWORD :'sim_pw';

REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO aml_app, aml_eval, aml_sim;

GRANT SELECT, INSERT, UPDATE ON accounts, transactions, cases, alerts, ml_scores, strs, users TO aml_app;
GRANT SELECT, INSERT          ON audit_log TO aml_app;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO aml_app;
-- ground_truth / account_truth: deliberately no grant for aml_app.

GRANT SELECT ON ALL TABLES IN SCHEMA public TO aml_eval;
GRANT INSERT ON eval_snapshots TO aml_eval;
GRANT USAGE ON SEQUENCE eval_snapshots_snapshot_id_seq TO aml_eval;
GRANT SELECT ON eval_snapshots TO aml_app;

GRANT SELECT, INSERT ON accounts TO aml_sim;
GRANT INSERT ON ground_truth, account_truth TO aml_sim;
SQL
