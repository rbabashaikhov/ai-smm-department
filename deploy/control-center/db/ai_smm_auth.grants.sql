-- ai_smm_auth -- the PostgreSQL role of the isolated smm-auth service
-- (SMM-024C-3B-1; docs/adr/ADR-024C-3A-auth-service-isolation.md, §6.1 GA).
--
-- Run by an operator, as a role that may create roles (in production: the
-- instance's superuser over the database container's local socket),
-- connected to the application database itself, AFTER
-- smm_auth.functions.sql:
--
--   psql -X -v ON_ERROR_STOP=1 -d ai_smm -f ai_smm_auth.grants.sql
--
-- One transaction: it applies completely or not at all. Re-running it is
-- safe and CONVERGENT: it first takes away whatever ai_smm_auth holds in
-- this database -- table, COLUMN, sequence, schema and function
-- privileges, each kind explicitly (no reliance on a table-level revoke
-- removing column grants, ADR §6.3) -- then grants exactly the set below.
-- Check the result with ai_smm_auth.verify.sql.
--
-- The role may:
--   * connect, and use schemas public and smm_auth;
--   * read the eight named columns of users (password verification and
--     the login response) -- named, so a column added later is not
--     granted with them;
--   * execute the five smm_auth contracts.
-- Nothing else: no table-level privilege anywhere, no write on any table
-- (sessions, last_login_at and audit rows are written by the definer
-- functions), no sequence, no helper function, no password_hash UPDATE.
--
-- What it never does:
--   * set a password. The role is created without one and cannot log in
--     until an operator sets it interactively with psql's \password;
--   * change another role: not ai_smm_cc (its G1 grants and settings stay
--     exactly as they are, ADR amendment A1), not the owner, the worker or
--     PUBLIC; no membership is granted;
--   * set default privileges, or touch alembic_version.

BEGIN;

SET LOCAL lock_timeout = '5s';

DO $preflight$
DECLARE
    owner_role name;
    missing text[];
    existing record;
BEGIN
    IF to_regnamespace('smm_auth') IS NULL THEN
        RAISE EXCEPTION 'ai_smm_auth: schema smm_auth does not exist -- run smm_auth.functions.sql first';
    END IF;

    owner_role := pg_get_userbyid(
        (SELECT relowner FROM pg_class WHERE oid = 'public.users'::regclass));

    IF owner_role = 'ai_smm_auth' THEN
        RAISE EXCEPTION 'ai_smm_auth owns the application tables; refusing';
    END IF;

    -- The contracts this file grants, as smm_auth.functions.sql makes them:
    -- present, definer, owned by the table owner, search_path pinned.
    SELECT array_agg(f) INTO missing
      FROM unnest(ARRAY[
               'smm_auth.session_create(uuid,text)',
               'smm_auth.session_resolve(text)',
               'smm_auth.session_revoke(text)',
               'smm_auth.record_login_failure(text)',
               'smm_auth.recent_login_failures(text)']) AS f
     WHERE NOT EXISTS (
         SELECT 1
           FROM pg_proc p
          WHERE p.oid = to_regprocedure(f)
            AND p.prosecdef
            AND pg_get_userbyid(p.proowner) = owner_role
            AND p.proconfig = ARRAY['search_path=pg_catalog, pg_temp']
     );

    IF missing IS NOT NULL THEN
        RAISE EXCEPTION 'ai_smm_auth: contracts missing or not as smm_auth.functions.sql makes them: %', missing;
    END IF;

    SELECT array_agg(c) INTO missing
      FROM unnest(ARRAY['id', 'email', 'password_hash', 'display_name',
                        'is_active', 'last_login_at', 'created_at',
                        'updated_at']) AS c
     WHERE NOT EXISTS (
         SELECT 1 FROM pg_attribute a
          WHERE a.attrelid = 'public.users'::regclass
            AND a.attname = c AND NOT a.attisdropped
     );

    IF missing IS NOT NULL THEN
        RAISE EXCEPTION 'ai_smm_auth: users columns missing: %', missing;
    END IF;

    -- An existing role is accepted only if it is already exactly what this
    -- file would create. It is never silently altered.
    SELECT r.rolsuper, r.rolinherit, r.rolcreaterole, r.rolcreatedb,
           r.rolreplication, r.rolbypassrls, r.rolcanlogin, r.rolconnlimit,
           (SELECT count(*) FROM pg_auth_members m
             WHERE m.member = r.oid OR m.roleid = r.oid) AS memberships
      INTO existing
      FROM pg_roles r
     WHERE r.rolname = 'ai_smm_auth';

    IF FOUND THEN
        IF existing.rolsuper OR existing.rolinherit OR existing.rolcreaterole
           OR existing.rolcreatedb OR existing.rolreplication
           OR existing.rolbypassrls OR NOT existing.rolcanlogin
           OR existing.rolconnlimit <> 5 OR existing.memberships <> 0
        THEN
            RAISE EXCEPTION 'ai_smm_auth exists with unexpected attributes or memberships: %; refusing to change it',
                existing;
        END IF;
    ELSE
        -- No PASSWORD: the role cannot log in until \password is run.
        CREATE ROLE ai_smm_auth WITH
            LOGIN NOSUPERUSER NOINHERIT NOCREATEDB NOCREATEROLE
            NOREPLICATION NOBYPASSRLS
            -- One uvicorn process: pool 2 + overflow 1, plus headroom.
            CONNECTION LIMIT 5;
    END IF;
END
$preflight$;

COMMENT ON ROLE ai_smm_auth IS
    'AI SMM smm-auth service: login, logout, failed-login records (SMM-024C-3B-1). Managed by deploy/control-center/db/ai_smm_auth.grants.sql.';

-- Role settings, converged: exactly these five. The two log_parameter_*
-- settings keep the raw session token -- a function argument -- out of
-- the server log (ADR §10). log_parameter_max_length may only be changed
-- by a superuser, so the role cannot undo it; the on-error one is a user
-- setting whose default is already 0 (protection against accident, not
-- against a compromised client, which holds the token anyway).
ALTER ROLE ai_smm_auth RESET ALL;
ALTER ROLE ai_smm_auth SET statement_timeout = '5s';
ALTER ROLE ai_smm_auth SET lock_timeout = '2s';
ALTER ROLE ai_smm_auth SET idle_in_transaction_session_timeout = '30s';
ALTER ROLE ai_smm_auth SET log_parameter_max_length = 0;
ALTER ROLE ai_smm_auth SET log_parameter_max_length_on_error = 0;

-- Converge: take away everything ai_smm_auth holds in this database, each
-- kind of privilege explicitly, then grant the exact set.
DO $reset$
DECLARE
    item record;
BEGIN
    EXECUTE format('REVOKE ALL ON DATABASE %I FROM ai_smm_auth', current_database());

    -- Every schema it holds anything on (public and smm_auth included).
    FOR item IN
        SELECT n.nspname
          FROM pg_namespace n, aclexplode(n.nspacl) a
         WHERE a.grantee = 'ai_smm_auth'::regrole
         GROUP BY n.nspname
    LOOP
        EXECUTE format('REVOKE ALL ON SCHEMA %I FROM ai_smm_auth', item.nspname);
    END LOOP;

    -- Table-level privileges on every relation, in any schema.
    FOR item IN
        SELECT c.oid::regclass AS rel, c.relkind
          FROM pg_class c, aclexplode(c.relacl) a
         WHERE a.grantee = 'ai_smm_auth'::regrole
         GROUP BY c.oid, c.relkind
    LOOP
        IF item.relkind = 'S' THEN
            EXECUTE format('REVOKE ALL ON SEQUENCE %s FROM ai_smm_auth', item.rel);
        ELSE
            EXECUTE format('REVOKE ALL ON TABLE %s FROM ai_smm_auth', item.rel);
        END IF;
    END LOOP;

    -- Column-level privileges, one column at a time: whatever a
    -- table-level revoke does or does not take with it, none survive.
    FOR item IN
        SELECT att.attrelid::regclass AS rel, att.attname, a.privilege_type
          FROM pg_attribute att, aclexplode(att.attacl) a
         WHERE att.attacl IS NOT NULL
           AND a.grantee = 'ai_smm_auth'::regrole
    LOOP
        EXECUTE format('REVOKE %s (%I) ON TABLE %s FROM ai_smm_auth',
                       item.privilege_type, item.attname, item.rel);
    END LOOP;

    -- Every function, in any schema.
    FOR item IN
        SELECT p.oid::regprocedure AS fn
          FROM pg_proc p, aclexplode(p.proacl) a
         WHERE a.grantee = 'ai_smm_auth'::regrole
         GROUP BY p.oid
    LOOP
        EXECUTE format('REVOKE ALL ON FUNCTION %s FROM ai_smm_auth', item.fn);
    END LOOP;

    EXECUTE format('GRANT CONNECT ON DATABASE %I TO ai_smm_auth', current_database());
END
$reset$;

GRANT USAGE ON SCHEMA public TO ai_smm_auth;
GRANT USAGE ON SCHEMA smm_auth TO ai_smm_auth;

-- Password verification reads the stored hash; the login response shows
-- the profile fields. SELECT only, on named columns.
GRANT SELECT (id, email, password_hash, display_name, is_active,
              last_login_at, created_at, updated_at)
    ON public.users TO ai_smm_auth;

GRANT EXECUTE ON FUNCTION
    smm_auth.session_create(uuid, text),
    smm_auth.session_resolve(text),
    smm_auth.session_revoke(text),
    smm_auth.record_login_failure(text),
    smm_auth.recent_login_failures(text)
TO ai_smm_auth;

-- Fail closed on what this file cannot grant away: privileges that reach
-- the role through PUBLIC (it is a separate decision to change PUBLIC,
-- which affects every role, the worker's included).
DO $postcheck$
BEGIN
    IF has_schema_privilege('ai_smm_auth', 'public', 'CREATE')
       OR has_schema_privilege('ai_smm_auth', 'smm_auth', 'CREATE')
    THEN
        RAISE EXCEPTION 'ai_smm_auth can CREATE in schema public or smm_auth; see docs/least-privilege-role.md before continuing';
    END IF;

    IF has_database_privilege('ai_smm_auth', current_database(), 'CREATE') THEN
        RAISE EXCEPTION 'ai_smm_auth can CREATE schemas in % (granted to PUBLIC); see docs/least-privilege-role.md before continuing',
            current_database();
    END IF;

    IF has_function_privilege('ai_smm_auth', 'smm_auth.token_digest(text)', 'EXECUTE')
       OR has_function_privilege('ai_smm_auth', 'smm_auth.failure_subject(text)', 'EXECUTE')
    THEN
        RAISE EXCEPTION 'ai_smm_auth can execute an internal smm_auth helper';
    END IF;

    IF has_table_privilege('ai_smm_auth', 'public.users', 'SELECT')
       OR has_column_privilege('ai_smm_auth', 'public.users', 'password_hash', 'UPDATE')
    THEN
        RAISE EXCEPTION 'ai_smm_auth holds table-level SELECT on users or may UPDATE password_hash';
    END IF;
END
$postcheck$;

COMMIT;
