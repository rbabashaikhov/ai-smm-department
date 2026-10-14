-- ai_smm_cc_rw -- the Control Center API's PostgreSQL role for the
-- BUSINESS-MUTATION mode (SMM-025C, FU-025B-1 / FU-024C2-3).
--
-- The read-only mode keeps ai_smm_cc, which PostgreSQL refuses every
-- business write. This role is ai_smm_cc's privileges plus, column by
-- column, the writes the API's business commands make -- and the API uses
-- it only while the Control Center runs in mutation mode (cc-compose.sh
-- mutations). Still never: DELETE or TRUNCATE anywhere, a write to
-- publication_attempts or alembic_version, an UPDATE of human_reviewed,
-- threads_post_id, published_at or a publication's content or account, a
-- read of credential key material, a password, a user, a role change.
--
-- Run by an operator, as a role that may create roles (in production: the
-- instance's superuser over the database container's local socket),
-- connected to the application database itself:
--
--   psql -X -v ON_ERROR_STOP=1 -d ai_smm -f ai_smm_cc_rw.grants.sql
--
-- One transaction: it applies completely or not at all. Re-running it is
-- safe and CONVERGENT -- it first takes away whatever ai_smm_cc_rw holds on
-- the schema, then grants exactly the set below -- so it is also how the
-- grants are restored after a migration recreates a table, and how drift
-- is removed. Check the result with ai_smm_cc_rw.verify.sql.
--
-- What it never does:
--   * set a password. The role is created without one and cannot log in
--     until an operator sets it interactively with psql's \password, which
--     hashes it client-side: the secret is not in this file, in a shell
--     history, in the server log or in pg_stat_statements;
--   * change another role. Nothing here is granted to or revoked from the
--     owner role, the worker or PUBLIC; no membership is granted;
--   * set default privileges. A table added by a later migration is
--     invisible to the API until this file grants it (fail closed);
--   * touch alembic_version, or any schema other than public.
--
-- The privilege matrix this implements, with the endpoint behind every
-- grant, is docs/mutation-role.md; tests/test_db_mutation_role.py asserts
-- it against a real PostgreSQL, and tests/mutation_role/ re-runs the API's
-- business-command suites connected as this role.

BEGIN;

-- A catalog lock held by another session must not make this wait (and so
-- make every later reader of the same objects wait behind it).
SET LOCAL lock_timeout = '5s';

DO $preflight$
DECLARE
    expected_tables CONSTANT text[] := ARRAY[
        'projects', 'project_memberships', 'project_settings',
        'publications', 'publication_attempts', 'content_series',
        'content_items', 'content_revisions', 'content_approvals',
        'content_publication_links', 'audit_log', 'users', 'user_sessions',
        'social_accounts', 'social_account_credentials',
        -- SMM-025D: without its guards this role must not exist at all.
        'mutation_contexts', 'approval_grants'
    ];
    expected_functions CONSTANT text[] := ARRAY[
        'public.guard_bind_session(text)',
        'public.guard_context_user()',
        'public.guard_context_session()',
        'public.guard_require_owner(text, text)',
        'public.guard_publication_problem(bigint)',
        'public.guard_consume_approval_grant(uuid, uuid, uuid)'
    ];
    missing text[];
    owners text[];
    existing record;
BEGIN
    -- The schema must be the one this file was written for, owned by ONE
    -- role (the migration owner), which is not ai_smm_cc_rw.
    SELECT array_agg(t) INTO missing
      FROM unnest(expected_tables) AS t
     WHERE to_regclass(format('public.%I', t)) IS NULL;

    IF missing IS NOT NULL THEN
        RAISE EXCEPTION 'ai_smm_cc_rw: tables missing in %.public: % -- wrong database or schema revision',
            current_database(), missing;
    END IF;

    SELECT array_agg(f) INTO missing
      FROM unnest(expected_functions) AS f
     WHERE to_regprocedure(f) IS NULL;

    IF missing IS NOT NULL THEN
        RAISE EXCEPTION 'ai_smm_cc_rw: guard functions missing in %: % -- migrate to c4d81e2a9f63 first',
            current_database(), missing;
    END IF;

    SELECT array_agg(DISTINCT pg_get_userbyid(c.relowner)) INTO owners
      FROM pg_class c
     WHERE c.oid = ANY (
         SELECT format('public.%I', t)::regclass FROM unnest(expected_tables) AS t
     );

    IF array_length(owners, 1) <> 1 THEN
        RAISE EXCEPTION 'ai_smm_cc_rw: the application tables have % owners (%); expected exactly one',
            array_length(owners, 1), owners;
    END IF;

    IF owners[1] = 'ai_smm_cc_rw' THEN
        RAISE EXCEPTION 'ai_smm_cc_rw owns the application tables; refusing';
    END IF;

    IF pg_get_serial_sequence('public.audit_log', 'id') IS NULL THEN
        RAISE EXCEPTION 'ai_smm_cc_rw: audit_log.id has no owned sequence';
    END IF;

    -- An existing role is accepted only if it is already exactly what this
    -- file would create. It is never silently altered: a role with other
    -- attributes is someone else's decision.
    SELECT r.rolsuper, r.rolinherit, r.rolcreaterole, r.rolcreatedb,
           r.rolreplication, r.rolbypassrls, r.rolcanlogin, r.rolconnlimit,
           (SELECT count(*) FROM pg_auth_members m WHERE m.member = r.oid)
               AS memberships
      INTO existing
      FROM pg_roles r
     WHERE r.rolname = 'ai_smm_cc_rw';

    IF FOUND THEN
        IF existing.rolsuper OR existing.rolinherit OR existing.rolcreaterole
           OR existing.rolcreatedb OR existing.rolreplication
           OR existing.rolbypassrls OR NOT existing.rolcanlogin
           OR existing.rolconnlimit <> 10 OR existing.memberships <> 0
        THEN
            RAISE EXCEPTION 'ai_smm_cc_rw exists with unexpected attributes or memberships: %; refusing to change it',
                existing;
        END IF;
    ELSE
        -- No PASSWORD: the role cannot log in until \password is run.
        CREATE ROLE ai_smm_cc_rw WITH
            LOGIN NOSUPERUSER NOINHERIT NOCREATEDB NOCREATEROLE
            NOREPLICATION NOBYPASSRLS
            -- One uvicorn process: pool 5 + overflow 2, plus headroom.
            CONNECTION LIMIT 10;
    END IF;
END
$preflight$;

COMMENT ON ROLE ai_smm_cc_rw IS
    'AI SMM Control Center API, business-mutation mode (SMM-025C). Managed by deploy/control-center/db/ai_smm_cc_rw.grants.sql.';

-- Bounds for a process that shares the instance with the live worker: a
-- slow or stuck API query must not hold locks the worker or a migration
-- queue behind.
ALTER ROLE ai_smm_cc_rw SET statement_timeout = '15s';
ALTER ROLE ai_smm_cc_rw SET lock_timeout = '5s';
ALTER ROLE ai_smm_cc_rw SET idle_in_transaction_session_timeout = '60s';

-- Converge: take away everything ai_smm_cc_rw holds here, then grant the
-- exact set. Revoking a table privilege also revokes the column
-- privileges of that table.
DO $reset$
BEGIN
    EXECUTE format('REVOKE ALL ON DATABASE %I FROM ai_smm_cc_rw', current_database());
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO ai_smm_cc_rw', current_database());
END
$reset$;

REVOKE ALL ON SCHEMA public FROM ai_smm_cc_rw;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM ai_smm_cc_rw;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM ai_smm_cc_rw;

GRANT USAGE ON SCHEMA public TO ai_smm_cc_rw;

-- Reads behind every GET endpoint, the session check and the audit view.
GRANT SELECT ON
    projects, project_memberships, project_settings,
    publications, publication_attempts, content_series,
    content_items, content_revisions, content_approvals,
    content_publication_links,
    audit_log, user_sessions,
    social_accounts
TO ai_smm_cc_rw;

-- SMM-025D, grants G2 (ADR-024C-3A §6.2, correction 1): users column by
-- column, never password_hash. Signing in is smm-auth's (ai_smm_auth); a
-- table-level SELECT would expose every column, a later one included.
GRANT SELECT (id, email, display_name, is_active, last_login_at,
              created_at, updated_at)
    ON users TO ai_smm_cc_rw;

-- SMM-025B: credential METADATA only, column by column. Never
-- ciphertext, nonce or ephemeral_public_key: the API holds no key that
-- could open them, and it does not get to copy them either.
GRANT SELECT (id, social_account_id, status, scheme, key_id, fingerprint,
              verification_status, verified_at, expires_at,
              created_by_user_id, created_at, superseded_at, revoked_at)
    ON social_account_credentials TO ai_smm_cc_rw;

-- G2 (SMM-025D): sessions are created, revoked and audited by smm-auth
-- only (smm_auth.session_create / session_revoke, as ai_smm_auth). This
-- role keeps what the session check of a read needs: advancing
-- last_seen_at (ADR-024C-3A RS-2); the identity of every write is
-- decided in SQL by guard_bind_session. No INSERT, no revoked_at, no
-- write to users.
GRANT UPDATE (last_seen_at) ON user_sessions TO ai_smm_cc_rw;

-- Business audit entries only (SMM-025D guard: as the bound user, business
-- actions only; sign-in audit is smm-auth's). Without id, at and
-- written_by: the API cannot choose an entry's id, backdate it, or claim
-- another writer. No UPDATE, no DELETE.
GRANT INSERT (actor, action, subject, details) ON audit_log TO ai_smm_cc_rw;

DO $sequence$
BEGIN
    EXECUTE format(
        'GRANT USAGE ON SEQUENCE %s TO ai_smm_cc_rw',
        pg_get_serial_sequence('public.audit_log', 'id')
    );
END
$sequence$;


-- ---------------------------------------------------------------------------
-- Business writes (mutation mode only). INSERT names every column the ORM sends (unset ones as NULL);
-- UPDATE names only what a command changes. A column added by a later
-- migration is granted to nobody until this file names it.

-- POST /projects: the project row (create_project).
GRANT INSERT (id, display_name, description, knowledge_path, default_platform, is_active, default_timezone, default_language, version, created_at, updated_at)
    ON projects TO ai_smm_cc_rw;

-- PATCH /projects/{id}. Never id, knowledge_path, default_platform, is_active.
GRANT UPDATE (display_name, description, default_timezone, default_language, version, updated_at)
    ON projects TO ai_smm_cc_rw;

-- POST /projects: the creator's owner membership. No UPDATE and no DELETE: no role is changed through the API.
GRANT INSERT (id, project_id, user_id, role, created_at, updated_at)
    ON project_memberships TO ai_smm_cc_rw;

-- POST /projects and the first PATCH /projects/{id}/settings.
GRANT INSERT (project_id, content_config, publishing_config, brand_config, version, updated_at)
    ON project_settings TO ai_smm_cc_rw;

-- PATCH /projects/{id}/settings (version-checked).
GRANT UPDATE (content_config, publishing_config, brand_config, version, updated_at)
    ON project_settings TO ai_smm_cc_rw;

-- POST /projects/{id}/social-accounts (the identity columns are sent as NULL).
GRANT INSERT (id, project_id, platform, display_name, status, external_account_id, external_username, version, created_by_user_id, created_at, updated_at, disabled_at)
    ON social_accounts TO ai_smm_cc_rw;

-- rename, disable/enable, and binding the identity on the first verified credential. Never project_id or platform.
GRANT UPDATE (display_name, status, disabled_at, external_account_id, external_username, version, updated_at)
    ON social_accounts TO ai_smm_cc_rw;

-- POST /social-accounts/{id}/credentials: a sealed token. The role WRITES key material and still cannot READ it (column SELECT above is metadata only).
GRANT INSERT (id, social_account_id, status, scheme, key_id, ephemeral_public_key, nonce, ciphertext, fingerprint, verification_status, verified_at, expires_at, created_by_user_id, created_at, superseded_at, revoked_at)
    ON social_account_credentials TO ai_smm_cc_rw;

-- supersede and revoke: the status change and wiping the material to NULL. Never social_account_id: a ciphertext cannot be moved to another account.
GRANT UPDATE (status, ephemeral_public_key, nonce, ciphertext, superseded_at, revoked_at)
    ON social_account_credentials TO ai_smm_cc_rw;

-- POST /projects/{id}/content-items.
GRANT INSERT (id, project_id, title, content_type, status, current_revision_id, approved_revision_id, version, created_by_user_id, created_at, updated_at, archived_at)
    ON content_items TO ai_smm_cc_rw;

-- new revision, submit, approve, reject. Never project_id.
GRANT UPDATE (current_revision_id, approved_revision_id, status, version, updated_at)
    ON content_items TO ai_smm_cc_rw;

-- a new immutable revision (the append-only trigger refuses UPDATE and DELETE for every role).
GRANT INSERT (id, content_item_id, revision_number, title, body, format, images, items, metadata, source, source_ref, editor_score, editor_notes, content_hash, social_account_id, created_by_user_id, created_at)
    ON content_revisions TO ai_smm_cc_rw;

-- approve / reject: one human decision (append-only).
GRANT INSERT (id, content_item_id, revision_id, decision, actor_user_id, note, created_at)
    ON content_approvals TO ai_smm_cc_rw;

-- materialize: the lineage row (the link trigger checks project and platform).
GRANT INSERT (content_item_id, revision_id, platform, publication_id, created_at)
    ON content_publication_links TO ai_smm_cc_rw;

-- materialize: a new delivery snapshot in `approved`. The ORM sends every column; the ones the API never sets (threads_post_id, published_at, the claim and series columns) go in as NULL. Not id: the sequence assigns it.
GRANT INSERT (project_id, platform, ordinal, title, format, body, images, items, status, scheduled_at, idempotency_key, threads_post_id, published_at, claimed_by, claimed_at, lease_expires_at, attempt_count, next_attempt_at, last_error, editor_score, human_reviewed, source_ref, social_account_id, series_id, series_position, series_total, parent_publication_id, previous_publication_id, created_at, updated_at)
    ON publications TO ai_smm_cc_rw;

-- schedule / reschedule / cancel (ai_smm.queue.reschedule, cancel). Never the content, human_reviewed, threads_post_id, published_at, project_id, social_account_id or the series links.
GRANT UPDATE (status, scheduled_at, next_attempt_at, claimed_by, claimed_at, lease_expires_at, last_error, attempt_count, updated_at)
    ON publications TO ai_smm_cc_rw;

-- materialize allocates publications.id from its sequence.
DO $business_sequence$
BEGIN
    EXECUTE format(
        'GRANT USAGE ON SEQUENCE %s TO ai_smm_cc_rw',
        pg_get_serial_sequence('public.publications', 'id')
    );
END
$business_sequence$;

-- SMM-025D: the guard functions this role calls, directly or from a guard
-- trigger that runs as it (ai_smm.db.guards). Their bodies run as the
-- table owner where they read or write what this role may not -- the
-- session table, mutation_contexts, approval_grants -- so EXECUTE is all
-- it gets. Never guard_token_digest; never a trigger function (those are
-- not callable); nothing else in the schema.
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM ai_smm_cc_rw;
GRANT EXECUTE ON FUNCTION
    public.guard_bind_session(text),
    public.guard_context_user(),
    public.guard_context_session(),
    public.guard_require_owner(text, text),
    public.guard_publication_problem(bigint),
    public.guard_consume_approval_grant(uuid, uuid, uuid)
TO ai_smm_cc_rw;

-- Fail closed on what this file cannot grant away: a privilege that
-- ai_smm_cc_rw receives through PUBLIC. CREATE on schema public is granted
-- to PUBLIC by databases created before PostgreSQL 15 (or by hand);
-- revoking it from PUBLIC changes every role, the worker's included, and
-- is a separate decision -- so stop here instead.
DO $postcheck$
BEGIN
    IF has_schema_privilege('ai_smm_cc_rw', 'public', 'CREATE') THEN
        RAISE EXCEPTION 'ai_smm_cc_rw can CREATE in schema public (granted to PUBLIC); see docs/least-privilege-role.md before continuing';
    END IF;

    IF has_database_privilege('ai_smm_cc_rw', current_database(), 'CREATE') THEN
        RAISE EXCEPTION 'ai_smm_cc_rw can CREATE schemas in % (granted to PUBLIC); see docs/least-privilege-role.md before continuing',
            current_database();
    END IF;
END
$postcheck$;

COMMIT;
