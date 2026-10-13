# Control Center — rollback

SMM-024B.3. What to do when a step of
[control-center-deployment.md](control-center-deployment.md) fails, or
the deployed Control Center has to be taken back.

> **Status (2026-10-13).** The Control Center was deployed on 2026-10-11
> (release `8ccccda`, schema `3d4ac1986f07`, mutations off) and accepted
> ([production-acceptance-smm-024b.md](production-acceptance-smm-024b.md));
> since 2026-10-13 it runs release `0cbd641` with `smm-api` on the role
> `ai_smm_cc`
> ([production-acceptance-smm-024c2.md](production-acceptance-smm-024c2.md)).
> None of the rollbacks in this document has been run. The role's own
> RB-1 ([least-privilege-role.md §7.8](least-privilege-role.md#78-rollback))
> ran once, automatically, during the first A3 attempt on 2026-10-13 and
> restored the owner URL as designed. Every block that touches the VPS is
> **NOT EXECUTED — REQUIRES OWNER APPROVAL**.
>
> Applied to the current production: §1 (stop) is the default rollback;
> for the role, RB-1 comes first (below); §3 can now go back to the
> previous release `8ccccda`, whose images are still on the host — but
> only after RB-1, because that image's login does not expect the
> restricted role (P1's rollback in least-privilege-role.md §7); §4.1
> belongs to the migration that already succeeded; for §4.3
> a downgrade needs a newly approved window, and `$EV/worker-before.txt`
> is the 2026-10-11 baseline — take a fresh one before acting. The
> pre-migration dump of 2026-10-11 is kept at least until 2026-11-10.
> Variables (`CC`, `EV`, `BK`, `PGC`, `PGU`, `ro`) and the session's
> `export DOCKER_HOST=unix:///var/run/docker.sock` are those of the
> deployment runbook, §5.

---

## 1. Principles

1. **The worker is never part of a Control Center rollback.** Nothing
   here stops, restarts, recreates or reconfigures `ai-smm-worker`, or runs
   anything in `/root/ai-smm`. If publishing itself must stop, that is the
   worker's own lever ([operations.md §7](operations.md#7-restart-recovery-rollback))
   and a separate decision.
2. **Only through `cc-compose.sh`.** It accepts only its own verbs, pins
   project `ai-smm-control-center` and the Docker daemon
   (`control-center.target`); `down` there removes its two containers and
   its two networks (`ai-smm-cc-internal`, `ai-smm-cc-edge`) and nothing
   else — never volumes or images. The shared network is external:
   Compose never removes it. If the wrapper refuses during a rollback,
   fix what it names; do not fall back to a bare `docker compose`.
3. **Prefer leaving the schema in place.** Both migrations are additive
   and the deployed worker's own suite passes on the head schema
   ([deployment §4.2](control-center-deployment.md#42-compatibility-with-the-running-worker)).
   Stopping the Control Center is a complete rollback of its behaviour;
   the extra tables do nothing on their own.
4. **Downgrade only with a reason, restore almost never.** `alembic
   downgrade` deletes the Control Center's data. Restoring the
   pre-migration dump over the live database deletes the **worker's**
   data written since — including publication outcomes — and can lead to a
   post being published twice (§4.4).
5. Record every rollback command and its output in `$EV/`.

The baseline every rollback returns to: `ai-smm-worker` identical to
`$EV/worker-before.txt`, `docker compose ls` identical to
`$EV/compose-ls-before.txt`, nothing listening on port 8088.

### Stop the Control Center (used by every scenario below)

`down`, `ps` and `logs` pass no gate: they work while the database is
unreachable. `up` and `restart` pass the identity gate.

> **NOT EXECUTED — REQUIRES OWNER APPROVAL**

```bash
cd /root/ai-smm-control-center
./cc-compose.sh down
./cc-compose.sh ps -a                                   # empty
ss -ltnH 'sport = :8088'                                # empty
docker network ls --filter name=ai-smm-cc --format '{{.Name}}'   # empty
docker network inspect <shared-network> --format '{{.Name}}'      # still there
docker inspect ai-smm-worker --format \
  '{{.Id}} {{.Image}} {{.Config.Image}} {{.State.StartedAt}} restarts={{.RestartCount}} {{.State.Health.Status}}' \
  | diff - "$EV/worker-before.txt" && echo "worker identical"
```

This leaves the database, its schema and the two images in place.

### After SMM-024C-2 (the API's own database role)

Once `smm-api` connects as `ai_smm_cc`, putting it back on the owner URL
— and, separately, disabling or retiring the role — is
[least-privilege-role.md §7.8](least-privilege-role.md#78-rollback)
(RB-1 to RB-3). With the files of release `0cbd641` (production since
2026-10-13), every wrapper verb — `down`, `ps` and `logs` included —
needs `control-center.api.env` to exist (Compose loads it for
`smm-api`), and `up`/`restart` also pass the API role gate. Keep the file
until the previous files are back (RB-1); remove it only as part of
RB-3. A readiness 503 or a `permission denied` in the API log
right after that switch is RB-1's trigger, not §2 below.

---

## 2. Web or API failure

Symptoms: `up --wait` fails or times out; a container `unhealthy` or
restarting; 502 `DEPENDENCY_UNAVAILABLE` from nginx; 5xx from the API;
the smoke test fails.

> **NOT EXECUTED — REQUIRES OWNER APPROVAL** (diagnosis is read-only)

```bash
./cc-compose.sh ps -a
./cc-compose.sh logs --since 15m smm-api smm-web
docker inspect "$(./cc-compose.sh ps -q smm-api)" --format '{{json .State.Health}}'
curl -sS http://127.0.0.1:8088/health/ready; echo
```

| Finding | Action |
|---|---|
| readiness 503 `database` | `control-center.env` DSN or the shared network is wrong → stop (§1), fix, step 8 again |
| API 500 on every query, log mentions a missing table/column | the image is ahead of the schema → stop; check `migrate current` against the image's `alembic heads` (§3) |
| `MemoryError` / OOM-killed | stop; raising `mem_limit` is a reviewed compose change |
| web unhealthy, API healthy | nginx log; stop; it is a packaging defect, fix locally |
| a single transient failure | `./cc-compose.sh restart smm-api` once; if it recurs, stop |

Data impact: none. The API wrote at most session and audit rows.

---

## 3. Incorrect image version

The wrapper refuses an image whose `org.opencontainers.image.revision`
label is not **exactly** `SMM_CC_VERSION` (a prefix does not match), an
image built for another architecture, and any version that is not a full
40-hex commit — so "the wrong bytes under the right tag" and `latest` are
excluded. What remains is a correctly labelled image of the wrong commit.

Rules for the version to go back to:

- its `alembic heads` must equal the database revision
  (`3d4ac1986f07` after the migration). Check it without a database:
  `docker run --rm --network none --entrypoint /app/.venv/bin/alembic -w /app ai-smm-api:<v> heads`;
- it must be **at or after `50f2f11`** (SMM-024B.2 merge). An API built
  before `5cf730b` has no mutation gate: started with this compose file it
  would accept business changes, because the variable that disables them
  did not exist yet;
- never "fix forward" by migrating the database further during a
  rollback.

> **NOT EXECUTED — REQUIRES OWNER APPROVAL**

```bash
cd /root/ai-smm-control-center
./cc-compose.sh down
sed -i 's/^SMM_CC_VERSION=.*/SMM_CC_VERSION=<previous-verified-full-commit>/' control-center.compose.env
./cc-compose.sh up                  # the wrapper re-checks engine, labels, architecture
# then deployment steps 9, 11, 12
```

If there is no earlier verified Control Center image (first deployment),
the rollback is §1 alone. Data impact: none.

---

## 4. Migration failure

### 4.1 The upgrade failed

`migrate upgrade 3d4ac1986f07` exited non-zero — lock timeout, statement
timeout, connection loss, any SQL error.

All pending revisions run in **one transaction**, so nothing was applied.
Verify, do not assume:

> **NOT EXECUTED — REQUIRES OWNER APPROVAL** (read-only)

```bash
./cc-compose.sh migrate current </dev/null                   # 79731eadacd7
ro "SELECT to_regclass('public.users'), to_regclass('public.content_items')"  # both empty
ro "SELECT count(*) FROM information_schema.columns
    WHERE table_name = 'projects' AND column_name IN ('description','version')"  # 0
ro "SELECT pid, application_name, state FROM pg_stat_activity
    WHERE application_name LIKE 'ai-smm-control-center%'"                      # none left (migrate)
docker inspect ai-smm-worker --format '{{.State.Health.Status}}'
```

The filter above finds `migrate`'s connection only: the API and `cli`
appear as `ai-smm/worker-1` (FU-024C1-1).

Then: no downgrade, no restore. Either retry once after
`./cc-compose.sh preflight` passes again (deployment step 5c; lock timeout
in a busy moment), or stop and analyse `$EV/migrate.log` locally. The
worker is unaffected: its schema never changed.

A refusal with `GATE CLOSED` (exit 2) is **not** a failed upgrade:
Compose was never called. The identity gate closed → stop, the database
is not the recorded one or the record is stale; resolve it in a separate
read-only check, never by editing `control-center.identity`. The queue
gate closed → wait for the queue to be quiet, or agree another window
([deployment §2.1](control-center-deployment.md#21-deployment-gates)).

If `current` unexpectedly shows `2964ac9ceea7` (only possible if someone
ran the revisions separately), the schema is consistent and the worker is
compatible with it; continue with `upgrade 3d4ac1986f07` or treat it as
§4.2.

### 4.2 The upgrade committed, but the Control Center must go

Default: **stop the Control Center (§1) and leave the schema.** The worker
is verified on it, nothing reads the new tables, and the next deployment
needs them anyway. Data impact: none.

### 4.3 The schema itself must be reverted — `alembic downgrade`

Only when the new schema is shown to harm the worker or the shared
instance. What `downgrade 79731eadacd7` removes:

| Removed | Holds after a read-only deployment | Lost on downgrade |
|---|---|---|
| `users`, `project_memberships` | the bootstrap owner | recreatable with `cli create-owner` |
| `user_sessions` | session hashes | every session ends; nothing else |
| `project_settings` | no rows (PATCH is refused) | nothing |
| `projects.description`, `default_timezone`, `default_language`, `version` | server defaults only | nothing |
| `content_items`, `content_revisions`, `content_approvals`, `content_publication_links` | no rows (creation is refused) | nothing **today**; the whole editorial history and the content→publication lineage if mutations were ever enabled |
| enums, trigger functions, triggers of the above | — | — |

Not touched: `publications`, `publication_attempts`, `content_series`,
`audit_log` (its `auth.*` rows stay, naming users that no longer exist),
`alembic_version` moves back to `79731eadacd7`. The downgrade runs in one
transaction with the same `lock_timeout` as the upgrade, and needs short
exclusive locks on `projects` and locks on `publications` and `users` to
drop the foreign keys — the same quiet-window rules apply, and are
enforced: `migrate downgrade` passes the identity and queue gates first,
so `control-center.window` must hold the window approved for the
downgrade. It does **not** need the upgrade's backup evidence — a stale
or missing one never blocks a rollback — and it invalidates it
(`control-center.backup-evidence` → `…invalidated-<stamp>`): a later
upgrade needs a new dump, restore check and off-host copy.

> **NOT EXECUTED — REQUIRES OWNER APPROVAL** (destructive schema change)

```bash
./cc-compose.sh down                                         # §1 first
# The window approved for this downgrade (UTC), then both gates
printf 'SMM_CC_WINDOW_START=%s\nSMM_CC_WINDOW_END=%s\n' \
    '<approved start, YYYY-MM-DDTHH:MM:SSZ>' '<approved end, YYYY-MM-DDTHH:MM:SSZ>' > control-center.window
./cc-compose.sh preflight | tee "$EV/preflight-before-downgrade.txt"   # exit 2 = STOP

# What would be lost -- must match the table above
ro "SELECT 'users', count(*) FROM users UNION ALL
    SELECT 'project_settings', count(*) FROM project_settings UNION ALL
    SELECT 'content_items', count(*) FROM content_items UNION ALL
    SELECT 'content_revisions', count(*) FROM content_revisions UNION ALL
    SELECT 'content_publication_links', count(*) FROM content_publication_links" \
  | tee "$EV/pre-downgrade-counts.txt"

# Keep it anyway: data-only export of the Control Center's tables
# (written to .partial; kept only if pg_dump succeeded -- otherwise STOP)
STAMP=$(date -u +%Y%m%dT%H%M%SZ); OUT="$BK/ai_smm-cc-tables-$STAMP.dump"
( umask 077
  docker exec "$PGC" pg_dump -U "$PGU" -d ai_smm -Fc --data-only \
      -t users -t user_sessions -t project_memberships -t project_settings \
      -t content_items -t content_revisions -t content_approvals -t content_publication_links \
    > "$OUT.partial" \
  && test -s "$OUT.partial" && mv "$OUT.partial" "$OUT" && sha256sum "$OUT" > "$OUT.sha256" ) \
  || { rm -f "$OUT.partial"; echo "EXPORT FAILED -- do not downgrade"; }
ls -l "$OUT"

./cc-compose.sh migrate downgrade 79731eadacd7 </dev/null
./cc-compose.sh migrate current </dev/null                   # 79731eadacd7
ro "SELECT status, count(*) FROM publications GROUP BY 1 ORDER BY 1"
docker inspect ai-smm-worker --format '{{.State.Health.Status}}'
```

Stop before the downgrade if the export failed (no `$OUT`), or if any
content table has rows or `project_settings` is not empty: that is data the table above says
cannot exist in read-only mode, and it needs its own decision.

The Control Center must stay down afterwards: its images need the head
schema.

### 4.4 Restore the pre-migration dump — last resort

**Not a rollback for this deployment.** Restoring the step 4 dump over
`ai_smm` rewinds the whole database to the dump time, including
everything the **live worker** did since:

- a publication that was `scheduled` at dump time and `published`
  afterwards comes back as `scheduled`, without its `threads_post_id` and
  `published_at`. `claim_due_publication` takes any `scheduled` row whose
  `scheduled_at` is in the past — there is no age limit — so on its next
  cycle the worker **publishes it again**: a duplicate post on Threads.
  The attempts that prove the first post existed are gone;
- attempts, `needs_review` decisions, series progress and audit rows
  written since are lost;
- replacing a database the worker is connected to interrupts the worker
  mid-cycle.

`alembic downgrade` (§4.3) reaches the same schema without any of this,
so a restore is only for a database that downgrade cannot repair. It
then needs its own approved plan, at minimum:

1. stop publishing first through the worker's own levers (a separate,
   approved worker change);
2. take a fresh dump of the current state, so nothing is lost for good;
3. restore the pre-migration dump into a **new** database, never over
   `ai_smm`;
4. for every `publications` row changed after the dump stamp
   (`updated_at > <stamp>` in the fresh dump), carry its status,
   `threads_post_id`, `published_at` and attempts over, and confirm each
   against the Threads account;
5. only then swap databases and resume the worker, in dry-run first.

The dump's value here is forensic and as the ultimate safety net: step 5
of the deployment proves it restores into a scratch database.

---

## 5. Partial startup

| State | Cause | Action |
|---|---|---|
| `smm-api` running, `smm-web` never created | API never became healthy (`depends_on: service_healthy`) | §2, then §1 |
| `smm-web` fails with "port is already allocated" | something else bound 8088 since step 1 | §1; identify the owner (`ss -ltnp 'sport = :8088'`) and do **not** stop it. The port is a literal: moving it is a reviewed compose change |
| `up` fails with "network … declared as external, but could not be found" | wrong `SMM_CC_SHARED_NETWORK` | nothing was started; fix `control-center.compose.env` |
| a `migrate`/`cli` container left behind | interrupted `run` | `./cc-compose.sh ps -a`; `./cc-compose.sh down --remove-orphans` (project-scoped) |
| networks `ai-smm-cc-*` left without containers | interrupted `up`/`down` | `./cc-compose.sh down`; if they remain, §6 |

After any of these: `./cc-compose.sh down`, the §1 checks, and start again
at the failed step. Data impact: none (the migration is a separate step
with its own scenario).

---

## 6. Network misconfiguration

> **NOT EXECUTED — REQUIRES OWNER APPROVAL** (read-only checks first)

```bash
ss -ltnH | grep ':8088'
docker ps --filter label=com.docker.compose.project=ai-smm-control-center --format '{{.Names}}\t{{.Ports}}'
for n in ai-smm-cc-internal ai-smm-cc-edge; do
  docker network inspect "$n" --format \
    '{{.Name}} internal={{.Internal}} project={{index .Labels "com.docker.compose.project"}} containers={{len .Containers}}'
done
docker network inspect <shared-network> --format '{{range .Containers}}{{.Name}} {{end}}'
```

| Finding | Action |
|---|---|
| 8088 bound on `0.0.0.0`/`[::]`, or reachable from outside | **stop at once** (§1); the committed file cannot produce this, so the host copy differs from Git: `diff` it against the reviewed commit |
| `smm-web` attached to `<shared-network>` | same: stop, compare the file |
| `ai-smm-cc-internal` not `internal=true` | same |
| a Control Center container still on `<shared-network>` after `down` | `docker network disconnect <shared-network> <that container>` — only containers of project `ai-smm-control-center`; never disconnect anything else |
| `ai-smm-cc-*` left over with `project=ai-smm-control-center` and `containers=0` | `docker network rm ai-smm-cc-internal ai-smm-cc-edge` — these two names only |
| an `ai-smm-cc-*` network with **another** project label | not ours: stop and investigate; do not remove it |
| API cannot resolve the database host | wrong shared network or DSN host; §1, fix the env files |

Never `docker network rm <shared-network>`, `docker network prune` or
`docker system prune` on this host.

---

## 7. After any rollback

> **NOT EXECUTED — REQUIRES OWNER APPROVAL** (read-only)

```bash
docker inspect ai-smm-worker --format \
  '{{.Id}} {{.Image}} {{.Config.Image}} {{.State.StartedAt}} restarts={{.RestartCount}} {{.State.Health.Status}}' \
  | diff - "$EV/worker-before.txt" && echo "worker identical"
docker compose ls -a
ss -ltnH 'sport = :8088'
ro "SELECT version_num FROM alembic_version"
ro "SELECT status, count(*) FROM publications GROUP BY 1 ORDER BY 1"
docker logs --since 30m ai-smm-worker 2>&1 | tail -n 20
```

Record the outcome, the database revision left in place, and whether the
images and `/root/ai-smm-control-center` stay for a retry (default) or
are removed (`docker image rm ai-smm-api:<v> ai-smm-web:<v>`; the
directory holds the database password — `shred -u control-center.env`).

Local rehearsal of §1 (stop leaves shared network, database and worker
untouched): `scripts/control_center_rehearsal.sh`.
