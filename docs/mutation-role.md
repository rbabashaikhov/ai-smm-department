# Control Center API — business-mutation role `ai_smm_cc_rw`

| | |
|---|---|
| Stage | SMM-025C Phase 2 — design and local validation (FU-025B-1, the database part of FU-024C2-3) |
| Baseline | `main` at `5b4e3d7a4a9941950c90b53def871994d297c29c` (SMM-025B merged) |
| Status | **Local only.** Not created on any production instance; production keeps `ai_smm_cc` with mutations off. Switching mutations on is a separate, owner-approved step (§8) |
| Companion | [least-privilege-role.md](least-privilege-role.md) — the read-only role `ai_smm_cc`, whose design and checks this role extends |

## 1. Problem

`ai_smm_cc` makes the read-only business mode a property of PostgreSQL:
with `AI_SMM_API_MUTATIONS_ENABLED=true` every business command fails in
the database (least-privilege-role.md §6.2). An editable Control Center
needs the writes of those commands — projects and settings (SMM-025B
B1), social accounts and credentials (B1), content items, revisions,
approvals, materialisation and scheduling (SMM-024C, SMM-025A) — and
nothing more.

## 2. Decision

A **second** login role, `ai_smm_cc_rw`, used by `smm-api` only while the
Control Center runs in mutation mode. `ai_smm_cc` stays exactly as it is.

- **Same bounds as `ai_smm_cc`.** Owns nothing, member of nothing,
  `NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
  NOINHERIT`, no `CREATE` on the database or `public`, nothing on
  `alembic_version`, no default privileges, the same timeouts and
  connection limit. Its grants file is `ai_smm_cc.grants.sql` with the
  role renamed plus one block of business writes; it is convergent,
  transactional and sets no password, like the original.
- **Reads:** what `ai_smm_cc` reads, including the metadata-only column
  `SELECT` on `social_account_credentials` — never `ciphertext`,
  `nonce`, `ephemeral_public_key`.
- **Writes:** `ai_smm_cc`'s session and audit writes, plus the business
  writes of §3 — column-level `INSERT` and `UPDATE` only. No `DELETE`,
  no `TRUNCATE`, no `REFERENCES`/`TRIGGER`, anywhere.

Why a second role and not more grants on `ai_smm_cc`:

- **The read-only mode stays enforced by PostgreSQL.** Going back
  (`mutations disable`) switches the API to a role that cannot write a
  business row, whatever the flag says — not to "the same role with the
  flag off".
- **Each mode is verified on its own.** `ai_smm_cc.verify.sql` keeps
  asserting that the read-only role writes nothing; `ai_smm_cc_rw.verify.sql`
  asserts the business matrix. Neither file has a "mutations" branch.
- **Approval is per role.** Creating `ai_smm_cc_rw` and setting its
  password are separate owner approvals, independent of the read-only
  deployment that is already accepted.

## 3. Business writes

`INSERT` lists name every column the ORM sends: SQLAlchemy inserts
every mapped column that has no client-side default, unset ones as
`NULL`, so a narrower list fails the command. The `UPDATE` lists are
what restricts the role — they name only what a command changes.

| Table | INSERT | UPDATE | Endpoint / command |
|---|---|---|---|
| `projects` | all columns | `display_name, description, default_timezone, default_language, version, updated_at` | `POST /projects`, `PATCH /projects/{id}` — never `id`, `knowledge_path`, `default_platform`, `is_active` |
| `project_memberships` | all columns | — | the creator's owner membership on `POST /projects`; no role change through the API |
| `project_settings` | all columns | `content_config, publishing_config, brand_config, version, updated_at` | `PATCH /projects/{id}/settings` (version-checked) |
| `social_accounts` | all columns | `display_name, status, disabled_at, external_account_id, external_username, version, updated_at` | create, rename, disable/enable, identity binding on the first verified credential — never `project_id`, `platform` |
| `social_account_credentials` | all columns | `status, ephemeral_public_key, nonce, ciphertext, superseded_at, revoked_at` | `POST /social-accounts/{id}/credentials`, revoke — supersede/revoke wipe the material to `NULL`; never `social_account_id`, `fingerprint`, `verification_status` |
| `content_items` | all columns | `current_revision_id, approved_revision_id, status, version, updated_at` | create, new revision, submit, approve, reject — never `project_id` |
| `content_revisions` | all columns | — (append-only trigger) | new revision |
| `content_approvals` | all columns | — (append-only trigger) | approve / reject |
| `content_publication_links` | all columns | — | materialise (the link trigger checks project and platform) |
| `publications` | all columns except `id` | `status, scheduled_at, next_attempt_at, claimed_by, claimed_at, lease_expires_at, last_error, attempt_count, updated_at` | materialise, schedule / reschedule / cancel — never the content, `human_reviewed`, `threads_post_id`, `published_at`, `project_id`, `social_account_id`, the series links |
| sequence of `publications.id` | `USAGE` | | materialise |

Never granted, by design: `DELETE`/`TRUNCATE` on every table; any write
to `publication_attempts`, `content_series`, `users` beyond
`ai_smm_cc`'s login columns, `alembic_version`; `UPDATE` of
`project_memberships`, of `users.password_hash`, of any column in
`NEVER_UPDATED` of `tests/test_db_mutation_role.py`.

Wider than least-privilege-role.md §9 allowed: `INSERT` on
`project_memberships`, which SMM-025B's project creation needs
(FU-025B-1); still no `UPDATE` or `DELETE` there, and the risk is in §6.

Narrower than FU-025B-1 asked for: `verification_status` and
`verified_at` of a credential are written once, on `INSERT`; no command
updates them, so the role cannot either.

## 4. Credentials: write-only key material

Tokens are sealed in the API with the **public** key (X25519 + HKDF +
AES-GCM, AAD = account id + credential id) and opened only by the worker
with the private key. Towards the API's role:

- **Plaintext cannot be obtained through the API.** The role writes
  `ciphertext`, `nonce`, `ephemeral_public_key` and cannot `SELECT`
  them (asserted by `test_effective_privileges_match_the_matrix` and
  `test_reads_still_work_and_key_material_stays_hidden`); even with the
  ciphertext, opening needs the private key, which no API configuration
  names (`test_the_api_package_never_configures_a_private_key`) and
  which the Control Center's compose files do not mount.
- **A sealed token cannot be moved to another account.** No `UPDATE` of
  `social_account_id`; the AAD binds the ciphertext to its account and
  credential id, so a copied blob does not open under another row.
- **No credential is ever deleted** by the role; revocation wipes the
  material and keeps the metadata row.

## 5. Project and account isolation

Enforced by PostgreSQL whatever the API does:

- no `UPDATE` of any `project_id`, of `social_accounts.platform`, of
  `publications.social_account_id` or of a credential's account;
- `fk_publications_social_account (project_id, social_account_id)`:
  an account-bound publication names an account of its own project;
- `trg_content_publication_links_consistent`: lineage only within one
  project and platform;
- `trg_publications_account_claim_guard`: only a worker that declares
  account routing may claim an account-bound row — the role's `UPDATE
  (status)` does not let it claim one
  (`test_the_old_worker_guard_holds_for_the_mutation_role`).

Enforced by the API (and proved by the business-command suites re-run as
this role, §7): project-scoped RBAC on every command (owner / admin /
editor / viewer), owner-only account and credential management, CSRF.

## 6. Human approval

Unchanged as an application invariant (ADR, *Production invariant*): no
route, agent or job approves a revision or sets `human_reviewed`; only
an approved, materialised snapshot can be scheduled. The role adds what
PostgreSQL can hold without a migration:

- revisions and approvals are append-only for every role (trigger);
- `human_reviewed` and a publication's content are never updatable;
- the approval decision is a row in `content_approvals` with its
  `actor_user_id`.

**Residual risk (FU-025C-1):** `INSERT` on `publications` includes
`status` and `human_reviewed`, because materialise writes them. Whoever
controls the role's connection can insert a row already `scheduled` and
`human_reviewed` with no approval or lineage row, which the LIVE worker
would publish — and can store any sealed blob as a `verified`
credential. Both are proved, not assumed:
`test_residual_risk_the_role_can_insert_a_reviewed_scheduled_publication`,
`test_residual_risk_the_role_can_store_a_credential_marked_verified`.
Likewise `INSERT` on `project_memberships` (needed for the creator's
owner membership on `POST /projects`) lets such a connection add any
existing user to any project in any role. This is the same trust class as FU-024C2-1 (a forged owner session can
approve and schedule through the API itself) and FU-024C2-2 (the
`actor_user_id` of an approval is what the API writes). Closing it needs
a migration — a `BEFORE INSERT` trigger that, for this role, requires
`status = 'approved'`, `human_reviewed` only with a matching approval and
link, empty claim and delivery columns — and is left as a decision for
the release that enables mutations, not part of SMM-025C.

## 7. Local validation

PostgreSQL 16 cluster laid out like production: a superuser, a
non-superuser owner of the schema, `ai_smm_cc` and `ai_smm_cc_rw`
provisioned by their own SQL files.

| Suite | Result |
|---|---|
| `tests/test_db_mutation_role.py` — matrix (independent of the grants file), sequences, role bounds, verify file, no other role changed, convergence on drift, 47 statements refused with `42501`, claim guard, no onward grants, revoke refuses while in use, no private key in the API, key material hidden, migrated database, two residual risks | 64 passed |
| `tests/mutation_role/` — the API's business-command suites (content, materialise, links, publication commands, project create, social accounts, destination, project RBAC, auth, command ↔ worker concurrency) connected **as `ai_smm_cc_rw`** | 275 passed |
| `tests/test_control_center_mutations.py` — the wrapper's mutation mode (§8), including the real verify file on the real role and the override rendered by the real Compose | 62 passed |
| `ai_smm_cc_rw.verify.sql` on the test database | 831 lines: 826 PASS, 5 INFO, 0 FAIL |

## 8. Switching the mode: `cc-compose.sh mutations`

> **Locked up to SMM-025D; permitted from R7.** Up to SMM-025D `cc-gate.sh`
> ships `CC_RELEASE_PERMITS_MUTATIONS=no`: `mutations enable` is refused
> before anything is read or run, and a `control-center.mutations` file —
> written by hand or left by another copy — is never honoured (`up`,
> `restart`, `preflight`, `migrate`, `create-owner` refuse until
> `mutations disable`). R7 (N′, [reports/smm-025d/r7/](reports/smm-025d/r7/README.md))
> ships `yes`: the mechanism below is available, and the Control Center
> still starts read-only until `mutations enable`. It is not an
> environment switch. `tests/test_control_center_mutations.py` tests both:
> the mechanism on the shipped scripts, the lock on a copy set to `no`.

| Verb | What it does |
|---|---|
| `mutations status` | prints the mode (read-only / business-mutation / stale mode file) and what the running `smm-api` has: the flag and the database user — never a URL |
| `mutations enable --approved-by NAME` | images, identity gate, **API role gate on `ai_smm_cc_rw`** with `ai_smm_cc_rw.verify.sql` (pinned), and the read-only way back (`control-center.api.env` still `ai_smm_cc`, the two URLs distinct). Then writes `control-center.mutations` (atomically, 644), recreates `smm-api` with `compose.control-center.mutations.yml` (pinned), and checks the running container: flag `true`, user `ai_smm_cc_rw`. A failure before the start leaves nothing behind; a failed start or a wrong running container removes the mode file **and stops `smm-api`** (it may be half-started in mutation mode) |
| `mutations disable` | removes the mode file **first**, then recreates `smm-api` read-only after the identity and read-only role gates; if they fail, stops `smm-api` instead. Needs none of the mutation files |

Files on the host, next to `compose.control-center.yml`:

| File | Who writes it | Holds |
|---|---|---|
| `compose.control-center.mutations.yml` | release (pinned in `cc-gate.sh`) | smm-api only: `env_file: !override [control-center.api-rw.env]`, `AI_SMM_API_MUTATIONS_ENABLED: "true"` — nothing else |
| `ai_smm_cc_rw.verify.sql` | release (pinned) | the role's check matrix |
| `control-center.api-rw.env` | operator, mode 600 | exactly `AI_SMM_DATABASE_URL` as `ai_smm_cc_rw` (example: `deploy/control-center/control-center.api-rw.env.example`) |
| `control-center.mutations` | `mutations enable` only | `SMM_CC_MUTATIONS=on`, the release, UTC time, approver; anything else is refused |

Properties:

- **Per release.** The mode file names `SMM_CC_VERSION`. For any other
  release, `up`, `restart`, `preflight`, `migrate`, `create-owner` are
  refused until `mutations disable`; `ps`, `logs`, `down`, `config` run
  read-only. A new release always starts read-only.
- **Consistent across verbs.** While the mode file is valid, every verb
  adds the override, so `restart smm-api` cannot silently fall back to a
  half-configured state, and `up` re-checks `ai_smm_cc_rw` before
  starting the API.
- **The literal stays.** `compose.control-center.yml` still says
  `AI_SMM_API_MUTATIONS_ENABLED: "false"`; only the pinned override,
  loaded by the wrapper, changes it.
- **Not a protection against the host.** Who can run `docker compose` by
  hand can bypass any wrapper gate (as for the read-only role,
  least-privilege-role.md §6). The gates guard against mistakes; the role
  guards against the API process.

## 9. Before mutations are switched on in production

Not part of SMM-025C; listed so the switch has one checklist
([follow-ups.md](follow-ups.md)):

1. FU-024C2-1 (session forgery) and FU-024C2-2 (audit / approval
   authenticity) closed or explicitly accepted by the owner;
2. FU-025C-1 decided (the insert invariant of §6);
3. FU-024B4A-1: once the API can write the queue, no further migration
   without closing the queue-gate → DDL gap — so the SMM-025B migration
   is applied **before** mutations are enabled;
4. FU-024C1-5's checklist executed on the exact release;
5. the role created (`ai_smm_cc_rw.grants.sql`), its password set with
   `\password`, `control-center.api-rw.env` written, verify run — each a
   separate approval; the production steps are in
   [reports/smm-025b/migration-rollback.md](reports/smm-025b/migration-rollback.md) (R7);
6. a credential key pair generated and backed up off-host before the
   first token is saved (the API seals with the public key; without it
   `POST …/credentials` refuses with `CREDENTIALS_NOT_CONFIGURED`).

**Status after SMM-025D (2026-10-14, local):** items 1, 2 and 6 are
implemented in code — database guards, smm-auth with step-up approval,
grants G2, audit provenance, key packaging
([ADR-025D](adr/ADR-025D-database-enforced-mutation-safety.md),
[credential-keys.md](credential-keys.md)). Under the guards this role also
needs EXECUTE on the six guard functions and its writes are accepted only
in a transaction bound to the owner's live session. Items 3–5 and the
production steps are in
[reports/smm-025d/rollout-rollback.md](reports/smm-025d/rollout-rollback.md)
(§4 R7, §5 the FU-024C1-5 checklist).

Rollback at any point: `mutations disable` (API read-only on `ai_smm_cc`
within one container recreate); the role itself is removed with
`ai_smm_cc_rw.revoke.sql`, which refuses while a session of the role is
open.
