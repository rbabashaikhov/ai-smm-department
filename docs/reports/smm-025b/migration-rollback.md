# SMM-025B — Migration & Rollback Report

**Status: a plan. Nothing here has been executed against production.** Every step that touches the VPS, the production database, the live worker or a production secret needs its own approval from the owner. §3 was rewritten in SMM-025C (2026-10-14) from a read-only inventory of the host; the order is the one the tests validate locally.

## 1. What changes in the database

Migration `a7c2e5f19b40_social_accounts_credentials` (revises `3d4ac1986f07`, the current production head):

| Object | Change | Effect on existing rows |
|---|---|---|
| `social_accounts` | new table | — |
| `social_account_credentials` | new table | — |
| `content_revisions.social_account_id` | new nullable column + FK | none: NULL for all; append-only trigger unaffected (no row is updated) |
| `publications.social_account_id` | new nullable column, composite FK `(project_id, social_account_id)`, index | none: NULL for all = **legacy rows**, published exactly as before (for allow-listed projects) |
| `publication_account_claim_guard()` + `trg_publications_account_claim_guard` | new trigger on `publications` | none for existing rows (the trigger only acts on rows with an account) |

No row is read or rewritten. The migration takes no `lock_timeout` of its own; it runs with the bound the `migrate` service passes through `PGOPTIONS` (proved by `tests/test_migrations.py::test_a_lock_timeout_rolls_back_every_pending_revision`, which now runs through this revision too).

## 2. The transition hazard and how it is closed

The one dangerous state is **an old worker meeting an account-bound row**: it would publish it with the deployment token, i.e. as the wrong account. Three independent barriers:

1. **Data:** in production no account-bound row can exist before business mutations are enabled (API read-only; `ai_smm_cc` has no write grant on any of the new tables).
2. **Database:** the guard trigger refuses `status → claimed` for an account-bound row unless the claiming transaction ran `set_config('ai_smm.account_routing','on', true)`. Only the new `claim_due_publication` does. The pre-SMM-025B claim statement, run verbatim, is refused (`tests/test_migration_social_accounts.py::test_an_old_worker_claim_cannot_take_an_account_row`).
3. **Downgrade guard:** `downgrade()` refuses while any account-bound publication is not `published`/`cancelled` — otherwise dropping the column would turn it into a legacy row. This refusal has no override.
4. **Worker preflight (fail-closed):** the new worker checks its configuration against the queue before it recovers, schedules or claims anything, and refuses to start (exit 78) on any FAIL — §3a.

## 3. Production rollout R0–R7 (SMM-025C, from the VPS as found on 2026-10-14)

Rewritten in SMM-025C Phase 3 from a read-only inventory of the host
(2026-10-14 ~04:20Z; raw output kept off-repo in
`~/.local/share/ai-smm-deploy/audit-SMM-025C-1-20261014/`). It replaces
the earlier sequence, which assumed packaging the host does not have
(§3.2). **Nothing below has been executed.** Groups A–F are proposed for
approval one by one; R7 (mutations) is out of scope until its
prerequisites are closed.

Mapping to the earlier step names: R0 backup → group B; R1 keys →
deferred to §3.6 (D2); R2 migrate → C; R3 grants → D; R4a worker
preflight → F1; R4 worker → F2; R5 Control Center → E; R6 second project
→ deferred (FU-025B-2); R7 mutations → §3.6. Group 0 and A are new: a
read-only re-check and the staging the old plan did not spell out.

### 3.1 The host as found

| Item | Found |
|---|---|
| Worker | `ai-smm-worker`, image `ai-smm:706b383` (built from `6c54669`), **LIVE** (`AI_SMM_DRY_RUN=false`), healthy since 2026-10-09; project `/root/ai-smm`, compose project `ai-smm`, services `worker`, `cli` (profile `cli`), `migrate` (profile `tools`); user 10001, read-only rootfs; mounts knowledge (ro) and `/srv/file-storage/ai-smm/threads` → `/srv/ai-smm/media` (rw) |
| Worker config | `/root/ai-smm/.env`: `AI_SMM_VERSION`, `AI_SMM_NETWORK`, `AI_SMM_KNOWLEDGE_HOST_PATH`, `AI_SMM_MEDIA_HOST_PATH`; `ai-smm.env` (600): no `AI_SMM_LEGACY_THREADS_PROJECTS`, no `AI_SMM_CREDENTIALS_*` |
| Control Center | `/root/ai-smm-control-center`, release `0cbd641` (`smm-api`, `smm-web`), API as `ai_smm_cc`, mutations `"false"`, `127.0.0.1:8088`, `/health/ready` 200; gate pins: compose `f894a9be…` (= this repo's), verify `26a65b4b…` (the verify file sits in `$CC`, not `$CC/db`); staging `/root/ai-smm-control-center-staging/{8ccccda…,0cbd641…}` |
| Database | shared `n8n-compose-postgres-1` (pgvector, PG 16), database `ai_smm`, Alembic `3d4ac1986f07`; `ai_smm_cc` verify 701 PASS / 0 FAIL / 5 INFO; PUBLIC has no CONNECT/TEMP on `ai_smm` |
| Queue | 3 `published`, 0 `scheduled`/`claimed`/`publishing`, no lease; last publication 2026-10-10T06:00:32Z. **No publication is due**, so no window is constrained by the queue |
| Projects | one project `ai-catalog-consultant`, one user (owner) |
| Backups | nightly `/root/ai-smm/backup.sh` 03:30 host time (**00:30 UTC**), 14-day rotation in `/root/backups/ai-smm`, no checksum, no off-host copy; `/root/backups/ai-smm-control-center` holds the 2026-10-11 pre-migration dump with `.sha256`/`.source` |
| Rollback images | `ai-smm:706b383` (and `2652213`, `be3bc75`); `ai-smm-api`/`ai-smm-web` at `0cbd641…` and `8ccccda…` |
| Resources | 2 vCPU; 2.5 GB RAM available; disk 9.5 GB free of 25 GB; images 6.7 GB (2.7 GB reclaimable) |
| Secrets dir | none (`/srv/ai-smm/secrets` does not exist) |

### 3.2 Where the earlier plan does not match the host

| # | Discrepancy | Consequence |
|---|---|---|
| D1 | The worker's `compose.yaml` differs from this repo's: a comment, and `AI_SMM_MEDIA_HOST_PATH` / `AI_SMM_NETWORK` have `:-` defaults on the host where the repo requires them (`:?`). The host's `.env` sets both, so the two render the same service | Do **not** copy the repo's `compose.yaml` in this rollout; only `AI_SMM_VERSION` changes |
| D2 | Neither compose file carries a credential key: the worker has no key mount; `compose.control-center.yml` passes no `AI_SMM_CREDENTIALS_PUBLIC_KEY` and no `AI_SMM_LEGACY_THREADS_PROJECTS`, and the wrapper allows only the URL in `control-center.api.env` | Old R1 (keygen) and the key parts of old R4/R5 are **not executable** as written. Not needed while read-only: no credential exists, the worker's preflight passes without a key (§3a, `accounts.credential_keys`). Packaging both is **FU-025C-2**, before R7 |
| D3 | Without the allow-list in the API, Project Settings shows legacy publishing for `ai-catalog-consultant` as "не используется", and (mutation mode only) content of that project would need an account — fail-closed | Cosmetic while read-only; FU-025C-2 |
| D4 | Old R2 said "migrate with the CC `migrate` service": the running release `0cbd641` does not carry `a7c2e5f19b40` | The CC directory is switched to the new release's files and version (group A) **before** `migrate`; the running containers are not recreated by that |
| D5 | The private key must be readable by the worker's uid 10001 and by nobody else (the loader refuses group/other bits) — the old plan said only "mode 600" | `10001:10001`, mode 600, in a root-owned 700 directory with an `x` ACL for 10001, or a 10001-owned 700 directory (R7) |
| D6 | The window file holds the expired window 2026-10-13T04:30Z–06:00Z | A new window is written in group B |
| D7 | The API's connections show `application_name=ai-smm/worker-1` (FU-024C1-1) | All checks below filter `pg_stat_activity` by `usename` |
| D8 | The nightly backup has no checksum and stays on the host | The pre-migration dump is taken, restore-checked and copied off-host with `cc-db-backup.sh` (group B), as on 2026-10-11 |

### 3.3 The release

One commit `M` for both images, on `main`, reviewed: recommended the
merge of SMM-025C (it carries the dormant mutation tooling and the
SMM-025B code; the CC compose file is byte-identical to the deployed one,
sha `f894a9be…`). `5b4e3d7` would work the same for groups A–F, without
the `mutations` verb. Built locally from `git archive "$M"` (see
control-center-deployment.md §3):

```bash
docker build --target api     --label org.opencontainers.image.revision="$M" -t "ai-smm-api:$M" "$B"
docker build                  --label org.opencontainers.image.revision="$M" -t "ai-smm-web:$M" "$B/web"
docker build --target runtime --label org.opencontainers.image.revision="$M" -t "ai-smm:${M:0:7}" "$B"
docker run --rm --network none --entrypoint /app/.venv/bin/alembic -w /app "ai-smm-api:$M" heads   # a7c2e5f19b40 (head)
docker run --rm --network none --entrypoint /app/.venv/bin/alembic -w /app "ai-smm:${M:0:7}" heads  # a7c2e5f19b40 (head)
```

Verified locally on this branch: the full suite; `scripts/check_worker_schema_compat.sh 6c54669`
(the deployed worker's own suite on the migrated head schema: 232 passed,
1 skipped); `tests/test_preflight.py`; the wrapper suites.

### 3.4 Session variables and the queue check (used by every group)

```bash
# On the host, as root. Nothing here prints a secret.
CC=/root/ai-smm-control-center
WK=/root/ai-smm
M=<40-hex release>; W=${M:0:7}
STG=/root/ai-smm-control-center-staging/$M
PG=n8n-compose-postgres-1
EV=$CC/evidence/smm-025c-$(date -u +%Y%m%dT%H%M%SZ); mkdir -m 700 "$EV"
export DOCKER_HOST=unix:///var/run/docker.sock; unset DOCKER_CONTEXT
PGSU=$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$PG" | sed -n 's/^POSTGRES_USER=//p')
ro() { docker exec -i -e PGOPTIONS='-c default_transaction_read_only=on' "$PG" \
         psql -X -At -v ON_ERROR_STOP=1 -U "$PGSU" -d ai_smm -c "$1" </dev/null; }

# Q -- before and after every group; the output must be identical except `now`.
# The fingerprint covers the columns that exist before AND after the
# migration, so adding social_account_id does not change it.
q() {
  ro "SELECT 'fingerprint', count(*), md5(coalesce(string_agg(concat_ws('|', id, project_id,
        platform, ordinal, status, scheduled_at, published_at, threads_post_id, md5(title),
        md5(body), md5(images::text), md5(items::text), human_reviewed, attempt_count,
        next_attempt_at, claimed_by, lease_expires_at, md5(coalesce(last_error, '')), updated_at),
        E'\n' ORDER BY id), '')) FROM publications"
  ro "SELECT 'status', status, count(*) FROM publications GROUP BY status ORDER BY status"
  ro "SELECT 'pending', count(*) FILTER (WHERE status IN ('scheduled','claimed','publishing')),
        min(scheduled_at) FILTER (WHERE status = 'scheduled'),
        count(*) FILTER (WHERE lease_expires_at > now()) FROM publications"
  ro "SELECT 'attempts', count(*), max(id) FROM publication_attempts"
  ro "SELECT 'revision', version_num FROM alembic_version"
  docker inspect -f 'worker {{.Config.Image}} {{.State.Health.Status}} {{.State.StartedAt}}' ai-smm-worker
}
q | tee "$EV/q-<group>-before.txt"     # ... and q-<group>-after.txt
```

**Stop rule for every group:** if `pending` is not `0` (or a row is due
inside the window plus 30 minutes), if the fingerprint changes, or if
any gate fails — stop, do not continue, run the group's rollback.

### 3.5 Groups for approval

Each group is approved on its own; the result of one is reported before
the next is asked for. None of A–F publishes anything, changes an
existing publication, or enables a business mutation.

**Read-only release.** Groups A–F deploy API, UI and worker in the
read-only business mode only: `ai_smm_cc` gains `SELECT` on the new
tables and no write (`test_the_read_only_role_gains_no_write_over_the_deployed_release`),
no key is needed or mounted (`test_the_read_only_release_needs_no_credential_key`,
`test_the_read_only_rollout_needs_no_private_key`), the compose literal
stays `AI_SMM_API_MUTATIONS_ENABLED: "false"`, and `mutations enable` is
refused by this release (§3.6). No group writes a publication.

**Group 0 — Read-only re-check (no change; needs no approval beyond this plan).**
Q; `./cc-compose.sh ps`; `./cc-compose.sh preflight` (old release: PASS);
`df -h /`; `free -m`; that `/root/ai-smm-control-center-staging/0cbd641…/host/ai_smm_cc.grants.sql` exists with sha256 `b65cda833832daf1…` (= `git show 0cbd641:deploy/control-center/db/ai_smm_cc.grants.sql`; confirmed 2026-10-14 09:40Z)
(the read-only rollback below needs it); that no cron/timer other than
the 00:30 UTC backup falls into the window.

**Group A — Stage and install the release files (no container is recreated).**

```bash
# A1 (laptop): images and files, with a sha256 manifest, into $STG (new dir).
docker save "ai-smm-api:$M" "ai-smm-web:$M" | gzip > "ai-smm-cc-$M.tar.gz"
docker save "ai-smm:$W"                     | gzip > "ai-smm-worker-$W.tar.gz"
#   + compose.control-center.yml compose.control-center.mutations.yml cc-common.sh cc-gate.sh
#     cc-compose.sh cc-db-backup.sh ai_smm_cc.{grants,verify,revoke}.sql acl-snapshot.sql
#     ai_smm_cc_rw.{grants,verify,revoke}.sql ai_smm_cc_rw.acl-snapshot.sql
#     control-center.api-rw.env.example  ->  MANIFEST-$M.sha256
# A2 (host): check, load (adds images; nothing runs them yet).
cd "$STG" && sha256sum -c "MANIFEST-$M.sha256"
docker load < "ai-smm-cc-$M.tar.gz"; docker load < "ai-smm-worker-$W.tar.gz"
docker image inspect -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' \
    "ai-smm-api:$M" "ai-smm-web:$M" "ai-smm:$W"                  # three times $M
# A3 (host): keep the live files, then install. compose.control-center.yml
# is byte-identical (f894a9be...): installed for completeness, sha checked.
mkdir -m 700 "$CC/rollback-0cbd641" && cp -p "$CC"/{cc-*.sh,ai_smm_cc.verify.sql,compose.control-center.yml,control-center.compose.env} "$CC/rollback-0cbd641/"
install -m 644 "$STG"/{cc-common.sh,cc-gate.sh,ai_smm_cc.verify.sql,ai_smm_cc_rw.verify.sql,compose.control-center.mutations.yml} "$CC/"
install -m 755 "$STG"/{cc-compose.sh,cc-db-backup.sh} "$CC/"
sed -i "s/^SMM_CC_VERSION=.*/SMM_CC_VERSION=$M/" "$CC/control-center.compose.env"
cd "$CC" && sha256sum cc-*.sh ai_smm_cc*.verify.sql compose.control-center*.yml   # = manifest
./cc-compose.sh ps                 # smm-api/smm-web still 0cbd641, healthy
./cc-compose.sh migrate current    # new image, read-only: 3d4ac1986f07
./cc-compose.sh mutations status   # mode: read-only; smm-api ... role=ai_smm_cc
```

From here a mistaken `up` fails closed: the new release's API role gate
refuses `ai_smm_cc` until group D (its verify expects the SMM-025B tables).
*Rollback A:* copy `rollback-0cbd641/*` back, `SMM_CC_VERSION=0cbd641…`,
`./cc-compose.sh preflight` PASS; the loaded images can stay.

**Group B — Window and pre-migration backup.**

```bash
printf 'SMM_CC_WINDOW_START=%s\nSMM_CC_WINDOW_END=%s\n' "<start Z>" "<end Z, <= 2h, not 00:00-01:00 UTC>" > "$CC/control-center.window"
BK=/root/backups/ai-smm-control-center        # mode 700, outside the nightly rotation
./cc-db-backup.sh dump   --container "$PG" --out-dir "$BK"          # identity + queue gates; prints <dump>
./cc-db-backup.sh verify --container "$PG" --dump <dump> --expect-revision 3d4ac1986f07
#   restore check into ai_smm_restore_check_<stamp>, dropped after; writes control-center.backup-evidence
# Off-host exactly as control-center-deployment.md step 5b: the laptop pulls <dump>, .sha256,
# .source; `sha256sum -c` OK; encrypts (gpg, owner's key), test-decrypts. Then on the host, with
# the sha256 the LAPTOP printed:
( umask 077; printf '%s\n' 'SMM_CC_OFFHOST_DUMP_SHA256=<sha256 printed on the laptop>' \
    "SMM_CC_OFFHOST_CONFIRMED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    'SMM_CC_OFFHOST_CONFIRMED_BY=<owner>' > "$CC/control-center.offhost-ack" )
```

*Rollback B:* nothing to undo (a dump and evidence files only).

**Group C — Migrate to `a7c2e5f19b40` (worker keeps running).**

```bash
q | tee "$EV/q-C-before.txt"
./cc-compose.sh migrate upgrade a7c2e5f19b40   # gates: identity, queue window, backup evidence
./cc-compose.sh migrate current                # a7c2e5f19b40 (head)
q | tee "$EV/q-C-after.txt"                    # fingerprint identical; worker healthy
```

The old worker is safe on the new schema (§2; compat check above): it
sees only legacy rows, and the claim guard refuses it any account row —
of which none can exist. *Rollback C:* `./cc-compose.sh migrate downgrade 3d4ac1986f07`
(allowed: no account row; it voids the evidence) — from the new release,
which carries the downgrade; then group D's rollback.

**Group D — Grants of the read-only role for the new tables.**

```bash
docker exec -i "$PG" psql -X -v ON_ERROR_STOP=1 -U "$PGSU" -d ai_smm -f - < "$STG/ai_smm_cc.grants.sql"
./cc-compose.sh preflight      # gate api-role: PASS -- ai_smm_cc ...; N checks, no FAIL
```

Convergent and transactional; the running `0cbd641` API keeps working
(it reads only tables it still has). *Rollback D:* the same with
`/root/ai-smm-control-center-staging/0cbd641…/host/ai_smm_cc.grants.sql` (its
`REVOKE ALL` removes the new `SELECT`s), then group A's rollback.

**Group E — Switch the Control Center to `M` (read-only).**

```bash
q | tee "$EV/q-E-before.txt"
./cc-compose.sh up                       # identity + API role gate, then smm-api/smm-web on M
./cc-compose.sh ps; curl -fsS http://127.0.0.1:8088/health/ready
./cc-compose.sh mutations status         # mode: read-only; AI_SMM_API_MUTATIONS_ENABLED=false role=ai_smm_cc
ro "SELECT usename, count(*) FROM pg_stat_activity WHERE datname = 'ai_smm' GROUP BY 1"
q | tee "$EV/q-E-after.txt"
```

Smoke through the SSH tunnel (owner login): Projects overview lists
`ai-catalog-consultant`; Project Settings render (legacy publishing shows
"не используется" — D3); Accounts: empty; Credentials: the read-only
banner, no form; Content and Queue as before with the 3 published rows;
one business `POST` answers `MUTATIONS_DISABLED`; logout. *Rollback E:*
group D's rollback, group A's rollback, `./cc-compose.sh up` (back on
`0cbd641`, its verify passes again).

**Group F — Switch the worker to `ai-smm:$W`.**

```bash
q | tee "$EV/q-F-before.txt"              # pending must be 0
cp -p "$WK/ai-smm.env" "$WK/ai-smm.env.bak-$(date -u +%Y%m%dT%H%M%SZ)"; cp -p "$WK/.env" "$WK/.env.bak-$(date -u +%Y%m%dT%H%M%SZ)"
echo 'AI_SMM_LEGACY_THREADS_PROJECTS=ai-catalog-consultant' >> "$WK/ai-smm.env"   # ignored by the old worker
cd "$WK"
# F1 -- preflight of the NEW image with exactly the worker's environment, read-only:
AI_SMM_VERSION=$W docker compose -p ai-smm run --rm --no-deps cli preflight worker
#   PASS schema.revision a7c2e5f19b40 / PASS legacy.allow_list.known_projects 1 allow-listed:
#   ai-catalog-consultant / PASS legacy.pending_rows_allowed / PASS legacy.deployment_token
#   / PASS accounts.credential_keys ...; exit 0. Any FAIL: stop here (nothing switched).
# F2 -- the switch:
sed -i "s/^AI_SMM_VERSION=.*/AI_SMM_VERSION=$W/" "$WK/.env"
docker compose -p ai-smm up -d --no-build --pull never worker
docker logs --since 5m ai-smm-worker 2>&1 | grep -E 'Preflight passed|ERROR|FAIL'   # Preflight passed only
q | tee "$EV/q-F-after.txt"               # fingerprint identical; worker healthy on ai-smm:$W
```

*Rollback F:* `AI_SMM_VERSION=706b383` in `.env`, `docker compose -p ai-smm up -d --no-build --pull never worker`
(the allow-list key can stay: the old worker ignores it). Schema and CC stay.
If the new worker exits 78 it has claimed nothing (§3a): roll back the same way.

**Smoke of legacy publishing after F (no real post):** the next real
publication of `ai-catalog-consultant` is the test; until then the
preflight line `legacy.pending_rows_allowed` and `legacy.deployment_token`
are the evidence. A dry-run check is not possible on the LIVE worker
without changing its mode, and is not proposed.

### 3.6 R7 — Credentials and business mutations (later, separately approved)

Not part of SMM-025C's execution, and **not possible with it**: the
release ships `CC_RELEASE_PERMITS_MUTATIONS=no`, so step 4 below is
refused by its wrapper; R7 needs a later, reviewed release that lifts it. Prerequisites: FU-024C2-1, FU-024C2-2,
FU-025C-1 decided; FU-025C-2 (key and allow-list packaging) implemented;
FU-024C1-5's checklist executed on the exact release; FU-024B4A-1
considered (any later migration with a writable queue). Then, each
approved separately:

1. **Keys.** On the host, from the worker image, into a new directory:
   `install -d -m 700 -o 10001 -g 10001 /srv/ai-smm/secrets`, then
   `cd /root/ai-smm && docker compose -p ai-smm run --rm --no-deps -v /srv/ai-smm/secrets:/k cli credentials keygen --private-key-file /k/credentials.pem`
   (runs as 10001 with the worker's environment; mode 600, O_EXCL; prints
   the key id and the public key — not secret).
   Off-host: copy the private key to the laptop, encrypt with gpg,
   test-decrypt, keep **separately** from database dumps (a dump plus the
   key is every token in plaintext); record key id and public key in the
   deployment record.
2. **Mount and configure** (FU-025C-2): worker `ai-smm.env`
   `AI_SMM_CREDENTIALS_PRIVATE_KEY_FILES=/run/ai-smm/credentials.pem` with
   a read-only mount; API: the public key and the allow-list; preflight
   (F1) again, worker switch (F2) again.
3. **Mutation role.** `ai_smm_cc_rw.grants.sql` as in group D;
   `\password ai_smm_cc_rw` interactively; `control-center.api-rw.env`
   (600); `./cc-compose.sh preflight` stays on `ai_smm_cc`.
4. **Enable.** `./cc-compose.sh mutations enable --approved-by <owner>`
   inside a window; Q before/after.
5. **Smoke without a publication:** create an account in a test project,
   save a test credential **without verification**, check the
   Credentials page shows only the fingerprint, revoke it; create a draft
   and a revision, submit and reject it; schedule nothing.
6. **Disable** at any sign of trouble: `./cc-compose.sh mutations disable`
   (read-only within one recreate). Remove the role with
   `ai_smm_cc_rw.revoke.sql` if the mode is abandoned.

### 3.7 Order and why

A before C (the migration comes with the new release's images) → B
before C (the gate requires the evidence) → C before D (grants name the
new tables) → D before E (the new release's gate checks them) → E and F
in either order, F last is proposed (the worker is the only component
that publishes; the longest-proven part changes last) → R7 only after
its prerequisites. Within one window A–F take about 30–45 minutes; A1
and B's off-host copy can be done ahead of the window.

## 3a. Worker preflight (mandatory, fail-closed)

`ai_smm.preflight.worker_preflight` runs in `Worker.run()` **before** recovery, the scheduler and any claim. On any `FAIL` the worker logs every failed check and exits with **78 (EX_CONFIG)** — so a release started with a configuration that does not fit the queue publishes nothing *and fails nothing*: every scheduled row stays `scheduled`, untouched, until the configuration is fixed and the worker restarted (a restart loop under `restart: unless-stopped` is harmless for the same reason). The operator command `ai-smm preflight worker` runs the same checks read-only and exits 1 on any `FAIL`; R4a uses it before the switch.

| Check | FAIL when | Why |
|---|---|---|
| `schema.revision` | `alembic_version` ≠ `a7c2e5f19b40` (or missing); nothing else is checked | the new worker's queries need the new columns: migrate first (R2) |
| `legacy.allow_list.known_projects` | an id in `AI_SMM_LEGACY_THREADS_PROJECTS` is not a project | a typo would silently leave the intended project out |
| `legacy.pending_rows_allowed` | any `scheduled`/`claimed` row **without** an account belongs to a project outside the allow-list | exactly the forgotten-allow-list case: those rows would otherwise be failed one by one |
| `legacy.deployment_token` | live mode, allow-listed pending legacy rows exist, `THREADS_ACCESS_TOKEN` empty | they could not be published (value never printed) |
| `accounts.credential_keys` | an active credential's `key_id` has no private key in `AI_SMM_CREDENTIALS_PRIVATE_KEY_FILES` | its account's rows could not be decrypted |
| (key file) | the private key file is unreadable or group/world-readable | worker exits 78 before the preflight; the CLI reports `FAIL|accounts.private_key_files` |
| `accounts.pending_rows_ready` | never FAIL — **INFO** | a disabled account or revoked credential is an owner's decision; its rows are refused one by one, as designed, and must not stop every other project |

No check prints a secret (at most project ids, counts and key ids). Tests: `tests/test_preflight.py`, in particular `test_a_misconfigured_worker_never_claims_and_leaves_the_queue_as_it_was`.

## 4. Rollback

**Rule: once real `SocialAccount` / `Credential` data exists, roll back the application, not the schema.** The migration is additive and the old code runs on the new schema (old worker: legacy rows as before, account rows refused by the guard; old CC: never reads the new tables), so an application rollback loses nothing and is itself reversible. A schema downgrade is for the window before any account exists (R2 → R5), or for a deliberate, approved decision to discard that data.

| From | To | Procedure | Data |
|---|---|---|---|
| New worker | Old worker | redeploy previous image; **schema stays** | nothing lost; legacy rows publish as before; account rows are refused by the guard (fail loudly, nothing sent) and wait for the new worker |
| New CC | Old CC (`0cbd641`) | re-apply the old grants file `/root/ai-smm-control-center-staging/0cbd641…/host/ai_smm_cc.grants.sql` (removes `SELECT` on new tables so the old verify passes), restore `$CC/rollback-0cbd641/*` and `SMM_CC_VERSION`, `./cc-compose.sh up`; **schema stays** (§3.5 E) | nothing lost |
| CC mutation mode | read-only | `./cc-compose.sh mutations disable` (§3.6) | nothing lost |
| Schema `a7c2e5f19b40` | `3d4ac1986f07`, **no** account data yet | `alembic downgrade 3d4ac1986f07` | nothing to lose |
| Schema `a7c2e5f19b40` | `3d4ac1986f07`, account data exists | **not recommended.** Refused unless all account-bound rows are `published`/`cancelled` (no override) **and** `AI_SMM_DOWNGRADE_DESTROYS_ACCOUNT_DATA=yes` is set; backup first | **destroyed**, see §4.1 |
| Keys | — | see security report §4 | — |

### 4.1 What a downgrade destroys

A downgrade of `a7c2e5f19b40` drops, permanently and without a copy:

- every `social_accounts` row — labels, status, the bound Threads identity (`external_account_id`, `external_username`);
- every `social_account_credentials` row — the sealed tokens (owners must re-enter them) and the credential history (fingerprints, verification and revocation times);
- `content_revisions.social_account_id` — the approved destination of every revision. Their `content_hash` still includes the dropped account id, so a re-upgrade cannot restore the destinations from the hashes;
- `publications.social_account_id` — the account attribution of every publication, **including published ones**: afterwards nothing records which account a past post went out on, except the audit log (`content.materialized` details) and the platform itself.

That is why the migration refuses a downgrade over existing account data by default, and why the switch that permits it is named for what it does. The only way back is the backup taken before the downgrade.

## 5. Removing a project created by mistake

There is no delete in the API. A project without content can be removed by the owner role, after a backup, in one transaction: delete its `social_account_credentials`, `social_accounts`, `project_settings`, `project_memberships`, `projects` rows. Content and publications reference the project with `ON DELETE RESTRICT`, so a project that has any cannot be removed by accident.

## 6. Verified locally

| Check | Test |
|---|---|
| Upgrade keeps every existing publication byte-identical, as legacy rows | `test_migration_social_accounts.py::test_existing_rows_keep_their_meaning` |
| Only one column, one FK, one index added to `publications` | same |
| Old-worker claim refused for account rows, allowed for legacy rows | `test_an_old_worker_claim_cannot_take_an_account_row` |
| Downgrade refused with a live account row, nothing dropped | `test_a_downgrade_refuses_while_an_account_row_is_live` |
| Downgrade over account data refused without the explicit switch (unset, empty, `no`, `true`); data kept | `test_a_downgrade_that_would_destroy_account_data_needs_the_explicit_switch` |
| The switch never overrides the live-row refusal | `test_the_destroy_switch_never_overrides_the_live_row_refusal` |
| With the switch and only terminal rows, downgrade restores the exact schema | `test_a_downgrade_with_only_terminal_account_rows_restores_the_schema` |
| Misconfigured new worker refuses to start; scheduled rows untouched, no attempt written | `test_preflight.py::test_a_misconfigured_worker_never_claims_and_leaves_the_queue_as_it_was` |
| Up/down/up repeatable; models == migrations | `test_the_migration_is_repeatable`, `test_migrations.py::test_migration_matches_models` |
| `lock_timeout` via `PGOPTIONS` still bounds the whole upgrade | `test_migrations.py::test_a_lock_timeout_rolls_back_every_pending_revision` |
| Grants file + verify file on a migrated DB: 0 FAIL; recreated tables fail closed until grants re-run | `test_db_least_privilege.py::test_migrations_run_as_the_owner_alongside_the_role` |
