# Follow-ups

Open items accepted for later work. Each one names where it came from,
what is wrong or missing, and what "done" means. None of them blocks the
release it came from. Some of them block a **later** step — above all,
switching on business mutations in the production Control Center; the
register below says which.

---

<a id="register-as-of-2026-10-12-smm-024c-1"></a>

## Register

**As of 2026-10-14 (SMM-024C-2B; SMM-025A review added FU-025A-1…4; SMM-025B review added FU-025B-1…9; SMM-025C closed FU-025B-1 locally and added FU-025C-1).** The heading stays the same when the
register is updated, so links to `#register` keep working; the anchor of
its first version (`#register-as-of-2026-10-12-smm-024c-1`) is kept too.

Production state these priorities refer to: Control Center v1 deployed
read-only on 2026-10-11, since 2026-10-13 on release `0cbd641` with the
API connected as the least-privilege role `ai_smm_cc` (schema
`3d4ac1986f07`, mutations off); live worker unchanged, still on the owner
role — see
[production-acceptance-smm-024c2.md](production-acceptance-smm-024c2.md)
and, for the first deployment,
[production-acceptance-smm-024b.md](production-acceptance-smm-024b.md).

Priorities as agreed in the SMM-024C-1 review (2026-10-12):
**P1** — must be closed before business (publication) mutations are
switched on; **P2** — should be closed soon, does not gate mutations;
**P3** — UX, localisation, hygiene or accepted limitation. A
**migration gate** item is not a priority of the read-only UI but a
mandatory condition for a later step, named in its row.

| FU | Title | Status | Priority | Blocks business mutations? |
|---|---|---|---|---|
| [FU-024C1-5](#fu-024c1-5--verify-the-safety-gates-before-publication-mutations-are-switched-on) | Verify the safety gates before publication mutations are switched on | open; its database-role part for the read-only mode is done (FU-024B3-1) | **P1** | **Yes** — this is the check that precedes the switch |
| [FU-024C2-1](#fu-024c2-1--session-forgery-through-the-apis-database-connection) | Session forgery through the API's database connection | open, accepted for read-only mode | **P1** | **Yes** — a forged owner session could schedule a post the LIVE worker sends |
| [FU-024C2-2](#fu-024c2-2--authenticity-of-audit-actor-and-action) | Authenticity of audit actor/action | open, accepted for read-only mode | **P1** | **Yes** — business actions must be attributable |
| [FU-024C2-3](#fu-024c2-3--a-privilege-model-for-business-mutations) | Privilege model for business mutations (SMM-025) | **designed and validated locally** (SMM-025C, `ai_smm_cc_rw`); not in production, not yet part of an executed FU-024C1-5 checklist | **P1** | **Yes** — until the role is created in production and checked with FU-024C1-5 |
| [FU-025C-1](#fu-025c-1--database-side-invariants-for-rows-the-mutation-role-inserts) | Database-side invariants for rows the mutation role inserts | open | **P1** | **Yes** — decide (implement or accept) before the switch |
| [FU-025C-2](#fu-025c-2--packaging-of-the-credential-keys-and-the-legacy-allow-list) | Packaging of the credential keys and the legacy allow-list | open | **P1** | **Yes** — no credential can be saved or used without it |
| [FU-024C2-4](#fu-024c2-4--flush-before-the-savepoint-of-the-optional-hash-upgrade) | Flush before the SAVEPOINT of the optional hash upgrade | open | P2 | No — fail-closed today; diagnostics and robustness |
| [FU-024B4A-1](#fu-024b4a-1--the-gap-between-the-queue-gate-and-the-first-ddl-statement) | Queue gate → DDL gap (TOCTOU) | open | **migration gate** | No for the current read-only UI. **Mandatory before any further migration while the queue can be written** (mutations on, or any other writer active) |
| [FU-024B2-2](#fu-024b2-2--validate-worker_mode_source-when-a-worker-heartbeat-is-introduced) | Source and reliability of the worker mode | open | P2 | No — the UI shows `UNKNOWN` with a warning; still wanted soon, as the worker is LIVE |
| [FU-024C1-1](#fu-024c1-1--the-api-connects-to-postgresql-as-ai-smmworker-1) | API's `application_name` in PostgreSQL | open | P2 | No — observability only |
| [FU-024C1-3](#fu-024c1-3--off-host-backup-encryption-and-off-host-copies-of-nightly-dumps) | Off-host backup encryption; nightly dumps off the host | open; one encrypted, restore-tested off-host copy made by hand (2026-10-12) | P2 | No |
| [FU-024C1-2](#fu-024c1-2--historical-approval-provenance-of-publication-1) | Approval provenance of publication 1 | open, NOT VERIFIED | P2 | No — historical record only |
| [FU-024B2-1](#fu-024b2-1--flaky-vitest-tests) | Flaky Vitest tests | open | P2 | No — but fix before UI work for mutations, so regressions are not masked |
| [FU-024B3-4](#fu-024b3-4--atomic-finalisation-of-dump-and-checksum) | Atomic finalisation of dump and checksum | open | P2 | No (before the next migration preferably) |
| [FU-024B3-2](#fu-024b3-2--take-the-api-off-the-shared-network) | Take the API off the shared network | open | P2 | No (session + CSRF still required) |
| [FU-025A-1](#fu-025a-1--local-state-of-content-detail-survives-a-change-of-item) | Content Detail keeps local state when the item changes | open | P2 | No — the server refuses a revision of another item; fix before the editorial UI is used with mutations on |
| [FU-025A-3](#fu-025a-3--diagnostics-of-a-failed-projects-overview-row) | Diagnostics of a failed Projects Overview row | open | P3 | No |
| [FU-025A-4](#fu-025a-4--the-initial-route-uses-a-snapshot-of-meprojects) | Initial route uses a snapshot of `me.projects` | open | P3 | No |
| [FU-025A-2](#fu-025a-2--module-global-navigateref-in-the-test-renderer) | Module-global `navigateRef` in the test renderer | open | P3 | No — test hygiene |
| [FU-025B-1](#fu-025b-1--the-mutation-privilege-model-must-cover-the-smm-025b-tables) | Mutation privilege model must cover the SMM-025B tables | **done technically** (SMM-025C, local; production with FU-024C2-3) | — | Via FU-024C2-3 only |
| [FU-025B-2](#fu-025b-2--a-gated-wrapper-verb-for-creating-a-project-in-production) | Gated wrapper verb for creating a project in production | open | P2 | No |
| [FU-025B-3](#fu-025b-3--generation-does-not-read-topics-and-tone-of-voice-yet) | Generation does not read topics / tone of voice yet | open | P2 | No |
| [FU-025B-4](#fu-025b-4--credential-key-rotation-without-re-entering-tokens) | Credential key rotation without re-entering tokens | open | P3 | No |
| [FU-025B-5](#fu-025b-5--egress-policy-for-the-apis-connection-test) | Egress policy for the API's connection test | open | P3 | No |
| [FU-025B-6](#fu-025b-6--rate-limits-per-account) | Rate limits per account | open | P3 | No |
| [FU-025B-7](#fu-025b-7--configuration-changed-under-a-running-worker) | Configuration changed under a running worker | open, accepted | P3 | No |
| [FU-025B-8](#fu-025b-8--alert-on-a-worker-that-refuses-to-start) | Alert on a worker that refuses to start (exit 78) | open | P3 | No |
| [FU-025B-9](#fu-025b-9--image-paths-of-legacy-rows-are-not-project-scoped) | Image paths of legacy rows are not project-scoped | open, accepted | P3 | No |
| [FU-024C1-4](#fu-024c1-4--ux-and-localisation-defects-in-audit-and-preview) | UX and localisation defects in Audit / Preview | open, needs triage | P3 | No |
| [FU-024B3-3](#fu-024b3-3--secure-session-cookie) | `Secure` session cookie | open, depends on HTTPS | P3 | No (SSH tunnel only) |
| [FU-024B4A-2](#fu-024b4a-2--the-trust-model-of-backup-evidence-and-the-off-host-acknowledgement) | Trust model of backup evidence / off-host ack | accepted limitation | P3 | No |
| [FU-024B2-3](#fu-024b2-3--documentation-read-only-means-business-operations-authentication-still-writes-session-state) | Read-only wording: auth still writes | docs and `web/README.md` aligned in SMM-024C-1 | P3 | No |
| [FU-024B3-1](#fu-024b3-1--a-least-privilege-database-role-for-the-api) | Least-privilege PostgreSQL role for the API | **done** for the read-only mode (SMM-024C-2A design; production 2026-10-13, SMM-024C-2B) | — | No longer — the role for an editable API is FU-024C2-3 |
| [FU-024B3-5](#fu-024b3-5--verify-the-postgresql-identity-before-the-production-run) | PostgreSQL identity before the production run | **done** (SMM-024B.4A-FIX; used in production 2026-10-11) | — | — |

Switching on business mutations is in any case a reviewed change to
`compose.control-center.yml` (the value is a literal there), a new task
and a separate owner approval — none of the items above authorises it.

---

## From the SMM-024B.2 code review

The architectural review of SMM-024B.2 (read-only control plane) ended in
**ACCEPT WITH FOLLOW-UPS** on 2026-10-11. The three follow-ups below were
recorded at that point.

### FU-024B2-1 — Flaky Vitest tests

**Status:** open. **Pre-existing:** yes. The tests are already flaky on
`main` at `46dbe85`, before SMM-024B.2, so the flakiness was not
introduced by it.

Evidence (20 consecutive `vitest run` on Node 24):

| Test | `main` | SMM-024B.2 | Failure |
|---|---|---|---|
| `src/tests/auth.test.tsx` › loads me, then the CSRF token, then the protected page | 5/20 | 4/20 | `expected -1 to be greater than 1`: the call order is asserted as soon as the "Проекты" heading appears, before `GET /api/v1/projects` has been recorded |
| `src/tests/content.test.tsx` › content list › shows the columns and passes filters and pagination to the API | 1/20 | 1/20 | `findByRole` gives up after the default 1000 ms under load |
| `src/tests/publications.test.tsx` › publication list › shows the delivery columns and passes filters to the API | 0/20 | 1/20 | same cause as above |

Proposed fix:

- assert the request order inside `waitFor`, once `GET /api/v1/projects`
  has been recorded, instead of right after the heading renders;
- raise the Testing Library async timeout centrally
  (`configure({ asyncUtilTimeout })` in `src/test/setup.ts`) rather than
  per test.

Done when: 50 consecutive `npx vitest run` pass, with no test weakened
(no removed assertions, no `retry`).

Re-observed during SMM-024B.3 (2026-10-11, Node 24.21.0, no web code
changed): 2 of 10 consecutive runs failed, both on the `auth.test.tsx`
call-order assertion.

### FU-024B2-2 — Validate `worker_mode_source` when a worker heartbeat is introduced

**Status:** open. **Depends on:** a worker change, which the ADR
postpones and which needs separate approval.

Today `observed_worker_mode()` always returns `UNKNOWN` with no source,
and `worker_mode_source` in `/operations/summary` is an unconstrained
`str | None`. That is safe only because nothing ever sets it. When the
worker starts reporting its mode, the API must not let that field become
a weaker kind of truth than the rule it replaces.

Requirements for that change:

- **Invariant:** `worker_mode` is `live` or `dry_run` **only** with a
  non-null `worker_mode_source`; `unknown` **only** with `null`.
  Enforce it in the schema, not only by convention.
- **Constrained source:** `worker_mode_source` is a closed set of
  identifiers (an enum such as `worker_heartbeat`), not free text. It never
  carries a hostname, a container id, a path, a DSN or any other topology
  or secret.
- **Freshness:** a report older than an explicit threshold (for example,
  derived from the poll interval and `AI_SMM_HEALTH_STALE_FACTOR`) is
  `unknown`, not the last mode seen. So is a report without a timestamp.
- **Attribution:** the report names the worker (`worker_id`). With
  several workers, or several reporting different modes, the answer is
  `unknown` unless the rule for combining them is decided and tested.
- **No inference:** still never derived from the API's `AI_SMM_DRY_RUN`,
  nor from past attempt outcomes.
- **Tests:** fresh / stale / missing / conflicting reports, the invariant
  above, and that the UI shows `LIVE` / `DRY RUN` with the source and
  `UNKNOWN` otherwise.

**Production note (SMM-024C-1, 2026-10-12).** This item also covers the
question "where does the worker mode come from, and how reliable is it".
In production today the only source is a human reading the worker's
container configuration: LIVE (`AI_SMM_DRY_RUN=false`) was reported by the
SMM-024B.1 discovery (2026-10-10), re-confirmed read-only by the
SMM-024B.4A gate (2026-10-11), and the worker container stayed identical
through the SMM-024B.4B-5 deployment. Nothing records it continuously, so
the deployed Control Center correctly reports `unknown`. The misleading
`application_name` of the API (FU-024C1-1) must be fixed first or with
this change: a heartbeat attributed by `worker_id` must not be confusable
with the API's own connection, which today also identifies itself as
`worker-1`. **Priority P2; does not block business mutations** (the UI
keeps showing `UNKNOWN` with its warning).

### FU-024B2-3 — Documentation: read-only means business operations; authentication still writes session state

**Status:** documentation aligned in SMM-024C-1 (2026-10-12, pending
review): `README.md`, `docs/runtime-packaging.md` §7, the ADR,
`docs/operations.md` and `docs/production-deployment-plan.md` now say
"read-only for business operations" and name the session and audit
writes. The UI notice in `web/src/layouts/AppLayout.tsx` says the
Control Center "does not accept changes", which is about business
changes and claims no "writes nothing" — left as is. `web/README.md`
(documentation only) was brought into scope by the SMM-024C-1 review and
now states the production status and the same session/audit writes.
Production confirmed the table below: the
deployment's owner bootstrap, two logins and two logouts added exactly
five `audit_log` rows (SMM-024B.4B-5).

Several places say the read-only Control Center "only reads" or "writes
nothing". That is true of **business state** — content, approvals,
project settings, the publication queue — and not of authentication.
With `AI_SMM_API_MUTATIONS_ENABLED=false`, the API still writes:

| Trigger | Writes |
|---|---|
| `POST /api/v1/auth/login` (success) | a new `user_sessions` row; `users.last_login_at` and `updated_at`; an `auth.login` audit row |
| `POST /api/v1/auth/login` (failure) | an `auth.login_failed` audit row |
| `POST /api/v1/auth/logout` | `user_sessions.revoked_at`; an `auth.logout` audit row |
| any authenticated request, `GET` included | `user_sessions.last_seen_at` (the idle deadline) |

`docs/api.md` (Read-only mode) already lists these. The wording elsewhere
should use the same precise terms: "read-only for business operations;
authentication and session maintenance still write session and audit
rows". Places to align:

- `README.md` (HTTP API — control plane: "По умолчанию API только
  читает");
- `docs/runtime-packaging.md` §7;
- `docs/adr/automation-and-publication-safety.md` (Control plane safety);
- `docs/operations.md` (the Control Center note);
- `docs/production-deployment-plan.md` §17.3;
- the UI notice in `web/src/layouts/AppLayout.tsx`, if its wording is
  changed too.

Done when: no document or UI text claims that the read-only Control Center
writes nothing, and each of them names the session and audit writes, or
links to the table in `docs/api.md`.

---

## From SMM-024B.3 (production packaging)

Recorded on 2026-10-11 while preparing `compose.control-center.yml`. None
blocks the read-only deployment; each changes what a later step may do.

### FU-024B3-1 — A least-privilege database role for the API

**Status: done for the read-only mode.** Designed and validated locally
in SMM-024C-2A (2026-10-12):
[least-privilege-role.md](least-privilege-role.md). Rolled out in
SMM-024C-2B: role and grants (A1) and credential (A2) on 2026-10-12, the
switch of `smm-api` (A3) on 2026-10-13. In production the read-only smoke
test passed (37/37) with the restricted role, and a direct
`UPDATE publications` from the API's connection — like nine other
business writes and a `CREATE TABLE` — is refused by PostgreSQL with
42501; record:
[production-acceptance-smm-024c2.md](production-acceptance-smm-024c2.md).
The privileges an editable API needs are FU-024C2-3.

The read-only grants below were refined: `users` and `user_sessions` writes are column-level, the
`user_sessions` insert includes `revoked_at` (the ORM always sends it),
and — after the SMM-024C-2A security review — **no** `UPDATE` on
`users.password_hash`: the optional Argon2 upgrade at login now runs in a
SAVEPOINT and is skipped for the role, which changes `auth.py` and
therefore needs rebuilt images. Accepted residual risks (session creation
for any user, unauthenticated audit actor/action) are listed in
[least-privilege-role.md §4.1–4.2](least-privilege-role.md#41-sessions-what-is-and-is-not-guaranteed).

Before 2026-10-13 the API connected as `ai_smm`, the database owner,
the same role the worker and the migrations use. The read-only guarantee
therefore rested on the application (central mutation gate plus
service-level checks), not on privileges: a code path that escaped both
could change anything.

Proposed: a role `ai_smm_cc` with `SELECT` on the queue and editorial
tables and only the writes authentication needs (`INSERT`/`UPDATE` on
`user_sessions`, `UPDATE (last_login_at, updated_at)` on `users`,
`INSERT` on `audit_log`); `migrate` and `cli` keep the owner role through
a second env file. Done when the read-only smoke test passes with the
restricted role and a direct `UPDATE publications` from the API's
connection is refused by PostgreSQL.

**Production note (SMM-024C-1, 2026-10-12; historical).** From
2026-10-11 to 2026-10-13 the deployed API connected as the owner role.
With mutations on, the role needs more than the read-only grants above
(content tables, the queue columns the three commands move), so the
design must be redone for the editable control plane — still without
DDL, ownership or access to `alembic_version`. That part is now
FU-024C2-3 (P1, blocks business mutations). Verifying the role together
with the other safety gates is FU-024C1-5.

### FU-024B3-2 — Take the API off the shared network

**Status:** open. **Needs:** a change to another project's stack.

`smm-api` joins `<shared-network>` because the database is only reachable
there. That gives it outbound internet access and makes `smm-api:8000`
reachable from every container on that network, bypassing nginx (the API
still requires a session and CSRF). Done when the database is reachable
from a network that holds only the database and the Control Center, and
`smm-api` is detached from `<shared-network>`.

### FU-024B3-3 — `Secure` session cookie

**Status:** open. **Depends on:** HTTPS in front of the Control Center.

The production compose sets `AI_SMM_API_COOKIE_SECURE=false`, because the
only transport is an SSH tunnel to plain-HTTP loopback. If the Control
Center is ever served over HTTPS (still private), switch it to `true` in
the same change and run the smoke test with `--expect-secure-cookie`.


### FU-024B3-4 — Atomic finalisation of dump and checksum

**Status:** open. **Found in:** review of the SMM-024B.3 fixes.

`cc-db-backup.sh dump` renames `<name>.dump.partial` to `<name>.dump` and
only then writes `<name>.dump.sha256`. A crash or kill between the two
leaves a final-looking dump without a checksum. `verify` refuses it
(".sha256 is missing"), so it cannot be trusted by mistake, but the dump
is then unusable until re-taken. Done when the checksum is written first
(to a `.partial`) and both files are published so that a final `.dump`
never exists without a valid `.sha256` (for example checksum computed on
the `.partial`, `.sha256` renamed first, then `.dump`, with `verify`
treating a lone `.sha256` as incomplete), with a test that kills the
script between the steps.

### FU-024B3-5 — Verify the PostgreSQL identity before the production run

**Status:** done in SMM-024B.4A-FIX: `deploy/control-center/cc-gate.sh`
(identity gate against `control-center.identity`, run inside
`cc-db-backup.sh` and before every schema change; `--container` must be
the recorded container), see
[control-center-deployment.md §2.1](control-center-deployment.md#21-deployment-gates).
**Found in:** review of the SMM-024B.3 fixes.

`cc-db-backup.sh` checks the Docker engine and that the named container
is running, but not that the container is the intended PostgreSQL
instance: a mistyped `--container` naming another database container on
the shared host would be dumped and restored into, and the scratch
database created there. Only `ai_smm` is read and only a uniquely named
scratch database is written, but it would be the wrong instance. Done
when the deployment step records the expected container id (or image
and `ai_smm` system identifier) alongside the engine id, the script
refuses a mismatch, and the runbook captures it in the evidence
directory in step 3.

### FU-024B4A-1 — The gap between the queue gate and the first DDL statement

**Status:** open. **Found in:** review v2 of SMM-024B.4A-FIX.

`cc-compose.sh migrate upgrade|downgrade` runs the queue gate and then
starts Compose; the DDL begins seconds later, in another process and
another database session. A publication that becomes claimable in that
gap (inserted by the operator CLI, or by anything else with the
superuser role) is not seen by the gate. It is narrow — the gate already
protects the whole window ± 60 minutes, the Control Center cannot
schedule (mutations off), the migration is one transaction with
`lock_timeout` — but it is a time-of-check/time-of-use gap. Done when the
check moves into the migration's own transaction (for example an Alembic
pre-step that takes `LOCK TABLE publications IN SHARE MODE`, re-runs the
same queue query and aborts the transaction on any row), with a test that
inserts a due publication between the gate and the DDL.

**Note (SMM-024C-1).** One of the mitigations above — "the Control Center
cannot schedule" — holds only while mutations are off. This item is
therefore a **mandatory condition before any further production
migration while the queue can be written** (mutations on, or another
writer active). It does not block the current read-only UI. The
2026-10-11 migration ran with mutations off and an empty queue.

### FU-024B4A-2 — The trust model of backup evidence and the off-host acknowledgement

**Status:** open (accepted limitation). **Found in:** review v2 of SMM-024B.4A-FIX.

`control-center.backup-evidence` is self-checksummed but not signed, and
`control-center.offhost-ack` is an operator's attestation: root on the
host can write either, and nothing on the host can prove that a copy of
the dump left it or where its sha256 was computed
([control-center-deployment.md §2.2](control-center-deployment.md#22-trust-boundary)).
The gates stop mistakes — a missing, stale, corrupted or mismatched
backup — not a root operator who decides to bypass them. Done when the
off-host side produces something the host can check but not forge: for
example the receiving machine signs `<sha256> <dump name> <time>` with a
key the host does not hold, and `migrate upgrade` verifies that
signature against a public key recorded in the deployment record; or the
copy goes to storage the host can read back but not write.

---

## From the SMM-024B.4B production deployment (recorded in SMM-024C-1)

Recorded on 2026-10-12 while aligning the documentation with the
2026-10-11 deployment
([production-acceptance-smm-024b.md](production-acceptance-smm-024b.md)).
None of them affected the read-only deployment. Facts the available
deployment records do not contain are marked **NOT VERIFIED**.

### FU-024C1-1 — The API connects to PostgreSQL as `ai-smm/worker-1`

**Status:** open. **Priority:** P2. **Blocks mutations:** no.
**Found in:** SMM-024B.4B-5 post-deployment checks (2026-10-11).

`compose.control-center.yml` sets `PGAPPNAME=ai-smm-control-center-api`
(and `-cli`, `-migrate`) so that the Control Center's connections are
recognisable in `pg_stat_activity`. For `smm-api` and `cli` this has no
effect: `src/ai_smm/db/session.py` passes
`application_name=f"ai-smm/{settings.worker_id}"` in `connect_args`, which
libpq prefers over `PGAPPNAME`, and `AI_SMM_WORKER_ID` defaults to
`worker-1`. In production the API's connection therefore looks exactly
like the live worker's. `migrate` is not affected: `alembic/env.py`
builds its own engine without `application_name`.

Consequences:

- an operator reading `pg_stat_activity` sees two "workers" and could
  terminate the wrong backend during an incident;
- the runbook queries that filter on `application_name LIKE
  'ai-smm-control-center%'` (deployment step 12, rollback §4.1) miss the
  API's sessions — the documents now say so;
- a future worker heartbeat attributed by `worker_id` (FU-024B2-2) could
  be confused with the API.

**Production note (SMM-024C-2B, 2026-10-13).** Since the API connects as
`ai_smm_cc`, `usename` tells its connections apart from the worker's
(`ai_smm`) in `pg_stat_activity`; `application_name` is still
`ai-smm/worker-1`. Queries that must find the API's sessions should
filter on `usename = 'ai_smm_cc'` until this item is fixed.

Done when the API and the operator CLI identify themselves distinctly
(for example: `connect_args` omits `application_name` when `PGAPPNAME` is
set, or the API factory passes its own name), a test asserts the
`application_name` each entry point sends, and the two runbook queries
match the API again. A worker image change is **not** needed if the fix
keeps the worker's current name; the worker is not to be redeployed for
this item alone.

### FU-024C1-2 — Historical approval provenance of publication 1

**Status:** open. **Priority:** P2. **Blocks mutations:** no.

Publication 1 (the carousel posted on 2026-10-08) was published from the
local JSON queue by `scripts/publish_saved.py` (whose live path requires
`--confirm-reviewed`), before the PostgreSQL queue existed. It entered the database later
through `ai-smm import-json` as `published`, with its `threads_post_id`.
The importer sets `human_reviewed` once, from the document-level
`approval` block of the JSON file: `approved is True and not
human_review_required` (`src/ai_smm/importer.py`). The API returns the
stored column unchanged (`src/ai_smm/application/publications.py`) and
the publication detail page renders it as `yes` / `no`.

What the available records show about publication 1:

| Evidence | Value |
|---|---|
| Control Center, publication detail, owner's read-only browser check after SMM-024B.4B-5 | `Human reviewed: no` |
| Local copy of the JSON queue (last modified 2026-10-08): `approval.approved = true`, `approval.human_review_required = true`; no per-publication review field | the importer yields `human_reviewed = false` |
| [production-deployment-plan.md §16](production-deployment-plan.md#16-stop-point-2026-10-09-исторический) (2026-10-09, after the import) | `human_reviewed=false` for every record |
| §15.6 (2026-10-09) | publication 1 has `attempts=0` |

So publication 1 is stored as **`published` with `human_reviewed =
false`**: it was published outside the PostgreSQL queue, and the database
has never recorded a review of it. That the file imported on the host was
identical to the local copy is **NOT VERIFIED**, and the current
production value was not re-read in SMM-024C-1 (no production access);
the Control Center observation above is the latest evidence. It has no
`content_publication_links` row (that table was created on 2026-10-11 and
cannot be written with mutations off).

Who approved the text before the 2026-10-08 post, and how, is **NOT
VERIFIED**: the only trace is `publish_saved.py` requiring
`--confirm-reviewed` for a live run, which is not stored anywhere. Unlike
publication 2, whose approval is an `audit_log` row
([production-deployment-plan.md §15.4](production-deployment-plan.md#154-что-осталось-до-первой-реальной-публикации)),
no approval record for publication 1 is known.

Done when the owner has decided how the gap is represented to readers —
for example a note in the operator documentation, or a UI hint that
`human_reviewed = no` on a row that arrived already `published` means
"published before the queue existed", not "published without review".
Not acceptable: setting `human_reviewed` now, rewriting the historical
row, or an audit entry that presents itself as the 2026-10-08 approval.
Any new record must carry its real date and actor and say that it is a
later annotation.

### FU-024C1-3 — Off-host backup encryption, and off-host copies of nightly dumps

**Status:** open. **Priority:** P2. **Blocks mutations:** no.

**Progress (SMM-024C-2B, 2026-10-12).** Before the role rollout the
nightly dump of 2026-10-12 was restore-tested in a disposable,
network-less PostgreSQL container (all foreign keys without orphans,
Alembic `3d4ac1986f07`, data fingerprints equal to production), and its
off-host copy was replaced by a symmetrically encrypted archive (GnuPG,
AES-256) whose decryption was checked against the recorded sha256; the
plaintext copy was securely deleted. This was done **by hand, once**: it
is not yet a scheduled procedure, and the points below remain open.

- The pre-migration dump of 2026-10-11 has a verified off-host copy on
  the owner's workstation (owner-only permissions). Runbook step 5b asks
  for an **encrypted** location; the SMM-024B.4B-4B record states that the
  storage holding the copy is **not encrypted at rest**. Encryption of the
  off-host copy is therefore **NOT CONFIRMED**. The dump holds publication
  texts and the audit log.
- The nightly `backup.sh` dumps have no `.sha256` and no off-host copy at
  all (SMM-024B.4B-3); the host disk remains a single point of failure
  for everything after 2026-10-11.

Done when the off-host copy lives on encrypted storage (or is replaced by
an encrypted archive whose sha256 is re-checked against the deployment
record) and the unencrypted copy is securely deleted; the nightly dumps
get a checksum and a scheduled encrypted off-host copy with a documented,
tested restore. Once mutations are on, Control Center tables also hold
password and session hashes, editorial history and approvals — data with
no other copy.

### FU-024C1-4 — UX and localisation defects in Audit and Preview

**Status:** open, needs triage. **Priority:** P3. **Blocks mutations:** no.
**Found in:** the owner's browser acceptance after SMM-024B.4B-5 (Audit,
and the Preview of publication 3).

The owner reported UX defects on the Audit and Preview screens, and
localisation issues, during the manual acceptance. The available
deployment records do not describe them: the exact screens, strings,
steps, expected and actual behaviour are **NOT VERIFIED** and must be
written down here first.

One documented behaviour that may be part of it: the project audit view
lists only entries whose subject belongs to the project
([api.md §6, Audit](api.md#audit)). Login, logout and the owner bootstrap
are user-scoped, so the five audit rows the deployment wrote are by
design **not** shown on the Audit page, which can look like a broken or
empty audit trail.

Done when each defect is reproduced with steps, classified (display only,
or misleading for an approval or audit decision), fixed or explicitly
accepted, and — for Preview — it is shown that an approver sees exactly
the revision and content that would be published. A defect classified as
misleading for an approval decision is moved to FU-024C1-5's checklist.

### FU-024C1-5 — Verify the safety gates before publication mutations are switched on

**Status:** open. **Priority:** P1. **Blocks mutations:** yes.
**Recorded in:** the SMM-024C-1 review (2026-10-12).

Switching `AI_SMM_API_MUTATIONS_ENABLED` to `true` in production lets an
admin schedule, and the LIVE worker then publishes. The safety gates were
proven on the read-only deployment (refusal of every mutation), not on an
editable one in production. Before the switch, on the exact release that
will run with mutations on:

- the human-approval invariant: no route, agent or job can approve a
  revision or set `human_reviewed`; only an approved, materialised
  snapshot can be scheduled (ADR, *Production invariant*);
- the central mutation gate and the service-boundary guard still refuse
  when the flag is off, and the flag is still a literal in
  `compose.control-center.yml` (no environment override);
- RBAC (editor vs admin/owner), CSRF and the project-scoping of every
  command;
- the command ↔ worker concurrency guarantees (row lock, `SKIP LOCKED`,
  lease) and the duplicate-publication barriers;
- the database role of FU-024B3-1 in place and its grants checked;
- FU-024C2-1 (session forgery), FU-024C2-2 (audit authenticity) and
  FU-024C2-3 (the mutation privilege model) closed;
- a rehearsal of the enable / disable switch on a production-like copy,
  including the rollback (flag back to `false`) and what the worker does
  with anything scheduled meanwhile.

Done when the checklist above is executed and recorded for that release,
the result is reviewed, and the owner approves the compose change
separately.

---

## From SMM-024C-2A (least-privilege role, 2026-10-12)

Recorded when SMM-024C-2A-FIX was accepted with follow-ups. Background,
tests and evidence: [least-privilege-role.md](least-privilege-role.md)
§4.1–4.3 and §6. None of them blocks switching the **read-only** API to
`ai_smm_cc`: each risk below existed in a larger form while the API
connected as the owner (until 2026-10-13); with the role in production
(SMM-024C-2B) they are the accepted residual risks of the read-only
mode. The first three block business mutations.

### FU-024C2-1 — Session forgery through the API's database connection

**Status:** open, accepted for the read-only mode. **Priority:** P1.
**Blocks mutations:** yes.

Login needs `INSERT` on `user_sessions`, so whoever controls the API's
database connection (code execution in the API, injected SQL) can create
a session for **any** user, with a token it knows and any `expires_at`;
the application then accepts it. `FOREIGN KEY (user_id)` only requires
the user to exist and `UNIQUE (token_hash)` only a new token — neither
prevents this. The role can also revoke or keep alive any session
(`UPDATE (revoked_at, last_seen_at)` is not limited to one's own rows).
Proved by `test_residual_risk_the_role_can_mint_a_session_for_any_user`
and `test_residual_risk_the_role_can_revoke_or_refresh_any_session`.

In place: detection only — `deploy/control-center/db/session-anomalies.sql`
(lifetime, future timestamps, no matching `auth.login` audit row); it
misses a forgery that also writes a consistent audit row.

Options, smallest first: (a) application only, no migration — stop
sending `created_at`/`last_seen_at` on insert (both have
`server_default now()`) and take `INSERT` on them away, so a forged
session cannot be backdated; reject implausible sessions in
`resolve_session` (lifetime, future timestamps); (b) migration — create
sessions through a `SECURITY DEFINER` function owned by the owner, or a
`BEFORE INSERT` trigger enforcing server timestamps and the lifetime;
(c) row-level restriction of the session `UPDATE`s (RLS or a function).
None of them stops a compromised API from choosing *which* user: that is
inherent while the API itself decides that a password was correct.

Done when the chosen option is implemented and tested on the role, and
the decision on the inherent remainder is recorded before mutations.

### FU-024C2-2 — Authenticity of audit actor and action

**Status:** open, accepted for the read-only mode. **Priority:** P1.
**Blocks mutations:** yes.

Guaranteed today towards `ai_smm_cc`: existing audit entries cannot be
changed or removed (no `UPDATE`/`DELETE`/`TRUNCATE`), `at` is the
server's `now()` and `id` comes from the sequence only. **Not**
guaranteed: who wrote an entry. The role writes any `actor`, `action`,
`subject`, `details` — e.g. `worker:worker-1` / `publication.published`
(`test_residual_risk_the_role_can_write_any_actor_and_action`). Nothing
in the database records or verifies the author, structurally or
cryptographically; that the API writes only `auth.*` entries is a
property of its code.

Proposed (migration, separate decision): a column `written_by name NOT
NULL DEFAULT current_user` without `INSERT` privilege for the role, plus
`CHECK (written_by <> 'ai_smm_cc' OR (action LIKE 'auth.%' AND (actor =
'anonymous' OR actor LIKE 'user:%')))`. Tamper evidence against the
owner (hash chaining, off-host shipping) is a further, independent step.

Done when API-written rows are structurally distinguishable and confined
to their event types, tested on the role.

### FU-024C2-3 — A privilege model for business mutations

**Status:** open. **Priority:** P1. **Blocks mutations:** yes.

`ai_smm_cc` is designed for the read-only business mode only: with it,
`AI_SMM_API_MUTATIONS_ENABLED=true` makes every business command fail in
PostgreSQL (`test_every_business_command_fails_in_the_database_without_the_gate`).
Enabling mutations therefore needs a new, reviewed privilege model — a
second role or a reviewed extension — with its own grants, verify matrix,
gate and tests, built from the inventory of refused statements in
least-privilege-role.md §6.2 and the constraints in §9: column-level
`UPDATE` on `publications` limited to what the three commands write,
`INSERT`-only on the append-only editorial tables, no `DELETE`/DDL, no
access to `publication_attempts` writes or `alembic_version`, and the
command ↔ worker locking re-verified under the new role. FU-024C2-1 and
FU-024C2-2 are prerequisites.

Done when that model is designed, validated locally like SMM-024C-2A,
and part of FU-024C1-5's checklist for the release that enables
mutations.

**SMM-025C (2026-10-14):** designed and validated locally — the second
role `ai_smm_cc_rw` (`deploy/control-center/db/ai_smm_cc_rw.*.sql`), a
pinned compose override and `cc-compose.sh mutations status | enable |
disable`; the business-command suites, command ↔ worker concurrency
included, pass connected as the role. Design, matrix, residual risks:
[mutation-role.md](mutation-role.md). Remaining: create it in production
and run FU-024C1-5's checklist on the release that enables mutations;
FU-024C2-1, FU-024C2-2 and FU-025C-1 decided first.

### FU-024C2-4 — Flush before the SAVEPOINT of the optional hash upgrade

**Status:** open. **Priority:** P2. **Blocks mutations:** no.

`ai_smm.application.auth._upgrade_password_hash` absorbs only SQLSTATE
42501, and inside its SAVEPOINT the only statement is the hash
`UPDATE`. But `Session.begin_nested()` first flushes anything already
pending, in the *outer* transaction; a 42501 there is caught by the same
`except`. Measured: the outer transaction is already rolled back, the
logging line's access to `user.id` raises `PendingRollbackError`, and
the request fails with 500 — fail-closed, but with the wrong error, and
without that attribute access it would log a false "upgrade skipped"
before failing on the next statement. Unreachable in the current login
(nothing is pending when `authenticate()` runs), but not enforced by the
function.

Fix: an explicit `db.flush()` before the `try`, the user id captured
before it, and a test with a pending change the role may not make.

---

## From the SMM-025A code review (2026-10-13)

The independent review of the SMM-025A frontend changes (F1–F8, branch
`feat/smm-025a-multi-project-ui`) ended in **ACCEPT WITH FOLLOW-UPS**. No
finding blocked the patch. Design and acceptance criteria:
[architecture/smm-025a-product-ui-design.md](architecture/smm-025a-product-ui-design.md).

### FU-025A-1 — Local state of Content Detail survives a change of item

**Status:** open. **Priority:** P2. **Blocks mutations:** no. **Pre-existing:**
yes — not introduced by SMM-025A; F1 keys only the `/projects/:projectId/*`
pages.

`/content/:contentItemId` is one route instance. Going from one item
straight to another (back / forward, a typed URL) keeps
`ContentDetailPage`'s local state: `editing`, the open confirmation
`dialog` (with the revision and item version it was opened for),
`note`, `notice` and `lastMaterialize`. Queries, breadcrumbs and the
active project follow the new item correctly; only this local state is
stale. By reading the code (not tested): confirming a dialog left over
from item A sends A's `revision_id` to item B's endpoint, which the
server refuses because only B's current revision is accepted — an error,
not a wrong decision. `PublicationDetailPage` has the same shape with
less state.

Done when the detail pages start from fresh state for each id (for
example keyed by the id, as `ProjectScope` keys its outlet), with a test
that navigates item → item with a dialog open.

### FU-025A-2 — Module-global `navigateRef` in the test renderer

**Status:** open. **Priority:** P3. **Blocks mutations:** no.

`web/src/test/render.tsx` keeps the router's `navigate` in a module-level
variable set by `NavigateProbe`, used by `navigateTo()`. It works because
tests in a file run one after another and each render replaces it, but
it is shared mutable state: a test that calls `navigateTo()` before its
own render, or tests run concurrently within a file, would use another
render's router.

Done when `renderApp()` returns its own `navigateTo` (bound to that
render), and the global is gone.

### FU-025A-3 — Diagnostics of a failed Projects Overview row

**Status:** open. **Priority:** P3. **Blocks mutations:** no.

When one project's `/operations/summary` fails, its row shows "—" in the
four counter cells (AC-A5) — with no reason and no way to retry that
row; the error code and request id are discarded. A 401 still ends the
session as everywhere else.

Done when a failed row says why (at least the error title and request
id, for example in a tooltip or a small inline note) and offers a
per-row retry, without turning one row's failure into a page banner.

### FU-025A-4 — The initial route uses a snapshot of `me.projects`

**Status:** open. **Priority:** P3. **Blocks mutations:** no.

`/` decides between "the only project's dashboard" and `/projects` from
`/auth/me`, read at sign-in or bootstrap, while the switcher and the
overview use `GET /projects`. If memberships change during a session,
`/` can send the user into a project that now answers with the
no-access panel, or to the list although one project is left. Nothing
is disclosed (the server decides), and a reload corrects it.

Done when the initial route uses the same fresh source as the switcher
(`GET /projects`, waiting for it), or the choice is documented as
accepted.

## From SMM-025C (production readiness, 2026-10-14)

FU-025C-1 is recorded above, next to FU-025B-1.

### FU-025C-2 — Packaging of the credential keys and the legacy allow-list

**Status:** open. **Priority:** P1. **Blocks mutations:** yes.
**Recorded in:** SMM-025C Phase 3, from the VPS inventory of 2026-10-14
([reports/smm-025b/migration-rollback.md](reports/smm-025b/migration-rollback.md) §3.2, D2/D3).

The SMM-025B code reads `AI_SMM_CREDENTIALS_PUBLIC_KEY` and
`AI_SMM_LEGACY_THREADS_PROJECTS` in the API and
`AI_SMM_CREDENTIALS_PRIVATE_KEY_FILES` in the worker, but no deployed
file passes them: `compose.control-center.yml` sets every API variable
as a literal and the wrapper allows only the database URL in
`control-center.api.env`; the worker's `compose.yaml` mounts no key.
Read-only, the only effect is that Project Settings shows legacy
publishing as unused for the allow-listed project. With mutations on,
saving a credential answers `CREDENTIALS_NOT_CONFIGURED` and content of
an allow-listed project needs an account (both fail closed).

Proposed: two non-secret keys in `control-center.compose.env`
(interpolated into smm-api's `environment`, validated by the wrapper);
in the worker's compose file a read-only bind of the key file (owner
10001, mode 600) and the path in `ai-smm.env`; tests as for the existing
packaging; the worker preflight (§3a) already fails closed on a missing
key once a credential exists. Done when packaged, tested and part of the
R7 steps.

## From the SMM-025B review (2026-10-13)

The final independent review of SMM-025B (multi-project & multi-account
platform) accepted B1 and B2 with these follow-ups; none blocks the
local commit, FU-025B-1 blocks production mutations. Design and reports:
[architecture/smm-025b-project-management.md](architecture/smm-025b-project-management.md),
[reports/smm-025b/](reports/smm-025b/README.md) (the open-issues report
maps OI-n to these entries).

### FU-025B-1 — The mutation privilege model must cover the SMM-025B tables

**Status:** open. **Priority:** P1. **Blocks mutations:** yes (part of
FU-024C2-3). **Was:** OI-1.

An editable API needs, and `ai_smm_cc` deliberately has none of:
`INSERT` on `projects`, `project_memberships`, `project_settings`;
`UPDATE (display_name, description, default_timezone, default_language,
version, updated_at)` on `projects`; `INSERT`/`UPDATE` on
`social_accounts` (no `DELETE`); `INSERT` and `UPDATE (status,
ciphertext, nonce, ephemeral_public_key, superseded_at, revoked_at,
verification_status, verified_at)` on `social_account_credentials` —
writing key material without being able to read it back.

Done when FU-024C2-3's role, grants, verify matrix and tests include
these, with the refusals of `tests/test_db_least_privilege.py` kept.

**Done technically (SMM-025C, 2026-10-14), local only.** `ai_smm_cc_rw`
grants all of the above except `UPDATE (verification_status,
verified_at)` on credentials, which no command performs (they are written
on `INSERT`); `ai_smm_cc` is unchanged and its refusals still pass.
Tests: `tests/test_db_mutation_role.py`, `tests/mutation_role/`,
`tests/test_control_center_mutations.py`. Design:
[mutation-role.md](mutation-role.md). Production follows FU-024C2-3.

### FU-025C-1 — Database-side invariants for rows the mutation role inserts

**Status:** open. **Priority:** P1. **Blocks mutations:** yes (decide
before the switch). **Recorded in:** SMM-025C Phase 2 (2026-10-14).

`ai_smm_cc_rw` must `INSERT` every column the ORM sends, so whoever
controls its connection can insert a publication already `scheduled`
and `human_reviewed` with no approval or lineage row (the LIVE worker
would publish it), store any sealed blob as a `verified` credential, and
add any existing user to any project in any role. Proved by
`test_residual_risk_the_role_can_insert_a_reviewed_scheduled_publication`
and `test_residual_risk_the_role_can_store_a_credential_marked_verified`.
Same trust class as FU-024C2-1 / FU-024C2-2: the database cannot tell a
compromised API from a correct one.

Proposed (migration): `BEFORE INSERT` triggers active for
`current_user = 'ai_smm_cc_rw'` — on `publications`, `status =
'approved'`, delivery and claim columns empty, `human_reviewed` only
with an approval and lineage row for the same revision (or the lineage
row inserted in the same transaction, checked by a deferred constraint
trigger); on `project_memberships`, only for a project created in the
same transaction. Done when implemented and tested on the role, or the
remainder is accepted by the owner in writing for the release that
enables mutations.

### FU-025B-2 — A gated wrapper verb for creating a project in production

**Status:** open. **Priority:** P2. **Blocks mutations:** no. **Was:** OI-2.

Before mutations, a second production project can only be created with
`ai-smm project create` as the owner role. The CC wrapper offers
`create-owner` (exactly three options) but no project verb.

Done when `cc-compose.sh create-project` exists behind the identity,
queue-window and backup gates, with the wrapper's allowlist tests.

### FU-025B-3 — Generation does not read topics and tone of voice yet

**Status:** open. **Priority:** P2. **Blocks mutations:** no. **Was:** OI-3.

`content_config.topics` and `brand_config.tone_of_voice` are stored and
edited; the strategist/copywriter agents do not use them (the settings
page says so).

Done when the AI-assisted stage feeds them into generation.

### FU-025B-4 — Credential key rotation without re-entering tokens

**Status:** open. **Priority:** P3. **Blocks mutations:** no. **Was:** OI-4.

Rotation works today by holding both private keys and re-entering each
token. Done when `ai-smm credentials rewrap` re-seals active credentials
for a new public key on the worker host, audited, without printing them.

### FU-025B-5 — Egress policy for the API's connection test

**Status:** open. **Priority:** P3. **Blocks mutations:** no. **Was:** OI-5.

Saving a token makes the API call `GET /me` on the Threads Graph API.
Done when the CC network policy allows exactly that host for `smm-api`
(or the decision to run the test elsewhere is recorded).

### FU-025B-6 — Rate limits per account

**Status:** open. **Priority:** P3. **Blocks mutations:** no. **Was:** OI-7.

The daily cap and spacing still count per platform across all accounts
— conservative, never above the old limits. Belongs to SMM-028.

### FU-025B-7 — Configuration changed under a running worker

**Status:** open, accepted. **Priority:** P3. **Blocks mutations:** no.
**Was:** OI-8 (remainder).

The worker preflight checks the configuration at start. A change while a
correctly started worker runs (a key file removed, the allow-list edited
without a restart) is refused row by row — nothing is sent, the rows
become `failed`. Done when this is either re-checked periodically or
documented in the operations runbook as "restart after any change".

### FU-025B-8 — Alert on a worker that refuses to start

**Status:** open. **Priority:** P3. **Blocks mutations:** no. **Was:** OI-12.

A failed preflight exits with 78; under `restart: unless-stopped` that is
a harmless but noisy restart loop. Done when monitoring alerts on it.

### FU-025B-9 — Image paths of legacy rows are not project-scoped

**Status:** open, accepted. **Priority:** P3. **Blocks mutations:** no.
**Was:** OI-11.

The editorial layer confines revision images to the project's own
`assets/`; rows written by `import-json` or the CLI are only confined to
the shared media root. Done when the importer applies the same rule.
