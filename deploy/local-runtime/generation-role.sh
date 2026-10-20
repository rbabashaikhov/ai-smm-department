#!/usr/bin/env bash
# Provision the generation worker's database role on the LOCAL disposable
# runtime database (compose project ai-smm-runtime-local) -- never on any
# other database: every statement goes through `docker compose exec` of
# compose.runtime.yml's own postgres service, and generation-owner.sql
# refuses any database but ai_smm_runtime_local.
#
# The role itself comes from the operator's production files, unchanged:
# deploy/control-center/db/ai_smm_generation.grants.sql, then .verify.sql.
# The password is the fixed, public, local-only value compose.runtime.yml
# spells out for smm-generation; it is not a secret.
#
# Run after `migrate upgrade head`, from anywhere:
#   deploy/local-runtime/generation-role.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DB_DIR="$ROOT/deploy/control-center/db"

psql_local() {
    docker compose -f "$ROOT/compose.runtime.yml" exec -T postgres \
        psql -X -q -v ON_ERROR_STOP=1 -U ai_smm_local -d ai_smm_runtime_local "$@"
}

psql_local < "$ROOT/deploy/local-runtime/generation-owner.sql"
psql_local < "$DB_DIR/ai_smm_generation.grants.sql"
psql_local -c "ALTER ROLE ai_smm_generation PASSWORD 'local-generation-not-a-secret'"

verify="$(psql_local -At < "$DB_DIR/ai_smm_generation.verify.sql")"
printf '%s\n' "$verify"

if grep -q '^FAIL' <<<"$verify"; then
    echo "ai_smm_generation verification FAILED" >&2
    exit 1
fi

echo "ai_smm_generation ready on ai_smm_runtime_local"
