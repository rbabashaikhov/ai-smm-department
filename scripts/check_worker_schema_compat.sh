#!/usr/bin/env bash
# Runs the DEPLOYED worker's own test suite against the schema produced by
# THIS checkout's migrations -- the situation production is in between
# `alembic upgrade head` and the next worker release.
#
#   AI_SMM_TEST_DATABASE_URL=postgresql+psycopg://ai_smm:testpass@127.0.0.1:55433/ai_smm_test \
#       scripts/check_worker_schema_compat.sh 6c54669
#
# The argument is the commit the running worker image was built from
# (6c54669 for ai-smm:706b383, see docs/production-deployment-plan.md
# §17.1). Local only: it creates and drops a scratch database
# ai_smm_worker_compat_test on the test server and touches nothing else.
#
# The worker's suite normally builds its schema from its own models
# (drop_all + create_all). Here those two lines are removed from a
# throwaway copy of its conftest, so every test runs against the tables,
# columns, defaults, triggers and foreign keys of the migrated head
# schema instead. The worker's code and tests are otherwise unchanged.
# test_migrations.py is skipped: it migrates with the old revisions.

set -euo pipefail

worker_commit="${1:?usage: $0 <worker-commit>}"
admin_url="${AI_SMM_TEST_DATABASE_URL:?set AI_SMM_TEST_DATABASE_URL to a disposable test database}"

case "${admin_url##*/}" in
    *test*) ;;
    *) echo "refusing: database name in AI_SMM_TEST_DATABASE_URL must contain 'test'" >&2; exit 2 ;;
esac

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python="$repo/.venv/bin/python"
scratch_db=ai_smm_worker_compat_test
scratch_url="${admin_url%/*}/$scratch_db"
workdir="$(mktemp -d)"

admin_sql() {
    "$python" - "$admin_url" "$1" <<'PY'
import sys
from sqlalchemy import create_engine, text
engine = create_engine(sys.argv[1], isolation_level="AUTOCOMMIT")
with engine.connect() as connection:
    connection.execute(text(sys.argv[2]))
engine.dispose()
PY
}

cleanup() {
    admin_sql "DROP DATABASE IF EXISTS \"$scratch_db\" WITH (FORCE)" || true
    rm -rf "$workdir"
}
trap cleanup EXIT

admin_sql "DROP DATABASE IF EXISTS \"$scratch_db\" WITH (FORCE)"
admin_sql "CREATE DATABASE \"$scratch_db\""

echo "== schema: this checkout's migrations, upgrade head"
(cd "$repo" && AI_SMM_DATABASE_URL="$scratch_url" "$repo/.venv/bin/alembic" upgrade head)
(cd "$repo" && AI_SMM_DATABASE_URL="$scratch_url" "$repo/.venv/bin/alembic" current)

echo "== code: worker at $(git -C "$repo" rev-parse --short "$worker_commit")"
git -C "$repo" archive "$worker_commit" | tar -x -C "$workdir"

conftest="$workdir/tests/conftest.py"
grep -q "Base.metadata.drop_all(engine)" "$conftest"
grep -q "Base.metadata.create_all(engine)" "$conftest"
sed -i \
    -e '/Base.metadata.drop_all(engine)/d' \
    -e '/Base.metadata.create_all(engine)/d' \
    "$conftest"

echo "== the worker's suite against the head schema"
cd "$workdir"
AI_SMM_TEST_DATABASE_URL="$scratch_url" \
    "$python" -m pytest tests -q -p no:cacheprovider \
        --ignore tests/test_migrations.py
