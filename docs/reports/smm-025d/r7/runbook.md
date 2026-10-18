# R7 — switching on business mutations in production: runbook

**Nothing here has been executed in production.** Every step below needs
the owner's separate approval; between steps the system is in a
consistent, supported state, and until R7.6 it stays exactly as read-only
as after SMM-025D Group E. Production release **N** = `dce93e5`
(SMM-025D); the integration release **N′** is based on main
`fbf1b1a` and adds `CC_RELEASE_PERMITS_MUTATIONS=yes`.

Conventions as in SMM-025D: `ssh n8n-vps 'bash -s' < script`, read-only
psql (`PGOPTIONS='-c default_transaction_read_only=on'`) for every check,
secrets as key names only, no helper files left on the host, `</dev/null`
on every `docker exec` / `compose run`. `$CC=/root/ai-smm-control-center`,
`$W=/root/ai-smm`, `$STG=/root/ai-smm-control-center-staging/<N′>`.

## What changes, what does not

| | N (now) | after R7 |
|---|---|---|
| Schema | `c4d81e2a9f63` | separately approved upgrade: `c4d81e2a9f63 → e04cb3a748f5 → f7a91c2d5e04`; no automatic downgrade |
| Worker | `ai-smm:dce93e5`, LIVE | unchanged (no image, no restart) |
| Generation worker | absent from production deployment | remains absent and disabled; its role, credentials and LLM budget are a later, separately approved activation |
| smm-auth, smm-web | `dce93e5` | N′ images from the integrated commit, recreated by R7.2 |
| smm-api | `dce93e5`, `ai_smm_cc`, flag `false` | `N′`; after R7.6 **`ai_smm_cc_rw`, flag `true`** |
| Roles | `ai_smm`, `ai_smm_cc` (G2), `ai_smm_auth` | refreshed `ai_smm_cc` grants after migration; + `ai_smm_cc_rw` only in R7.4; `ai_smm_generation` is not activated |
| `$CC` files | N | N′: `cc-gate.sh`, generation-aware grants/verify files and their SHA pins are from the integrated commit |

## The one risk this runbook is built around

The worker is **LIVE** (`AI_SMM_DRY_RUN=false`) and holds the deployment
token for the allow-listed project `ai-catalog-consultant`. With mutations
on, *any* content the owner approves (step-up with the password) and
schedules there is published for real — that is the purpose of R7, and
also why:

- the smoke runs in its **own project `r7-smoke`**, which is not
  allow-listed and has no verified account, so nothing there can be
  routed even by mistake; the smoke client refuses a project with legacy
  publishing and never schedules;
- **`mutations disable` does not stop publishing.** It makes the Control
  Center read-only; rows already approved and scheduled stay scheduled
  and the worker publishes them (proved: rehearsal R7-6/R7-7). To stop
  publishing, cancel them first, while the mode is still on (R7.R1).
- A generation job may be created only while mutations are enabled, but no
  generation worker is deployed by R7. Do not create one in this rollout:
  its future activation needs a reviewed service definition, an
  `ai_smm_generation` credential and explicit LLM spend/test limits.

## R7.0 Preconditions (no change)

1. Group O of SMM-025D closed and accepted (end slice ≥ 2026-10-16T06:00Z, `RESULT: OK`).
2. The owner accepts SMM-025D: ADR-025D (status "Proposed") and the
   residual risks ([../residual-risks.md](../residual-risks.md)).
3. The owner reviews and approves N′ ([readiness-report.md](readiness-report.md));
   commit + merge + push of N′ (separate approval).
4. The owner's decisions D1–D5 ([readiness-report.md §4](readiness-report.md#4-decisions-for-the-owner)).

## R7.1 Release N′: build, stage, load (Group A pattern; nothing running changes)

```bash
# laptop, from the merge commit N′ (40 hex), never from a working tree
B=$(mktemp -d) && git archive "$NP" | tar -x -C "$B"
docker build --target api --label org.opencontainers.image.revision="$NP" -t "ai-smm-api:$NP" "$B"
docker build              --label org.opencontainers.image.revision="$NP" -t "ai-smm-web:$NP" "$B/web"
```

- **Content check:** build from the exact integrated commit and validate the
  archive, manifest, labels and staged SHA pins. N′ intentionally differs
  from N in SMM-026A API/web code, Alembic revisions and grants; it must
  not be represented as a label-only rebuild. The LIVE publisher image is
  not rebuilt or restarted, and no generation-worker service is added.
- Bundle: `ai-smm-cc-$NP.tar.gz` + the deploy files of N′ (same list as
  Group A) + `MANIFEST-$NP.sha256`; transfer to a **new** `$STG`; `sha256sum -c`;
  `docker load` (refuse an existing tag); `chown -R root:root $STG`.
- Pins in the staged `cc-gate.sh` equal the staged files (5 pins);
  `CC_RELEASE_PERMITS_MUTATIONS=yes`.

**Checkpoint R7.1:** images N′ loaded with label N′; `$STG` = Git at N′; Q unchanged.

## R7.2 Backup, migrate and Control Center N → N′, read-only

1. Window (as Group B step B1, ≤ 2 h, not 00:00–01:00 UTC) — `preflight`'s
   queue gate needs one.
2. Before the migration, take and verify a fresh, off-host backup of the
   current `c4d81e2a9f63` database (R7.3's procedure). The owner performs
   the decrypt check and acknowledges it before any schema write.
3. In its own approved window, run Alembic to `f7a91c2d5e04`; verify the
   linear chain `c4d81e2a9f63 → e04cb3a748f5 → f7a91c2d5e04`. Do not
   downgrade production automatically. Apply the staged `ai_smm_cc` grants
   after the migration so its generation-job SELECT and verify contract are
   exact; re-run the existing auth-role verify unchanged.
4. Keep `$CC` files in `$CC/rollback-dce93e5/` (+ `SHA256SUMS`).
5. Install the complete staged deployment set and compare every file with
   the manifest; `control-center.compose.env` gets `SMM_CC_VERSION=<N′>`,
   other keys unchanged.
6. `./cc-compose.sh preflight` → identity (key id `bd53581cbed23d6d`,
   allow-list = the LIVE worker's), api-role, auth-role, queue: PASS.
7. `./cc-compose.sh up` → smm-api, smm-auth, smm-web healthy on N′.
8. `./cc-compose.sh mutations status` → `mode: read-only`, **no** "does not
   permit" line, `smm-api: AI_SMM_API_MUTATIONS_ENABLED=false role=ai_smm_cc`.
9. T-N6 and the read-only authenticated smoke of Group E (57 checks).

**Checkpoint R7.2:** schema head is `f7a91c2d5e04`; CC is on N′ and
read-only. Rollback: restore the prior CC files/images only if their
forward-schema compatibility was verified in the window; never run an
automatic production schema downgrade.

## R7.3 Backup (checklist #9; Group B pattern)

Window; `cc-db-backup.sh dump` + `verify --expect-revision c4d81e2a9f63`
before migration, and retain the verified off-host copy after migration;
gpg copy off-host to the DB-backup key (not the credential-key copy);
owner's decrypt test; `control-center.offhost-ack`; backup gate PASS
(the wrapper's own gate functions, initialised like `cc-db-backup.sh`).

**Checkpoint R7.3:** a verified pre-migration dump of `c4d81e2a9f63` is
< 24 h old, off-host and acknowledged. A second post-migration backup is
recommended before any later generation-worker activation.

## R7.4 The mutation role (production DB change; each sub-step approved)

1. Confirm that the refreshed `ai_smm_cc` verify reports 0 FAIL on
   `f7a91c2d5e04`. As the instance superuser, apply
   `ai_smm_cc_rw.grants.sql` from `$STG`
   (one transaction; creates the role without a password; EXECUTE on the
   six guard functions).
2. **Owner, interactively:**
   `ssh -t n8n-vps "docker exec -it n8n-compose-postgres-1 psql -X -U n8n -d ai_smm -c '\\password ai_smm_cc_rw'"`
   — a new, unique password, kept in the owner's password manager.
3. **Owner, interactively:** `control-center.api-rw.env` (600 root) with
   the same pattern as Group E's `write-auth-env.sh` (password over SSH
   stdin, DSN built on the host from the non-secret parts of
   `control-center.api.env`, login test as `ai_smm_cc_rw` with
   `--env-file`), role `ai_smm_cc_rw`.
4. `ai_smm_cc_rw.verify.sql` (in `$CC`, pinned): 0 FAIL, ≥ 200 checks;
   `ai_smm_cc.verify.sql` and `ai_smm_auth.verify.sql` still 0 FAIL;
   ACL snapshot diff (Group D `acl.sh`): only `ai_smm_cc_rw` entries added.

**Checkpoint R7.4:** three role checks 0 FAIL; nothing else changed;
smm-api still `ai_smm_cc`. Rollback: `ai_smm_cc_rw.revoke.sql` (refuses
while a session of the role is open; none is, until R7.6).

`ai_smm_generation.grants.sql` is deliberately **not** applied here. It
creates an execute-only role for a future generation worker; R7 neither
creates its password nor starts a worker, so no LLM call can occur.

## R7.5 FU-024C1-5 checklist on N′ (rollout-rollback.md §5)

Items 1–3, 5–8 read-only; item 4 is a transaction that is rolled back:

```sql
-- as the instance superuser, connected to ai_smm; SET ROLE makes the
-- guards see ai_smm_cc_rw exactly as if it had logged in, without its password
BEGIN;
SET LOCAL ROLE ai_smm_cc_rw;
INSERT INTO publications (project_id, platform, ordinal, status, human_reviewed,
    title, format, body, idempotency_key, scheduled_at)
VALUES ('r7-smoke', 'threads', 9001, 'scheduled', true, 'forged', 'text', 'forged', 'r7-forged', now());
ROLLBACK;
```

Expected: `ERROR:  guard: creating a publication needs a session-bound
transaction (guard_bind_session)` — and the same for an `audit_log`
business entry and a `projects` row (rehearsal R7-2). Item 10 is R7.8.

## R7.6 Enable (production change; inside a window)

1. Q before (Group O slice: `o-take-slice.sh r7-before`): queue empty or
   understood; nothing due within the window.
2. `./cc-compose.sh mutations enable --approved-by "<owner>"` — identity
   gate, API role gate **on `ai_smm_cc_rw`** (pinned verify), the read-only
   way back checked; writes `control-center.mutations` for N′; recreates
   **smm-api only** with the pinned override; checks the running container.
3. `./cc-compose.sh mutations status` → `mode: business-mutation (release
   <N′>, … approved by <owner>)`, `smm-api: AI_SMM_API_MUTATIONS_ENABLED=true role=ai_smm_cc_rw`.
4. smm-auth, smm-web and the LIVE publisher: same container ids as before;
   no generation-worker container or credential is introduced.

**Stop:** any gate refusal — nothing was changed (the wrapper refuses
before writing the mode file); a failed start — the wrapper removes the
mode file and stops smm-api: run `mutations disable` at once.

## R7.7 Smoke without a publication

Owner creates the password RAM file (see the script header), then:

```bash
scripts/r7_production_smoke.sh n8n-vps <owner-email> r7-smoke
```

The client (rehearsed as R7-4: 22 checks, 0 failed):
`r7-smoke` created by the owner on the first run; refuses a project with
legacy publishing; CSRF refusal; account; credential with `verify:false`
(no platform call) then revoked (material wiped); draft → revision →
submit → reject, the destination being that test account (active, never
verified: nothing addressed to it can be routed); an approval **without**
step-up → `403 APPROVAL_CONFIRMATION_REQUIRED` and the item still in
review; no publication; then both items rejected and the account
disabled.

Read-only afterwards: in `r7-smoke` 0 publications, 0 approvals, 0
approval grants; audit rows of the smoke: `auth.*` by `ai_smm_auth`,
business by `ai_smm_cc_rw`; `session-anomalies.sql` 0; Q of
`ai-catalog-consultant` unchanged (fingerprint `5a1aff8a…` while nothing
new is scheduled there).

What stays in production by design (no DELETE right for any API role):
project `r7-smoke` with its membership and settings, one disabled
account, one revoked credential (no key material), two rejected items,
their audit rows.

## R7.8 Checklist #10: the way back, rehearsed in production

`./cc-compose.sh mutations disable` → `mode: read-only`, smm-api as
`ai_smm_cc`, flag `false`; a business write is 403 `MUTATIONS_DISABLED`
(the read-only checks of the client, phase `after-disable`, project
`r7-smoke`). Then, only if the owner decides so (D2), `mutations enable`
again.

## R7.9 Observation

As SMM-025D Group O, plus: approvals carry an `approval_grants` row each;
no `ApprovalLineage` failure; every business audit row `written_by =
ai_smm_cc_rw`; every publication the worker takes has a link and a
human approval (`guard_publication_problem(id) IS NULL`).
Generation jobs remain absent unless a later, separately approved worker
activation has defined its monitoring and cost limits.

## Rollback

| From | To | Procedure | Loses |
|---|---|---|---|
| mutation mode | read-only | **R7.R1:** if publishing must stop too, first cancel what is scheduled **in the UI, while the mode is still on** (cancel is a business command: once read-only, nothing can cancel it — there is no operator CLI `cancel`, FU-025D-6); then `./cc-compose.sh mutations disable` (always allowed, one recreate). Emergency brake when that is not possible: `docker compose -p ai-smm stop worker` in `$W` — stops **all** publishing, the queue waits, a separately approved start resumes it | nothing; drafts and approvals stay |
| role `ai_smm_cc_rw` | none | after `mutations disable`: `ai_smm_cc_rw.revoke.sql`; `control-center.api-rw.env` shredded | nothing (rows it wrote stay) |
| CC N′ | CC N | only after checking forward-schema compatibility; restore `$CC/rollback-dce93e5/`, `./cc-compose.sh up` (N refuses the N′ mode file: `mutations disable` first) | nothing |
| schema | — | no automatic downgrade; stop, preserve evidence and restore only from the verified pre-migration backup under a separately approved incident procedure | rollback is not routine |

Proved locally: [evidence/r7-mutation-rehearsal.log](evidence/r7-mutation-rehearsal.log)
(enable → smoke → step-up/approve/schedule → disable → worker → revoke).
