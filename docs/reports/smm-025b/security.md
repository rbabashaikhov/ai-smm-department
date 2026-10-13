# SMM-025B — Security Report

Scope: the code on branch `feat/smm-025b-multi-project-accounts` (local, uncommitted), against baseline `main` @ `6fc6d22`. No production system, real credential or real platform was used. Every token in the tests is a random test string.

## 1. Assets and trust boundaries

| Asset | Where it lives | Who can read it |
|---|---|---|
| Social account tokens | `social_account_credentials.ciphertext` (sealed) | nobody without the private key |
| Credential private key | file on the worker host (mode 600), off-host encrypted backup | worker + operator CLI only |
| Credential public key | API env `AI_SMM_CREDENTIALS_PUBLIC_KEY` | not secret |
| Deployment Threads token | worker env `THREADS_ACCESS_TOKEN` (unchanged) | worker; used only for allow-listed legacy projects |
| Destination of a post | `content_revisions.social_account_id` → `publications.social_account_id` | written once, never updated by any route |

```
 Browser ──► smm-web ──► smm-api (role ai_smm_cc)        holds PUBLIC key only: seals, cannot open
                              │ SELECT social_accounts, credential METADATA columns only
                              ▼
                         PostgreSQL ── trigger: account rows claimable only by an account-routing worker
                              ▲
 worker / cli (owner role) ───┘ holds PRIVATE key file: opens a token only to publish as that account,
                                after GET /me proves the token is that account
```

## 2. Threats, controls and the tests that prove them

| # | Threat | Controls | Tests |
|---|---|---|---|
| T1 | **Post goes out on the wrong account** | Account is part of the approved immutable revision → copied into the publication; composite FK keeps it in-project; route resolved per row (`ai_smm.account_routing` + `ai_smm.publishing.routing`); separate legacy and account factories, no code path from an account row to the deployment token; identity (`GET /me`) must equal the bound `external_account_id` before any upload or publish; series and replies cannot mix accounts; old-worker claim guard | `test_publish_routing.py` (31): own-token-only, two accounts two tokens, disabled/revoked/no-key/other-key fail with no fallback, identity mismatch, rejected token, retry keeps account, legacy allow-list, guard trigger, series, worker end-to-end; `test_migration_social_accounts.py` |
| T2 | **Human approval bypassed** | Editorial invariants unchanged; destination change = new revision → draft → new approval; submit/materialise refuse a revision without a destination (non-legacy projects); materialise re-checks the account; schedule re-checks the route | `test_content_destination.py` (17), existing approval suites unchanged and green |
| T3 | **Token read back** | No route or response schema has a token/ciphertext field; credentials are write-only; ciphertext NULLed on supersede/revoke (DB CHECK); API role may not SELECT key-material columns (DB grants) — and the API code never selects them (`credential_metadata_only`) | OpenAPI schema walk; response scans; `test_db_least_privilege.py` column matrix + `read key material` refusal; DB CHECK tests |
| T4 | **Token in logs, audit, errors, Langfuse, test output** | `SecretStr` on input → `SecretToken` (masked repr/str/format); validation errors never echo values; audit holds fingerprint only; worker registers every decrypted token with the log redaction filter; identity client errors never contain token/body; Langfuse is not on any of these paths; tests use random tokens and test runs had `.env` loading disabled | `test_the_token_never_reaches_audit_or_logs`, `test_a_decrypted_token_is_masked_in_worker_logs`, crypto `SecretToken` tests, malformed-token echo tests (API + unit) |
| T5 | **Compromised API decrypts tokens** | Sealed box: API holds only the public key. Decryption code is not importable by the control plane (`tests/test_api_contract.py` import-graph invariants, kept) | contract tests |
| T6 | **Ciphertext swapped between rows** (DB writer) | AES-GCM associated data = (account id, credential id) | `test_ciphertexts_swapped_between_accounts_open_for_neither`, crypto tamper tests |
| T7 | **Cross-project access** | Row-first authorisation for accounts and credentials (404 for strangers); revision account must belong to the item's project; composite FK on publications | `test_accounts_of_another_project_are_invisible`, `test_an_account_of_another_project_is_refused`, `test_the_database_refuses_an_account_of_another_project`, project 404 tests |
| T8 | **Wrong token pasted** | Connection test at entry binds/compares identity (409 mismatch, nothing stored); one identity per project; worker re-checks before each live post | `test_a_token_of_another_identity_is_refused_and_nothing_changes`, `test_one_identity_cannot_be_connected_twice_in_a_project` |
| T9 | **Mass assignment / privilege escalation via create** | `extra=forbid`; owner always the caller; creation only for owners of an existing project; accounts/credentials owner-only | `test_invalid_input_is_refused_and_writes_nothing` (20 cases), role matrices |
| T10 | **Path traversal / foreign media** | Project id regex + reserved ids, loader containment; revision images confined to `knowledge/projects/<id>/assets/` | id validation cases, `test_images_must_be_the_projects_own_assets` |
| T11 | **Business writes in production** | Central gate + service-boundary checks on every new route; all new unsafe routes in `GATED`; `ai_smm_cc` has no write grant on the new tables | `test_api_mutation_gate.py` (every role × every route, DB fingerprint unchanged), `test_db_least_privilege.py::test_every_business_command_fails_in_the_database_without_the_gate` |
| T12 | **Key file misconfigured** | group/world-readable key refused; worker refuses to start (exit 78) | `test_a_group_readable_private_key_file_is_refused`, `test_a_worker_with_a_group_readable_key_refuses_to_start`, `test_main_exits_with_ex_config_on_a_bad_key_file` |
| T14 | **A release started with a configuration that does not fit the queue** fails existing scheduled rows | mandatory fail-closed worker preflight before recovery/scheduler/claim (exit 78); `ai-smm preflight worker` before the switch | `tests/test_preflight.py` |
| T15 | **Developer secrets in non-entry-point processes** (tests, the API, a REPL) via import-time `.env` loading | no module loads `.env` at import; only `ai-smm` and `ai-smm-worker` opt in | `tests/test_local_env.py` |
| T16 | **Account data destroyed by an accidental downgrade** | downgrade over account data requires `AI_SMM_DOWNGRADE_DESTROYS_ACCOUNT_DATA=yes`; live account-bound rows always refuse | `test_migration_social_accounts.py` |
| T13 | **Frontend retains the token** | `type=password`, `autocomplete=off`; input cleared on every submit; mutation `gcTime: 0` + `reset()` so variables are not cached | `projectManagement.test.tsx` scans DOM and both TanStack caches for the token |

## 3. Cryptography

`ai_smm.security.credentials`: per-message ephemeral X25519 key, ECDH with the recipient public key, HKDF-SHA256 (salt = ephemeral ‖ recipient public key, fixed info string), AES-256-GCM with a random 96-bit nonce and AAD = `ai-smm:social-credential:<account_id>:<credential_id>`. Primitives from `cryptography` 50.0.2 (already locked transitively; now declared directly). `key_id` = first 16 hex of SHA-256 of the recipient public key selects the private key during rotation; no trial decryption. Fingerprint = first 12 hex of SHA-256 of the token: identifies a token without revealing any of its characters (tokens are high-entropy, so the hash is not invertible in practice).

## 4. Key management and recovery

- **Generate:** `ai-smm credentials keygen --private-key-file PATH` — `O_EXCL`, mode 600, never overwrites; prints the public key and key id.
- **Store:** private key on the worker host, mounted read-only into the worker and `cli` containers only; never into `smm-api`/`smm-web`; never in the database or its dumps.
- **Back up:** encrypted, off-host, **separately** from database backups (a dump alone opens nothing; the key alone has nothing to open).
- **Rotate:** new pair → worker gets both private keys (`AI_SMM_CREDENTIALS_PRIVATE_KEY_FILES=new,old`) → API gets the new public key → replace each credential (re-enter) → remove the old private key when no row has its `key_id`. (A `rewrap` command is an open item, OI-4.)
- **Lose the key:** every stored token becomes unusable; account rows fail at preflight (`credential_key_missing`), nothing is sent. Re-enter tokens (they are re-obtainable from Meta). No fallback.
- **Key + dump stolen:** revoke the tokens at Meta, rotate the key, re-enter.

## 5. Checks performed in this sprint

- Static: import-graph invariants (API/application never import the publisher, the worker, or the decryption module); `ruff` clean.
- Dynamic: full Python suite against PostgreSQL 16.15 in the production role layout, including every least-privilege suite with the API connected as `ai_smm_cc`; web unit/component suite; see [tests.md](tests.md).
- Self-review findings fixed during the sprint: see [implementation.md §4](implementation.md#4-defects-found-and-fixed-during-the-sprint).

## 6. Residual risks

| Risk | Why accepted | Follow-up |
|---|---|---|
| Owner DB role can rewrite `social_account_id` or ciphertexts | outside every application boundary (as for all queue data) | — |
| The API makes one outbound call (`GET /me`) when a token is entered | needed for the connection test; read-only; no token in errors | OI-5 egress policy |
| Legacy `ai-smm approve` sets `human_reviewed` outside the editorial layer | pre-existing (025A Q-6); applies to legacy rows only | existing FU |
| Revoking in the CC does not revoke at Meta | stated in the UI | — |
