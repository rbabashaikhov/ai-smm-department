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
| One-shot tools | `migrate` (profile `cc-migrate`), `cli` (profile `cc-admin`, `ai-smm user` only); reachable only as the wrapper's `migrate` and `create-owner` verbs |
| Not in this project | worker, scheduler, PostgreSQL, any volume or bind mount |
| Images | `ai-smm-api:<full commit>`, `ai-smm-web:<full commit>`, built locally, loaded with `docker load`; never built or pulled on the host (`pull_policy: never`); revision label checked for equality |
| Docker daemon | pinned in `control-center.target` (socket + engine id) and checked before every command |
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

Everything lives in `/root/ai-smm-control-center/`:

| File | Source in Git | Holds | Mode |
|---|---|---|---|
| `compose.control-center.yml` | copied unchanged | services, literals | 644 |
| `cc-compose.sh` | `deploy/control-center/cc-compose.sh` | the only way to run Compose here | 755 |
| `cc-db-backup.sh` | `deploy/control-center/cc-db-backup.sh` | pre-migration dump and restore check | 755 |
| `cc-common.sh` | `deploy/control-center/cc-common.sh` | shared checks, sourced by both | 644 |
| `control-center.compose.env` | `deploy/control-center/control-center.compose.env.example` | **exactly** `SMM_CC_VERSION` (the full 40-hex commit) and `SMM_CC_SHARED_NETWORK` — not secret | 644 |
| `control-center.env` | `deploy/control-center/control-center.env.example` | **exactly** `AI_SMM_DATABASE_URL` — secret | **600** |
| `control-center.target` | `deploy/control-center/control-center.target.example` | **exactly** `SMM_CC_DOCKER_HOST` (a `unix://` socket) and `SMM_CC_DOCKER_ENGINE_ID` — host-specific | 644 |

The filled files never enter Git (`.gitignore`: `*.env`,
`control-center.target`) or an image (`.dockerignore`: `deploy/`, `.env*`;
the web context is `web/` only).

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

The Docker CLI has the same kind of hidden input: without `DOCKER_HOST`
it talks to the daemon of the *current context* stored in
`~/.docker/config.json`. On the workstation used for SMM-024B.3 that
context was Docker Desktop while `/var/run/docker.sock` was a different
engine — nothing on the command line showed which one a command reached.

`deploy/control-center/cc-compose.sh` is therefore **not a pass-through**.
It accepts a fixed set of verbs and builds the entire Compose command
line itself:

| Verb | Compose command it runs (after the fixed prefix) |
|---|---|
| `config [--quiet\|--json\|--services\|--profiles] [--with-tools]` | `config …`; anything but `--quiet` adds `--no-env-resolution`, so the database URL is never printed |
| `up` | `up --detach --wait --wait-timeout 180 --no-build --pull never smm-api smm-web` |
| `down [--remove-orphans]` | `down [--remove-orphans]` — never `-v` or `--rmi` |
| `ps [-a] [-q] [--services] [service…]` | `ps …`, services of this project only |
| `logs [--since N{s,m,h}] [--tail N] [--timestamps] [smm-api\|smm-web…]` | `logs --no-color …` |
| `restart smm-api\|smm-web…` | `restart …` |
| `migrate current\|heads` | `--profile cc-migrate run --rm --no-deps -T migrate current\|heads` |
| `migrate upgrade\|downgrade <12-hex revision>` | `… migrate upgrade\|downgrade <revision>` — never `head`, `base`, `+N`, `-N`, ranges, `--sql`, `stamp` |
| `create-owner --email E --display-name N --project P` | `--profile cc-admin run --rm --no-deps [-T] cli create-owner --email=E --display-name=N --project=P`; values validated, `-T` only without a terminal |

The fixed prefix is `docker compose --project-name ai-smm-control-center
--project-directory <dir> --file <dir>/compose.control-center.yml
--env-file <dir>/control-center.compose.env`, run under `env -i` with only
`PATH`, `HOME`, `TERM` and the pinned `DOCKER_HOST`.

Anything else is refused with exit 2 **before Docker is called**: a
global option in any position (`-p`, `--project-name`, `-f`,
`--project-directory`, `--env-file`, `--profile`, `--context`, `-H`), a
raw Compose verb (`run`, `exec`, `start`, `rm`, `pull`, `build`, `cp`, …),
`-e`/`--env`, `--entrypoint`, `-v`, `--user`, a service outside this
project, an unvalidated value. `tests/test_control_center_wrapper.py`
holds ~90 such attempts.

Before running, the wrapper also:

- refuses a directory that holds `.env`, `compose.yaml`, `compose.yml`,
  `docker-compose.y[a]ml` or `ai-smm.env`, and any of its files that is a
  symlink;
- refuses each env file unless it defines exactly its keys
  (`COMPOSE_PROJECT_NAME` and the like are impossible), as plain
  `KEY=value` lines; `control-center.env` must be `600`;
- refuses an `SMM_CC_VERSION` that is not the full 40-hex commit;
- **pins the daemon**: `SMM_CC_DOCKER_HOST` must be a `unix://` socket
  that exists; a shell with `DOCKER_CONTEXT` set, or `DOCKER_HOST` set to
  anything else, is refused rather than silently overridden; every
  docker call runs with `DOCKER_HOST` set explicitly (which takes
  precedence over any context), and every verb except `config` first
  checks that `docker info` at that socket reports
  `SMM_CC_DOCKER_ENGINE_ID`;
- for `up`, `restart`, `migrate` and `create-owner`: requires both
  `ai-smm-api:<commit>` and `ai-smm-web:<commit>` on that daemon, with
  `org.opencontainers.image.revision` **equal** to the full commit (a
  prefix, a longer string, another case or a missing label is refused)
  and the daemon's architecture.

It performs only reads itself. `cc-db-backup.sh` uses the same directory
checks and the same pinned daemon (§5, steps 4–5).

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

V=$(git rev-parse HEAD)                               # the FULL 40-hex commit
B=$(mktemp -d) && git archive "$V" | tar -x -C "$B"

docker build --target api \
    --label org.opencontainers.image.revision="$V" -t "ai-smm-api:$V" "$B"
docker build \
    --label org.opencontainers.image.revision="$V" -t "ai-smm-web:$V" "$B/web"

# The migration chain this image carries; must print 3d4ac1986f07 (head).
docker run --rm --network none --entrypoint /app/.venv/bin/alembic \
    -w /app "ai-smm-api:$V" heads

# Validation (section 7 lists what each covers)
uv run pytest tests/test_control_center_*.py tests/test_runtime_packaging.py
AI_SMM_TEST_DATABASE_URL=... uv run pytest
AI_SMM_TEST_DATABASE_URL=... scripts/check_worker_schema_compat.sh 6c54669
scripts/control_center_rehearsal.sh "$V"             # full runbook, disposable stand-ins

# Transfer artefacts and one manifest of their checksums
docker save "ai-smm-api:$V" "ai-smm-web:$V" | gzip > "ai-smm-cc-$V.tar.gz"
docker image inspect "ai-smm-api:$V" --format '{{.Architecture}}'   # amd64
S=$(mktemp -d)
cp "$B/compose.control-center.yml" "$B"/deploy/control-center/cc-*.sh "ai-smm-cc-$V.tar.gz" "$S/"
(cd "$S" && sha256sum * > "MANIFEST-$V.sha256")
```

Record `$V`, the manifest and the test results in the change record. The
files copied to the host come from the same `git archive` as the images.

Images are pinned by the full commit as their tag plus a build-time OCI
label that the wrapper compares for equality. They are not pinned by
image ID in the compose file: after `docker save`/`docker load` the ID
depends on the receiving daemon's image store (classic vs containerd), so
it is not knowable at build time. A root user on the host could still
retag another image and add the same label; that is outside what any
file on the host can prevent (§8).

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
BK=/root/backups/ai-smm                      # existing, mode 700
PGC='<postgres-container>'
V='<full 40-hex commit>'
# Every docker command of this session -- including the ones the runbook
# runs directly -- talks to this daemon, whatever context ~/.docker
# selects. cc-*.sh accept this value and refuse any other.
unset DOCKER_CONTEXT
export DOCKER_HOST=unix:///var/run/docker.sock
# A failing command piped into tee must not look like a success.
set -o pipefail
```

### Step 0 — Stage the artefacts (no runtime effect)

> **NOT EXECUTED — REQUIRES OWNER APPROVAL**

```bash
# From the laptop (S = the directory with the manifest, section 3)
ssh <vps> 'install -d -m 700 /root/ai-smm-control-center /root/ai-smm-control-center/evidence'
scp "$S"/* <vps>:/root/ai-smm-control-center/

# On the host
cd "$CC"
sha256sum -c "MANIFEST-$V.sha256"            # compose file, cc-*.sh, image tarball
chmod 644 compose.control-center.yml cc-common.sh
chmod 755 cc-compose.sh cc-db-backup.sh

# The daemon: the host's own engine, on the socket pinned above
docker context show; docker info --format '{{.Name}} {{.ID}} {{.Architecture}}'
printf 'SMM_CC_DOCKER_HOST=%s\nSMM_CC_DOCKER_ENGINE_ID=%s\n' \
    "$DOCKER_HOST" "$(docker info --format '{{.ID}}')" > control-center.target

df -h /var/lib/docker                        # ≥ 2 GB free before loading
gunzip -c "ai-smm-cc-$V.tar.gz" | docker load
for i in ai-smm-api ai-smm-web; do
  docker image inspect "$i:$V" --format \
    '{{.RepoTags}} {{.Id}} {{.Architecture}} {{index .Config.Labels "org.opencontainers.image.revision"}}'
done | tee "$EV/images.txt"
docker run --rm --network none --entrypoint /app/.venv/bin/alembic -w /app "ai-smm-api:$V" heads

printf 'SMM_CC_VERSION=%s\nSMM_CC_SHARED_NETWORK=%s\n' "$V" '<shared-network>' > control-center.compose.env
( umask 077 && install -m 600 /dev/null control-center.env )
# Edit control-center.env by hand: one line, AI_SMM_DATABASE_URL=..., the
# same DSN the worker uses (read it from /root/ai-smm/ai-smm.env without
# printing it). Do not copy any other line.
ls -l "$CC"
./cc-compose.sh config --quiet && echo config-ok
./cc-compose.sh ps                           # checks the engine id; lists nothing yet
```

Stop if: a manifest line fails; `docker info` names a host or engine you
did not expect; a label is not exactly `$V`; architecture ≠ host's
`uname -m`; `alembic heads` ≠ `3d4ac1986f07 (head)`; the wrapper refuses.

`docker load` only adds two images; it does not touch any container.

### Step 1 — Baseline

> **NOT EXECUTED — REQUIRES OWNER APPROVAL**

```bash
date -u; uname -m
echo "DOCKER_HOST=$DOCKER_HOST DOCKER_CONTEXT=${DOCKER_CONTEXT:-<unset>}"
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
cd "$CC"
./cc-db-backup.sh dump --container "$PGC" --out-dir "$BK" | tee "$EV/dump.txt"
DUMP=$(sed -n 's/^dump: *//p' "$EV/dump.txt"); test -s "$DUMP" && echo "$DUMP"
```

`pg_dump` takes only ACCESS SHARE locks; the worker keeps running. The
script only reads `ai_smm`, and stops — exit 2, nothing kept — when:

| Failure | What is left |
|---|---|
| the daemon is not the pinned engine; the container is not running; `ai_smm` does not exist | nothing was run |
| `$BK` is readable by group/others, or has less free space than twice the database size | nothing was run |
| `pg_dump` exits non-zero (connection lost, disk full, killed) | the `.partial` file is removed |
| the output is empty, lacks the custom-format header, or its table of contents does not list `alembic_version` and `publications` | the `.partial` file is removed |
| Ctrl-C / SIGTERM | the `.partial` file is removed |

Only a dump that passed all of that is renamed to
`ai_smm-pre-control-center-<UTC stamp>.dump` (600) with its `.sha256`. An
existing file is never overwritten. Stop on any failure: no migration
without a verified dump.

### Step 5 — Verify the dump and prove it restores

> **NOT EXECUTED — REQUIRES OWNER APPROVAL** (creates and drops a scratch database on the shared instance)

```bash
./cc-db-backup.sh verify --container "$PGC" --dump "$DUMP" --expect-revision 79731eadacd7 \
  | tee "$EV/restore-check.txt"
grep -q '^restore check passed' "$EV/restore-check.txt" && echo VERIFIED || echo 'STOP: no verified dump'
```

The script restores into a **new** scratch database
`ai_smm_restore_check_<UTC YYYYmmddHHMMSS>` — the only name it ever
creates, restores into or drops, checked against
`^ai_smm_restore_check_[0-9]{14}$` before each statement — then prints
its revision and publication counts and drops it.

| Failure | Exit | What is left |
|---|---|---|
| `$DUMP.sha256` missing, or the checksum does not match (archive corrupted or replaced) | 2 | nothing was created |
| not a readable custom-format archive of the queue | 2 | nothing was created |
| an `ai_smm_restore_check_*` database already exists | 2 | nothing was created; the existing one is **not** dropped — it is not this run's. Review it and drop it by hand |
| `CREATE DATABASE` fails | 2 | nothing to drop |
| `pg_restore` fails or is interrupted (one transaction, `--exit-on-error`), or Ctrl-C / SIGTERM | 2 | the scratch database is dropped |
| the restored revision is not `79731eadacd7` | 2 | the scratch database is dropped |
| dropping the scratch database fails | **3** | the scratch database; the script prints its name and the `DROP DATABASE` to run by hand |

In every case `ai_smm` was only read. Stop on any non-zero exit. Also
stop if the status counts differ from `publications-before.txt` other
than by publications the worker sent in between (check the worker log).

This proves the dump is **restorable**. It does not make restoring it over
the live database a safe rollback — see
[rollback §4.4](control-center-rollback.md#44-restore-the-pre-migration-dump--last-resort).

### Step 6 — Migration

> **NOT EXECUTED — REQUIRES OWNER APPROVAL** (schema change on the production database)

```bash
cd "$CC"
./cc-compose.sh migrate current </dev/null                  # 79731eadacd7
./cc-compose.sh migrate upgrade 3d4ac1986f07 </dev/null 2>&1 | tee "$EV/migrate.log"
```

If it fails with `lock timeout` or any other error: nothing was applied
(one transaction). Confirm with `migrate current` = `79731eadacd7` and go
to [rollback §4.1](control-center-rollback.md#41-the-upgrade-failed).
Retry at most once, after re-running step 3's checks.

### Step 7 — Schema after

> **NOT EXECUTED — REQUIRES OWNER APPROVAL** (read-only queries)

```bash
./cc-compose.sh migrate current </dev/null                  # 3d4ac1986f07 (head)
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
./cc-compose.sh create-owner \
    --email <owner-email> --display-name "<name>" --project <project-id>
# prompts twice for the password (getpass); never pass it as an argument
```

`cli` is pinned to `ai-smm user`, and the wrapper offers only
`create-owner` through it: it cannot schedule, approve, import or
publish.

### Step 8 — Start API and web only

> **NOT EXECUTED — REQUIRES OWNER APPROVAL**

```bash
./cc-compose.sh up
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
  from `/root/ai-smm` — including when the wrapper refuses something:
  fix what it names instead;
- run the scripts with `DOCKER_CONTEXT` set or `DOCKER_HOST` pointing at
  another daemon (they refuse), or edit `control-center.target` to make a
  refusal go away;
- put a `.env` in `/root/ai-smm-control-center` or copy `ai-smm.env` there;
- `docker network rm <shared-network>`, `docker system prune`,
  `docker compose down` in `/root/ai-smm` as part of this deployment;
- `migrate upgrade head` (the wrapper refuses it), or any migration while
  a publication is due, or without a dump that passed `cc-db-backup.sh
  verify`;
- drop an `ai_smm_restore_check_*` database you did not just see the
  script create, or anything else on the shared instance;
- restore a dump over the live `ai_smm` database while the worker runs;
- add a Traefik label or a port other than `127.0.0.1:8088`;
- enable mutations by editing the environment (it is a compose-file
  change, and a separate, approved decision).

---

## 7. Local validation evidence (2026-10-11)

Baseline `50f2f11`, branch `feat/smm-024b3-production-packaging`, Docker
29.7.2, Compose v5.4.0, PostgreSQL 16.15 (test container). The rehearsal
ran against an explicitly pinned daemon (Docker Desktop's socket and
engine id), not whatever the CLI context happened to be.

| Check | Result |
|---|---|
| `tests/test_control_center_packaging.py` — YAML contract, rendered `config` through the wrapper (no database URL printed), hostile shell/`.env`/`env_file` cases | 42 passed |
| `tests/test_control_center_wrapper.py` — exact Compose argv of every verb; ~90 bypass attempts refused before Docker is called; directory/file rules for two verbs; daemon pinning (context, foreign `DOCKER_HOST`, wrong engine, unreachable daemon); revision label equality (prefixes, longer, uppercase, missing, wrong arch, missing image) for every image-creating verb; every invocation in both runbooks accepted | 220 passed |
| `tests/test_control_center_backup.py` — pg_dump failing or dying half-way, empty or non-archive output, unreadable TOC, no disk space, open directory, wrong engine; checksum mismatch, missing checksum, restore failure, existing scratch database, failed CREATE, failed DROP (exit 3), wrong revision; `ai_smm` only ever read | 26 passed |
| `tests/test_runtime_packaging.py` (local runtime, unchanged) | passed |
| `tests/test_migrations.py`, incl. lock-timeout rollback | 15 passed |
| Full backend suite (`pytest`, live PostgreSQL) | 1010 passed |
| `ruff check .` | clean |
| `scripts/check_worker_schema_compat.sh 6c54669` | 232 passed, 1 skipped (pilot data file absent from the archive) |
| `scripts/control_center_rehearsal.sh 50f2f113…` — host layout with `cc-*.sh`; baseline `79731eadacd7` with a published row; `cc-db-backup.sh` dump + verify; live failure cases (checksum mismatch, half-truncated archive, tail-truncated archive whose restore fails and whose scratch DB is dropped, foreign scratch DB kept, open backup dir) with `ai_smm` unchanged; explicit migration; owner bootstrap; `up`; ports/networks; `runtime_smoke.py` 37/37; live bypass attempts (`run`, `-p`, `down -v`, `upgrade head`, `exec`, foreign `DOCKER_HOST`, `DOCKER_CONTEXT`, wrong engine id) refused; worker stand-in unchanged; `down` leaves shared network, database, worker | passed |
| Images: no `.env*` anywhere in either filesystem; API runs as 10001, web as 101 | verified |
| Web: `tsc --noEmit`, `eslint`, `vite build` (Node 24.21.0) | clean |
| Web: `vitest run` ×10 | 8/10 green; 2 runs failed on `auth.test.tsx › loads me, then the CSRF token…` — the known pre-existing flake FU-024B2-1, no web code changed here |

---

## 8. Residual risks

| Risk | Mitigation / status |
|---|---|
| `smm-api` is on `<shared-network>`: it has outbound internet access, and every container on that network can reach `smm-api:8000` directly, bypassing nginx | API still requires a session and CSRF; no credentials in the API; unroutable Threads URL; no Traefik route. Removing it needs the database on a network of its own — a change to another project's stack, out of scope |
| The API uses the database owner role `ai_smm` | Application-level gate only; FU-024B3-1 (least-privilege role) |
| A bare `docker compose` bypassing the wrapper | `name:` in the file, separate directory with no `.env`, runbook rule; the wrapper is the only documented entry point and `tests/test_control_center_wrapper.py` fails if a runbook calls Compose directly |
| Root on the host can still do anything: retag another image and give it the same label, edit the wrapper or `control-center.target`, run `docker` by hand | Out of reach of any file on the host. The manifest check in step 0 and `diff` against the reviewed commit (rollback §6) detect a changed copy; the label check catches the accidental cases (wrong tag, wrong build, prefix collision) |
| The wrapper reads `docker` from `PATH` and the Docker CLI's config (plugins, proxies) from `HOME` | Operator's own root shell; `DOCKER_HOST` is set explicitly, so no context in that config can redirect the daemon |
| Worker publishes LIVE; the UI cannot confirm the mode | UI shows UNKNOWN with a warning; FU-024B2-2 |
| Cookie without `Secure` | Only transport is the SSH tunnel to loopback; revisit when HTTPS terminates in front |
| `docker load` disk usage on a 25 GB host | step 0 `df` check; remove superseded tags after a successful rollout |
| Migration lock contention | `lock_timeout`, single transaction, quiet-window checks |
