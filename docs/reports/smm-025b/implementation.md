# SMM-025B — Implementation Report

Branch `feat/smm-025b-multi-project-accounts` from `main` @ `6fc6d22`. **Not committed, not pushed.** Architecture: [docs/architecture/smm-025b-project-management.md](../../architecture/smm-025b-project-management.md).

## 1. B1 — projects, accounts, credentials manager

| Feature | Where |
|---|---|
| Atomic create: project + owner membership + settings v1 + audit, or nothing; idempotent replay; concurrent-create safe (savepoint) | `application/projects.py` `create_project` |
| Project id rule (one place, no dependencies) | `application/project_ids.py` |
| Edit record with optimistic version (`projects.version`, previously unused) | `application/projects.py` `update_project_record`, `PATCH /projects/{id}` |
| Typed settings keys `content_config.topics`, `brand_config.tone_of_voice`; 16 KiB cap; unknown keys preserved | `application/projects.py` `validate_settings_sections` |
| `POST /projects`, `PATCH /projects/{id}`; `ProjectOut.legacy_publishing` | `api/routers/projects.py`, `api/schemas.py` |
| `import-json` refuses unknown projects, no longer renames/repaths | `importer.py`, `cli.py` |
| Knowledge loader: id validation + containment | `knowledge/loader.py` |
| Media isolation: revision images only from `knowledge/projects/<id>/assets/` | `application/content.py` `validate_image_paths` |
| Social accounts: create, rename, disable/enable (versioned), several per platform, readiness | `application/social_accounts.py`, `api/routers/social_accounts.py` |
| Credentials: write-only, sealed with the public key, connection test (`GET /me`) binds/compares identity, supersede wipes ciphertext, revoke wipes ciphertext | same + `security/credentials.py`, `platform_identity.py` |
| No implicit binding of a new project to the deployment token | `account_routing.py` legacy allow-list (`AI_SMM_LEGACY_THREADS_PROJECTS`, default empty) |
| CLI: `project create`, `credentials keygen`, `accounts verify` | `cli.py` |
| UI: create project, settings (two versioned cards), accounts, separate credentials manager, destination select, account shown on content and publications | `web/src/pages/{CreateProject,ProjectSettings,Accounts,Credentials}Page.tsx`, `features/projects/TopicsInput.tsx`, `features/accounts/labels.ts`, `features/content/RevisionForm.tsx`, routes, layout, `AuthProvider.refreshMe` |

## 2. B2 — account-aware publishing

| Feature | Where |
|---|---|
| `publications.social_account_id` (composite FK to the project's accounts) and `content_revisions.social_account_id` | `db/models.py`, migration `a7c2e5f19b40` |
| Destination is part of the revision and its hash → approval covers it; account change = new revision = new approval | `application/content.py` (`content_hash`, `_build_revision`, `create_revision`) |
| Submit and materialise require a destination (except allow-listed legacy projects); materialise re-checks the account | `application/content.py` |
| Route policy (pure, importable by the control plane) | `account_routing.py` |
| Token decryption (worker side only) + runtime log masking | `publishing/routing.py` |
| Publish pipeline: route → (dry-run) → identity check → account publisher; legacy publisher only for legacy rows; failures before the point of no return | `publishing/service.py` |
| `ThreadsPublisher(access_token=…)` never reads the environment when given a token | `publishing/threads.py` |
| Worker: opener loaded at start (bad key file = refuses to start), startup warning for legacy rows that will be refused | `worker.py` |
| Claim declares account routing (transaction-local `set_config`) | `queue.py` |
| Old-worker claim guard trigger; downgrade guard | `db/models.py`, migration |
| Series/replies: one account per series, reply must be in the parent's account | `series.py` |
| Scheduling refused for an unready route (API + CLI `schedule`/`publish-now`) | `application/publications.py`, `cli.py` |
| CC role: `SELECT` on `social_accounts`, column `SELECT` on credential metadata only; verify file + gate pin | `deploy/control-center/db/ai_smm_cc.{grants,verify}.sql`, `cc-gate.sh` |

Queue semantics, leases, retries, `needs_review`, idempotency keys and the duplicate barriers are unchanged.

## 3. Decisions taken autonomously (and why)

| Decision | Reason |
|---|---|
| Account belongs to one project; same Threads account in two projects = two rows | RBAC stays project-scoped; no owner-level object needed |
| Destination stored in the immutable revision (not on the item) | reuses the existing "approval = this exact revision" invariant; no new invalidation logic |
| Sealed box (public key in API, private key in worker) instead of a symmetric key | a compromised API or its DB role cannot decrypt anything |
| Connection test only at entry in the API; worker re-verifies before each live post | API cannot decrypt; the worker check is the authoritative one |
| Legacy allow-list as configuration, default empty | a new project can never reach the deployment token implicitly; fail-closed |
| Route failures = permanent failure (row `failed`, nothing sent) | needs a human (fix account/credential/config, reschedule); visible in Attention |
| Split route policy (`ai_smm.account_routing`) from decryption (`ai_smm.publishing.routing`) and identity check (`ai_smm.platform_identity`) | keeps the existing invariant that the control plane cannot import anything in the publishing layer |
| Status/route columns as `String` + `CHECK`, not PG enums | simpler, reversible migrations |
| Migration has no own `lock_timeout` | follows the existing `PGOPTIONS` mechanism of the `migrate` service |

## 4. Defects found and fixed during the sprint

| # | Found by | Defect | Fix | Regression test |
|---|---|---|---|---|
| D1 | `test_api_contract.py` | API imported `ai_smm.publishing.*` (identity check, routing) — violated the "control plane cannot reach the publisher" invariant | moved policy to `account_routing.py`, identity check to `platform_identity.py`; decryption stays worker-side | existing contract tests |
| D2 | LP suite as `ai_smm_cc` | ORM loaded credential rows whole (incl. ciphertext): account reads failed under the least-privilege role | `credential_metadata_only()` on every control-plane read | `test_the_session_lifecycle_and_every_read_work_through_the_role` with the new account paths |
| D3 | `test_migrations.py` lock-timeout test | migration set its own `lock_timeout`, overriding the operator's `PGOPTIONS` bound | removed | `test_a_lock_timeout_rolls_back_every_pending_revision` |
| D4 | self-review | `set_credential` held the account row lock during the network call | connection test before the lock | `test_a_credential_is_tested_before_the_account_row_is_locked` |
| D5 | self-review | silent rollout failure if the legacy allow-list is forgotten | first a startup warning; **superseded by D11** (fail-closed preflight), the warning was removed | see D11 |
| D6 | tests | test settings were cached before the test allow-list was set (`get_logger` at import calls `get_settings`) | `conftest.py` sets the variable before any `ai_smm` import | whole suite |
| D7 | vitest under load | pre-existing race in `auth.test.tsx` (asserted call order before the page's query fired) | wait for the call, then assert the order | 10× vitest under concurrent load |
| D9 | self-review | an `assert route.token is not None` guarded the account path; under `python -O` a missing token would have reached `ThreadsPublisher(access_token=None)`, which reads the **deployment** token (unreachable today, but the wrong kind of guard for "never fall back") | explicit refusal in the pipeline, and the default account factory refuses anything but a non-empty `SecretToken` | `test_the_default_account_publisher_refuses_to_run_without_a_token`, `test_an_account_route_without_a_token_is_refused_not_asserted` |
| D8 | test infra | import-time `load_dotenv()` loaded the developer's real `.env` into tests (observed with `test_config.py`) | sprint runs use `PYTHON_DOTENV_DISABLED=1`; code fixed in D10 | see D10 |

### 4.1 Review follow-ups (independent review: ACCEPT WITH FOLLOW-UPS)

| # | Requested / found | Change | Regression tests |
|---|---|---|---|
| D10 | review (OI-6): no import-time `.env` loading | `load_dotenv()` removed from `publishing/threads.py` and `storage.py`; new `ai_smm.local_env.load_local_dotenv()` (same search start, never overrides, honours `PYTHON_DOTENV_DISABLED`) called only by the entry points `ai-smm` (`cli.main`) and `ai-smm-worker` (`worker.main`) and by the two local scripts that relied on the implicit load (`scripts/test_storage_upload.py`, `scripts/test_threads_image_upload.py`). Production unchanged: images never contain `.env` (`.dockerignore`), containers get their environment from compose, and an already-set variable is never overridden. Side effect, intended: the HTTP API no longer reads a developer's `.env` (it used to, through `application → publishing.service → publishing.threads`) | `tests/test_local_env.py` (7): a fresh interpreter importing nine modules with dotenv's functions replaced by spies makes 0 calls; AST check for module-level calls; loader reads/never overrides/honours the switch/no file; both entry points load before reading settings. Mutation-checked: re-adding the import-time call fails 2 tests |
| D11 | review (legacy rollout): mandatory fail-closed preflight | `ai_smm/preflight.py`; `Worker.run()` runs it **before** recovery, scheduler and any claim and raises `PreflightFailed`; `worker.main()` exits **78** on it (and on a bad key file); `ai-smm preflight worker` for the operator (exit 1 on FAIL, no secret printed). FAIL on: schema ≠ `a7c2e5f19b40`, unknown allow-listed project, pending legacy row outside the allow-list, live mode without deployment token for allow-listed pending legacy rows, active credential key id without private key. INFO only for owner choices (disabled account, revoked credential) | `tests/test_preflight.py` (13), incl. `test_a_misconfigured_worker_never_claims_and_leaves_the_queue_as_it_was` (rows stay `scheduled`, 0 attempts) and `test_the_expected_revision_is_the_alembic_head` |
| D12 | review (rollback doc) | downgrade over existing account/credential data refused unless `AI_SMM_DOWNGRADE_DESTROYS_ACCOUNT_DATA=yes`; the live-row refusal stays without override; migration-rollback §4 now prefers application rollback and lists exactly what a downgrade destroys | `test_a_downgrade_that_would_destroy_account_data_needs_the_explicit_switch` (4 switch values), `test_the_destroy_switch_never_overrides_the_live_row_refusal` |
| D13 | self-review during D11 | **regression from OD-4:** an empty installation could not be bootstrapped (`import-json` no longer creates projects, `project create` needs a user, `create-owner` needs a project); the local runtime's documented step 4 would fail. Production unaffected (its project and owner exist; the CC wrapper's `create-owner` passes exactly three options) | `ai-smm user create-owner … --create-project NAME` creates user + project + owner membership + settings + audit in one transaction via `create_project`; refused for an existing project; `compose.runtime.yml` usage comment and `docs/runtime-packaging.md` updated | `tests/test_cli_user.py` (+5) |
| D14 | final review | `assert credential is not None` in `publishing/routing.py` (and two asserts of the same kind in `account_routing.py`) would vanish under `python -O`; a credential revoked or replaced between the readiness check and decryption would also have reached `bytes(None)` | explicit `RouteUnavailable(no_active_credential)` when the credential is missing, not active, of another account or without material; same in the policy | `test_a_credential_gone_before_decryption_is_refused` (missing / revoked / other account), `test_the_pipeline_fails_a_row_whose_credential_vanished`, `test_the_refusals_hold_under_python_dash_o` (a `python -O` interpreter; mutation-checked: the assert fails it) |
| D15 | pre-commit full run | `test_existing_rows_keep_their_meaning` read `publications` on a connection it never closed; when it was not garbage-collected in time, its open read transaction held a lock and the upgrade's `ALTER TABLE` waited forever (the scratch DB has no `lock_timeout`; production `migrate` has one). Test bug only | the read uses `with engine.connect()` | the suite itself (full run green) |

## 5. Changed files

Backend (new): `account_routing.py`, `preflight.py`, `local_env.py`, `platform_identity.py`, `security/credentials.py`, `publishing/routing.py`, `application/project_ids.py`, `application/social_accounts.py`, `api/routers/social_accounts.py`, `alembic/versions/a7c2e5f19b40_social_accounts_credentials.py`.
Backend (modified): `db/models.py`, `config.py`, `queue.py`, `worker.py`, `series.py`, `importer.py`, `cli.py`, `logging_setup.py`, `knowledge/loader.py`, `publishing/{service,threads}.py`, `application/{projects,content,publications}.py`, `api/{app,dependencies,errors,schemas}.py`, `api/routers/{projects,content,publications}.py`, `pyproject.toml`/`uv.lock` (direct `cryptography` dependency).
Deployment artifacts (local only): `deploy/control-center/db/ai_smm_cc.{grants,verify}.sql`, `deploy/control-center/cc-gate.sh` (verify pin).
Web (new): 4 pages, `TopicsInput`, `accounts/labels.ts`, `tests/projectManagement.test.tsx`. Web (modified): client, endpoints, errors, query keys, types, rbac, AuthProvider, RevisionForm, hooks, layout, routes, content/publication/projects pages, fixtures, `auth.test.tsx`.
Tests (new): 6 files; (modified): `conftest.py`, contract, mutation gate, least privilege, importer, CLI, migrations, materialize, links.
Docs: architecture (rev. 2), these reports, `docs/api.md`, `docs/operations.md`.
