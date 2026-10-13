# SMM-025B — Migration & Rollback Report

**Status: a plan. Nothing here has been executed against production.** Every step that touches the VPS, the production database, the live worker or a production secret needs its own approval from the owner. The order below is the one the tests in this sprint validate locally.

## 1. What changes in the database

Migration `a7c2e5f19b40_social_accounts_credentials` (revises `3d4ac1986f07`, the current production head):

| Object | Change | Effect on existing rows |
|---|---|---|
| `social_accounts` | new table | — |
| `social_account_credentials` | new table | — |
| `content_revisions.social_account_id` | new nullable column + FK | none: NULL for all; append-only trigger unaffected (no row is updated) |
| `publications.social_account_id` | new nullable column, composite FK `(project_id, social_account_id)`, index | none: NULL for all = **legacy rows**, published exactly as before (for allow-listed projects) |
| `publication_account_claim_guard()` + `trg_publications_account_claim_guard` | new trigger on `publications` | none for existing rows (the trigger only acts on rows with an account) |

No row is read or rewritten. The migration takes no `lock_timeout` of its own; it runs with the bound the `migrate` service passes through `PGOPTIONS` (proved by `tests/test_migrations.py::test_a_lock_timeout_rolls_back_every_pending_revision`, which now runs through this revision too).

## 2. The transition hazard and how it is closed

The one dangerous state is **an old worker meeting an account-bound row**: it would publish it with the deployment token, i.e. as the wrong account. Three independent barriers:

1. **Data:** in production no account-bound row can exist before business mutations are enabled (API read-only; `ai_smm_cc` has no write grant on any of the new tables).
2. **Database:** the guard trigger refuses `status → claimed` for an account-bound row unless the claiming transaction ran `set_config('ai_smm.account_routing','on', true)`. Only the new `claim_due_publication` does. The pre-SMM-025B claim statement, run verbatim, is refused (`tests/test_migration_social_accounts.py::test_an_old_worker_claim_cannot_take_an_account_row`).
3. **Downgrade guard:** `downgrade()` refuses while any account-bound publication is not `published`/`cancelled` — otherwise dropping the column would turn it into a legacy row. This refusal has no override.
4. **Worker preflight (fail-closed):** the new worker checks its configuration against the queue before it recovers, schedules or claims anything, and refuses to start (exit 78) on any FAIL — §3a.

## 3. Future rollout sequence (each step separately approved)

| # | Step | Gate / check | Reversible by |
|---|---|---|---|
| R0 | Backup (existing `cc-db-backup.sh dump` + verify + off-host), as for every CC migration | backup evidence gate | — |
| R1 | **Generate the credential key pair** on the host: `ai-smm credentials keygen --private-key-file /srv/ai-smm/secrets/credentials.pem` (mode 600). Back the private key up **off-host, encrypted, separately from DB dumps**. Record the public key | file mode 600; key id printed | delete the file (nothing uses it yet) |
| R2 | **Migrate** to `a7c2e5f19b40` with the CC `migrate` service, worker running (old worker is safe: §2) | `alembic current` = `a7c2e5f19b40`; `scripts/check_worker_schema_compat.sh` | `alembic downgrade 3d4ac1986f07` (allowed: no account rows exist yet) |
| R3 | **Re-apply `ai_smm_cc.grants.sql`** of this release (adds `SELECT` on `social_accounts` and metadata columns of credentials), run `ai_smm_cc.verify.sql` | verify: 0 FAIL; `cc-gate.sh` pin = this release's verify sha256 | re-run the previous release's grants file (its `REVOKE ALL` removes the new grants) |
| R4a | **Preflight the new worker image** *before* switching: `ai-smm preflight worker`, run from the new image with exactly the environment the new worker will get (§3a) | exit 0, no `FAIL` line | — (read-only) |
| R4 | **Deploy the new worker image** with `AI_SMM_LEGACY_THREADS_PROJECTS=ai-catalog-consultant` and `AI_SMM_CREDENTIALS_PRIVATE_KEY_FILES=/srv/ai-smm/secrets/credentials.pem` (read-only mount into the worker and the `cli` tool only) | worker log `Preflight passed`; existing scheduled pilot rows publish as before. If the preflight fails at start the worker exits 78 and claims nothing (§3a) | redeploy the previous worker image (schema stays; old worker + guard is safe) |
| R5 | **Deploy the new CC release** (API + web) with `AI_SMM_CREDENTIALS_PUBLIC_KEY` and the same `AI_SMM_LEGACY_THREADS_PROJECTS`; mutations stay `"false"` | read pages: Projects, Settings, Accounts (empty), Credentials (owner); no write control | redeploy the previous CC release **after** step R3's reverse |
| R6 | *(Optional, before mutations)* create a second project with `ai-smm project create --owner-email …` through a gated wrapper verb (to be added like `create-owner`) | identity, queue-window and backup gates | operator SQL delete of the empty project (§5) |
| R7 | **Business mutations** — only after FU-024C1-5, FU-024C2-1/2/3 (the mutation privilege model must now also cover `projects`, `project_memberships`, `project_settings`, `social_accounts`, `social_account_credentials` writes, see open-issues OI-1) | separate approval | flag back to `"false"` |

Order matters: R2 before R4 (new worker needs the columns); R3 before R5 (new API reads the new tables as `ai_smm_cc`); R4 before any account-bound row can exist (R7).

## 3a. Worker preflight (mandatory, fail-closed)

`ai_smm.preflight.worker_preflight` runs in `Worker.run()` **before** recovery, the scheduler and any claim. On any `FAIL` the worker logs every failed check and exits with **78 (EX_CONFIG)** — so a release started with a configuration that does not fit the queue publishes nothing *and fails nothing*: every scheduled row stays `scheduled`, untouched, until the configuration is fixed and the worker restarted (a restart loop under `restart: unless-stopped` is harmless for the same reason). The operator command `ai-smm preflight worker` runs the same checks read-only and exits 1 on any `FAIL`; R4a uses it before the switch.

| Check | FAIL when | Why |
|---|---|---|
| `schema.revision` | `alembic_version` ≠ `a7c2e5f19b40` (or missing); nothing else is checked | the new worker's queries need the new columns: migrate first (R2) |
| `legacy.allow_list.known_projects` | an id in `AI_SMM_LEGACY_THREADS_PROJECTS` is not a project | a typo would silently leave the intended project out |
| `legacy.pending_rows_allowed` | any `scheduled`/`claimed` row **without** an account belongs to a project outside the allow-list | exactly the forgotten-allow-list case: those rows would otherwise be failed one by one |
| `legacy.deployment_token` | live mode, allow-listed pending legacy rows exist, `THREADS_ACCESS_TOKEN` empty | they could not be published (value never printed) |
| `accounts.credential_keys` | an active credential's `key_id` has no private key in `AI_SMM_CREDENTIALS_PRIVATE_KEY_FILES` | its account's rows could not be decrypted |
| (key file) | the private key file is unreadable or group/world-readable | worker exits 78 before the preflight; the CLI reports `FAIL|accounts.private_key_files` |
| `accounts.pending_rows_ready` | never FAIL — **INFO** | a disabled account or revoked credential is an owner's decision; its rows are refused one by one, as designed, and must not stop every other project |

No check prints a secret (at most project ids, counts and key ids). Tests: `tests/test_preflight.py`, in particular `test_a_misconfigured_worker_never_claims_and_leaves_the_queue_as_it_was`.

## 4. Rollback

**Rule: once real `SocialAccount` / `Credential` data exists, roll back the application, not the schema.** The migration is additive and the old code runs on the new schema (old worker: legacy rows as before, account rows refused by the guard; old CC: never reads the new tables), so an application rollback loses nothing and is itself reversible. A schema downgrade is for the window before any account exists (R2 → R5), or for a deliberate, approved decision to discard that data.

| From | To | Procedure | Data |
|---|---|---|---|
| New worker | Old worker | redeploy previous image; **schema stays** | nothing lost; legacy rows publish as before; account rows are refused by the guard (fail loudly, nothing sent) and wait for the new worker |
| New CC | Old CC (`0cbd641`) | re-apply the old grants file (removes `SELECT` on new tables so the old verify passes), redeploy; **schema stays** | nothing lost |
| Schema `a7c2e5f19b40` | `3d4ac1986f07`, **no** account data yet | `alembic downgrade 3d4ac1986f07` | nothing to lose |
| Schema `a7c2e5f19b40` | `3d4ac1986f07`, account data exists | **not recommended.** Refused unless all account-bound rows are `published`/`cancelled` (no override) **and** `AI_SMM_DOWNGRADE_DESTROYS_ACCOUNT_DATA=yes` is set; backup first | **destroyed**, see §4.1 |
| Keys | — | see security report §4 | — |

### 4.1 What a downgrade destroys

A downgrade of `a7c2e5f19b40` drops, permanently and without a copy:

- every `social_accounts` row — labels, status, the bound Threads identity (`external_account_id`, `external_username`);
- every `social_account_credentials` row — the sealed tokens (owners must re-enter them) and the credential history (fingerprints, verification and revocation times);
- `content_revisions.social_account_id` — the approved destination of every revision. Their `content_hash` still includes the dropped account id, so a re-upgrade cannot restore the destinations from the hashes;
- `publications.social_account_id` — the account attribution of every publication, **including published ones**: afterwards nothing records which account a past post went out on, except the audit log (`content.materialized` details) and the platform itself.

That is why the migration refuses a downgrade over existing account data by default, and why the switch that permits it is named for what it does. The only way back is the backup taken before the downgrade.

## 5. Removing a project created by mistake

There is no delete in the API. A project without content can be removed by the owner role, after a backup, in one transaction: delete its `social_account_credentials`, `social_accounts`, `project_settings`, `project_memberships`, `projects` rows. Content and publications reference the project with `ON DELETE RESTRICT`, so a project that has any cannot be removed by accident.

## 6. Verified locally

| Check | Test |
|---|---|
| Upgrade keeps every existing publication byte-identical, as legacy rows | `test_migration_social_accounts.py::test_existing_rows_keep_their_meaning` |
| Only one column, one FK, one index added to `publications` | same |
| Old-worker claim refused for account rows, allowed for legacy rows | `test_an_old_worker_claim_cannot_take_an_account_row` |
| Downgrade refused with a live account row, nothing dropped | `test_a_downgrade_refuses_while_an_account_row_is_live` |
| Downgrade over account data refused without the explicit switch (unset, empty, `no`, `true`); data kept | `test_a_downgrade_that_would_destroy_account_data_needs_the_explicit_switch` |
| The switch never overrides the live-row refusal | `test_the_destroy_switch_never_overrides_the_live_row_refusal` |
| With the switch and only terminal rows, downgrade restores the exact schema | `test_a_downgrade_with_only_terminal_account_rows_restores_the_schema` |
| Misconfigured new worker refuses to start; scheduled rows untouched, no attempt written | `test_preflight.py::test_a_misconfigured_worker_never_claims_and_leaves_the_queue_as_it_was` |
| Up/down/up repeatable; models == migrations | `test_the_migration_is_repeatable`, `test_migrations.py::test_migration_matches_models` |
| `lock_timeout` via `PGOPTIONS` still bounds the whole upgrade | `test_migrations.py::test_a_lock_timeout_rolls_back_every_pending_revision` |
| Grants file + verify file on a migrated DB: 0 FAIL; recreated tables fail closed until grants re-run | `test_db_least_privilege.py::test_migrations_run_as_the_owner_alongside_the_role` |
