# SMM-025D — Security review

Scope: everything that lets the Control Center change business state with
mutations on, against a LIVE worker. Design:
[ADR-025D](../../adr/ADR-025D-database-enforced-mutation-safety.md) (database
guards, step-up approval, lineage, provenance) and
[ADR-024C-3A](../../adr/ADR-024C-3A-auth-service-isolation.md) (smm-auth).
Reviewer's position: the API process and its database credential are
**not** trusted; PostgreSQL, smm-auth and the worker are.

## 1. Attackers and what they get now

| ID | Attacker | Before SMM-025D | Now |
|---|---|---|---|
| S3/S4 | the API's DB credential, arbitrary SQL as `ai_smm_cc_rw` / `ai_smm_cc`, no session token | publish anything (insert a `scheduled`, reviewed row), forge sessions, approvals, audit | **no business write at all**: every guarded write needs a transaction bound to a live session, and no API role can create, revive or extend one (G2) |
| S5 | code execution in smm-api, sees the owner's cookie | everything the owner can, plus forged approvals | acts as the owner for edits, schedules, cancels, accounts, credentials — **cannot approve** (needs a grant only smm-auth issues after the password) and **cannot make the worker send anything that is not an approved revision for its approved destination** |
| S5a | code execution in smm-auth | — | open sessions, issue grants for users it verifies; no business table right; no listener on the shared network |
| S6 | owner role (worker, migrate, CLI) | trusted | trusted; the worker still re-checks lineage before any external call |
| S7 | a container on `<shared-network>` | reach smm-api:8000 (session + CSRF still required) | same for smm-api (FU-024B3-2); **cannot connect to smm-auth** (T-N1, rehearsed) |

## 2. The bypasses asked about, and the proof

Every row is a direct-SQL test as `ai_smm_cc_rw` unless noted
(`tests/test_db_guards.py`, real PostgreSQL 16, production-like roles).

| Bypass | Control | Test |
|---|---|---|
| INSERT a publication already sendable | guarded INSERT must be `approved`, reviewed, unscheduled, unclaimed, no outcome; at COMMIT a link and valid lineage | `test_a_new_publication_is_never_queued_or_delivered`, `test_a_snapshot_without_lineage_is_refused_at_commit` |
| UPDATE a row into a sendable state / claim or outcome columns | UPDATE whitelist: only the three queue commands; claim, lease and outcome columns never; scheduling re-checks lineage, publish metadata, ready destination | `test_the_worker_columns_and_states_are_the_workers`, `test_an_unreviewed_legacy_row_cannot_be_scheduled`, `test_an_account_row_is_not_scheduled_to_a_disabled_account` |
| Fake a revision | author forced to the bound user; destination must be an account of the same project | `test_creator_actor_and_approver_are_always_the_bound_user` |
| Fake an approval | approver = bound user; only the current revision; consumes a step-up grant (row-locked, once, this session, expiry 5 min, pinned identity) | `test_approval_without_a_step_up_grant_is_refused`, `test_a_grant_is_good_once_for_its_session_only`, `test_only_the_current_revision_can_be_decided`, `test_approval_rows_reference_their_grant` |
| Change `social_account_id` after approval | revision destination is in the content hash (new revision ends the approval); a snapshot's destination is immutable; the grant pins the account's external identity | `test_a_snapshot_cannot_change_its_destination`, `test_an_approval_pins_the_destinations_identity`, `tests/test_e2e_owner_workflow.py::test_changing_the_destination_ends_the_approval_and_the_grant_does_not_carry_over` |
| Re-bind an account to another identity | a bound `external_account_id` never changes or disappears; never created with one | `test_a_bound_identity_never_changes`, `test_an_account_is_never_created_with_an_identity` |
| Substitute project / publication / revision / author | owner of the **row's** project; lineage compares item, project, platform, text, format, images, items, destination column by column; a link only for a publication created in the same transaction | `test_the_owner_of_another_project_cannot_write_here`, `test_a_snapshot_must_be_exactly_what_was_approved`, `test_lineage_cannot_be_attached_to_an_existing_publication` |
| Make oneself a member | memberships only for a project created in this transaction, by its creator, as owner | `test_nobody_is_made_a_member_of_an_existing_project`, `test_an_owner_creates_a_project_with_themselves_as_its_owner` |
| Revive a revoked credential | `active → superseded / revoked` only | `test_a_retired_credential_never_comes_back` |
| Forge audit | `written_by := session_user` by trigger; business role writes business actions only, actor = bound user | `test_audit_rows_say_which_role_wrote_them`, `test_migration_guards.py::test_existing_audit_rows_get_no_invented_provenance` |
| Act without a session / for two users | binding per transaction; a second user refused; contexts invisible to guarded roles | `test_without_a_bound_session_every_business_write_is_refused`, `test_a_binding_lasts_exactly_one_transaction`, `test_one_transaction_cannot_act_for_two_users`, `test_the_role_cannot_touch_the_guards_own_state` |
| Bind a dead session | same predicate as `smm_auth.session_resolve` (revoked, expired, idle, inactive user, RS-3 bounds) | `test_postgresql_binds_only_a_session_it_considers_live`, `test_a_malformed_or_missing_token_binds_nothing` |
| Concurrency / TOCTOU | grant consumed under `FOR UPDATE`; approved-item and lineage checks are `DEFERRABLE INITIALLY DEFERRED` (checked at COMMIT, after every statement of the transaction) | `test_two_approvals_cannot_share_one_grant`, `tests/test_api_command_concurrency.py` (as both roles) |
| Worker publishes a broken row anyway | worker asks `guard_publication_problem` before routing, decryption or GET /me; fails the row `ApprovalLineage` | `tests/test_publish_routing.py::test_the_worker_refuses_a_row_whose_approval_lineage_does_not_hold` |
| Guards silently off | PUBLIC has no EXECUTE on guard functions; migrated bodies = `create_all`'s; downgrade refuses without its switch | `tests/test_migration_guards.py` |

Authenticity limit (asked: "if the DB alone cannot prove authenticity"):
the database cannot tell a human click from S5 replaying the owner's
cookie. The trusted boundary is **smm-auth's step-up**: the password is
checked again for each approval, by a process smm-api cannot reach, and
the grant it issues is bound to session, revision, content hash and the
destination's external identity. The preview the owner confirms is read
by smm-auth from the database, so S5 cannot show one text and approve
another.

## 3. Authorization and isolation (FU-024C2-1/2)

| Requirement | Implementation | Test |
|---|---|---|
| owner-only business mutations | API dependencies (`Owner*` aliases) **and** DB guards | `tests/test_api_projects_rbac.py`, `tests/test_api_content.py`, `tests/test_api_publication_commands.py` (editor/admin → 403), `test_only_the_projects_owner_writes` |
| IDOR | project loaded from the URL's object, then membership; guards check the row's project | `test_the_owner_of_another_project_cannot_write_here`, RBAC suites |
| CSRF | unchanged on smm-api; smm-auth requires it on logout and step-up | `tests/test_api_csrf.py`, `tests/test_auth_service.py::test_a_confirmation_needs_session_csrf_and_password` |
| sessions | only smm-auth creates/revokes (G2); API roles cannot `INSERT user_sessions`, `UPDATE revoked_at`, `UPDATE users`, read `password_hash` | `tests/test_db_least_privilege.py`, `tests/test_db_mutation_role.py`, verify matrices |
| audit | provenance per row, see §2 | — |
| least privilege | G2 for `ai_smm_cc` and `ai_smm_cc_rw`; `ai_smm_auth` column SELECT on users + 9 smm_auth functions | verify files; in the network rehearsal on a fresh production-like database: `ai_smm_cc` 930 checks, `ai_smm_auth` 960 checks, 0 FAIL |
| no plaintext credential or private key in the API | API has the public key only; responses carry fingerprints; `hide_parameters=True` on every engine | `tests/test_api_social_accounts.py`, E2E (the token never appears in an API response) |
| smm-auth reachable only from smm-web | bind `10.231.18.2`, internal `cc-auth`, peer allowlist | packaging tests; **rehearsal T-N1..T-N5 PASSED** (`evidence/cc-auth-network-rehearsal.log`) |

## 4. Credentials (FU-025C-2)

See [docs/credential-keys.md](../../credential-keys.md). Public key only in
smm-api (compose interpolation, validated); private key only in the
worker's project (read-only mount into `worker` and `cli`, never `migrate`,
never a CC service); token ↔ account binding by associated data plus GET
/me against the bound identity before every publish; no fallback to the
deployment token for an account row (`test_an_unusable_account_fails_without_any_fallback`,
`test_the_account_factory_never_reads_the_deployment_token`); the legacy
allow-list shown in Settings equals the worker's (identity gate).

## 5. Findings fixed during the sprint

| # | Finding | Fix |
|---|---|---|
| F1 | a row inserted by the rw role **without** a link passed lineage as "legacy" — a direct way to queue unapproved text | the deferred insert check requires a link for every guarded insert |
| F2 | `has_table_privilege` missed column-level grants, so the audit guard misclassified the API roles | `has_any_column_privilege` |
| F3 | a wrong step-up password (401) would have signed the owner out in the web client (every 401 except `INVALID_CREDENTIALS` did) | `STEP_UP_FAILED` excluded; test `a wrong step-up password approves nothing and keeps the session` |
| F4 | `cc_list_set` returned non-zero on an empty list under `pipefail`, killing `cc-db-backup.sh` silently | pure-bash implementation; backup suites green |
| F5 | the auth gate's line pattern rejected function signatures in check names, so a real verify run would close the gate | pattern widened to `(),` (never `|`); live test against the real role |

## 6. Residual risks

See [residual-risks.md](residual-risks.md).
