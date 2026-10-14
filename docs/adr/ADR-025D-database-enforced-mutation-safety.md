# ADR-025D — Database-enforced safety of business mutations

| | |
|---|---|
| Status | **Proposed — for the owner's acceptance.** Implemented and verified locally (SMM-025D, 2026-10-14); not committed, not deployed; production unchanged |
| Project | PRJ-013 AI SMM Department 2.0 |
| Baseline | `main` at `5f32b504444a52c8d569003d351427a64546aee6` (production release, read-only) |
| Closes | FU-025C-1 (insert invariants), FU-024C2-1 (session forgery, with smm-auth), FU-024C2-2 (audit provenance), FU-025C-2 (keys / allow-list packaging — see §9) |
| Builds on | [ADR-024C-3A](ADR-024C-3A-auth-service-isolation.md) (smm-auth), [mutation-role.md](../mutation-role.md) (`ai_smm_cc_rw`), [automation-and-publication-safety.md](automation-and-publication-safety.md) (human approval) |
| Amends | ADR-024C-3A §12 and §17 (how "session resolution in the database" and "token-bound business commands" are realised) |

## 1. Problem

With mutations on, the Control Center API writes business rows as
`ai_smm_cc_rw`. Before this ADR every rule that protects publishing —
an approval is a human's, a publication is exactly what was approved,
only the owner acts — lived in the application. Whoever controlled the
API's database connection could, in SQL:

- insert a publication already `scheduled` and `human_reviewed`, with no
  approval, which the LIVE worker would publish (FU-025C-1);
- write approvals, audit entries and memberships in anyone's name;
- create a session for any user (FU-024C2-1), so that any of the above
  would also pass the application's own checks.

## 2. Threat model

| ID | Attacker | Before | After this ADR |
|---|---|---|---|
| S3/S4 | the API's DB credential or arbitrary SQL as `ai_smm_cc_rw`, no session token | everything above | **no business write at all**: every guarded write needs a transaction bound to a live session (§4.1), and no API role can create one (§6) |
| S5 | code execution in smm-api, sees the owner's cookie | everything above | acts as the owner for edits, schedules, cancels, accounts and credentials — **but cannot approve** (§4.3) and cannot publish anything that is not an approved revision for its approved destination (§4.4) |
| S5a | code execution in smm-auth | — | can open sessions and issue approval grants for users it verifies (inherent: whoever checks the password decides the identity); kept minimal and isolated |
| S6 | the owner role (worker, CLI, migrate) | trusted | trusted, unchanged; the worker still re-checks lineage before any external call (§4.5) |

## 3. Decision

Enforce the rules in PostgreSQL, for every role that is not the table
owner, with the smallest set of objects that makes them checkable:

1. a **session binding** per transaction (`guard_bind_session`);
2. **guard triggers** on every business table;
3. **step-up approval grants**, issued only by smm-auth;
4. a **lineage function** used both by the guards and by the worker;
5. **audit provenance** (`written_by`);
6. **smm-auth** for every session-creating operation; the API roles
   lose session-creating rights (grants G2).

Rejected alternative (ADR-024C-3A §17 as written): one token-bound
SECURITY DEFINER function per business command, the API role without
direct writes. It would re-implement in PL/pgSQL about eighteen commands
already implemented, tested and reviewed in Python — two implementations
of every rule, of which only the SQL one would be enforced. The triggers
below enforce the invariants that matter for safety on the rows
themselves, whatever SQL writes them, and leave the application logic
where it is.

## 4. Mechanisms

Source: `src/ai_smm/db/guards.py` (`FUNCTIONS_V1`, `TRIGGERS_V1`, versioned),
migration `c4d81e2a9f63`; `db/models.py` creates the same objects for
`create_all`; `tests/test_migration_guards.py` proves both identical.

### 4.1 Who is guarded, and for whom a transaction acts

- A trigger is **guarded** when the statement's `current_user` is not a
  member of the table's owner. Owner, superusers and the bodies of the
  owner's SECURITY DEFINER functions pass untouched; every other role —
  `ai_smm_cc`, `ai_smm_cc_rw`, any future role — is guarded without being
  listed.
- `guard_bind_session(raw token)` (SECURITY DEFINER): hashes the token
  exactly as the API does, applies `smm_auth.session_resolve`'s predicate
  (not revoked, not expired, not idle 24 h, active user, RS-3 plausibility
  bounds) and writes `(txid → user, session)` to `mutation_contexts`. A
  second binding of the same transaction to another user is refused.
  Contexts are per transaction; nothing binds the next one.
- The API calls it on every unsafe request with mutations on
  (`api/session_binding.py`), in the request's own transaction. A code path
  that forgot would fail closed in the database.
- No guarded role may read or write `mutation_contexts` or
  `approval_grants`, nor execute `guard_token_digest`.

### 4.2 Per-table rules (guarded roles)

| Table | Rule |
|---|---|
| every business table | a bound transaction; the bound user is an **owner** of the row's project |
| `projects` | INSERT only by a user who already owns a project; `created_at`/`updated_at` := the transaction's `now()` |
| `project_memberships` | only the bound user, as `owner`, of a project created in this transaction and without members; never a change to an existing project's members |
| `project_settings` | the owner, or the settings row of a project created in this transaction |
| `social_accounts` | creator = bound user; never created with an identity; a bound `external_account_id` never changes or disappears |
| `social_account_credentials` | creator = bound user; created `active`; only `active → superseded / revoked` |
| `content_items` | creator = bound user; at COMMIT an `approved` item has an approval of its approved revision |
| `content_revisions` | author = bound user; the destination is an account of the same project |
| `content_approvals` | approver = bound user; only the current revision; `created_at` := `now()`; **an approval consumes a step-up grant** (§4.3) |
| `content_publication_links` | only for a publication created in this transaction, from the item's approved revision |
| `publications` | INSERT: `approved`, reviewed, unscheduled, unclaimed, no outcome, no series links; at COMMIT a link and valid lineage (§4.4). UPDATE: only `approved/failed → scheduled`, `scheduled/failed → scheduled`, `draft/approved/scheduled/failed → cancelled`; claim, lease and outcome columns never; scheduling re-checks lineage, publish metadata and a ready destination |
| `audit_log` | `written_by := session_user` for every row; a business role writes business actions only, as the bound user; a role without business rights writes `auth.*` only |

All refusals use SQLSTATE 42501 and a message starting `guard: `; the API
maps them to 403 `APPROVAL_CONFIRMATION_REQUIRED` or `GUARD_REFUSED`.

### 4.3 Step-up approval

`approval_grants` holds a grant per (user, session, item, revision,
destination, pinned destination identity, content hash), valid five
minutes, consumed once. It is written only by
`smm_auth.approval_grant_issue`, which only `ai_smm_auth` may execute, and
which smm-auth calls only after re-verifying the owner's password
(`POST /api/v1/auth/step-up/approvals`). The guard on `content_approvals`
consumes it under a row lock (two approvals cannot share one) and refuses
a grant of another session, an expired or used one, or one whose pinned
identity no longer matches the destination. `smm_auth.approval_preview`
gives the owner the text and destination from the database, served by
smm-auth: a compromised smm-api cannot show another text at that moment.
Rejections need no grant.

### 4.4 Lineage

`guard_publication_problem(id)` returns NULL or the reason a row may not
be delivered: a content snapshot must link to a revision of the linked
item, in the same project and platform, whose latest decision is a human
approval, with identical title, body, format, images, items and
destination; a row without a link (importer, operator CLI) must be
reviewed and account-less. A new row from a guarded role must have a link
— it can never pass as a legacy row.

### 4.5 Worker

Before routing, decrypting or any external call, the worker asks
`guard_publication_problem`; a non-NULL answer fails the row permanently
(`ApprovalLineage`), sending nothing and calling not even GET /me. Account
routes still prove the token's identity against the bound
`external_account_id` before publishing, and never fall back to the
deployment token.

### 4.6 Audit provenance

`audit_log.written_by` (nullable; historical rows NULL = "before
provenance") is set by trigger from `session_user`, whatever the writer
sends; the API roles cannot even name the column. Rows written inside
smm_auth functions carry `ai_smm_auth`.

## 5. What the database still cannot know

| Residual | Bound |
|---|---|
| S5 acts as the owner for every non-approval command | inherent while the API serves the owner's cookie; nothing it does publishes unapproved content |
| `verification_status = verified` and the first identity binding are the API's conclusion from GET /me | the worker re-checks identity before every publish; a bound identity never changes; approvals pin it |
| S5a (smm-auth compromise) can issue sessions and grants | inherent; smm-auth is small, has no business rights, and no listener on the shared network (ADR-024C-3A §9) |
| reads by the API roles are not project-scoped in SQL | confidentiality only (RS-5) |
| session resolution for reads remains in Python; `ai_smm_cc` keeps `SELECT user_sessions` and `UPDATE (last_seen_at)` (RS-1, RS-2) | the raw token is never stored; every write's identity is decided in SQL by the binding — the reason ADR-024C-3A §12 required DB resolution |

## 6. Sessions (FU-024C2-1)

smm-auth (`api/auth_service.py`) serves `login`, `logout` and the step-up
routes, as `ai_smm_auth`, through the smm_auth functions. Grants **G2** take
from both API roles every session-creating right: `INSERT` on
`user_sessions`, `UPDATE (revoked_at)`, `UPDATE` on `users`, table-level
`SELECT` on `users` (columns without `password_hash` instead), and — for
`ai_smm_cc` — `INSERT` on `audit_log`. smm-api stops mounting login and
logout (`AI_SMM_API_AUTH_ROUTES: "false"`).

## 7. Compatibility

- **Old API `5f32b50` on the new schema**: works (its sign-in audit is
  `auth.*`, allowed for a role without business rights) — the application
  rollback keeps the schema.
- **Old worker** on the new schema: **not allowed.** `5f32b50` refuses to
  start (preflight expects `a7c2e5f19b40`); older images have no such check
  and are not a supported configuration on this schema, although their
  suites pass on it. The rollout therefore migrates and switches the
  worker to the release of the migration as one step, and a worker
  rollback below this release requires the schema downgrade first.
- **Downgrade** refuses without `AI_SMM_DOWNGRADE_REMOVES_MUTATION_GUARDS=yes`:
  it removes protections; with mutations on, switch them off first.

## 8. Verification

`tests/test_db_guards.py` (direct SQL as `ai_smm_cc_rw`, bound and
unbound), `tests/test_migration_guards.py`, `tests/test_auth_service.py`,
`tests/mutation_role/` (every business suite as the rw role, with the
step-up played by the owner; `test_mr_guard_refusals.py` without it),
`tests/test_publish_routing.py` (worker lineage),
`tests/test_e2e_owner_workflow.py` (smm-auth + smm-api as `ai_smm_cc_rw` +
worker, end to end), the role matrices and the wrapper gates. Results:
[tests.md](../reports/smm-025d/tests.md).

## 9. Configuration (FU-025C-2)

- `control-center.compose.env` carries `SMM_CC_CREDENTIALS_PUBLIC_KEY` and
  `SMM_CC_LEGACY_THREADS_PROJECTS`; the wrapper refuses a malformed key, the
  example's placeholder and a malformed list, prints the key id, and the
  identity gate requires the list to equal the running worker's
  `AI_SMM_LEGACY_THREADS_PROJECTS`. Compose interpolates both into
  **smm-api only**.
- The worker's `compose.yaml` mounts the key directory read-only into
  `worker` and `cli` (never `migrate`); `ai-smm.env` lists the key files.
- Backup, restore, rotation, revocation: [credential-keys.md](../credential-keys.md).

## 10. smm-auth packaging and network isolation

ADR-024C-3A §9 as designed: `cc-auth` (internal, `10.231.18.0/29`),
smm-auth bound only to `10.231.18.2`, healthcheck on that address,
`AI_SMM_AUTH_ALLOWED_PEERS=10.231.18.0/29` as the second layer, nginx
`= /api/v1/auth/login`, `= /api/v1/auth/logout`, `^~ /api/v1/auth/step-up/`
→ smm-auth (JSON 502 `dependency: auth` when it is down), smm-api with
`AI_SMM_API_AUTH_ROUTES=false`. The wrapper runs an **auth role gate**
(`ai_smm_auth.verify.sql`, pinned; URLs pairwise distinct) before `up`,
`restart` and `preflight`.

The release gate of ADR-024C-3A §9.5 — rehearsal T-N1 to T-N5 — **passed**
locally on 2026-10-14 with images built from this tree
(`scripts/cc_auth_network_rehearsal.sh`,
`docs/reports/smm-025d/evidence/cc-auth-network-rehearsal.log`). T-N4 in
the reverse direction (smm-auth → smm-api) is open over the shared network
as for every container there (FU-024B3-2, FU-025D-2); T-N6 is a production
post-check.

## 11. Rollout, rollback, residual risks

[docs/reports/smm-025d/rollout-rollback.md](../reports/smm-025d/rollout-rollback.md),
[security-review.md](../reports/smm-025d/security-review.md),
[residual-risks.md](../reports/smm-025d/residual-risks.md). The release
keeps `CC_RELEASE_PERMITS_MUTATIONS=no`; lifting it is a separate,
reviewed change after acceptance (rollout §4).
