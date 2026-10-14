# SMM-025D — Test report

All runs local, 2026-10-14, on the uncommitted branch
`feat/smm-025d-mutations-hardening`. No production system, no real token,
no platform call: identity checks go to `FakeIdentityService`, publishing to
`FakePublisher`; the repo's `.env` is never loaded (`PYTHON_DOTENV_DISABLED=1`,
every secret variable unset).

## 1. Environment

| | |
|---|---|
| PostgreSQL | 16.15, disposable container on `127.0.0.1:55435` (`ai_smm_test`), roles `ai_smm_cc`, `ai_smm_cc_rw`, `ai_smm_auth` provisioned by the operator's own SQL files |
| Python | `uv run pytest`, FastAPI 0.143 |
| Web | Node 24.21, vitest, eslint, tsc, vite build |
| Docker | Engine local, Compose v5.4.0 (production runs 5.1.3) |

## 2. Results

| Suite | Command | Result |
|---|---|---|
| **Full Python suite** | `pytest -q tests/` | **2752 passed, 0 failed** (9 min 37 s; 2583 before SMM-025D) |
| ruff | `ruff check src tests alembic` | all checks passed |
| whitespace | `git diff --check main` + new files | clean |
| Web lint / types | `npx eslint .`, `npx tsc --noEmit` | clean |
| Web tests | `npx vitest run` | **149 passed** (11 files; 139 before SMM-025D's UI changes) |
| Web build | `npm run build` | built |
| Deployed worker on the new schema | `scripts/check_worker_schema_compat.sh 6c54669` (image `706b383`) | 232 passed, 1 skipped |
| Deployed worker/API code on the new schema | `scripts/check_worker_schema_compat.sh 5f32b50` (production now) | 1914 passed, 698 skipped (role suites need an admin URL) |
| CC rollback grants | `5f32b50`'s `ai_smm_cc.grants.sql` over G2 on `c4d81e2a9f63`, then its verify file | 913 checks, 0 FAIL; session INSERT, `revoked_at`, audit INSERT, `password_hash` read back |
| **Network rehearsal** | `scripts/cc_auth_network_rehearsal.sh <image version>` (images built from this tree) | **T-N1, T-N2, T-N3, T-N4 (a, b), T-N5 PASSED**; T-N4c (reverse) INFO; log in `evidence/` |
| Secret scan | patterns for OpenAI / Meta / GitHub / AWS keys, PEM private keys, DSNs with a password, over the whole diff and the new files | 0 findings (only fixtures and shell variables) |

## 3. What the new and changed suites prove

| File | Tests | Proves |
|---|---|---|
| `tests/test_db_guards.py` | 57 | direct SQL as `ai_smm_cc_rw`, bound and unbound: every bypass in [security-review.md §2](security-review.md#2-the-bypasses-asked-about-and-the-proof) |
| `tests/test_migration_guards.py` | 5 | migrated guards = `create_all`'s; PUBLIC no EXECUTE; downgrade refused without its switch; down/up; old audit rows keep NULL provenance |
| `tests/test_auth_service.py` | 24 | smm-auth surface (exact routes), sign-in / sign-out through the functions, rate limit, step-up preview and grant (owner only, CSRF, password, state), its role writes nothing directly, peer allowlist |
| `tests/test_e2e_owner_workflow.py` | 2 | Phase 5 end to end (smm-auth + smm-api as `ai_smm_cc_rw` + worker): second project, settings, two accounts + verified credentials, content with destination, refused approval, step-up, approval, snapshot, schedule, worker publishes with the chosen token only, provenance per audit row; destination change ends the approval and an old grant does not carry over |
| `tests/mutation_role/` | 263 | every business suite as `ai_smm_cc_rw` under the guards; `test_mr_guard_refusals.py` (6): an approval without step-up is `403 APPROVAL_CONFIRMATION_REQUIRED` and writes nothing; only guard refusals map to 403 |
| `tests/least_privilege/` | 138 | read suites as `ai_smm_cc` with G2 (no session creation, no audit insert) |
| `tests/test_db_least_privilege.py`, `test_db_mutation_role.py`, `test_db_auth_contracts.py` | — | G2 matrices, residual risks closed by the guards, 9 smm_auth functions, P3 rollback restores everything |
| `tests/test_control_center_auth_role_gate.py` | 35 | auth role gate: guarded verbs, URL rules (role, host, port, db, password, distinct from every other URL), file rules (600, one key, pinned verify), strict output, unreachable DB, live run of the real verify file on the real role, a drifted function body stops `up` |
| `tests/test_control_center_packaging.py` | 74 | smm-auth service, `cc-auth` (fixed subnet, internal), bind + healthcheck address, allowlist literal, nginx routes + snippet + Dockerfile, only smm-api gets the public key, worker key mount read-only and never in migrate, 4 interpolation variables, pins |
| `tests/test_control_center_wrapper.py`, `…_gates.py`, `…_mutations.py`, `…_api_role_gate.py`, backup suites | 238 / 451 / 78 / 48 / … | interpolation file rules (public key, allow-list shapes, placeholder), allow-list equal to the worker's (8 cases), `up` starts three services, the release still refuses `mutations enable` and names what is open |
| `tests/test_publish_routing.py` | +2 | the worker refuses a row whose lineage does not hold, before any external call |
| `tests/test_config.py` | +5 | `AI_SMM_AUTH_ALLOWED_PEERS` parsing (CIDR only) |
| `web/src/tests/content.test.tsx` | +4 | approve shows smm-auth's preview, needs the password, step-up before approve, password never in the approve body; wrong password keeps the session; mismatched preview disables; DB refusal explained |
| `web/src/tests/rbac.test.tsx` | rewritten | editor and admin are offered no business change; owner is |

## 4. Limitations

- Compose v5.4.0 locally, 5.1.3 in production: the rehearsal must be
  repeated, read-only parts first, as T-N6 after the production `up`.
- The rehearsal's database is a fresh migration, not a production copy.
- The step-up UI is tested in vitest against the fake API, not in a browser
  against the rehearsal stack.
- `5f32b50`'s role suites skipped in the compat run (no admin URL), so the
  old API as `ai_smm_cc` on the new schema is covered by the grants/verify
  check and by design, not by its suite (residual R-6).
