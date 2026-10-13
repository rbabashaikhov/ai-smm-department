# Control Center API — least-privilege PostgreSQL role `ai_smm_cc`

| | |
|---|---|
| Stage | SMM-024C-2A — design and local validation (FU-024B3-1); SMM-024C-2A-FIX — security review corrections (2026-10-12); SMM-024C-2B — production rollout (2026-10-12 – 2026-10-13) |
| Baseline | `main` at `bc60b48bf79eeb44f744f4bd6ff3058794f0c7f3`; released as `0cbd641074ad3bc0501e54f59dac90bd1ccc9006` |
| Status | **In production since 2026-10-13, accepted by the owner.** `smm-api` connects as `ai_smm_cc`; the worker, `migrate` and `cli` keep the owner role. Record: [production-acceptance-smm-024c2.md](production-acceptance-smm-024c2.md) |
| Scope | the read-only business mode deployed on 2026-10-11. Business mutations (SMM-025) need a separate design (§9) |

## 1. Problem

From 2026-10-11 until the rollout of this design on 2026-10-13 the
deployed API connected as `ai_smm`: the database owner, the role the live
worker and the migrations use. The read-only guarantee therefore rested
on the application alone — the central mutation gate and
the service-level checks. A code path that escaped both could change or
delete anything, including the publication queue that the live worker
sends to Threads, and could run DDL.

This stage moves that guarantee down into PostgreSQL: the API gets a role
that can read what the Control Center shows and write only the session and
audit rows that signing in and out create. Every other change is refused
by the database, whatever the application does.

## 2. Decision

A dedicated login role, **`ai_smm_cc`**, used by `smm-api` only.

- **No ownership, no DDL, no membership.** It owns nothing, is a member of
  no role (so it inherits nothing and cannot `SET ROLE`), is `NOSUPERUSER
  NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT`, and has no
  `CREATE` on the database or on schema `public`.
- **Reads:** `SELECT` on the 13 application tables (§3).
- **Writes:** column-level only, and only what login, the session check and
  logout write: `user_sessions` (insert; `last_seen_at`, `revoked_at`),
  `users` (`last_login_at`, `updated_at` — **not** `password_hash`, §4.3), `audit_log`
  (insert `actor, action, subject, details` — not `id`, not `at`), and
  `USAGE` on `audit_log`'s own sequence. No `DELETE`, `TRUNCATE`,
  `REFERENCES` or `TRIGGER` anywhere.
- **Nothing on `alembic_version`**, no default privileges: a table added by
  a later migration is invisible to the API until the grants file grants it
  (fail closed).
- **Bounds for a shared instance:** `CONNECTION LIMIT 10` (one uvicorn
  process: pool 5 + overflow 2), and role-level `statement_timeout=15s`,
  `lock_timeout=5s`, `idle_in_transaction_session_timeout=60s`.
- **Owner, worker, migrations, CLI: unchanged.** Nothing is granted to or
  revoked from `ai_smm`, the worker, `PUBLIC` or any other role (proved by
  a before/after snapshot of every other role's privileges, §6).
- **Deployment:** `smm-api` reads a new `control-center.api.env`;
  `migrate` and `cli` keep `control-center.env` (owner, byte-identical to
  the worker's URL — the existing identity gate is untouched). A new
  **API role gate** runs before `up`, `restart` and `preflight` and
  refuses unless PostgreSQL itself reports the role as designed, and
  unless the compose and verify files are exactly this release's (§5).
- **One application change** (SMM-024C-2A-FIX): the optional upgrade of an
  outdated Argon2 hash at login runs in a SAVEPOINT and is skipped when
  the role may not write it (§4.3). That changes the API image: the
  rollout builds new images (§7).

Files:

| File | Purpose |
|---|---|
| [`deploy/control-center/db/ai_smm_cc.grants.sql`](../deploy/control-center/db/ai_smm_cc.grants.sql) | create the role (no password) and converge it to exactly the matrix; one transaction; safe to re-run |
| [`deploy/control-center/db/ai_smm_cc.verify.sql`](../deploy/control-center/db/ai_smm_cc.verify.sql) | one read-only `SELECT`: `PASS/FAIL/INFO` per check of **effective** privileges on every table, column and sequence in `public`, role attributes, membership, ownership, password, settings |
| [`deploy/control-center/db/ai_smm_cc.revoke.sql`](../deploy/control-center/db/ai_smm_cc.revoke.sql) | rollback stage 2: retire the role; refuses while it has sessions, owns anything or has memberships |
| [`deploy/control-center/db/acl-snapshot.sql`](../deploy/control-center/db/acl-snapshot.sql) | read-only: the privileges of every OTHER role; taken before/after, must not differ |
| [`deploy/control-center/db/session-anomalies.sql`](../deploy/control-center/db/session-anomalies.sql) | read-only detective check: live sessions the API's own login did not produce (§4.1) |
| [`deploy/control-center/control-center.api.env.example`](../deploy/control-center/control-center.api.env.example) | `smm-api`'s URL, as `ai_smm_cc` |
| `compose.control-center.yml`, `cc-common.sh`, `cc-gate.sh`, `cc-compose.sh` | `smm-api` → `control-center.api.env`; the API role gate; pinned compose and verify files |
| `src/ai_smm/application/auth.py` | the optional hash upgrade in a SAVEPOINT, absorbing only SQLSTATE 42501 |

### 2.1 Alternatives considered

| Option | Why not |
|---|---|
| **Keep the owner role** (status quo) | the read-only guarantee stays application-only; any bypass is unbounded (DDL included) |
| `default_transaction_read_only` on a role, or a read-only replica | login/logout must write sessions and audit; a read-only API cannot authenticate. Splitting auth into another service is a much larger change |
| Table-level `INSERT`/`UPDATE` on `users`, `user_sessions`, `audit_log` | lets the API reactivate a user, change an e-mail, extend or reassign a session, choose or backdate an audit entry. Column-level grants cost nothing at runtime |
| A `NOLOGIN` group role holding the grants + a login role in it | useful for rotation by role swap and for several API processes; not needed for one process. Membership is exactly what this design forbids, so adding it later is a reviewed change to the verify file |
| `ALTER DEFAULT PRIVILEGES` for future tables | would silently expose any new table — including a future secrets or token table — to the API. Explicit grants per migration are the safer default |
| Row-level security | nothing to separate by row today; the API's project scoping is done in SQL. Adds policy complexity with no gain for a read-only role |
| `SECURITY DEFINER` functions for the auth writes | narrower still, but needs a migration, and every function is a privilege boundary to maintain. Revisit with the session hardening (§4.1) and SMM-025 |
| Keep `UPDATE (password_hash)` for the transparent Argon2 upgrade (the 2A design) | a compromised API could set **any** account's password hash and sign in as that user. Removed in 2A-FIX (§4.3) |
| Remove the grant, keep the code as it was | the first change of Argon2 parameters (e.g. an argon2-cffi upgrade changing its defaults) would make **every** login fail with 500: the hash write is in the login transaction |
| An administrative rehash (CLI, as the owner) instead of the one at login | impossible as such: a hash can only be recomputed from the plaintext, which exists only during a login. The administrative path is setting a new password, which the CLI cannot do yet; not needed, because an old hash keeps verifying (§4.3) |
| Creating the role in an Alembic migration | roles are cluster-wide objects on a **shared** instance and need `CREATEROLE`; migrations run as the owner and must not depend on, or create, the API role |

### 2.2 Risks and how they are bounded

| Risk | Bound |
|---|---|
| A table recreated by a migration loses its grants (no default privileges) | the API fails closed (permission denied → 500 on that endpoint); the gate refuses `up`/`restart`; re-running the grants file restores it. Every migration that creates or recreates a table must re-run it — a step in the migration runbook (§7.9) |
| A future column on `users`/`user_sessions` that the ORM always sends | login fails closed with permission denied. Caught locally: run the suite with `AI_SMM_TEST_API_ROLE=restricted` (§6) |
| Privileges reaching the role through `PUBLIC` (e.g. `CREATE` on `public` in a database created before PostgreSQL 15) | the verify file checks **effective** privileges; the grants file stops if `PUBLIC` gives `CREATE` and does not revoke from `PUBLIC` itself (that would change the worker too) |
| `PUBLIC` can `CONNECT` to every database on the shared instance and has `TEMPORARY` in `ai_smm` | reported as `INFO` by the verify file, not changed: changing `PUBLIC` on other projects' databases is not ours to do. Same exposure as the owner role today (§8) |
| The password leaks | it is never in Git, a command line, a server log or `pg_stat_statements` (`\password` sends a SCRAM verifier). A leaked password gives read access to the Control Center's data, not write access to the queue |
| A compromised API connection can create a session for **any** user (INSERT on `user_sessions` is needed for login) | **not prevented** by the database — the foreign key only requires the user to exist, the unique `token_hash` only a new token. Bounded: the session gives no more database rights than the role has; detected (not prevented) by `session-anomalies.sql`; prevention needs a migration (§4.1) |
| A compromised API connection can write audit entries with any actor and action | **not prevented**: the database does not record who wrote an entry. Guaranteed: existing entries cannot be changed or removed by the role, and `at` is the server's (§4.2) |
| An outdated Argon2 hash is no longer upgraded through the API | it still verifies; upgrading it is a password reset by an operator (§4.3) |

## 3. Permission matrix

What the API actually issues was read from the code
(`src/ai_smm/api`, `src/ai_smm/application`) and then confirmed on
PostgreSQL by revoking each privilege and watching a named flow break.

**Necessity** = `tests/test_db_least_privilege.py::test_each_grant_is_needed[<id>]`;
with the grant taken away, the flow answers 500, with it in place it
succeeds.
**Denial** = `test_postgresql_refuses_business_changes_from_the_api_role[<id>]`
(SQLSTATE 42501, database fingerprint unchanged), plus
`test_effective_privileges_match_the_matrix` for every privilege not
listed.

### 3.1 Granted

| Object | Privilege | Caller (endpoint → service) | Why | Necessity | Denial of the rest |
|---|---|---|---|---|---|
| database `ai_smm` | `CONNECT` | every request | — | (no connection otherwise) | `database.create` denied |
| schema `public` | `USAGE` | every request | name resolution | (no query otherwise) | `schema.public.create` denied |
| `users` | `SELECT` (all columns) | `POST /auth/login` → `authenticate`; every authenticated request → `resolve_session` (`db.get(User)`) | find the account, verify the Argon2 hash, check `is_active` | `select users` | `insert user`, `delete users` |
| `users` | `UPDATE (last_login_at, updated_at)` | `POST /auth/login` | record the login | `update users.last_login_at`, `update users.updated_at` | `reactivate user`, `change email`, `set a password` (`password_hash`, removed in 2A-FIX) |
| `user_sessions` | `SELECT` | every authenticated request → `resolve_session` | find the session by token hash | `select user_sessions` | — |
| `user_sessions` | `INSERT (id, user_id, token_hash, expires_at, last_seen_at, created_at, revoked_at)` | `POST /auth/login` → `create_session` | open the session. The ORM always sends `revoked_at` (as NULL): found by this validation, see §6.3 | `insert user_sessions` | — |
| `user_sessions` | `UPDATE (last_seen_at)` | every authenticated request → `resolve_session` | idle timeout | `update user_sessions.last_seen_at` | `extend session` (`expires_at`), `steal session` (`user_id`) |
| `user_sessions` | `UPDATE (revoked_at)` | `POST /auth/logout` → `revoke_session` | end the session | `update user_sessions.revoked_at` | `delete sessions` |
| `audit_log` | `INSERT (actor, action, subject, details)` | `POST /auth/login` (`auth.login`, `auth.login_failed`), `POST /auth/logout` (`auth.logout`) | the audit trail of sign-ins | `insert audit_log` | `backdate audit` (`at`), `choose audit id` (`id`), `update audit`, `delete audit`, `truncate audit` |
| `audit_log` | `SELECT` | `GET /projects/{id}/audit` → `list_project_audit_page`; also `INSERT … RETURNING id, at` | the audit view | `select audit_log` | — |
| sequence of `audit_log.id` | `USAGE` | the audit inserts above (`nextval` in the column default) | `BIGSERIAL` | `usage audit sequence` | `setval audit sequence`; every other sequence: no privilege (`nextval publications`) |
| `projects` | `SELECT` | every project-scoped route → `require_project_role`; `GET /projects` | authorisation and display | (covered by every read) | `update projects` |
| `project_memberships` | `SELECT` | every project-scoped route → `get_membership`; `GET /auth/me`, `GET /projects` | RBAC | `select project_memberships` | `grant membership`, `insert membership` |
| `project_settings` | `SELECT` | `GET /projects/{id}/settings` | display | (read test) | `update project settings` |
| `publications` | `SELECT` | publications list/detail/preview, operations summary/attention, series detail, audit view subjects | display | `select publications` | `update publications`, `schedule publications`, `insert publications`, `delete publications`, `lock publications` (FOR UPDATE), `lock publications skip locked`, `block the queue` (LOCK … SHARE) |
| `publication_attempts` | `SELECT` | `GET /publications/{id}/attempts` | display | (read test) | `update attempts`, `delete attempts` |
| `content_series` | `SELECT` | series list/detail, operations summary, preview (series readiness), audit subjects | display | `select content_series` | `update series`, `insert series` |
| `content_items` | `SELECT` | content list/detail, audit subjects | display | `select content_items` | `update content items`, `lock content items` |
| `content_revisions` | `SELECT` | content detail (current revision), revisions list | display | (read test) | `insert revisions` |
| `content_approvals` | `SELECT` | approvals list | display | (read test) | `insert approvals` |
| `content_publication_links` | `SELECT` | content detail (lineage) | display | (read test) | `delete links` |

"(read test)": exercised by
`test_the_session_lifecycle_and_every_read_work_through_the_role`, which
calls every GET endpoint through the role.

### 3.2 Not granted, by design

| Object | Why | Proven by |
|---|---|---|
| `alembic_version` | migration state is the owner's | `test_migrations_run_as_the_owner_alongside_the_role` (`table.alembic_version.select` denied; `alembic upgrade` as the role → 42501) |
| every table: `DELETE`, `TRUNCATE`, `REFERENCES`, `TRIGGER`; table-level `INSERT`/`UPDATE` | — | `test_effective_privileges_match_the_matrix`, verify file |
| `SELECT … FOR UPDATE` / `FOR UPDATE SKIP LOCKED` on any table | needs `UPDATE`: the API cannot hold a row lock the worker's claim would skip, nor block the queue with `LOCK … SHARE` | `lock publications*`, `lock content items`, `block the queue` |
| DDL (`CREATE TABLE/SCHEMA`, `ALTER`, `DROP`, `DROP TRIGGER`) | owner only | `create table`, `create schema`, `alter table`, `drop table`, `drop trigger` |
| `SET ROLE <owner>`, `SET session_replication_role` | no membership; superuser only (would disable the append-only triggers) | `become owner`, `disable triggers` |
| grant option | no `WITH GRANT OPTION` anywhere | `grant onwards`, `test_the_api_role_cannot_grant_what_it_holds` |
| `pg_authid` and other restricted catalogs | standard | `test_the_api_role_cannot_read_the_role_catalog` |

### 3.3 PostgreSQL mechanics taken into account

- **Ownership.** The owner of a table implicitly holds every privilege on
  it; the role owns nothing (`role.owns_nothing`), and the grants file
  refuses to run if the tables have more than one owner or are owned by
  `ai_smm_cc`.
- **Inheritance.** `NOINHERIT` and no membership in either direction
  (`role.member_of`, `role.members`). An existing `ai_smm_cc` with a
  membership or other attributes is refused, never altered.
- **PUBLIC.** Effective privileges are what is checked
  (`has_table_privilege` etc. include `PUBLIC`). PostgreSQL 16 gives
  `PUBLIC` `CONNECT` and `TEMPORARY` on databases and `USAGE` on `public`;
  the grants file stops if `PUBLIC` has `CREATE` on `public` or on the
  database (`test_the_grants_file_stops_if_public_may_create_in_the_schema`).
  `EXECUTE` on functions is a `PUBLIC` default too; the two functions in
  `public` are trigger functions that cannot be called directly.
- **Default privileges.** None for or by the role (`role.default_privileges`).
- **Sequences.** Only `audit_log`'s, resolved with
  `pg_get_serial_sequence` rather than by name. `user_sessions`,
  `users` use UUIDs generated in Python.
- **Foreign keys and triggers.** Referential checks on insert run with the
  table owner's rights, so `user_sessions.user_id → users` needs no extra
  grant (the login test inserts through the role). The append-only and
  link-consistency triggers are irrelevant to a role that cannot write
  those tables.
- **Column privileges and the ORM.** SQLAlchemy updates only the changed
  columns, so column-level `UPDATE` is exact. For `INSERT` it sends every
  mapped column it has a value for, NULLs included (§6.3).
- **`RETURNING`.** `INSERT … RETURNING` needs `SELECT` on the returned
  columns; `audit_log` and `user_sessions` have it.

## 4. What the role cannot do, even with the application's gate off

`test_every_business_command_fails_in_the_database_without_the_gate`
builds the API with `AI_SMM_API_MUTATIONS_ENABLED=true` — a
misconfiguration or a bypassed gate — connected as `ai_smm_cc`, signs in
as a project owner and sends each of the 10 business commands (create
content, create revision, submit, approve, reject, materialise, patch
settings, schedule, reschedule, cancel). Every one answers **500
`INTERNAL_ERROR`** and the fingerprint of every table, every column, is
unchanged — not even the caller's `last_seen_at`, because the request
transaction rolls back. The failing statement is the first lock or write
(`SELECT … FOR UPDATE` on the item or publication, or the `INSERT`).

That is the property this stage adds: with the role in place, a business
change from the API needs a change in PostgreSQL, not only in the code.

What the role **can** still do is equally part of the design, and the
next three sections state it without softening (2A-FIX).

### 4.1 Sessions: what is and is not guaranteed

The role must `INSERT` into `user_sessions` (login) and `UPDATE`
`last_seen_at`/`revoked_at` (every request, logout). The table's only
constraints are `PRIMARY KEY (id)`, `UNIQUE (token_hash)` and
`FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE`, plus
`NOT NULL`; no `CHECK`, no trigger
(`test_the_session_table_has_only_these_constraints`).

**Not prevented — whoever controls the API's database connection can
create a session for any user.** It chooses `user_id`, a token it knows
(stored as `token_hash`) and any `expires_at`; the application then accepts
that session. The foreign key only requires that the user exists, the
unique index only that the token is new
(`test_residual_risk_the_role_can_mint_a_session_for_any_user`,
`test_what_the_session_constraints_actually_stop`). It can also revoke
any session, or keep any session from idling out
(`test_residual_risk_the_role_can_revoke_or_refresh_any_session`).

**What bounds it:**

- a forged session carries no more database rights than `ai_smm_cc`
  itself — read access and the same session/audit writes. Today
  impersonation affects the audit trail and what the UI shows, not the
  queue. With mutations (SMM-025) it would matter much more;
- the role cannot change an existing session's user, token or expiry,
  and cannot delete sessions (the `DENIED` set, §3.2);
- **detection without a migration:** `session-anomalies.sql` lists live
  sessions that the application's login did not produce — lifetime over
  7 days, timestamps in the future, no `auth.login` audit row naming the
  session, or one whose server-set time differs from the session's.
  A real login yields `END|anomalies|0`; the forgery above is reported
  (`test_a_real_login_leaves_no_session_anomaly`). It does **not** catch
  a forgery that also writes a consistent pair — a now-dated session
  within the lifetime plus a fresh `auth.login` row — which anyone
  controlling the API's connection can do. Recommended: run it with the
  other read-only checks after the switch (R6) and periodically.

**Minimal strengthening without a migration (proposed, not
implemented):** in `resolve_session`, reject a session whose
`expires_at − created_at` exceeds the configured lifetime, or whose
`created_at`/`last_seen_at` lie in the future. That bounds a forgery
written through the connection alone (e.g. injected SQL) to one lifetime
per forged row; it cannot help against code execution in the API, where
the check itself is under the attacker's control. A one-file application change, so it belongs in an image
rebuild that is happening anyway — owner's call (O8).

**Prevention needs a migration (separate architectural decision, not
proposed for this stage):** take `INSERT` away from the role and create
sessions through a `SECURITY DEFINER` function owned by the owner that
sets every timestamp itself; or a `BEFORE INSERT` trigger enforcing
`created_at = last_seen_at = now()` and the lifetime. Neither can stop a
compromised API from choosing *which* user — that is inherent as long as
the API itself decides that a password was correct.

### 4.2 Audit integrity: three separate properties

| Property | Status for rows written by `ai_smm_cc` | Proved by |
|---|---|---|
| **Existing entries are immutable** | **guaranteed towards the role**: no `UPDATE`, `DELETE`, `TRUNCATE`. Not immutability of the table as such — the owner can still change it | `test_existing_audit_entries_cannot_be_changed_by_the_role`, `DENIED[update/delete/truncate audit]` |
| **Server time** | **guaranteed**: `at` comes from the database's `now()` (the writing transaction's start); the role has no `INSERT` on `at` and cannot backdate. `id` comes from the sequence only (no `setval`) | `test_the_audit_time_is_the_servers_not_the_writers`, `DENIED[backdate audit, choose audit id, setval audit sequence]` |
| **Authenticity of actor / action / subject** | **not guaranteed**: the role writes any values; nothing structural or cryptographic in the database records or verifies who wrote an entry. That the API writes only `auth.login`, `auth.login_failed`, `auth.logout` is a property of its code | `test_residual_risk_the_role_can_write_any_actor_and_action` |

**Future hardening (needs a migration; separate decision):** a column
`written_by name NOT NULL DEFAULT current_user` on which the role has no
`INSERT` privilege, so every row records the database role that wrote it
— structurally, though not cryptographically — plus
`CHECK (written_by <> 'ai_smm_cc' OR (action LIKE 'auth.%' AND (actor =
'anonymous' OR actor LIKE 'user:%')))`. That separates API-written rows
from worker/CLI rows and confines the API to its own event types. Tamper
evidence against the owner (hash chaining, off-host shipping) is a
further, independent step.

### 4.3 Password hash upgrade without `UPDATE (password_hash)`

The 2A design granted `UPDATE (password_hash)` so that login could
transparently re-hash a password stored with older Argon2 parameters.
That let a compromised API set **any** account's hash and sign in as it.
2A-FIX removes the grant and changes one function,
`ai_smm.application.auth._upgrade_password_hash`:

- the upgrade is **optional**; the old hash keeps verifying;
- it runs in a **SAVEPOINT** inside the login transaction (`begin_nested`);
  on refusal only the savepoint is rolled back, and the session row, the
  `last_login_at`/`updated_at` update and the `auth.login` audit row that
  follow are written as usual;
- only **SQLSTATE 42501** (`insufficient_privilege`) is absorbed, with a
  warning naming the user id (never the hash or password). Any other
  database error propagates and fails the login exactly as before —
  tested with `raise_exception`, `lock_not_available` and
  `check_violation` from a test-only trigger: 500, no session, no audit;
- no privileged connection is opened by the API; under the owner role
  (deployed today, and the CLI) the upgrade still happens.

Tests (real outdated hashes — Argon2id with `time_cost=1,
memory_cost=8 MiB` — not a mocked `needs_rehash`):
`test_an_outdated_hash_logs_in_through_the_role_and_stays_as_it_is`
(two logins and a logout through the role: 200/200/204, hash unchanged,
2 sessions, `auth.login ×2 + auth.logout`, 2 warnings, no hash or
password in the log), `test_an_outdated_hash_is_still_upgraded_where_the_role_may`,
`test_any_other_error_during_the_upgrade_still_fails_the_login[3]`,
`DENIED[set a password]`.

Consequences: under the role, an outdated hash stays outdated until an
operator sets a new password (no CLI command for that exists yet — O9);
PostgreSQL logs one `permission denied for table users` per such login.
After an argon2-cffi upgrade that changes defaults, that is every
login until passwords are reset — noisy, not failing.

## 5. Configuration changes

| Where | Change |
|---|---|
| `compose.control-center.yml` | `smm-api.env_file` → `control-center.api.env`. `migrate` and `cli` unchanged (`control-center.env`). All literals unchanged |
| `deploy/control-center/control-center.api.env.example` | new: exactly `AI_SMM_DATABASE_URL`, user `ai_smm_cc`, mode 600 |
| `deploy/control-center/control-center.env.example` | comment only: owner role, for `migrate`/`cli` |
| `cc-common.sh` | `cc_load_api_secret`: the file must exist (every verb — Compose cannot load the project without it), be a regular file, hold exactly one key, mode 600 |
| `cc-gate.sh` | `cc_gate_api_role` (below); `CC_PINNED_COMPOSE_SHA256`, `CC_PINNED_VERIFY_SHA256` |
| `cc-compose.sh` | the API role gate runs for `up`, `restart`, `preflight`; never for `migrate`, `create-owner`, `ps`, `logs`, `down` |

**The API role gate** (after the identity gate, before Compose):

0. `compose.control-center.yml` and `ai_smm_cc.verify.sql` hash to the
   values pinned in `cc-gate.sh` (2A-FIX). A verify file edited or swapped
   on the host could print a passing answer; the compose file deployed on
   2026-10-11 next to the new scripts would start `smm-api` on the
   **owner** URL while the gate checked `control-center.api.env`. Both are
   refused before anything else; `ps`, `logs`, `down` are not gated, so a
   rollback is never blocked. `test_the_gate_pins_the_files_of_this_release`
   keeps the pins equal to Git.
1. `control-center.api.env` connects as `ai_smm_cc` **with a password**, to
   the same host, port and database as `control-center.env`; it is not the
   owner's URL; `control-center.env` is not `ai_smm_cc`. Nothing is
   printed but names.
2. `ai_smm_cc.verify.sql` (copied next to the compose file) is run
   read-only, as the instance's superuser over the container's socket —
   the API's password is never handled — and the output is read strictly:
   only `PASS|`, `FAIL|`, `INFO|` lines and one final `END|checks|N` with
   `N` equal to the lines before it and at least 200; no `FAIL`; and the
   checks for existence, attributes, SCRAM password, no membership,
   owning nothing, no `CREATE` on `public` and `publications` not
   updatable must each be present and pass.
3. `INFO` lines are printed for the record (other databases the role can
   connect to; `TEMPORARY`).

Any failure: exit 2 before Compose — the running `smm-api` is not touched.
`tests/test_control_center_api_role_gate.py` covers every refusal, the
gate answering before the first Compose call, and runs the real verify
file through the gate against a real role with eight kinds of drift
(extra grant, grant via `PUBLIC`, missing grant, attribute, cleared
password, membership, default privileges, a new granted table).

**What the gate cannot do.** It lives in `cc-compose.sh`. Compose itself
enforces nothing: `docker compose` run by hand renders `smm-api` with
whatever `control-center.api.env` holds, the owner URL included
(`test_bare_compose_runs_no_gate_the_wrapper_does`); `docker restart`
reuses a container's recorded configuration; a privilege granted after
`up` is only seen by the next `up`/`restart`/`preflight` or a manual
verify run. Whoever can run Docker on the host can bypass any gate — it
protects against mistakes (the trust model of FU-024B4A-2). Against the
**API process**, the protection is the role itself: the container holds
no owner credential (no `control-center.env`, no bind mount, read-only
filesystem), the DSN comes only from the env file (a query string is
refused, so no `options=-c role=…`), `.env` files do not exist in the
image and `load_dotenv()` never overrides a set variable, and from the
shared network `pg_hba` requires SCRAM. A wrong password is not detected
by the gate (it never handles the API's secret): `/health/ready` answers
503 after `up` — a rollback trigger in R6.

**Application code changed** in 2A-FIX (`src/ai_smm/application/auth.py`,
§4.3): the API image must be rebuilt (§7).

## 6. Local validation

2026-10-12, branch `feat/smm-024c2-least-privilege` from `bc60b48`. No
production host, database or credential was accessed.

**Environment.** A disposable `postgres:16.15-bookworm` container — the
production version — on `127.0.0.1:55432`, data on tmpfs, laid out like
production: a superuser (stand-in for the container's `POSTGRES_USER`),
the owner `ai_smm` as a **non-superuser** owning the test database, and a
second database standing in for a neighbouring project. The owner role
was given `CREATEDB` only because `tests/test_migrations.py` creates a
scratch database; nothing in the design uses it.

### 6.1 Results

| Run | Result |
|---|---|
| Baseline: the unchanged suite at `bc60b48`, API as owner | **1509 passed** |
| `tests/test_db_least_privilege.py` | **113 passed** |
| `tests/test_control_center_api_role_gate.py` (incl. the real verify file against the real role, 5 drifts) | **37 passed** |
| `tests/least_privilege/` (auth read-only, mutation gate, publication reads, API as `ai_smm_cc`) | **130 passed** |
| Full suite, final (default mode + admin URL: all new tests run) | **1795 passed**, 0 failed (run 2). Run 1: 1794 passed, **1 error** in `test_migrations.py` fixture setup — `DROP DATABASE ai_smm_migration_test WITH (FORCE)` refused to terminate a session of another role on that scratch database. Not reproduced: `test_migrations.py` alone 15/15, with every role suite 295/295, full run 2 1795/1795 with a watcher that saw no non-owner session on the migration databases. **Root cause found in 2A-FIX (§6.6):** an autovacuum worker in the scratch database, which a non-superuser's `FORCE` may not terminate — a test-infrastructure race, fixed in the fixtures |
| Full suite with `AI_SMM_TEST_API_ROLE=restricted` (only the API as `ai_smm_cc`) | 1542 passed, 83 failed, 39 errors — **every one** caused by `permission denied` on a business write (§6.2) |
| `ruff check .` | clean |
| Operator rehearsal (§6.4) | every step as expected |

### 6.2 The whole suite through the role

With only the API connected as `ai_smm_cc`, the failures are exactly the
suites that send business commands with mutations **on**, and every
failure's root cause is PostgreSQL refusing a business write:

| Refused statement | Refusals (tests) | Where |
|---|---|---|
| `SELECT … FROM content_items … FOR UPDATE` | 49 | lock before every editorial command |
| `SELECT … FROM publications … FOR UPDATE` | 35 | lock before schedule / reschedule / cancel |
| `INSERT INTO content_items` | 24 | create content |
| `INSERT INTO project_settings` | 14 | first settings patch |
| **Total** | **122** | |

**122 expected refusals, not 327 SQL events.** Each failing test has
exactly **one** refused statement — the request aborts at its first
denied write — so 122 tests mean 122 refusals by PostgreSQL. The same
statement is *printed* 2 or 3 times in the pytest log: in the traceback
(122), in the captured stdout of the API's own `Unhandled API error` log
(83 + 39 in fixture setup), and in pytest's captured log of that record
(83) — **327 printed copies in total**, which earlier versions of this
report and of the review answers wrongly listed as 108 / 105 / 72 / 42
"denied statements". Method: for every FAILED/ERROR test, split its log
section into the traceback and the `Captured …` blocks, take every
`[SQL: …]`, strip pytest's `E   ` continuation prefix, normalise
whitespace: one distinct statement per test (122/122), 2–3 copies each
(83 × 3 + 39 × 2 = 327). No other error type appears in any section.

| Suite | Failed / total | Why |
|---|---|---|
| `test_api_auth`, `test_api_auth_read_only`, `test_api_mutation_gate`, `test_api_publications_read`, `test_api_contract`, `test_api_docs_config`, `test_api_health`, `test_cli_user`, `test_content_concurrency` | **0** | reads, sessions, CSRF, audit of logins, the read-only gate |
| `test_api_publication_commands` 28/44, `test_api_content` 24/37, `test_api_projects_rbac` 8/28, `test_api_csrf` 5/15, `test_api_series_operations` 4/26, `test_api_command_concurrency` 4/12, `test_api_transaction_boundary` 1/2 | as listed | commands with mutations on. The CSRF tests use a settings `PATCH` as their accepted request; the audit-view tests create their entries with queue commands |
| `test_content_links` 22/22, `test_content_materialize` 26/26 | all | their fixtures create content through the API |

This list is also the starting inventory for SMM-025 (§9).

### 6.3 Found and fixed during validation

- **The ORM always inserts `user_sessions.revoked_at`** (as NULL). The
  first matrix granted `INSERT` on six columns; login failed with
  `permission denied for table user_sessions`. The grant now names all
  seven columns (still column-level, so a column added later is not
  granted with them). `test_effective_privileges_match_the_matrix` and
  the login tests pin it.

### 6.4 Operator rehearsal

`§7` followed as literally as possible on the local instance: a database
named **`ai_smm`** owned by the non-superuser `ai_smm`, migrated to
`3d4ac1986f07` by Alembic as the owner, an owner user made with `ai-smm
user create-owner`, one scheduled publication.

| Step | Observed |
|---|---|
| R1 | role absent; tables owned by `ai_smm` only; `PUBLIC` `CREATE` on schema/database `f|f`; `password_encryption` `scram-sha-256`; hba: `trust` only for the container's socket and loopback, `scram-sha-256` for `host all all all` |
| R2 | `psql -f` over `docker exec`: `BEGIN … COMMIT`; verify: exactly one `FAIL` (`role.password|not set`), 5 `INFO`, `END|checks|707`; ACL snapshot of every other role: **no diff** |
| R3 | SCRAM verifier computed client-side and set (as `\password` does); verify: no `FAIL` |
| R5 | a real `uvicorn` API with the packaged literals as `ai_smm_cc`: `/health/ready` 200 |
| R6 | `runtime_smoke.py` session checks **16/16 PASS** (docs off, login, cookie flags, `/auth/me`, projects, `mutations_enabled=False`, queue command refused `MUTATIONS_DISABLED`, CSRF on logout, logout, revoked session rejected); the runbook's snippet on the API's own connection: `current_user ai_smm_cc`, reads work, `UPDATE`/`DELETE`/`FOR UPDATE SKIP LOCKED` on `publications` **refused 42501**; `pg_stat_activity` shows the API as `usename ai_smm_cc`; API log: 0 `permission denied`, 0 occurrences of the password; `audit_log`: `auth.login`, `auth.logout`; the scheduled publication untouched |
| RB-3 | API stopped; `ai_smm_cc.revoke.sql`: `COMMIT`; role gone; ACL snapshot **identical to the R1 baseline** |

### 6.5 Lock impact (measured)

A worker-shaped transaction (claim with `FOR UPDATE SKIP LOCKED`, then
`UPDATE` the claimed row) held open while the grants file ran: the file
committed in **0.011 s**. The grants transaction held open (every
statement, not committed) while worker-shaped statements ran with
`lock_timeout=2s`: claim, `UPDATE publications`, `INSERT` attempt,
`INSERT` audit, plain read — **all immediate**. Locks the open grants
transaction held: catalog only (`pg_authid`, `pg_auth_members`,
`pg_class` *AccessShare*, `pg_db_role_setting` and `pg_shdescription`
*RowExclusive*); none on an application table. An `ACCESS EXCLUSIVE`
lock on `publications` (a migration) did not block it either (0.010 s).

### 6.6 SMM-024C-2A-FIX validation (2026-10-12)

Same disposable PG 16.15 cluster. Before and after every run: no stray
test process, no client session, only `ai_smm_test`, no `ai_smm_cc` left.

| Run | Result |
|---|---|
| Full suite, default mode + admin URL (all new tests run) | **1822 passed**, 0 skipped, 0 failed, 0 errors |
| Full suite, API as `ai_smm_cc` (`AI_SMM_TEST_API_ROLE=restricted`) | 1700 passed; 83 failed + 39 errors = **122 expected refusals, 0 real failures** — classified automatically: every failure's root cause is `InsufficientPrivilege` on one business write — 49 `FOR UPDATE content_items`, 35 `FOR UPDATE publications`, 24 `INSERT content_items`, 14 `INSERT project_settings` (one refusal per test; 327 is the number of printed copies in the log, §6.2); same 122 as in 2A |
| Least-privilege suites on PG16 (`test_db_least_privilege`, API role gate, `least_privilege/`) | **305 passed** |
| Login with a real outdated Argon2 hash (role, owner, other errors) | **5 passed** |
| Auth / session / audit selection | **160 passed** |
| Negative direct SQL (42501, fingerprint unchanged) | **48 passed** |
| Migrations incl. the scratch-database race | **18 passed** |
| Packaging, wrapper, gates, backup, runtime packaging, health, docs | **897 passed** |
| `ruff check .` | clean |
| Operator rehearsal (§6.4, extended) | as before, plus: a login with a real outdated hash through the real `uvicorn` API as `ai_smm_cc` — 13/13 smoke checks, hash unchanged, one `upgrade skipped` warning, hash never in the log, `session-anomalies.sql`: `END|anomalies|0` |

**What the least-privilege tests assert** (305):

| Kind | Count |
|---|---|
| Positive: works through the role / gate passes | 18 |
| Expected refusal: PostgreSQL or the gate says no, nothing changes | 141 |
| Residual risk, demonstrated (the test passes **because** the risk is real) | 3 |
| Documented limit (bare Compose runs no gate) | 1 |
| Structure: matrix, effective privileges, verify file, snapshots, credentials | 10 |
| Test infrastructure: the scratch-database drop race (below) | 2 |
| Existing suites re-run with the API as `ai_smm_cc` | 130 |

**Found and fixed: the intermittent `DROP DATABASE … WITH (FORCE)`
failure.** Reproduced directly: while an **autovacuum worker** is active in
a database, `DROP DATABASE … WITH (FORCE)` by its non-superuser owner fails
with `42501 permission denied to terminate process` — the worker runs as
no role, so the owner holds no privileges of it. A freshly created and
migrated scratch database attracts one within seconds. That was 2A's
unexplained `test_migrations` error and, during this validation, a
teardown failure of the new scratch-database test that left grants
behind (the revoke file then rightly refused to drop the role). Both
fixtures now use `tests/conftest.py::drop_scratch_database`, which retries
only that refusal, briefly; a deterministic test proves a session that
goes away is outlasted and one that stays still fails. Not a product
issue: `cc-db-backup.sh` drops its scratch database as the superuser.

## 7. Production rollout

> **Executed in production (SMM-024C-2B).** P1 on 2026-10-12 15:04 UTC,
> A1 at 18:30, A2 at 19:03, A3 on 2026-10-13 04:42 UTC — after a first A3
> attempt at 04:38 that was rolled back by RB-1 because one diagnostic
> probe named a non-existent column. Each step had its own owner approval
> and window. The sanitised record, with results and deviations, is
> [production-acceptance-smm-024c2.md](production-acceptance-smm-024c2.md).
>
> The blocks below stay as the **template** for any later run (another
> release, a re-creation of the role). Differences from the text as it
> was approved:
>
> - the release files were staged in a separate staging directory next
>   to `$CC`, not in `$CC/release-$M`; the commands work the same with
>   that path;
> - §7.4 installs `compose.control-center.yml` **last** (corrected below;
>   the original single `cp` copied it first);
> - the new wrapper's `preflight` needs a current `control-center.window`
>   (it runs the queue gate); A3 rewrote the file for its window and kept
>   the previous one in `$EV/prev`;
> - a privilege probe used as a rollback trigger must name real columns:
>   check them in `information_schema` before the run.

Four separate owner approvals:

| | Operation | Touches |
|---|---|---|
| **A0** | load images built from the merge commit `M` and run them on the **current** configuration (P1) | `smm-api` and `smm-web` are recreated (seconds of Control Center downtime). Still the owner URL, the 2026-10-11 compose file and scripts |
| **A1** | create `ai_smm_cc` and its grants (R2) | the shared instance's catalogs: one new cluster-wide role, ACL entries on `ai_smm`'s tables. No table lock, no data |
| **A2** | set its password, write `control-center.api.env` (R3) | the role's password; one new secret file on the host |
| **A3** | switch `smm-api` to the role (R4–R5) | `smm-api` is recreated. The worker, the scheduler and the publishing flow are not touched |

**Which images, and why (changed by 2A-FIX).** `src/ai_smm/application/auth.py`
changed (§4.3), so **`ai-smm-api` must be rebuilt** from `M`: the deployed
`8ccccda` image would, under `ai_smm_cc`, fail every login whose hash
needs an upgrade. **`ai-smm-web` is rebuilt too**, although its content is
unchanged, because the wrapper requires both images to carry the OCI
revision label equal to `SMM_CC_VERSION`. The build is the existing
runbook's §3 (clean `git archive` of `M`, `--target api`, the web build,
labels, manifest). No migration, schema change or queue window is needed;
the worker image is not built or touched.

The code change is deliberately deployed **before** the privilege change,
so that each has its own rollback:

- **P1 — new images, old configuration (A0).** Stage the image tarball,
  `docker load`, put `SMM_CC_VERSION=M` into `control-center.compose.env`
  (keep a copy of the old file), and with the **current** compose file and
  scripts run `./cc-compose.sh up`: identity gate, then `smm-api` and
  `smm-web` recreated on images `M`, still as the owner. Check readiness,
  run the read-only smoke test and the browser walk, and confirm the
  worker is unchanged (runbook steps 9, 11, 12). Under the owner the hash
  upgrade still happens, so nothing visible changes. *Rollback P1:* the old
  `control-center.compose.env` (`SMM_CC_VERSION=8ccccda…`) back, `up`;
  the `8ccccda` images stay loaded until P2 has been accepted.
- **P2 — the role (A1–A3):** R1–R7 below. *Rollback P2:* RB-1 — previous
  compose file and scripts back, `up`; `smm-api` returns to the owner URL
  on images `M`. Going back further is P1's rollback.

P1 and P2 may happen in one session; they must not be merged into one
`up`.

### 7.0 Session setup and staging (no runtime effect)

```bash
CC=/root/ai-smm-control-center
EV=$CC/evidence/smm-024c2-$(date -u +%Y%m%dT%H%M%SZ); install -d -m 700 "$EV"
PGC='<postgres-container>'          # = SMM_CC_PG_CONTAINER in control-center.identity
M='<full 40-hex merge commit>'      # the reviewed commit carrying this design
unset DOCKER_CONTEXT; export DOCKER_HOST=unix:///var/run/docker.sock
set -o pipefail
PGU=$(docker inspect "$PGC" --format '{{range .Config.Env}}{{println .}}{{end}}' | sed -n 's/^POSTGRES_USER=//p')
ro() { docker exec -i -e PGOPTIONS='-c default_transaction_read_only=on' "$PGC" \
         psql -X -U "$PGU" -d ai_smm -v ON_ERROR_STOP=1 -tA -c "$1"; }

# From the laptop: the new files of $M, with a sha256 manifest, into a
# staging directory NEXT to the live one (the live files are not touched).
#   compose.control-center.yml cc-common.sh cc-gate.sh cc-compose.sh cc-db-backup.sh
#   ai_smm_cc.grants.sql ai_smm_cc.verify.sql ai_smm_cc.revoke.sql acl-snapshot.sql
#   session-anomalies.sql control-center.api.env.example
#   ai-smm-cc-$M.tar.gz (the two images, P1)
install -d -m 700 "$CC/release-$M"          # then scp into it
cd "$CC/release-$M" && sha256sum -c "MANIFEST-$M.sha256"
```

### 7.1 R1 — Read-only baseline

```bash
cd "$CC"
docker inspect ai-smm-worker --format \
  '{{.Id}} {{.Image}} {{.Config.Image}} {{.State.StartedAt}} restarts={{.RestartCount}} {{.State.Health.Status}}' \
  | tee "$EV/worker-before.txt"
./cc-compose.sh ps | tee "$EV/cc-ps-before.txt"
docker inspect "$(./cc-compose.sh ps -q smm-api)" --format '{{.Id}} {{.State.StartedAt}}' | tee "$EV/smm-api-before.txt"
curl -fsS http://127.0.0.1:8088/health/ready; echo

ro "SELECT version()"                                                   # 16.x
ro "SELECT rolname FROM pg_roles WHERE rolname = 'ai_smm_cc'"           # EMPTY
ro "SELECT DISTINCT pg_get_userbyid(relowner) FROM pg_class
     WHERE relnamespace = 'public'::regnamespace AND relkind IN ('r','S')"   # ai_smm only
ro "SELECT has_schema_privilege('public', 'public', 'CREATE'),
           has_database_privilege('public', 'ai_smm', 'CREATE')"        # f|f
ro "SHOW password_encryption"                                           # scram-sha-256
ro "SELECT line_number, type, database, user_name, address, auth_method
      FROM pg_hba_file_rules ORDER BY 1" | tee "$EV/pg-hba.txt"            # see stop
ro "SELECT name, setting FROM pg_settings
     WHERE name IN ('log_statement', 'max_connections', 'shared_preload_libraries')" | tee "$EV/pg-settings.txt"
ro "SELECT usename, application_name, client_addr, state, count(*)
      FROM pg_stat_activity WHERE datname = 'ai_smm' GROUP BY 1,2,3,4" | tee "$EV/activity-before.txt"
# PUBLIC on the other databases of the instance (facts for §8; nothing is changed there)
for db in $(ro "SELECT datname FROM pg_database WHERE datallowconn AND datname <> 'ai_smm'"); do
  printf '%s ' "$db"
  docker exec -e PGOPTIONS='-c default_transaction_read_only=on' "$PGC" psql -X -U "$PGU" -d "$db" -tA \
    -c "SELECT has_database_privilege('public', current_database(), 'CONNECT'),
               has_schema_privilege('public', 'public', 'CREATE')"
done | tee "$EV/other-databases.txt"
ro "$(cat "release-$M/acl-snapshot.sql")" > "$EV/acl-before.txt"
```

**Stop if:** `ai_smm_cc` already exists; the application tables have an
owner other than `ai_smm`; `PUBLIC` has `CREATE` on `public` or on the
database (the grants file would stop too — revoking it from `PUBLIC`
changes the worker's privileges and is a separate decision, B3); a
`trust`, `password` or `md5` rule applies to `host` connections from
anything but `127.0.0.1`/`::1` (a password would protect nothing — for the
owner role either). The official image's own rules — `trust` for the
container's socket and loopback only, `scram-sha-256` for `host all all
all` — are expected (seen locally on `postgres:16.15-bookworm`); the
worker is not healthy.

### 7.2 R2 — Create the role and its grants (approval A1)

```bash
docker exec -i "$PGC" psql -X -v ON_ERROR_STOP=1 -U "$PGU" -d ai_smm \
  < "release-$M/ai_smm_cc.grants.sql" 2>&1 | tee "$EV/grants.txt"
ro "$(cat "release-$M/ai_smm_cc.verify.sql")" | tee "$EV/verify-1.txt" | grep -v '^PASS|'
ro "$(cat "release-$M/acl-snapshot.sql")" > "$EV/acl-after.txt"
diff "$EV/acl-before.txt" "$EV/acl-after.txt" && echo "no other role changed"
```

Expected: `COMMIT`; the verify output has exactly one `FAIL` —
`role.password|not set: the role cannot log in` — plus the `INFO` lines
and `END|checks|N`; the ACL diff is empty. The role exists but no process
can use it yet.

**Stop if:** any `ERROR` (the file is one transaction: nothing was
applied; on `tuple concurrently updated` — a concurrent DDL on the same
table — simply run it again); any other `FAIL`; a non-empty diff.
Measured locally: the file holds no lock on any application table and
commits in ~10 ms next to a worker transaction holding a claimed row (§6).

### 7.3 R3 — Password and the API's env file (approval A2)

Generate the password in the owner's password manager: at least 40
characters of `[A-Za-z0-9]` (no URL-encoding needed). It is typed (pasted)
twice into psql's hidden prompt, once into `read -s`, and nowhere else.

```bash
# psql hashes it client-side (SCRAM): only the verifier reaches the server.
docker exec -it "$PGC" psql -X -U "$PGU" -d ai_smm -c '\password ai_smm_cc'

cd "$CC"
( umask 077
  IFS= read -rs PW                                   # paste, Enter; nothing echoed
  printf 'AI_SMM_DATABASE_URL=postgresql+psycopg://ai_smm_cc:%s@%s:5432/ai_smm\n' "$PW" "$PGC" \
    > control-center.api.env )                       # printf is a builtin: not in ps
chmod 600 control-center.api.env
test "$(wc -l < control-center.api.env)" = 1 && echo one-line
grep -c '^AI_SMM_DATABASE_URL=postgresql+psycopg://ai_smm_cc:' control-center.api.env   # 1

# The credentials work, from the shared network, before anything uses them.
# The secret goes in by --env-file; only current_user is printed.
docker run --rm --network '<shared-network>' --env-file control-center.api.env \
  --read-only --cap-drop ALL --security-opt no-new-privileges:true --user 10001:10001 \
  --entrypoint /app/.venv/bin/python "ai-smm-api:$(sed -n 's/^SMM_CC_VERSION=//p' control-center.compose.env)" -c \
  'import os, sqlalchemy as sa; e = sa.create_engine(os.environ["AI_SMM_DATABASE_URL"]);
c = e.connect(); print(c.execute(sa.text("SELECT current_user")).scalar())'   # ai_smm_cc

ro "$(cat "release-$M/ai_smm_cc.verify.sql")" | tee "$EV/verify-2.txt" | grep -v '^PASS|'   # INFO + END only
```

**Stop if:** the test connection fails or prints anything but
`ai_smm_cc`; any `FAIL`. Do not proceed with a password that was ever
echoed or written elsewhere — set a new one.

### 7.4 R4 — Stage the new compose file and scripts (approval A3)

Still no runtime effect: the running `smm-api` keeps its current
configuration until `up`.

```bash
cd "$CC"
install -d -m 700 "$EV/prev"
cp -p compose.control-center.yml cc-common.sh cc-gate.sh cc-compose.sh cc-db-backup.sh \
      control-center.window "$EV/prev/"
( cd "$EV/prev" && sha256sum * > SHA256 )
# the approved window: the new wrapper's preflight runs the queue gate
printf 'SMM_CC_WINDOW_START=%s\nSMM_CC_WINDOW_END=%s\n' '<start, UTC>' '<end, UTC>' > control-center.window
# scripts and the verify file first ...
cp -p "release-$M"/{cc-common.sh,cc-gate.sh,cc-compose.sh,cc-db-backup.sh,ai_smm_cc.verify.sql} .
# ... the compose file LAST: the previous wrapper with the new compose file
# would start smm-api on control-center.api.env without the API role gate.
cp -p "release-$M/compose.control-center.yml" .
chmod 644 compose.control-center.yml cc-common.sh cc-gate.sh ai_smm_cc.verify.sql
chmod 755 cc-compose.sh cc-db-backup.sh
# the installed copies against the manifest: six lines "OK"
grep -E '  (compose\.control-center\.yml|cc-(common|gate|compose|db-backup)\.sh|ai_smm_cc\.verify\.sql)$' \
  "release-$M/MANIFEST-$M.sha256" | sha256sum -c -
./cc-compose.sh config --quiet && echo config-ok
./cc-compose.sh config --services                  # smm-api smm-web
./cc-compose.sh ps                                  # unchanged: same smm-api container
./cc-compose.sh preflight                           # identity, API role and queue gates; starts nothing
```

If anything in this step fails, put the previous files back before any
`up` (RB-1's `cp` from `$EV/prev`, §7.8); no container has changed yet.

### 7.5 R5 — Switch `smm-api`

```bash
./cc-compose.sh up 2>&1 | tee "$EV/up.txt"
```

`up` runs the identity gate, then the API role gate (verify file, no
`FAIL`), and only then Compose. Compose sees `smm-api`'s environment
changed and **recreates `smm-api` only**; `smm-web` stays (`Running`).
The old container's connections (as `ai_smm`, ≤ 7) close; the new one
opens its own (as `ai_smm_cc`, ≤ 7, limit 10) on first use. The worker's
connections and container are not involved. A refused gate exits 2 and
leaves the old `smm-api` running untouched.

### 7.6 R6 — Checks after the switch

```bash
curl -fsS http://127.0.0.1:8088/health/ready; echo                     # 200 ready
./cc-compose.sh ps
API_IP=$(docker inspect "$(./cc-compose.sh ps -q smm-api)" \
  --format '{{(index .NetworkSettings.Networks "<shared-network>").IPAddress}}')
ro "SELECT usename, application_name, client_addr, state, count(*)
      FROM pg_stat_activity WHERE datname = 'ai_smm' GROUP BY 1,2,3,4" | tee "$EV/activity-after.txt"
# From $API_IP: usename ai_smm_cc only (application_name still ai-smm/worker-1, FU-024C1-1).
# usename ai_smm: the worker's (and the nightly backup's) connections only.

# The API's own connection: reads work, a business UPDATE is refused BY POSTGRESQL.
# WHERE false and the refusal: no row can change. Prints no secret.
docker exec -i "$(./cc-compose.sh ps -q smm-api)" /app/.venv/bin/python - <<'PY'
import os, sqlalchemy as sa
e = sa.create_engine(os.environ["AI_SMM_DATABASE_URL"])
with e.connect() as c:
    print("current_user", c.execute(sa.text("SELECT current_user")).scalar())
    print("publications readable", c.execute(sa.text("SELECT count(*) >= 0 FROM publications")).scalar())
    for sql in ("UPDATE publications SET status = status WHERE false",
                "DELETE FROM publications WHERE false",
                "SELECT id FROM publications WHERE false FOR UPDATE SKIP LOCKED"):
        try:
            c.execute(sa.text(sql)); print("NOT REFUSED:", sql)
        except sa.exc.ProgrammingError as exc:
            print("refused", exc.orig.sqlstate, sql.split()[0])
        c.rollback()
PY
ro "$(cat ai_smm_cc.verify.sql)" | tee "$EV/verify-3.txt" | grep -v '^PASS|'
ro "$(cat "release-$M/session-anomalies.sql")" | tee "$EV/session-anomalies.txt"   # END|anomalies|0
```

Expected: `current_user ai_smm_cc`, `publications readable True`, three
lines `refused 42501`. This is FU-024B3-1's "done when".

Then the read-only smoke test and the browser walk of the existing
runbook (step 11, through the SSH tunnel): login, `/auth/me`
(`mutations_enabled=False`), the cancel probe refused with
`MUTATIONS_DISABLED`, logout; in the browser dashboard, publications,
attempts, preview, series, content, audit; sign out. And then:

```bash
./cc-compose.sh logs --since 15m smm-api > "$EV/smm-api-after.log"
# Unexpected refusals: 0. The deliberate one is excluded and counted on its own.
grep -iE 'permission denied|InsufficientPrivilege' "$EV/smm-api-after.log" \
  | grep -vc 'Password hash upgrade skipped'                                   # 0
grep -c 'Password hash upgrade skipped' "$EV/smm-api-after.log"               # see below
```

The second count is the number of logins whose stored hash needed an
upgrade the role may not write (§4.3). The production hashes were created
on 2026-10-11 with the same argon2-cffi as image `M` — check
`uv.lock` — so 0 is expected; a non-zero count is not a failure, but
record it (O9).

### 7.7 R7 — The worker is unchanged

The existing runbook's step 12, unchanged: the worker line identical to
`$EV/worker-before.txt`, its log still cycling, `docker compose ls`
unchanged, publications and attempts not changed by this operation.

**Rollback triggers (any of R5–R7):** readiness not 200 within 2 minutes;
any smoke check failing; any `permission denied` in the API log; any
`NOT REFUSED`; any doubt about the worker.

### 7.8 Rollback

**RB-1 — configuration rollback (minutes, no SQL).** Puts `smm-api` back
on the owner URL with the previous compose file and scripts (images stay
`M`). It does not restore `control-center.window` or remove
`ai_smm_cc.verify.sql`: the previous wrapper does not read the verify
file and reads the window (same format) only for `preflight` and
`migrate`. Its `up`
recreates only `smm-api` (`smm-web` keeps running) and never touches the
worker, which is not part of this Compose project. Ran once in production,
on 2026-10-13, and worked:

```bash
cd "$CC"
( cd "$EV/prev" && sha256sum -c SHA256 )
cp -p "$EV/prev"/{compose.control-center.yml,cc-common.sh,cc-gate.sh,cc-compose.sh,cc-db-backup.sh} .
./cc-compose.sh up                       # previous wrapper: identity gate; recreates smm-api with control-center.env
curl -fsS http://127.0.0.1:8088/health/ready; echo
ro "SELECT usename, count(*) FROM pg_stat_activity WHERE datname = 'ai_smm' GROUP BY 1"   # no ai_smm_cc
```

The images stay `M` (P1's state); to go back to `8ccccda` as well, follow
with P1's rollback. `control-center.api.env` may stay (the previous scripts
ignore it) or be
removed with `shred -u` — but only after the previous files are back: the
new scripts need it for **every** verb, `down` and `ps` included (Compose
cannot load the project without it), and refuse without it. The role stays, unused. Data impact: none — the
role could only write sessions and audit rows; sessions survive the
switch in both directions, so signed-in users stay signed in.

**RB-2 — disable the role (seconds, reversible).** When it must not be
usable while the cause is investigated: `ALTER ROLE ai_smm_cc NOLOGIN`
(run as `$PGU` in `ai_smm`). Existing sessions are not cut; after RB-1
there are none. Undo: `ALTER ROLE ai_smm_cc LOGIN`.

**RB-3 — retire the role (owner decision).** After RB-1, with no session:

```bash
ro "$(cat "release-$M/acl-snapshot.sql")" > "$EV/acl-before-retire.txt"
docker exec -i "$PGC" psql -X -v ON_ERROR_STOP=1 -U "$PGU" -d ai_smm < "release-$M/ai_smm_cc.revoke.sql"
ro "$(cat "release-$M/acl-snapshot.sql")" | diff "$EV/acl-before-retire.txt" - && echo "no other role changed"
shred -u control-center.api.env
```

The revoke file refuses while the role has a session, owns anything or
has a membership.

Abandoning after R2 or R3 (before R5): nothing uses the role; leave it or
run RB-3.

### 7.9 Later operations that involve the role

- **A migration that creates or recreates a table.** After `migrate
  upgrade`, re-run `ai_smm_cc.grants.sql` (idempotent, converges) and the
  verify file; a recreated table has lost its grants (no default
  privileges). A new table the API must read needs the grants file, the
  verify file and the test matrix updated in the same release —
  `test_every_model_table_has_a_privilege_decision` fails until it is.
  `up`/`restart` refuse while the verify file reports a `FAIL`.
- **Password rotation.** R3 with a new password, then
  `./cc-compose.sh restart smm-api` (the gate runs). Existing API
  connections keep working until the restart closes them.
- **Restore.** `cc-db-backup.sh verify` restores with `--no-privileges`:
  unaffected. A dump restored onto an instance **without** the role
  prints `role "ai_smm_cc" does not exist` for its `GRANT`s — create the
  role first (R2, R3) or restore with `--no-privileges` and run the
  grants file after. Restoring a dump taken before R2 over the live
  database (the last resort of the rollback runbook) brings back the old
  ACLs: run the grants file again.

## 8. Shared instance: constraints and residual risks

The PostgreSQL 16 instance is shared with other projects (n8n and
others). What this changes there, and what it does not:

- **A role is cluster-wide.** `ai_smm_cc` exists for every database on the
  instance. R1 stops if the name is taken; the grants file refuses an
  existing role that differs from the design instead of altering it.
- **Other databases.** PostgreSQL gives `PUBLIC` `CONNECT` on every new
  database, so `ai_smm_cc` — like the owner role `ai_smm` today — can
  open a session in another project's database and read its catalogs. It
  cannot read their tables unless those were granted to `PUBLIC`, and
  cannot create objects there unless `PUBLIC` has `CREATE` on their
  `public` schema (default only for databases created before
  PostgreSQL 15). R1 records both facts per database; the verify file
  lists the connectable ones as `INFO`. Closing this needs a change owned
  by the other projects or by the instance (`REVOKE CONNECT … FROM PUBLIC`
  on their databases, or a `pg_hba.conf` line restricting `ai_smm_cc` to
  `ai_smm`) — not done here, open item O3.
- **`TEMPORARY` in `ai_smm`** (a `PUBLIC` default): the role can create
  temporary tables. They vanish with the session and are bounded by
  `statement_timeout`; `temp_file_limit` could be set on the role if
  wanted (O6).
- **Connections.** The API's connections move from `ai_smm` to
  `ai_smm_cc`: the same number (≤ 7), so `max_connections` sees no
  change; `CONNECTION LIMIT 10` caps the API. The worker's connections are
  never closed or touched. In `pg_stat_activity` the API is now told
  apart from the worker by `usename` even though its `application_name`
  is still `ai-smm/worker-1` (FU-024C1-1 — mitigated for operators, not
  fixed).
- **Locks.** Creating the role and granting take catalog locks only
  (`pg_authid`, `pg_db_role_setting`, `pg_shdescription`), never a lock on
  an application table; measured in §6. `lock_timeout=5s` inside the file
  bounds any wait.
- **Timeouts are per role.** `statement_timeout`, `lock_timeout` and
  `idle_in_transaction_session_timeout` are set on `ai_smm_cc` only; no
  instance setting changes.
- **Logs and statistics.** The SQL files carry no secret. `\password`
  sends a SCRAM verifier, not the password; with `log_statement` at
  `ddl`/`all` that verifier would be logged — not usable as a password,
  and with ≥ 40 random characters not practically reversible. R1 records
  `log_statement`.
- **Role dumps.** A `pg_dumpall --roles-only` taken after R2 (operations
  §8 keeps such snapshots) contains the role's SCRAM verifier: keep it at
  mode 600 like the others (O5).
- **Backups.** See §7.9, *Restore*.

## 9. SMM-025: business mutations

Nothing here grants what the authoring commands will need, on purpose.
With this role, turning `AI_SMM_API_MUTATIONS_ENABLED` on makes every
business command fail in PostgreSQL (§4): flipping the flag alone can no
longer let an admin schedule a post the live worker would send. Enabling
mutations therefore *requires* the SMM-025 privilege design first — an
interlock, recorded in FU-024C1-5's checklist.

Constraints for that design, from what this stage established:

- keep it a separate, reviewed matrix (a second role, or a reviewed
  extension of this one) with the same three files and the same gate;
- `publications`: column-level `UPDATE` limited to what the three
  commands write today (`ai_smm.queue.reschedule` / `cancel`): `status`,
  `scheduled_at`, `next_attempt_at`, `claimed_by`, `claimed_at`,
  `lease_expires_at`, `last_error`, `updated_at`, and `attempt_count`
  (reset on request). The commands only ever *clear* the lease columns;
  whether the role may write them at all, or a narrower path is built,
  is that stage's decision. Never `threads_post_id`, `published_at`,
  `body`, `images`, `human_reviewed` or the series columns by UPDATE.
  `SELECT … FOR UPDATE` comes with any column `UPDATE`, so the command ↔
  worker locking (row lock vs `SKIP LOCKED`) must be re-verified on the
  role (FU-024C1-5);
- `content_revisions`, `content_approvals`, `content_publication_links`:
  `INSERT` only (the append-only triggers stay the second line);
  `content_items`, `project_settings`: column-level `INSERT`/`UPDATE`;
  `USAGE` on `publications`'s sequence for materialisation;
- still no `DELETE`, `TRUNCATE`, DDL, membership, default privileges, or
  anything on `alembic_version`, `publication_attempts` (the worker's
  record) or `users`/`project_memberships` beyond today's;
- the failures recorded in §6.2 (`AI_SMM_TEST_API_ROLE=restricted`) are
  the starting list of what that role must allow, command by command.

## 10. Open items and blockers

**Blocking the production rollout — all resolved (SMM-024C-2B):**

| # | Item | Resolution |
|---|---|---|
| B1 | Approvals A0 (images `M` on the current configuration), A1 (role and grants on the shared instance), A2 (credential), A3 (switch `smm-api`) — §7 | granted and executed 2026-10-12 – 2026-10-13 |
| B2 | Build `ai-smm-api` and `ai-smm-web` from the merge commit `M` (required since 2A-FIX: `auth.py` changed) and the release bundle with its manifest | built from `0cbd641`, reproducible; loaded under A0 (P1) |
| B3 | Facts only the read-only baseline R1 can establish on the host: the role name is free; every application table is owned by `ai_smm`; `PUBLIC` has no `CREATE` on `public`/`ai_smm`; no `trust` rule applies to connections from the shared network | confirmed by the read-only preflight (SMM-024C-2B-1) and again before A1 |
| B4 | Review and merge of this branch | merged to `main` as `0cbd641` |

**Open, not blocking:**

| # | Item |
|---|---|
| O1 | FU-024C1-1: the API's `application_name` is still `ai-smm/worker-1`. After the switch `usename` tells the API apart in `pg_stat_activity`; the runbook queries that filter on `application_name` should then filter on `usename = 'ai_smm_cc'` |
| O2 | With the database unreachable or the credentials wrong, `/health/ready` answers 503 but login and other endpoints answer **500 `INTERNAL_ERROR`**, not 503 (existing behaviour, unchanged here; the error carries no internals) — a candidate follow-up |
| O3 | `ai_smm_cc`, like `ai_smm`, can connect to the other databases on the instance (`PUBLIC CONNECT`); closing it is the instance's or the other projects' change (§8) |
| O4 | The migration runbook must re-run the grants file after any migration that creates or recreates a table (§7.9); to be added to the deployment runbook with the next migration |
| O5 | Role-only dumps taken after R2 contain the role's SCRAM verifier: keep them at mode 600 |
| O6 | Optional: `temp_file_limit` on the role |
| O7 | SMM-025 needs its own privilege design before mutations are enabled (§9); with this role in place, enabling mutations without it fails closed |
| O8 | Session plausibility check in `resolve_session` (lifetime, future timestamps) — no migration, a small application change; proposed, not implemented (§4.1) |
| O9 | No CLI command sets a password; under the role, an outdated Argon2 hash is only upgraded by such a reset (§4.3) |
| O10 | **Security risk, accepted for read-only mode:** a compromised API connection can create sessions for any user and write audit entries with any actor/action (§4.1, §4.2). Prevention needs migrations (session-creation function or trigger; `written_by` column + `CHECK` on `audit_log`) — an architectural decision, required before SMM-025. Registered as **FU-024C2-1**, **FU-024C2-2**; the mutation privilege model as **FU-024C2-3** ([follow-ups.md](follow-ups.md)) |
| O11 | `_upgrade_password_hash`: flush pending changes before the SAVEPOINT so that only the hash write can be absorbed (fail-closed today, wrong diagnostics) — **FU-024C2-4** |
