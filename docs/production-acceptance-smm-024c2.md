# Production acceptance — SMM-024C-2B (Control Center API on a least-privilege role)

Audience: the project owner and whoever operates or extends the
production deployment. This is the sanitised record of how the
production Control Center API was moved from the database owner role to
the restricted role `ai_smm_cc` on **2026-10-12 – 2026-10-13**, and of
what was and was not verified.

> **Sanitised.** Database URLs, addresses, container and image ids, the
> Docker engine id, PostgreSQL identity values, checksums of backups,
> credentials and private paths of the owner's machines are **not** in
> this document. They are in the private step reports and audits,
> outside Git. Names in angle brackets follow
> [operations.md](operations.md); `$CC` is the Control Center directory
> on the host ([control-center-deployment.md](control-center-deployment.md)).
>
> Sources: the step reports SMM-024C-2B-1 (preflight), B-C1 (rehearsal),
> B-2 (bundle), B-3 (staging), P1, P2-BACKUP, P2-BACKUP-SEC, A1, A2 and
> A3, and the design and local validation in
> [least-privilege-role.md](least-privilege-role.md). A fact that none of
> them contains is marked **NOT VERIFIED**.

**Status: PRODUCTION ACCEPTED** by the owner on **2026-10-13** (A3
VERIFIED at 04:44 UTC after one controlled retry). Business mutations
stay off; the live worker was not changed.

---

## 1. Release identity

| | |
|---|---|
| Release commit | `0cbd641074ad3bc0501e54f59dac90bd1ccc9006` (`main`, merge of SMM-024C-2A `74cd10a` into `bc60b48`) |
| Previous production release | `8ccccda161c3a0b4bc3fd4a90d86dfd19dabd665` ([production-acceptance-smm-024b.md](production-acceptance-smm-024b.md)); its images stay loaded on the host |
| Images | `ai-smm-api:<release>`, `ai-smm-web:<release>`, built from a `git archive` of the release, OCI revision label = full commit, linux/amd64; a second build from the archive gave identical image ids |
| Transfer | one bundle (image archive, compose file, `cc-*.sh`, the role's SQL files) covered by a `MANIFEST-<commit>.sha256`; `sha256sum -c` passed on the host at staging and again before every step |
| Why new images | `src/ai_smm/application/auth.py` changed: under the role the optional Argon2 hash upgrade is skipped instead of failing the login ([least-privilege-role.md §4.3](least-privilege-role.md#43-password-hash-upgrade-without-update-password_hash)) |
| Database role of `smm-api` | **`ai_smm_cc`** (from `control-center.api.env`) |
| Database role of the worker, `migrate`, `cli` | the owner role `ai_smm`, unchanged |
| Schema | Alembic **`3d4ac1986f07`**, unchanged — no migration in this stage |
| Business mutations | **off**: `AI_SMM_API_MUTATIONS_ENABLED=false`, a literal in the compose file |
| Access | unchanged: only `127.0.0.1:8088` on the host, through an SSH tunnel |

## 2. What was approved, step by step

Each step had its own owner approval and, from A1 on, its own window.
Approval of one step did not extend to the next.

| Step | Content | Approval |
|---|---|---|
| B-1 | read-only production preflight | none (read-only) |
| B-C1 | local rehearsal on a database with production's exact ACLs | none (local) |
| B-2, B-3 | release bundle; staging next to the live directory, nothing loaded | B-3: owner |
| P1 | load images `0cbd641`, run them on the **old** configuration (owner URL, old compose file and scripts) | A0 |
| P2-BACKUP | restore test of the current nightly dump; encrypted off-host copy | owner |
| A1 | create `ai_smm_cc` and its grants on the shared instance | A1 |
| A2 | the role's password; `control-center.api.env` | A2 |
| A3 | install the new scripts and compose file; switch `smm-api` | A3, and one retry approved separately |
| RB-1 | previous compose file and scripts back, `up` (trigger-based) | granted together with A3 |

## 3. Timeline (UTC)

| When | Step | Result |
|---|---|---|
| 2026-10-12 12:13 | B-1 read-only preflight | READY FOR APPROVAL; facts for the grants file confirmed (all objects owned by `ai_smm`, role name free, `PUBLIC` has no `CREATE`) |
| 2026-10-12 | B-C1 local rehearsal: P1 → P2 → RB-1 → RB-3 through the real wrapper | passed; only `smm-api` recreated by P2 |
| 14:35–14:38 | B-3 staging | STAGED; checksums passed; production unchanged |
| 15:04 | P1 — images `0cbd641` on the old configuration | VERIFIED: ≈ 6.7 s API unavailability, smoke 37/37, worker unchanged; **accepted** by the owner |
| 15:12–15:33 | P2-BACKUP, P2-BACKUP-SEC | restore test PASS; encrypted off-host copy (§6) |
| 18:19 | A1, first attempt | **stopped at preflight**: the approved window had expired; nothing applied |
| 18:30:42 | A1 — grants file committed (new window 18:30–19:30) | VERIFIED: verify 700 PASS / 1 FAIL (`role.password`, expected before A2) / 5 INFO; ACLs of all other roles identical before and after |
| 19:03 | A2 — password (entered interactively by the owner), `control-center.api.env` | VERIFIED: verify 701 / 0 / 5; test connection as `ai_smm_cc` with the expected privileges; `smm-api` still on the owner URL |
| 19:11 | A3, first approval | **stopped at preflight**: 19 min left in the window — not enough for checks, smoke and a rollback. Owner paused A3 |
| 2026-10-13 04:30–06:00 | A3 window (`control-center.window` rewritten for it) | — |
| 04:30–04:31 | A3 preflight | 63 checks, 0 failures |
| 04:38:21 | A3 attempt 1 | switched, then **automatic RB-1** on a defective diagnostic probe (§5) |
| 04:41:59 | preflight before the retry | 67 checks, 0 failures; state after RB-1 confirmed |
| 04:42:25 | A3 attempt 2 (retry) | switched; all checks passed |
| 04:43 | authenticated smoke and read views through the tunnel | 37/37, 24/24 |
| 04:44:04–04:44:09 | post-checks | all passed; **A3 VERIFIED** |
| 2026-10-13 | owner's Dashboard check in the browser | successful; **PRODUCTION ACCEPTED** |

## 4. Tests before production

| Check | Result |
|---|---|
| Full backend suite (`pytest`, live PostgreSQL) | **1822 passed** |
| Same suite with only the API connected as `ai_smm_cc` (`AI_SMM_TEST_API_ROLE=restricted`) | **122 expected refusals, 0 unexpected failures** — 49 `FOR UPDATE` on `content_items`, 35 `FOR UPDATE` on `publications`, 24 `INSERT` into `content_items`, 14 `INSERT` into `project_settings`: exactly the business commands the role must not run ([least-privilege-role.md §6](least-privilege-role.md#6-local-validation)) |
| Rehearsal on a database with production's exact database and schema ACLs, real wrapper, production's Compose version | passed; measured unavailability P1 7.1 s, P2 3.8 s, RB-1 3.5 s |
| Gate pins | `cc-gate.sh` pins the checksums of `compose.control-center.yml` and `ai_smm_cc.verify.sql`; a packaging test keeps them in sync |

## 5. A3 — the switch

### Order of installation (as executed)

1. `control-center.api.env` already existed (A2). The new scripts refuse
   every verb, `ps` and `down` included, without it.
2. Previous `compose.control-center.yml`, `cc-*.sh` and
   `control-center.window` copied into `$EV/prev` with a `SHA256` file —
   the RB-1 source.
3. `control-center.window` rewritten for the approved window.
4. New `cc-common.sh`, `cc-gate.sh`, `cc-compose.sh`, `cc-db-backup.sh`
   and `ai_smm_cc.verify.sql`; each checksum compared with the manifest.
5. **`compose.control-center.yml` last**; checksum compared.
6. `cc-compose.sh config --quiet`, then the new wrapper's `preflight`
   (identity, API role and queue gates): PASS.
7. `cc-compose.sh up`: identity gate, API role gate (706 checks, no
   `FAIL`), then Compose.

The compose file goes last because the **old** wrapper with the **new**
compose file would start `smm-api` on `control-center.api.env` without
the API role gate. The opposite intermediate state (new scripts, old
compose file) is refused by the checksum pin.

### Attempt 1 — rolled back by its own checker

Everything that mattered passed: `smm-api` alone recreated, `smm-web`
untouched, API connections as `ai_smm_cc` only, verify 701/0/5, session
anomalies 0, worker unchanged. One of the eleven privilege probes,
`UPDATE projects SET name = name WHERE false`, returned SQLSTATE
**42703** (undefined column — the column is `display_name`) instead of
**42501**. The script required eleven 42501 refusals and started RB-1
automatically.

RB-1 restored the previous files (checksums verified), ran `up` through
the previous wrapper and confirmed: API ready, connected through the
owner URL, no `ai_smm_cc` session, `smm-web` and worker unchanged,
business data identical. The same probe against a real column, run as
`ai_smm_cc` afterwards, was refused with 42501. RB-1 therefore ran once
in production and worked; its own unavailability was not sampled
separately (the whole RB-1, including checks, took about 10 s).

### Attempt 2 — the controlled retry

The owner approved exactly one retry. The retry script differed from
attempt 1 in two lines only: the probe column (`projects.display_name`)
and the expected baseline id of the API container after RB-1. Every
probe column was checked against `information_schema` first. All probes
are `… WHERE false` and rolled back: none can change a row.

## 6. Results in production (attempt 2)

| Check | Result |
|---|---|
| Containers | only `smm-api` recreated; `smm-web` kept its container id and start time; restart counts 0; both healthy |
| `/health/live`, `/health/ready` | 200 |
| API unavailability | **≈ 3.6 s** (`/health/ready` sampled every 0.5 s; 6 non-200 samples) |
| `pg_stat_activity` | every connection from `smm-api` as **`ai_smm_cc`**; the worker's as **`ai_smm`** |
| `current_user` on the API's own connection | `ai_smm_cc`; reads work |
| PostgreSQL privilege denials | **11/11 refused with SQLSTATE 42501**: `UPDATE` / `DELETE` / `SELECT … FOR UPDATE` on `publications`; `INSERT` into `content_items` and `project_settings`; `UPDATE projects`; `UPDATE publication_attempts`; `UPDATE` and `DELETE` on `audit_log`; `UPDATE users.password_hash`; `CREATE TABLE` in `public` |
| `ai_smm_cc.verify.sql` | **701 PASS / 0 FAIL / 5 INFO**, after the switch and again after the smoke test. The 5 INFO lines: no temporary tables; the role, like every role, can `CONNECT` to the instance's other databases through `PUBLIC` (§8) |
| `runtime_smoke.py`, authenticated (owner) | **37/37** — login, cookie attributes, `/auth/me` with `mutations_enabled=False`, project list, a queue command refused with `MUTATIONS_DISABLED`, logout refused without CSRF token, logout, revoked session 401 |
| Read views (GET only) | **24/24** returned 200: `/auth/me`, `/auth/csrf`, projects, project, settings, operations summary (Dashboard), attention, audit, the three publications with preview and attempts, series list and series, content items (none exist), plus login, logout and the revoked-session check |
| `session-anomalies.sql` | `END|anomalies|0` |
| API log | 0 `permission denied`; 0 warnings or errors; 0 skipped hash upgrades (the production hashes need none) |
| Web log | 6 upstream-refused lines, all from the readiness probe during the 3.6 s gap |
| Mutations | `AI_SMM_API_MUTATIONS_ENABLED=false`; docs off; ports and networks unchanged |
| ACL snapshot of all other roles | identical to the one taken after A2 |
| Files on the host | the six installed files match the manifest |

### Business data and the worker

| | Result |
|---|---|
| `publications`, `publication_attempts`, `projects`, `project_settings`, `content_*`, `content_series` | fingerprints (row count + hash of all rows) **identical** before and after |
| Queue | **3 historical publications, all `published`**; nothing scheduled, claimed, leased or awaiting review; no unfinished attempt |
| Expected writes | the two sign-ins of the checks only: `audit_log` +4 (`auth.login` / `auth.logout` twice), `user_sessions` +2 (both revoked), `users.last_login_at` |
| Worker | container, image, process, restart count, environment and log identical before and after; healthy, heartbeat current; mode **LIVE**, still on the owner role |

The worker, the scheduler, the publication flow and the schema were not
touched. No `GRANT`/`REVOKE`, migration or credential change happened in
A3.

## 7. Owner acceptance

After A3 the owner opened the Control Center through the tunnel and
confirmed that the **Dashboard** works. The other screens were **not**
re-checked by hand in this stage; they are covered by the automated
read-view check above (every API endpoint behind them returned 200 as
the owner). The manual walk of all screens of 2026-10-11 is in
[production-acceptance-smm-024b.md §8](production-acceptance-smm-024b.md#8-manual-ui-acceptance).

## 8. Security: what the role does and does not guarantee

**Now guaranteed by PostgreSQL** (not only by application code): the
API cannot change publications, attempts, projects, settings, content,
approvals or series, cannot delete or change audit entries, cannot
change password hashes and cannot run DDL. This closes FU-024B3-1 for
the read-only mode.

**Accepted residual risks** (unchanged by the rollout, now smaller than
under the owner role):

| Risk | Follow-up |
|---|---|
| **Session forgery:** whoever controls the API's database connection can create a session for any user, and revoke or keep alive any session. Detection only: `session-anomalies.sql` | FU-024C2-1 (P1) |
| **Audit actor/action spoofing:** the role can write audit rows with any `actor` and `action`; existing rows cannot be changed or removed | FU-024C2-2 (P1) |
| **Business mutations blocked by design:** with `ai_smm_cc`, switching `AI_SMM_API_MUTATIONS_ENABLED` on makes every business command fail in PostgreSQL; a separate privilege model is needed first | FU-024C2-3 (P1), FU-024C1-5 (P1) |
| Worker mode shown as `UNKNOWN` in the UI (no verifiable source); the worker is LIVE | FU-024B2-2 |
| API's `application_name` is still `ai-smm/worker-1`; `usename = 'ai_smm_cc'` now tells the API apart | FU-024C1-1 |
| Backups: the 2026-10-12 nightly dump has a restore-tested, encrypted off-host copy, made by hand; nightly dumps still have no checksum and no scheduled off-host copy; the off-host copy of the 2026-10-11 pre-migration dump is still unencrypted | FU-024C1-3 |
| `ai_smm_cc`, like every role, can connect to the instance's other databases (`PUBLIC CONNECT`); closing that is the other projects' change | [least-privilege-role.md §8](least-privilege-role.md#8-shared-instance-constraints-and-residual-risks) |

## 9. Operating notes after this rollout

- **`control-center.window`.** With the new wrapper, `preflight` runs the
  identity, API role **and queue** gates and refuses unless `now()` is
  inside the window in `control-center.window` (at most 12 h). `up` and
  `restart` need only the identity and API role gates, not the window.
  The file now holds the expired A3 window of 2026-10-13; a later
  `preflight` or migration needs a newly approved window written there
  (keep the previous file in the evidence directory).
- **API role gate.** `up` and `restart` run `ai_smm_cc.verify.sql`
  through `control-center.api.env` first and refuse on any `FAIL`. A
  migration that creates or recreates a table must be followed by the
  grants file, or the gate refuses the next `up`
  ([least-privilege-role.md §7.9](least-privilege-role.md#79-later-operations-that-involve-the-role)).
- **Installation order** for any later release that changes these files:
  `control-center.api.env` present → scripts and `ai_smm_cc.verify.sql`
  → `compose.control-center.yml` **last**
  ([least-privilege-role.md §7.4](least-privilege-role.md#74-r4--stage-the-new-compose-file-and-scripts-approval-a3)).
- **Diagnostic probes used as rollback triggers** must be checked
  against the live schema (`information_schema`) before the run: a
  wrong column turns a 42501 check into a false trigger (attempt 1).
- **Evidence** of both A3 attempts is in `$CC/evidence/` on the host
  (mode 700), including the RB-1 source of attempt 2.
- **Previous images** (`8ccccda`) are still loaded; removing them is a
  separate owner decision.

## 10. Rollback

| | What | When |
|---|---|---|
| **RB-1** | previous compose file and scripts from `$EV/prev` back (one `cp`, checksums first), `cc-compose.sh up` through the previous wrapper: `smm-api` back on the owner URL, images stay `0cbd641`. Run once in production (attempt 1) and worked | readiness not 200 within 2 min, a failing smoke check, `permission denied` in the API log, any probe not refused, any doubt about the worker ([least-privilege-role.md §7.8](least-privilege-role.md#78-rollback)) |
| **P1 rollback** | `control-center.compose.env` of `8ccccda` back, `up` | only after RB-1, to return to the previous images |
| **RB-2** | `ALTER ROLE ai_smm_cc NOLOGIN` | separate approval |
| **RB-3** | revoke the role's grants, drop the role, `shred` the env file | separate approval (owner decision) |

The worker is never part of a Control Center rollback; stopping
publishing is the worker's own lever
([operations.md §7](operations.md#7-restart-recovery-rollback)).

## 11. Next steps

| Task | Scope |
|---|---|
| SMM-024C | Post-deployment stabilisation continues with the remaining follow-ups ([follow-ups.md, Register](follow-ups.md#register)) |
| Business mutations | Not scheduled. Need FU-024C1-5, FU-024C2-1, FU-024C2-2 and FU-024C2-3 closed, a reviewed compose change and a separate owner approval; a migration while the queue can be written also needs FU-024B4A-1 |
| SMM-025, SMM-031 | In the agreed roadmap; detailed specification and acceptance criteria **not yet approved** |
