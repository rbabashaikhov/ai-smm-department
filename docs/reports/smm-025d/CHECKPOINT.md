# SMM-025D — working checkpoint

Updated as the sprint goes; the last section is always the next step.
**Nothing here is committed, pushed, merged or deployed. Production is
untouched.**

| | |
|---|---|
| Branch | `feat/smm-025d-mutations-hardening` (from `main` `5f32b50`), uncommitted |
| Test DB | disposable PG 16 on `127.0.0.1:55435` (container `ai-smm-025b-pg2`), runner `scratchpad/pt.sh` (secrets unset, dotenv off) |
| Full suite last green | see `tests.md` (final run of 2026-10-14) |

## Design decisions (for the ADR still to be written)

1. **DB guards instead of one SECURITY DEFINER function per command.** A
   guarded transaction must call `guard_bind_session(raw token)`; it
   checks the session like `smm_auth.session_resolve` and records
   txid → user/session in `mutation_contexts` (no guarded role can touch
   it). Triggers on every business table then require a bound OWNER of
   the row's project and force actor/creator columns. Guarded = the
   statement's `current_user` is not a member of the table owner (worker,
   CLI, migrate, SECURITY DEFINER bodies, superusers are not guarded).
   Source: `src/ai_smm/db/guards.py` (versioned `FUNCTIONS_V1`,
   `TRIGGERS_V1`); migration `c4d81e2a9f63`; `models.py` builds the same
   for `create_all`.
2. **Step-up approval (S5 boundary).** An approval by a guarded role
   consumes an `approval_grants` row (5 min, this user, this session, this
   revision, pinned destination identity). Only
   `smm_auth.approval_grant_issue` creates one, and only smm-auth calls it,
   after re-verifying the owner's password; the preview the owner confirms
   comes from `smm_auth.approval_preview`. A compromised smm-api can
   therefore not approve even with the owner's cookie.
3. **Publication lineage (FU-025C-1).** A guarded INSERT is an approved,
   unscheduled snapshot; at COMMIT it must have a link to the approved
   revision with identical content and destination
   (`guard_publication_problem`). Updates are only the three queue
   commands; never a claim/outcome column. A link only for a publication
   created in the same transaction. The worker asks the same function
   before any external call (`publishing/service.py`) and fails the row
   permanently (`ApprovalLineage`).
4. **Audit provenance (FU-024C2-2).** `audit_log.written_by` = `session_user`,
   forced by trigger (historical rows NULL). Business role: business
   actions only, actor = bound user. Role without business rights
   (read-only API before smm-auth): `auth.*` only.
5. **Owner-only business mutations** in the API (dependencies) and in the
   DB guards.
6. **smm-auth** (`src/ai_smm/api/auth_service.py`, `create_auth_app`):
   login / logout / step-up preview / step-up grant + health only, as
   `ai_smm_auth`, via smm_auth functions; failure-count rate limit (10 per
   address / 15 min), Argon2 semaphore (2), no opportunistic rehash.
7. Session resolution for reads stays in Python for now (RS-1/RS-2 of
   ADR-024C-3A accepted: identity for every write is decided in SQL by the
   binding). To be recorded as an ADR amendment.
8. `hide_parameters=True` on every engine.

## Implemented (files)

- `src/ai_smm/db/guards.py`, `alembic/versions/c4d81e2a9f63_business_mutation_guards.py`,
  `db/models.py` (`MutationContext`, `ApprovalGrant`, `AuditLog.written_by`,
  guard DDL listeners), `preflight.py` (expected revision `c4d81e2a9f63`).
- `api/session_binding.py` (+ wired in `api/app.py`), `application/auth.py`
  (`bind_transaction_to_session`), `api/dependencies.py` + routers
  (owner-only aliases), `api/errors.py` (new codes), `db/session.py`.
- `publishing/service.py` (`lineage_problem` before routing).
- SQL: `ai_smm_cc_rw.grants.sql` (requires the SMM-025D schema; EXECUTE on 6
  guard functions), `ai_smm_cc_rw.verify.sql` and `ai_smm_cc.verify.sql`
  (function checks), `smm_auth.functions.sql` (+`approval_preview`,
  `approval_grant_issue`, preflight/postcheck 9 functions),
  `ai_smm_auth.grants.sql`, `ai_smm_auth.verify.sql` (md5 pins),
  `smm_auth.drop.sql`. `pyproject.toml`: E501 off for `guards.py`.
- Tests: new `test_db_guards.py`, `test_migration_guards.py`,
  `test_auth_service.py`; updated API suites (owner-only), mutation-role
  conftest (`open_session` like smm-auth, step-up client), conftest
  (`open_session`, `make_publication(lineage=...)`), migration tests
  (downgrade switch), least-privilege / mutation-role / auth-contract tests.
  `tests/mutation_role/test_mr_api_auth.py` removed (login is smm-auth's in
  mutation mode).

## Verified (last runs, all green)

- `tests/test_api_*.py tests/test_content_*.py` — 535 passed (owner role)
- `tests/mutation_role` + `tests/test_api_social_accounts.py` + content — 316 passed
- `tests/test_db_least_privilege.py tests/test_db_mutation_role.py tests/test_db_auth_contracts.py` — 314 passed; auth contracts alone 90
- `tests/test_db_guards.py tests/test_migration_guards.py` — 66 passed
- publishing / worker / series / routing / preflight — 113 passed (+2 lineage tests)
- `tests/test_auth_service.py` — 19 passed
- migrations + preflight + content — 102 passed

## Done since the first checkpoint

- G2 grants for both API roles + verify matrices + pins in `cc-gate.sh`;
  `password_hash` deferred; `api_auth_routes_enabled`; guard refusals → 403
  (`errors.py`); least-privilege / mutation-role / auth-contract /
  control-center suites updated and green (444 + 114).
- `docs/adr/ADR-025D-database-enforced-mutation-safety.md` drafted.

## Done since the second checkpoint

- Packaging of smm-auth: service + `cc-auth` network (`10.231.18.0/29`,
  bind `10.231.18.2` only, healthcheck there), nginx routes + snippet
  `web/nginx/smm-auth-proxy.inc` + Dockerfile COPY, `AI_SMM_API_AUTH_ROUTES:
  "false"`, application peer allowlist (`AI_SMM_AUTH_ALLOWED_PEERS`).
- Wrapper: `control-center.auth.env` (mode 600, one key), auth role gate
  `cc_gate_auth_role` (pinned `ai_smm_auth.verify.sql`, URLs pairwise
  distinct, ≥400 checks), `up`/`restart`/`preflight` run it; pins updated.
- FU-025C-2: `SMM_CC_CREDENTIALS_PUBLIC_KEY` / `SMM_CC_LEGACY_THREADS_PROJECTS`
  in `control-center.compose.env` (validated; allow-list = worker's, checked
  in the identity gate; key id printed), interpolated into smm-api only;
  worker `compose.yaml` read-only key dir mount (worker + cli, not migrate);
  `docs/credential-keys.md` (backup / restore / rotation / revocation).
- Tests: `tests/test_control_center_auth_role_gate.py` (new),
  `tests/mutation_role/test_mr_guard_refusals.py` (403 mapping), packaging /
  wrapper / gates / mutations updated; config + auth service peer tests.
  CC suites: 998 + 8 green.
- **Network rehearsal T-N1..T-N5 PASSED locally**
  (`scripts/cc_auth_network_rehearsal.sh`, images built from the working
  tree as version `00000000000000000000000000000000025d0001`; log
  `docs/reports/smm-025d/evidence/cc-auth-network-rehearsal.log`). T-N4
  reverse (smm-auth → smm-api) is reachable via the shared network, as
  every shared-network container — FU-024B3-2, recorded as INFO.

## Done since the third checkpoint (sprint complete locally)

- Web: owner-only permissions; approve dialog with smm-auth preview +
  password; `STEP_UP_FAILED` no longer signs the user out; new error codes;
  149 vitest green; eslint, tsc, build clean.
- E2E `tests/test_e2e_owner_workflow.py` (2) green.
- Full verification: full suite, ruff, compat (`6c54669`, `5f32b50`), CC
  rollback grants check, network rehearsal on fresh images, secret scan —
  see `tests.md`.
- Docs: ADR-025D final (Proposed, for acceptance), README, implementation,
  security review, rollout & rollback, tests, residual risks,
  credential-keys, follow-ups (FU-025C-1, FU-024C2-1/2, FU-025C-2 done
  technically; FU-025D-1…5 added), api.md RBAC, deployment file table,
  mutation-role §9 status.
- `CC_MUTATIONS_BLOCKED_BY` now names what remains (owner review, smm-auth
  rollout, FU-024C1-5 on the release); the flag stays `no`.

## Remaining

Nothing in the local scope. Needs the owner: review and acceptance; then
commit / merge (not done); production groups A–O of `rollout-rollback.md`,
each approved separately; R7 later.

## Known issues / notes

- The deployed worker `5f32b50` refuses to start on schema `c4d81e2a9f63`
  (preflight equality): the rollout must ship worker N with the migration;
  no older worker may be started on the new schema (review follow-up:
  rollout-rollback.md §1, §3).
- Old API `5f32b50` stays compatible with schema `c4d81e2a9f63` (its
  sign-in audit is `auth.*`, allowed for the read-only role).
- Test cluster leftovers: if `test_db_auth_contracts` aborts mid-run,
  drop `smm_auth` with CASCADE on the disposable cluster before re-running.

## Next command

```bash
# Owner review first. Re-verify before any commit:
S=/tmp/claude-1000/-home-ruslan-Documents-OTUS-LLM-Engeneering-13-ai-smm-department/5a4687f6-60a9-4b11-bddd-742d56160ec4/scratchpad
cd /home/ruslan/Documents/OTUS-LLM-Engeneering/13/ai-smm-department
DOCKER_HOST=unix:///var/run/docker.sock timeout 3500 $S/pt.sh -q -p no:cacheprovider tests/
```
