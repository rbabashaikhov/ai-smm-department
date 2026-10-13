# The deployment gates: PostgreSQL identity and publication-queue safety.
# Sourced after cc-common.sh by cc-compose.sh and cc-db-backup.sh, never
# executed.
#
# FAIL CLOSED. Every value is read, parsed and compared; a value that
# cannot be read, does not parse, is not unique or does not match stops
# the calling script with exit 2 before it dumps, restores, migrates or
# writes. The gates themselves only read: `docker inspect`, `docker
# network inspect`, and psql with default_transaction_read_only=on.
#
# Expected values are NEVER constants here. They come from two
# host-specific files next to the compose file (not in Git):
#
#   control-center.identity   what was CONFIRMED about the database before
#                             the deployment, copied from the deployment
#                             record. Never filled from the live host in
#                             the same session: a value read from the
#                             thing being checked proves nothing.
#     SMM_CC_PG_CONTAINER=<container name = the host in both database URLs>
#     SMM_CC_PG_CONTAINER_ID=<full 64-hex id of that container>
#     SMM_CC_PG_SYSTEM_IDENTIFIER=<SELECT system_identifier FROM pg_control_system()>
#     SMM_CC_PG_DATABASE_OID=<SELECT oid FROM pg_database WHERE datname = 'ai_smm'>
#     SMM_CC_WORKER_CONTAINER=<the live worker's container name>
#
#   control-center.window     the agreed migration window, in UTC:
#     SMM_CC_WINDOW_START=YYYY-MM-DDTHH:MM:SSZ
#     SMM_CC_WINDOW_END=YYYY-MM-DDTHH:MM:SSZ
#
# Identity: the worker, the backup (`docker exec` into the container) and
# the migrate/cli/API containers (the database URL, resolved by Docker DNS
# on the shared network) must all reach ONE PostgreSQL instance, the
# recorded one:
#   * control-center.env and the running worker hold the same database URL,
#     whose host is SMM_CC_PG_CONTAINER and whose database is ai_smm;
#   * the worker runs and is attached to the shared network;
#   * on that network exactly one container answers to that host name, and
#     it is SMM_CC_PG_CONTAINER_ID -- as is the container of that name;
#   * inside it: system identifier, database oid and port as recorded.
#
# API role (SMM-024C-2), before anything starts smm-api: its URL, in
# control-center.api.env, names the API's own role ai_smm_cc on the same
# host, port and database as the owner URL -- never the owner; and inside
# the recorded instance, ai_smm_cc.verify.sql (next to the compose file)
# reports no FAIL: the role has exactly the designed privileges, a SCRAM
# password, no membership and owns nothing.
#
# Queue: nothing may be publishing, hold a lease, or become due -- first
# attempt or retry -- from CC_QUEUE_MARGIN_MINUTES before the window
# starts until CC_QUEUE_MARGIN_MINUTES after it ends; nothing may have
# been published or attempted in the margin before it; no attempt may be
# unfinished; no publication may wait for review. All times are the
# database's now(), in UTC.

readonly CC_IDENTITY_FILE_NAME=control-center.identity
readonly CC_WINDOW_FILE_NAME=control-center.window
# The application database: the name the schema and the scripts are
# written for, not a property of any host.
readonly CC_APP_DB=ai_smm
readonly CC_QUEUE_MARGIN_MINUTES=60
readonly CC_WINDOW_MAX_HOURS=12
readonly CC_READ_ONLY='-c default_transaction_read_only=on'
readonly CC_API_VERIFY_FILE_NAME=ai_smm_cc.verify.sql
# The verify file checks every column of every table; far fewer lines means
# it is not the file from Git, or the schema is not the one it was run on.
readonly CC_API_VERIFY_MIN_CHECKS=200
# The two files the API role gate relies on, pinned to the release these
# scripts belong to (tests/test_control_center_packaging.py keeps the pins
# equal to the files in Git). A verify file that was edited or swapped
# could print a passing answer; a compose file from another release -- the
# 2026-10-11 one gives smm-api control-center.env -- would start the API
# on the owner URL whatever control-center.api.env says.
readonly CC_PINNED_COMPOSE_SHA256=f894a9be7e00e020b677adaf1d16af26ad63fa9d5b0086e2663f05bee5c82ac6
readonly CC_PINNED_VERIFY_SHA256=8e5561c1255ba09951f457a7be371d21828749f8b36f20930c071f7b541a10cc

readonly CC_NAME_RE='^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$'
readonly CC_UTC_RE='^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$'

cc_gate_die() {
    cc_die "GATE CLOSED: $*"
}

# One line, no embedded newline: a second row is an ambiguous answer.
cc_single_line() {
    [[ -n "$1" && "$1" != *$'\n'* ]]
}

# Sets CC_PG_CONTAINER, CC_PG_CONTAINER_ID, CC_PG_SYSTEM_ID, CC_PG_DB_OID,
# CC_WORKER_CONTAINER.
cc_load_identity() {
    local file="$CC_DIR/$CC_IDENTITY_FILE_NAME"

    cc_require_file "$file"
    cc_check_keys "$file" SMM_CC_PG_CONTAINER SMM_CC_PG_CONTAINER_ID \
        SMM_CC_PG_SYSTEM_IDENTIFIER SMM_CC_PG_DATABASE_OID SMM_CC_WORKER_CONTAINER

    CC_PG_CONTAINER="$(cc_value_of "$file" SMM_CC_PG_CONTAINER)"
    CC_PG_CONTAINER_ID="$(cc_value_of "$file" SMM_CC_PG_CONTAINER_ID)"
    CC_PG_SYSTEM_ID="$(cc_value_of "$file" SMM_CC_PG_SYSTEM_IDENTIFIER)"
    CC_PG_DB_OID="$(cc_value_of "$file" SMM_CC_PG_DATABASE_OID)"
    CC_WORKER_CONTAINER="$(cc_value_of "$file" SMM_CC_WORKER_CONTAINER)"

    [[ "$CC_PG_CONTAINER" =~ $CC_NAME_RE ]] \
        || cc_die "$file: SMM_CC_PG_CONTAINER is not a container name"
    [[ "$CC_PG_CONTAINER_ID" =~ ^[0-9a-f]{64}$ ]] \
        || cc_die "$file: SMM_CC_PG_CONTAINER_ID must be the full 64-hex container id"
    [[ "$CC_PG_SYSTEM_ID" =~ ^[1-9][0-9]{0,19}$ ]] \
        || cc_die "$file: SMM_CC_PG_SYSTEM_IDENTIFIER is not a system identifier"
    [[ "$CC_PG_DB_OID" =~ ^[1-9][0-9]{0,9}$ ]] \
        || cc_die "$file: SMM_CC_PG_DATABASE_OID is not an oid"
    [[ "$CC_WORKER_CONTAINER" =~ $CC_NAME_RE ]] \
        || cc_die "$file: SMM_CC_WORKER_CONTAINER is not a container name"
    [[ "$CC_WORKER_CONTAINER" != "$CC_PG_CONTAINER" ]] \
        || cc_die "$file: the worker and the database cannot be the same container"
}

# Sets CC_WINDOW_START, CC_WINDOW_END.
cc_load_window() {
    local file="$CC_DIR/$CC_WINDOW_FILE_NAME"

    cc_require_file "$file"
    cc_check_keys "$file" SMM_CC_WINDOW_START SMM_CC_WINDOW_END

    CC_WINDOW_START="$(cc_value_of "$file" SMM_CC_WINDOW_START)"
    CC_WINDOW_END="$(cc_value_of "$file" SMM_CC_WINDOW_END)"

    [[ "$CC_WINDOW_START" =~ $CC_UTC_RE && "$CC_WINDOW_END" =~ $CC_UTC_RE ]] \
        || cc_die "$file: the window must be UTC, YYYY-MM-DDTHH:MM:SSZ"
    # Same fixed-width format: the string order is the time order.
    [[ "$CC_WINDOW_START" < "$CC_WINDOW_END" ]] \
        || cc_die "$file: SMM_CC_WINDOW_END must be after SMM_CC_WINDOW_START"
}

# Sets CC_DSN_HOST, CC_DSN_PORT, CC_DSN_DB from the URL in $1. Never
# prints the URL. Refuses anything it cannot read in exactly one way: a
# second '@', a query string (libpq parameters such as host= or service=
# would override the host), a missing database.
cc_parse_dsn() {
    local re='^postgresql(\+[a-z0-9]+)?://[^:@/?#]+(:[^@/?#]*)?@([A-Za-z0-9][A-Za-z0-9_.-]*)(:([0-9]{1,5}))?/([A-Za-z0-9_]+)$'

    [[ "$1" =~ $re ]] \
        || cc_gate_die "$2: the database URL cannot be read unambiguously (postgresql[+driver]://user:password@host[:port]/database, password URL-encoded, no query string); it is not printed"

    CC_DSN_HOST="${BASH_REMATCH[3]}"
    CC_DSN_PORT="${BASH_REMATCH[5]:-5432}"
    CC_DSN_DB="${BASH_REMATCH[6]}"
}

# "<id> <running>" of a container, validated.
cc_container_state() {
    local out

    out="$(cc_docker inspect --format '{{.Id}} {{.State.Running}}' "$1" 2>/dev/null)" \
        || cc_gate_die "container $1 not found on this daemon"
    cc_single_line "$out" && [[ "$out" =~ ^[0-9a-f]{64}\ (true|false)$ ]] \
        || cc_gate_die "unexpected answer inspecting container $1"

    echo "$out"
}

# "<id> /<name> @on <dns names...> <aliases...>" when attached to $2,
# "<id> /<name>" otherwise.
cc_container_on_network() {
    local out

    # range, not join: the CLI hands these lists to the template untyped.
    out="$(cc_docker inspect --format \
        "{{.Id}} {{.Name}}{{with index .NetworkSettings.Networks \"$2\"}} @on{{range .DNSNames}} {{.}}{{end}}{{range .Aliases}} {{.}}{{end}}{{end}}" \
        "$1" 2>/dev/null)" \
        || cc_gate_die "cannot read the networks of container $1"
    cc_single_line "$out" && [[ "$out" =~ ^[0-9a-f]{64}\ /[A-Za-z0-9] ]] \
        || cc_gate_die "unexpected answer reading the networks of container $1"

    echo "$out"
}

# Sets CC_PG_USER: the database container's superuser, used over its local
# socket for the read-only queries, pg_dump and the scratch database.
cc_load_pg_user() {
    local env count

    env="$(cc_docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$CC_PG_CONTAINER" 2>/dev/null)" \
        || cc_gate_die "cannot read the environment of $CC_PG_CONTAINER"
    count="$(grep -c '^POSTGRES_USER=' <<<"$env" || true)"
    [[ "$count" == 1 ]] || cc_gate_die "$CC_PG_CONTAINER defines POSTGRES_USER $count times"

    CC_PG_USER="$(sed -n 's/^POSTGRES_USER=//p' <<<"$env")"
    [[ "$CC_PG_USER" =~ ^[A-Za-z_][A-Za-z0-9_]{0,62}$ ]] \
        || cc_gate_die "POSTGRES_USER of $CC_PG_CONTAINER is not a role name"
}

# Read-only query in the recorded database container; rows unaligned.
cc_pg_read() {
    cc_docker exec -e PGOPTIONS="$CC_READ_ONLY" "$CC_PG_CONTAINER" \
        psql -X -v ON_ERROR_STOP=1 -U "$CC_PG_USER" -d "$1" -tA -c "$2" </dev/null
}

# Requires CC_DSN (cc_load_secret), CC_DOCKER_HOST and a verified engine.
cc_gate_identity() {
    local network="$1" state worker_env count worker_dsn line names matches=() id cid
    local answer re sysid oid port db

    cc_load_identity
    cc_parse_dsn "$CC_DSN" "$CC_SECRET_FILE_NAME"

    [[ "${CC_DSN_HOST,,}" == "${CC_PG_CONTAINER,,}" ]] \
        || cc_gate_die "$CC_SECRET_FILE_NAME points at host '$CC_DSN_HOST', the recorded database container is '$CC_PG_CONTAINER'"
    [[ "$CC_DSN_DB" == "$CC_APP_DB" ]] \
        || cc_gate_die "$CC_SECRET_FILE_NAME names database '$CC_DSN_DB', not $CC_APP_DB"

    # The worker: running, on the shared network, same database URL.
    state="$(cc_container_state "$CC_WORKER_CONTAINER")"
    [[ "${state#* }" == true ]] || cc_gate_die "the worker $CC_WORKER_CONTAINER is not running"

    worker_env="$(cc_docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$CC_WORKER_CONTAINER" 2>/dev/null)" \
        || cc_gate_die "cannot read the environment of $CC_WORKER_CONTAINER"
    count="$(grep -c '^AI_SMM_DATABASE_URL=' <<<"$worker_env" || true)"
    [[ "$count" == 1 ]] \
        || cc_gate_die "the worker $CC_WORKER_CONTAINER defines AI_SMM_DATABASE_URL $count times"
    worker_dsn="$(sed -n 's/^AI_SMM_DATABASE_URL=//p' <<<"$worker_env")"
    [[ "$worker_dsn" == "$CC_DSN" ]] \
        || cc_gate_die "the database URL in $CC_SECRET_FILE_NAME differs from the running worker's (neither is printed)"

    line="$(cc_container_on_network "$CC_WORKER_CONTAINER" "$network")"
    [[ "$line" == *" @on"* ]] \
        || cc_gate_die "the worker $CC_WORKER_CONTAINER is not attached to $network"

    # The container of that name is the recorded one, and it runs.
    state="$(cc_container_state "$CC_PG_CONTAINER")"
    [[ "${state% *}" == "$CC_PG_CONTAINER_ID" ]] \
        || cc_gate_die "container $CC_PG_CONTAINER is ${state:0:12}, not the recorded ${CC_PG_CONTAINER_ID:0:12}"
    [[ "${state#* }" == true ]] || cc_gate_die "the database container $CC_PG_CONTAINER is not running"

    # Exactly one container on the shared network answers to the host
    # name -- what the worker, migrate, cli and the API resolve.
    answer="$(cc_docker network inspect --format '{{range $id, $c := .Containers}}{{println $id}}{{end}}' "$network" 2>/dev/null)" \
        || cc_gate_die "cannot list the containers on $network"

    for id in $answer; do
        [[ "$id" =~ ^[0-9a-f]{64}$ ]] || cc_gate_die "unexpected container id on $network"
        line="$(cc_container_on_network "$id" "$network")"
        cid="${line%% *}"
        [[ "$cid" == "$id" && "$line" == *" @on"* ]] \
            || cc_gate_die "container $id is listed on $network but does not report it"

        names="${line#* }"
        names=" ${names#/} "
        names="${names/ @on / }"
        [[ "${names,,}" == *" ${CC_DSN_HOST,,} "* ]] && matches+=("$id")
    done

    (( ${#matches[@]} == 1 )) \
        || cc_gate_die "'$CC_DSN_HOST' resolves to ${#matches[@]} containers on $network; expected exactly one"
    [[ "${matches[0]}" == "$CC_PG_CONTAINER_ID" ]] \
        || cc_gate_die "'$CC_DSN_HOST' on $network is container ${matches[0]:0:12}, not the recorded ${CC_PG_CONTAINER_ID:0:12}"

    # Inside: the recorded instance and database.
    cc_load_pg_user
    answer="$(cc_pg_read "$CC_APP_DB" \
        "SELECT 'identity|' || s.system_identifier || '|' || d.oid || '|' || current_setting('port') || '|' || current_database() FROM pg_control_system() s, pg_database d WHERE d.datname = current_database()" \
        2>/dev/null)" \
        || cc_gate_die "cannot query $CC_APP_DB in $CC_PG_CONTAINER (database unreachable, or the query failed)"

    re='^identity\|([0-9]+)\|([0-9]+)\|([0-9]+)\|([A-Za-z0-9_]+)$'
    cc_single_line "$answer" && [[ "$answer" =~ $re ]] \
        || cc_gate_die "ambiguous identity answer from $CC_PG_CONTAINER"
    sysid="${BASH_REMATCH[1]}" oid="${BASH_REMATCH[2]}" port="${BASH_REMATCH[3]}" db="${BASH_REMATCH[4]}"

    [[ "$db" == "$CC_APP_DB" ]] || cc_gate_die "connected to '$db', not $CC_APP_DB"
    [[ "$sysid" == "$CC_PG_SYSTEM_ID" ]] \
        || cc_gate_die "PostgreSQL system identifier $sysid is not the recorded $CC_PG_SYSTEM_ID: another instance"
    [[ "$oid" == "$CC_PG_DB_OID" ]] \
        || cc_gate_die "$CC_APP_DB has oid $oid, not the recorded $CC_PG_DB_OID: another database"
    [[ "$port" == "$CC_DSN_PORT" ]] \
        || cc_gate_die "the instance listens on $port, the database URL uses $CC_DSN_PORT"

    echo "gate identity: PASS -- $CC_APP_DB (oid $oid) in $CC_PG_CONTAINER (${CC_PG_CONTAINER_ID:0:12}, system $sysid); worker $CC_WORKER_CONTAINER and $CC_SECRET_FILE_NAME use the same URL; '$CC_DSN_HOST' unique on $network"
}

# $1 is a regular file whose sha256 is exactly $2.
cc_require_pinned() {
    local actual

    cc_require_file "$1"
    actual="$(sha256sum "$1" | cut -d' ' -f1)"
    [[ "$actual" == "$2" ]] \
        || cc_gate_die "${1##*/} is not the file this release ships (sha256 ${actual:0:12}, expected ${2:0:12}); copy it unchanged from the same commit as the scripts"
}

# The user name of a database URL, or nothing. Never prints the URL.
cc_dsn_user() {
    [[ "$1" =~ ^postgresql(\+[a-z0-9]+)?://([^:@/?#]+)(:[^@/?#]*)?@ ]] && echo "${BASH_REMATCH[2]}"
}

# Requires cc_gate_identity to have passed in this process (CC_DSN_*,
# CC_PG_CONTAINER, CC_PG_USER), and CC_API_DSN (cc_load_api_secret).
cc_gate_api_role() {
    local owner_host="$CC_DSN_HOST" owner_port="$CC_DSN_PORT" owner_db="$CC_DSN_DB"
    local owner_user api_user verify answer line last="" count=0 fails=() info=()
    local re='^(PASS|FAIL|INFO)\|[a-z0-9_.]+\|[^|]*$' required

    cc_require_pinned "$CC_DIR/$CC_COMPOSE_FILE_NAME" "$CC_PINNED_COMPOSE_SHA256"
    cc_require_pinned "$CC_DIR/$CC_API_VERIFY_FILE_NAME" "$CC_PINNED_VERIFY_SHA256"

    owner_user="$(cc_dsn_user "$CC_DSN")" \
        || cc_gate_die "$CC_SECRET_FILE_NAME: no user name in the database URL; it is not printed"
    cc_parse_dsn "$CC_API_DSN" "$CC_API_SECRET_FILE_NAME"
    api_user="$(cc_dsn_user "$CC_API_DSN")" \
        || cc_gate_die "$CC_API_SECRET_FILE_NAME: no user name in the database URL; it is not printed"

    [[ "$CC_API_DSN" =~ ^postgresql(\+[a-z0-9]+)?://[^:@/?#]+:[^@/?#]+@ ]] \
        || cc_gate_die "$CC_API_SECRET_FILE_NAME: the database URL carries no password; it is not printed"
    [[ "$api_user" == "$CC_API_ROLE" ]] \
        || cc_gate_die "$CC_API_SECRET_FILE_NAME connects as '$api_user', not the API role $CC_API_ROLE"
    [[ "$owner_user" != "$CC_API_ROLE" ]] \
        || cc_gate_die "$CC_SECRET_FILE_NAME connects as $CC_API_ROLE; it must keep the owner role (migrate, cli)"
    [[ "$CC_API_DSN" != "$CC_DSN" ]] \
        || cc_gate_die "$CC_API_SECRET_FILE_NAME and $CC_SECRET_FILE_NAME hold the same URL (neither is printed)"
    [[ "${CC_DSN_HOST,,}" == "${owner_host,,}" && "$CC_DSN_PORT" == "$owner_port" && "$CC_DSN_DB" == "$owner_db" ]] \
        || cc_gate_die "$CC_API_SECRET_FILE_NAME points at $CC_DSN_HOST:$CC_DSN_PORT/$CC_DSN_DB, not the recorded $owner_host:$owner_port/$owner_db"

    # Restore the owner's values for anything that runs after this gate.
    cc_parse_dsn "$CC_DSN" "$CC_SECRET_FILE_NAME"

    verify="$CC_DIR/$CC_API_VERIFY_FILE_NAME"
    cc_require_file "$verify"

    # Read-only, as the instance's superuser over the local socket (the
    # file reads pg_authid): the API's password is never handled here.
    answer="$(cc_pg_read "$CC_APP_DB" "$(cat "$verify")" 2>/dev/null)" \
        || cc_gate_die "cannot run $CC_API_VERIFY_FILE_NAME in $CC_APP_DB (database unreachable, or the query failed)"

    while IFS= read -r line; do
        [[ -z "$last" ]] || cc_gate_die "$CC_API_VERIFY_FILE_NAME: output continues after the END line"

        if [[ "$line" =~ ^END\|checks\|([0-9]+)$ ]]; then
            last="${BASH_REMATCH[1]}"
            continue
        fi

        [[ "$line" =~ $re ]] || cc_gate_die "$CC_API_VERIFY_FILE_NAME: unexpected output line"
        count=$((count + 1))

        case "$line" in
            FAIL\|*) fails+=("${line#FAIL|}") ;;
            INFO\|*) info+=("${line#INFO|}") ;;
        esac
    done <<<"$answer"

    [[ -n "$last" && "$last" == "$count" ]] \
        || cc_gate_die "$CC_API_VERIFY_FILE_NAME: incomplete output ($count checks, END reports '${last:-none}')"
    (( count >= CC_API_VERIFY_MIN_CHECKS )) \
        || cc_gate_die "$CC_API_VERIFY_FILE_NAME ran only $count checks; expected at least $CC_API_VERIFY_MIN_CHECKS"
    (( ${#fails[@]} == 0 )) \
        || cc_gate_die "$CC_API_ROLE is not as designed: ${fails[*]}"

    for required in \
        'PASS|role.exists|' 'PASS|role.attributes|' 'PASS|role.password|set, SCRAM-SHA-256' \
        'PASS|role.member_of|none' 'PASS|role.owns_nothing|0 owned objects' \
        'PASS|schema.public.create|' 'PASS|table.publications.update|denied'; do
        # At the start of a line: the check ran, and passed.
        [[ $'\n'"$answer" == *$'\n'"$required"* ]] \
            || cc_gate_die "$CC_API_VERIFY_FILE_NAME did not report '$required'"
    done

    for line in "${info[@]}"; do
        echo "gate api-role: INFO $line"
    done

    echo "gate api-role: PASS -- smm-api connects as $CC_API_ROLE to $owner_host:$owner_port/$owner_db; $count checks, no FAIL"
}

# Requires cc_gate_identity to have passed in this process.
cc_gate_queue() {
    local answer re names value i blocked=() lo hi rows
    local -a counts

    cc_load_window

    lo="timestamptz '$CC_WINDOW_START' - interval '$CC_QUEUE_MARGIN_MINUTES minutes'"
    hi="timestamptz '$CC_WINDOW_END' + interval '$CC_QUEUE_MARGIN_MINUTES minutes'"

    # due: when the worker could next claim a scheduled row (first attempt
    # or retry); an overdue row counts, it would be claimed at once.
    answer="$(cc_pg_read "$CC_APP_DB" "SELECT concat_ws('|', 'queue',
        (now() >= timestamptz '$CC_WINDOW_START' AND now() <= timestamptz '$CC_WINDOW_END')::int,
        (timestamptz '$CC_WINDOW_END' - timestamptz '$CC_WINDOW_START' <= interval '$CC_WINDOW_MAX_HOURS hours')::int,
        (SELECT count(*) FROM publications WHERE status IN ('claimed', 'publishing')),
        (SELECT count(*) FROM publications WHERE lease_expires_at > $lo),
        (SELECT count(*) FROM publications WHERE status = 'scheduled' AND scheduled_at IS NOT NULL
            AND greatest(scheduled_at, coalesce(next_attempt_at, scheduled_at)) < $hi),
        (SELECT count(*) FROM publications WHERE status = 'scheduled' AND next_attempt_at < $hi),
        (SELECT count(*) FROM publications WHERE published_at >= $lo),
        (SELECT count(*) FROM publication_attempts WHERE finished_at IS NULL OR started_at >= $lo),
        (SELECT count(*) FROM publications WHERE status = 'needs_review'),
        to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"'))" 2>/dev/null)" \
        || cc_gate_die "cannot read the publication queue in $CC_APP_DB (database unreachable, or the query failed)"

    re='^queue\|([01])\|([01])\|([0-9]+)\|([0-9]+)\|([0-9]+)\|([0-9]+)\|([0-9]+)\|([0-9]+)\|([0-9]+)\|([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z)$'
    cc_single_line "$answer" && [[ "$answer" =~ $re ]] \
        || cc_gate_die "ambiguous answer from the queue check"

    [[ "${BASH_REMATCH[1]}" == 1 ]] \
        || cc_gate_die "now (${BASH_REMATCH[10]}) is outside the agreed window $CC_WINDOW_START .. $CC_WINDOW_END"
    [[ "${BASH_REMATCH[2]}" == 1 ]] \
        || cc_gate_die "the window $CC_WINDOW_START .. $CC_WINDOW_END is longer than $CC_WINDOW_MAX_HOURS hours"

    names=(publishing active-lease due-in-window retry-in-window published-in-margin attempts-active-or-in-margin needs-review)
    counts=("${BASH_REMATCH[@]:3:7}")

    for i in "${!names[@]}"; do
        value="${counts[$i]}"
        (( value == 0 )) || blocked+=("${names[$i]}=$value")
    done

    if (( ${#blocked[@]} > 0 )); then
        # Which rows: ids, states and times only, never a body.
        rows="$(cc_pg_read "$CC_APP_DB" "SELECT id, status, scheduled_at, next_attempt_at, lease_expires_at, published_at
            FROM publications
            WHERE status IN ('claimed', 'publishing', 'needs_review')
               OR lease_expires_at > $lo OR published_at >= $lo
               OR (status = 'scheduled' AND greatest(scheduled_at, coalesce(next_attempt_at, scheduled_at)) < $hi)
            ORDER BY id" 2>/dev/null)" || rows="(could not list the rows)"
        echo "$rows" | sed 's/^/  publication /' >&2
        cc_gate_die "publication queue not quiet for $CC_WINDOW_START .. $CC_WINDOW_END +/- ${CC_QUEUE_MARGIN_MINUTES}m: ${blocked[*]}"
    fi

    echo "gate queue: PASS -- at ${BASH_REMATCH[10]} nothing publishing, leased, due or retried in $CC_WINDOW_START .. $CC_WINDOW_END +/- ${CC_QUEUE_MARGIN_MINUTES}m; nothing published or attempted in the margin before; none needs review"
}

# -- backup evidence -------------------------------------------------------------
#
# A schema upgrade needs proof that THIS database, at THIS revision, was
# dumped, that the dump restores, and that a copy left the host:
#
#   <dump>.source                  written by `cc-db-backup.sh dump`: the
#                                  dump's sha256, the recorded instance and
#                                  database, the live alembic revision, the
#                                  time it was taken
#   control-center.backup-evidence written ONLY by a successful
#                                  `cc-db-backup.sh verify` (checksum, test
#                                  restore, source record = recorded identity
#                                  = restored revision = live revision); every
#                                  verify run deletes the previous one first.
#                                  Its last line is the sha256 of the lines
#                                  above it.
#   control-center.offhost-ack     written BY THE OPERATOR after copying the
#                                  dump off the host and checking it there:
#                                  the sha256 computed on the receiving
#                                  machine. An attestation, not something
#                                  this host can verify.
#
# `migrate upgrade` refuses unless all three agree with each other, with the
# recorded identity, with the live revision and with the agreed window, and
# the dump file still hashes to the recorded sha256. `migrate downgrade`
# needs none of it, and invalidates the evidence (cc_invalidate_evidence):
# after the schema moved back, an earlier dump is not a backup of the
# database as it now is.

readonly CC_EVIDENCE_FILE_NAME=control-center.backup-evidence
readonly CC_OFFHOST_FILE_NAME=control-center.offhost-ack
readonly CC_SHA_RE='^[0-9a-f]{64}$'
readonly CC_REVISION_RE='^[0-9a-f]{12}$'
readonly CC_PATH_RE='^/[A-Za-z0-9._/-]+$'
readonly CC_EVIDENCE_KEYS=(
    SMM_CC_EVIDENCE_DUMP
    SMM_CC_EVIDENCE_DUMP_SHA256
    SMM_CC_EVIDENCE_PG_CONTAINER_ID
    SMM_CC_EVIDENCE_SYSTEM_IDENTIFIER
    SMM_CC_EVIDENCE_DATABASE_OID
    SMM_CC_EVIDENCE_REVISION
    SMM_CC_EVIDENCE_TAKEN_AT
    SMM_CC_EVIDENCE_VERIFIED_AT
    SMM_CC_EVIDENCE_CHECKSUM
)
readonly CC_SOURCE_KEYS=(
    SMM_CC_SOURCE_DUMP
    SMM_CC_SOURCE_SHA256
    SMM_CC_SOURCE_PG_CONTAINER_ID
    SMM_CC_SOURCE_SYSTEM_IDENTIFIER
    SMM_CC_SOURCE_DATABASE_OID
    SMM_CC_SOURCE_REVISION
    SMM_CC_SOURCE_TAKEN_AT
)
readonly CC_OFFHOST_KEYS=(
    SMM_CC_OFFHOST_DUMP_SHA256
    SMM_CC_OFFHOST_CONFIRMED_AT
    SMM_CC_OFFHOST_CONFIRMED_BY
)

# The live alembic revision of ai_smm: exactly one 12-hex row.
cc_live_revision() {
    local answer

    answer="$(cc_pg_read "$CC_APP_DB" "SELECT version_num FROM alembic_version" 2>/dev/null)" \
        || cc_gate_die "cannot read the alembic revision of $CC_APP_DB"
    cc_single_line "$answer" && [[ "$answer" =~ $CC_REVISION_RE ]] \
        || cc_gate_die "ambiguous alembic revision in $CC_APP_DB"

    echo "$answer"
}

cc_sha256_of() {
    local out

    out="$(sha256sum "$1" 2>/dev/null)" || return 1
    echo "${out%% *}"
}

# Requires cc_gate_identity and cc_gate_queue (the window) in this process.
cc_gate_backup_evidence() {
    local file="$CC_DIR/$CC_EVIDENCE_FILE_NAME" ack="$CC_DIR/$CC_OFFHOST_FILE_NAME"
    local dump sha cid sysid oid revision taken verified recorded live actual
    local ack_sha ack_at ack_by

    [[ -e "$file" || -L "$file" ]] \
        || cc_gate_die "no backup evidence ($CC_EVIDENCE_FILE_NAME): run cc-db-backup.sh dump and verify in this window first"
    cc_require_file "$file"
    cc_check_keys "$file" "${CC_EVIDENCE_KEYS[@]}"

    # Self-checksum: a truncated, hand-edited or mixed-up file is refused.
    [[ "$(wc -l < "$file")" == "${#CC_EVIDENCE_KEYS[@]}" \
        && "$(tail -n 1 "$file")" == SMM_CC_EVIDENCE_CHECKSUM=* ]] \
        || cc_gate_die "backup evidence is corrupted (unexpected layout)"
    recorded="$(cc_value_of "$file" SMM_CC_EVIDENCE_CHECKSUM)"
    [[ "$(head -n -1 "$file" | sha256sum | cut -d' ' -f1)" == "$recorded" ]] \
        || cc_gate_die "backup evidence is corrupted or was edited (its checksum does not match)"

    dump="$(cc_value_of "$file" SMM_CC_EVIDENCE_DUMP)"
    sha="$(cc_value_of "$file" SMM_CC_EVIDENCE_DUMP_SHA256)"
    cid="$(cc_value_of "$file" SMM_CC_EVIDENCE_PG_CONTAINER_ID)"
    sysid="$(cc_value_of "$file" SMM_CC_EVIDENCE_SYSTEM_IDENTIFIER)"
    oid="$(cc_value_of "$file" SMM_CC_EVIDENCE_DATABASE_OID)"
    revision="$(cc_value_of "$file" SMM_CC_EVIDENCE_REVISION)"
    taken="$(cc_value_of "$file" SMM_CC_EVIDENCE_TAKEN_AT)"
    verified="$(cc_value_of "$file" SMM_CC_EVIDENCE_VERIFIED_AT)"

    [[ "$dump" =~ $CC_PATH_RE && "$dump" != *..* && "$sha" =~ $CC_SHA_RE \
        && "$revision" =~ $CC_REVISION_RE && "$taken" =~ $CC_UTC_RE && "$verified" =~ $CC_UTC_RE ]] \
        || cc_gate_die "backup evidence is malformed"

    # The recorded instance and database -- which the identity gate has
    # just matched against the live ones.
    [[ "$cid" == "$CC_PG_CONTAINER_ID" && "$sysid" == "$CC_PG_SYSTEM_ID" && "$oid" == "$CC_PG_DB_OID" ]] \
        || cc_gate_die "backup evidence belongs to another PostgreSQL instance, container or database"

    # The schema has not moved since the dump.
    live="$(cc_live_revision)"
    [[ "$revision" == "$live" ]] \
        || cc_gate_die "backup evidence is for revision $revision, the database is at $live: stale, take a new dump"

    # Taken and verified within this window, in that order.
    [[ ! "$taken" < "$CC_WINDOW_START" && ! "$verified" < "$taken" && ! "$CC_WINDOW_END" < "$verified" ]] \
        || cc_gate_die "backup evidence ($taken, verified $verified) is not from the window $CC_WINDOW_START .. $CC_WINDOW_END: stale"

    # The dump is still there and still the same bytes.
    [[ -f "$dump" && ! -L "$dump" ]] \
        || cc_gate_die "the dump named by the backup evidence is missing or not a regular file: $dump"
    actual="$(cc_sha256_of "$dump")" || cc_gate_die "cannot read the dump $dump"
    [[ "$actual" == "$sha" ]] \
        || cc_gate_die "the dump $dump no longer matches the backup evidence (sha256 differs)"
    [[ "$(cut -d' ' -f1 "$dump.sha256" 2>/dev/null)" == "$sha" ]] \
        || cc_gate_die "$dump.sha256 does not match the backup evidence"

    # The off-host copy: what the operator attests, checked for consistency.
    [[ -e "$ack" || -L "$ack" ]] \
        || cc_gate_die "no off-host copy confirmation ($CC_OFFHOST_FILE_NAME): copy the dump off the host, check it there, then record it"
    cc_require_file "$ack"
    cc_check_keys "$ack" "${CC_OFFHOST_KEYS[@]}"
    ack_sha="$(cc_value_of "$ack" SMM_CC_OFFHOST_DUMP_SHA256)"
    ack_at="$(cc_value_of "$ack" SMM_CC_OFFHOST_CONFIRMED_AT)"
    ack_by="$(cc_value_of "$ack" SMM_CC_OFFHOST_CONFIRMED_BY)"

    [[ "$ack_sha" =~ $CC_SHA_RE && "$ack_at" =~ $CC_UTC_RE && "$ack_by" =~ ^[A-Za-z0-9][A-Za-z0-9._@\ -]{0,63}$ ]] \
        || cc_gate_die "$CC_OFFHOST_FILE_NAME is malformed"
    [[ "$ack_sha" == "$sha" ]] \
        || cc_gate_die "the off-host copy confirmation is for another dump (sha256 differs)"
    [[ ! "$ack_at" < "$verified" && ! "$CC_WINDOW_END" < "$ack_at" ]] \
        || cc_gate_die "the off-host copy was confirmed at $ack_at, before the restore check ($verified) or after the window"

    echo "gate backup: PASS -- dump $dump (sha256 ${sha:0:12}) taken $taken at revision $revision, restore-checked $verified; off-host copy attested by $ack_by at $ack_at (operator attestation, not verifiable from this host)"
}

# `migrate downgrade`: evidence never survives a schema change backwards.
cc_invalidate_evidence() {
    local file="$CC_DIR/$CC_EVIDENCE_FILE_NAME"

    if [[ -e "$file" || -L "$file" ]]; then
        mv -f "$file" "$file.invalidated-$(date -u +%Y%m%dT%H%M%SZ)" \
            || cc_die "cannot invalidate $file before the downgrade"
        echo "backup evidence invalidated: a downgrade changes the schema it was taken at"
    fi
}
