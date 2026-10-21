# Runtime packaging: the Control Center as one origin

This document describes how the private SMM Control Center is packaged as
containers and how to run that package locally against a disposable
database. It covers SMM-024A and the SMM-024B.2 read-only control plane;
nothing here deploys to a server.

The **production** packaging of the same images is a separate file,
`compose.control-center.yml` (SMM-024B.3), described in
[control-center-deployment.md](control-center-deployment.md). It has been
running in production since 2026-10-11, read-only for business
operations ([acceptance record](production-acceptance-smm-024b.md)). It
differs from this runtime where production requires it:

| | `compose.runtime.yml` (local) | `compose.control-center.yml` (production) |
|---|---|---|
| Project | `ai-smm-runtime-local` | `ai-smm-control-center`, run only through `deploy/control-center/cc-compose.sh` |
| Database | disposable `postgres` service | none: the existing instance, over the external shared network |
| Images | built by Compose, tag `local` | prebuilt, `<commit>` tag + OCI revision label, `pull_policy: never`, no `build:` |
| Database URL | literal, local | `control-center.env` (that key only, mode 600) |
| Mutations / docs / cookie | `SMM_RUNTIME_*` switches, default off / off / not Secure | literals `false` / `false` / `false`, not switchable |
| API networks | `backend` (internal) | `cc-internal` (internal) + the shared network |
| Tool profiles | `tools` | `cc-migrate`, `cc-admin`; `cli` pinned to `ai-smm user` |
| Web port | `127.0.0.1:${SMM_RUNTIME_WEB_PORT:-8088}` | `127.0.0.1:8088`, literal |

## 1. Topology

```
browser
  └─ http://127.0.0.1:${SMM_RUNTIME_WEB_PORT:-8088}      (only published port)
       └─ smm-web   nginx, React production build
            ├─ /                         static SPA, index.html fallback
            ├─ /api/v1/auth/login, /logout, /step-up/*  → smm-auth:8000
            ├─ /api, /api/*              ─┐
            ├─ /health, /health/*         ├─ reverse proxy → smm-api:8000
            └─ /docs, /redoc, /openapi.json ─┘
                 └─ smm-api   FastAPI (uvicorn), no host port
                      └─ postgres   disposable, local only
```

The worker is a separate execution plane and is not part of this runtime.
No service in `compose.runtime.yml` can claim or publish a publication.

| Service | Image | Network | Started by `up` |
|---|---|---|---|
| `postgres` | `postgres:16.15-bookworm` (by digest) | `backend` | yes |
| `smm-api` | `ai-smm-api:local` — `Dockerfile`, target `api` | `backend` | yes |
| `smm-auth` | `ai-smm-api:local` — `ai_smm.api.auth_service`, login `ai_smm_auth` | `backend` | yes (needs its role, §5) |
| `smm-web` | `ai-smm-web:local` — `web/Dockerfile` | `edge` + `backend` | yes |
| `migrate` | `ai-smm-api:local`, entrypoint `alembic` | `backend` | no (`tools` profile) |
| `cli` | `ai-smm-api:local`, entrypoint `ai-smm` | `backend` | no (`tools` profile) |

`smm-auth` is the production sign-in application (SMM-025D) run on the
same image: nginx sends login, logout and step-up to it, so the browser
cannot sign in without it. It has no host port and, as in production, its
own PostgreSQL login `ai_smm_auth` — EXECUTE on the `smm_auth` contracts and
column SELECT on `users`, no table, sequence or credential access — next to
the API's `ai_smm_local` and the generation worker's `ai_smm_generation`.
It holds no Threads, OpenAI, storage or credential-key variable.

`backend` is an `internal` Docker network: the API, the sign-in service,
the database and the tools have no route to the internet at all. Only `smm-web` is attached to
`edge`, the network the loopback port is published on.

## 2. Images

### API — `Dockerfile`, target `api`

The existing Python image, not a second stack. The Dockerfile now has a
shared `runtime-base` stage (virtualenv, non-root user 10001, read-only
layout) and two targets on top of it:

```bash
docker build -t ai-smm:<tag> .                   # worker, the default target, unchanged
docker build --target api -t ai-smm-api:<tag> .  # HTTP control plane
```

The worker stays the last stage, so a plain `docker build` produces the
same worker image as before.

The `api` target runs

```
uvicorn ai_smm.api.app:create_app --factory --host 0.0.0.0 --port 8000 --no-access-log --no-server-header
```

and nothing else: no `alembic upgrade`, no worker, no publisher. `0.0.0.0`
is the container's interface on the internal network; no host port is
published. Its healthcheck calls `/health/live` with the standard library.
It starts without `OPENAI_API_KEY`, `THREADS_ACCESS_TOKEN` or storage
credentials, because no endpoint uses them.

### Web — `web/Dockerfile`

Multi-stage:

1. `node:24-alpine` (Node 24.21.0, matching `web/.nvmrc`) runs `npm ci`
   and `npm run build`, with `VITE_API_BASE_URL` forced empty so the bundle
   calls `/api` on its own origin. `web/.dockerignore` keeps any local
   `.env` out of the build context.
2. `nginxinc/nginx-unprivileged` (nginx 1.30.5) serves `/srv/smm-web` as
   uid 101 on port 8080. The final image has no Node runtime.

`nginx -t` runs at build time, so a config error fails the build.

## 3. Reverse proxy and SPA routing

`web/nginx/default.conf` and `web/nginx/smm-proxy.inc`:

| Request | Result |
|---|---|
| `/`, `/projects/demo/content`, any other non-file path | `index.html` (200), `Cache-Control: no-cache`, strict same-origin CSP |
| `/assets/<hash>.js` | the file, `Cache-Control: immutable` |
| `/assets/missing.js` | 404 — never `index.html` |
| `/api`, `/api/...`, `/health`, `/health/...` | proxied to FastAPI unchanged; an unknown path is FastAPI's **404 JSON** |
| `/docs`, `/redoc`, `/openapi.json` | proxied; 404 JSON unless docs are enabled on the API |
| any `/api` or `/health` request while the API is unreachable | 502 JSON `DEPENDENCY_UNAVAILABLE` in the API's error shape |

Other properties:

- `proxy_pass` takes a variable resolved through Docker's DNS per request.
  nginx therefore starts without the API and follows it to a new address
  after a restart, instead of keeping the one it resolved at boot.
- `X-Request-ID` is passed through, or generated by nginx when absent, and
  appears in both logs.
- `autoindex off`, `server_tokens off`, `X-Content-Type-Options`,
  `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`.
- No secrets and no environment substitution in the config.

## 4. Configuration

All configuration is injected at runtime. Nothing secret is in a
Dockerfile, in compose, in Git or in the React bundle. `compose.runtime.yml`
reads no `env_file`.

| Variable | Local runtime value | Notes |
|---|---|---|
| `AI_SMM_DATABASE_URL` | the local `postgres` service (literal) | not interpolated: a `.env` file next to the compose file cannot redirect it |
| `AI_SMM_DRY_RUN` | `true` (literal) | the **API process's** flag, reported as `api_dry_run`; it gates no API command and is not the worker's mode |
| `AI_SMM_API_COOKIE_SECURE` | `${SMM_RUNTIME_COOKIE_SECURE:-false}` | `false` for plain-HTTP localhost; HTTPS deployments use `true`. `smm-auth` sets the cookie, so it reads the same switch |
| `AI_SMM_API_DOCS_ENABLED` | `${SMM_RUNTIME_API_DOCS_ENABLED:-false}` | the code default stays `true` for local development |
| `AI_SMM_API_MUTATIONS_ENABLED` | `${SMM_RUNTIME_API_MUTATIONS_ENABLED:-false}` | read-only Control Center; the code default is `false` too. See §7 |
| `OPENAI_API_KEY`, `THREADS_ACCESS_TOKEN` | empty | defence in depth |
| `THREADS_API_BASE_URL` | `http://127.0.0.1:9` | unroutable, defence in depth |

Compose interpolation variables use the `SMM_RUNTIME_*` prefix, which no
production file uses:

| Variable | Default |
|---|---|
| `SMM_RUNTIME_WEB_PORT` | `8088` (bound to `127.0.0.1` only) |
| `SMM_RUNTIME_TAG` | `local` |
| `SMM_RUNTIME_COOKIE_SECURE` | `false` |
| `SMM_RUNTIME_API_DOCS_ENABLED` | `false` |
| `SMM_RUNTIME_API_MUTATIONS_ENABLED` | `false` — `true` only to edit the disposable local database |

The session cookie is unchanged: `smm_session`, HttpOnly, `SameSite=Lax`,
`Path=/`. The browser talks to one origin, so there is no CORS
configuration anywhere.

## 5. Local run, step by step

The database here is **local and disposable**: compose project
`ai-smm-runtime-local`, database `ai_smm_runtime_local`, user
`ai_smm_local`, volume `ai-smm-runtime-local-pgdata` (labelled
`ai-smm.disposable=true`). Its password is a fixed public value and is not
a secret. It has no host port.

```bash
C="docker compose -f compose.runtime.yml"

# 1. Images
$C build

# 2. Database
$C up -d --wait postgres

# 3. Schema -- an explicit operator command, never run by the API
$C --profile tools run --rm migrate upgrade head
$C --profile tools run --rm migrate current        # expect "(head)"

# 4. The first owner and an empty local project "demo", in one transaction
#    -- prompts for the password twice; there is no default. Since SMM-025B
#    import-json no longer creates projects, so the bootstrap does.
$C --profile tools run --rm cli user create-owner \
    --email owner@local.test --display-name "Local Owner" \
    --project demo --create-project "Demo (local)"

# 5. The sign-in role -- once per database, after the migration. It runs
#    the operator's smm_auth.functions.sql, ai_smm_auth.grants.sql and
#    ai_smm_auth.verify.sql (any FAIL line aborts) on the local database
#    only; it never migrates. smm-auth cannot connect until this ran.
deploy/local-runtime/auth-role.sh

# 6. API, sign-in service and web
$C up -d --wait
open http://127.0.0.1:8088/
```

`auth-role.sh` first runs `generation-owner.sql` (the same idempotent step
`generation-role.sh` takes), because the `smm_auth` contracts, like the
generation ones, are refused when the local superuser owns the tables.
Re-run it after a migration that adds a table. The role's local password
is a fixed public value in `compose.runtime.yml`; it is not a secret, it
exists only for the disposable local database, and it must never be reused
in production (there `ai_smm_auth` gets its own password, set
interactively by the operator).

No owner, password or project is ever created automatically.

### Smoke test

```bash
uv run python scripts/runtime_smoke.py --base-url http://127.0.0.1:8088

# including login, /projects, CSRF and logout; the password is read from
# a file so it is not in argv or the shell history
uv run python scripts/runtime_smoke.py --base-url http://127.0.0.1:8088 \
    --email owner@local.test --password-file ./owner.pw --project demo
```

It checks the SPA and its fallback, that unknown `/api` and `/health`
paths are API 404 JSON, the health probes through the proxy, docs
on/off (`--expect-docs`), request-id propagation, cookie attributes, CSRF
enforcement and session revocation. With a session it also checks the
read-only gate (`--expect-mutations`, default `disabled`): `/auth/me`
reports the mode, and a cancel of publication `0` — an id that never
exists — is refused with `MUTATIONS_DISABLED` (or is a 404 with mutations
on). It never publishes or schedules, and writes nothing beyond its own
login/logout.

### Teardown

```bash
$C down        # keep the local database
$C down -v     # discard it
```

## 6. Automated checks

`tests/test_runtime_packaging.py` reads `compose.runtime.yml`, both
Dockerfiles and the nginx config and pins the safety decisions: no publish
worker service (the generation worker only behind its profile, §8), no
`env_file`, only a loopback port, a literal local database URL,
`AI_SMM_DRY_RUN=true`, tools behind a profile, no egress for API,
sign-in service and database, `smm-auth` on the existing auth application
with its own least-privileged login and no credentials, `auth-role.sh`
local-only and never migrating, an acyclic startup order, the worker still the default build
target, the API target running uvicorn only, a Node-free web image, API
prefixes without SPA fallback, and 404 for missing assets.

`tests/test_control_center_packaging.py` does the same for the production
file and its wrapper, including `docker compose config` renders against
fixture env files.

`tests/test_api_docs_config.py` covers `AI_SMM_API_DOCS_ENABLED`.
`tests/test_runtime_packaging.py` also pins the read-only default of
`AI_SMM_API_MUTATIONS_ENABLED`, and `tests/test_api_mutation_gate.py`
covers the gate itself.

## 7. Read-only Control Center and worker mode

The packaged Control Center is **read-only for business operations**
(SMM-024B.2, [ADR](adr/automation-and-publication-safety.md)):
authentication and session maintenance still write session and audit
rows. With `AI_SMM_API_MUTATIONS_ENABLED=false`:

- every unsafe `/api/v1` request is refused with 403 `MUTATIONS_DISABLED`
  for every role, owner included, and the database is left unchanged;
- login and logout still work: they are the gate's only allowlist, and
  they write a `user_sessions` row (or its revocation),
  `users.last_login_at` and an `auth.*` audit row;
- every authenticated request, `GET` included, moves
  `user_sessions.last_seen_at` (the idle deadline);
- reading — projects, queue, content, attempts, audit — works as before;
- the UI offers no write control and shows a read-only notice. It reads
  the mode from `GET /api/v1/auth/me` (`control_plane.mutations_enabled`);
  the server enforces it regardless.

Details, the allowlist and the order of checks: [api.md, Read-only
mode](api.md#read-only-mode).

**API dry-run is not the worker's mode.** `AI_SMM_DRY_RUN=true` in this
runtime is the API process's own setting. It gates nothing the API does,
and the API runs separately from the worker, so it says nothing about
whether the worker publishes. `/operations/summary` therefore reports it
as `api_dry_run`, and reports the worker as `worker_mode: "unknown"` —
there is not yet a source the API can verify. The dashboard shows
**UNKNOWN** and states that a scheduled publication may be sent. It shows
`LIVE` or `DRY RUN` only if the API reports one with its source.

The switch belongs to the API alone: the worker never reads it, so turning
it on or off neither stops nor starts publishing.

## 8. Generation worker (opt-in, SMM-033B)

`smm-generation` runs `ai-smm-generation-worker`
(`ai_smm.generation_worker:main`) from the API image: it claims queued
generation jobs, runs the agent graph and writes **draft** content items
with their revision. It never approves, materialises or publishes, and
it is not the publish worker — this runtime still has none.

A plain `$C up` does not start it; it sits behind the `generation`
profile, and `restart: "no"` keeps it from coming back after a Docker
restart. One container only (`container_name` refuses `--scale`), and the
worker runs one job at a time.

```bash
# once per database, after `migrate upgrade head`
deploy/local-runtime/generation-role.sh

$C --profile generation up -d --wait smm-generation
$C exec smm-generation ai-smm-generation-worker --check
$C logs -f smm-generation
$C --profile generation stop smm-generation
```

`generation-role.sh` provisions `ai_smm_generation` with the operator's own
`deploy/control-center/db/ai_smm_generation.grants.sql` and runs
`.verify.sql` (any `FAIL` line aborts). Before that,
`deploy/local-runtime/generation-owner.sql` gives the local schema the
production shape: the local bootstrap user is a superuser, and `grants.sql`
refuses superuser-owned contracts, so the public tables, sequences and the
three generation contracts move to a non-superuser `NOLOGIN` owner
`ai_smm_local_owner`. That file refuses any database except
`ai_smm_runtime_local`. Re-run the script after a migration that adds a
table. The role's local password is a fixed public value in
`compose.runtime.yml`; it is not a secret.

What the service has, and does not have:

- **Database**: only the EXECUTE-only `ai_smm_generation` login — the
  three contracts, no table, sequence or credential access, no approvals,
  no publications. `--check` and startup verify this from the catalogs:
  connected as a superuser or as any role with table access, the worker
  refuses to start (exit 2) and the healthcheck fails.
- **Filesystem**: read-only root, `/tmp` tmpfs (heartbeat), the knowledge
  base mounted read-only at `/srv/ai-smm/knowledge`. A job can only
  generate for a project that has `knowledge/projects/<project_id>/`;
  otherwise it fails terminally with `knowledge unavailable`.
- **No credentials**: no Threads, storage, credential-key or Langfuse
  variables, `OPENAI_API_KEY=""`, and only the internal `backend` network.
  **At this stage a job that reaches the real agent graph fails** (no key,
  no route out) within its own `max_attempts` and calls no external API.
  Enabling real generation needs a separate decision: an OpenAI key from a
  secret source and a network with egress.
- **Limits**: 512 MiB, one CPU, 64 pids; lease 600 s (the contract caps
  any lease at 3600 s); the retry ceiling is the job's own
  `max_attempts`; the graph's revision loop is capped by
  `AI_SMM_GENERATION_MAX_REVISIONS=3`.
- **Health**: `ai-smm-generation-worker --check` — readiness (database,
  contracts, least privilege) and liveness (the heartbeat may be at most
  one lease plus `AI_SMM_HEALTH_STALE_FACTOR` poll intervals old, since a
  running job holds the cycle). Its output never contains the database
  URL.

Stopping is safe at any point: SIGTERM lets the current job finish
(`stop_grace_period: 120s`); a kill leaves nothing, because the drafts and
the job's outcome are committed in one transaction at the very end, and
the expired lease returns the job to the queue (or fails it after its
final attempt).

Checks: `tests/test_runtime_packaging.py` pins the service definition;
`tests/test_generation_local_activation.py` runs the packaged path as
`ai_smm_generation` with a fake agent graph and every outbound socket
refused (queued → claimed → succeeded → draft + revision, the editor
verdict, real knowledge loading, retry exhaustion, stop and restart, the
readiness probe).
