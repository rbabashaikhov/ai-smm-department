#!/usr/bin/env bash
# The only supported way to run compose.control-center.yml.
#
#   SMM_CC_DIR=/root/ai-smm-control-center cc-compose.sh <compose args...>
#
# SMM_CC_DIR defaults to the directory this script is in, and must hold:
#
#   compose.control-center.yml    the compose file, copied unchanged
#   control-center.compose.env    interpolation: SMM_CC_VERSION, SMM_CC_SHARED_NETWORK
#   control-center.env            container env: AI_SMM_DATABASE_URL only, mode 600
#
# Why a wrapper: Compose takes its project name from COMPOSE_PROJECT_NAME
# -- in the shell or in a .env file in the working directory -- before
# the file's own `name:`. Run from the worker's directory, a bare
# `docker compose -f compose.control-center.yml down` would act on the
# worker's project. This script:
#
#   * refuses a directory that holds a .env, compose.yaml or ai-smm.env
#     (the worker's files);
#   * refuses env files with any key outside their allowlist, and a
#     secret file readable by group or others;
#   * refuses a version that is not a git commit (no "latest");
#   * pins the project name, the compose file and the interpolation file,
#     and runs Compose with a clean environment, so nothing exported in
#     the operator's shell reaches interpolation;
#   * before anything that creates a container, checks that the loaded
#     images were built from that commit (OCI revision label).
#
# It changes nothing itself: every check is a read.

set -euo pipefail

readonly PROJECT=ai-smm-control-center
readonly COMPOSE_FILE_NAME=compose.control-center.yml
readonly INTERP_FILE_NAME=control-center.compose.env
readonly SECRET_FILE_NAME=control-center.env
readonly REVISION_LABEL=org.opencontainers.image.revision

die() {
    echo "cc-compose: $*" >&2
    exit 2
}

dir="${SMM_CC_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
[[ -d "$dir" ]] || die "SMM_CC_DIR is not a directory: $dir"
dir="$(cd "$dir" && pwd)"

compose_file="$dir/$COMPOSE_FILE_NAME"
interp_file="$dir/$INTERP_FILE_NAME"
secret_file="$dir/$SECRET_FILE_NAME"

for foreign in .env compose.yaml compose.yml docker-compose.yml ai-smm.env; do
    [[ ! -e "$dir/$foreign" ]] \
        || die "$dir holds $foreign; the Control Center must live in a directory of its own"
done

for required in "$compose_file" "$interp_file" "$secret_file"; do
    [[ -f "$required" ]] || die "missing $required"
done

# Prints the keys of a dotenv file; fails on a line that is not KEY=value.
env_keys() {
    local file="$1" line

    while IFS= read -r line || [[ -n "$line" ]]; do
        [[ "$line" =~ ^[[:space:]]*(#.*)?$ ]] && continue

        if [[ "$line" =~ ^([A-Z][A-Z0-9_]*)= ]]; then
            echo "${BASH_REMATCH[1]}"
        else
            die "$file: not a KEY=value line (no 'export', no spaces around '=')"
        fi
    done < "$file"
}

# Exactly the listed keys, each once.
check_keys() {
    local file="$1"
    shift
    local expected actual

    expected="$(printf '%s\n' "$@" | sort)"
    actual="$(env_keys "$file" | sort)"

    [[ "$actual" == "$expected" ]] \
        || die "$file must define exactly: $* (found: $(echo $actual))"
}

check_keys "$interp_file" SMM_CC_SHARED_NETWORK SMM_CC_VERSION
check_keys "$secret_file" AI_SMM_DATABASE_URL

mode="$(stat -c '%a' "$secret_file")"
(( (8#$mode & 8#077) == 0 )) \
    || die "$secret_file is mode $mode; it holds the database password and must be 600"

value_of() {
    sed -n "s/^$2=//p" "$1" | tail -n 1
}

version="$(value_of "$interp_file" SMM_CC_VERSION)"
[[ "$version" =~ ^[0-9a-f]{7,40}$ ]] \
    || die "SMM_CC_VERSION must be a git commit id (7-40 lowercase hex), got '$version'"

[[ -n "$(value_of "$interp_file" SMM_CC_SHARED_NETWORK)" ]] \
    || die "SMM_CC_SHARED_NETWORK is empty"

# Subcommands that create or start containers from the images.
creates_containers=false
for arg in "$@"; do
    case "$arg" in
        up|run|create|start|restart)
            creates_containers=true
            break
            ;;
    esac
done

if [[ "$creates_containers" == true ]]; then
    for image in "ai-smm-api:$version" "ai-smm-web:$version"; do
        revision="$(docker image inspect --format "{{ index .Config.Labels \"$REVISION_LABEL\" }}" "$image" 2>/dev/null)" \
            || die "image $image is not loaded on this host"

        [[ -n "$revision" && "$revision" == "$version"* ]] \
            || die "image $image carries $REVISION_LABEL='$revision', expected commit $version"
    done
fi

# A clean environment: nothing exported in this shell -- COMPOSE_*, a
# stray SMM_CC_*, DOCKER_DEFAULT_PLATFORM -- reaches Compose. HOME is
# needed for the Docker CLI config, TERM for an interactive `run`.
clean_env=(PATH="$PATH" HOME="${HOME:-/root}" TERM="${TERM:-dumb}")
[[ -n "${DOCKER_HOST:-}" ]] && clean_env+=(DOCKER_HOST="$DOCKER_HOST")
[[ -n "${DOCKER_CONTEXT:-}" ]] && clean_env+=(DOCKER_CONTEXT="$DOCKER_CONTEXT")

exec env -i "${clean_env[@]}" \
    docker compose \
        --project-name "$PROJECT" \
        --project-directory "$dir" \
        --file "$compose_file" \
        --env-file "$interp_file" \
        "$@"
