#!/usr/bin/env bash
# Local dress rehearsal of docs/control-center-deployment.md, against
# DISPOSABLE stand-ins for the production pieces:
#
#   <shared-network>       -> network   ai-smm-cc-rehearsal-shared
#   <postgres-container>   -> container ai-smm-cc-rehearsal-postgres (alias postgres)
#   ai-smm-worker          -> container ai-smm-cc-rehearsal-worker (idle stand-in)
#
#   scripts/control_center_rehearsal.sh <version>
#
# <version> is the tag of ai-smm-api:<version> / ai-smm-web:<version>,
# built and labelled as the deployment guide's step 0 describes. The
# database starts at 79731eadacd7 with one published row, like
# production; the script then follows the runbook: dump and verify,
# migrate explicitly, create the owner, start API and web only, run the
# smoke test through 127.0.0.1:8088, stop the Control Center, and checks
# that the stand-in worker, database and network were never touched.
#
# Everything is created here and removed on exit. Needs port 8088 free.

set -euo pipefail

version="${1:?usage: $0 <image version>}"
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

readonly NET=ai-smm-cc-rehearsal-shared
readonly PG=ai-smm-cc-rehearsal-postgres
readonly WORKER=ai-smm-cc-rehearsal-worker
# postgres:16.15-bookworm, as in compose.runtime.yml
readonly PG_IMAGE=postgres@sha256:0ea6700a3b4f0ae6ce746519073558aed4d88a79d8d07622a9a644946c7319c4
readonly BASELINE=79731eadacd7
readonly HEAD_REVISION=3d4ac1986f07

work="$(mktemp -d)"
dir="$work/ai-smm-control-center"
mkdir -p "$dir"

step() { printf '\n== %s\n' "$*"; }
fail() { echo "REHEARSAL FAILED: $*" >&2; exit 1; }

cc() { SMM_CC_DIR="$dir" "$repo/deploy/control-center/cc-compose.sh" "$@"; }
psql_ai_smm() { docker exec -i "$PG" psql -v ON_ERROR_STOP=1 -U ai_smm -d "${2:-ai_smm}" -tA -c "$1"; }

cleanup() {
    step "cleanup"
    cc down --remove-orphans >/dev/null 2>&1 || true
    docker rm -f "$WORKER" "$PG" >/dev/null 2>&1 || true
    docker network rm "$NET" >/dev/null 2>&1 || true
    rm -rf "$work"
}
trap cleanup EXIT

step "stand-ins for the shared network, the database and the worker"
docker network create "$NET" >/dev/null
password="$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')"
docker run -d --name "$PG" --network "$NET" --network-alias postgres \
    -e POSTGRES_USER=ai_smm -e POSTGRES_PASSWORD="$password" -e POSTGRES_DB=ai_smm \
    "$PG_IMAGE" >/dev/null
docker run -d --name "$WORKER" --network "$NET" --read-only \
    --entrypoint python "ai-smm-api:$version" -c "import time; time.sleep(10**9)" >/dev/null

for _ in $(seq 60); do
    docker exec "$PG" pg_isready -U ai_smm -d ai_smm >/dev/null 2>&1 && break
    sleep 1
done

step "deployment directory (as /root/ai-smm-control-center on the host)"
cp "$repo/compose.control-center.yml" "$dir/"
printf 'SMM_CC_VERSION=%s\nSMM_CC_SHARED_NETWORK=%s\n' "$version" "$NET" \
    > "$dir/control-center.compose.env"
( umask 077
  printf 'AI_SMM_DATABASE_URL=postgresql+psycopg://ai_smm:%s@postgres:5432/ai_smm\n' "$password" \
      > "$dir/control-center.env" )
cc config --quiet

step "production-like baseline: $BASELINE with one published row"
cc --profile cc-migrate run --rm -T migrate upgrade "$BASELINE" </dev/null
psql_ai_smm "INSERT INTO projects (id, display_name, knowledge_path)
             VALUES ('rehearsal', 'Rehearsal', 'knowledge/projects/rehearsal')" >/dev/null
psql_ai_smm "INSERT INTO publications (project_id, ordinal, title, format, body, status,
                 idempotency_key, threads_post_id, published_at, human_reviewed)
             VALUES ('rehearsal', 1, 'Seed', 'text', 'seed body', 'published',
                 'rehearsal-1', 'stand-in-post-1', now(), true)" >/dev/null

worker_before="$(docker inspect -f '{{.Id}} {{.State.StartedAt}} {{.RestartCount}}' "$WORKER")"

step "runbook 3: revision"
current="$(cc --profile cc-migrate run --rm -T migrate current </dev/null 2>/dev/null | tail -n 1)"
[[ "$current" == "$BASELINE" ]] || fail "expected $BASELINE, got '$current'"
echo "current: $current"

step "runbook 4-5: pg_dump -Fc, then prove it restores"
docker exec "$PG" pg_dump -U ai_smm -d ai_smm -Fc -f /tmp/pre-migration.dump
docker exec "$PG" pg_restore --list /tmp/pre-migration.dump | grep -q "TABLE DATA public publications" \
    || fail "dump has no publications data"
psql_ai_smm "CREATE DATABASE ai_smm_restore_check" postgres >/dev/null
docker exec "$PG" pg_restore -U ai_smm -d ai_smm_restore_check --no-owner --exit-on-error /tmp/pre-migration.dump
restored="$(psql_ai_smm "SELECT version_num FROM alembic_version" ai_smm_restore_check)/$(psql_ai_smm "SELECT count(*) FROM publications" ai_smm_restore_check)"
[[ "$restored" == "$BASELINE/1" ]] || fail "restore check: $restored"
psql_ai_smm "DROP DATABASE ai_smm_restore_check WITH (FORCE)" postgres >/dev/null
echo "restore check: revision/publications = $restored"

step "runbook 6: the migration, by explicit revision"
cc --profile cc-migrate run --rm -T migrate upgrade "$HEAD_REVISION" </dev/null

step "runbook 7: schema after"
current="$(cc --profile cc-migrate run --rm -T migrate current </dev/null 2>/dev/null | tail -n 1)"
[[ "$current" == "$HEAD_REVISION (head)" ]] || fail "expected $HEAD_REVISION (head), got '$current'"
[[ "$(psql_ai_smm "SELECT status || '/' || threads_post_id FROM publications")" == "published/stand-in-post-1" ]] \
    || fail "the published row changed"
echo "current: $current; published row intact"

step "bootstrap owner (password on stdin, never an argument)"
owner_password="rehearsal-$(od -An -N8 -tx1 /dev/urandom | tr -d ' \n')"
printf '%s\n%s\n' "$owner_password" "$owner_password" \
    | cc --profile cc-admin run --rm -T cli create-owner \
        --email owner@rehearsal.test --display-name "Rehearsal Owner" --project rehearsal \
        2>/dev/null
password_file="$work/owner.pw"
( umask 077; printf '%s' "$owner_password" > "$password_file" )

step "runbook 8: API and web only"
cc up -d --wait
running="$(cc ps --format '{{.Service}}' | sort | tr '\n' ' ')"
[[ "$running" == "smm-api smm-web " ]] || fail "unexpected services: $running"

step "runbook 9: ports and networks"
published="$(docker ps --filter label=com.docker.compose.project=ai-smm-control-center \
    --format '{{.Names}} {{.Ports}}')"
echo "$published"
grep -q '127.0.0.1:8088->8080/tcp' <<<"$published" || fail "web is not on 127.0.0.1:8088"
grep -E -q '0\.0\.0\.0:|\[::\]:' <<<"$published" && fail "a port is bound to all interfaces"
[[ "$(grep -c -- '->' <<<"$published")" == 1 ]] || fail "more than one published port"
for svc in smm-api smm-web; do
    cid="$(cc ps -q "$svc")"
    echo "$svc: $(docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$cid")"
done
web_networks="$(docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$(cc ps -q smm-web)")"
grep -q "$NET" <<<"$web_networks" && fail "smm-web is attached to the shared network"
docker stats --no-stream --format '{{.Name}} mem={{.MemUsage}} pids={{.PIDs}}' \
    "$(cc ps -q smm-api)" "$(cc ps -q smm-web)"

step "runbook 11: smoke test through the loopback origin"
"$repo/.venv/bin/python" "$repo/scripts/runtime_smoke.py" \
    --base-url http://127.0.0.1:8088 \
    --email owner@rehearsal.test --password-file "$password_file" --project rehearsal

step "runbook 12: worker stand-in unchanged"
[[ "$(docker inspect -f '{{.Id}} {{.State.StartedAt}} {{.RestartCount}}' "$WORKER")" == "$worker_before" ]] \
    || fail "the worker stand-in was restarted or replaced"
echo "worker: unchanged"

step "rollback A: stop the Control Center only"
cc down
[[ -z "$(cc ps -q)" ]] || fail "Control Center still running"
docker network inspect "$NET" >/dev/null || fail "shared network removed"
[[ "$(docker inspect -f '{{.State.Running}}' "$PG")" == true ]] || fail "database stopped"
[[ "$(docker inspect -f '{{.Id}} {{.State.StartedAt}} {{.RestartCount}}' "$WORKER")" == "$worker_before" ]] \
    || fail "the worker stand-in was touched by down"
echo "Control Center down; shared network, database and worker untouched"

step "REHEARSAL PASSED"
