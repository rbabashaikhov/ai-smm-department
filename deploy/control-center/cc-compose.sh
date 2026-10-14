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
#   control-center.auth.env       exactly AI_SMM_DATABASE_URL, mode 600: the
#                                 sign-in service's role ai_smm_auth (smm-auth
#                                 only, SMM-025D)
#   ai_smm_cc.verify.sql          deploy/control-center/db/, copied unchanged;
#                                 run by the API role gate (cc-gate.sh)
#   ai_smm_auth.verify.sql        deploy/control-center/db/, copied unchanged;
#                                 run by the auth role gate (cc-gate.sh)
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
# designed privileges, and smm-auth's is ai_smm_auth with exactly its own; for a schema change, the publication queue is quiet
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

readonly HTTP_SERVICES="smm-api smm-auth smm-web"
readonly ALL_SERVICES="smm-api smm-auth smm-web migrate cli"

usage() {
    cat <<'EOF'
usage: cc-compose.sh <verb> [options]

  config [--quiet | --json | --services | --profiles] [--with-tools]
                                  render the configuration; never prints the
                                  database URL (env files are not resolved)
  up                              start smm-api, smm-auth and smm-web, wait
                                  until healthy; runs the identity, API role
                                  and auth role gates first
  down [--remove-orphans]         stop and remove this project's containers
                                  and networks (never volumes or images)
  ps [-a] [-q] [--services] [SERVICE...]
  logs [--since N{s|m|h}] [--tail N] [--timestamps] [smm-api|smm-auth|smm-web...]
  restart smm-api|smm-auth|smm-web...
                                  runs the identity, API role and auth role
                                  gates first
  preflight                       the PostgreSQL identity, API role, auth role
                                  and publication-queue gates only, read-only;
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
  mutations status                read-only or business-mutation mode, and what
                                  the running smm-api actually has
  mutations enable --approved-by NAME
                                  business-mutation mode for THIS release:
                                  checks the mutation role (ai_smm_cc_rw), then
                                  recreates smm-api on it with mutations on.
                                  Refused outright by a release that does not
                                  permit mutations (CC_RELEASE_PERMITS_MUTATIONS)
  mutations disable               back to read-only, always allowed: drops the
                                  mode first, then recreates smm-api on
                                  ai_smm_cc -- or stops it if that role fails
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
gate_auth_role=false
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
        gate_auth_role=true
        gate_queue=true
        ;;

    up)
        [[ $# -eq 0 ]] || cc_die "up takes no arguments; it starts smm-api, smm-auth and smm-web only"
        needs_images=true
        gate_identity=true
        gate_api_role=true
        gate_auth_role=true
        compose_args=(up --detach --wait --wait-timeout 180 --no-build --pull never smm-api smm-auth smm-web)
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
        [[ $# -gt 0 ]] || cc_die "restart: name smm-api, smm-auth and/or smm-web"
        needs_images=true
        gate_identity=true
        gate_api_role=true
        gate_auth_role=true

        for service in "$@"; do
            in_list "$service" $HTTP_SERVICES || cc_die "restart: only smm-api, smm-auth or smm-web, not '$service'"
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

    mutations)
        [[ $# -ge 1 ]] || cc_die "mutations: status | enable --approved-by NAME | disable"
        mutations_sub="$1"
        shift

        case "$mutations_sub" in
            status)
                [[ $# -eq 0 ]] || cc_die "mutations status takes no arguments"
                ;;
            enable)
                [[ $# -eq 2 && "$1" == --approved-by ]] \
                    || cc_die "mutations enable takes exactly --approved-by NAME"
                [[ "$2" =~ ^[A-Za-z0-9][A-Za-z0-9_.@\ -]{0,127}$ ]] \
                    || cc_die "mutations enable: --approved-by must be a plain name (letters, digits, . _ @ - space)"
                approved_by="$2"
                # Before anything is read or run: this release may not.
                [[ "$CC_RELEASE_PERMITS_MUTATIONS" == yes ]] \
                    || cc_die "mutations enable: this release does not permit business mutations (open: $CC_MUTATIONS_BLOCKED_BY); only a reviewed release can change that. Nothing was changed"
                needs_images=true
                gate_identity=true
                gate_api_role=true
                compose_args=(up --detach --wait --wait-timeout 180 --no-build --pull never --no-deps smm-api)
                ;;
            disable)
                [[ $# -eq 0 ]] || cc_die "mutations disable takes no arguments"
                needs_images=true
                compose_args=(up --detach --wait --wait-timeout 180 --no-build --pull never --no-deps smm-api)
                ;;
            *)
                cc_die "mutations: unsupported subcommand '$mutations_sub'"
                ;;
        esac
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
auth_secret_file="$CC_DIR/$CC_AUTH_SECRET_FILE_NAME"

# Every verb: Compose cannot load the project without smm-api's and
# smm-auth's env files.
for required in "$compose_file" "$interp_file" "$secret_file" "$api_secret_file" "$auth_secret_file"; do
    cc_require_file "$required"
done

cc_load_compose_env
cc_load_secret
cc_load_api_secret
cc_load_auth_secret

version="$CC_VERSION"
network="$CC_NETWORK"

# -- read-only or business-mutation mode ------------------------------------
#
# Mutation mode exists only while control-center.mutations (written by
# `mutations enable`, nothing else) names THIS release. Then, and only
# then, every verb adds the pinned override and the API role gate checks
# the mutation role. A state file for another release -- or any state
# file, in a release that does not permit mutations -- is refused by every
# verb that could start the API, so a new release always starts read-only.

mutations_state="$CC_DIR/$CC_MUTATIONS_STATE_FILE_NAME"
api_mode=read-only
stale_mutations=false
stale_reason=""

if [[ "$verb" == mutations && "${mutations_sub:-}" == disable ]]; then
    # First of all: whatever happens next, nothing can bring the mode back.
    if [[ -e "$mutations_state" || -L "$mutations_state" ]]; then
        rm -f -- "$mutations_state"
        echo "mutations: mode file removed; the Control Center is read-only from here on"
    fi
elif [[ "$verb" == mutations && "${mutations_sub:-}" == enable ]]; then
    api_mode=mutation
elif [[ -e "$mutations_state" || -L "$mutations_state" ]]; then
    cc_load_mutations_state

    if [[ "$CC_RELEASE_PERMITS_MUTATIONS" != yes ]]; then
        stale_mutations=true
        stale_reason="this release does not permit business mutations (open: $CC_MUTATIONS_BLOCKED_BY); the mode file was not written by it: run 'mutations disable'"
    elif [[ "$CC_MUTATIONS_VERSION" == "$version" ]]; then
        api_mode=mutation
    else
        stale_mutations=true
        stale_reason="mutation mode was enabled for release ${CC_MUTATIONS_VERSION:0:12}, this is ${version:0:12}; a new release starts read-only: run 'mutations disable' (and 'mutations enable' again once approved for this release)"
    fi

    if [[ "$stale_mutations" == true ]]; then
        case "$verb" in
            down|ps|logs|config|mutations) ;;
            *) cc_die "$stale_reason" ;;
        esac
    fi
fi

compose_files=(--file "$compose_file")

if [[ "$api_mode" == mutation ]]; then
    cc_require_file "$CC_DIR/$CC_MUTATIONS_COMPOSE_FILE_NAME"
    cc_load_rw_secret
    compose_files+=(--file "$CC_DIR/$CC_MUTATIONS_COMPOSE_FILE_NAME")
fi

export CC_API_MODE="$api_mode"

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

if [[ "$verb" == mutations && "$mutations_sub" == status ]]; then
    if [[ "$api_mode" == mutation ]]; then
        echo "mode: business-mutation (release ${version:0:12}, enabled $CC_MUTATIONS_ENABLED_AT, approved by $CC_MUTATIONS_APPROVED_BY)"
    elif [[ "$stale_mutations" == true ]]; then
        echo "mode: read-only -- a mode file for release ${CC_MUTATIONS_VERSION:0:12} is present and ignored: $stale_reason"
    else
        echo "mode: read-only"
    fi

    if [[ "$CC_RELEASE_PERMITS_MUTATIONS" != yes ]]; then
        echo "release: does not permit business mutations (open: $CC_MUTATIONS_BLOCKED_BY)"
    fi

    running_env="$(cc_docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$CC_PROJECT-smm-api-1" 2>/dev/null)" || running_env=""

    if [[ -z "$running_env" ]]; then
        echo "smm-api: not running"
    else
        running_flag="$(sed -n 's/^AI_SMM_API_MUTATIONS_ENABLED=//p' <<<"$running_env" | tail -n 1)"
        running_user="$(cc_dsn_user "$(sed -n 's/^AI_SMM_DATABASE_URL=//p' <<<"$running_env" | tail -n 1)")" || running_user=""
        echo "smm-api: AI_SMM_API_MUTATIONS_ENABLED=${running_flag:-<unset>} role=${running_user:-<unknown>}"
    fi

    exit 0
fi

# Stops smm-api, with the read-only project files: the fail-closed end of
# `mutations enable` and `disable` when the API cannot be trusted to start.
cc_stop_api() {
    env -i PATH="$PATH" HOME="${HOME:-/root}" DOCKER_HOST="$CC_DOCKER_HOST" \
        docker compose --project-name "$CC_PROJECT" --project-directory "$CC_DIR" \
            --file "$compose_file" --env-file "$interp_file" stop smm-api
}

# -- the gates (cc-gate.sh) ----------------------------------------------------
#
# Last, immediately before Compose: the API and the one-shot tools only
# ever reach the recorded database, a schema change only a quiet queue,
# an upgrade only a database with a verified, off-host-copied dump. Any
# failure exits 2 here, and nothing below runs.

if [[ "$verb" == mutations && "$mutations_sub" == disable ]]; then
    # The read-only role must check out before smm-api is started on it.
    # If it does not, the safe state is no API at all.
    if ! ( cc_gate_identity "$network" && cc_gate_api_role ); then
        echo "mutations disable: the read-only gates failed; stopping smm-api instead of starting it" >&2
        cc_stop_api || cc_die "mutations disable: could not stop smm-api; stop it by hand"
        cc_die "mutations disable: smm-api is stopped and the Control Center is read-only; fix the read-only role, then run 'up'"
    fi
fi

if [[ "$gate_identity" == true ]]; then
    cc_gate_identity "$network"
fi

if [[ "$gate_api_role" == true ]]; then
    cc_gate_api_role
fi

if [[ "$gate_auth_role" == true ]]; then
    cc_gate_auth_role
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

if [[ "$verb" == mutations && "$mutations_sub" == enable ]]; then
    # The mode file first, written whole or not at all: from here on every
    # verb includes the override, so a later `restart` cannot fall back to
    # a half-configured state. Undone below if the API does not come up.
    tmp_state="$(mktemp "$CC_DIR/.mutations.XXXXXX")"
    {
        echo "SMM_CC_MUTATIONS=on"
        echo "SMM_CC_MUTATIONS_VERSION=$version"
        echo "SMM_CC_MUTATIONS_ENABLED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        echo "SMM_CC_MUTATIONS_APPROVED_BY=$approved_by"
    } > "$tmp_state"
    chmod 644 "$tmp_state"
    mv -f -- "$tmp_state" "$mutations_state"

    if ! env -i PATH="$PATH" HOME="${HOME:-/root}" DOCKER_HOST="$CC_DOCKER_HOST" \
        ${TERM:+"TERM=$TERM"} \
        docker compose \
            --project-name "$CC_PROJECT" \
            --project-directory "$CC_DIR" \
            "${compose_files[@]}" \
            --env-file "$interp_file" \
            "${compose_args[@]}"; then
        # It may be running half-started in mutation mode: stop it.
        rm -f -- "$mutations_state"
        cc_stop_api || cc_die "mutations enable: smm-api did not come up in mutation mode and could not be stopped; the mode file is removed -- stop smm-api by hand, then run 'mutations disable'"
        cc_die "mutations enable: smm-api did not come up in mutation mode; the mode file is removed and smm-api is stopped -- run 'mutations disable' to start it read-only"
    fi

    # What the running container actually has: the role and the flag.
    running_env="$(cc_docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$CC_PROJECT-smm-api-1" 2>/dev/null)" || running_env=""
    running_flag="$(sed -n 's/^AI_SMM_API_MUTATIONS_ENABLED=//p' <<<"$running_env" | tail -n 1)"
    running_user="$(cc_dsn_user "$(sed -n 's/^AI_SMM_DATABASE_URL=//p' <<<"$running_env" | tail -n 1)")" || running_user=""

    if [[ "$running_flag" != true || "$running_user" != "$CC_RW_ROLE" ]]; then
        rm -f -- "$mutations_state"
        cc_stop_api || cc_die "mutations enable: smm-api runs with flag='${running_flag:-<unset>}' role='${running_user:-<unknown>}' and could not be stopped; the mode file is removed -- stop smm-api by hand, then run 'mutations disable'"
        cc_die "mutations enable: smm-api ran with flag='${running_flag:-<unset>}' role='${running_user:-<unknown>}', not mutations on as $CC_RW_ROLE; the mode file is removed and smm-api is stopped -- run 'mutations disable' to start it read-only"
    fi

    echo "mutations enable: PASS -- smm-api runs as $CC_RW_ROLE with business mutations on (release ${version:0:12}, approved by $approved_by)"
    exit 0
fi

exec env -i PATH="$PATH" HOME="${HOME:-/root}" DOCKER_HOST="$CC_DOCKER_HOST" \
    ${TERM:+"TERM=$TERM"} \
    docker compose \
        --project-name "$CC_PROJECT" \
        --project-directory "$CC_DIR" \
        "${compose_files[@]}" \
        --env-file "$interp_file" \
        "${compose_args[@]}"
