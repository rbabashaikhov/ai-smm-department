#!/usr/bin/env bash
# The only supported way to run compose.control-center.yml.
#
#   cc-compose.sh <verb> [options]          (run with no arguments for the list)
#
# This is NOT a pass-through to `docker compose`. It accepts a fixed set of
# verbs, each with a fixed set of options and validated values, and builds
# the whole Compose command line itself. Nothing an operator types can add
# a Compose file, an env file, a project name or directory, a profile, an
# environment variable, a volume, a user, an entrypoint, or a command for
# the `migrate` and `cli` containers other than the ones listed below.
#
# SMM_CC_DIR (default: this script's directory) must hold:
#
#   compose.control-center.yml    the compose file, copied unchanged
#   control-center.compose.env    exactly SMM_CC_VERSION (full 40-hex commit)
#                                 and SMM_CC_SHARED_NETWORK
#   control-center.env            exactly AI_SMM_DATABASE_URL, mode 600: the
#                                 owner role, the worker's URL (migrate, cli)
#   control-center.api.env        exactly AI_SMM_DATABASE_URL, mode 600: the
#                                 API's role ai_smm_cc (smm-api only)
#   ai_smm_cc.verify.sql          deploy/control-center/db/, copied unchanged;
#                                 run by the API role gate (cc-gate.sh)
#   control-center.target         exactly SMM_CC_DOCKER_HOST (unix:// socket)
#                                 and SMM_CC_DOCKER_ENGINE_ID
#   control-center.identity       the recorded PostgreSQL identity (cc-gate.sh);
#                                 needed by preflight, migrate upgrade|downgrade,
#                                 create-owner
#   control-center.window         the agreed migration window, UTC (cc-gate.sh);
#                                 needed by preflight, migrate upgrade|downgrade
#   control-center.backup-evidence  written by cc-db-backup.sh verify; and
#   control-center.offhost-ack      written by the operator: both needed by
#                                 migrate upgrade (cc-gate.sh, backup evidence)
#
# and must not hold .env, compose.yaml or ai-smm.env (the worker's files).
#
# Before anything that talks to the daemon, the daemon at the pinned
# socket must report the pinned engine id. Before anything that creates a
# container, both images must exist on that daemon with an OCI revision
# label EQUAL to SMM_CC_VERSION and the daemon's architecture. Before
# anything that starts the API or the one-shot tools against the database
# (up, restart, migrate upgrade|downgrade, create-owner) the deployment
# gates (cc-gate.sh) must pass: the database is the recorded one; before
# the API starts (up, restart), its role is ai_smm_cc with exactly the
# designed privileges; for a schema change, the publication queue is quiet
# around the window; for an upgrade, a verified dump of this database at
# this revision exists and a copy of it was confirmed off the host. Fail
# closed, exit 2.
#
# Why: Compose takes its project name from a .env file or COMPOSE_* in the
# shell before the file's own `name:`, and the Docker CLI picks its daemon
# from a context stored in ~/.docker/config.json. Either could point a
# bare `docker compose down` at the worker's project or another host.

set -euo pipefail

CC_SELF=cc-compose
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/control-center/cc-common.sh
source "$here/cc-common.sh"
# shellcheck source=deploy/control-center/cc-gate.sh
source "$here/cc-gate.sh"

readonly HTTP_SERVICES="smm-api smm-web"
readonly ALL_SERVICES="smm-api smm-web migrate cli"

usage() {
    cat <<'EOF'
usage: cc-compose.sh <verb> [options]

  config [--quiet | --json | --services | --profiles] [--with-tools]
                                  render the configuration; never prints the
                                  database URL (env files are not resolved)
  up                              start smm-api and smm-web, wait until healthy;
                                  runs the identity and API role gates first
  down [--remove-orphans]         stop and remove this project's containers
                                  and networks (never volumes or images)
  ps [-a] [-q] [--services] [SERVICE...]
  logs [--since N{s|m|h}] [--tail N] [--timestamps] [smm-api|smm-web...]
  restart smm-api|smm-web...      runs the identity and API role gates first
  preflight                       the PostgreSQL identity, API role and
                                  publication-queue gates only, read-only;
                                  exit 2 if any fails
  migrate current | heads         alembic, read-only
  migrate upgrade REVISION        REVISION: a 12-hex alembic id, never head/+N;
                                  runs both gates and the backup evidence gate
  migrate downgrade REVISION      REVISION: a 12-hex alembic id, never base/-N;
                                  runs both gates, then invalidates the backup
                                  evidence
  create-owner --email E --display-name NAME --project ID
                                  ai-smm user create-owner; prompts for the
                                  password (reads it from stdin without a TTY);
                                  runs the identity gate first
EOF
}

in_list() {
    local needle="$1" item
    shift

    for item in "$@"; do
        [[ "$item" == "$needle" ]] && return 0
    done

    return 1
}

# -- the command line ------------------------------------------------------

[[ $# -gt 0 ]] || { usage >&2; exit 2; }

verb="$1"
shift

compose_args=()
needs_daemon=true
needs_images=false
# cc-gate.sh: identity of the database; the queue as well before a schema change
gate_identity=false
gate_api_role=false
gate_queue=false
gate_backup=false
invalidate_evidence=false

case "$verb" in
    help|-h|--help)
        usage
        exit 0
        ;;

    config)
        needs_daemon=false
        mode=""
        with_tools=false

        while [[ $# -gt 0 ]]; do
            case "$1" in
                --quiet|--json|--services|--profiles)
                    [[ -z "$mode" ]] || cc_die "config: one of --quiet, --json, --services, --profiles"
                    mode="$1"
                    ;;
                --with-tools)
                    with_tools=true
                    ;;
                *)
                    cc_die "config: unsupported argument '$1'"
                    ;;
            esac
            shift
        done

        [[ "$with_tools" == true ]] && compose_args+=(--profile cc-migrate --profile cc-admin)
        compose_args+=(config)

        case "$mode" in
            --quiet)    compose_args+=(--quiet) ;;
            --json)     compose_args+=(--format json --no-env-resolution) ;;
            --services) compose_args+=(--services) ;;
            --profiles) compose_args+=(--profiles) ;;
            "")         compose_args+=(--no-env-resolution) ;;
        esac
        ;;

    preflight)
        [[ $# -eq 0 ]] || cc_die "preflight takes no arguments; the window is in $CC_WINDOW_FILE_NAME"
        gate_identity=true
        gate_api_role=true
        gate_queue=true
        ;;

    up)
        [[ $# -eq 0 ]] || cc_die "up takes no arguments; it starts smm-api and smm-web only"
        needs_images=true
        gate_identity=true
        gate_api_role=true
        compose_args=(up --detach --wait --wait-timeout 180 --no-build --pull never smm-api smm-web)
        ;;

    down)
        compose_args=(down)

        while [[ $# -gt 0 ]]; do
            case "$1" in
                --remove-orphans)
                    in_list --remove-orphans "${compose_args[@]}" && cc_die "down: --remove-orphans given twice"
                    compose_args+=(--remove-orphans)
                    ;;
                *)
                    cc_die "down: unsupported argument '$1' (volumes and images are never removed here)"
                    ;;
            esac
            shift
        done
        ;;

    ps)
        compose_args=(ps)
        services=()

        while [[ $# -gt 0 ]]; do
            case "$1" in
                -a|--all)   compose_args+=(--all) ;;
                -q|--quiet) compose_args+=(--quiet) ;;
                --services) compose_args+=(--services) ;;
                -*)         cc_die "ps: unsupported option '$1'" ;;
                *)
                    in_list "$1" $ALL_SERVICES || cc_die "ps: unknown service '$1'"
                    services+=("$1")
                    ;;
            esac
            shift
        done

        compose_args+=("${services[@]}")
        ;;

    logs)
        compose_args=(logs --no-color)
        services=()

        while [[ $# -gt 0 ]]; do
            case "$1" in
                --since)
                    [[ $# -ge 2 && "$2" =~ ^[0-9]{1,5}[smh]$ ]] \
                        || cc_die "logs: --since takes a duration such as 15m"
                    compose_args+=(--since "$2")
                    shift
                    ;;
                --tail)
                    [[ $# -ge 2 && "$2" =~ ^[0-9]{1,6}$ ]] \
                        || cc_die "logs: --tail takes a number of lines"
                    compose_args+=(--tail "$2")
                    shift
                    ;;
                --timestamps)
                    compose_args+=(--timestamps)
                    ;;
                -*)
                    cc_die "logs: unsupported option '$1'"
                    ;;
                *)
                    in_list "$1" $HTTP_SERVICES || cc_die "logs: unknown service '$1'"
                    services+=("$1")
                    ;;
            esac
            shift
        done

        compose_args+=("${services[@]}")
        ;;

    restart)
        [[ $# -gt 0 ]] || cc_die "restart: name smm-api and/or smm-web"
        needs_images=true
        gate_identity=true
        gate_api_role=true

        for service in "$@"; do
            in_list "$service" $HTTP_SERVICES || cc_die "restart: only smm-api or smm-web, not '$service'"
        done

        compose_args=(restart "$@")
        ;;

    migrate)
        [[ $# -ge 1 ]] || cc_die "migrate: current | heads | upgrade REVISION | downgrade REVISION"
        needs_images=true
        sub="$1"
        shift

        case "$sub" in
            current|heads)
                [[ $# -eq 0 ]] || cc_die "migrate $sub takes no arguments"
                alembic_args=("$sub")
                ;;
            upgrade|downgrade)
                # An explicit revision id only: no head, base, +N, -N,
                # branch@rev, ranges or --sql.
                [[ $# -eq 1 && "$1" =~ ^[0-9a-f]{12}$ ]] \
                    || cc_die "migrate $sub takes exactly one 12-hex revision id"
                alembic_args=("$sub" "$1")
                gate_identity=true
                gate_queue=true
                # No switch turns these off: an upgrade always needs the
                # evidence, a downgrade always voids it.
                if [[ "$sub" == upgrade ]]; then
                    gate_backup=true
                else
                    invalidate_evidence=true
                fi
                ;;
            *)
                cc_die "migrate: unsupported subcommand '$sub'"
                ;;
        esac

        compose_args=(--profile cc-migrate run --rm --no-deps -T migrate "${alembic_args[@]}")
        ;;

    create-owner)
        needs_images=true
        email="" display_name="" project=""

        while [[ $# -gt 0 ]]; do
            [[ $# -ge 2 ]] || cc_die "create-owner: $1 needs a value"

            case "$1" in
                --email)
                    [[ -z "$email" ]] || cc_die "create-owner: --email given twice"
                    [[ "$2" =~ ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]] \
                        || cc_die "create-owner: not an e-mail address: '$2'"
                    email="$2"
                    ;;
                --display-name)
                    [[ -z "$display_name" ]] || cc_die "create-owner: --display-name given twice"
                    [[ "$2" =~ ^[^[:cntrl:]]{1,255}$ ]] \
                        || cc_die "create-owner: --display-name must be 1-255 printable characters"
                    display_name="$2"
                    ;;
                --project)
                    [[ -z "$project" ]] || cc_die "create-owner: --project given twice"
                    [[ "$2" =~ ^[a-z0-9][a-z0-9_-]{0,119}$ ]] \
                        || cc_die "create-owner: not a project id: '$2'"
                    project="$2"
                    ;;
                *)
                    cc_die "create-owner: unsupported argument '$1'"
                    ;;
            esac
            shift 2
        done

        [[ -n "$email" && -n "$display_name" && -n "$project" ]] \
            || cc_die "create-owner: --email, --display-name and --project are all required"

        gate_identity=true
        compose_args=(--profile cc-admin run --rm --no-deps)
        # getpass needs a terminal; without one it reads stdin.
        [[ -t 0 && -t 1 ]] || compose_args+=(-T)
        # --opt=value: a value can never be read as another option.
        compose_args+=(cli create-owner
            "--email=$email" "--display-name=$display_name" "--project=$project")
        ;;

    *)
        cc_die "unsupported verb '$verb' (run without arguments for the list)"
        ;;
esac

# -- the deployment directory ------------------------------------------------

cc_load_dir "$here"

compose_file="$CC_DIR/$CC_COMPOSE_FILE_NAME"
interp_file="$CC_DIR/$CC_INTERP_FILE_NAME"
secret_file="$CC_DIR/$CC_SECRET_FILE_NAME"
api_secret_file="$CC_DIR/$CC_API_SECRET_FILE_NAME"

# Every verb: Compose cannot load the project without smm-api's env file.
for required in "$compose_file" "$interp_file" "$secret_file" "$api_secret_file"; do
    cc_require_file "$required"
done

cc_load_compose_env
cc_load_secret
cc_load_api_secret

version="$CC_VERSION"
network="$CC_NETWORK"

cc_load_target

# -- the daemon and the images -----------------------------------------------

if [[ "$needs_daemon" == true ]]; then
    cc_verify_engine
fi

if [[ "$needs_images" == true ]]; then
    arch="$(cc_docker version --format '{{.Server.Arch}}')" \
        || cc_die "cannot read the daemon's architecture"

    for image in "ai-smm-api:$version" "ai-smm-web:$version"; do
        facts="$(cc_docker image inspect --format \
            "{{.Architecture}} {{index .Config.Labels \"$CC_REVISION_LABEL\"}}" "$image" 2>/dev/null)" \
            || cc_die "image $image is not loaded on this daemon"

        image_arch="${facts%% *}"
        revision="${facts#* }"

        # Equality with the full commit: a matching prefix is not enough.
        [[ "$revision" == "$version" ]] \
            || cc_die "image $image carries $CC_REVISION_LABEL='$revision', expected exactly $version"
        [[ "$image_arch" == "$arch" ]] \
            || cc_die "image $image is $image_arch, the daemon is $arch"
    done
fi

# -- the gates (cc-gate.sh) ----------------------------------------------------
#
# Last, immediately before Compose: the API and the one-shot tools only
# ever reach the recorded database, a schema change only a quiet queue,
# an upgrade only a database with a verified, off-host-copied dump. Any
# failure exits 2 here, and nothing below runs.

if [[ "$gate_identity" == true ]]; then
    cc_gate_identity "$network"
fi

if [[ "$gate_api_role" == true ]]; then
    cc_gate_api_role
fi

if [[ "$gate_queue" == true ]]; then
    cc_gate_queue
fi

if [[ "$gate_backup" == true ]]; then
    cc_gate_backup_evidence
fi

if [[ "$invalidate_evidence" == true ]]; then
    cc_invalidate_evidence
fi

if [[ "$verb" == preflight ]]; then
    echo "preflight: PASS"
    exit 0
fi

# -- run ------------------------------------------------------------------------

exec env -i PATH="$PATH" HOME="${HOME:-/root}" DOCKER_HOST="$CC_DOCKER_HOST" \
    ${TERM:+"TERM=$TERM"} \
    docker compose \
        --project-name "$CC_PROJECT" \
        --project-directory "$CC_DIR" \
        --file "$compose_file" \
        --env-file "$interp_file" \
        "${compose_args[@]}"
