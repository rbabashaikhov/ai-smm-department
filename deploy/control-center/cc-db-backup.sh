#!/usr/bin/env bash
# Pre-migration backup of the ai_smm database, and proof that it restores.
#
#   cc-db-backup.sh dump   --container <postgres-container> --out-dir <dir outside any rotation>
#   cc-db-backup.sh verify --container <postgres-container> --dump <file> [--expect-revision REV]
#
# Runs against the daemon pinned in $SMM_CC_DIR/control-center.target (see
# cc-common.sh). Every failure stops the script with a non-zero exit and
# says what was and was not left behind. What it guarantees:
#
#   * the database is the recorded one (cc-gate.sh, identity gate) before
#     anything is read or created: --container must be the recorded
#     container, and the worker, control-center.env and Docker DNS must
#     all lead to it. `dump` also passes the queue gate (nothing
#     publishing, leased or due around control-center.window) immediately
#     before pg_dump. A closed gate exits 2 and nothing was run;
#   * the dump is kept out of automatic rotation: its name does not match
#     the nightly backup's ai_smm-*.dump, and a directory holding such
#     files (one that rotation prunes) is refused;
#   * backup evidence (cc-gate.sh): `dump` writes <dump>.source -- its
#     sha256, the recorded instance and database, the live alembic
#     revision, the time. `verify` first DELETES any earlier
#     control-center.backup-evidence, and writes a new one only when the
#     checksum, the test restore and the source record all hold and the
#     source revision is still the live one. `migrate upgrade` refuses to
#     run without it;
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
# shellcheck source=deploy/control-center/cc-gate.sh
source "$here/cc-gate.sh"

readonly SOURCE_DB="$CC_APP_DB"
readonly SCRATCH_PREFIX=ai_smm_restore_check_
readonly SCRATCH_PATTERN='^ai_smm_restore_check_[0-9]{14}$'
# Not ai_smm-*.dump: the nightly backup's rotation deletes those after 14
# days (docs/operations.md), and this one must outlive it.
readonly DUMP_PREFIX=cc-premigration-ai_smm-

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

# Whatever happens next, an earlier verify's evidence no longer stands:
# only the outcome of THIS run may authorise an upgrade.
if [[ "$command" == verify ]]; then
    rm -f "$CC_DIR/$CC_EVIDENCE_FILE_NAME"
fi

cc_load_target
cc_verify_engine
cc_load_compose_env
cc_load_secret

# The identity gate, before anything touches the instance. Every docker
# exec below goes to the recorded container, as its POSTGRES_USER.
cc_load_identity
[[ "$container" == "$CC_PG_CONTAINER" ]] \
    || cc_gate_die "--container $container is not the recorded database container $CC_PG_CONTAINER ($CC_IDENTITY_FILE_NAME)"
cc_gate_identity "$CC_NETWORK"
pg_user="$CC_PG_USER"

# Read-only query; prints rows unaligned, no header.
pg_read() {
    cc_pg_read "$1" "$2"
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

    # A directory the nightly rotation prunes would delete this dump too.
    if compgen -G "$out_dir/ai_smm-*.dump" >/dev/null; then
        cc_die "dump: $out_dir holds ai_smm-*.dump files, so an automatic rotation prunes it; use a directory of its own"
    fi

    size="$(pg_read "$SOURCE_DB" "SELECT pg_database_size(current_database())")"
    [[ "$size" =~ ^[0-9]+$ ]] || cc_die "dump: cannot read the size of $SOURCE_DB"
    free="$(df --output=avail -B1 "$out_dir" | tail -n 1 | tr -d ' ')"
    (( free > 2 * size )) || cc_die "dump: $free bytes free in $out_dir, need more than twice the database size ($size)"

    # The queue gate, immediately before pg_dump: closed -> exit 2, nothing written.
    cc_gate_queue

    # The schema the dump is taken at, for its source record.
    local revision epoch taken
    revision="$(cc_live_revision)"

    epoch="$(date -u +%s)"
    stamp="$(date -u -d "@$epoch" +%Y%m%dT%H%M%SZ)"
    taken="$(date -u -d "@$epoch" +%Y-%m-%dT%H:%M:%SZ)"
    final="$out_dir/$DUMP_PREFIX$stamp.dump"
    partial="$final.partial"
    [[ ! -e "$final" && ! -e "$partial" && ! -e "$final.sha256" && ! -e "$final.source" ]] \
        || cc_die "dump: $final already exists"

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
    printf '%s\n' \
        "SMM_CC_SOURCE_DUMP=$(basename "$final")" \
        "SMM_CC_SOURCE_SHA256=$(cut -d' ' -f1 "$final.sha256")" \
        "SMM_CC_SOURCE_PG_CONTAINER_ID=$CC_PG_CONTAINER_ID" \
        "SMM_CC_SOURCE_SYSTEM_IDENTIFIER=$CC_PG_SYSTEM_ID" \
        "SMM_CC_SOURCE_DATABASE_OID=$CC_PG_DB_OID" \
        "SMM_CC_SOURCE_REVISION=$revision" \
        "SMM_CC_SOURCE_TAKEN_AT=$taken" > "$final.source"
    chmod 600 "$final" "$final.sha256" "$final.source"
    trap - EXIT INT TERM

    echo "dump:   $final"
    echo "sha256: $(cut -d' ' -f1 "$final.sha256")"
    echo "source: $final.source (revision $revision, taken $taken)"
}

# -- verify --------------------------------------------------------------------

# Sets src_sha, src_taken; dies unless <dump>.source describes exactly this
# archive, taken from the recorded instance and database, at the restored
# revision, and the live database is still at that revision.
check_source() {
    local restored="$1" source="$dump.source" name sha cid sysid oid rev live

    [[ -f "$source" && ! -L "$source" ]] \
        || cc_die "verify: $source is missing; only a dump taken by this script can become backup evidence"
    cc_check_keys "$source" "${CC_SOURCE_KEYS[@]}"

    name="$(cc_value_of "$source" SMM_CC_SOURCE_DUMP)"
    src_sha="$(cc_value_of "$source" SMM_CC_SOURCE_SHA256)"
    cid="$(cc_value_of "$source" SMM_CC_SOURCE_PG_CONTAINER_ID)"
    sysid="$(cc_value_of "$source" SMM_CC_SOURCE_SYSTEM_IDENTIFIER)"
    oid="$(cc_value_of "$source" SMM_CC_SOURCE_DATABASE_OID)"
    rev="$(cc_value_of "$source" SMM_CC_SOURCE_REVISION)"
    src_taken="$(cc_value_of "$source" SMM_CC_SOURCE_TAKEN_AT)"

    sha="$(cc_sha256_of "$dump")" || cc_die "verify: cannot read $dump"
    [[ "$name" == "$(basename "$dump")" && "$src_sha" == "$sha" ]] \
        || cc_die "verify: $source describes another archive"
    [[ "$src_taken" =~ $CC_UTC_RE && "$rev" =~ $CC_REVISION_RE ]] \
        || cc_die "verify: $source is malformed"
    [[ "$cid" == "$CC_PG_CONTAINER_ID" && "$sysid" == "$CC_PG_SYSTEM_ID" && "$oid" == "$CC_PG_DB_OID" ]] \
        || cc_die "verify: the dump was taken from another instance, container or database than the recorded one"
    [[ "$rev" == "$restored" ]] \
        || cc_die "verify: the archive restores at $restored, its source record says $rev"

    live="$(cc_live_revision)"
    [[ "$live" == "$rev" ]] \
        || cc_die "verify: $SOURCE_DB is now at $live, the dump was taken at $rev: stale, take a new dump"
}

# Written last, atomically, only after a passed restore check.
write_evidence() {
    local file="$CC_DIR/$CC_EVIDENCE_FILE_NAME" path body

    path="$(cd "$(dirname "$dump")" && pwd -P)/$(basename "$dump")"
    [[ "$path" =~ $CC_PATH_RE && "$path" != *..* ]] \
        || cc_die "verify: the dump path cannot be recorded as evidence: $path"

    body="$(printf '%s\n' \
        "SMM_CC_EVIDENCE_DUMP=$path" \
        "SMM_CC_EVIDENCE_DUMP_SHA256=$src_sha" \
        "SMM_CC_EVIDENCE_PG_CONTAINER_ID=$CC_PG_CONTAINER_ID" \
        "SMM_CC_EVIDENCE_SYSTEM_IDENTIFIER=$CC_PG_SYSTEM_ID" \
        "SMM_CC_EVIDENCE_DATABASE_OID=$CC_PG_DB_OID" \
        "SMM_CC_EVIDENCE_REVISION=$1" \
        "SMM_CC_EVIDENCE_TAKEN_AT=$src_taken" \
        "SMM_CC_EVIDENCE_VERIFIED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)")"

    ( umask 077
      { printf '%s\n' "$body"
        printf 'SMM_CC_EVIDENCE_CHECKSUM=%s\n' "$(printf '%s\n' "$body" | sha256sum | cut -d' ' -f1)"
      } > "$file.partial" ) || cc_die "verify: cannot write $file"
    mv -f "$file.partial" "$file"

    echo "evidence: $file (dump sha256 ${src_sha:0:12}, revision $1)"
}

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
    [[ "$revision" =~ $CC_REVISION_RE ]] || cc_die "verify: cannot read the restored revision"

    local src_sha src_taken
    check_source "$revision"

    created=false
    drop_scratch || { created=true; exit 3; }
    trap - EXIT INT TERM

    echo "restore check passed; scratch database $scratch dropped"
    write_evidence "$revision"
}

case "$command" in
    dump)   do_dump ;;
    verify) do_verify ;;
    *)      usage >&2; cc_die "unsupported command '$command'" ;;
esac
