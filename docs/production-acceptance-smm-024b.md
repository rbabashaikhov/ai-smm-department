# Production acceptance — SMM-024B (Control Center v1, read-only)

Audience: the project owner and whoever operates or extends the
production deployment. This is the sanitised record of how the private
Control Center went into production on **2026-10-11**, and of what was
and was not verified.

> **Sanitised.** Database URLs, addresses, container and image ids, the
> Docker engine id, checksums of the backup, credentials and private
> paths of the owner's machines are **not** in this document. They are in
> the private deployment record and the step audits, outside Git. Names
> in angle brackets follow [operations.md](operations.md).
>
> Sources: the step reports SMM-024B.4B-2 (staging), B.4B-3 (preflight),
> B.4B-4A (preparation), B.4B-4B (backup) and B.4B-5 (deployment), and
> the local validation in
> [control-center-deployment.md §7](control-center-deployment.md#7-local-validation-evidence-2026-10-11).
> A fact that none of them contains is marked **NOT VERIFIED**.

**Verdict at acceptance: DEPLOYMENT VERIFIED** (SMM-024B.4B-5,
2026-10-11, 19:32 UTC). Business mutations are off; the live worker was
not changed.

---

## 1. Release identity

| | |
|---|---|
| Release commit | `8ccccda161c3a0b4bc3fd4a90d86dfd19dabd665` (`main`, merge of SMM-024B.4A-FIX; tree identical to `ead9d01`) |
| Images | `ai-smm-api:<release commit>`, `ai-smm-web:<release commit>`, built locally from a `git archive` of the release, OCI revision label = full commit, linux/amd64 |
| Transfer | one image archive plus `compose.control-center.yml` and the `cc-*.sh` scripts, covered by one `MANIFEST-<commit>.sha256`; `sha256sum -c` and `gzip -t` passed on the host (B.4B-2) |
| Compose project | `ai-smm-control-center`, run only through `cc-compose.sh` |
| Services | `smm-api` (FastAPI), `smm-web` (nginx + React/Vite build) |
| Database | the existing `ai_smm` database on the shared PostgreSQL 16 instance, existing owner role (FU-024B3-1) |
| Schema | Alembic `79731eadacd7` → **`3d4ac1986f07`** (head) |
| Access | only `127.0.0.1:8088` on the host, through an SSH tunnel; no public route, no reverse-proxy label |
| Business mutations | **off**: `AI_SMM_API_MUTATIONS_ENABLED=false`, a literal in the compose file |
| API docs | off (`AI_SMM_API_DOCS_ENABLED=false`) |

## 2. Timeline (UTC)

| When | Step | Result |
|---|---|---|
| 2026-10-11, morning | B.4B-2 — stage the release on the host | STAGING VERIFIED; nothing loaded into Docker; production baseline unchanged |
| 07:43, 07:59 | B.4B-3 — read-only preflight | GO WITH CONDITIONS; PostgreSQL identity matched the private record; queue: 3 published, nothing open |
| ~08:15 | B.4B-4A — paused by the owner | — |
| 18:40–22:30 | agreed migration window (`control-center.window`) | clear of the nightly backup with ≥ 60 min margin |
| 18:41 | B.4B-4A — preflight, `docker load` | PREPARATION READY; preflight PASS before and after the load; only the two images added |
| 18:48:55 | B.4B-4B — pre-migration dump | taken through all gates |
| 18:49:17 | restore check into a scratch database | passed; scratch database dropped; backup evidence written |
| 18:50:03 | off-host copy checked, acknowledgement written | see §5 |
| 19:10:31–19:10:40 | B.4B-5 — `migrate upgrade 3d4ac1986f07` | applied in one transaction |
| 19:18:21 | owner account created (`create-owner`) | run interactively by the owner; password entered via `getpass` |
| 19:25:19–19:25:32 | `cc-compose.sh up` | `smm-api` and `smm-web` healthy |
| until 19:32 | exposure, smoke and worker checks | §6, §7 |

## 3. Test results

**At release (local, before production).** Not re-run in SMM-024C-1.

| Check | Result | Where recorded |
|---|---|---|
| Full backend suite (`pytest`, live PostgreSQL) | **1509 passed** | [deployment §7.1](control-center-deployment.md#71-deployment-gates-smm-024b4a-fix-2026-10-11) |
| Deployment gates, backup evidence, wrapper, packaging, backup suites | 423 + 20, 57, 220, 42, 25 passed | same |
| `ruff check .` | clean | same |
| `scripts/control_center_rehearsal.sh` (full runbook on disposable stand-ins, incl. gate refusals, migration, smoke 37/37, downgrade, rollback) | passed; repeated on the release images in B.4B-1 | same; B.4B-1 record |
| Deployed worker's own suite on the head schema (`check_worker_schema_compat.sh 6c54669`) | 232 passed, 1 skipped (unrelated data file) | [deployment §4.2](control-center-deployment.md#42-compatibility-with-the-running-worker) |
| Web: `tsc --noEmit`, `eslint`, `vite build` | clean (SMM-024B.3) | [deployment §7](control-center-deployment.md#7-local-validation-evidence-2026-10-11) |
| Web: `vitest run` | flaky, known: FU-024B2-1 | [follow-ups.md](follow-ups.md#fu-024b2-1--flaky-vitest-tests) |

**In production** — §6 and §7.

## 4. Migration

| | |
|---|---|
| Revisions applied | `2964ac9ceea7` (control plane: users, sessions, memberships, project settings, four `projects` columns with defaults), `3d4ac1986f07` (content items, immutable revisions, approvals, content → publication links) |
| Command | `cc-compose.sh migrate upgrade 3d4ac1986f07` — explicit revision, never `head` |
| Gates passed immediately before | PostgreSQL identity, queue (window ± 60 min), backup evidence + off-host acknowledgement ([deployment §2.1](control-center-deployment.md#21-deployment-gates)) |
| Mechanics | one transaction, `lock_timeout=5s`, `statement_timeout=120s` |
| Duration | 9 s (19:10:31–19:10:40) |
| Result | revision `3d4ac1986f07`; existing tables `publications`, `publication_attempts`, `content_series` unchanged (data fingerprints before/after identical) |

## 5. Backup and restore

| | |
|---|---|
| Pre-migration dump | `cc-premigration-ai_smm-<stamp>.dump` (≈ 28 KB) with `.sha256` and `.source`, in the dedicated pre-migration backup directory on the host, outside the nightly 14-day rotation |
| Integrity | sha256 recorded in the private deployment record |
| Restore check | **passed**: restored into `ai_smm_restore_check_<stamp>` in one transaction, revision `79731eadacd7` confirmed, scratch database dropped |
| Backup evidence | written by the successful `verify` only, valid only inside the 2026-10-11 window (now stale by design) |
| Off-host copy | **exists**, checked with `sha256sum -c` on the owner's workstation; acknowledgement written afterwards |
| Off-host encryption | **NOT CONFIRMED** — the B.4B-4B record states the storage holding the copy is not encrypted at rest (runbook step 5b asks for encrypted storage). FU-024C1-3 |
| Nightly backups | unchanged: `backup.sh`, 03:30 Europe/Vilnius, 14 days, no checksum, no off-host copy. FU-024C1-3 |
| Retention of the pre-migration dump | never automatic; deletion is a recorded owner decision **no earlier than 2026-11-10** (30 days after the deployment) and only once a later verified dump exists |

## 6. Authentication, exposure and read-only smoke

All through the SSH tunnel (local port 18088 → host `127.0.0.1:8088`).

| Check | Result |
|---|---|
| Listening sockets | only `127.0.0.1:8088` (on `smm-web`); `smm-api` publishes nothing |
| From outside, without the tunnel | port 8088 on the public address **unreachable** |
| Health | `smm-api` and `smm-web` healthy (container healthchecks); the health probes through the proxy are part of the smoke test below |
| `scripts/runtime_smoke.py`, unauthenticated | **24/24** passed |
| `scripts/runtime_smoke.py`, authenticated (owner) | **37/37** passed — including docs disabled, cookie `Secure=False` (tunnel to loopback), `mutations_enabled=False`, and a cancel probe refused with `MUTATIONS_DISABLED` |
| Read views (GET only): project, settings, operations summary, attention, audit, publications, one publication and its attempts, content items, series | all **200** |
| Password file used by the smoke test | shredded; tunnel closed afterwards |

**What the read-only deployment did write.** Business state was not
changed. Authentication did write, as documented
([api.md, Read-only mode](api.md#read-only-mode)): `audit_log` grew by
exactly **5** rows — `user.create_owner`, two `auth.login`, two
`auth.logout` — plus the owner's `users` / `project_memberships` rows and
the sessions' `user_sessions` rows.

## 7. Worker baseline

| | Before | After |
|---|---|---|
| Worker container (id, image, start time, restart count, health) | recorded in the evidence directory | **identical** |
| Worker mode | LIVE (`AI_SMM_DRY_RUN=false`), re-confirmed read-only by SMM-024B.4A on 2026-10-11 | unchanged — not touched |
| Publications, attempts, series | data fingerprint recorded | **unchanged** |
| Queue | 3 published, nothing scheduled, claimed or awaiting review | unchanged |

The worker was never stopped, restarted, recreated or reconfigured.

**Finding:** in `pg_stat_activity` the API's connection identifies itself
as `ai-smm/worker-1`, the same name as the worker, because
`src/ai_smm/db/session.py` overrides `PGAPPNAME`. It is one connection of
the API, not a second worker. FU-024C1-1.

## 8. Manual UI acceptance

Performed by the owner in a browser through the tunnel after B.4B-5;
the list of screens was confirmed in the SMM-024C-1 review (2026-10-12).

| Screen | Checked |
|---|---|
| Projects | yes |
| Dashboard | yes |
| Publications | yes |
| Preview of publication 3 | yes; UX / localisation remarks (FU-024C1-4) |
| Content | yes |
| Attention | yes |
| Audit | yes; UX remarks (FU-024C1-4) |

Independently of what the UI shows, the server refuses every business
mutation (§6). The remarks are classified P3 and do not affect
the read-only acceptance. Per-screen evidence (screenshots, the exact
strings, whether the dashboard showed the worker as `UNKNOWN` with its
warning) is not part of the deployment records — **NOT VERIFIED**.

## 9. Open limitations

| Limitation | Follow-up |
|---|---|
| The API runs as the database owner role; read-only rests on application code | FU-024B3-1 |
| Worker mode has no verifiable source; the UI shows `UNKNOWN` | FU-024B2-2 |
| API's `application_name` looks like the worker's | FU-024C1-1 |
| Off-host backup not confirmed encrypted; nightly dumps not off the host | FU-024C1-3 |
| Audit / Preview UX and localisation remarks, not yet described | FU-024C1-4 |
| Approval provenance of publication 1 — **NOT VERIFIED**: published on 2026-10-08 from the JSON queue before the PostgreSQL queue existed, imported as `published` with `human_reviewed = false` (the Control Center shows `Human reviewed: no`); no approval record is known | FU-024C1-2 |
| Safety gates not yet verified for an editable production deployment | FU-024C1-5 |
| `smm-api` on the shared Docker network | FU-024B3-2 |
| Session cookie without `Secure` (SSH tunnel only) | FU-024B3-3 |
| Queue gate → DDL gap (mandatory before the next migration while the queue can be written); trust model of the backup evidence | FU-024B4A-1, FU-024B4A-2 |

Priorities (agreed in the SMM-024C-1 review): **P1**, blocking business
mutations — FU-024B3-1 (least-privilege role) and FU-024C1-5 (safety gate
verification); **P2** — worker mode source, `application_name`, backup
encryption, publication 1 provenance, Vitest flakes; **P3** — Audit /
Preview UX and localisation. Full register:
[follow-ups.md, Register](follow-ups.md#register-as-of-2026-10-12-smm-024c-1).

## 10. Rollback strategy

In order of preference
([control-center-rollback.md](control-center-rollback.md)):

1. **Stop the Control Center** — `cc-compose.sh down` (rollback §1).
   Removes its two containers and two networks only; the worker, the
   shared network, the database and the schema stay. This is a complete
   rollback of the Control Center's behaviour.
2. **Go back to an earlier image** — not applicable: this is the first
   production Control Center image (rollback §3).
3. **Leave the schema.** Both migrations are additive and the worker's
   own suite passes on the head schema. `migrate downgrade 79731eadacd7`
   only with a shown reason; it needs a new approved window and passes
   the identity and queue gates (rollback §4.3).
4. **Never restore the pre-migration dump over `ai_smm`** while the
   worker runs: it would rewind the live worker's outcomes and can
   publish a post twice (rollback §4.4).

The worker is never part of a Control Center rollback; stopping
publishing is the worker's own lever ([operations.md §7](operations.md#7-restart-recovery-rollback)).

## 11. Next tasks

| Task | Scope |
|---|---|
| SMM-024C | Post-deployment stabilisation. SMM-024C-1 (this document set) aligns the documentation with production; the P1 follow-ups are the candidates for the next steps |
| SMM-025 | In the agreed roadmap. Detailed specification and acceptance criteria **not yet approved** |
| SMM-031 | In the agreed roadmap. Detailed specification and acceptance criteria **not yet approved** |
| Business mutations | Not scheduled. Needs the P1 follow-ups (FU-024B3-1, FU-024C1-5) closed, a reviewed compose change and a separate owner approval; any migration while the queue can be written also needs FU-024B4A-1 |
