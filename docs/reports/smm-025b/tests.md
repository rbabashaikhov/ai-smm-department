# SMM-025B — Test Report

All runs local, 2026-10-13. No real platform, no real credential, no production system.

## 1. Environment

- **PostgreSQL 16.15** (`postgres:16.15-bookworm`, the production version) in a disposable container on `127.0.0.1:55435`, data on tmpfs, laid out like production (`docs/least-privilege-role.md` §6): superuser `postgres`, owner `ai_smm` **non-superuser** with `CREATEDB` (for the migration scratch databases), a neighbour database.
- Python via `uv run pytest`; Node 24.21.0 for the web app.
- **Secrets isolation for every Python run:** `PYTHON_DOTENV_DISABLED=1` (the repository `.env` holds live tokens and is loaded at import by two modules — OI-6), every secret variable unset, `AI_SMM_DRY_RUN=true`. Platform calls are fakes (`FakeIdentityService`, `FakePublisher`, account/legacy factory recorders); test tokens are random strings.

```bash
export PYTHON_DOTENV_DISABLED=1 AI_SMM_DRY_RUN=true
unset OPENAI_API_KEY LANGFUSE_SECRET_KEY LANGFUSE_PUBLIC_KEY THREADS_ACCESS_TOKEN STORAGE_SSH_PASSWORD ...
AI_SMM_TEST_DATABASE_URL=postgresql+psycopg://ai_smm:…@127.0.0.1:55435/ai_smm_test \
AI_SMM_TEST_ADMIN_DATABASE_URL=postgresql+psycopg://postgres:…@127.0.0.1:55435/ai_smm_test \
  uv run pytest -q -rfE
```

## 2. Results

| Suite | Baseline `main` @ `6fc6d22` | Branch (final) |
|---|---|---|
| Full Python suite (incl. all least-privilege suites, API as `ai_smm_cc` where they apply) | **1917 passed**, 1 skipped, 0 failed | **2199 passed**, 0 failed, 0 skipped (final, at commit; 2165 before the review follow-ups) |
| `ruff check src tests` | clean | clean |
| Deployed worker `6c54669`'s own suite on this branch's migrated head schema (`scripts/check_worker_schema_compat.sh 6c54669`) | 232 passed, 1 skipped (recorded at SMM-024B) | **232 passed, 1 skipped** |
| Web: `eslint .` | clean | clean |
| Web: `tsc --noEmit` | clean | clean |
| Web: `vitest run` | 121 passed (10× stable) | **139 passed**, 10/10 runs under concurrent CPU load; 3/3 at commit |
| Web: `vite build` | ok | ok |

Baseline was run from a clean worktree of `main` against its own database on the same cluster. A first baseline attempt with a superuser owner produced 91 failures in `test_db_auth_contracts.py` — those contract tests deliberately refuse a superuser table owner; the cluster was rebuilt in the production layout and the baseline re-run (above).

## 3. New test functions (Python 131, web 18)

| File | Tests | Covers |
|---|---|---|
| `tests/test_credentials_crypto.py` | 18 (26 with parameters) | seal/open, per-message randomness, AAD binding (row swap), tamper detection, wrong key, rotation by key id, unknown scheme, `SecretToken` never printable, fingerprint, malformed tokens not echoed, key file 600/`O_EXCL`/group-readable refusal |
| `tests/test_publish_routing.py` | 34 | own token only; two accounts two tokens; attempt details without token; disabled / revoked / no key / foreign key / swapped ciphertext → failed, no fallback; identity mismatch / rejected / unreachable (retry); retry keeps the account; dry-run decrypts but sends nothing; legacy allow-list on/off/default/invalid; account factory never reads the env token; refuses a missing token (no assert); old-worker claim refused by the DB, legacy claim still works, routing flag transaction-local; scheduling route checks; check without decryption; composite FK; series one account; reply across accounts not ready; worker end-to-end; group-readable key refuses start; connection test before row lock; no assert-based token guard; credential missing / revoked / of another account at decryption → refused (D14), and the same under `python -O` in a separate interpreter |
| `tests/test_api_projects_create.py` | 18 (44) | create + owner + settings + audit; no publication, nothing claimable; idempotent replay; id/name conflicts; no takeover of another owner's id; role matrix; 20 invalid inputs; failure injection leaves nothing; concurrent creates; read-only refusal; versioned edit + audit diff; edit role matrix; non-member 404; immutable fields; typed settings keys; CLI create |
| `tests/test_api_social_accounts.py` | 19 (23) | several accounts; owner-only management, member reads; cross-project 404; rename/disable/enable with versions; scheduled-rows warning; verified token binds identity, never returned, not in ciphertext; supersede wipes material; identity mismatch / already connected / rejected / unreachable store nothing; unverified not publishable; revoke wipes; DB CHECK and one-active index; no public key → 503; malformed tokens not echoed; OpenAPI walk; token absent from audit and logs; runtime masking |
| `tests/test_content_destination.py` | 12 (17) | account in revision and hash, old hashes unchanged; account change → new approval; materialise carries account; destination required; materialise re-checks; legacy allow-list removal; foreign / disabled account refused; image path isolation; API round trip; submit refused without destination |
| `tests/test_migration_social_accounts.py` | 5 (7 after the follow-ups) | rows keep meaning; exact additions to `publications`; old-worker claim on migrated DB; downgrade refused while live; downgrade restores schema; repeatable |
| `tests/test_local_env.py` (review follow-up OI-6) | 7 | a fresh interpreter importing nine package modules with dotenv's functions replaced by spies: 0 calls (never reads a real file); no module-level `load_dotenv` call in the package (AST); the loader reads, never overrides, honours `PYTHON_DOTENV_DISABLED`, tolerates no file; `ai-smm` and `ai-smm-worker` load before reading settings. Mutation-checked: re-adding the import-time call in `storage.py` fails 2 tests |
| `tests/test_preflight.py` (review follow-up, legacy rollout) | 13 | fitting configuration passes; forgotten allow-list with pending legacy rows, unknown allow-listed project, live without deployment token (dry run: not needed), credentials without / with the wrong private key, wrong or missing schema revision → FAIL; owner choices → INFO only; no secret in any line; expected revision == Alembic head; **a misconfigured worker's `run()` raises before claiming — rows stay `scheduled`, 0 attempts**; `main()` exits 78 on a failed preflight and on a bad key file; operator command exit codes and output |
| `tests/test_migration_social_accounts.py` (review follow-up) | +2 (+5 with parameters) | downgrade over account data refused without `AI_SMM_DOWNGRADE_DESTROYS_ACCOUNT_DATA=yes` (unset, empty, `no`, `true`), data kept; the switch never overrides the live-row refusal |
| `tests/test_cli_user.py` (bootstrap regression D13) | +3 (+5) | `create-owner --create-project` creates user, project, owner membership and settings in one transaction; refused for an existing project; invalid ids create nothing |
| `web/src/tests/projectManagement.test.tsx` | 18 | slug suggestion; create flow + `/auth/me` refresh + no owner/role in body; id conflict; invalid id; not offered to non-owners / read-only; settings save keeps unknown keys; version conflict panel; viewer read-only; accounts list/add/disable with version; editor read-only; credential sent once, field cleared, token absent from DOM and both TanStack caches; mismatch message; revoke confirmation; owner-only; destination select and display |

Existing tests changed (intentional behaviour changes only): route inventory (+6 paths), `GATED` (+8 unsafe routes, so every one is proven refused read-only and refused by PostgreSQL for `ai_smm_cc`), privilege matrix (+`social_accounts`, credential metadata columns, key-material refusals), importer/CLI (project must exist), materialise/links (project asset paths, audit field), migrations (scope of "publications unchanged", new head), `auth.test.tsx` (race fix D7).

## 4. Limitations

- No test touches the real Threads API: `fetch_identity` and the publisher are exercised only through fakes; the request shape (`GET /me?fields=id,username`, `Authorization: Bearer`) follows the existing client and the platform's documented endpoint.
- The production role suites ran with the API as `ai_smm_cc` only where the existing suites do; a full `AI_SMM_TEST_API_ROLE=restricted` run was not repeated (it fails mutation suites by design).
- Web tests use jsdom and a fake API; no browser end-to-end run.
- Performance of `account_view` (one count query per account) was not measured; accounts per project are capped at 20.
