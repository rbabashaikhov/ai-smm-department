#!/usr/bin/env bash
# Provision the sign-in service's database role on the LOCAL disposable
# runtime database (compose project ai-smm-runtime-local) -- never on any
# other database: every statement goes through `docker compose exec` of
# compose.runtime.yml's own postgres service, and generation-owner.sql
# refuses any database but ai_smm_runtime_local.
#
# Everything comes from the operator's production files, unchanged:
# deploy/control-center/db/smm_auth.functions.sql (the smm_auth contracts),
# then ai_smm_auth.grants.sql, then ai_smm_auth.verify.sql. Both refuse a
# superuser-owned schema, so generation-owner.sql first gives the local
# tables the production shape (a non-superuser NOLOGIN owner); it is
# idempotent and is the same step generation-role.sh takes. The password is
# the fixed, public, local-only value compose.runtime.yml spells out for
# smm-auth; it is not a secret, exists only for this disposable database
# and must never be reused in production.
#
# Run after `migrate upgrade head` and before `up` (it never migrates):
#   deploy/local-runtime/auth-role.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DB_DIR="$ROOT/deploy/control-center/db"

psql_local() {
    docker compose -f "$ROOT/compose.runtime.yml" exec -T postgres \
        psql -X -q -v ON_ERROR_STOP=1 -U ai_smm_local -d ai_smm_runtime_local "$@"
}

psql_local < "$ROOT/deploy/local-runtime/generation-owner.sql"
psql_local < "$DB_DIR/smm_auth.functions.sql"
psql_local < "$DB_DIR/ai_smm_auth.grants.sql"
psql_local -c "ALTER ROLE ai_smm_auth PASSWORD 'local-auth-not-a-secret'"

verify="$(psql_local -At < "$DB_DIR/ai_smm_auth.verify.sql")"
printf '%s\n' "$verify" | grep -v '^PASS' || true

if grep -q '^FAIL' <<<"$verify"; then
    echo "ai_smm_auth verification FAILED" >&2
    exit 1
fi

echo "ai_smm_auth ready on ai_smm_runtime_local ($(grep -c '^PASS' <<<"$verify") checks passed)"
