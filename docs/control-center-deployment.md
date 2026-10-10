# Control Center — production deployment runbook

SMM-024B.3. How the private Control Center (`smm-api` + `smm-web`) is
added to the production host next to the live worker, read-only, reachable
only through an SSH tunnel.

> **Status: packaging only. Nothing in this document has been run against
> production.** Every block that touches the VPS is marked
> **NOT EXECUTED — REQUIRES OWNER APPROVAL**. Approval of one step does
> not extend to the next.
>
> Names in angle brackets — `<shared-network>`, `<postgres-container>`,
> `<vps>`, `<project-id>`, `<owner-email>` — stand for resources and values
> that are deliberately not named in a public repository, as in
> [operations.md](operations.md).

Related: [control-center-rollback.md](control-center-rollback.md) ·
[runtime-packaging.md](runtime-packaging.md) (the images and the local
runtime) · [api.md](api.md#read-only-mode) (what read-only means) ·
[production-deployment-plan.md §17](production-deployment-plan.md#17-обновление-по-результатам-discovery-2026-10-10-smm-024b1)
(discovery facts) ·
[ADR](adr/automation-and-publication-safety.md).

---

## 1. What is deployed

```
operator laptop
  └─ ssh -L 8088:127.0.0.1:8088 <vps>
       └─ host 127.0.0.1:8088                   the only new listening socket
            └─ smm-web   nginx + React build     cc-edge, cc-internal
                 └─ /api, /health ─► smm-api     cc-internal, <shared-network>
                                      └─► <postgres-container>:5432 / ai_smm  (existing)

ai-smm-worker (project "ai-smm")                  untouched: not recreated,
                                                  restarted, reconfigured
```

| | |
|---|---|
| Compose file | `compose.control-center.yml` |
| Compose project | `ai-smm-control-center` (the worker's is `ai-smm`) |
| Host directory | `/root/ai-smm-control-center/` — **never** `/root/ai-smm/` |
| Services started by `up` | `smm-api`, `smm-web` |
| One-shot tools | `migrate` (profile `cc-migrate`), `cli` (profile `cc-admin`, `ai-smm user` only) |
| Not in this project | worker, scheduler, PostgreSQL, any volume or bind mount |
| Images | `ai-smm-api:<commit>`, `ai-smm-web:<commit>`, built locally, loaded with `docker load`; never built or pulled on the host (`pull_policy: never`) |
| Host ports | `127.0.0.1:8088` → `smm-web:8080`, nothing else |
| Public routes | none; `traefik.enable=false` on every service |
| Business mutations | **off** for every role, owner included (literal in the compose file) |
| Worker mode in the UI | `UNKNOWN` (no verifiable source) |

### Networks

| Network | Kind | Attached |
|---|---|---|
| `ai-smm-cc-internal` (`cc-internal`) | created by the project, `internal: true` — no route out | `smm-web`, `smm-api` |
| `ai-smm-cc-edge` (`cc-edge`) | created by the project, bridge; carries the loopback port | `smm-web` |
| `<shared-network>` (`shared`) | **external**, never created or removed here | `smm-api`, `migrate`, `cli` |

`smm-web` never joins the shared network: the web tier cannot see the
database or any other project's container. `smm-api` must join it, because
that is the only network the existing PostgreSQL is on — see §8 for what
that exposes.

### Hardening (every service)

Non-root (`10001`, nginx `101`), `read_only: true` with a `tmpfs` `/tmp`,
`cap_drop: ALL`, `no-new-privileges`, `pids_limit`, `mem_limit` =
`memswap_limit`, `cpus`, `json-file` logs rotated at 10 MB × 3, no Docker
socket, no volumes. Sizing (host: 2 vCPU, ≈ 2.5 GiB free, worker 384 MB):

| Service | Memory | CPU | PIDs |
|---|---|---|---|
| `smm-api` | 320 MB | 0.5 | 128 |
| `smm-web` | 64 MB | 0.25 | 64 |
| `migrate`, `cli` (while running) | 256 MB | 0.5 | 64 |

Measured in the local rehearsal after the smoke test: `smm-api` 63 MiB,
`smm-web` 9 MiB.

---

## 2. Configuration contract

Three files in `/root/ai-smm-control-center/`, plus the wrapper:

| File | Source in Git | Holds | Mode |
|---|---|---|---|
| `compose.control-center.yml` | copied unchanged | services, literals | 644 |
| `cc-compose.sh` | `deploy/control-center/cc-compose.sh` | the only way to run Compose here | 755 |
| `control-center.compose.env` | `deploy/control-center/control-center.compose.env.example` | **exactly** `SMM_CC_VERSION`, `SMM_CC_SHARED_NETWORK` — not secret | 644 |
| `control-center.env` | `deploy/control-center/control-center.env.example` | **exactly** `AI_SMM_DATABASE_URL` — secret | **600** |

The filled files never enter Git (`.gitignore`: `*.env`) or an image
(`.dockerignore`: `deploy/`, `.env*`; the web context is `web/` only).

### Container environment

Everything except the database URL is a **literal** in
`compose.control-center.yml`. Compose gives `environment:` precedence over
`env_file:`, so a `control-center.env` that wrongly carried a token or
`AI_SMM_API_MUTATIONS_ENABLED=true` still could not change any of these
(`tests/test_control_center_packaging.py` renders exactly that case):

| Variable | Value | Why |
|---|---|---|
| `AI_SMM_DATABASE_URL` | from `control-center.env` | the existing `ai_smm` database |
| `AI_SMM_API_MUTATIONS_ENABLED` | `false` | read-only for business operations, every role |
| `AI_SMM_API_DOCS_ENABLED` | `false` | no `/docs`, `/redoc`, `/openapi.json` |
| `AI_SMM_API_COOKIE_SECURE` | `false` | the transport is an SSH tunnel to plain-HTTP loopback; not every browser returns a `Secure` cookie there. The tunnel encrypts the session on the wire |
| `AI_SMM_DRY_RUN` | `true` | the **API process's** flag (`api_dry_run`); not the worker's mode |
| `OPENAI_API_KEY`, `THREADS_ACCESS_TOKEN`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `STORAGE_SSH_PASSWORD`, `STORAGE_SSH_KEY_PATH` | empty | no credential reaches the API; also masks anything `python-dotenv` could find (the images contain no `.env`, verified) |
| `THREADS_API_BASE_URL` | `http://127.0.0.1:9` | unroutable |
| `PGAPPNAME` | `ai-smm-control-center-{api,migrate,cli}` | the Control Center's connections are visible in `pg_stat_activity` |
| `PGOPTIONS` (`migrate` only) | `-c lock_timeout=5s -c statement_timeout=120s` | §4.3 |
| `TZ`, `LOG_LEVEL`, `LOG_FORMAT` | `UTC`, `INFO`, `json` | |

Turning mutations on is therefore a reviewed change to the compose file,
not an environment edit.

### Why a wrapper, and what it checks

Compose takes the project name from `COMPOSE_PROJECT_NAME` — exported in
the shell **or written in a `.env` in the working directory** — before the
file's own `name:`. Verified locally (Compose v5.4): with
`COMPOSE_PROJECT_NAME=ai-smm` in a `.env`, `docker compose -f
compose.control-center.yml config` renders project `ai-smm`, the worker's.
Run from `/root/ai-smm`, a bare `down` would act on the worker's project.
`.env` values are also used for `${...}` interpolation.

`deploy/control-center/cc-compose.sh` closes that:

- refuses a directory that holds `.env`, `compose.yaml`, `compose.yml`,
  `docker-compose.yml` or `ai-smm.env`;
- refuses either env file if it defines any key outside its allowlist
  (e.g. `COMPOSE_PROJECT_NAME`), or uses `export`/non-`KEY=value` lines;
- refuses `control-center.env` unless it is `600` (no group/other bits);
- refuses an `SMM_CC_VERSION` that is not a 7–40 hex commit id (`latest`
  is impossible);
- for `up`, `run`, `create`, `start`, `restart`: refuses unless both images
  are loaded and their `org.opencontainers.image.revision` label starts
  with `SMM_CC_VERSION`;
- runs `docker compose --project-name ai-smm-control-center
  --project-directory <dir> --file <dir>/compose.control-center.yml
  --env-file <dir>/control-center.compose.env` under `env -i` (only `PATH`,
  `HOME`, `TERM`, and `DOCKER_HOST`/`DOCKER_CONTEXT` if set), so nothing
  exported in the operator's shell reaches Compose.

It performs only reads itself.

### Database role

The API connects as the existing role `ai_smm` — the database owner, the
same role the worker uses. This is what SMM-024B.2 decided (§17.3 of the
plan): it creates nothing on the shared instance. Its cost: the API's
database credentials could do anything the worker's can, and the
read-only guarantee rests on the application (the central mutation gate
plus the service-level checks), not on database privileges. A dedicated
least-privilege role is follow-up FU-024B3-1
([follow-ups.md](follow-ups.md)).

---

## 3. Local preparation (no production access)

Build from a **clean export of the reviewed commit**, never from a
working tree:

```bash
git fetch origin && git switch --detach <reviewed-commit>
test -z "$(git status --porcelain)"                  # clean tree

FULL=$(git rev-parse HEAD)
V=$(git rev-parse --short=12 HEAD)
B=$(mktemp -d) && git archive "$FULL" | tar -x -C "$B"

docker build --target api \
    --label org.opencontainers.image.revision="$FULL" -t "ai-smm-api:$V" "$B"
docker build \
    --label org.opencontainers.image.revision="$FULL" -t "ai-smm-web:$V" "$B/web"

# The migration chain this image carries; must print 3d4ac1986f07 (head).
docker run --rm --network none --entrypoint /app/.venv/bin/alembic \
    -w /app "ai-smm-api:$V" heads

# Validation (section 7 lists what each covers)
uv run pytest tests/test_control_center_packaging.py tests/test_runtime_packaging.py
AI_SMM_TEST_DATABASE_URL=... uv run pytest
AI_SMM_TEST_DATABASE_URL=... scripts/check_worker_schema_compat.sh 6c54669
scripts/control_center_rehearsal.sh "$V"             # full runbook, disposable stand-ins

# Transfer artefact
docker save "ai-smm-api:$V" "ai-smm-web:$V" | gzip > "ai-smm-cc-$V.tar.gz"
sha256sum "ai-smm-cc-$V.tar.gz" > "ai-smm-cc-$V.tar.gz.sha256"
docker image inspect "ai-smm-api:$V" --format '{{.Architecture}}'   # amd64
```

Record `$FULL`, `$V`, the tarball's SHA-256 and the test results in the
change record.

Images are pinned by an immutable commit tag plus a build-time OCI label
that the wrapper verifies. They are not pinned by image ID in the compose
file: after `docker save`/`docker load` the ID depends on the receiving
daemon's image store (classic vs containerd), so it is not knowable at
build time.

---

## 4. Database migration: compatibility assessment

Production is at `79731eadacd7`; the images carry head `3d4ac1986f07`.
Two revisions are pending: `2964ac9ceea7` (control plane) and
`3d4ac1986f07` (content revisions, approval).

### 4.1 What the SQL does

| Revision | Creates | Alters existing | Locks taken on existing tables |
|---|---|---|---|
| `2964ac9ceea7` | `users`, `user_sessions`, `project_memberships`, `project_settings`, enum `membership_role`, 4 indexes | `projects`: `ADD COLUMN` ×4 (`description`, `default_timezone`, `default_language`, `version`), each `NOT NULL` with a constant server default | `projects`: SHARE ROW EXCLUSIVE (new FKs), then ACCESS EXCLUSIVE (`ADD COLUMN`) |
| `3d4ac1986f07` | `content_items`, `content_revisions`, `content_approvals`, `content_publication_links`, 3 enums, 2 plpgsql functions, 3 triggers (on the new tables only), 4 indexes | nothing | `projects`, `publications`, `users`: SHARE ROW EXCLUSIVE (FKs from the new tables) |

No existing row is read or rewritten. In PostgreSQL 16 an `ADD COLUMN`
with a constant default is a catalogue change, not a table rewrite.
`publications`, `publication_attempts`, `content_series` and `audit_log`
are not altered (`tests/test_migrations.py` pins this).

### 4.2 Compatibility with the running worker

The worker runs `ai-smm:706b383`, whose code is identical to `6c54669` on
`main`. Between `6c54669` and this commit, `worker.py`, `queue.py`,
`publishing/`, `series.py` and `healthcheck.py` are byte-identical; only
the ORM models gained classes and columns the worker never queries.

| Worker operation | On the head schema |
|---|---|
| reads/updates `publications`, `publication_attempts`, `content_series`, `audit_log` | unchanged tables |
| reads `projects` | selects its own column list; the four new columns are ignored |
| inserts `projects` (`import-json`, `ensure_project`) | the new columns take their server defaults |
| deletes a `publications` row | never happens in worker or CLI code; `content_publication_links` (`ON DELETE RESTRICT`) would refuse it only for a linked row, and none can exist while mutations are off |

Verified, not only argued: `scripts/check_worker_schema_compat.sh 6c54669`
runs the **deployed worker's own test suite** (queue claiming, worker
cycle, publish service, series publishing, importer, CLI) against a
database built by this commit's `alembic upgrade head`: 232 passed,
1 skipped (`test_importer.py`: the pilot queue file is not in a
`git archive` checkout — unrelated to the schema).

### 4.3 Running it next to a live worker

- **One transaction.** `alembic/env.py` runs every pending revision inside
  one `context.begin_transaction()` (no `transaction_per_migration`), and
  PostgreSQL DDL is transactional. A failure at any point leaves the
  database exactly at `79731eadacd7`.
- **Fail fast on locks.** `ADD COLUMN` waits for every open transaction
  that has read `projects`, and while it waits, every later reader queues
  behind it — including the worker's next cycle. The `migrate` service
  sets `lock_timeout=5s` (and `statement_timeout=120s`) through
  `PGOPTIONS`, so a conflict aborts the upgrade instead of stalling the
  worker. `tests/test_migrations.py::test_a_lock_timeout_rolls_back_every_pending_revision`
  holds a reader on `projects`, runs `79731eadacd7 → head` with the same
  mechanism, and asserts the timeout, an unchanged schema snapshot and
  revision, then a clean retry.
- **The worker's transactions are short.** It commits after the claim and
  before every external call (`mark_publishing` is committed before the
  Threads request), and polls every 60 s.
- **Pick a quiet window**: no publication due within the next hour, none
  in `claimed`/`publishing`, not during the nightly `pg_dump` (03:30 host
  time, Europe/Vilnius), which holds ACCESS SHARE on every table.
- **Explicit revision.** The runbook runs `upgrade 3d4ac1986f07`, not
  `upgrade head`: an image that unexpectedly carried a further revision
  could not apply it.
- **Never on start.** The API image's entrypoint is uvicorn only;
  `migrate` has no default command and sits behind its own profile.

### 4.4 What the Control Center writes after the migration

With mutations off: the bootstrap owner (`users`, `project_memberships`,
one `audit_log` row, via `cli`); per login/logout `user_sessions` rows,
`users.last_login_at`, `auth.*` audit rows; `user_sessions.last_seen_at` on
authenticated requests. Nothing else. No content, approval, project
setting or publication row can be written through the API.

### 4.5 Assessment

**Compatible.** The migration is additive, runs atomically, fails fast
rather than blocking the worker, and the deployed worker's suite passes
on the resulting schema. Residual risk: a lock timeout (harmless, retry)
and the operator choosing a busy window (mitigated by the step 3 checks).
For reversal see [control-center-rollback.md §4](control-center-rollback.md#4-migration-failure).

---

## 5. Production runbook

Run from a root shell on the host, **never in `/root/ai-smm`**. Each step
has a stop condition; on any stop, go to the rollback document and do not
continue.

```bash
# Used throughout. Values come from `docker ps` / the deployment record.
CC=/root/ai-smm-control-center
EV=$CC/evidence                              # evidence of this run, mode 700
PGC='<postgres-container>'
V='<12-char commit>'
```

### Step 0 — Stage the artefacts (no runtime effect)

> **NOT EXECUTED — REQUIRES OWNER APPROVAL**

```bash
# From the laptop
ssh <vps> 'install -d -m 700 /root/ai-smm-control-center /root/ai-smm-control-center/evidence'
scp ai-smm-cc-$V.tar.gz ai-smm-cc-$V.tar.gz.sha256 \
    compose.control-center.yml deploy/control-center/cc-compose.sh \
    <vps>:/root/ai-smm-control-center/

# On the host
cd "$CC"
sha256sum -c "ai-smm-cc-$V.tar.gz.sha256"
df -h /var/lib/docker                        # ≥ 2 GB free before loading
gunzip -c "ai-smm-cc-$V.tar.gz" | docker load
for i in ai-smm-api ai-smm-web; do
  docker image inspect "$i:$V" --format \
    '{{.RepoTags}} {{.Id}} {{.Architecture}} {{index .Config.Labels "org.opencontainers.image.revision"}}'
done | tee "$EV/images.txt"
docker run --rm --network none --entrypoint /app/.venv/bin/alembic -w /app "ai-smm-api:$V" heads

chmod 755 cc-compose.sh
printf 'SMM_CC_VERSION=%s\nSMM_CC_SHARED_NETWORK=%s\n' "$V" '<shared-network>' > control-center.compose.env
( umask 077 && install -m 600 /dev/null control-center.env )
# Edit control-center.env by hand: one line, AI_SMM_DATABASE_URL=..., the
# same DSN the worker uses (read it from /root/ai-smm/ai-smm.env without
# printing it). Do not copy any other line.
ls -l "$CC"
./cc-compose.sh config --quiet && echo config-ok
```

Stop if: checksum mismatch; label ≠ `$FULL`; architecture ≠ host's
`uname -m`; `alembic heads` ≠ `3d4ac1986f07 (head)`; the wrapper refuses.

`docker load` only adds two images; it does not touch any container.

### Step 1 — Baseline

> **NOT EXECUTED — REQUIRES OWNER APPROVAL**

```bash
date -u; uname -m
docker version --format '{{.Server.Version}}'; docker compose version --short
docker compose ls -a | tee "$EV/compose-ls-before.txt"      # no ai-smm-control-center yet
ss -ltnH 'sport = :8088'                                     # must print nothing
docker network inspect <shared-network> --format '{{.Name}} internal={{.Internal}}'
docker network ls --filter name=ai-smm-cc --format '{{.Name}}'   # must print nothing
docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}' | tee "$EV/ps-before.txt"
free -m
```

Stop if: port 8088 in use; an `ai-smm-control-center` project or
`ai-smm-cc-*` network already exists (investigate first); memory
available < 600 MB.

### Step 2 — Worker health and identity

> **NOT EXECUTED — REQUIRES OWNER APPROVAL**

```bash
docker inspect ai-smm-worker --format \
  '{{.Id}} {{.Image}} {{.Config.Image}} {{.State.StartedAt}} restarts={{.RestartCount}} {{.State.Health.Status}}' \
  | tee "$EV/worker-before.txt"
docker inspect ai-smm-worker --format '{{range .Config.Env}}{{println .}}{{end}}' \
  | grep '^AI_SMM_DRY_RUN='                                 # expected false: LIVE
docker logs --since 30m ai-smm-worker 2>&1 | tail -n 20
```

Stop if: the worker is not `healthy`, or its logs show errors.

### Step 3 — Database revision and a quiet window

> **NOT EXECUTED — REQUIRES OWNER APPROVAL** (read-only queries)

```bash
PGU=$(docker inspect "$PGC" --format '{{range .Config.Env}}{{println .}}{{end}}' | sed -n 's/^POSTGRES_USER=//p')
ro() { docker exec -i -e PGOPTIONS='-c default_transaction_read_only=on' "$PGC" \
         psql -U "$PGU" -d ai_smm -v ON_ERROR_STOP=1 -tA -c "$1"; }

ro "SELECT version_num FROM alembic_version"                          # 79731eadacd7
ro "SELECT to_regclass('public.users'), to_regclass('public.content_items')"   # both empty
ro "SELECT status, count(*) FROM publications GROUP BY 1 ORDER BY 1" | tee "$EV/publications-before.txt"
ro "SELECT count(*), max(id) FROM publication_attempts"            | tee "$EV/attempts-before.txt"
ro "SELECT id, status, scheduled_at, next_attempt_at FROM publications
    WHERE status IN ('claimed','publishing','needs_review')
       OR (status = 'scheduled' AND scheduled_at < now() + interval '60 minutes')"
ro "SELECT pid, application_name, state, now() - xact_start AS age
    FROM pg_stat_activity WHERE datname = 'ai_smm' AND xact_start IS NOT NULL"
```

Stop if: revision ≠ `79731eadacd7`; new tables already exist; any row
`claimed`/`publishing`; a publication due within 60 minutes (wait, or
choose another window); a transaction on `ai_smm` older than a minute; the
nightly backup window.

### Step 4 — Pre-migration dump

> **NOT EXECUTED — REQUIRES OWNER APPROVAL**

```bash
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
DUMP=/root/backups/ai-smm/ai_smm-pre-smm024b3-$STAMP.dump
docker exec "$PGC" pg_dump -U "$PGU" -d ai_smm -Fc > "$DUMP"
chmod 600 "$DUMP"
sha256sum "$DUMP" | tee "$DUMP.sha256"
echo "$STAMP" > "$EV/dump-stamp.txt"
```

`pg_dump` takes only ACCESS SHARE locks; the worker keeps running.

### Step 5 — Verify the dump and prove it restores

> **NOT EXECUTED — REQUIRES OWNER APPROVAL** (creates and drops a scratch database on the shared instance)

```bash
docker exec -i "$PGC" pg_restore --list < "$DUMP" | grep -c 'TABLE DATA public'
docker exec -i "$PGC" pg_restore --list < "$DUMP" | grep -E 'TABLE DATA public (publications|publication_attempts|alembic_version)'

docker exec "$PGC" psql -U "$PGU" -d postgres -c "CREATE DATABASE ai_smm_restore_check OWNER ai_smm"
docker exec -i "$PGC" pg_restore -U "$PGU" -d ai_smm_restore_check --no-owner --exit-on-error < "$DUMP"
docker exec "$PGC" psql -U "$PGU" -d ai_smm_restore_check -tA \
  -c "SELECT version_num FROM alembic_version" \
  -c "SELECT status, count(*) FROM publications GROUP BY 1 ORDER BY 1" | tee "$EV/restore-check.txt"
docker exec "$PGC" psql -U "$PGU" -d postgres -c "DROP DATABASE ai_smm_restore_check WITH (FORCE)"
```

Stop if: `pg_restore` errors; the restored revision is not
`79731eadacd7`; the status counts differ from `publications-before.txt`
(other than by publications the worker sent in between — check the
worker log).

This proves the dump is **restorable**. It does not make restoring it over
the live database a safe rollback — see
[rollback §4.4](control-center-rollback.md#44-restore-the-pre-migration-dump--last-resort).

### Step 6 — Migration

> **NOT EXECUTED — REQUIRES OWNER APPROVAL** (schema change on the production database)

```bash
cd "$CC"
./cc-compose.sh --profile cc-migrate run --rm -T migrate current </dev/null    # 79731eadacd7
./cc-compose.sh --profile cc-migrate run --rm -T migrate upgrade 3d4ac1986f07 </dev/null \
  2>&1 | tee "$EV/migrate.log"
```

If it fails with `lock timeout` or any other error: nothing was applied
(one transaction). Confirm with `migrate current` = `79731eadacd7` and go
to [rollback §4.1](control-center-rollback.md#41-the-upgrade-failed).
Retry at most once, after re-running step 3's checks.

### Step 7 — Schema after

> **NOT EXECUTED — REQUIRES OWNER APPROVAL** (read-only queries)

```bash
./cc-compose.sh --profile cc-migrate run --rm -T migrate current </dev/null       # 3d4ac1986f07 (head)
ro "SELECT string_agg(t, ',') FROM unnest(ARRAY['users','user_sessions','project_memberships',
    'project_settings','content_items','content_revisions','content_approvals',
    'content_publication_links']) t WHERE to_regclass('public.' || t) IS NOT NULL"   # all eight
ro "SELECT column_name FROM information_schema.columns WHERE table_name = 'projects' ORDER BY 1"
ro "SELECT status, count(*) FROM publications GROUP BY 1 ORDER BY 1" | diff - "$EV/publications-before.txt"
docker inspect ai-smm-worker --format '{{.State.Health.Status}}'                  # healthy
docker logs --since 10m ai-smm-worker 2>&1 | tail -n 20                           # cycles continue, no errors
```

Stop if: revision ≠ head; a table missing; publication counts changed
other than by the worker's own scheduled work; the worker unhealthy.

### Step 7b — Bootstrap the owner

> **NOT EXECUTED — REQUIRES OWNER APPROVAL** (writes one user, one membership, one audit row)

```bash
./cc-compose.sh --profile cc-admin run --rm cli create-owner \
    --email <owner-email> --display-name "<name>" --project <project-id>
# prompts twice for the password (getpass); never pass it as an argument
```

`cli` is pinned to `ai-smm user`: it cannot schedule, approve, import or
publish.

### Step 8 — Start API and web only

> **NOT EXECUTED — REQUIRES OWNER APPROVAL**

```bash
./cc-compose.sh up -d --wait
./cc-compose.sh ps
```

`up` starts `smm-api` and `smm-web` only. It creates `ai-smm-cc-internal`
and `ai-smm-cc-edge`, and attaches `smm-api` to `<shared-network>` without
changing that network.

### Step 9 — Health and exposure checks

> **NOT EXECUTED — REQUIRES OWNER APPROVAL** (read-only)

```bash
curl -fsS http://127.0.0.1:8088/health/live; echo
curl -fsS http://127.0.0.1:8088/health/ready; echo
ss -ltnH 'sport = :8088'                                  # only 127.0.0.1:8088
docker ps --filter label=com.docker.compose.project=ai-smm-control-center \
    --format '{{.Names}}\t{{.Ports}}' | tee "$EV/cc-ps.txt"
for s in smm-api smm-web; do
  docker inspect "$(./cc-compose.sh ps -q $s)" \
    --format "$s {{range \$k, \$v := .NetworkSettings.Networks}}{{\$k}} {{end}}"
done
docker network inspect <shared-network> --format '{{range .Containers}}{{.Name}} {{end}}' | tr ' ' '\n' | grep control-center
./cc-compose.sh logs --since 10m
```

Expected: one published port, `127.0.0.1:8088->8080/tcp` on `smm-web`;
`smm-api` shows `8000/tcp` unpublished; `smm-web` on `ai-smm-cc-edge
ai-smm-cc-internal` only; `smm-api` on `ai-smm-cc-internal
<shared-network>`. From the laptop, without the tunnel,
`curl -m 5 http://<vps-public-address>:8088/` must fail.

Stop if: any `0.0.0.0` or `[::]` binding; `smm-web` on the shared network;
readiness 503; port reachable from outside → `./cc-compose.sh down`
immediately.

### Step 10 — SSH tunnel

> **NOT EXECUTED — REQUIRES OWNER APPROVAL**

```bash
# On the laptop; use another local port if 8088 is busy (-L 18088:127.0.0.1:8088)
ssh -N -L 8088:127.0.0.1:8088 <vps>
```

### Step 11 — Login, logout and read-only smoke test

> **NOT EXECUTED — REQUIRES OWNER APPROVAL** (one login/logout pair: session and audit rows)

```bash
# On the laptop, through the tunnel; the password is read from a file
uv run python scripts/runtime_smoke.py --base-url http://127.0.0.1:8088 \
    --email <owner-email> --password-file ./owner.pw --project <project-id>
shred -u ./owner.pw
```

Expected: every check PASS, including docs disabled, `Secure=False`,
`mutations_enabled=False`, and the cancel probe of publication `0`
refused with `MUTATIONS_DISABLED`. The probe uses an id that never exists:
it is refused before any lookup.

Then in a browser at `http://127.0.0.1:8088/`: sign in; the read-only
notice is shown; the dashboard shows the worker as **UNKNOWN** with the
warning that scheduled publications may be sent; projects, publications,
attempts and audit are readable; no write control is offered; sign out.

### Step 12 — The worker is unchanged

> **NOT EXECUTED — REQUIRES OWNER APPROVAL** (read-only)

```bash
docker inspect ai-smm-worker --format \
  '{{.Id}} {{.Image}} {{.Config.Image}} {{.State.StartedAt}} restarts={{.RestartCount}} {{.State.Health.Status}}' \
  | diff - "$EV/worker-before.txt" && echo "worker identical"
docker compose ls -a | diff - "$EV/compose-ls-before.txt"        # only the new project added
ro "SELECT application_name, count(*) FROM pg_stat_activity WHERE datname = 'ai_smm' GROUP BY 1"
docker logs --since 30m ai-smm-worker 2>&1 | tail -n 20
```

Done when: the worker line is identical (same container id, image,
start time, restart count, healthy), the worker keeps cycling, and the
Control Center is reachable only through the tunnel.

---

## 6. Never, on this host

- run `docker compose` for the Control Center without `cc-compose.sh`, or
  from `/root/ai-smm`;
- put a `.env` in `/root/ai-smm-control-center` or copy `ai-smm.env` there;
- `docker network rm <shared-network>`, `docker system prune`,
  `docker compose down` in `/root/ai-smm` as part of this deployment;
- `migrate upgrade head`, or any migration while a publication is due;
- restore a dump over the live `ai_smm` database while the worker runs;
- add a Traefik label or a port other than `127.0.0.1:8088`;
- enable mutations by editing the environment (it is a compose-file
  change, and a separate, approved decision).

---

## 7. Local validation evidence (2026-10-11)

Baseline `50f2f11`, branch `feat/smm-024b3-production-packaging`, Docker
29.7.2, Compose v5.4.0, PostgreSQL 16.15 (test container).

| Check | Result |
|---|---|
| `tests/test_control_center_packaging.py` — YAML contract, rendered `docker compose config` with fixture env files, hostile shell/`.env`/`env_file` cases, wrapper refusals | 49 passed |
| `tests/test_runtime_packaging.py` (local runtime, unchanged) | passed |
| `tests/test_migrations.py`, incl. lock-timeout rollback | 15 passed |
| Full backend suite (`pytest`, live PostgreSQL) | 771 passed |
| `ruff check .` | clean |
| `scripts/check_worker_schema_compat.sh 6c54669` | 232 passed, 1 skipped (pilot data file absent from the archive) |
| `scripts/control_center_rehearsal.sh 50f2f1136058` — baseline `79731eadacd7` with a published row; dump + test restore; explicit migration; owner bootstrap; `up`; ports/networks; `runtime_smoke.py` 37/37 with login and read-only probe; worker stand-in unchanged; `down` leaves shared network, database, worker | passed |
| Images: no `.env*` anywhere in either filesystem; API runs as 10001, web as 101 | verified |
| Web: `tsc --noEmit`, `eslint`, `vite build` (Node 24.21.0) | clean |
| Web: `vitest run` ×10 | 8/10 green; 2 runs failed on `auth.test.tsx › loads me, then the CSRF token…` — the known pre-existing flake FU-024B2-1, no web code changed here |

---

## 8. Residual risks

| Risk | Mitigation / status |
|---|---|
| `smm-api` is on `<shared-network>`: it has outbound internet access, and every container on that network can reach `smm-api:8000` directly, bypassing nginx | API still requires a session and CSRF; no credentials in the API; unroutable Threads URL; no Traefik route. Removing it needs the database on a network of its own — a change to another project's stack, out of scope |
| The API uses the database owner role `ai_smm` | Application-level gate only; FU-024B3-1 (least-privilege role) |
| A bare `docker compose` bypassing the wrapper | `name:` in the file, separate directory with no `.env`, runbook rule; the wrapper is the only documented entry point |
| Worker publishes LIVE; the UI cannot confirm the mode | UI shows UNKNOWN with a warning; FU-024B2-2 |
| Cookie without `Secure` | Only transport is the SSH tunnel to loopback; revisit when HTTPS terminates in front |
| `docker load` disk usage on a 25 GB host | step 0 `df` check; remove superseded tags after a successful rollout |
| Migration lock contention | `lock_timeout`, single transaction, quiet-window checks |
