# ADR — Automation Modes and Publication Safety

| | |
|---|---|
| Status | **Approved** architectural decision, 2026-10-10 |
| Project | PRJ-013 AI SMM Department 2.0 |
| Baseline | `main` at `46dbe854d969f1ed1c5aeb7ae5edea027c82db9d` |
| Implemented by | SMM-024B.2 (read-only control plane, worker-mode reporting) |

## Context: verified deployment finding

The SMM-024B.1 read-only discovery reported that the production worker
runs with `AI_SMM_DRY_RUN=false`, holds live Threads credentials, and
published the third post at 2026-10-10 06:00 UTC. Historical documents
stating that the worker runs in dry-run mode are stale.

This is the state reported by that discovery, not a snapshot re-checked
independently since.

Two consequences shaped this decision:

- the API's own `AI_SMM_DRY_RUN` gates nothing the API does — an admin
  could schedule a publication and the live worker would send it;
- the Control Center showed the API's `AI_SMM_DRY_RUN` as if it were the
  worker's mode ("the worker sends nothing to Threads"), which was false.

## Decision

### Automation modes

The architecture anticipates three modes:

| Mode | Meaning | Status |
|---|---|---|
| `manual` | The content pipeline runs on demand. Human approval is required before publication. | supported |
| `semi_auto` | Content generation and preparation may be scheduled. Human approval is required before publication. | supported (design) |
| `full_auto` | — | **reserved only** |

`full_auto` must not be activatable in the current release and must not
lead to any bypass of human approval. No working `full_auto` execution
path exists or may be added in SMM-024B.2.

Project-specific modes should eventually let independent blogs, authors,
brand policies, schedules and knowledge bases coexist. That is design
intent, not current behaviour.

Three things are kept separate, in both supported modes:

1. **Generation scheduling** — when content is drafted or prepared. May
   be automated in `semi_auto`.
2. **Human approval** — a person approves one exact revision. Never
   automated.
3. **Publication scheduling** — when an approved snapshot is sent. Sending
   an already approved item automatically at its approved time is allowed.

### Production invariant

**No publication without explicit human approval.**

Automatically sending an already approved item at the approved time is
allowed. `human_reviewed` must never be set or inferred by a model, an
agent or a background job.

### Control plane safety

The initial deployment of the new Control Center is **read-only, even for
owner and admin users**.

`AI_SMM_DRY_RUN` on the API is only a process-local configuration and
report field. It does not gate API commands and does not reflect the live
worker. The API's dry-run state must not be displayed as the worker mode.

Setting: `AI_SMM_API_MUTATIONS_ENABLED`, default **false** for the
packaged / production Control Center.

When it is disabled:

- every data-mutating API route is rejected with a stable, documented
  error, and nothing is written to the database;
- the guard is central (FastAPI) and repeated at the service boundary for
  the high-risk queue transitions: schedule, reschedule, cancel;
- hiding buttons in React is not relied on;
- owner and admin credentials provably cannot bypass the gate.

Login, logout and session maintenance need database writes. They get an
explicit, narrow, tested exemption. There is no broad exemption for
administrative bootstrap or for project or publication changes; the
initial owner is created with the operator CLI, under separate
authorisation.

**Worker mode in the UI:** `LIVE` is shown only from a reliable worker
source. Without a verified worker state, the UI shows `UNKNOWN`; it never
infers the worker's mode from the API's environment.

### Deployment boundaries

- The existing worker remains untouched.
- The Control Center is a separate Compose project: the web origin is
  bound to `127.0.0.1:8088` and reached over an SSH tunnel, the API
  publishes no host port, and no reverse-proxy route is added.
- Production database migrations need separate approval, a backup and a
  compatibility review.
- SMM-024B.2 makes no VPS modification, no push or merge, and takes no
  publication action.

### Postponed

- `full_auto` activation;
- a per-project publication-policy schema;
- automatic approval;
- worker code changes;
- injecting credentials for agent generation;
- any deployment.

## Implementation (SMM-024B.2)

| Requirement | Where |
|---|---|
| `AI_SMM_API_MUTATIONS_ENABLED`, default false | `src/ai_smm/config.py`; set explicitly in `compose.runtime.yml` |
| Central deny-by-default gate, 403 `MUTATIONS_DISABLED` | `src/ai_smm/api/mutation_gate.py`, a dependency of the whole `/api/v1` router |
| Narrow session exemption | `POST /api/v1/auth/login`, `POST /api/v1/auth/logout` only |
| Service-boundary guard | `schedule_publication`, `reschedule_publication`, `cancel_publication`, `materialize_approved_revision` take a required `mutations_enabled` argument (`src/ai_smm/application/control_plane.py`) |
| API dry-run separated from worker mode | `/operations/summary` reports `api_dry_run`, `worker_mode`, `worker_mode_source`; `worker_mode` is `unknown` because no verifiable worker source exists yet |
| UI | no write control on a read-only deployment, a read-only notice, a worker-mode panel that shows `UNKNOWN` |
| Proof | `tests/test_api_mutation_gate.py`, `tests/test_api_auth_read_only.py`, `web/src/tests/readOnly.test.tsx` |

No database migration was needed. The worker, its Compose file and its
configuration are unchanged; the worker never reads
`AI_SMM_API_MUTATIONS_ENABLED`, so the switch can neither stop nor start
publishing.
