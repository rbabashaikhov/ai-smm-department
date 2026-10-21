# Generation worker — production packaging

SMM-Generation-A1. How the generation worker (`ai-smm-generation-worker`,
Agent → **Draft**) is packaged for the production host as a compose project
of its own, `ai-smm-generation`, next to the live publish worker (project
`ai-smm`) and the Control Center (project `ai-smm-control-center`).

> **Status: packaging only, NOT deployed.** Nothing here has been run on the
> host. Every step below that touches it — creating the database role,
> writing the env file, starting the worker — needs its own owner approval,
> as does the OpenAI key. Local rehearsal:
> [runtime-packaging.md §8](runtime-packaging.md#8-generation-worker-opt-in-smm-033b)
> (the same worker and role, FakeGraph).

## What it is

| | |
|---|---|
| File | `compose.generation.yml`, project `ai-smm-generation` |
| Image | `ai-smm:<release>`, the publish worker's own image; entrypoint `ai-smm-generation-worker`; `pull_policy: never` |
| Database login | `ai_smm_generation`: EXECUTE on `generation_claim/complete/fail` only — no table, sequence or credential access, so no approvals, no publications. The worker refuses to start with more (exit 2) |
| Env files | `generation.env` (600): exactly `AI_SMM_DATABASE_URL`, `OPENAI_API_KEY`; `generation.compose.env`: `SMM_GEN_VERSION`, `SMM_GEN_SHARED_NETWORK`, `SMM_GEN_KNOWLEDGE_PATH` |
| Not present | Threads, storage, tracing credentials (literal empty), credential-key mount, media mount, Docker socket, host ports |
| Knowledge | host `/srv/ai-smm/knowledge`, mounted read-only; a project needs `projects/<id>/` there |
| Limits | one instance (`container_name`), one job at a time, 512 MiB, 0.5 CPU, 64 pids, lease 600 s, ≤ 3 revisions. **Retries are per job**: `max_attempts` is stored on each job and may be 1–10 (the API and CLI default to 2); it is not a service setting |
| Lifecycle | `restart: "no"`: started by hand, stopped by hand, not after a reboot |
| Entry point | **only** `gen-compose.sh` (below) |

It never approves, materialises or publishes: publishing stays the publish
worker's alone, behind a human approval of the exact revision.

The application has **no spend cap**: the retry ceiling, `series_size` ≤ 5,
one job at a time and the 30 s model timeout bound a run indirectly, but a
monetary limit exists only if the owner sets one on the OpenAI project.

Network: the shared network is the only one that reaches the database and it
has outbound access (the host's other projects use it). With
`OPENAI_API_KEY` empty nothing can be called. A real key is a separate step.

## The wrapper: `gen-compose.sh`

Like `cc-compose.sh`, it is not a pass-through: fixed verbs
(`config`, `up`, `ps`, `logs`, `check`, `down`), one fixed Compose command
line, a clean environment, no value ever printed, exit 2 before Docker is
called. Before Compose can read anything it refuses:

- a `generation.env` that is not **exactly** `AI_SMM_DATABASE_URL` and
  `OPENAI_API_KEY`, each once, plain `KEY=value`, mode 600 — so
  `OPENAI_BASE_URL`, `OPENAI_ORG_ID`, `OPENAI_PROJECT`, `THREADS_*`, unknown
  keys, duplicates, `export`, quotes, `$` and multi-line values are all
  refused; the URL must name the login `ai_smm_generation`;
- a `generation.compose.env` with other keys than its three, or a tag
  `latest`;
- a directory holding `.env`, `compose.yaml`/`compose.override.*`,
  `ai-smm.env` or `control-center.env`, a symlinked file, a `DOCKER_CONTEXT`
  or a foreign `DOCKER_HOST`.

`up` refuses further, **fail closed**:

1. unless `OPENAI_API_KEY` is **non-empty**. Only presence is checked: the
   wrapper makes no external request, so it does **not** verify that OpenAI
   accepts the key. A production E2E with a key needs the owner's separate,
   explicit approval to run with that named key and a limited budget (the
   limit is set on the OpenAI project; the application has no spend cap).
   With an empty key the worker would
   claim real jobs and fail them, and **stopping the worker does not give
   consumed attempts or failed jobs back**. Empty-key and FakeGraph tests run
   on a disposable database only, never against production;
2. unless the job's facts are piped in (read-only query below) and show a
   **queued** job with `attempt_count 0`, `series_size 1`, strategy
   `standalone_series`, `max_attempts` **1 or 2**, and **no other** queued or
   claimed job — the initial authorised E2E is exactly one such job. The
   facts are parsed strictly: only top-level `params` keys count (nested
   values do not), a repeated JSON key or a missing, mistyped or out-of-range
   field refuses. **This is an operator attestation, not a database guarantee:** the wrapper checks the text it is given, it does not read
   PostgreSQL, and the worker takes
   whatever job is due when it starts. (`max_attempts` can
   be up to 10, the worker takes any due job, and the `ai-smm generation
   status` CLI does not print `max_attempts`, hence the query.)

## Sequence (each step needs approval)

```bash
# 0. Read-only: the release image is on the host, the knowledge base is there
docker image inspect ai-smm:<release> --format '{{index .Config.Labels "org.opencontainers.image.revision"}}'
ls /srv/ai-smm/knowledge/projects/

# 1. Database role (superuser, in a window; one transaction, re-runnable)
#    deploy/control-center/db/ai_smm_generation.grants.sql, then:
#    \password ai_smm_generation        -- interactively, never on a command line
#    deploy/control-center/db/ai_smm_generation.verify.sql   -- 0 FAIL

# 2. Files, in a directory of its own (never /root/ai-smm or the CC's)
install -d -m 700 /root/ai-smm-generation
#    compose.generation.yml, gen-compose.sh (755), cc-common.sh (the wrapper sources it),
#    generation.compose.env (from the example), generation.env (600, written by
#    the owner, with a dedicated OpenAI key; any monetary limit is configured on the OpenAI project, not here)

# 3. Queue exactly one job (operator path), verify it, then start with it
docker compose -p ai-smm run --rm cli generation enqueue --project <id> --requested-by <owner-email> --series-size 1
#    (default max_attempts is 2; the CLI prints the job id)
JOB=<job-id>; PGC=<postgres-container>; PGU=<its superuser>
docker exec -i -e PGOPTIONS='-c default_transaction_read_only=on' "$PGC" psql -X -U "$PGU" -d ai_smm -At <<SQL | ./gen-compose.sh up
SELECT format(E'status : %s\\nattempt_count : %s\\nmax_attempts : %s\\nparams : %s\\nopen_jobs : %s',
  j.status, j.attempt_count, j.max_attempts, j.params::text,
  (SELECT count(*) FROM content_generation_jobs WHERE status IN ('queued','claimed')))
FROM content_generation_jobs j WHERE j.id = '$JOB';
SQL
./gen-compose.sh check
./gen-compose.sh logs --since 10m
./gen-compose.sh down
```

A queued job is picked up within one poll (15 s).

## Rollback

`./gen-compose.sh down` removes the container (images, network and database untouched).
To retire the role: `ai_smm_generation.revoke.sql`. Drafts already written
stay and are harmless: they are `draft` and cannot be published without a
human approval. A job that was running returns to the queue when its lease
expires. Attempts already consumed and jobs already failed are **not**
restored by stopping or removing the worker.

## Never

- put this service in `compose.yaml` or the Control Center's project, or run
  it from `/root/ai-smm`;
- give it the owner, `ai_smm_cc`, `ai_smm_auth` or any other login, the
  publish worker's `ai-smm.env`, a Threads or storage secret;
- set a restart policy, scale it, or mount media or the credential keys;
- fill `OPENAI_API_KEY` without the owner's approval of a dedicated key, or start the worker with it empty against production;
- run `docker compose` on this file directly, or add an override file or
  `.env`: only `gen-compose.sh` validates the env files.

## Residual ways around the wrapper

Root on the host can edit the wrapper or `compose.generation.yml`, or run
`docker compose` by hand — no file on the host prevents that (same trust
boundary as `cc-compose.sh`, control-center-deployment.md §2.2). The job check
is an attestation: the wrapper verifies the text it is given, not the
database. The container's own literals (no Threads, storage or tracing
secret) hold even then, and the database login can only call the three
generation contracts.

## Checks

`tests/test_generation_packaging.py` pins all of the above;
`tests/test_generation_local_activation.py` runs the worker as
`ai_smm_generation` with a fake graph and every outbound socket refused.
