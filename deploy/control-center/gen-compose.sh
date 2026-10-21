#!/usr/bin/env bash
# The ONLY way to run compose.generation.yml on the host (Generation-A1).
#
#   gen-compose.sh config | ps | logs [--since N{s,m,h}] [--tail N] | check | down
#   gen-compose.sh up        < the job's facts, from a read-only query (below)
#
# Not a pass-through, like cc-compose.sh (docs/control-center-deployment.md
# §2): it accepts a fixed set of verbs and builds the whole Compose command
# line itself, from a clean environment, and it validates the two env files
# BEFORE Compose can read them:
#
#   generation.env          exactly AI_SMM_DATABASE_URL + OPENAI_API_KEY, each
#                           once, mode 600, plain KEY=value (no quotes, no
#                           `export`, no interpolation); the URL must name the
#                           login ai_smm_generation. Any other variable --
#                           OPENAI_BASE_URL, OPENAI_ORG_ID, THREADS_*, an
#                           unknown one -- is refused, so an env_file can not
#                           redirect or widen the worker.
#   generation.compose.env  exactly SMM_GEN_VERSION, SMM_GEN_SHARED_NETWORK,
#                           SMM_GEN_KNOWLEDGE_PATH.
#
# `up` additionally refuses to start the worker unless
#   * OPENAI_API_KEY is non-empty (an empty key would make the worker claim
#     real jobs and fail them, burning their attempts -- stopping the worker
#     does not give those back). Only presence is checked: whether OpenAI
#     accepts the key is NOT verified without an external request, which this
#     wrapper never makes. A production E2E with a key needs the owner's
#     separate approval of that key and its budget (runbook), and
#   * the first authorised run is exactly one fresh job. Its facts are piped
#     in (docs/generation-worker-deployment.md gives the read-only psql query:
#     status, attempt_count, max_attempts, params, open_jobs) and must show a
#     queued job, attempt_count 0, series_size 1, max_attempts <= 2 (stored
#     per job, up to 10 -- the CLI does not print it) and no other open job,
#     because the worker takes ANY due job. The text is parsed strictly (the
#     `key : value` lines, params as JSON, top-level keys only, duplicate JSON
#     keys refused). This is an operator ATTESTATION: PostgreSQL is not read
#     here, so it is not authoritative.
# Empty-key and FakeGraph tests run on a disposable database only, never here.
#
# Values are never printed. Exit 2 = refused before Docker is called.
set -euo pipefail

CC_SELF=gen-compose.sh
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=cc-common.sh
source "$HERE/cc-common.sh"

readonly GEN_PROJECT=ai-smm-generation
readonly GEN_FILE=compose.generation.yml
readonly GEN_INTERP=generation.compose.env
readonly GEN_SECRET=generation.env
readonly GEN_SAFE_VALUE='^[A-Za-z0-9_@:/.%+=,~-]*$'

# Strict parse of the attested job facts. Fail closed: any missing, repeated,
# mistyped or out-of-range field exits 1. Only TOP-LEVEL params keys count.
readonly GEN_JOB_CHECK='
import json, re, sys

def refuse(why):
    print("up: " + why, file=sys.stderr)
    sys.exit(1)

def pairs(items):
    out = {}
    for k, v in items:
        if k in out:
            refuse("duplicate JSON key in params")
        out[k] = v
    return out

fields = {}
for line in sys.stdin.read().splitlines():
    if not line.strip():
        continue
    name, sep, value = line.partition(":")
    name = name.strip()
    if not sep or name in fields:
        refuse("malformed or repeated line")
    fields[name] = value.strip()

def count(name):
    value = fields.get(name, "")
    if not re.fullmatch(r"[0-9]{1,6}", value):
        refuse(name + " missing or not a number")
    return int(value)

if fields.get("status") != "queued":
    refuse("the job is not queued")
if count("attempt_count") != 0:
    refuse("the job has already been attempted")
if count("max_attempts") not in (1, 2):
    refuse("max_attempts must be 1 or 2")
if count("open_jobs") != 1:
    refuse("exactly one job may be queued or claimed")
try:
    params = json.loads(fields.get("params", ""), object_pairs_hook=pairs)
except ValueError:
    refuse("params is not valid JSON")
if type(params) is not dict:
    refuse("params is not a JSON object")
size = params.get("series_size")
if type(size) is not int or size != 1:
    refuse("series_size must be 1")
if params.get("publishing_strategy") != "standalone_series":
    refuse("publishing_strategy must be standalone_series")
'

usage() {
    cat <<'EOF'
usage: gen-compose.sh config | ps | check | down
       gen-compose.sh logs [--since N{s,m,h}] [--tail N]
       gen-compose.sh up   < <read-only query of the job; see the runbook>
EOF
}

[[ $# -gt 0 ]] || { usage >&2; exit 2; }
verb="$1"
shift

case "$verb" in
    help|-h|--help) usage; exit 0 ;;
esac

# -- the directory ---------------------------------------------------------
DIR="${SMM_GEN_DIR:-$HERE}"
[[ -d "$DIR" ]] || cc_die "not a directory: $DIR"
DIR="$(cd "$DIR" && pwd -P)"
for foreign in .env compose.yaml compose.yml docker-compose.yml docker-compose.yaml \
    compose.override.yaml compose.override.yml ai-smm.env control-center.env; do
    [[ ! -e "$DIR/$foreign" && ! -L "$DIR/$foreign" ]] \
        || cc_die "$DIR holds $foreign; the generation worker must live in a directory of its own"
done

# -- the daemon: the host's own, never a context ---------------------------
[[ -z "${DOCKER_CONTEXT:-}" ]] || cc_die "DOCKER_CONTEXT is set; unset it"
[[ -z "${DOCKER_HOST:-}" || "$DOCKER_HOST" == unix:///var/run/docker.sock ]] \
    || cc_die "DOCKER_HOST points at another daemon"
docker_env=(PATH="$PATH" HOME="${HOME:-/root}" DOCKER_HOST=unix:///var/run/docker.sock)

# -- the files -------------------------------------------------------------
cc_require_file "$DIR/$GEN_FILE"
cc_require_file "$DIR/$GEN_INTERP"
cc_require_file "$DIR/$GEN_SECRET"

# Plain KEY=value only: Compose would otherwise expand $, strip quotes or
# read a quoted value across lines, so what we validate is what it loads.
check_plain_values() {
    local file="$1" line value
    while IFS= read -r line || [[ -n "$line" ]]; do
        [[ "$line" =~ ^[[:space:]]*(#.*)?$ ]] && continue
        value="${line#*=}"
        [[ "$value" =~ $GEN_SAFE_VALUE ]] \
            || cc_die "$file: a value holds a quote, space, '\$', backslash or other character a plain KEY=value file must not"
    done < "$file"
}

cc_check_keys "$DIR/$GEN_INTERP" SMM_GEN_VERSION SMM_GEN_SHARED_NETWORK SMM_GEN_KNOWLEDGE_PATH
check_plain_values "$DIR/$GEN_INTERP"
version="$(cc_value_of "$DIR/$GEN_INTERP" SMM_GEN_VERSION)"
[[ "$version" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$ && "$version" != latest ]] \
    || cc_die "SMM_GEN_VERSION must be a release tag, never latest"
[[ "$(cc_value_of "$DIR/$GEN_INTERP" SMM_GEN_SHARED_NETWORK)" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] \
    || cc_die "SMM_GEN_SHARED_NETWORK is not a network name"
[[ "$(cc_value_of "$DIR/$GEN_INTERP" SMM_GEN_KNOWLEDGE_PATH)" =~ ^/[A-Za-z0-9_./-]+$ ]] \
    || cc_die "SMM_GEN_KNOWLEDGE_PATH must be an absolute path"

cc_check_keys "$DIR/$GEN_SECRET" AI_SMM_DATABASE_URL OPENAI_API_KEY
check_plain_values "$DIR/$GEN_SECRET"
mode="$(stat -c '%a' "$DIR/$GEN_SECRET")"
(( (8#$mode & 8#077) == 0 )) || cc_die "$DIR/$GEN_SECRET is mode $mode; it holds secrets and must be 600"
dsn="$(cc_value_of "$DIR/$GEN_SECRET" AI_SMM_DATABASE_URL)"
[[ "$dsn" =~ ^postgresql\+psycopg://ai_smm_generation:[A-Za-z0-9._~%+-]+@[A-Za-z0-9][A-Za-z0-9._-]*(:[0-9]+)?/ai_smm$ ]] \
    || cc_die "AI_SMM_DATABASE_URL must be postgresql+psycopg://ai_smm_generation:<password>@<host>[:port]/ai_smm, no query string"
openai_key="$(cc_value_of "$DIR/$GEN_SECRET" OPENAI_API_KEY)"
unset dsn

compose() {
    env -i "${docker_env[@]}" docker compose --project-name "$GEN_PROJECT" \
        --project-directory "$DIR" --file "$DIR/$GEN_FILE" --env-file "$DIR/$GEN_INTERP" "$@"
}

# -- the verbs -------------------------------------------------------------
case "$verb" in
    config)
        [[ $# -eq 0 ]] || cc_die "config takes no arguments"
        compose config --quiet && echo "config: OK (files validated, no value printed)"
        ;;
    ps)
        [[ $# -eq 0 ]] || cc_die "ps takes no arguments"
        compose ps -a
        ;;
    check)
        [[ $# -eq 0 ]] || cc_die "check takes no arguments"
        compose exec smm-generation /app/.venv/bin/ai-smm-generation-worker --check
        ;;
    down)
        [[ $# -eq 0 ]] || cc_die "down takes no arguments (never -v or --rmi)"
        compose down
        ;;
    logs)
        args=(logs --no-color)
        while [[ $# -gt 0 ]]; do
            case "$1" in
                --since) [[ "${2:-}" =~ ^[0-9]{1,4}[smh]$ ]] || cc_die "logs: --since N{s,m,h}"; args+=(--since "$2"); shift 2 ;;
                --tail) [[ "${2:-}" =~ ^[0-9]{1,5}$ ]] || cc_die "logs: --tail N"; args+=(--tail "$2"); shift 2 ;;
                *) cc_die "logs: unsupported argument '$1'" ;;
            esac
        done
        compose "${args[@]}" smm-generation
        ;;
    up)
        [[ $# -eq 0 ]] || cc_die "up takes no arguments; the job's status comes on stdin"
        # F2: no key, no real queue. Presence only: validity is OpenAI's to judge.
        [[ -n "$openai_key" ]] \
            || cc_die "OPENAI_API_KEY is empty: the worker would claim real jobs and fail them (attempts are not restored). Test with an empty key on a disposable database only"
        unset openai_key
        # F3: the first run is exactly one job, as attested on stdin.
        [[ ! -t 0 ]] || cc_die "up: pipe in the job's facts (read-only query in the runbook)"
        status="$(head -c 8192)"
        python3 -I -c "$GEN_JOB_CHECK" <<<"$status" || cc_die "up: the job's facts do not describe exactly one fresh, bounded job"
        compose up --detach --wait --wait-timeout 120 --no-build --pull never smm-generation
        ;;
    *)
        cc_die "unsupported verb '$verb'"
        ;;
esac
