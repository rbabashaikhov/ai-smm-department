-- ai_smm_cc -- the Control Center API's least-privilege PostgreSQL role
-- (SMM-024C-2, FU-024B3-1). Read-only business mode.
--
-- Run by an operator, as a role that may create roles (in production: the
-- instance's superuser over the database container's local socket),
-- connected to the application database itself:
--
--   psql -X -v ON_ERROR_STOP=1 -d ai_smm -f ai_smm_cc.grants.sql
--
-- One transaction: it applies completely or not at all. Re-running it is
-- safe and CONVERGENT -- it first takes away whatever ai_smm_cc holds on
-- the schema, then grants exactly the set below -- so it is also how the
-- grants are restored after a migration recreates a table, and how drift
-- is removed. Check the result with ai_smm_cc.verify.sql.
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
-- grant, is docs/least-privilege-role.md; tests/test_db_least_privilege.py
-- asserts it against a real PostgreSQL.

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
        'social_accounts', 'social_account_credentials'
    ];
    missing text[];
    owners text[];
    existing record;
BEGIN
    -- The schema must be the one this file was written for, owned by ONE
    -- role (the migration owner), which is not ai_smm_cc.
    SELECT array_agg(t) INTO missing
      FROM unnest(expected_tables) AS t
     WHERE to_regclass(format('public.%I', t)) IS NULL;

    IF missing IS NOT NULL THEN
        RAISE EXCEPTION 'ai_smm_cc: tables missing in %.public: % -- wrong database or schema revision',
            current_database(), missing;
    END IF;

    SELECT array_agg(DISTINCT pg_get_userbyid(c.relowner)) INTO owners
      FROM pg_class c
     WHERE c.oid = ANY (
         SELECT format('public.%I', t)::regclass FROM unnest(expected_tables) AS t
     );

    IF array_length(owners, 1) <> 1 THEN
        RAISE EXCEPTION 'ai_smm_cc: the application tables have % owners (%); expected exactly one',
            array_length(owners, 1), owners;
    END IF;

    IF owners[1] = 'ai_smm_cc' THEN
        RAISE EXCEPTION 'ai_smm_cc owns the application tables; refusing';
    END IF;

    IF pg_get_serial_sequence('public.audit_log', 'id') IS NULL THEN
        RAISE EXCEPTION 'ai_smm_cc: audit_log.id has no owned sequence';
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
     WHERE r.rolname = 'ai_smm_cc';

    IF FOUND THEN
        IF existing.rolsuper OR existing.rolinherit OR existing.rolcreaterole
           OR existing.rolcreatedb OR existing.rolreplication
           OR existing.rolbypassrls OR NOT existing.rolcanlogin
           OR existing.rolconnlimit <> 10 OR existing.memberships <> 0
        THEN
            RAISE EXCEPTION 'ai_smm_cc exists with unexpected attributes or memberships: %; refusing to change it',
                existing;
        END IF;
    ELSE
        -- No PASSWORD: the role cannot log in until \password is run.
        CREATE ROLE ai_smm_cc WITH
            LOGIN NOSUPERUSER NOINHERIT NOCREATEDB NOCREATEROLE
            NOREPLICATION NOBYPASSRLS
            -- One uvicorn process: pool 5 + overflow 2, plus headroom.
            CONNECTION LIMIT 10;
    END IF;
END
$preflight$;

COMMENT ON ROLE ai_smm_cc IS
    'AI SMM Control Center API, read-only business mode (SMM-024C-2). Managed by deploy/control-center/db/ai_smm_cc.grants.sql.';

-- Bounds for a process that shares the instance with the live worker: a
-- slow or stuck API query must not hold locks the worker or a migration
-- queue behind.
ALTER ROLE ai_smm_cc SET statement_timeout = '15s';
ALTER ROLE ai_smm_cc SET lock_timeout = '5s';
ALTER ROLE ai_smm_cc SET idle_in_transaction_session_timeout = '60s';

-- Converge: take away everything ai_smm_cc holds here, then grant the
-- exact set. Revoking a table privilege also revokes the column
-- privileges of that table.
DO $reset$
BEGIN
    EXECUTE format('REVOKE ALL ON DATABASE %I FROM ai_smm_cc', current_database());
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO ai_smm_cc', current_database());
END
$reset$;

REVOKE ALL ON SCHEMA public FROM ai_smm_cc;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM ai_smm_cc;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM ai_smm_cc;

GRANT USAGE ON SCHEMA public TO ai_smm_cc;

-- Reads behind every GET endpoint, the session check and the audit view.
-- users includes password_hash: login verifies it.
GRANT SELECT ON
    projects, project_memberships, project_settings,
    publications, publication_attempts, content_series,
    content_items, content_revisions, content_approvals,
    content_publication_links,
    audit_log, users, user_sessions,
    social_accounts
TO ai_smm_cc;

-- SMM-025B: credential METADATA only, column by column. Never
-- ciphertext, nonce or ephemeral_public_key: the API holds no key that
-- could open them, and it does not get to copy them either.
GRANT SELECT (id, social_account_id, status, scheme, key_id, fingerprint,
              verification_status, verified_at, expires_at,
              created_by_user_id, created_at, superseded_at, revoked_at)
    ON social_account_credentials TO ai_smm_cc;

-- POST /auth/login opens a session; every authenticated request advances
-- last_seen_at; POST /auth/logout sets revoked_at. Never DELETE. INSERT
-- names every column the ORM sends (revoked_at too, as NULL), by name, so
-- that a column added later is not granted with it.
GRANT INSERT (id, user_id, token_hash, expires_at, last_seen_at, created_at,
              revoked_at)
    ON user_sessions TO ai_smm_cc;
GRANT UPDATE (last_seen_at, revoked_at) ON user_sessions TO ai_smm_cc;

-- POST /auth/login records the login. Not password_hash: the optional
-- upgrade of an outdated Argon2 hash is skipped for this role
-- (ai_smm.application.auth, SAVEPOINT), so a compromised API cannot set
-- any account's password. Not email, is_active, display_name: accounts
-- are managed by the CLI.
GRANT UPDATE (last_login_at, updated_at) ON users TO ai_smm_cc;

-- auth.login, auth.login_failed, auth.logout. Without id and at: the API
-- cannot choose an entry's id or backdate it. No UPDATE, no DELETE.
GRANT INSERT (actor, action, subject, details) ON audit_log TO ai_smm_cc;

DO $sequence$
BEGIN
    EXECUTE format(
        'GRANT USAGE ON SEQUENCE %s TO ai_smm_cc',
        pg_get_serial_sequence('public.audit_log', 'id')
    );
END
$sequence$;

-- Fail closed on what this file cannot grant away: a privilege that
-- ai_smm_cc receives through PUBLIC. CREATE on schema public is granted
-- to PUBLIC by databases created before PostgreSQL 15 (or by hand);
-- revoking it from PUBLIC changes every role, the worker's included, and
-- is a separate decision -- so stop here instead.
DO $postcheck$
BEGIN
    IF has_schema_privilege('ai_smm_cc', 'public', 'CREATE') THEN
        RAISE EXCEPTION 'ai_smm_cc can CREATE in schema public (granted to PUBLIC); see docs/least-privilege-role.md before continuing';
    END IF;

    IF has_database_privilege('ai_smm_cc', current_database(), 'CREATE') THEN
        RAISE EXCEPTION 'ai_smm_cc can CREATE schemas in % (granted to PUBLIC); see docs/least-privilege-role.md before continuing',
            current_database();
    END IF;
END
$postcheck$;

COMMIT;
