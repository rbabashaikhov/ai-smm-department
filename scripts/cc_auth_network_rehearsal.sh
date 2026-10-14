#!/usr/bin/env bash
# Local network rehearsal of smm-auth's isolation (ADR-024C-3A §9.5,
# tests T-N1 to T-N5), against DISPOSABLE stand-ins for the production
# pieces -- the release gate the ADR requires before any rollout:
#
#   <shared-network>       -> network   ai-smm-cc-authrh-shared
#   <postgres-container>   -> container ai-smm-cc-authrh-postgres
#   ai-smm-worker          -> container ai-smm-cc-authrh-worker (idle stand-in)
#
#   scripts/cc_auth_network_rehearsal.sh <full 40-hex image version>
#
# The images ai-smm-api:<version> / ai-smm-web:<version> must exist,
# built from the tree under test and labelled as the deployment guide's
# section 3 describes. The Control Center is started THROUGH THE WRAPPER
# (identity, API role, auth role and queue gates, with the real verify
# files against the real roles); only the schema, the roles and the
# deliberately widened bind of T-N3 are set up outside it.
#
#   T-N1  from a container on the shared network: TCP to smm-auth:8000 is
#         refused; smm-auth's only listening socket is its cc-auth address
#   T-N2  from smm-web, smm-auth resolves to its cc-auth address; sign-in,
#         /auth/me (smm-api) and sign-out work through nginx
#   T-N3  with the bind widened to 0.0.0.0, the application's allowlist
#         answers 403 to the shared network and still serves smm-web
#   T-N4  smm-api cannot open a TCP connection to smm-auth on any network
#   T-N5  the cc-auth subnet overlaps no route or Docker network on this
#         host; bind and healthcheck survive a restart by the restart
#         policy (which does not pass through the wrapper)
#
# Everything is created here and removed on exit. Needs port 8088 free.
# No secret is printed: passwords are random, live only in the work
# directory (mode 700) and in the disposable database.

set -euo pipefail

version="${1:?usage: $0 <full 40-hex image version>}"
[[ "$version" =~ ^[0-9a-f]{40}$ ]] || { echo "the version must be 40 hex characters" >&2; exit 2; }
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

readonly NET=ai-smm-cc-authrh-shared
readonly PG=ai-smm-cc-authrh-postgres
readonly WORKER=ai-smm-cc-authrh-worker
readonly PROBE_IMAGE="ai-smm-api:$version"
# postgres:16.15-bookworm, as in compose.runtime.yml
readonly PG_IMAGE=postgres@sha256:0ea6700a3b4f0ae6ce746519073558aed4d88a79d8d07622a9a644946c7319c4
readonly AUTH_SUBNET=10.231.18.0/29
readonly AUTH_IP=10.231.18.2
readonly PROJECT=ai-smm-control-center

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

work="$(mktemp -d)"
chmod 700 "$work"
dir="$work/$PROJECT"
mkdir -p "$dir"

cc() { "$dir/cc-compose.sh" "$@"; }
# As the instance's superuser, like the operator (the owner role ai_smm is
# not a superuser, as in production).
psql_admin() { docker exec -i "$PG" psql -X -q -v ON_ERROR_STOP=1 -U pgadmin -d "${PSQL_DB:-ai_smm}" -tA "$@"; }
random_secret() { od -An -N16 -tx1 /dev/urandom | tr -d ' \n'; }
cid_of() { docker ps -q --filter "label=com.docker.compose.project=$PROJECT" --filter "label=com.docker.compose.service=$1"; }
# Direct compose, for the steps the wrapper rightly refuses (T-N3 only).
compose_direct() {
    docker compose --project-name "$PROJECT" --project-directory "$dir" \
        --file "$dir/compose.control-center.yml" --env-file "$dir/control-center.compose.env" "$@"
}

# A Python snippet in a throwaway container on a network.
probe_on() {
    local network="$1"
    shift
    docker run --rm --network "$network" --read-only --entrypoint python "$PROBE_IMAGE" -c "$@"
}

readonly CONNECT_PY='
import socket, sys
host, port = sys.argv[1], int(sys.argv[2])
try:
    ip = socket.gethostbyname(host)
except OSError as exc:
    print(f"{host}: does not resolve ({type(exc).__name__})"); sys.exit(0)
s = socket.socket(); s.settimeout(4)
try:
    s.connect((ip, port))
except ConnectionRefusedError:
    print(f"{host} -> {ip}:{port}: refused"); sys.exit(0)
except OSError as exc:
    print(f"{host} -> {ip}:{port}: no connection ({type(exc).__name__})"); sys.exit(0)
print(f"{host} -> {ip}:{port}: CONNECTED"); sys.exit(1)
'

# Exit 0 if NO TCP connection to $2:$3 can be opened from $1 (a network
# name, or "exec:<container>").
no_connection() {
    local where="$1" host="$2" port="$3"

    if [[ "$where" == exec:* ]]; then
        docker exec "${where#exec:}" python -c "$CONNECT_PY" "$host" "$port"
    else
        docker run --rm --network "$where" --read-only --entrypoint python "$PROBE_IMAGE" \
            -c "$CONNECT_PY" "$host" "$port"
    fi
}

# The IPv4 listening sockets on port 8000 in a container, as a.b.c.d.
listeners_8000() {
    docker exec "$1" python -c '
import socket, struct
for line in open("/proc/net/tcp").read().splitlines()[1:]:
    local, state = line.split()[1], line.split()[3]
    address, port = local.split(":")
    if state == "0A" and int(port, 16) == 8000:
        print(socket.inet_ntoa(struct.pack("<I", int(address, 16))))
'
}

cleanup() {
    step "cleanup"
    [[ -x "$dir/cc-compose.sh" ]] && cc down --remove-orphans >/dev/null 2>&1 || true
    compose_direct down --remove-orphans >/dev/null 2>&1 || true
    docker rm -f "$WORKER" "$PG" >/dev/null 2>&1 || true
    docker network rm "$NET" >/dev/null 2>&1 || true
    rm -rf "$work"
}
trap cleanup EXIT

step "daemon: $(docker info --format '{{.Name}}') engine $engine_id at $DOCKER_HOST"
docker compose version

step "T-N5 (facts): the cc-auth subnet against every route and Docker network here"
others="$( (ip -4 route show 2>/dev/null | awk '{print $1}' | grep -E '^[0-9]+\.' || true;
           docker network ls -q | xargs -r docker network inspect \
               --format '{{.Name}} {{range .IPAM.Config}}{{.Subnet}} {{end}}' \
             | grep -v '^ai-smm-cc-auth ' | awk '{for (i = 2; i <= NF; i++) print $i}') | sort -u)"
"$repo/.venv/bin/python" -c '
import ipaddress, sys
auth = ipaddress.ip_network(sys.argv[1])
clash = [n for n in sys.argv[2].split() if ":" not in n and ipaddress.ip_network(n, strict=False).overlaps(auth)]
print("checked:", " ".join(sys.argv[2].split()) or "<none>")
sys.exit(f"overlaps: {clash}" if clash else 0)
' "$AUTH_SUBNET" "$others" || fail "T-N5: $AUTH_SUBNET overlaps a network on this host"
pass "T-N5a: $AUTH_SUBNET overlaps no route or Docker network on this host"

step "stand-ins: shared network, database, worker"
docker network create "$NET" >/dev/null
admin_password="$(random_secret)"
pg_password="$(random_secret)"
docker run -d --name "$PG" --network "$NET" \
    -e POSTGRES_USER=pgadmin -e POSTGRES_PASSWORD="$admin_password" -e POSTGRES_DB=postgres \
    "$PG_IMAGE" >/dev/null
dsn="postgresql+psycopg://ai_smm:$pg_password@$PG:5432/ai_smm"
docker run -d --name "$WORKER" --network "$NET" --read-only \
    -e AI_SMM_DATABASE_URL="$dsn" -e AI_SMM_LEGACY_THREADS_PROJECTS= \
    --entrypoint python "$PROBE_IMAGE" -c "import time; time.sleep(10**9)" >/dev/null
for _ in $(seq 60); do
    docker exec "$PG" pg_isready -U pgadmin -d postgres >/dev/null 2>&1 && break
    sleep 1
done
sleep 2
printf "CREATE ROLE ai_smm LOGIN PASSWORD '%s';\nCREATE DATABASE ai_smm OWNER ai_smm;\n" "$pg_password" \
    | PSQL_DB=postgres psql_admin >/dev/null

step "schema and roles (outside the runbook: P2-P4 of ADR-024C-3A are not under test here)"
docker run --rm --network "$NET" -e AI_SMM_DATABASE_URL="$dsn" \
    --entrypoint /app/.venv/bin/alembic -w /app "$PROBE_IMAGE" upgrade head 2>&1 | tail -n 1
for sql in ai_smm_cc.grants.sql smm_auth.functions.sql ai_smm_auth.grants.sql; do
    psql_admin < "$repo/deploy/control-center/db/$sql" >/dev/null
    echo "applied $sql"
done
api_password="$(random_secret)"
auth_password="$(random_secret)"
# On stdin, never in an argument list.
printf "ALTER ROLE ai_smm_cc PASSWORD '%s';\nALTER ROLE ai_smm_auth PASSWORD '%s';\n" \
    "$api_password" "$auth_password" | psql_admin >/dev/null
psql_admin -c "INSERT INTO projects (id, display_name, knowledge_path)
               VALUES ('rehearsal', 'Rehearsal', 'knowledge/projects/rehearsal')" >/dev/null

step "deployment directory, laid out as on the host"
cp "$repo/compose.control-center.yml" "$repo"/deploy/control-center/cc-*.sh \
    "$repo/deploy/control-center/db/ai_smm_cc.verify.sql" \
    "$repo/deploy/control-center/db/ai_smm_auth.verify.sql" "$dir/"
chmod 755 "$dir/cc-compose.sh" "$dir/cc-db-backup.sh"
public_key="$(head -c 32 /dev/urandom | base64)"
printf '%s\n' "SMM_CC_VERSION=$version" "SMM_CC_SHARED_NETWORK=$NET" \
    "SMM_CC_CREDENTIALS_PUBLIC_KEY=$public_key" "SMM_CC_LEGACY_THREADS_PROJECTS=" \
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

step "the gates, through the wrapper (real verify files on the real roles)"
cc preflight </dev/null

step "owner and start (the wrapper's up: all gates, then the three HTTP services)"
owner_password="rehearsal-$(random_secret)"
printf '%s\n%s\n' "$owner_password" "$owner_password" \
    | cc create-owner --email owner@rehearsal.test --display-name "Rehearsal Owner" --project rehearsal \
        >/dev/null 2>&1
( umask 077; printf '%s' "$owner_password" > "$work/owner.pw" )
cc up </dev/null
running="$(cc ps --services </dev/null | sort | tr '\n' ' ')"
[[ "$running" == "smm-api smm-auth smm-web " ]] || fail "unexpected services: $running"
api="$(cid_of smm-api)" auth="$(cid_of smm-auth)" web="$(cid_of smm-web)"
for c in "$api" "$auth" "$web"; do
    echo "$(docker inspect -f '{{.Name}}: {{range $k, $v := .NetworkSettings.Networks}}{{$k}}={{$v.IPAddress}} {{end}}' "$c")"
done

step "T-N1: the shared network cannot reach smm-auth"
[[ "$(listeners_8000 "$auth")" == "$AUTH_IP" ]] \
    || fail "T-N1: smm-auth listens on '$(listeners_8000 "$auth" | tr '\n' ' ')', not only $AUTH_IP"
pass "T-N1a: smm-auth's only listening socket on :8000 is $AUTH_IP"
out="$(no_connection "$NET" smm-auth 8000)" || fail "T-N1: $out"
grep -q ": refused$" <<<"$out" || fail "T-N1: expected a TCP refusal, got: $out"
pass "T-N1b: from $NET: $out"
out="$(no_connection "$NET" "$AUTH_IP" 8000)" || fail "T-N1: $out"
pass "T-N1c: from $NET to the cc-auth address: $out"

step "T-N2: smm-web resolves smm-auth to its cc-auth address; sign-in and sign-out through nginx"
resolved="$(docker exec "$web" getent hosts smm-auth | awk '{print $1}' | sort -u | tr '\n' ' ')"
[[ "$resolved" == "$AUTH_IP " ]] || fail "T-N2: smm-web resolves smm-auth to '$resolved'"
pass "T-N2a: smm-web resolves smm-auth to $AUTH_IP"
"$repo/.venv/bin/python" - "$work/owner.pw" <<'PY' || fail "T-N2: sign-in / sign-out through nginx"
import http.cookiejar, json, sys, urllib.error, urllib.request

base = "http://127.0.0.1:8088"
password = open(sys.argv[1]).read()
jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

def call(method, path, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(base + path, data=data, method=method, headers={
        "Content-Type": "application/json", **(headers or {})})
    try:
        with opener.open(request, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()

status, _ = call("POST", "/api/v1/auth/login", {"email": "owner@rehearsal.test", "password": "wrong"})
assert status == 401, status
status, body = call("POST", "/api/v1/auth/login", {"email": "owner@rehearsal.test", "password": password})
assert status == 200, (status, body[:200])
csrf = json.loads(body)["csrf_token"]
status, body = call("GET", "/api/v1/auth/me")
assert status == 200 and json.loads(body)["user"]["email"] == "owner@rehearsal.test", status
status, _ = call("POST", "/api/v1/auth/logout", {}, {"X-CSRF-Token": csrf})
assert status in (200, 204), status
status, _ = call("GET", "/api/v1/auth/me")
assert status == 401, status
print("sign-in 401 then 200 via smm-auth; /auth/me 200 via smm-api; sign-out; /auth/me 401")
PY
pass "T-N2b: sign-in, session use and sign-out through nginx"
login_on_api="$(docker exec "$api" python -c '
import urllib.request, urllib.error
r = urllib.request.Request("http://127.0.0.1:8000/api/v1/auth/login", data=b"{}", method="POST",
                           headers={"Content-Type": "application/json"})
try:
    print(urllib.request.urlopen(r, timeout=4).status)
except urllib.error.HTTPError as e:
    print(e.code)
')"
[[ "$login_on_api" == 404 ]] || fail "T-N2: smm-api answers $login_on_api on its own /auth/login, expected 404"
pass "T-N2c: smm-api mounts no login (404 on its own port)"

step "T-N4: smm-api cannot reach smm-auth on any network"
out="$(no_connection "exec:$api" smm-auth 8000)" || fail "T-N4: $out"
pass "T-N4a: from smm-api by name: $out"
out="$(no_connection "exec:$api" "$AUTH_IP" 8000)" || fail "T-N4: $out"
pass "T-N4b: from smm-api to the cc-auth address: $out"
out="$(docker exec "$auth" python -c "$CONNECT_PY" smm-api 8000 || true)"
# smm-api still listens on 0.0.0.0 on the shared network: every container
# there reaches it, smm-auth included (FU-024B3-2, accepted in ADR-024C-3A
# §9.1-9.3). Recorded, not a gate: cookie and CSRF still apply.
echo "INFO T-N4c (reverse, FU-024B3-2): from smm-auth: $out"

step "T-N5: bind and health survive a restart by the restart policy"
restarts_before="$(docker inspect -f '{{.RestartCount}}' "$auth")"
docker exec "$auth" python -c "import os, signal; os.kill(1, signal.SIGTERM)" || true
for _ in $(seq 90); do
    sleep 1
    [[ "$(docker inspect -f '{{.RestartCount}} {{.State.Health.Status}}' "$auth")" == "$((restarts_before + 1)) healthy" ]] && break
done
[[ "$(docker inspect -f '{{.RestartCount}} {{.State.Health.Status}}' "$auth")" == "$((restarts_before + 1)) healthy" ]] \
    || fail "T-N5: smm-auth did not come back healthy after a restart ($(docker inspect -f '{{.RestartCount}} {{.State.Health.Status}}' "$auth"))"
[[ "$(listeners_8000 "$auth")" == "$AUTH_IP" ]] || fail "T-N5: the bind changed after the restart"
out="$(no_connection "$NET" smm-auth 8000)" || fail "T-N5: $out"
pass "T-N5b: restarted by Docker (restart count $((restarts_before + 1))), healthy, bind $AUTH_IP, shared network: $out"

step "T-N3: the bind widened to 0.0.0.0 (a deliberately broken build): the allowlist answers"
cat > "$dir/widened.yml" <<EOF
services:
  smm-auth:
    entrypoint: ["/app/.venv/bin/uvicorn", "ai_smm.api.auth_service:create_auth_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--no-server-header"]
EOF
compose_direct --file "$dir/widened.yml" up --detach --wait --wait-timeout 120 --no-deps smm-auth </dev/null >/dev/null
auth="$(cid_of smm-auth)"
[[ "$(listeners_8000 "$auth")" == "0.0.0.0" ]] || fail "T-N3: the widened build does not listen on 0.0.0.0"
answer="$(docker run --rm --network "$NET" --read-only --entrypoint python "$PROBE_IMAGE" -c '
import json, urllib.request, urllib.error
try:
    urllib.request.urlopen("http://smm-auth:8000/health/live", timeout=4); print("200")
except urllib.error.HTTPError as e:
    print(e.code, json.loads(e.read())["error"]["code"])
')"
[[ "$answer" == "403 FORBIDDEN" ]] || fail "T-N3: the shared network got '$answer' from a widened smm-auth"
pass "T-N3a: widened bind, shared network: $answer (application allowlist)"
answer="$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' \
    -d '{"email":"owner@rehearsal.test","password":"wrong"}' http://127.0.0.1:8088/api/v1/auth/login)"
[[ "$answer" == 401 ]] || fail "T-N3: smm-web got $answer from the widened smm-auth, expected 401"
pass "T-N3b: widened bind, smm-web (allowed peer): $answer for a wrong password"
echo "T-N3c: the packaging test pinning the bind is tests/test_control_center_packaging.py::test_auth_runs_the_auth_application_bound_to_its_address_only"

step "back to the packaged configuration, through the wrapper"
cc up </dev/null >/dev/null
auth="$(cid_of smm-auth)"
[[ "$(listeners_8000 "$auth")" == "$AUTH_IP" ]] || fail "the packaged bind did not come back"
out="$(no_connection "$NET" smm-auth 8000)" || fail "$out"
pass "packaged again: bind $AUTH_IP; shared network: $out"

step "the worker stand-in and the database are untouched by the Control Center"
docker inspect -f '{{.Name}} running={{.State.Running}} restarts={{.RestartCount}}' "$WORKER" "$PG"

step "REHEARSAL PASSED: T-N1, T-N2, T-N3, T-N4 (a, b), T-N5"
