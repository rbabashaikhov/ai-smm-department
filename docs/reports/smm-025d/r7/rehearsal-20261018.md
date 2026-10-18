# R7 integrated rehearsal — 2026-10-18

## Scope and evidence source

Integration commit: `aab618ae1686534b1a0a471efa12477b8951c576`,
based directly on main `fbf1b1ac02d95474f54f43234b95e8192bcc2fd5`.

The owner reported the successful terminal run of
`scripts/r7_mutation_rehearsal.sh` for the integrated R7 tree. The complete
2026-10-18 terminal transcript is not available inside the Codex sandbox;
the results below record the owner-provided summary, not an independently
replayed transcript. The untracked
`evidence/r7-mutation-rehearsal.log` is the historical 2026-10-15 run and
is not presented as evidence for this integrated run and must not be
included in Git.

## Confirmed rehearsal result

The owner-provided terminal result reports **R7-0 through R7-8 PASS**:

- **R7-0:** initial Control Center mode was read-only; mutation requests
  were refused while reads and authentication remained available.
- **R7-1:** the `ai_smm_cc_rw`, `ai_smm_cc`, and `ai_smm_auth` role
  verification gates completed without a reported failure.
- **R7-2:** direct business writes as `ai_smm_cc_rw` without a bound
  session were rejected by the database guards.
- **R7-3:** the wrapper enabled business mutations using
  `ai_smm_cc_rw`; this was a disposable environment only.
- **R7-4:** the smoke confirmed CSRF/RBAC boundaries, used an isolated
  non-publishing project and left no approval or publication.
- **R7-5:** step-up was required for human approval; incorrect and reused
  approval attempts were rejected. The rehearsal created controlled rows
  only for the subsequent worker-safety checks.
- **R7-6:** `mutations disable` restored read-only mode. As designed, it
  did not cancel rows already scheduled in the disposable database.
- **R7-7:** the publishing worker ran in dry-run mode: the intact approved
  row reached dry-run handling, modified content was rejected by approval
  lineage, and the unreviewed row was blocked. No external publication was
  performed.
- **R7-8:** `ai_smm_cc_rw` was revoked after disable; the read-only role
  remained valid.

Script review confirms that this rehearsal uses disposable
`ai-smm-r7rh-*` resources, an internal Docker network, random test
credentials and `AI_SMM_DRY_RUN=true`. It does not invoke SSH, a production
DSN, a production mutation switch, an external publishing API, or a real
LLM call.

## Cleanup

The owner verified from a normal terminal that no temporary R7 rehearsal
containers, networks or volumes remained under the checked R7 resource
names. The separate disposable PostgreSQL 16 instance used for DB tests
was also removed.

Codex did not independently verify Docker cleanup because the sandbox was
denied access to the `desktop-linux` Docker socket. The cleanup result in
this section is owner-provided evidence.

## PostgreSQL-backed targeted tests

The first run of the six targeted modules completed with **357 passed,
2 failed and 20 errors**. Both connection variables incorrectly pointed
to the PostgreSQL `postgres` superuser; this did not exercise the intended
split between the ordinary database owner and the test-cluster admin.

The owner then created the ordinary role `ai_smm_test_owner`, recreated the
disposable database with that owner, and repeated the 22 affected tests.
The rerun result was **22 passed in 9.12s**. The original 357 passing tests
were not rerun. No application code or SQL contract was changed between
the first run and the focused rerun.

The six targeted modules were:

- `tests/test_db_least_privilege.py`
- `tests/test_db_mutation_role.py`
- `tests/test_db_generation_role.py`
- `tests/test_api_mutation_gate.py`
- `tests/test_migration_guards.py`
- `tests/test_migrations.py`

## Git and readiness conclusion

The validated integration commit is
`aab618ae1686534b1a0a471efa12477b8951c576`.

The local R7 integration is technically ready to complete Git integration.
Production rollout remains blocked until its separate approval and all
production prerequisites are completed, including Group O acceptance,
verified backup/off-host recovery evidence, the approved migration window,
role provisioning, read-only deployment checks and a separate decision to
enable mutations. Git integration is not a production deployment.

## Reproduction command

With environment variables pointing to one disposable PostgreSQL 16
database whose name contains `test`, the targeted DB command for the owner
terminal is:

```bash
AI_SMM_TEST_DATABASE_URL="$AI_SMM_TEST_DATABASE_URL" \
AI_SMM_TEST_ADMIN_DATABASE_URL="$AI_SMM_TEST_ADMIN_DATABASE_URL" \
PYTHONPATH="$PWD" \
  /home/ruslan/Documents/OTUS-LLM-Engeneering/13/ai-smm-department/.venv/bin/python \
  -m pytest -q \
  tests/test_db_least_privilege.py \
  tests/test_db_mutation_role.py \
  tests/test_db_generation_role.py \
  tests/test_api_mutation_gate.py \
  tests/test_migration_guards.py \
  tests/test_migrations.py
```

Both URLs must identify the same disposable database; the admin URL must
use its test-cluster superuser, while the test URL must use the ordinary
database owner rather than that superuser. Never point this suite at
production: its fixtures drop, recreate and truncate application tables
and create/drop test roles.
