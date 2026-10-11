# Shared by cc-compose.sh and cc-db-backup.sh. Sourced, never executed.
#
# Validates the Control Center's deployment directory and pins the Docker
# daemon every docker command talks to:
#
#   control-center.target   SMM_CC_DOCKER_HOST=unix:///var/run/docker.sock
#                           SMM_CC_DOCKER_ENGINE_ID=<docker info --format '{{.ID}}'>
#
# The Docker CLI otherwise picks its daemon from DOCKER_HOST, DOCKER_CONTEXT
# or the "current context" stored in ~/.docker/config.json -- the last one
# invisibly. Every docker call made through cc_docker runs with a clean
# environment and DOCKER_HOST set to the pinned endpoint, which takes
# precedence over any context, and cc_verify_engine checks that the
# daemon answering there is the one recorded in the target file.

readonly CC_PROJECT=ai-smm-control-center
readonly CC_COMPOSE_FILE_NAME=compose.control-center.yml
readonly CC_INTERP_FILE_NAME=control-center.compose.env
readonly CC_SECRET_FILE_NAME=control-center.env
readonly CC_TARGET_FILE_NAME=control-center.target
readonly CC_REVISION_LABEL=org.opencontainers.image.revision

cc_die() {
    echo "${CC_SELF:-cc}: $*" >&2
    exit 2
}

# Prints the keys of a dotenv file; refuses a line that is not KEY=value.
cc_env_keys() {
    local file="$1" line

    while IFS= read -r line || [[ -n "$line" ]]; do
        [[ "$line" =~ ^[[:space:]]*(#.*)?$ ]] && continue

        if [[ "$line" =~ ^([A-Z][A-Z0-9_]*)= ]]; then
            echo "${BASH_REMATCH[1]}"
        else
            cc_die "$file: not a KEY=value line (no 'export', no spaces around '=')"
        fi
    done < "$file"
}

# Exactly the listed keys, each once.
cc_check_keys() {
    local file="$1"
    shift
    local expected actual

    expected="$(printf '%s\n' "$@" | sort)"
    actual="$(cc_env_keys "$file" | sort)"

    [[ "$actual" == "$expected" ]] \
        || cc_die "$file must define exactly: $* (found: $(echo $actual))"
}

cc_value_of() {
    sed -n "s/^$2=//p" "$1" | tail -n 1
}

# A regular file, not a symlink: a link could point at the worker's files.
cc_require_file() {
    [[ ! -L "$1" ]] || cc_die "$1 is a symlink; copy the file instead"
    [[ -f "$1" ]] || cc_die "missing $1"
}

# Sets CC_DIR and validates the directory itself.
cc_load_dir() {
    local foreign

    CC_DIR="${SMM_CC_DIR:-$1}"
    [[ -d "$CC_DIR" ]] || cc_die "SMM_CC_DIR is not a directory: $CC_DIR"
    CC_DIR="$(cd "$CC_DIR" && pwd -P)"

    for foreign in .env compose.yaml compose.yml docker-compose.yml docker-compose.yaml ai-smm.env; do
        [[ ! -e "$CC_DIR/$foreign" && ! -L "$CC_DIR/$foreign" ]] \
            || cc_die "$CC_DIR holds $foreign; the Control Center must live in a directory of its own"
    done
}

# Sets CC_VERSION and CC_NETWORK from control-center.compose.env.
cc_load_compose_env() {
    local file="$CC_DIR/$CC_INTERP_FILE_NAME"

    cc_require_file "$file"
    cc_check_keys "$file" SMM_CC_SHARED_NETWORK SMM_CC_VERSION

    CC_VERSION="$(cc_value_of "$file" SMM_CC_VERSION)"
    [[ "$CC_VERSION" =~ ^[0-9a-f]{40}$ ]] \
        || cc_die "SMM_CC_VERSION must be the full 40-hex commit id the images were built from, got '$CC_VERSION'"

    CC_NETWORK="$(cc_value_of "$file" SMM_CC_SHARED_NETWORK)"
    [[ -n "$CC_NETWORK" ]] || cc_die "SMM_CC_SHARED_NETWORK is empty"
    [[ "$CC_NETWORK" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] \
        || cc_die "SMM_CC_SHARED_NETWORK is not a network name: '$CC_NETWORK'"
}

# Sets CC_DSN from control-center.env: exactly one key, mode 600. The
# value is a secret: it is compared, never printed.
cc_load_secret() {
    local file="$CC_DIR/$CC_SECRET_FILE_NAME" mode

    cc_require_file "$file"
    cc_check_keys "$file" AI_SMM_DATABASE_URL

    mode="$(stat -c '%a' "$file")"
    (( (8#$mode & 8#077) == 0 )) \
        || cc_die "$file is mode $mode; it holds the database password and must be 600"

    CC_DSN="$(cc_value_of "$file" AI_SMM_DATABASE_URL)"
}

# Sets CC_DOCKER_HOST and CC_ENGINE_ID; refuses an ambiguous caller.
cc_load_target() {
    local target="$CC_DIR/$CC_TARGET_FILE_NAME" socket

    cc_require_file "$target"
    cc_check_keys "$target" SMM_CC_DOCKER_HOST SMM_CC_DOCKER_ENGINE_ID

    CC_DOCKER_HOST="$(cc_value_of "$target" SMM_CC_DOCKER_HOST)"
    CC_ENGINE_ID="$(cc_value_of "$target" SMM_CC_DOCKER_ENGINE_ID)"

    # A local socket only: no tcp://, no ssh://, no relative path.
    [[ "$CC_DOCKER_HOST" =~ ^unix:///[A-Za-z0-9._/-]+$ && "$CC_DOCKER_HOST" != *..* ]] \
        || cc_die "SMM_CC_DOCKER_HOST must be unix:///<absolute socket path>, got '$CC_DOCKER_HOST'"
    [[ "$CC_ENGINE_ID" =~ ^[A-Za-z0-9][A-Za-z0-9:-]{7,127}$ ]] \
        || cc_die "SMM_CC_DOCKER_ENGINE_ID is not a Docker engine id: '$CC_ENGINE_ID'"

    socket="${CC_DOCKER_HOST#unix://}"
    [[ -S "$socket" ]] || cc_die "no Docker socket at $socket"

    # Never silently override what the operator's shell says: if it names
    # a daemon, it must be the pinned one.
    [[ -z "${DOCKER_CONTEXT:-}" ]] \
        || cc_die "DOCKER_CONTEXT is set ('$DOCKER_CONTEXT'); unset it -- the daemon is pinned in $target"
    [[ -z "${DOCKER_HOST:-}" || "$DOCKER_HOST" == "$CC_DOCKER_HOST" ]] \
        || cc_die "DOCKER_HOST is '$DOCKER_HOST' but $target pins '$CC_DOCKER_HOST'"
}

# docker, against the pinned daemon only, with nothing else inherited.
cc_docker() {
    local env=(PATH="$PATH" HOME="${HOME:-/root}" DOCKER_HOST="$CC_DOCKER_HOST")
    [[ -n "${TERM:-}" ]] && env+=(TERM="$TERM")

    env -i "${env[@]}" docker "$@"
}

cc_verify_engine() {
    local actual

    actual="$(cc_docker info --format '{{.ID}}' 2>/dev/null)" \
        || cc_die "the Docker daemon at $CC_DOCKER_HOST does not answer"
    [[ "$actual" == "$CC_ENGINE_ID" ]] \
        || cc_die "the daemon at $CC_DOCKER_HOST is engine '$actual', not the pinned '$CC_ENGINE_ID'"
}
