# SMM-025D — Implementation

Branch `feat/smm-025d-mutations-hardening` from `main` `5f32b50`.
Uncommitted; nothing deployed.

## 1. By phase

| Phase | What | Where |
|---|---|---|
| 1 Audit | baseline `5f32b50`: API writes as `ai_smm_cc_rw` were trusted for approval, lineage, actor and session; `ai_smm_cc` could create sessions; no key packaging | CHECKPOINT.md |
| 2 DB-enforced publication safety (FU-025C-1) | session binding, 25 guard functions, 18 triggers, step-up grants, lineage function used by guards and worker | `src/ai_smm/db/guards.py`, migration `c4d81e2a9f63`, `db/models.py`, `publishing/service.py` |
| 3 Authorization & isolation (FU-024C2-1/2) | owner-only dependencies; smm-auth service (login, logout, step-up preview, step-up grant); grants G2; `written_by`; guard refusals → 403 | `api/auth_service.py`, `api/session_binding.py`, `api/dependencies.py`, `api/errors.py`, `db/*.sql` |
| 3b Packaging | smm-auth in compose (`cc-auth`, static bind, peer allowlist), nginx routes, auth env file + auth role gate in the wrapper | `compose.control-center.yml`, `web/nginx/*`, `deploy/control-center/cc-*.sh` |
| 4 Credentials & config (FU-025C-2) | public key + allow-list via validated compose env to smm-api only; allow-list equal to the worker's (gate); worker read-only key mount; key procedure | `compose.yaml`, `cc-common.sh`, `cc-gate.sh`, `docs/credential-keys.md` |
| 5 Web | owner-only permissions; approve dialog shows smm-auth's preview and asks the password; step-up error codes; wrong password keeps the session | `web/src/**` |
| 5 E2E | sign-in at smm-auth → second project → settings → two accounts + verified credentials → content with destination → refused approval → step-up → approve → materialise → schedule → worker publishes with the chosen account's token only; provenance per audit row | `tests/test_e2e_owner_workflow.py` |
| 6 Verification | full suite, network rehearsal, web | [tests.md](tests.md) |
| 7 Release readiness | ADR-025D, rollout/rollback, security review, residual risks, follow-ups | this directory |

## 2. Autonomous decisions

| # | Decision | Alternative rejected |
|---|---|---|
| D1 | triggers + per-transaction session binding, application logic stays in Python | one SECURITY DEFINER function per command (two implementations of every rule) |
| D2 | approval needs a step-up grant from smm-auth | trusting the cookie (S5 could approve) |
| D3 | guarded = "not a member of the table owner", no role list | listing roles (a new role would be unguarded) |
| D4 | deferred constraint triggers for approved-item and lineage | row triggers (order of statements in one command would matter) |
| D5 | content compared column by column, `content_hash` not recomputed in SQL | recomputing (jsonb serialisation differs from Python's) |
| D6 | owner-only for every business change | per-role guards now (FU-025D-3) |
| D7 | smm-auth bound to a static `cc-auth` address + application allowlist | allowlist alone (ADR-024C-3A §9.3) |
| D8 | `CC_RELEASE_PERMITS_MUTATIONS` stays `no` in this branch | lifting it here (the owner's decision after review) |
| D9 | worker key directory, list of files in `ai-smm.env` | a single literal path (rotation needs two files for a while) |
| D10 | schema downgrade only manually with `AI_SMM_DOWNGRADE_REMOVES_MUTATION_GUARDS=yes` | a wrapper switch (removing protections must not be one command) |

## 3. Defects found and fixed

See [security-review.md §5](security-review.md#5-findings-fixed-during-the-sprint)
(F1–F5) and, from the test side: the FastAPI 0.143 route enumeration
(`iter_route_contexts`), stale verify pins, function-count assertions,
`smm_auth.drop.sql` missing the new functions.
