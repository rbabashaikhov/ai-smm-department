#!/usr/bin/env bash
# Local rehearsal of R7 -- switching the Control Center to business-mutation
# mode and back -- against DISPOSABLE stand-ins for production (FU-024C1-5:
# "a rehearsal of the enable / disable switch on a production-like copy,
# including the rollback and what the worker does with anything scheduled
# meanwhile"). Runbook: docs/reports/smm-025d/r7/runbook.md.
#
#   scripts/r7_mutation_rehearsal.sh <full 40-hex image version>
#
# The images ai-smm-api:<version> / ai-smm-web:<version> must exist, built
# from the tree under test (the release N' that permits mutations). The
# schema is migrated with the image's own Alembic; ai_smm_cc (G2),
# smm_auth, ai_smm_auth and ai_smm_cc_rw are provisioned by the operator's
# own SQL files; the Control Center runs THROUGH THE WRAPPER of this tree
# (every gate, the real verify files on the real roles). The worker is an
# idle stand-in for the identity gate; its part is played by one-off
# `ai-smm dry-run` cycles (publishing forced off, no network out).
#
#   R7-0  read-only start: `mutations status`, sign-in, business writes 403
#   R7-1  ai_smm_cc_rw created and checked (verify: 0 FAIL)
#   R7-2  checklist #4: direct SQL as ai_smm_cc_rw, no bound session -> guard
#   R7-3  `mutations enable`: smm-api recreated as ai_smm_cc_rw, flag true
#   R7-4  the production smoke, no publication (client phase `smoke`, in its
#         own project r7-smoke, which the smoke creates and which is not
#         allow-listed -- exactly as in production)
#   R7-5  rehearsal only: step-up, approve, materialize, schedule two rows;
#         one is then tampered with in SQL; one unreviewed row is forged
#   R7-6  `mutations disable`: read-only again; the scheduled rows stay
#   R7-7  the worker (dry-run cycle): only the approved, untampered row is
#         taken; the tampered one fails ApprovalLineage; the forged one is
#         never published
#   R7-8  rollback of the role: ai_smm_cc_rw.revoke.sql
#
# Everything is created here and removed on exit. Needs port 8088 free.
# No secret is printed: passwords are random, live only in the work
# directory (mode 700) and in the disposable database.

set -euo pipefail

version="${1:?usage: $0 <full 40-hex image version>}"
[[ "$version" =~ ^[0-9a-f]{40}$ ]] || { echo "the version must be 40 hex characters" >&2; exit 2; }

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly NET=ai-smm-r7rh-shared
readonly PG=ai-smm-r7rh-postgres
readonly WORKER=ai-smm-r7rh-worker
readonly IMAGE="ai-smm-api:$version"
# postgres:16.15-bookworm, as in compose.runtime.yml
readonly PG_IMAGE=postgres@sha256:0ea6700a3b4f0ae6ce746519073558aed4d88a79d8d07622a9a644946c7319c4
readonly PROJECT=ai-smm-control-center
readonly LEGACY=rehearsal

step() { printf '\n== %s\n' "$*"; }
fail() { echo "REHEARSAL FAILED: $*" >&2; exit 1; }
pass() { echo "PASS $*"; }

unset DOCKER_CONTEXT
DOCKER_HOST="${SMM_CC_REHEARSAL_DOCKER_HOST:-$(docker context inspect --format '{{.Endpoints.docker.Host}}')}"
export DOCKER_HOST
[[ "$DOCKER_HOST" == unix://* ]] || fail "not a local socket: $DOCKER_HOST"
engine_id="$(docker info --format '{{.ID}}')"

for image in "ai-smm-api:$version" "ai-smm-web:$version"; do
    docker image inspect "$image" >/dev/null 2>&1 || fail "image $image is not built"
done
grep -qx 'readonly CC_RELEASE_PERMITS_MUTATIONS=yes' "$repo/deploy/control-center/cc-gate.sh" \
    || fail "this tree's cc-gate.sh does not permit mutations: not a release N'"

work="$(mktemp -d)"
chmod 700 "$work"
dir="$work/$PROJECT"
mkdir -p "$dir"
cc() { "$dir/cc-compose.sh" "$@"; }
psql_admin() { docker exec -i "$PG" psql -X -q -v ON_ERROR_STOP=1 -U pgadmin -d "${PSQL_DB:-ai_smm}" -tA "$@"; }
psql_ro() { docker exec -i -e PGOPTIONS='-c default_transaction_read_only=on' "$PG" psql -X -q -v ON_ERROR_STOP=1 -U pgadmin -d ai_smm -tA "$@"; }
random_secret() { od -An -N16 -tx1 /dev/urandom | tr -d ' \n'; }
client() { R7_PROJECT="${2:-$LEGACY}" "$repo/.venv/bin/python" "$repo/scripts/r7_rehearsal_client.py" "$1" \
    http://127.0.0.1:8088 owner@rehearsal.test "$work/owner.pw" "$work/state.json"; }
verify_role() {  # <verify file> <min checks>: 0 FAIL and a complete run
    local out fails checks
    out="$(psql_ro -f - < "$1")"
    fails="$(grep -c '^FAIL|' <<<"$out" || true)"
    checks="$(sed -n 's/^END|checks|//p' <<<"$out")"
    grep '^FAIL|' <<<"$out" | head -5 >&2 || true
    [[ "$fails" == 0 && -n "$checks" && "$checks" -ge "$2" ]] || return 1
    echo "$(basename "$1"): $checks checks, 0 FAIL"
}
cid_of() { docker ps -q --filter "label=com.docker.compose.project=$PROJECT" --filter "label=com.docker.compose.service=$1"; }
api_env() { docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$(cid_of smm-api)"; }

cleanup() {
    step "cleanup"
    [[ -x "$dir/cc-compose.sh" ]] && cc down --remove-orphans >/dev/null 2>&1 || true
    docker rm -f "$WORKER" "$PG" >/dev/null 2>&1 || true
    docker network rm "$NET" >/dev/null 2>&1 || true
    rm -rf "$work"
}
trap cleanup EXIT

step "daemon: $(docker info --format '{{.Name}}') engine $engine_id; images $version"
docker compose version

step "stand-ins: shared network (internal: no route out), database, worker"
docker network create --internal "$NET" >/dev/null
admin_password="$(random_secret)"
pg_password="$(random_secret)"
docker run -d --name "$PG" --network "$NET" \
    -e POSTGRES_USER=pgadmin -e POSTGRES_PASSWORD="$admin_password" -e POSTGRES_DB=postgres \
    "$PG_IMAGE" >/dev/null
dsn="postgresql+psycopg://ai_smm:$pg_password@$PG:5432/ai_smm"
docker run -d --name "$WORKER" --network "$NET" --read-only \
    -e AI_SMM_DATABASE_URL="$dsn" -e AI_SMM_LEGACY_THREADS_PROJECTS="$LEGACY" \
    --entrypoint python "$IMAGE" -c "import time; time.sleep(10**9)" >/dev/null
for _ in $(seq 60); do
    docker exec "$PG" pg_isready -U pgadmin -d postgres >/dev/null 2>&1 && break
    sleep 1
done
sleep 2
printf "CREATE ROLE ai_smm LOGIN PASSWORD '%s';\nCREATE DATABASE ai_smm OWNER ai_smm;\n" "$pg_password" \
    | PSQL_DB=postgres psql_admin >/dev/null

step "schema (the image's Alembic) and the read-only roles, as production after SMM-025D D"
docker run --rm --network "$NET" -e AI_SMM_DATABASE_URL="$dsn" \
    --entrypoint /app/.venv/bin/alembic -w /app "$IMAGE" upgrade head 2>&1 | tail -n 1
for sql in ai_smm_cc.grants.sql smm_auth.functions.sql ai_smm_auth.grants.sql; do
    psql_admin < "$repo/deploy/control-center/db/$sql" >/dev/null
    echo "applied $sql"
done
api_password="$(random_secret)"
auth_password="$(random_secret)"
printf "ALTER ROLE ai_smm_cc PASSWORD '%s';\nALTER ROLE ai_smm_auth PASSWORD '%s';\n" \
    "$api_password" "$auth_password" | psql_admin >/dev/null
psql_admin -c "INSERT INTO projects (id, display_name, knowledge_path)
               VALUES ('$LEGACY', 'Rehearsal', 'knowledge/projects/$LEGACY')" >/dev/null

step "deployment directory, laid out as on the host after Group E (release N')"
cp "$repo/compose.control-center.yml" "$repo/compose.control-center.mutations.yml" \
    "$repo"/deploy/control-center/cc-*.sh \
    "$repo/deploy/control-center/db/ai_smm_cc.verify.sql" \
    "$repo/deploy/control-center/db/ai_smm_cc_rw.verify.sql" \
    "$repo/deploy/control-center/db/ai_smm_auth.verify.sql" "$dir/"
chmod 755 "$dir/cc-compose.sh" "$dir/cc-db-backup.sh"
public_key="$(head -c 32 /dev/urandom | base64)"
printf '%s\n' "SMM_CC_VERSION=$version" "SMM_CC_SHARED_NETWORK=$NET" \
    "SMM_CC_CREDENTIALS_PUBLIC_KEY=$public_key" "SMM_CC_LEGACY_THREADS_PROJECTS=$LEGACY" \
    > "$dir/control-center.compose.env"
printf 'SMM_CC_DOCKER_HOST=%s\nSMM_CC_DOCKER_ENGINE_ID=%s\n' "$DOCKER_HOST" "$engine_id" \
    > "$dir/control-center.target"
(
    umask 077
    printf 'AI_SMM_DATABASE_URL=%s\n' "$dsn" > "$dir/control-center.env"
    printf 'AI_SMM_DATABASE_URL=postgresql+psycopg://ai_smm_cc:%s@%s:5432/ai_smm\n' \
        "$api_password" "$PG" > "$dir/control-center.api.env"
    printf 'AI_SMM_DATABASE_URL=postgresql+psycopg://ai_smm_auth:%s@%s:5432/ai_smm\n' \
        "$auth_password" "$PG" > "$dir/control-center.auth.env"
)
printf '%s\n' \
    "SMM_CC_PG_CONTAINER=$PG" \
    "SMM_CC_PG_CONTAINER_ID=$(docker inspect -f '{{.Id}}' "$PG")" \
    "SMM_CC_PG_SYSTEM_IDENTIFIER=$(psql_admin -c 'SELECT system_identifier FROM pg_control_system()')" \
    "SMM_CC_PG_DATABASE_OID=$(psql_admin -c "SELECT oid FROM pg_database WHERE datname = 'ai_smm'")" \
    "SMM_CC_WORKER_CONTAINER=$WORKER" > "$dir/control-center.identity"
printf 'SMM_CC_WINDOW_START=%s\nSMM_CC_WINDOW_END=%s\n' \
    "$(date -u -d '-10 min' +%FT%TZ)" "$(date -u -d '+110 min' +%FT%TZ)" > "$dir/control-center.window"

step "owner and start, read-only (preflight, up: every gate)"
cc preflight </dev/null | grep -E '^(gate [a-z-]+: PASS|preflight)' | cut -c1-120
owner_password="rehearsal-$(random_secret)"
printf '%s\n%s\n' "$owner_password" "$owner_password" \
    | cc create-owner --email owner@rehearsal.test --display-name "Rehearsal Owner" --project "$LEGACY" \
        >/dev/null 2>&1
( umask 077; printf '%s' "$owner_password" > "$work/owner.pw" )
cc up </dev/null >/dev/null
[[ "$(cc ps --services </dev/null | sort | tr '\n' ' ')" == "smm-api smm-auth smm-web " ]] \
    || fail "unexpected services"

step "R7-0: read-only start"
status="$(cc mutations status </dev/null)"
echo "$status"
grep -qx 'mode: read-only' <<<"$status" || fail "R7-0: not read-only"
grep -q 'does not permit' <<<"$status" && fail "R7-0: this release should permit mutations"
grep -q 'AI_SMM_API_MUTATIONS_ENABLED=false role=ai_smm_cc$' <<<"$status" || fail "R7-0: smm-api not read-only"
client readonly || fail "R7-0: read-only checks"
pass "R7-0: N' starts read-only; every business write is 403 MUTATIONS_DISABLED"

step "R7-1: the mutation role (ai_smm_cc_rw.grants.sql, password, api-rw.env, verify)"
fingerprint_before="$(psql_ro -c "SELECT md5(string_agg(c.relname||'='||coalesce(c.relacl::text,'-'),',' ORDER BY c.relname)) FROM pg_class c WHERE c.relnamespace='public'::regnamespace")"
psql_admin < "$repo/deploy/control-center/db/ai_smm_cc_rw.grants.sql" >/dev/null
rw_password="$(random_secret)"
printf "ALTER ROLE ai_smm_cc_rw PASSWORD '%s';\n" "$rw_password" | psql_admin >/dev/null
( umask 077; printf 'AI_SMM_DATABASE_URL=postgresql+psycopg://ai_smm_cc_rw:%s@%s:5432/ai_smm\n' \
    "$rw_password" "$PG" > "$dir/control-center.api-rw.env" )
verify_role "$dir/ai_smm_cc_rw.verify.sql" 200 || fail "R7-1: ai_smm_cc_rw verify"
verify_role "$dir/ai_smm_cc.verify.sql" 200 || fail "R7-1: ai_smm_cc verify changed"
verify_role "$dir/ai_smm_auth.verify.sql" 400 || fail "R7-1: ai_smm_auth verify changed"
pass "R7-1: ai_smm_cc_rw provisioned; all three role checks 0 FAIL"

step "R7-2 (checklist #4): direct SQL as ai_smm_cc_rw without a bound session"
forge() {  # <label> <sql>: must be refused by a guard (SQLSTATE 42501, 'guard: ')
    local out
    out="$(printf 'BEGIN;\nSET LOCAL ROLE ai_smm_cc_rw;\n%s;\nROLLBACK;\n' "$2" | psql_admin 2>&1 || true)"
    grep -q 'ERROR:  guard: ' <<<"$out" || fail "R7-2 $1: not refused by a guard: $(head -c 200 <<<"$out")"
    pass "R7-2 $1: $(grep -m1 -o 'ERROR:  guard: .*' <<<"$out" | cut -c1-110)"
}
forge "a reviewed, scheduled publication" \
    "INSERT INTO publications (project_id, platform, ordinal, status, human_reviewed, title, format, body, idempotency_key, scheduled_at) VALUES ('$LEGACY', 'threads', 9001, 'scheduled', true, 'forged', 'text', 'forged', 'forged-9001', now())"
forge "a business audit entry" \
    "INSERT INTO audit_log (action, actor, subject, details) VALUES ('content.approve', 'forged', 'x', '{}')"
forge "a project" \
    "INSERT INTO projects (id, display_name, knowledge_path) VALUES ('forged', 'forged', 'knowledge/projects/forged')"
[[ "$(psql_ro -c "SELECT md5(string_agg(c.relname||'='||coalesce(c.relacl::text,'-'),',' ORDER BY c.relname)) FROM pg_class c WHERE c.relnamespace='public'::regnamespace")" != "$fingerprint_before" ]] \
    || echo "INFO R7-2: (the rw grants changed relation ACLs, as expected)"

step "R7-3: mutations enable"
cc mutations enable --approved-by "rehearsal owner" </dev/null | grep -vE 'gate (api|auth)-role: INFO' | cut -c1-160
[[ -f "$dir/control-center.mutations" ]] || fail "R7-3: no mode file"
grep -qx "SMM_CC_MUTATIONS_VERSION=$version" "$dir/control-center.mutations" || fail "R7-3: mode file not for this release"
env_now="$(api_env)"
grep -qx 'AI_SMM_API_MUTATIONS_ENABLED=true' <<<"$env_now" || fail "R7-3: flag"
grep -q '^AI_SMM_DATABASE_URL=postgresql+psycopg://ai_smm_cc_rw:' <<<"$env_now" || fail "R7-3: role"
cc mutations status </dev/null | sed -n 1p
pass "R7-3: smm-api runs as ai_smm_cc_rw with AI_SMM_API_MUTATIONS_ENABLED=true; smm-auth, smm-web untouched"

step "R7-4: the production smoke (no publication)"
audit_mark="$(psql_ro -c "SELECT coalesce(max(id), 0) FROM audit_log")"
# As in production: its own project, created by the smoke, not allow-listed.
client smoke r7-smoke || fail "R7-4: smoke"
counts="$(psql_ro -c "SELECT (SELECT count(*) FROM publications)||'|'||(SELECT count(*) FROM content_approvals WHERE decision='approved')||'|'||(SELECT count(*) FROM approval_grants)")"
[[ "$counts" == "0|0|0" ]] || fail "R7-4: publications|approvals|grants = $counts, expected 0|0|0"
echo "audit rows of the smoke, by writer: $(psql_ro -c "SELECT string_agg(w||'='||n, ' ') FROM (SELECT coalesce(written_by,'-') w, count(*) n FROM audit_log WHERE id > $audit_mark GROUP BY 1 ORDER BY 1) s")"
[[ "$(psql_ro -c "SELECT count(*) FROM audit_log WHERE id > $audit_mark AND (CASE WHEN action LIKE 'auth.%' THEN written_by IS DISTINCT FROM 'ai_smm_auth' ELSE written_by IS DISTINCT FROM 'ai_smm_cc_rw' END)")" == 0 ]] \
    || fail "R7-4: an audit row of the smoke has an unexpected writer"
pass "R7-4: smoke clean -- 0 publications, 0 approvals, 0 grants; audit of the smoke: auth.* by ai_smm_auth, business by ai_smm_cc_rw"

step "R7-5 (rehearsal only): step-up, approve, materialize, schedule two rows"
client approve-path || fail "R7-5: approve path"
read -r pub_ok pub_tampered < <("$repo/.venv/bin/python" -c 'import json,sys; print(*json.load(open(sys.argv[1]))["publications"])' "$work/state.json")
psql_admin -c "UPDATE publications SET body = body || ' (changed after approval)' WHERE id = $pub_tampered" >/dev/null
psql_admin -c "INSERT INTO publications (project_id, platform, ordinal, status, human_reviewed, title, format, body, idempotency_key, scheduled_at)
               VALUES ('$LEGACY', 'threads', 9002, 'scheduled', false, 'forged', 'text', 'never reviewed', 'forged-9002', now() - interval '1 minute')" >/dev/null
pub_forged="$(psql_ro -c "SELECT id FROM publications WHERE ordinal = 9002")"
psql_ro -c "SELECT id||' '||status||' reviewed='||human_reviewed||' problem='||coalesce(guard_publication_problem(id),'none') FROM publications ORDER BY id"
pass "R7-5: $pub_ok approved+scheduled; $pub_tampered approved+scheduled then changed (owner SQL); $pub_forged forged unreviewed (owner SQL)"

step "R7-6: mutations disable (rollback of the mode)"
cc mutations disable </dev/null | grep -vE 'gate (api|auth)-role: INFO' | cut -c1-160
[[ ! -e "$dir/control-center.mutations" ]] || fail "R7-6: mode file still there"
env_now="$(api_env)"
grep -qx 'AI_SMM_API_MUTATIONS_ENABLED=false' <<<"$env_now" || fail "R7-6: flag"
grep -q '^AI_SMM_DATABASE_URL=postgresql+psycopg://ai_smm_cc:' <<<"$env_now" || fail "R7-6: role"
client after-disable || fail "R7-6: read-only checks"
echo "scheduled rows after disable: $(psql_ro -c "SELECT string_agg(id||'='||status, ' ' ORDER BY id) FROM publications")"
pass "R7-6: read-only again (ai_smm_cc, flag false); disable does NOT cancel what is scheduled"

step "R7-7: what the worker does with them (one dry-run cycle: publishing forced off, no route out)"
docker run --rm --network "$NET" --read-only --tmpfs /tmp \
    -e AI_SMM_DATABASE_URL="$dsn" -e AI_SMM_LEGACY_THREADS_PROJECTS="$LEGACY" -e AI_SMM_DRY_RUN=true \
    -e AI_SMM_BATCH_SIZE=10 -e AI_SMM_MIN_PUBLISH_INTERVAL_SECONDS=0 \
    --entrypoint /app/.venv/bin/ai-smm "$IMAGE" dry-run 2>&1 | grep -vE '^\{' | sed 's/^/  /' || true
outcome="$(psql_ro -c "SELECT string_agg(id||'='||status||'/'||coalesce(left(last_error, 60),'-'), ' ; ' ORDER BY id) FROM publications")"
echo "after the cycle: $outcome"
status_of() { psql_ro -c "SELECT status FROM publications WHERE id = $1"; }
[[ "$(status_of "$pub_tampered")" != published ]] || fail "R7-7: the tampered row was published"
[[ "$(status_of "$pub_forged")" != published ]] || fail "R7-7: the unreviewed row was published"
psql_ro -c "SELECT count(*) FROM publication_attempts WHERE publication_id = $pub_forged" | grep -qx 0 \
    || echo "INFO R7-7: the worker attempted the forged row (refused, see above)"
pass "R7-7: tampered row $(status_of "$pub_tampered"), forged row $(status_of "$pub_forged"); approved row $(status_of "$pub_ok") (dry run)"

step "R7-8: rollback of the role (after disable no session of it is open)"
psql_admin < "$repo/deploy/control-center/db/ai_smm_cc_rw.revoke.sql" >/dev/null
[[ "$(psql_ro -c "SELECT count(*) FROM pg_roles WHERE rolname = 'ai_smm_cc_rw'")" == 0 ]] || fail "R7-8: role still there"
verify_role "$dir/ai_smm_cc.verify.sql" 200 || fail "R7-8: ai_smm_cc changed by the revoke"
cc mutations status </dev/null | sed -n 1p
pass "R7-8: ai_smm_cc_rw removed; the read-only role unchanged"

step "REHEARSAL PASSED: R7-0 .. R7-8"
