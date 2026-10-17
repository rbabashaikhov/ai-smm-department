# SMM-026A/2.1-FINAL — Hash Integrity & Full Regression

Date: 2026-10-17. Worktree: `ai-smm-department-smm026a`; branch:
`feat/smm-026a-agent-stabilization`; unchanged HEAD:
`cc2c4d2a8a0e21e7788a09031e711342e7a6aaf8`.

No commit, push, merge, production/VPS change, R7/Group O change, real LLM
call, or publication was performed.

## Result

SF-01 is closed. `generation_complete` no longer trusts the caller's hash or
canonical text. The trusted PostgreSQL contract constructs the exact canonical
ApprovalLineage text, calculates SHA-256 over its UTF-8 bytes, compares it with
the supplied hash, and stores only the DB-computed value.

The completed full regression run passed:

- **2921 passed, 1 skipped**;
- pytest time: **1207.14 s (20:07)**;
- wrapper time: **1210 s** (`11:56:40Z`–`12:16:50Z`);
- exit code: **0**;
- [full log](pytest-full-sf01-final2-pg16.log);
- [machine-readable status](pytest-full-sf01-final2-pg16.status).

During the post-run security review, one missing lease predicate was found in
`generation_fail`. It was added without changing the function signature, ACL,
or successful execution path. Per the one-full-run constraint, the full suite
was not repeated. The exact final code was instead verified by the directly
related role/queue/worker/migration targeted suite: **48 passed**, pytest
30.14 s, wrapper 31 s, exit 0 ([log](pytest-targeted-final-security-pg16.log),
[status](pytest-targeted-final-security-pg16.status)).

## SF-01 design and tests

ApprovalLineage uses Python
`json.dumps(sort_keys=True, ensure_ascii=False, separators=(",", ":"))` and
SHA-256 of UTF-8. PostgreSQL `jsonb::text` is not compatible for every number
(for example exponent formatting and negative zero), so no second hashing
algorithm was introduced.

The generation worker currently creates text-only revisions with revision
`title = NULL`, `images = []`, and `items = []`. The DB contract now:

1. rejects empty/whitespace-only body and non-empty generation `images/items`;
2. JSON-quotes the body and format in PostgreSQL and assembles the fields in
   the same sorted order as ApprovalLineage;
3. calculates `sha256(convert_to(canonical, 'UTF8'))` inside the
   `SECURITY DEFINER` function;
4. rejects any supplied hash that differs and writes the computed hash.

This keeps existing human draft/revision rich-media contracts unchanged. It
also avoids accepting caller-selected canonical bytes that merely decode to an
equivalent JSON value. The SQL uses no dynamic SQL; JSON string quoting blocks
SQL/JSON injection.

Final SF-01 targeted result: **49 passed**, pytest 19.02 s, wrapper 21 s,
exit 0 ([log](pytest-targeted-sf01-canonical-final-pg16.log),
[status](pytest-targeted-sf01-canonical-final-pg16.status)). Coverage includes:

- direct `generation_complete` calls as `ai_smm_generation`;
- a valid-looking hash of different text;
- empty string, spaces, and newline/tab-only input;
- non-empty images/items rejected at the safe canonicalization boundary;
- Unicode, combining characters, and two drafts in one job;
- persisted hashes compared with the application `content_hash` result;
- worker behavior and Alembic/`create_all` function parity.

Two intermediate diagnostics are retained in the evidence directory: an
early whitespace case exposed PostgreSQL `btrim` semantics, and the first
canonical DDL attempt exposed SQLAlchemy's `:name` bind parsing inside a
literal. Both causes were corrected before the final targeted and full runs.
An earlier pre-final full run was intentionally interrupted at 261 passes
after the equivalent-noncanonical-JSON bypass was identified; it is not counted
as a completed regression run.

## PostgreSQL security review

- `ai_smm_generation` remains `LOGIN NOINHERIT`, `NOSUPERUSER`,
  `NOBYPASSRLS`, `NOCREATEDB`, with no memberships or owned objects.
- The role has no table/view/sequence rights and cannot read users,
  `password_hash`, credentials, or sessions, and cannot create approvals or
  create/update publication jobs.
- Its only application privileges are schema `USAGE` and `EXECUTE` on
  `generation_claim`, `generation_complete`, and `generation_fail`. No SF-01
  table grant was added.
- All three contracts are `SECURITY DEFINER`, owned by the independent
  non-superuser application owner, have fixed
  `search_path = pg_catalog, pg_temp`, are revoked from `PUBLIC`, fully qualify
  application objects, and contain no dynamic SQL.
- Claim atomically locks a due job and increments `attempt_count`. Complete and
  fail lock the exact job and require status `claimed`, matching worker, and an
  unexpired lease. Wrong-worker and expired-lease failure calls are no-ops.
- Complete derives project/provenance/editor metadata from the locked job and
  accepts no actor capable of impersonating a human approval. Duplicate
  completion is a no-op.
- API/Control Center writes require NULL `generation_job_id`; DB guards reject
  forged or cross-project provenance and make provenance immutable.
- Retryable and terminal failures are separate. Expired final claims become
  terminal, every claim consumes an attempt, `_write_drafts`
  `GenerationFailed` is handled immediately, and tests cover crash recovery,
  exhaustion, and duplicate prevention.

The role-definition/operator verification and direct negative capability tests
pass as part of the full and targeted suites.

## Migration and environment

Alembic head is `e04cb3a748f5`; `c4d81e2a9f63` remains the separate
`GUARDS_REVISION`. Migration and guard tests cover clean upgrade, protected
downgrade, switch-enabled downgrade/upgrade, guard parity, PUBLIC EXECUTE
revocation, audit compatibility, and unchanged lock-timeout behavior. They
passed in the full suite and both relevant targeted suites. Migration and
`create_all` install the same `GENERATION_CONTRACTS` source.

Tests used only local disposable PostgreSQL **16.15** on
`127.0.0.1:55437`, database `ai_smm_test`. Its fixture owner `ai_smm` was
confirmed `CREATEDB`, `NOSUPERUSER`, and without `BYPASSRLS`. `CREATEDB` is
needed only because migration/least-privilege fixtures create scratch
databases. No production role was changed. The pre-existing PostgreSQL 13 on
port 55436 was not changed or stopped.

No production publishing implementation file changed. `ruff check src tests
alembic` and `git diff --check` pass. A high-confidence secret scan found only
unchanged redaction tests containing deliberate fake markers; no changed
`.env`, private key, credential file, or live-looking API key was found.

## Exact worktree composition

There are **22 modified tracked files**, **74 untracked files**, and **0 staged
files**: 96 dirty filesystem paths in total. The 36 implementation/test paths
are listed below; the remaining 60 are preserved reports and evidence.

Modified tracked (22):

```text
deploy/control-center/cc-gate.sh
deploy/control-center/db/ai_smm_cc.grants.sql
deploy/control-center/db/ai_smm_cc.verify.sql
deploy/control-center/db/ai_smm_cc_rw.grants.sql
deploy/control-center/db/ai_smm_cc_rw.verify.sql
src/ai_smm/api/app.py
src/ai_smm/api/dependencies.py
src/ai_smm/api/schemas.py
src/ai_smm/application/content.py
src/ai_smm/cli.py
src/ai_smm/config.py
src/ai_smm/db/guards.py
src/ai_smm/db/models.py
src/ai_smm/preflight.py
tests/conftest.py
tests/pg_roles.py
tests/test_api_contract.py
tests/test_api_mutation_gate.py
tests/test_db_least_privilege.py
tests/test_db_mutation_role.py
tests/test_migration_guards.py
tests/test_migrations.py
```

Untracked implementation/test files (14):

```text
alembic/versions/e04cb3a748f5_content_generation_jobs.py
deploy/control-center/db/ai_smm_generation.grants.sql
deploy/control-center/db/ai_smm_generation.revoke.sql
deploy/control-center/db/ai_smm_generation.verify.sql
src/ai_smm/api/routers/generation.py
src/ai_smm/application/generation.py
src/ai_smm/db/generation_contracts.py
src/ai_smm/generation_queue.py
src/ai_smm/generation_worker.py
tests/test_api_generation.py
tests/test_db_generation_role.py
tests/test_db_guards_generation.py
tests/test_generation_queue.py
tests/test_generation_worker.py
```

Untracked reports/evidence (60): 22 files in this final report directory, 22
functional-E2E files, 10 Part 1 files, 4 Part 1b files, and the two earlier
Part 2 reports. All inherited evidence was preserved.

The verified external backup remains at
`/home/ruslan/smm026a-backups/smm026a-uncommitted-backup-20261017-toiLhJ`
(73-file checkpoint); its original `/tmp` copy was retained.

## Readiness and residual blockers

No known P0/P1 security blocker remains. The changes are ready for commit and
code review, subject to reviewer acceptance of the explicit text-only boundary
for generation drafts. The ordinary human revision contract remains the path
for rich media.

The only verification caveat is that the successful full suite preceded the
final one-condition expired-lease guard in `generation_fail`; the exact final
code, migration parity, role behavior, retry behavior, and worker behavior all
passed the 48-test targeted suite after that change. No commit was made.
