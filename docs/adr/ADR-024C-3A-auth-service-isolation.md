# ADR-024C-3A — Authentication service isolation (`smm-auth`, `ai_smm_auth`)

| | |
|---|---|
| Status | **Proposed — documentation review.** The owner accepted the architectural direction (a separate `smm-auth` service with its own PostgreSQL role) with two mandatory corrections, both incorporated here (§3.2) |
| Project | PRJ-013 AI SMM Department 2.0 |
| Baseline | `main` at `dc2d54ee6d96a50cd7064a73f4cc7eaa8762efe4` (production release `0cbd641`) |
| Addresses | [FU-024C2-1](../follow-ups.md#fu-024c2-1--session-forgery-through-the-apis-database-connection) (session forgery) in full for session creation; prepares [FU-024C2-2](../follow-ups.md#fu-024c2-2--authenticity-of-audit-actor-and-action) and [FU-024C2-3](../follow-ups.md#fu-024c2-3--a-privilege-model-for-business-mutations) |
| Implemented by | SMM-024C-3B (planned). Nothing in this document is implemented yet |
| Does not change | the worker, the publication queue, any table, `alembic_version`, `AI_SMM_API_MUTATIONS_ENABLED` (stays the literal `"false"`) |

This ADR was validated against the code at the baseline. Every statement
about current behaviour carries a file and line reference; statements
about the production host are marked as **facts to verify read-only**
(§16) and are not assumed.

## 1. Context

Since 2026-10-13 the Control Center API (`smm-api`) connects as the
least-privilege role `ai_smm_cc`
([least-privilege-role.md](../least-privilege-role.md)). PostgreSQL
refuses every business write from it. Login and logout still need writes,
so the role keeps:

- `INSERT` on all seven columns of `user_sessions`, `UPDATE (last_seen_at,
  revoked_at)` ([grants.sql:150-153](../../deploy/control-center/db/ai_smm_cc.grants.sql#L150-L153));
- `UPDATE (last_login_at, updated_at)` on `users`
  ([grants.sql:160](../../deploy/control-center/db/ai_smm_cc.grants.sql#L160));
- `INSERT (actor, action, subject, details)` on `audit_log` and its
  sequence ([grants.sql:164-173](../../deploy/control-center/db/ai_smm_cc.grants.sql#L164-L173));
- table-level `SELECT` on `users`, **including `password_hash`**
  ([grants.sql:138-144](../../deploy/control-center/db/ai_smm_cc.grants.sql#L138-L144)).

Whoever controls that connection — injected SQL, a leaked API credential,
code execution in the API — can therefore:

1. create a session for **any** user with a token it knows
   ([least-privilege-role.md §4.1](../least-privilege-role.md#41-sessions-what-is-and-is-not-guaranteed));
2. revoke any session, and — not recorded before this ADR — **un-revoke**
   one by setting `revoked_at = NULL`, so a stolen cookie works again
   after its owner logged out;
3. read every Argon2 hash for offline cracking;
4. write `auth.*` audit rows with any actor.

All of these are accepted for the read-only mode and all of them block
business mutations ([FU-024C1-5](../follow-ups.md#fu-024c1-5--verify-the-safety-gates-before-publication-mutations-are-switched-on)).

### 1.1 Threat model

| Scenario | Today | After this ADR | Inherent remainder |
|---|---|---|---|
| **S1** unauthenticated requests, from the SSH tunnel or from any container on `<shared-network>` (which reaches `smm-api:8000` directly, [FU-024B3-2](../follow-ups.md#fu-024b3-2--take-the-api-off-the-shared-network)) | password guessing without any rate limit (no limiter exists in `src/ai_smm/api`) | `smm-auth` has no listener on `<shared-network>` (§9); login is rate-limited (§11.3) | guessing through the tunnel, bounded |
| **S2** signed-in user | RBAC in the API | unchanged | — |
| **S3** leaked `ai_smm_cc` credential, **S4** arbitrary SQL as `ai_smm_cc` | items 1–4 above | cannot create, revoke or un-revoke sessions, cannot read `password_hash`, cannot write audit rows | §12: read session metadata, keep sessions alive up to their absolute expiry |
| **S5** code execution in `smm-api` | items 1–4, plus every password typed at login | as S3/S4; passwords never reach `smm-api` | acts as any user whose cookie it sees while compromised |
| **S5a** code execution in `smm-auth` | — | can create a session for any user | inherent: whoever verifies the password decides the identity. `smm-auth` is kept minimal for that reason |
| **S6** owner/worker role `ai_smm` | everything | everything | outside the database only (off-host audit, backups) |

## 2. Decision

1. Login and logout move to a new service **`smm-auth`**: the existing
   `ai-smm-api` image with a separate application factory that mounts
   only the authentication routes and the health probes. No new image, no
   new infrastructure component, no Redis, no second host.
2. `smm-auth` connects as a new role **`ai_smm_auth`**. It writes only
   through four `SECURITY DEFINER` functions in a schema `smm_auth`
   (§8). Session creation sets every timestamp on the server and writes
   the `auth.login` row in the same transaction.
3. `smm-api` stays on `ai_smm_cc`. After compatibility is proven, the
   role loses every authentication write and the `password_hash` column
   (grants **G2**, §6).
4. `smm-api` keeps resolving sessions in Python for now (§12). Moving that
   into the database is a prerequisite of business mutations (SMM-024C-3D).
5. No component that faces the network may write `password_hash` (§11).

## 3. Amendments from validation

### 3.1 Amendments found in the source review

| # | Amendment | Reason in the code |
|---|---|---|
| A1 | `ai_smm_cc` gets **no** `USAGE` on `smm_auth`, no `EXECUTE` on its functions and no new role settings in this ADR | the pinned verify file of the running release fails on `schema.other.usage` ([verify.sql:152-157](../../deploy/control-center/db/ai_smm_cc.verify.sql#L152-L157)) and on any role-settings set other than the three it expects ([verify.sql:104-110](../../deploy/control-center/db/ai_smm_cc.verify.sql#L104-L110)); the gate would then refuse `up` and a rollback to `0cbd641` could not start |
| A2 | `smm-api` keeps Python session resolution until SMM-024C-3D | an early switch would break A1 and hold the session row lock for a whole request (§7.4) |
| A3 | `hide_parameters=True` on the engines of `smm-api` and `smm-auth` | §10 |
| A4 | the ORM in `smm-api` stops loading `password_hash` | `db.get(User, …)` loads every column ([auth.py:239](../../src/ai_smm/application/auth.py#L239)). Superseded in its security role by correction 1 (§3.2) |
| A5 | the rehash function is defined but granted to no one | §11 |
| A6 | the functions are installed by a superuser-run SQL file, not by an Alembic revision | `migrate` runs the image named by the single `SMM_CC_VERSION` ([compose.control-center.yml:172-186](../../compose.control-center.yml#L172-L186)); a revision would tie the migration to the image switch and move `alembic_version`. A SQL file touches no table and rolls back on its own |

### 3.2 Mandatory corrections from the owner's review

**Correction 1 — column-level `SELECT` on `users`.** ORM `deferred()` or
`load_only()` is an application convenience, not a security boundary: a
compromised API can issue any SQL its role allows. The boundary is
PostgreSQL. G2 therefore revokes the **table-level** `SELECT` on `users`
from `ai_smm_cc` and grants `SELECT` on named columns only, excluding
`password_hash`. The ORM adaptation, the verification and the negative
test are in §6.2; restoring G1 for a rollback to `0cbd641` is in §6.3.

**Correction 2 — scoped P5 rollback.** Removing `smm-auth` must not stop
`smm-api` and `smm-web` and must never touch shared infrastructure. The
primary rollback is scoped to the `smm-auth` container and the
`ai-smm-cc-auth` network; a complete project `down` is only a tested
fallback (§14.4). Both are rehearsed locally with Compose 5.1.3, the
version production runs.

## 4. Trust boundaries

```
 Browser ──ssh -L──► 127.0.0.1:8088 ─► smm-web (nginx: static files + proxy)          [TB1]
                                        │ = /api/v1/auth/login, = /api/v1/auth/logout
                                        │     ──► smm-auth   role ai_smm_auth         [TB2]
                                        │ /api/*, /health/*
                                        │     ──► smm-api    role ai_smm_cc           [TB3]
 ┌──────────────────────── PostgreSQL ai_smm (shared instance) ───────────────────────┐
 │ smm_auth.* SECURITY DEFINER, owner ai_smm, EXECUTE: ai_smm_auth only              │
 │ tables: ai_smm_cc reads (users without password_hash), no auth writes after G2    │  [TB4]
 └────────────────────────────────────────────────────────────────────────────────────┘
          ▲ owner ai_smm: worker (LIVE), migrate, cli — trusted, untouched              [TB5]
```

- **TB1** — the SSH tunnel and the single loopback port; nginx is the
  only ingress.
- **TB2** — `smm-auth` decides who a user is. Its compromise is S5a.
- **TB3** — `smm-api` is not trusted to decide identity in the database:
  after G2 it cannot create a session or read a password hash.
- **TB4** — PostgreSQL privileges, the actual boundary.
- **TB5** — the owner role; outside this ADR's protection (S6).

## 5. Responsibility split

| Responsibility | Today | `smm-auth` | `smm-api` |
|---|---|---|---|
| Password verification, decoy hash for unknown e-mail, inactive user refused | [auth.py:80-110](../../src/ai_smm/application/auth.py#L80-L110) | ✅ | ❌ (after G2 it cannot read the hash) |
| Optional Argon2 rehash | [auth.py:113-155](../../src/ai_smm/application/auth.py#L113-L155) | **not attempted**: logs "outdated hash" only (§11) | ❌ |
| Session creation + `auth.login` | [auth.py:169-197](../../src/ai_smm/application/auth.py#L169-L197), [routers/auth.py:53-107](../../src/ai_smm/api/routers/auth.py#L53-L107) | ✅ `smm_auth.session_create` | ❌ after G2 |
| `auth.login_failed` + rate limit | [routers/auth.py:70-84](../../src/ai_smm/api/routers/auth.py#L70-L84); no limiter | ✅ | ❌ |
| Logout: session + CSRF check, revoke, `auth.logout`, clear cookie | [routers/auth.py:115-133](../../src/ai_smm/api/routers/auth.py#L115-L133), [csrf.py:45-86](../../src/ai_smm/api/csrf.py#L45-L86) | ✅ | not mounted after P7 |
| Session resolution and `last_seen_at` on every request | [auth.py:200-250](../../src/ai_smm/application/auth.py#L200-L250) | for logout only (in SQL) | ✅ in Python (§12) |
| `GET /auth/me`, `GET /auth/csrf`, CSRF guard of business routes, mutation gate, RBAC | [routers/auth.py:141-175](../../src/ai_smm/api/routers/auth.py#L141-L175), [app.py](../../src/ai_smm/api/app.py) | ❌ | ✅ (needs membership tables) |
| Health probes | [health.py](../../src/ai_smm/api/routers/health.py) | its own, for the Docker healthcheck only | its own; the only ones nginx proxies |

`smm-auth` reuses unchanged: the error envelope and the generic
`invalid_credentials` answer ([errors.py:92-107](../../src/ai_smm/api/errors.py#L92-L107)),
validation errors that never echo values ([errors.py:230-252](../../src/ai_smm/api/errors.py#L230-L252)),
`LoginRequest` with `SecretStr` and `extra="forbid"`
([schemas.py:27-31](../../src/ai_smm/api/schemas.py#L27-L31)), the request-id
middleware, the cookie module and the per-request transaction that
commits **before** the response is sent
([dependencies.py:54-87](../../src/ai_smm/api/dependencies.py#L54-L87)) — the
SPA calls `GET /auth/me` immediately after login.

## 6. Database privileges

### 6.1 Matrix

G1 = the grants in production today. G2 = `ai_smm_cc` at the end of
SMM-024C-3B. GA = `ai_smm_auth`. "3D" is the target before business
mutations (§15), listed so that G2 is not mistaken for it.

| Object / privilege | `ai_smm` (owner, worker, CLI) | `ai_smm_cc` G1 | `ai_smm_cc` G2 | `ai_smm_cc` 3D | `ai_smm_auth` GA | `PUBLIC` |
|---|---|---|---|---|---|---|
| `CONNECT` on `ai_smm` | owner | ✅ | ✅ | ✅ | ✅ | ❌ |
| `USAGE` on schema `public` | owner | ✅ | ✅ | ✅ | ✅ | ❌ |
| `USAGE` on schema `smm_auth` | owner | ❌ | ❌ (A1) | ✅ | ✅ | ❌ |
| `CREATE` on `public` or `smm_auth` | owner | ❌ | ❌ | ❌ | ❌ | ❌ |
| `users` table-level `SELECT` | ✅ | ✅ | **❌** | ❌ | **❌** | ❌ |
| `users` column `SELECT` | all | all (via table) | `id, email, display_name, is_active, last_login_at, created_at, updated_at` | same | `id, email, password_hash, display_name, is_active, last_login_at, created_at, updated_at` | ❌ |
| `users.password_hash` `SELECT` | ✅ | ✅ | **❌** | ❌ | ✅ (verification) | ❌ |
| `users.password_hash` `UPDATE` | ✅ (CLI only) | ❌ | ❌ | ❌ | ❌ | ❌ |
| other `users` `UPDATE` | ✅ | `last_login_at, updated_at` | ❌ | ❌ | ❌ (inside `session_create`) | ❌ |
| `user_sessions` `SELECT` | ✅ | ✅ | ✅ (§12, RS-1) | ❌ | ❌ | ❌ |
| `user_sessions` `INSERT` | ✅ | seven columns | **❌** | ❌ | ❌ (function) | ❌ |
| `user_sessions` `UPDATE` | ✅ | `last_seen_at, revoked_at` | `last_seen_at` only | ❌ | ❌ (function) | ❌ |
| `audit_log` `INSERT` + sequence `USAGE` | ✅ | ✅ | **❌** | ❌ (functions only) | ❌ (function) | ❌ |
| `SELECT` on the other application tables | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ |
| business writes | ✅ | ❌ | ❌ | ❌ (token-bound functions only) | ❌ | ❌ |
| `EXECUTE smm_auth.session_create` | owner | ❌ | ❌ | ❌ | ✅ | ❌ |
| `EXECUTE smm_auth.session_resolve` | owner | ❌ | ❌ | ✅ | ✅ | ❌ |
| `EXECUTE smm_auth.session_revoke`, `record_login_failure` | owner | ❌ | ❌ | `session_revoke` ✅ | ✅ | ❌ |
| `EXECUTE smm_auth.password_rehash` | owner | ❌ | ❌ | ❌ | ❌ (A5) | ❌ |
| role settings | — | the three timeouts (unchanged) | the same three (A1) | — | timeouts + `log_parameter_max_length=0` | — |

`ai_smm_auth` attributes: `LOGIN NOSUPERUSER NOINHERIT NOCREATEDB
NOCREATEROLE NOREPLICATION NOBYPASSRLS`, `CONNECTION LIMIT 5` (pool 2 +
overflow 1, plus headroom), no membership, owns nothing, password set
interactively with `\password` (SCRAM), never in a file in Git.

The `ai_smm` and `PUBLIC` columns are **not changed** by G1, G2 or GA;
the existing `acl-snapshot.sql` proves it before and after every step.

### 6.2 Column-level `SELECT` on `users` (correction 1)

**What G2 does, in one transaction:**

1. `REVOKE SELECT ON users FROM ai_smm_cc` — the table-level privilege,
   which by itself would expose every column, including columns added
   later.
2. `GRANT SELECT (id, email, display_name, is_active, last_login_at,
   created_at, updated_at) ON users TO ai_smm_cc` — named columns, the
   same convention G1 already uses for writes
   ([grants.sql:146-150](../../deploy/control-center/db/ai_smm_cc.grants.sql#L146-L150)).
   A column added later is denied until a reviewed grants file names it.

**ORM adaptation (required for function, not for security).** Every
query of `smm-api` that touches `users` must name its columns, because
PostgreSQL refuses a statement that references a denied column even if
the application never reads the value:

| Code path | Today | Required |
|---|---|---|
| `resolve_session` | `db.get(User, user_session.user_id)` loads all mapped columns ([auth.py:239](../../src/ai_smm/application/auth.py#L239)) | an explicit projection of the seven granted columns (`select(...)` of named columns or `load_only(...)`) |
| any relationship that lazily loads a `User` | full entity | the same projection, or no lazy load from `smm-api` |
| `GET /auth/me` and `UserOut` | uses fields of the loaded `User` ([routers/auth.py:178-186](../../src/ai_smm/api/routers/auth.py#L178-L186)) | unchanged fields, from the projection |

As a development guard, `password_hash` is mapped so that accessing it in
`smm-api` raises in the application (`raiseload`-style deferral) instead
of emitting SQL. This guard is not relied on: the full test suite runs
under G2 with the restricted role (`AI_SMM_TEST_API_ROLE=restricted`), so
any path that still references the column fails in PostgreSQL with 42501.

**Verification that nothing re-exposes the column** (to be added to the G2
verify file as `FAIL` checks):

| Check | Expected under G2 |
|---|---|
| `has_table_privilege('ai_smm_cc', 'users', 'SELECT')` | **false** (a table-level grant would cover every column) |
| `has_column_privilege('ai_smm_cc', 'users', 'password_hash', 'SELECT')` — **effective**, so it includes `PUBLIC` and any inherited role | **false** |
| `has_column_privilege('public', 'users', 'password_hash', 'SELECT')` | false |
| each of the seven named columns | true; every other column false |
| `ai_smm_cc` membership in any role, `rolinherit` | none, false (already checked by the current verify file) |
| views or materialized views in any schema that `ai_smm_cc` may `SELECT` | **0** (a view runs with its owner's rights and could expose the column) |
| `SECURITY DEFINER` functions `ai_smm_cc` may execute that read `users` | **0** in G2 (A1); in 3D, only reviewed functions whose results exclude `password_hash` |
| row-level security on `users` | not used; listed so that a later `BYPASSRLS` or policy change is reviewed here |

**Negative test on a real PostgreSQL 16** (connected as `ai_smm_cc`
under G2; each must fail with **SQLSTATE 42501**):

- `SELECT password_hash FROM users`
- `SELECT * FROM users` (the star includes the denied column)
- `SELECT u FROM users u` (whole-row reference)
- `COPY users TO STDOUT` and `COPY users (password_hash) TO STDOUT`
- `SELECT … FROM users JOIN …` that references `password_hash` in a
  `WHERE` clause only

and must succeed: `SELECT id, email, display_name, is_active,
last_login_at, created_at, updated_at FROM users`.

### 6.3 Restoring G1 (rollback to `0cbd641`)

**No assumption about column privileges.** G1 starts with table-level
`REVOKE ALL` ([grants.sql:120-132](../../deploy/control-center/db/ai_smm_cc.grants.sql#L120-L132))
and then re-grants its own set. This ADR does **not** rely on that
table-level revoke also removing column-level privileges that G2 granted
separately — for example `SELECT` on the seven `users` columns. Such a
leftover is dangerous because nothing shows it: while G1's table-level
`SELECT` covers the column, every effective-privilege check passes,
including the R0 verify file, which checks effective privileges only
([verify.sql:1-21](../../deploy/control-center/db/ai_smm_cc.verify.sql#L1-L21)).
The leftover would only surface the next time the table-level grant is
revoked. Restoring G1 is therefore three operations plus an explicit
comparison.

1. **Baseline, before G2 is first applied** (P7 step 1, §14.2): a
   read-only **raw-ACL snapshot of `ai_smm_cc`**. It is a new read-only
   query file delivered with SMM-024C-3B, the counterpart of the existing
   `acl-snapshot.sql`, which covers every *other* role. It lists every
   `aclexplode` entry naming `ai_smm_cc` in `pg_database.datacl`,
   `pg_namespace.nspacl` (every schema), `pg_class.relacl` (every
   relation), `pg_attribute.attacl` (every column) and `pg_proc.proacl`
   (every function), plus the role's attributes, memberships and
   settings. The output is sorted and its sha256 is recorded in the
   evidence directory.
2. **Rollback prelude**, a separate superuser operation in one
   transaction, run immediately before G1. A new file delivered with
   SMM-024C-3B (working name `ai_smm_cc.rollback-to-g1.sql`):
   - revokes every **column-level** privilege that `pg_attribute.attacl`
     records for `ai_smm_cc` on any table in `public`, one column at a
     time;
   - revokes everything `ai_smm_cc` holds on schema `smm_auth` and on its
     functions, if that schema exists. Under A1 there should be nothing,
     so this is defensive;
   - changes no role attribute or setting, and **refuses** if they differ
     from G1's: they belong to G1's own preflight, not to a cleanup;
   - never names another role.

   After the prelude, `ai_smm_cc` holds no column-level privilege at all,
   and G1 starts from a known floor.
3. **G1**, the file **byte-identical** to
   `deploy/control-center/db/ai_smm_cc.grants.sql` at commit `0cbd641`.
   Its sha256 is recorded in the release manifest; it is taken from the
   R0 release bundle and never regenerated or edited. The cleanup lives
   in the prelude precisely so that this file can stay unchanged.
4. **Explicit verification.** All of the following are required, and
   the rollback is not complete until they pass:
   - the R0 verify file (pinned in R0's
     [cc-gate.sh:72-73](../../deploy/control-center/cc-gate.sh#L72-L73))
     reports `0 FAIL` — effective privileges;
   - the raw-ACL snapshot of `ai_smm_cc` is **identical** (same sha256) to
     the baseline of step 1 — this catches a column grant hidden under a
     table grant, which no effective check can see;
   - `acl-snapshot.sql` for every other role is unchanged;
   - the probes of the test table below pass.

   If the raw snapshot differs, the state is not G1. Re-run the prelude
   and G1, and do not continue to a P5 rollback or to R0 images until the
   snapshots match.

`G1` does not know schema `smm_auth`; under A1 `ai_smm_cc` never had
anything there, and the prelude makes sure of it.

**Tests on PostgreSQL 16 (G1 → G2 → G1)**, on the rehearsal database
with production's ACLs. They run as `ai_smm_cc` unless stated otherwise,
and every write probe runs in a transaction that is rolled back.

| ID | After | Probe | Expected |
|---|---|---|---|
| G-P1 | G1 restored | raw-ACL snapshot vs. the pre-G2 baseline | identical sha256 |
| G-P2 | G1 restored | R0 verify file; `acl-snapshot.sql` of the other roles | `0 FAIL`; unchanged |
| G-P3 | G1 restored | the `0cbd641` login writes: `INSERT` into `user_sessions` (seven columns), `UPDATE user_sessions SET revoked_at`, `UPDATE users SET last_login_at, updated_at`, `INSERT` into `audit_log` (four columns) | succeed |
| G-P4 | G1 restored | `SELECT password_hash FROM users` | succeeds (the `0cbd641` login needs it) |
| G-P5 | G1 restored | the `0cbd641` test suite with `AI_SMM_TEST_API_ROLE=restricted` | only the 122 known business refusals |
| G-N1 | G1 restored | `pg_attribute.attacl` entries for `ai_smm_cc` | exactly G1's column grants: `user_sessions` `INSERT` (7) and `UPDATE` (2), `users` `UPDATE` (2), `audit_log` `INSERT` (4). **No** column-level `SELECT` anywhere |
| G-N2 | G1 restored, then in a test transaction as superuser: `REVOKE SELECT ON users FROM ai_smm_cc` | `SELECT id FROM users` as `ai_smm_cc` | **42501** — proves that no G2 column grant survived beneath the table grant |
| G-N3 | G1 restored | `USAGE` on `smm_auth`; `EXECUTE` on any `smm_auth` function; role settings | none; none; exactly the three timeouts |
| G-N4 | G1 restored | `INSERT` into `audit_log (id)` or `(at)`; `DELETE` on any table; `UPDATE publications`; `CREATE TABLE` in `public` | 42501 each |
| G-N5 | G2 applied | `pg_class.relacl` of `users`; `pg_attribute.attacl` of `users` | no `SELECT` for `ai_smm_cc` at table level; `SELECT` on exactly the seven named columns, not `password_hash` (plus the §6.2 negatives) |
| G-C1 | G2, then G1 **without** the prelude (characterisation only) | `pg_attribute.attacl` of `users` | the result is recorded for PostgreSQL 16.15. Whatever it shows, the procedure above does not change |

## 7. Session, cookie and CSRF compatibility

### 7.1 What must not change

| Property | Value | Source |
|---|---|---|
| Token | `secrets.token_urlsafe(32)`, 43 URL-safe ASCII characters | [tokens.py:19-25](../../src/ai_smm/security/tokens.py#L19-L25) |
| Stored form | `sha256(token.encode("utf-8")).hexdigest()` | [tokens.py:28-36](../../src/ai_smm/security/tokens.py#L28-L36) |
| CSRF token | `HMAC-SHA256(key=token, msg="ai-smm-csrf-v1")`, hex; no server secret | [tokens.py:43-65](../../src/ai_smm/security/tokens.py#L43-L65) |
| Cookie | `smm_session`, `Path=/`, `HttpOnly`, `SameSite=Lax`, `Max-Age` = lifetime, `Secure` from the environment (`false` behind the SSH tunnel) | [cookies.py:15-38](../../src/ai_smm/api/cookies.py#L15-L38), [compose.control-center.yml:68](../../compose.control-center.yml#L68) |
| Lifetime / idle | 7 days absolute, 24 hours idle; production does not override them | [config.py:180-185](../../src/ai_smm/config.py#L180-L185) |
| Audit shape | `auth.login`: actor and subject `user:<id>`, `details.session_id`; `auth.logout` the same; `auth.login_failed`: actor `anonymous`, subject `email:<address>` | [routers/auth.py:70-127](../../src/ai_smm/api/routers/auth.py#L70-L127); `session-anomalies.sql` depends on it |
| SPA paths and response shapes | `/api/v1/auth/{login,logout,me,csrf}`; login exempt from CSRF | [web/src/api/endpoints.ts:46-49](../../web/src/api/endpoints.ts#L46-L49), [web/src/api/client.ts:19](../../web/src/api/client.ts#L19) |

### 7.2 Consequences

- Sessions survive every step and every rollback: no step changes the
  `user_sessions` table or its rows, sessions are server-side, and cookies
  stay in the browser.
- A session created by `smm_auth.session_create` is accepted by the
  `0cbd641` code and by release N; a session created by `0cbd641` is
  accepted by `smm-auth`. Test T3 checks every combination of who created,
  who resolved and who revoked a session.
- The CSRF token needs no shared secret: both services derive it from the
  cookie. The value returned by login (from `smm-auth`) equals the value of
  `GET /auth/csrf` (from `smm-api`).
- Both services set and clear the cookie through the same module, so the
  attributes match and the browser never keeps two `smm_session`
  cookies (T4).
- The lifetime has one source of truth: `session_create` returns
  `expires_at`, and `smm-auth` derives `Max-Age` from it. The idle timeout
  exists twice (Python in `smm-api`, SQL in `smm-auth`) until 3D. Test T3
  holds both to the same boundary behaviour.

### 7.3 Logout and concurrent requests

- Requests that already resolved the session finish; the next request gets
  401 — as today. All of them are reads.
- `session_revoke` locks the row (`FOR UPDATE`) and writes `auth.logout`
  only when it changes `revoked_at` from `NULL`. A double logout writes one
  audit row; the second call answers 401, as an invalid session does today
  ([routers/auth.py:115-120](../../src/ai_smm/api/routers/auth.py#L115-L120)).
- `smm-api` updates only `last_seen_at`; the ORM writes changed columns
  only, so it never overwrites `revoked_at` written by `smm-auth`.

### 7.4 Why `smm-api` does not call `session_resolve` yet

Today `last_seen_at` is written by the ORM flush at commit: a short row
lock at the end of the request. A function that updates the row at the
start of the request would hold the lock for the whole request and
serialise parallel requests of one browser tab. When 3D moves resolution
into the database, the touch must be throttled (at most once per 60 s),
non-blocking (`SKIP LOCKED`) and in its own short transaction.

## 8. `SECURITY DEFINER` contracts

No SQL is part of this ADR; these are the contracts SMM-024C-3B implements.

### 8.1 Common rules and function-owner safety

- **Schema** `smm_auth`, owned by `ai_smm`. No role but the owner has
  `CREATE` on it, so no object can be planted next to the functions.
- **Owner** `ai_smm`. It already holds every right (S6), so ownership
  adds no new power. The functions are created by a superuser-run SQL
  file that switches to the owner role (A6). A narrower NOLOGIN owner role
  is an option for 3D (§15).
- **`SECURITY DEFINER`** with `SET search_path = pg_catalog, pg_temp`:
  `pg_temp` is listed last explicitly, so a temporary object cannot
  shadow anything. Every table is schema-qualified (`public.user_sessions`).
- **Default `EXECUTE` for `PUBLIC`** is revoked **in the same transaction**
  that creates each function; only then are explicit grants made (§6.1).
- **No dynamic SQL**, `VOLATILE`, no `LEAKPROOF`. The verify file pins
  `md5(prosrc)`, `prosecdef`, `proconfig` and the owner of every function.
- **Token hashing in SQL**: `encode(sha256(convert_to(p_token, 'UTF8')),
  'hex')`. `sha256(bytea)` is built into PostgreSQL since version 11, so no
  extension is needed; the instance runs 16. The token is ASCII, so the
  result does not depend on the server encoding. `p_token::bytea` must
  **not** be used: it interprets backslash escapes. T1 compares the SQL
  and Python digests.
- **Input**: `p_token` must be exactly 43 characters of `[A-Za-z0-9_-]`,
  otherwise SQLSTATE 22023. An unknown, revoked or expired token gives an
  empty result, with no reason distinguishable by the caller.

### 8.2 Contracts

| Function | Caller | Behaviour | Never accepts |
|---|---|---|---|
| `session_create(p_user_id uuid, p_token text) → (session_id uuid, expires_at timestamptz)` | `ai_smm_auth` | locks the user (`FOR SHARE`); an inactive or missing user gives an empty result. Inserts the session with `id = gen_random_uuid()`, `created_at = last_seen_at = now()`, `expires_at = now() + 7 days` (constant), `revoked_at = NULL`. Sets `users.last_login_at = updated_at = now()`. Writes `auth.login` in the existing shape (§7.1) in the same transaction, so the audit time equals `created_at` | timestamps, lifetime, actor, audit details |
| `session_resolve(p_token text) → (session_id, user_id, expires_at)` | `ai_smm_auth` (logout); `ai_smm_cc` only from 3D | `revoked_at IS NULL`, `expires_at > now()`, `last_seen_at + 24 h > now()`, active user — the same comparisons as [auth.py:226-242](../../src/ai_smm/application/auth.py#L226-L242). Touch: throttled to 60 s, skipped if the row is locked | a session id or user id |
| `session_revoke(p_token text) → (session_id, user_id, revoked boolean)` | `ai_smm_auth` | valid sessions only. `FOR UPDATE`, sets `revoked_at = now()` if `NULL`, writes `auth.logout` only on that transition | another session's identifier |
| `record_login_failure(p_email text) → integer` | `ai_smm_auth` | writes `auth.login_failed` in the existing shape. Returns the number of failures for that address in the rate-limit window, read through the existing `ix_audit_log_subject` index | a chosen action or actor |
| `password_rehash(p_user_id uuid, p_expected_hash text, p_new_hash text) → boolean` | **nobody** (A5) | compare-and-swap on the stored hash. Refuses unless the **stored** hash is weaker than the policy constants and the new hash matches the policy exactly (PHC `$argon2id$`, same m, t and p). Writes an audit row without the hash | — |

### 8.3 Inherent limit

`session_create` takes a `user_id`. That is acceptable **only** because
`EXECUTE` is limited to `ai_smm_auth`, whose single job is to verify the
password first. PostgreSQL cannot verify Argon2, so the service that
verifies the password decides the identity (S5a). That limit is the
reason `smm-auth` stays small and isolated (§9), not a reason to give the
function to any other role.

## 9. Network topology

### 9.1 What Compose allows

- `smm-api` is on `cc-internal` and `<shared-network>` and listens on
  `0.0.0.0:8000` ([compose.control-center.yml:116-120](../../compose.control-center.yml#L116-L120),
  [Dockerfile](../../Dockerfile) API stage).
- PostgreSQL is reachable **only** through the external `<shared-network>`
  ([compose.control-center.yml:234-238](../../compose.control-center.yml#L234-L238)).
  So `smm-auth` must join it, and Docker networks alone cannot stop other
  containers on that network from connecting to `smm-auth`. Attaching
  the database to a private network would change another project's stack
  ([FU-024B3-2](../follow-ups.md#fu-024b3-2--take-the-api-off-the-shared-network)).
  That is outside this ADR.

### 9.2 Decision

- A new network **`cc-auth`** (`ai-smm-cc-auth`), defined by this Compose
  project: `internal: true`, a small dedicated subnet in `ipam`, members
  `smm-web` and `smm-auth` only.
- `smm-auth` joins `cc-auth` with a **static address** and
  `<shared-network>`. It is **not** on `cc-internal`, so `smm-api` and
  `smm-auth` cannot reach each other.
- `smm-auth`'s uvicorn **binds only to its `cc-auth` address**. There is no
  listening socket on `<shared-network>`, so a connection from there is
  reset by the kernel before any HTTP parsing. Outbound connections to
  PostgreSQL over `<shared-network>` are unaffected.
- The `smm-auth` healthcheck targets the same address, not `127.0.0.1`.
- nginx reaches `smm-auth` by service name; Docker's DNS should answer
  with the address on the network both containers share (to be verified,
  §9.5).
- Second line, in the application, adding no component: `smm-auth`
  refuses a peer outside the `cc-auth` subnet. It catches the bind ever
  being widened back to `0.0.0.0` by mistake.
- `smm-web` does **not** depend on `smm-auth` being healthy. If
  `smm-auth` is down, login answers a JSON 502 and browsing with an
  existing session continues.

```
operator ─ssh -L─► 127.0.0.1:8088 ─► smm-web ─┬─ cc-internal (internal) ─► smm-api :8000 (0.0.0.0)
                     [cc-edge]                │                              │
                                              └─ cc-auth (internal) ───────► smm-auth :8000
                                                 static address; bind ONLY here│ (no listener on shared)
                                                                               │
 <shared-network> (external) ◄────────── smm-api ──────────────────────────────┤
     <postgres-container>    ◄────────── smm-auth (outbound only) ─────────────┘
     other projects' containers: reach smm-api:8000 (FU-024B3-2), NOT smm-auth
 worker: its own Compose project, never touched
```

### 9.3 Docker networks versus an IP allowlist

| Criterion | Dedicated network + bind address (chosen) | Application allowlist alone |
|---|---|---|
| TCP port reachable from `<shared-network>` | no | yes |
| Attack surface for neighbouring containers | none | HTTP parser and middleware |
| Where enforced | kernel and Docker | inside the process under attack |
| Configuration error | caught by the allowlist and by test T-N3 | one bug exposes the service |
| Cost | a fixed subnet; possible overlap with host networks (fact to verify, §16); the new network is created without recreating existing ones | a few lines of code |

The same technique for `smm-api` would need `cc-internal` to be
recreated with a subnet. That stays in FU-024B3-2.

### 9.4 Routes in nginx

| Location (nginx order) | Target | Notes |
|---|---|---|
| `= /api/v1/auth/login` | `smm-auth:8000`, address in a variable | `limit_req` (a global cap; all tunnel traffic shares one client address); `error_page 502 504` → `@auth_unavailable` |
| `= /api/v1/auth/logout` | `smm-auth:8000` | |
| `~ ^/(?:api\|health)(?:/\|$)` | `smm-api:8000` | includes `/api/v1/auth/me` and `/api/v1/auth/csrf` |
| `~ ^/(?:docs\|redoc\|openapi\.json)(?:/\|$)` | `smm-api:8000` | the API answers 404 |
| `@auth_unavailable` | JSON 502 `DEPENDENCY_UNAVAILABLE`, `details.dependency = "auth"` | same envelope as `@api_unavailable` |
| static locations | unchanged | |

- Exact-match locations take precedence over the existing regex location
  ([web/nginx/default.conf:64-66](../../web/nginx/default.conf#L64-L66)). The
  address goes in a variable with the existing resolver
  ([default.conf:53-55](../../web/nginx/default.conf#L53-L55)), so nginx
  starts while `smm-auth` is down.
- The nginx configuration is part of the web image
  ([web/Dockerfile](../../web/Dockerfile)), so a routing change means a new
  web image.
- `/api/v1/auth/login/` (trailing slash) reaches `smm-api`. Before P7,
  Starlette redirects it to `/login`; after P7 the route is not mounted
  there and G2 forbids session creation, so a misroute fails closed.

**Secrets:** `smm-api` gets `env_file: control-center.api.env`,
`smm-auth` gets `control-center.auth.env` (one key, mode 600), and
`migrate` and `cli` get `control-center.env`. The gate checks that the
three URLs differ pairwise, that their users are `ai_smm_cc`,
`ai_smm_auth` and the owner, and that host, port and database are equal.

### 9.5 Limitation — verified in a real Docker network rehearsal, fail-closed

Static-address binding, the interface the kernel uses, Docker's embedded
DNS answer for `smm-auth` from `smm-web`, the behaviour of `internal:
true` with a fixed subnet, and the healthcheck through the static
address are **assumptions until shown in a rehearsal** with Docker
networks laid out like production and Compose 5.1.3. In particular:

- the address `smm-web` resolves for `smm-auth` must be the `cc-auth` one;
- a connection from a container on a stand-in `<shared-network>` to
  `smm-auth:8000` must be refused at TCP level;
- the bind must survive a container restart by Docker's restart policy,
  which does not pass through the wrapper.

**Fail-closed policy.** The network tests below are **release gates**,
not observations:

- If static-address binding, Docker's DNS answer or TCP-level isolation
  does not behave as required in the rehearsal (any of T-N1 to T-N5
  fails), the production rollout of this ADR is **BLOCKED**. P3 and every
  later phase are not started.
- If the production post-check T-N6 fails after P5, the scoped P5
  rollback (§14.4) runs at once, and the rollout is blocked again.
- The application allowlist is **never** adopted automatically as a
  substitute. It exists only as the second layer of §9.2.
- Running `smm-auth` with a listener on `<shared-network>` protected only
  by the allowlist is a **weaker design**. It may be used only after
  (1) a separate threat review of the S1 exposure it creates, recorded
  as an amendment to this ADR, and (2) **explicit owner approval** of that
  amendment. Until both exist, the rollout stays blocked.

**Network tests (all are gates):**

| ID | Where | Test | Failure means |
|---|---|---|---|
| T-N1 | rehearsal | from a container on the stand-in `<shared-network>`: TCP connect to `smm-auth:8000` refused (reset, no HTTP answer) | rollout BLOCKED |
| T-N2 | rehearsal | from `smm-web`: the name `smm-auth` resolves to its `cc-auth` address, and login and logout succeed through nginx | rollout BLOCKED |
| T-N3 | rehearsal | with the bind widened to `0.0.0.0` in a test build, the allowlist answers 403, and the packaging test that pins the bind fails | rollout BLOCKED |
| T-N4 | rehearsal | `smm-api` cannot open a TCP connection to `smm-auth` (on any network), and the reverse | rollout BLOCKED |
| T-N5 | P1 facts + rehearsal | the `cc-auth` subnet overlaps no network or address pool on the host (§16); the bind and the healthcheck survive a restart by Docker's restart policy | rollout BLOCKED |
| T-N6 | production, P5 post-check, existing containers only | from inside `smm-api` (already on `<shared-network>`): TCP connect to `smm-auth:8000` refused; from inside `smm-web`: `smm-auth` resolves to an address in the `cc-auth` subnet | immediate scoped P5 rollback; rollout BLOCKED |

## 10. SQL parameter redaction

Raw tokens reach PostgreSQL as function arguments, so every place a
statement or its parameters can be recorded is closed or verified.

| Channel | Finding | Measure |
|---|---|---|
| Application log of an unexpected error | the handler logs the traceback ([errors.py:205-227](../../src/ai_smm/api/errors.py#L205-L227)); a SQLAlchemy `DBAPIError` text contains `[parameters: …]`; the redaction rule `\btoken\b` ([logging_setup.py:45-53](../../src/ai_smm/logging_setup.py#L45-L53)) does **not** match `p_token` or `token_hash`, because `_` is a word character | **`hide_parameters=True`** on both engines ([session.py:43-55](../../src/ai_smm/db/session.py#L43-L55)); a test forces DB errors and greps the logs |
| PostgreSQL statement text | `psycopg[binary]` 3 binds parameters on the server by default, so the text shows `$1` | test T9 checks `pg_stat_activity.query`; client-side binding is forbidden |
| PostgreSQL log with `log_statement` or `log_min_duration_statement` | parameters are logged unless `log_parameter_max_length = 0` | set on `ai_smm_auth` (a superuser-only setting, so the role cannot change it); instance values are a fact to verify (§16) |
| PostgreSQL log on error | `log_parameter_max_length_on_error` defaults to 0, but any user may change it | role default 0; it protects against accident, not against a compromised client, which has the token anyway |
| `pg_stat_statements` | constants are normalised | none |
| `auto_explain` | can log parameters if loaded | fact to verify (§16) |
| nginx and uvicorn access logs | method, path, status and id only; no cookies or bodies ([request_id.py:71-85](../../src/ai_smm/api/request_id.py#L71-L85), [default.conf:27-29](../../web/nginx/default.conf#L27-L29)); uvicorn runs with `--no-access-log` | none |
| Langfuse | imported only by `src/ai_smm/agents/*`; the API modules do not import them; its keys are empty literals in the Compose file ([compose.control-center.yml:76-82](../../compose.control-center.yml#L76-L82)) | none; `smm-auth` gets the same block |
| `application_name` | `ai-smm/{worker_id}` overrides `PGAPPNAME` ([session.py:53](../../src/ai_smm/db/session.py#L53)), so `smm-auth` would also appear as `ai-smm/worker-1` | fix [FU-024C1-1](../follow-ups.md#fu-024c1-1--the-api-connects-to-postgresql-as-ai-smmworker-1) in release N; until then, tell services apart by `usename` |

## 11. Argon2 and password-reset policy

PostgreSQL has no Argon2. A database operation that writes a new hash
cannot prove that the password stayed the same, so whoever may call it can
set a password of their choice — persistence for an attacker.

| Option | Decision |
|---|---|
| **V1. No opportunistic rehash** in any network-facing service. `smm-auth` logs "outdated hash" with the user id and counts it, and does not try to write. An outdated hash keeps verifying ([passwords.py](../../src/ai_smm/security/passwords.py)) | **SMM-024C-3B default.** It also removes [FU-024C2-4](../follow-ups.md#fu-024c2-4--flush-before-the-savepoint-of-the-optional-hash-upgrade) from `smm-auth`: no SAVEPOINT, no 42501 to absorb |
| **V2. Operator reset**: `ai-smm user set-password` in the existing `cli` service, which runs as the owner and whose entrypoint is already pinned to the `user` command group ([compose.control-center.yml:204-217](../../compose.control-center.yml#L204-L217)). The password is typed interactively; the hash uses current parameters; all of the user's sessions are revoked; an audit row is written | **SMM-024C-3B.** The planned way to upgrade hashes when the policy changes |
| **V3. Bounded rehash function** (`password_rehash`, §8.2): inert while every stored hash meets the policy, and at most one upgrade per user after a deliberate policy change | defined, **granted to no one**. Enabling it is a separate owner decision with its own acceptance test |
| **V4. Self-service password change** (old + new password) | deferred. The database cannot check the old password, and binding the change to a session token does not help against a compromised `smm-auth`, which can create tokens. It needs a UI, a recent login, revocation of other sessions and an audit row |

## 12. Residual session risk while `smm-api` resolves sessions in Python (D-1)

Under G2, `ai_smm_cc` keeps `SELECT` on `user_sessions`,
`UPDATE (last_seen_at)` and the seven `users` columns.

| # | What a compromised `ai_smm_cc` can still do | Bound |
|---|---|---|
| RS-1 | read session metadata, including `token_hash` | the hash cannot be replayed over HTTP: `smm-api` hashes the cookie. It reveals when users are active |
| RS-2 | keep any session alive past its idle timeout | never past `expires_at` (7 days), which it cannot change |
| RS-3 | set `last_seen_at` into the future once, and so disable the idle timeout | closed in 3B: Python refuses a session whose `created_at` or `last_seen_at` lies more than 60 s in the future, whose lifetime exceeds 7 days + 60 s, or whose `last_seen_at` precedes `created_at` |
| RS-4 | code execution: act as any user whose cookie it sees | inherent; read-only today |
| RS-5 | code execution: read everything its `SELECT` allows, across projects | confidentiality only |
| RS-6 | rely on the two resolution implementations disagreeing | held to the same boundaries by test T3 |

| Must be closed in SMM-024C-3B | May remain until SMM-024C-3D | Not acceptable once business mutations are on |
|---|---|---|
| `INSERT` on `user_sessions` (minting) | RS-1: `SELECT user_sessions` | any business write by `ai_smm_cc` while Python decides identity |
| `UPDATE revoked_at` (revoke, un-revoke) | RS-2: `UPDATE (last_seen_at)` | a direct `UPDATE (last_seen_at)`; the touch must go through a token-checked function |
| `SELECT users.password_hash`, table-level `SELECT users` (§6.2) | RS-5: reads unscoped by project in the database | `SELECT` on `user_sessions` |
| `UPDATE users`; `INSERT audit_log` and its sequence | RS-6: two implementations, compared by tests | idle and expiry checked only in Python on a mutating path |
| RS-3 plausibility check in `resolve_session` | | functions taking a `user_id` or a token hash instead of the raw token |
| `session-anomalies.sql` gains `seen_in_future` and `revoked_then_seen` | | |

## 13. Compatibility matrix

Releases:

- **R0** — production today: images `0cbd641`, compose C0, wrapper W0
  pinning verify V0 (the `ai_smm_cc` verify file of `0cbd641`).
- **RN** — release N (P5): images N, compose CN with `smm-auth` and
  `AI_SMM_API_AUTH_ROUTES: "true"`, wrapper WN pinning **V0** for
  `ai_smm_cc` plus VA for `ai_smm_auth`.
- **RN′** — P7: the same images N, compose CN′ with
  `AI_SMM_API_AUTH_ROUTES: "false"`, wrapper WN′ pinning **V2** and VA.

WN pins V0 on purpose: release N runs on the old grants first.

| State | Grants | Functions + `ai_smm_auth` | Wrapper / compose | api | web | auth | Login | Existing sessions | Gate for `up`/`restart` | Allowed |
|---|---|---|---|---|---|---|---|---|---|---|
| S0 | G1 | — | W0/C0 | 0cbd641 | 0cbd641 | — | via api | ✅ | V0 PASS | ✅ baseline |
| S1 | G1 | ✅ | W0/C0 | 0cbd641 | 0cbd641 | — | via api | ✅ | V0 PASS (A1) | ✅ after P3/P4 |
| S2 | G1 | ✅ | WN/CN | N | N | N | via auth | ✅ including S0 sessions | V0 + VA PASS | ✅ after P5 |
| S3 | **G2** | ✅ | WN/CN | N | N | N | via auth; a direct login to `smm-api` over `<shared-network>` fails with 500 | ✅ | **V0 FAIL → refused** | ⚠️ minutes only, inside P7 |
| S4 | G2 | ✅ | WN′/CN′ | N | N | N | via auth | ✅ | V2 + VA PASS | ✅ after P7 |
| X1 | G2 | ✅ | any | **0cbd641** | any | any | ❌ | ❌ total outage: every request reads `users.*` → 42501 | W0: V0 FAIL → refused | ⛔ |
| X2 | G2 | ✅ | W0/C0 | 0cbd641 | **0cbd641** | — | ❌ login reaches `smm-api` → 42501 | ❌ | refused | ⛔ |
| X3 | any | **—** | WN or WN′ | N | N | N | ❌ no functions | ✅ | VA FAIL → refused | ⛔ |
| X4 | any | ✅ | WN with CN′, or WN′ with CN | — | — | — | — | — | pin mismatch → refused | ⛔, caught |
| X5 | G1 | ✅ | WN′/CN′ | N | N | N | ✅ | ✅ | V2 FAIL → refused | ⛔, caught |
| X6 | G1 | ✅ | W0/C0 after a rollback that left `smm-auth` behind | 0cbd641 | 0cbd641 | **orphan** | via api | ✅ | V0 PASS | ⛔ an orphan holding a credential |
| X7 | — | — | WN installed, `control-center.auth.env` deleted | — | — | — | — | — | the wrapper refuses **every** verb, `down` included ([cc-compose.sh:359-362](../../deploy/control-center/cc-compose.sh#L359-L362)) | ⛔ |

## 14. Deployment and rollback order

Every step after P2 needs its own owner approval, inside an approved
window, with the existing identity and queue gates. Preflight checks that
the images of **both** the target and the rollback release are loaded.

### 14.1 P3–P6, forward

| Phase | Action | Runtime effect |
|---|---|---|
| P1 | read-only facts on the host (§16) | none |
| P2 | stage the bundle for release N; verify the manifest | none |
| P3 | superuser: the functions file (schema `smm_auth`, functions, `PUBLIC` revoked, bodies pinned), then the `ai_smm_auth` grants file, with no password yet. `ai_smm_cc` untouched (A1) | none; R0 verify still 0 FAIL |
| P4 | owner: `\password ai_smm_auth`; create `control-center.auth.env` (mode 600) **before** any release-N script is installed (the wrapper needs every env file for every verb) | none |
| P5 | **only if T-N1 to T-N5 passed in the rehearsal** (§9.5): install the WN scripts and verify files, then CN **last**; load images N; set `SMM_CC_VERSION=N`; WN `up`; post-check T-N6, whose failure triggers the scoped P5 rollback at once | `smm-api` and `smm-web` recreated, `smm-auth` created; about 7 s, the measured P1 figure ([production-acceptance-smm-024c2.md](../production-acceptance-smm-024c2.md#3-timeline-utc)) |
| P6 | observation: smoke test, `session-anomalies.sql` = 0, logout through `smm-auth`, rollback rehearsed | none |

### 14.2 P7, forward (S2 → S3 → S4)

1. WN `preflight`: V0 and VA PASS. Snapshot the worker (container id,
   `StartedAt`, heartbeat), the queue fingerprint, the number of live
   sessions, `acl-snapshot.sql` and the **raw-ACL baseline of
   `ai_smm_cc`** (§6.3, step 1). Without that baseline P7 does not start.
2. Stage WN′, V2 and CN′; verify the manifest. Live files untouched.
3. Copy the live RN set (scripts, verify files, compose, interpolation
   file) to `evidence/` as `.before-P7`.
4. Superuser: **G2** in one transaction. It takes catalog locks only, as
   measured for the role's grants
   ([least-privilege-role.md §6.5](../least-privilege-role.md#65-lock-impact-measured)).
   This is **S3**. API N runs under G2 because it no longer references
   `password_hash` and resolves sessions with `SELECT` and
   `UPDATE (last_seen_at)` only. A restart by Docker's own restart policy
   works, since it bypasses the wrapper; a restart through the wrapper is
   refused.
5. Install the WN′ scripts and V2, then CN′ **last**.
6. WN′ `up`: V2 and VA PASS. Only `smm-api` is recreated (one literal
   changes); `smm-web` and `smm-auth` keep their configuration. About 4 s,
   measured for A3 in production (3.6 s).
7. Post-checks: smoke test, extended `session-anomalies.sql` = 0, V2,
   `acl-snapshot.sql` unchanged, worker snapshots identical.

Steps 4 to 6 are one uninterrupted step. Logins and logouts keep working
throughout, because `smm-auth` is not recreated.

### 14.3 P7 rollback (S4 → S2)

1. Superuser: the **rollback prelude**, then **G1**, the byte-identical
   `0cbd641` file (§6.3, steps 2–3). API N runs under G1 and G2 alike.
2. Explicit verification (§6.3, step 4): V0 `0 FAIL`, the raw-ACL snapshot
   of `ai_smm_cc` identical to the pre-G2 baseline, and `acl-snapshot.sql`
   of the other roles unchanged. If the snapshot differs, repeat step 1;
   do not go on.
3. Restore the `.before-P7` set: scripts first, compose **last**.
4. WN `up`: V0 and VA PASS. Only `smm-api` is recreated, about 4 s.
5. Post-checks as in §14.2.

Steps 1–2 and step 3 may be swapped, because the gate runs only at `up`.
Grants first is the rule, so that nobody continues to R0 from X1.

### 14.4 P5 rollback (S2 → S1) — scoped; only after any P7 rollback

**Primary: scoped removal.** `smm-api` and `smm-web` keep serving until
the final `up`.

1. Confirm **G1**: WN `preflight` with V0 PASS, **and** — if P7 was ever
   applied — the raw-ACL snapshot of `ai_smm_cc` equal to the pre-G2
   baseline (§6.3). Otherwise the R0 images would hit X1.
2. WN `auth-remove`: a new, scoped verb in the release-N wrapper. It runs
   `stop` and then `rm` for the service `smm-auth` only, and refuses any
   other service name. It never takes `--remove-orphans`, `-v` or a
   network argument. Logins answer the JSON 502 from `@auth_unavailable`
   until step 4; existing sessions keep working.
3. Restore the R0 set: W0, its verify file, then C0 **last**, plus the
   interpolation file with `SMM_CC_VERSION=0cbd641`.
4. W0 `up`: V0 PASS. `smm-api` and `smm-web` are recreated on the R0
   images; `smm-web` leaves `cc-auth`. About 7 s.
5. Remove the then-unused `ai-smm-cc-auth` network with one guarded
   operator command, run against the wrapper's pinned Docker daemon. It
   runs only if all of these hold: the exact name `ai-smm-cc-auth`; the
   Compose project label `ai-smm-control-center`; `Internal = true`; **no
   attached endpoints**; not marked external. Never forced. If a check
   fails, the network stays: with no endpoints it is inert, and it is
   reported for the next maintenance.
6. Only then delete `control-center.auth.env` (X7). Rolling back P4 and
   P3 (password, role, functions) is optional and independent: with no
   connections they do not affect R0.

**Fallback: complete project `down`**, used only if a rehearsal shows that
the scoped steps cannot remove the container or the network safely. WN
`down` stops `smm-api`, `smm-web` and `smm-auth` and removes the
project's own networks (`cc-edge`, `cc-internal`, `cc-auth`). It must be
WN, because only CN defines `cc-auth`. Then restore the R0 set and run W0
`up`, which recreates the project networks. Downtime: estimated 10–20 s,
to be measured.

**Both paths must show, in the rehearsal and in production:**

- `<shared-network>` still exists, with the same id and its other
  endpoints (it is `external: true`; Compose must never remove it — shown,
  not assumed);
- the worker container's id and `StartedAt` are unchanged, and its
  heartbeat keeps advancing;
- `<postgres-container>` is untouched;
- no container or network labelled `ai-smm.component=control-center`
  remains for `smm-auth`;
- existing sessions still resolve.

Compose behaviour to verify in the Compose 5.1.3 rehearsal rather than
assume: scoped `stop` and `rm` of one service; that `up` of C0 does not
recreate or remove anything for `smm-auth`; that an orphan network is
neither removed nor reused by `up`; that `down` never touches an external
network.

### 14.5 Downtime

| Transition | Recreated | Figure | Basis |
|---|---|---|---|
| P5 forward | api and web; auth created | ≈ 7 s | measured, SMM-024C-2B P1 |
| P7 forward / P7 rollback | api only | ≈ 4 s | measured, A3 (3.6 s) and the local RB-1 rehearsal (3.5 s) |
| P5 rollback, scoped | login unavailable from step 2 to step 4; api and web ≈ 7 s | to measure | rehearsal |
| P5 rollback, `down` fallback | everything, networks included | 10–20 s, estimate | rehearsal |
| P3, P4, G1 ↔ G2 | nothing | 0 | catalog locks only |

### 14.6 Worker isolation

- No step names the worker. The Control Center is its own Compose project
  ([compose.control-center.yml:9-12](../../compose.control-center.yml#L9-L12)).
- G1, G2 and GA never change `ai_smm` or `PUBLIC`; `acl-snapshot.sql` is
  identical before and after.
- The functions file creates a schema and functions only: no table lock
  and no change to `alembic_version`. FU-024B4A-1 (the queue-gate to DDL
  gap) is therefore not triggered, since there is no DDL on a queue
  table.
- Pass criterion for every phase: the worker container's id and
  `StartedAt` are unchanged, its heartbeat keeps advancing, and the queue
  fingerprint is identical.

## 15. Security acceptance criteria

### 15.1 Tests T1–T16

| ID | Test | Pass condition |
|---|---|---|
| T1 | token digest in SQL vs Python | identical for 10 000 random tokens |
| T2 | `session_create` contract | server timestamps; 7-day lifetime; audit shape and time; inactive user refused; 22023 on a malformed token; `ai_smm_cc` and `PUBLIC` get 42501; `session-anomalies.sql` = 0 |
| T3 | cross-version sessions: code `0cbd641` vs release N, every created × resolved × revoked combination | all pass; expiry and idle boundaries (−1 s / 0 / +1 s) agree between Python and SQL |
| T4 | cookie and CSRF | `Set-Cookie` from `smm-auth` equals that of `0cbd641`; the login CSRF value equals `GET /auth/csrf`; logout without CSRF → 403 |
| T5 | concurrency | 20 parallel requests of one session are not serialised; logout during requests; a double logout writes one audit row |
| T6 | gates | V0 (pinned sha in R0's `cc-gate.sh`) gives 0 FAIL in S1, S2 and after G2 → prelude → G1; the raw-ACL snapshot of `ai_smm_cc` after that sequence equals the pre-G2 baseline; V2 and VA give 0 FAIL in S4; `acl-snapshot.sql` of the other roles unchanged |
| T7 | full suite | `AI_SMM_TEST_API_ROLE=restricted` under G1 and G2, plus a new `AI_SMM_TEST_AUTH_ROLE`; only the 122 known business refusals ([production-acceptance-smm-024c2.md](../production-acceptance-smm-024c2.md#4-tests-before-production)) |
| T8 | negatives for `ai_smm_cc` under G2 | 42501 for: `INSERT user_sessions`; `UPDATE user_sessions SET revoked_at = NULL`; `UPDATE … SET expires_at`; every statement of §6.2; `INSERT audit_log`; `UPDATE users`; `EXECUTE smm_auth.*`. `…can_mint…` becomes `…cannot_mint…`; new `cannot_unrevoke` |
| T9 | logs and statement text | forced DB errors leave no token in the application log (`hide_parameters`); no token in a PostgreSQL log with `log_statement=all` plus the role settings; `pg_stat_activity.query` shows `$1` |
| T10 | routing | login and logout reach `smm-auth`; everything else reaches `smm-api`; with `smm-auth` stopped → 502 `dependency: auth`, and browsing continues |
| T11 | rate limit | progressive delay with the generic 401; known and unknown addresses indistinguishable; any lockout bounded in time |
| T12 | Argon2 memory | 20 parallel logins cause no OOM (semaphore of 2; `mem_limit` sized for 64 MiB per hash) |
| T13 | rollback rehearsal | S0 → S4 → S0 with production-like ACLs, Compose 5.1.3 and the real wrapper; scoped P5 rollback **and** the `down` fallback; downtime measured |
| T14 | worker | container id and `StartedAt` unchanged; the worker test suite passes with the functions installed |
| T15 | shared-network bypass | T-N1 to T-N5 in the rehearsal and T-N6 in production all pass (§9.5). **Any failure blocks the rollout**; no automatic fallback to the application allowlist |
| T16 | packaging | pins in sync; the gate refuses if the auth URL equals the API or owner URL, if its user is not `ai_smm_auth`, or if `control-center.auth.env` is not mode 600 |

### 15.2 Compatibility checks C-1–C-9

| ID | Check | Prevents |
|---|---|---|
| C-1 | W0 `preflight` against a G2 database refuses; images `0cbd641` under G2 answer 500 on every authenticated request (documented) | X1, X2 |
| C-2 | WN and WN′ refuse each other's compose and verify files (cross-pin test) | X4, X5 |
| C-3 | WN or WN′ refuse with functions missing, a changed function body, a missing password or wrong `EXECUTE` grants | X3 |
| C-4 | after the scoped P5 rollback (and after the fallback): no `smm-auth` container or `ai-smm-cc-auth` network; `<shared-network>` present with the same id; worker untouched | X6 |
| C-5 | WN refuses without `control-center.auth.env`; the runbook deletes it last | X7 |
| C-6 | API N full suite under G1 and G2; G1 → G2 → prelude → G1 on PostgreSQL 16 with tests G-P1 to G-P5 and G-N1 to G-N5 passing (§6.3); V0 = 0 FAIL **and** the raw-ACL snapshot identical to the baseline; the G1 file sha equals that of `0cbd641` | broken rollback grants; a column grant hidden under a table grant |
| C-7 | T3 matrix | sessions lost in a rollback |
| C-8 | the full cycle S0 → S1 → S2 → S3 → S4 → S2 → S1 → S0 on the rehearsal stack; downtime measured; worker snapshots unchanged | the whole sequence |
| C-9 | every preflight checks that the images of the current and of the rollback release are loaded | a rollback blocked by pruned images |

## 16. Facts to verify read-only on the host (P1)

Not derivable from the repository; each is an implementation
precondition, not an assumption of this ADR:

- PostgreSQL logging: `log_statement`, `log_min_duration_statement`,
  `log_parameter_max_length`, `log_parameter_max_length_on_error`,
  `log_min_error_statement`, whether `auto_explain` is loaded,
  `log_line_prefix`, and where and how long logs are kept;
- whether the application ↔ PostgreSQL connection uses TLS;
- the `pg_hba.conf` rules a new role connecting from `<shared-network>`
  falls under;
- that the role name `ai_smm_auth` is free;
- a subnet for `cc-auth` that overlaps no existing network or daemon
  address pool;
- that the `0cbd641` images are still loaded;
- the server encoding (expected UTF8; the token is ASCII either way).

## 17. Dependencies and residual risks before business mutations

This ADR does **not** make business mutations safe. Before
`AI_SMM_API_MUTATIONS_ENABLED` may change, SMM-024C-3C and 3D must close:

- session resolution in the database: `ai_smm_cc` loses `SELECT` and
  `UPDATE` on `user_sessions` and gets `EXECUTE smm_auth.session_resolve`
  (with the non-blocking touch of §7.4);
- every business command as a token-bound `SECURITY DEFINER` function;
  `ai_smm_cc` keeps no direct business write
  ([FU-024C2-3](../follow-ups.md#fu-024c2-3--a-privilege-model-for-business-mutations));
- structural audit provenance
  ([FU-024C2-2](../follow-ups.md#fu-024c2-2--authenticity-of-audit-actor-and-action));
- worker-side verification of the approval lineage before any external
  call;
- the FU-024C1-5 checklist, extended with the "not acceptable" column of
  §12.

Residual risks accepted by this ADR:

| Risk | Why accepted | Closed by |
|---|---|---|
| a compromised `smm-auth` can create a session for any user | inherent: it verifies passwords | — (kept minimal and isolated) |
| a compromised `smm-api` can read session metadata and keep sessions alive up to 7 days (RS-1, RS-2) | read-only mode | 3D |
| a compromised `smm-api` acts as users whose cookies it sees (RS-4) | inherent | an optional step-up confirmation through `smm-auth` (owner decision) |
| `smm-api` remains reachable from `<shared-network>` | another project's stack | FU-024B3-2 |
| outdated Argon2 hashes are not upgraded automatically | V1 by design | V2 operator reset, or V3 by owner decision |
| none accepted for network isolation: if static binding, Docker DNS or TCP isolation fails, the rollout is **blocked** (§9.5) | an allowlist-only design would need a separate threat review and explicit owner approval | T-N1–T-N6 |

## 18. Alternatives considered

| Alternative | Why not |
|---|---|
| A second connection pool with an auth role inside `smm-api` | protects against injected SQL and a leaked credential, not against code execution in `smm-api` |
| `SECURITY DEFINER` session creation callable by `ai_smm_cc` | a function with an arbitrary `user_id` in the API's hands is the forgery it is meant to prevent |
| Row-level security keyed on a GUC holding a `user_id` | the GUC can be forged by the same role |
| An Alembic revision for the functions | ties the migration to the single image version and moves `alembic_version` (A6) |
| `deferred()` alone for `password_hash` | not a database boundary (correction 1) |
| Opportunistic rehash through a granted function | lets a compromised `smm-auth` set passwords (§11) |
| A complete project `down` as the primary P5 rollback | stops `smm-api` and `smm-web` without need (correction 2) |
| Redis or a second host for sessions or rate limits | not needed: sessions are in PostgreSQL; the rate limit uses memory plus the audit table |

## 19. References

- [least-privilege-role.md](../least-privilege-role.md) — the `ai_smm_cc` design, §4.1–4.3 residual risks, §8 shared instance
- [production-acceptance-smm-024c2.md](../production-acceptance-smm-024c2.md) — the role in production, measured downtime
- [follow-ups.md](../follow-ups.md) — FU-024C2-1 to 4, FU-024C1-5, FU-024B3-2, FU-024C1-1
- [automation-and-publication-safety.md](automation-and-publication-safety.md) — the human-approval invariant
- [control-center-rollback.md](../control-center-rollback.md) — the existing rollback procedures this ADR extends
