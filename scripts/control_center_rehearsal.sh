#!/usr/bin/env bash
# Local dress rehearsal of docs/control-center-deployment.md, against
# DISPOSABLE stand-ins for the production pieces:
#
#   <shared-network>       -> network   ai-smm-cc-rehearsal-shared
#   <postgres-container>   -> container ai-smm-cc-rehearsal-postgres (alias postgres)
#   ai-smm-worker          -> container ai-smm-cc-rehearsal-worker (idle stand-in)
#
#   scripts/control_center_rehearsal.sh <full 40-hex commit>
#
# The images ai-smm-api:<commit> / ai-smm-web:<commit> must exist, built
# and labelled as the deployment guide's section 3 describes.
#
# The Docker daemon is chosen EXPLICITLY, as on the host: the endpoint of
# the current context, or SMM_CC_REHEARSAL_DOCKER_HOST; it is printed,
# exported as DOCKER_HOST for every command here, and pinned with its
# engine id in control-center.target.
#
# The deployment directory is laid out as on the host (compose file and
# cc-*.sh copied in), the database starts at 79731eadacd7 with one
# published row, and the runbook is followed: dump and restore check
# (plus a truncated archive, a checksum mismatch and a leftover scratch
# database, each of which must stop the scripts), explicit migration,
# owner bootstrap, API and web only, smoke test through 127.0.0.1:8088,
# wrapper bypass attempts, and `down` -- after which the stand-in worker,
# database and network must be untouched.
#
# Everything is created here and removed on exit. Needs port 8088 free.

set -euo pipefail

version="${1:?usage: $0 <full 40-hex commit>}"
[[ "$version" =~ ^[0-9a-f]{40}$ ]] || { echo "the version must be the full 40-hex commit" >&2; exit 2; }
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

readonly NET=ai-smm-cc-rehearsal-shared
readonly PG=ai-smm-cc-rehearsal-postgres
readonly WORKER=ai-smm-cc-rehearsal-worker
# postgres:16.15-bookworm, as in compose.runtime.yml
readonly PG_IMAGE=postgres@sha256:0ea6700a3b4f0ae6ce746519073558aed4d88a79d8d07622a9a644946c7319c4
readonly BASELINE=79731eadacd7
readonly HEAD_REVISION=3d4ac1986f07

step() { printf '\n== %s\n' "$*"; }
fail() { echo "REHEARSAL FAILED: $*" >&2; exit 1; }

# The daemon, chosen once and explicitly.
unset DOCKER_CONTEXT
DOCKER_HOST="${SMM_CC_REHEARSAL_DOCKER_HOST:-$(docker context inspect --format '{{.Endpoints.docker.Host}}')}"
export DOCKER_HOST
[[ "$DOCKER_HOST" == unix://* ]] || fail "not a local socket: $DOCKER_HOST"
engine_id="$(docker info --format '{{.ID}}')"

work="$(mktemp -d)"
dir="$work/ai-smm-control-center"
backups="$work/backups"
mkdir -p "$dir"
install -d -m 700 "$backups"

cc() { "$dir/cc-compose.sh" "$@"; }
bk() { "$dir/cc-db-backup.sh" "$@"; }
psql_ai_smm() { docker exec -i "$PG" psql -v ON_ERROR_STOP=1 -U ai_smm -d "${2:-ai_smm}" -tA -c "$1"; }
scratch_dbs() { psql_ai_smm "SELECT count(*) FROM pg_database WHERE datname LIKE 'ai_smm_restore_check_%'" postgres; }
identity() { docker inspect -f '{{.Id}} {{.State.StartedAt}} {{.RestartCount}}' "$1"; }

# Runs a command that must be refused with the given exit code.
expect_exit() {
    local want="$1" got=0
    shift
    "$@" >/dev/null 2>"$work/stderr" || got=$?
    [[ "$got" == "$want" ]] || { cat "$work/stderr" >&2; fail "'$*' exited $got, expected $want"; }
    echo "refused as expected (exit $got): $(grep -v '^scratch database' "$work/stderr" | tail -n 1)"
}

cleanup() {
    step "cleanup"
    [[ -x "$dir/cc-compose.sh" ]] && cc down --remove-orphans >/dev/null 2>&1 || true
    docker rm -f "$WORKER" "$PG" >/dev/null 2>&1 || true
    docker network rm "$NET" >/dev/null 2>&1 || true
    rm -rf "$work"
}
trap cleanup EXIT

step "daemon: $(docker info --format '{{.Name}}') engine $engine_id at $DOCKER_HOST"

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
sleep 2

step "deployment directory, laid out as /root/ai-smm-control-center"
cp "$repo/compose.control-center.yml" "$repo"/deploy/control-center/cc-*.sh "$dir/"
chmod 644 "$dir/compose.control-center.yml" "$dir/cc-common.sh"
chmod 755 "$dir/cc-compose.sh" "$dir/cc-db-backup.sh"
printf 'SMM_CC_VERSION=%s\nSMM_CC_SHARED_NETWORK=%s\n' "$version" "$NET" \
    > "$dir/control-center.compose.env"
printf 'SMM_CC_DOCKER_HOST=%s\nSMM_CC_DOCKER_ENGINE_ID=%s\n' "$DOCKER_HOST" "$engine_id" \
    > "$dir/control-center.target"
( umask 077
  printf 'AI_SMM_DATABASE_URL=postgresql+psycopg://ai_smm:%s@postgres:5432/ai_smm\n' "$password" \
      > "$dir/control-center.env" )
cc config --quiet
cc config --json --with-tools | grep -q "$password" && fail "config printed the database password"
echo "config ok; the database password is not printed"

step "production-like baseline: $BASELINE with one published row"
cc migrate upgrade "$BASELINE" </dev/null
psql_ai_smm "INSERT INTO projects (id, display_name, knowledge_path)
             VALUES ('rehearsal', 'Rehearsal', 'knowledge/projects/rehearsal')" >/dev/null
psql_ai_smm "INSERT INTO publications (project_id, ordinal, title, format, body, status,
                 idempotency_key, threads_post_id, published_at, human_reviewed)
             VALUES ('rehearsal', 1, 'Seed', 'text', 'seed body', 'published',
                 'rehearsal-1', 'stand-in-post-1', now(), true)" >/dev/null

worker_before="$(identity "$WORKER")"
pg_before="$(identity "$PG")"

step "runbook 3: revision"
current="$(cc migrate current </dev/null 2>/dev/null | tail -n 1)"
[[ "$current" == "$BASELINE" ]] || fail "expected $BASELINE, got '$current'"
echo "current: $current"

step "runbook 4: dump"
bk dump --container "$PG" --out-dir "$backups" | tee "$work/dump.txt"
dump="$(sed -n 's/^dump: *//p' "$work/dump.txt")"
[[ -s "$dump" && -s "$dump.sha256" ]] || fail "no dump"
[[ "$(stat -c '%a' "$dump")" == 600 ]] || fail "dump is not 600"

step "runbook 5: restore check"
bk verify --container "$PG" --dump "$dump" --expect-revision "$BASELINE"
[[ "$(scratch_dbs)" == 0 ]] || fail "scratch database left behind"

step "runbook 5, failure cases: each must stop, none may leave a scratch database"
bad="$work/bad"; install -d -m 700 "$bad"
# (a) corrupted in transit: the recorded checksum no longer matches
cp "$dump" "$bad/flipped.dump"
printf '\xff' | dd of="$bad/flipped.dump" bs=1 seek=100 conv=notrunc status=none
sed "s/$(basename "$dump")/flipped.dump/" "$dump.sha256" > "$bad/flipped.dump.sha256"
expect_exit 2 bk verify --container "$PG" --dump "$bad/flipped.dump"
# (b) truncated before the checksum was taken: only the restore can tell
head -c "$(( $(stat -c %s "$dump") / 2 ))" "$dump" > "$bad/truncated.dump"
(cd "$bad" && sha256sum truncated.dump > truncated.dump.sha256)
expect_exit 2 bk verify --container "$PG" --dump "$bad/truncated.dump"
# (b2) only the tail missing: the table of contents still reads, the
#      restore into the scratch database fails half-way and must drop it
head -c "$(( $(stat -c %s "$dump") - 64 ))" "$dump" > "$bad/tail.dump"
(cd "$bad" && sha256sum tail.dump > tail.dump.sha256)
expect_exit 2 bk verify --container "$PG" --dump "$bad/tail.dump"
grep -q "pg_restore failed" "$work/stderr" || fail "the tail-truncated archive was not caught by the restore"
grep -q "dropped" "$work/stderr" || fail "the scratch database of the failed restore was not dropped"
[[ "$(scratch_dbs)" == 0 ]] || fail "a failed restore left its scratch database"
# (c) a scratch database that is not this run's is refused and kept
psql_ai_smm 'CREATE DATABASE "ai_smm_restore_check_20000101000000"' postgres >/dev/null
expect_exit 2 bk verify --container "$PG" --dump "$dump"
[[ "$(scratch_dbs)" == 1 ]] || fail "the foreign scratch database was dropped"
psql_ai_smm 'DROP DATABASE "ai_smm_restore_check_20000101000000"' postgres >/dev/null
# (d) a backup directory others can read
install -d -m 755 "$work/open"
expect_exit 2 bk dump --container "$PG" --out-dir "$work/open"
[[ -z "$(ls -A "$work/open")" ]] || fail "a refused dump left a file"
# ai_smm itself was only read
[[ "$(psql_ai_smm "SELECT version_num FROM alembic_version")/$(psql_ai_smm "SELECT count(*) FROM publications")" \
    == "$BASELINE/1" ]] || fail "ai_smm changed"
echo "ai_smm unchanged"

step "runbook 6: the migration, by explicit revision"
cc migrate upgrade "$HEAD_REVISION" </dev/null

step "runbook 7: schema after"
current="$(cc migrate current </dev/null 2>/dev/null | tail -n 1)"
[[ "$current" == "$HEAD_REVISION (head)" ]] || fail "expected $HEAD_REVISION (head), got '$current'"
[[ "$(psql_ai_smm "SELECT status || '/' || threads_post_id FROM publications")" == "published/stand-in-post-1" ]] \
    || fail "the published row changed"
echo "current: $current; published row intact"

step "bootstrap owner (password on stdin, never an argument)"
owner_password="rehearsal-$(od -An -N8 -tx1 /dev/urandom | tr -d ' \n')"
printf '%s\n%s\n' "$owner_password" "$owner_password" \
    | cc create-owner --email owner@rehearsal.test --display-name "Rehearsal Owner" --project rehearsal \
        2>/dev/null
password_file="$work/owner.pw"
( umask 077; printf '%s' "$owner_password" > "$password_file" )

step "runbook 8: API and web only"
cc up
running="$(cc ps --services | sort | tr '\n' ' ')"
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

step "wrapper: bypass attempts against the live stack are refused"
expect_exit 2 cc run --rm cli queue
expect_exit 2 cc -p ai-smm down
expect_exit 2 cc down -v
expect_exit 2 cc migrate upgrade head
expect_exit 2 cc exec smm-api sh
expect_exit 2 env DOCKER_HOST=unix:///var/run/other.sock "$dir/cc-compose.sh" ps
expect_exit 2 env DOCKER_CONTEXT=default "$dir/cc-compose.sh" ps
sed -i "s/^SMM_CC_DOCKER_ENGINE_ID=.*/SMM_CC_DOCKER_ENGINE_ID=not-this-engine-0/" "$dir/control-center.target"
expect_exit 2 cc ps
sed -i "s/^SMM_CC_DOCKER_ENGINE_ID=.*/SMM_CC_DOCKER_ENGINE_ID=$engine_id/" "$dir/control-center.target"
[[ "$(cc ps --services | sort | tr '\n' ' ')" == "smm-api smm-web " ]] || fail "stack changed"

step "runbook 12: worker stand-in unchanged"
[[ "$(identity "$WORKER")" == "$worker_before" ]] || fail "the worker stand-in was restarted or replaced"
echo "worker: unchanged"

step "rollback A: stop the Control Center only"
cc down
[[ -z "$(cc ps -q)" ]] || fail "Control Center still running"
docker network inspect "$NET" >/dev/null || fail "shared network removed"
[[ "$(identity "$PG")" == "$pg_before" ]] || fail "database container touched"
[[ "$(identity "$WORKER")" == "$worker_before" ]] || fail "the worker stand-in was touched by down"
echo "Control Center down; shared network, database and worker untouched"

step "REHEARSAL PASSED"
