#!/usr/bin/env bash
# Pre-migration backup of the ai_smm database, and proof that it restores.
#
#   cc-db-backup.sh dump   --container <postgres-container> --out-dir /root/backups/ai-smm
#   cc-db-backup.sh verify --container <postgres-container> --dump <file> [--expect-revision REV]
#
# Runs against the daemon pinned in $SMM_CC_DIR/control-center.target (see
# cc-common.sh). Every failure stops the script with a non-zero exit and
# says what was and was not left behind. What it guarantees:
#
#   * the live database `ai_smm` is only ever READ: pg_dump, and read-only
#     queries (default_transaction_read_only=on). CREATE, DROP and
#     pg_restore only ever name a scratch database whose name matches
#     ^ai_smm_restore_check_[0-9]{14}$, checked before each statement;
#   * dump: written to <name>.partial, removed on any failure; renamed to
#     its final name only once pg_dump succeeded, the file is non-empty,
#     starts with the custom-format magic, and its table of contents lists
#     the queue; then a .sha256 is written. An existing file is never
#     overwritten;
#   * verify: refuses a dump whose checksum does not match, an unreadable
#     archive, and any leftover ai_smm_restore_check_* database (a scratch
#     database it did not create is never dropped). Restores into a fresh
#     scratch database in one transaction with --exit-on-error, drops it on
#     success, failure or interruption; if that drop fails, exits 3 and
#     names the database to drop by hand.

set -euo pipefail

CC_SELF=cc-db-backup
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/control-center/cc-common.sh
source "$here/cc-common.sh"

readonly SOURCE_DB=ai_smm
readonly SCRATCH_PREFIX=ai_smm_restore_check_
readonly SCRATCH_PATTERN='^ai_smm_restore_check_[0-9]{14}$'
readonly READ_ONLY='-c default_transaction_read_only=on'

usage() {
    cat <<'EOF'
usage: cc-db-backup.sh dump   --container NAME --out-dir DIR
       cc-db-backup.sh verify --container NAME --dump FILE [--expect-revision REV]
EOF
}

[[ $# -gt 0 ]] || { usage >&2; exit 2; }
command="$1"
shift

container="" out_dir="" dump="" expect_revision=""

while [[ $# -gt 0 ]]; do
    [[ $# -ge 2 ]] || cc_die "$1 needs a value"

    case "$command:$1" in
        dump:--container|verify:--container) container="$2" ;;
        dump:--out-dir)                      out_dir="$2" ;;
        verify:--dump)                       dump="$2" ;;
        verify:--expect-revision)            expect_revision="$2" ;;
        *)                                   usage >&2; cc_die "unsupported argument '$1' for '$command'" ;;
    esac
    shift 2
done

[[ "$container" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || cc_die "--container: not a container name: '$container'"
[[ -z "$expect_revision" || "$expect_revision" =~ ^[0-9a-f]{12}$ ]] \
    || cc_die "--expect-revision takes a 12-hex alembic id"

cc_load_dir "$here"
cc_load_target
cc_verify_engine

[[ "$(cc_docker inspect --format '{{.State.Running}}' "$container" 2>/dev/null)" == true ]] \
    || cc_die "container $container is not running on this daemon"

pg_user="$(cc_docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$container" \
    | sed -n 's/^POSTGRES_USER=//p')"
[[ -n "$pg_user" ]] || cc_die "cannot read POSTGRES_USER of $container"

# Read-only query; prints rows unaligned, no header.
pg_read() {
    cc_docker exec -i -e PGOPTIONS="$READ_ONLY" "$container" \
        psql -X -v ON_ERROR_STOP=1 -U "$pg_user" -d "$1" -tA -c "$2"
}

# CREATE / DROP of the scratch database only.
pg_scratch_admin() {
    local verb="$1" name="$2"

    [[ "$name" =~ $SCRATCH_PATTERN && "$name" != "$SOURCE_DB" ]] || return 99

    case "$verb" in
        create) cc_docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 -U "$pg_user" -d postgres \
                    -c "CREATE DATABASE \"$name\"" ;;
        drop)   cc_docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 -U "$pg_user" -d postgres \
                    -c "DROP DATABASE IF EXISTS \"$name\" WITH (FORCE)" ;;
        *)      return 99 ;;
    esac
}

# The archive's table of contents must list the queue.
check_toc() {
    local file="$1" toc

    [[ "$(head -c 5 "$file")" == PGDMP ]] || return 1
    toc="$(cc_docker exec -i "$container" pg_restore --list < "$file")" || return 1

    grep -q "TABLE DATA public alembic_version " <<<"$toc" \
        && grep -q "TABLE DATA public publications " <<<"$toc"
}

[[ "$(pg_read postgres "SELECT 1 FROM pg_database WHERE datname = '$SOURCE_DB'")" == 1 ]] \
    || cc_die "database $SOURCE_DB does not exist in $container"

# -- dump ------------------------------------------------------------------------

do_dump() {
    [[ -n "$out_dir" ]] || cc_die "dump: --out-dir is required"
    [[ -d "$out_dir" && ! -L "$out_dir" ]] || cc_die "dump: $out_dir is not a directory"
    out_dir="$(cd "$out_dir" && pwd -P)"

    local dir_mode size free stamp final partial
    dir_mode="$(stat -c '%a' "$out_dir")"
    (( (8#$dir_mode & 8#077) == 0 )) \
        || cc_die "dump: $out_dir is mode $dir_mode; backups hold credential hashes, use a 700 directory"

    size="$(pg_read "$SOURCE_DB" "SELECT pg_database_size(current_database())")"
    free="$(df --output=avail -B1 "$out_dir" | tail -n 1 | tr -d ' ')"
    (( free > 2 * size )) || cc_die "dump: $free bytes free in $out_dir, need more than twice the database size ($size)"

    stamp="$(date -u +%Y%m%dT%H%M%SZ)"
    final="$out_dir/ai_smm-pre-control-center-$stamp.dump"
    partial="$final.partial"
    [[ ! -e "$final" && ! -e "$partial" && ! -e "$final.sha256" ]] || cc_die "dump: $final already exists"

    umask 077
    trap 'rm -f "$partial"' EXIT
    trap 'exit 130' INT TERM

    if ! cc_docker exec "$container" pg_dump -U "$pg_user" -d "$SOURCE_DB" -Fc > "$partial"; then
        cc_die "dump: pg_dump failed; the partial file was removed. The database was only read."
    fi

    [[ -s "$partial" ]] || cc_die "dump: pg_dump produced an empty file; removed"
    check_toc "$partial" \
        || cc_die "dump: the archive is unreadable or does not list alembic_version and publications; removed"

    mv "$partial" "$final"
    (cd "$out_dir" && sha256sum "$(basename "$final")" > "$(basename "$final").sha256")
    chmod 600 "$final" "$final.sha256"
    trap - EXIT INT TERM

    echo "dump:   $final"
    echo "sha256: $(cut -d' ' -f1 "$final.sha256")"
}

# -- verify --------------------------------------------------------------------

scratch=""
created=false

drop_scratch() {
    local left

    pg_scratch_admin drop "$scratch" >/dev/null || return 1
    left="$(pg_read postgres "SELECT count(*) FROM pg_database WHERE datname = '$scratch'")" || return 1
    [[ "$left" == 0 ]]
}

on_verify_exit() {
    local status=$?

    if [[ "$created" == true ]]; then
        created=false

        if ! drop_scratch; then
            echo "$CC_SELF: SCRATCH DATABASE $scratch WAS NOT DROPPED." >&2
            echo "$CC_SELF: $SOURCE_DB was not touched. Drop it by hand after checking the name:" >&2
            echo "$CC_SELF:   DROP DATABASE \"$scratch\" WITH (FORCE);" >&2
            exit 3
        fi

        echo "scratch database $scratch dropped" >&2
    fi

    exit "$status"
}

do_verify() {
    [[ -n "$dump" ]] || cc_die "verify: --dump is required"
    [[ -f "$dump" && ! -L "$dump" ]] || cc_die "verify: $dump is not a regular file"
    [[ -f "$dump.sha256" ]] || cc_die "verify: $dump.sha256 is missing; cannot tell a corrupted archive from a good one"

    (cd "$(dirname "$dump")" && sha256sum --check --status "$(basename "$dump").sha256") \
        || cc_die "verify: checksum mismatch -- $dump is corrupted or not the recorded dump; do not use it"
    check_toc "$dump" || cc_die "verify: $dump is not a readable custom-format archive of the queue"

    local leftover revision counts
    leftover="$(pg_read postgres "SELECT string_agg(datname, ' ') FROM pg_database WHERE datname LIKE '${SCRATCH_PREFIX}%'")"
    [[ -z "$leftover" ]] \
        || cc_die "verify: scratch database(s) already exist: $leftover -- left by an earlier check? Review and drop them by hand; this script drops only what it created"

    scratch="$SCRATCH_PREFIX$(date -u +%Y%m%d%H%M%S)"
    [[ "$scratch" =~ $SCRATCH_PATTERN ]] || cc_die "internal: bad scratch name $scratch"

    trap on_verify_exit EXIT
    trap 'exit 130' INT TERM

    pg_scratch_admin create "$scratch" >/dev/null || cc_die "verify: cannot create $scratch"
    created=true

    cc_docker exec -i "$container" pg_restore -U "$pg_user" -d "$scratch" \
            --no-owner --no-privileges --exit-on-error --single-transaction < "$dump" \
        || cc_die "verify: pg_restore failed; the archive does not restore"

    revision="$(pg_read "$scratch" "SELECT version_num FROM alembic_version")"
    counts="$(pg_read "$scratch" "SELECT status || ' ' || count(*) FROM publications GROUP BY status ORDER BY status")"

    echo "restored revision: $revision"
    echo "publications by status:"
    sed 's/^/  /' <<<"$counts"

    [[ -z "$expect_revision" || "$revision" == "$expect_revision" ]] \
        || cc_die "verify: restored revision $revision, expected $expect_revision"

    created=false
    drop_scratch || { created=true; exit 3; }
    trap - EXIT INT TERM

    echo "restore check passed; scratch database $scratch dropped"
}

case "$command" in
    dump)   do_dump ;;
    verify) do_verify ;;
    *)      usage >&2; cc_die "unsupported command '$command'" ;;
esac
