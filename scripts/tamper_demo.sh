#!/bin/bash
# Viva demo: an insider with database superuser rights rewrites an audit entry.
#   ./scripts/tamper_demo.sh tamper   -> edit entry #3 (bypassing the append-only trigger)
#   (now click "Verify integrity" on the Audit page as auditor1 -> TAMPERING DETECTED at #3)
#   ./scripts/tamper_demo.sh restore  -> put it back; verification passes again
set -euo pipefail
cd "$(dirname "$0")/.."
ENTRY=${ENTRY:-3}
psql_su() { docker compose exec -T postgres psql -U postgres -d walletwatch -v ON_ERROR_STOP=1 -tA "$@"; }

case "${1:-}" in
  tamper)
    orig=$(psql_su -c "SELECT actor FROM audit_log WHERE entry_id=$ENTRY")
    [ -n "$orig" ] || { echo "no audit entry #$ENTRY yet"; exit 1; }
    echo "$orig" > .tamper_backup
    echo "1) Trying a normal UPDATE (the trigger should block it):"
    psql_su -c "UPDATE audit_log SET actor='ghost' WHERE entry_id=$ENTRY" 2>&1 | sed 's/^/   /' || true
    echo "2) Superuser disables the trigger and rewrites entry #$ENTRY (actor '$orig' -> 'ghost')"
    psql_su -c "ALTER TABLE audit_log DISABLE TRIGGER audit_log_no_update;
                UPDATE audit_log SET actor='ghost' WHERE entry_id=$ENTRY;
                ALTER TABLE audit_log ENABLE TRIGGER audit_log_no_update;" >/dev/null
    echo "Done. Now press 'Verify integrity' on the Audit page."
    ;;
  restore)
    orig=$(cat .tamper_backup)
    psql_su -c "ALTER TABLE audit_log DISABLE TRIGGER audit_log_no_update;
                UPDATE audit_log SET actor='$orig' WHERE entry_id=$ENTRY;
                ALTER TABLE audit_log ENABLE TRIGGER audit_log_no_update;" >/dev/null
    rm -f .tamper_backup
    echo "Entry #$ENTRY restored to actor '$orig'. Verification will pass again."
    ;;
  *) echo "usage: $0 tamper|restore"; exit 2 ;;
esac
