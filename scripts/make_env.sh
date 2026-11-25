#!/bin/bash
# Create/complete .env with random secrets. Existing values are never overwritten.
set -euo pipefail
cd "$(dirname "$0")/.."
umask 077
touch .env
gen() { python3 -c 'import secrets; print(secrets.token_urlsafe(32))'; }
added=0
for key in POSTGRES_PASSWORD APP_DB_PASSWORD EVAL_DB_PASSWORD SIM_DB_PASSWORD JWT_SECRET INGEST_TOKEN AUDIT_HMAC_KEY; do
  if ! grep -q "^${key}=" .env; then echo "${key}=$(gen)" >> .env; added=$((added+1)); fi
done
if ! grep -q "^DEMO_PASSWORD=" .env; then
  # Readable demo password for the seeded users (analyst1, mlro1, auditor1, ...).
  echo "DEMO_PASSWORD=$(python3 -c 'import secrets; print("Ww-" + secrets.token_hex(4))')" >> .env
  added=$((added+1))
fi
chmod 600 .env
echo ".env ready (${added} new values). Demo login password: $(grep ^DEMO_PASSWORD= .env | cut -d= -f2)"
