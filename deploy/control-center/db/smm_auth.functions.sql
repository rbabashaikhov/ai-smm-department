-- smm_auth -- the PostgreSQL authentication primitives of the isolated
-- smm-auth service (SMM-024C-3B-1). Design and contracts:
-- docs/adr/ADR-024C-3A-auth-service-isolation.md, §8.
--
-- Run by an operator, as the instance's superuser, connected to the
-- application database itself:
--
--   psql -X -v ON_ERROR_STOP=1 -d ai_smm -f smm_auth.functions.sql
--
-- One transaction: it applies completely or not at all. Re-running it is
-- safe and CONVERGENT: CREATE OR REPLACE keeps every function's owner and
-- grants, and each run re-asserts the owner, the pinned search_path and
-- that PUBLIC holds nothing on the schema or its functions.
--
-- What it creates: schema smm_auth, owned by the role that owns the
-- application tables (the migration owner), and seven functions in it.
-- Five are the contracts; two are internal helpers only the owner may
-- execute. Check the result with ai_smm_auth.verify.sql, which pins every
-- function body.
--
-- What it never does:
--   * touch a table, a column, a constraint or alembic_version: it adds
--     no DDL to the publication queue and needs no migration window
--     (ADR amendment A6);
--   * grant anything to anyone. ai_smm_auth.grants.sql does that,
--     afterwards; until then only the owner may call these functions;
--   * create, change or name a role;
--   * provide a way to write users.password_hash. No network-facing role
--     may change a password (ADR §11).
--
-- It refuses, changing nothing, when:
--   * the table owner is a superuser, or lacks -- effectively -- any
--     privilege the function bodies use (USAGE on public; SELECT/UPDATE
--     on users; SELECT/INSERT/UPDATE on user_sessions; SELECT/INSERT on
--     audit_log; USAGE on audit_log's sequence; EXECUTE on the built-ins);
--   * schema smm_auth already exists with another owner, with objects this
--     file does not make, with a function owned by another role, or with
--     any grant beyond the designed ones -- refused before any body is
--     replaced, never repaired silently.
-- Every function is created and stripped of PUBLIC's default EXECUTE in
-- this one transaction, so no committed state ever lets PUBLIC call one.
--
-- Rolled back by smm_auth.drop.sql, after ai_smm_auth.revoke.sql.
--
-- Rules every function follows (ADR §8.1):
--   * SECURITY DEFINER only where the caller needs the owner's rights,
--     always with SET search_path = pg_catalog, pg_temp: pg_temp is named
--     last explicitly, so no temporary object can shadow anything;
--   * every table is schema-qualified (public.*), every helper too
--     (smm_auth.*); no dynamic SQL;
--   * nothing the caller supplies becomes a timestamp, an expiry, an
--     actor or an audit action: the server's now() and the constants
--     below decide those;
--   * the raw session token is hashed here, exactly as
--     ai_smm.security.tokens.hash_token does: hex(sha256(utf-8)). Only
--     the digest is ever stored or compared, and no error message
--     contains the token.

BEGIN;

-- A catalog lock held by another session must not make this wait.
SET LOCAL lock_timeout = '5s';

DO $preflight$
DECLARE
    owner_role name;
    owners text[];
    owner_is_super boolean;
    missing text[];
    unexpected text[];
BEGIN
    -- gen_random_uuid() and sha256() are built in from PostgreSQL 13.
    IF current_setting('server_version_num')::int < 130000 THEN
        RAISE EXCEPTION 'smm_auth: PostgreSQL 13 or later is required, found %',
            current_setting('server_version');
    END IF;

    SELECT array_agg(t) INTO missing
      FROM unnest(ARRAY['users', 'user_sessions', 'audit_log']) AS t
     WHERE to_regclass(format('public.%I', t)) IS NULL;

    IF missing IS NOT NULL THEN
        RAISE EXCEPTION 'smm_auth: tables missing in %.public: % -- wrong database or schema revision',
            current_database(), missing;
    END IF;

    SELECT array_agg(DISTINCT pg_get_userbyid(c.relowner)) INTO owners
      FROM pg_class c
     WHERE c.oid IN ('public.users'::regclass, 'public.user_sessions'::regclass,
                     'public.audit_log'::regclass);

    IF array_length(owners, 1) <> 1 THEN
        RAISE EXCEPTION 'smm_auth: users, user_sessions and audit_log have % owners (%); expected exactly one',
            array_length(owners, 1), owners;
    END IF;

    owner_role := owners[1];

    IF owner_role IN ('ai_smm_cc', 'ai_smm_auth') THEN
        RAISE EXCEPTION 'smm_auth: the application tables are owned by %; refusing', owner_role;
    END IF;

    -- The definer functions run with the owner's rights. A superuser
    -- owner would turn a defect in one of them into control of the whole
    -- shared instance.
    SELECT rolsuper INTO owner_is_super FROM pg_roles WHERE rolname = owner_role;

    IF owner_is_super THEN
        RAISE EXCEPTION 'smm_auth: the table owner % is a superuser; refusing to create SECURITY DEFINER functions owned by it',
            owner_role;
    END IF;

    -- The columns and types the functions write and compare. A schema
    -- that drifted from the one the contracts were written for stops here.
    SELECT array_agg(format('%s.%s %s', e.tab, e.col, e.typ)) INTO missing
      FROM (VALUES
              ('users', 'id', 'uuid'),
              ('users', 'is_active', 'boolean'),
              ('users', 'last_login_at', 'timestamp with time zone'),
              ('users', 'updated_at', 'timestamp with time zone'),
              ('user_sessions', 'id', 'uuid'),
              ('user_sessions', 'user_id', 'uuid'),
              ('user_sessions', 'token_hash', 'character varying(64)'),
              ('user_sessions', 'expires_at', 'timestamp with time zone'),
              ('user_sessions', 'last_seen_at', 'timestamp with time zone'),
              ('user_sessions', 'created_at', 'timestamp with time zone'),
              ('user_sessions', 'revoked_at', 'timestamp with time zone'),
              ('audit_log', 'at', 'timestamp with time zone'),
              ('audit_log', 'actor', 'character varying(160)'),
              ('audit_log', 'action', 'character varying(120)'),
              ('audit_log', 'subject', 'character varying(160)'),
              ('audit_log', 'details', 'jsonb')
           ) AS e(tab, col, typ)
     WHERE NOT EXISTS (
         SELECT 1
           FROM pg_attribute a
          WHERE a.attrelid = format('public.%I', e.tab)::regclass
            AND a.attname = e.col
            AND NOT a.attisdropped
            AND format_type(a.atttypid, a.atttypmod) = e.typ
     );

    IF missing IS NOT NULL THEN
        RAISE EXCEPTION 'smm_auth: columns missing or of another type: %', missing;
    END IF;

    -- audit_log.id and audit_log.at must come from the server.
    IF pg_get_serial_sequence('public.audit_log', 'id') IS NULL
       OR NOT EXISTS (SELECT 1 FROM pg_attrdef d
                       JOIN pg_attribute a ON a.attrelid = d.adrelid AND a.attnum = d.adnum
                      WHERE d.adrelid = 'public.audit_log'::regclass AND a.attname = 'at')
    THEN
        RAISE EXCEPTION 'smm_auth: audit_log.id has no sequence or audit_log.at has no default';
    END IF;

    -- A session is found by its digest; the digest must be unique.
    IF NOT EXISTS (
        SELECT 1
          FROM pg_index i
         WHERE i.indrelid = 'public.user_sessions'::regclass
           AND i.indisunique
           AND i.indnatts = 1
           AND i.indkey[0] = (SELECT attnum FROM pg_attribute
                               WHERE attrelid = 'public.user_sessions'::regclass
                                 AND attname = 'token_hash')
    ) THEN
        RAISE EXCEPTION 'smm_auth: user_sessions.token_hash is not unique';
    END IF;

    -- The owner runs every definer function, so it must hold -- EFFECTIVELY,
    -- whatever the route (ownership, a grant, PUBLIC) -- each privilege the
    -- function bodies use. Owning a table normally implies them all, but an
    -- owner's own privileges can be revoked, and a missing one would only
    -- surface at the first login. Checked one by one: has_*_privilege with a
    -- comma-separated list answers "any of", not "all of".
    SELECT array_agg(format('%s on %s', e.priv, e.obj)) INTO missing
      FROM (VALUES
              -- resolve the tables at all
              ('schema', 'public', 'USAGE'),
              -- session_create: UPDATE ... RETURNING; session_resolve and
              -- session_revoke: the join
              ('table', 'public.users', 'SELECT'),
              ('table', 'public.users', 'UPDATE'),
              -- session_create: INSERT; resolve/revoke: SELECT, the touch and
              -- the revoke UPDATE, and FOR UPDATE (which needs UPDATE)
              ('table', 'public.user_sessions', 'SELECT'),
              ('table', 'public.user_sessions', 'INSERT'),
              ('table', 'public.user_sessions', 'UPDATE'),
              -- the audit rows, and the failure count
              ('table', 'public.audit_log', 'SELECT'),
              ('table', 'public.audit_log', 'INSERT'),
              -- audit_log.id's default: nextval()
              ('sequence', pg_get_serial_sequence('public.audit_log', 'id'), 'USAGE')
           ) AS e(kind, obj, priv)
     WHERE NOT CASE e.kind
               WHEN 'schema' THEN has_schema_privilege(owner_role, e.obj, e.priv)
               WHEN 'table' THEN has_table_privilege(owner_role, e.obj, e.priv)
               ELSE has_sequence_privilege(owner_role, e.obj, e.priv)
           END;

    IF missing IS NOT NULL THEN
        RAISE EXCEPTION 'smm_auth: the table owner % lacks privileges the functions need: %',
            owner_role, missing;
    END IF;

    -- The built-ins the bodies call; PUBLIC normally holds EXECUTE on them,
    -- but a hardened instance may not.
    SELECT array_agg(f) INTO missing
      FROM unnest(ARRAY['gen_random_uuid', 'sha256', 'convert_to', 'encode',
                        'jsonb_build_object']) AS f
     WHERE NOT EXISTS (
         SELECT 1 FROM pg_proc p
          WHERE p.pronamespace = 'pg_catalog'::regnamespace
            AND p.proname = f
            AND has_function_privilege(owner_role, p.oid, 'EXECUTE')
     );

    IF missing IS NOT NULL THEN
        RAISE EXCEPTION 'smm_auth: the table owner % cannot execute built-in functions: %',
            owner_role, missing;
    END IF;

    -- An existing schema is accepted only if it is exactly what this file
    -- makes: same owner, nothing in it but these functions, each owned by
    -- the same role, and no grant beyond the designed ones. Anything else
    -- is refused BEFORE a body is replaced -- CREATE OR REPLACE would keep
    -- a foreign owner's grants, and a later ALTER OWNER would hide whose
    -- function it was.
    IF to_regnamespace('smm_auth') IS NOT NULL THEN
        IF (SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'smm_auth')
           <> owner_role
        THEN
            RAISE EXCEPTION 'smm_auth: the schema exists and is not owned by %; refusing', owner_role;
        END IF;

        SELECT array_agg(o) INTO unexpected
          FROM (
              SELECT p.oid::regprocedure::text AS o
                FROM pg_proc p
               WHERE p.pronamespace = 'smm_auth'::regnamespace
                 AND p.oid::regprocedure::text NOT IN (
                     'smm_auth.token_digest(text)',
                     'smm_auth.failure_subject(text)',
                     'smm_auth.session_create(uuid,text)',
                     'smm_auth.session_resolve(text)',
                     'smm_auth.session_revoke(text)',
                     'smm_auth.record_login_failure(text)',
                     'smm_auth.recent_login_failures(text)')
              UNION ALL
              SELECT c.oid::regclass::text
                FROM pg_class c WHERE c.relnamespace = 'smm_auth'::regnamespace
              UNION ALL
              SELECT t.oid::regtype::text
                FROM pg_type t
               WHERE t.typnamespace = 'smm_auth'::regnamespace
                 AND t.typrelid = 0 AND t.typelem = 0
          ) AS extra;

        IF unexpected IS NOT NULL THEN
            RAISE EXCEPTION 'smm_auth: the schema holds objects this file does not make: %', unexpected;
        END IF;

        SELECT array_agg(p.oid::regprocedure::text || ' owned by ' || pg_get_userbyid(p.proowner))
          INTO unexpected
          FROM pg_proc p
         WHERE p.pronamespace = 'smm_auth'::regnamespace
           AND pg_get_userbyid(p.proowner) <> owner_role;

        IF unexpected IS NOT NULL THEN
            RAISE EXCEPTION 'smm_auth: functions in the schema have another owner: %', unexpected;
        END IF;

        SELECT array_agg(format('%s %s -> %s', g.privilege, g.obj,
                                CASE g.grantee WHEN 0 THEN 'PUBLIC'
                                     ELSE pg_get_userbyid(g.grantee) END))
          INTO unexpected
          FROM (
              SELECT 'schema smm_auth' AS obj, false AS contract,
                     a.grantee, a.privilege_type AS privilege, n.nspowner AS owner_oid
                FROM pg_namespace n,
                     aclexplode(coalesce(n.nspacl, acldefault('n', n.nspowner))) a
               WHERE n.nspname = 'smm_auth'
              UNION ALL
              SELECT p.oid::regprocedure::text,
                     p.proname NOT IN ('token_digest', 'failure_subject'),
                     a.grantee, a.privilege_type, p.proowner
                FROM pg_proc p,
                     aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) a
               WHERE p.pronamespace = 'smm_auth'::regnamespace
          ) AS g
         WHERE g.grantee <> g.owner_oid
           AND NOT (g.grantee = coalesce(to_regrole('ai_smm_auth')::oid, 0::oid)
                    AND g.grantee <> 0
                    AND ((g.obj = 'schema smm_auth' AND g.privilege = 'USAGE')
                         OR (g.contract AND g.privilege = 'EXECUTE')));

        IF unexpected IS NOT NULL THEN
            RAISE EXCEPTION 'smm_auth: the schema or its functions carry unexpected grants: %', unexpected;
        END IF;

    ELSE
        EXECUTE format('CREATE SCHEMA smm_auth AUTHORIZATION %I', owner_role);
    END IF;
END
$preflight$;

COMMENT ON SCHEMA smm_auth IS
    'Authentication primitives of the smm-auth service (SMM-024C-3B-1). Managed by deploy/control-center/db/smm_auth.functions.sql.';

-- -- internal helpers: no SECURITY DEFINER, EXECUTE for the owner only ----------

-- The digest the session table stores: hex(sha256(utf-8 bytes of the
-- token)), identical to ai_smm.security.tokens.hash_token. A token is
-- secrets.token_urlsafe(32): exactly 43 URL-safe characters. Anything
-- else is refused before it is hashed or compared, and the error does not
-- repeat the input.
CREATE OR REPLACE FUNCTION smm_auth.token_digest(p_token text)
RETURNS text
LANGUAGE plpgsql
STABLE STRICT
SET search_path = pg_catalog, pg_temp
AS $token_digest$
BEGIN
    IF p_token !~ '^[A-Za-z0-9_-]{43}$' THEN
        RAISE EXCEPTION 'smm_auth: malformed session token'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    RETURN encode(sha256(convert_to(p_token, 'UTF8')), 'hex');
END
$token_digest$;

-- The audit subject of a failed login, exactly as the API writes it:
-- 'email:' || the normalised address (ai_smm.application.users
-- .normalize_email: trimmed, lower case). audit_log.subject holds 160
-- characters, so an address longer than 154 is refused here instead of
-- failing on the column.
CREATE OR REPLACE FUNCTION smm_auth.failure_subject(p_email text)
RETURNS text
LANGUAGE plpgsql
IMMUTABLE STRICT
SET search_path = pg_catalog, pg_temp
AS $failure_subject$
BEGIN
    IF p_email <> lower(p_email)
       OR p_email <> btrim(p_email)
       OR length(p_email) > 154
       OR p_email !~ '^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$'
    THEN
        RAISE EXCEPTION 'smm_auth: not a normalised e-mail address'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    RETURN 'email:' || p_email;
END
$failure_subject$;

-- -- contracts: SECURITY DEFINER, EXECUTE granted by ai_smm_auth.grants.sql ----

-- Open a session for a user whose password smm-auth has just verified.
-- Every timestamp is the server's; the lifetime is the constant 7 days
-- (AI_SMM_API_SESSION_LIFETIME_SECONDS default). Sets last_login_at and
-- updated_at, and writes auth.login in the API's exact shape, in the same
-- transaction: the audit time equals created_at, so session-anomalies.sql
-- finds nothing to report. An unknown or inactive user gets no row and
-- nothing is written.
CREATE OR REPLACE FUNCTION smm_auth.session_create(p_user_id uuid, p_token text)
RETURNS TABLE (session_id uuid, expires_at timestamptz)
LANGUAGE plpgsql
VOLATILE STRICT
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $session_create$
DECLARE
    v_hash text := smm_auth.token_digest(p_token);
    v_user uuid;
    v_session uuid;
    v_expires timestamptz;
BEGIN
    UPDATE public.users AS u
       SET last_login_at = now(),
           updated_at = now()
     WHERE u.id = p_user_id
       AND u.is_active
    RETURNING u.id INTO v_user;

    IF v_user IS NULL THEN
        RETURN;
    END IF;

    INSERT INTO public.user_sessions AS s
           (id, user_id, token_hash, expires_at, last_seen_at, created_at, revoked_at)
    VALUES (gen_random_uuid(), v_user, v_hash, now() + interval '7 days',
            now(), now(), NULL)
    RETURNING s.id, s.expires_at INTO v_session, v_expires;

    INSERT INTO public.audit_log (actor, action, subject, details)
    VALUES ('user:' || v_user::text, 'auth.login', 'user:' || v_user::text,
            jsonb_build_object('session_id', v_session::text));

    session_id := v_session;
    expires_at := v_expires;

    RETURN NEXT;
END
$session_create$;

-- The live session behind a token, or no row. The same comparisons as
-- ai_smm.application.auth.resolve_session -- not revoked, expires_at in
-- the future, last_seen_at within the 24-hour idle window, active user --
-- plus the plausibility bounds of ADR §12 (RS-3): no timestamp more than
-- 60 s in the future, last_seen_at not before created_at, a lifetime of
-- at most 7 days. The touch of last_seen_at is throttled to once per
-- 60 s and never waits: a row another transaction has locked is not
-- touched (ADR §7.4).
CREATE OR REPLACE FUNCTION smm_auth.session_resolve(p_token text)
RETURNS TABLE (session_id uuid, user_id uuid, expires_at timestamptz)
LANGUAGE plpgsql
VOLATILE STRICT
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $session_resolve$
DECLARE
    v_hash text := smm_auth.token_digest(p_token);
    v_session uuid;
    v_user uuid;
    v_expires timestamptz;
BEGIN
    SELECT s.id, s.user_id, s.expires_at
      INTO v_session, v_user, v_expires
      FROM public.user_sessions AS s
      JOIN public.users AS u ON u.id = s.user_id
     WHERE s.token_hash = v_hash
       AND s.revoked_at IS NULL
       AND s.expires_at > now()
       AND s.last_seen_at + interval '24 hours' > now()
       AND u.is_active
       AND s.created_at <= now() + interval '60 seconds'
       AND s.last_seen_at <= now() + interval '60 seconds'
       AND s.last_seen_at >= s.created_at - interval '60 seconds'
       AND s.expires_at - s.created_at <= interval '7 days 60 seconds';

    IF v_session IS NULL THEN
        RETURN;
    END IF;

    PERFORM 1
       FROM public.user_sessions AS s
      WHERE s.id = v_session
        AND s.last_seen_at < now() - interval '60 seconds'
        FOR UPDATE SKIP LOCKED;

    IF FOUND THEN
        UPDATE public.user_sessions AS s
           SET last_seen_at = now()
         WHERE s.id = v_session;
    END IF;

    session_id := v_session;
    user_id := v_user;
    expires_at := v_expires;

    RETURN NEXT;
END
$session_resolve$;

-- Log out: revoke the live session behind a token. Only a session that
-- session_resolve would accept is revoked; anything else gets no row and
-- nothing is written (the API answers 401, as today). The row is locked,
-- so of two concurrent logouts exactly one revokes and writes auth.logout;
-- the other gets no row.
CREATE OR REPLACE FUNCTION smm_auth.session_revoke(p_token text)
RETURNS TABLE (session_id uuid, user_id uuid, revoked boolean)
LANGUAGE plpgsql
VOLATILE STRICT
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $session_revoke$
DECLARE
    v_hash text := smm_auth.token_digest(p_token);
    v_session uuid;
    v_user uuid;
BEGIN
    SELECT s.id, s.user_id
      INTO v_session, v_user
      FROM public.user_sessions AS s
      JOIN public.users AS u ON u.id = s.user_id
     WHERE s.token_hash = v_hash
       AND s.revoked_at IS NULL
       AND s.expires_at > now()
       AND s.last_seen_at + interval '24 hours' > now()
       AND u.is_active
       AND s.created_at <= now() + interval '60 seconds'
       AND s.last_seen_at <= now() + interval '60 seconds'
       AND s.last_seen_at >= s.created_at - interval '60 seconds'
       AND s.expires_at - s.created_at <= interval '7 days 60 seconds'
       FOR UPDATE OF s;

    IF v_session IS NULL THEN
        RETURN;
    END IF;

    UPDATE public.user_sessions AS s
       SET revoked_at = now()
     WHERE s.id = v_session;

    INSERT INTO public.audit_log (actor, action, subject, details)
    VALUES ('user:' || v_user::text, 'auth.logout', 'user:' || v_user::text,
            jsonb_build_object('session_id', v_session::text));

    session_id := v_session;
    user_id := v_user;
    revoked := true;

    RETURN NEXT;
END
$session_revoke$;

-- Record a failed login in the API's exact shape (actor anonymous,
-- auth.login_failed, subject 'email:<address>', details {}) and return
-- the failures for that address in the last 15 minutes, this one
-- included: the input of smm-auth's rate limit (ADR §11.3 / T11).
CREATE OR REPLACE FUNCTION smm_auth.record_login_failure(p_email text)
RETURNS integer
LANGUAGE plpgsql
VOLATILE STRICT
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $record_login_failure$
DECLARE
    v_subject text := smm_auth.failure_subject(p_email);
    v_count integer;
BEGIN
    INSERT INTO public.audit_log (actor, action, subject, details)
    VALUES ('anonymous', 'auth.login_failed', v_subject, '{}'::jsonb);

    SELECT count(*) INTO v_count
      FROM public.audit_log AS a
     WHERE a.action = 'auth.login_failed'
       AND a.subject = v_subject
       AND a.at > now() - interval '15 minutes';

    RETURN v_count;
END
$record_login_failure$;

-- The failures for an address in the last 15 minutes, without recording
-- one: checked before a password is verified.
CREATE OR REPLACE FUNCTION smm_auth.recent_login_failures(p_email text)
RETURNS integer
LANGUAGE plpgsql
STABLE STRICT
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $recent_login_failures$
DECLARE
    v_subject text := smm_auth.failure_subject(p_email);
    v_count integer;
BEGIN
    SELECT count(*) INTO v_count
      FROM public.audit_log AS a
     WHERE a.action = 'auth.login_failed'
       AND a.subject = v_subject
       AND a.at > now() - interval '15 minutes';

    RETURN v_count;
END
$recent_login_failures$;

COMMENT ON FUNCTION smm_auth.token_digest(text) IS
    'Internal: hex(sha256) of a well-formed session token. Owner only.';
COMMENT ON FUNCTION smm_auth.failure_subject(text) IS
    'Internal: audit subject of a failed login. Owner only.';
COMMENT ON FUNCTION smm_auth.session_create(uuid, text) IS
    'Open a session with server timestamps and write auth.login. EXECUTE: ai_smm_auth.';
COMMENT ON FUNCTION smm_auth.session_resolve(text) IS
    'The live session behind a token; throttled, non-blocking touch. EXECUTE: ai_smm_auth.';
COMMENT ON FUNCTION smm_auth.session_revoke(text) IS
    'Revoke the live session behind a token and write auth.logout once. EXECUTE: ai_smm_auth.';
COMMENT ON FUNCTION smm_auth.record_login_failure(text) IS
    'Write auth.login_failed and return the recent failures for the address. EXECUTE: ai_smm_auth.';
COMMENT ON FUNCTION smm_auth.recent_login_failures(text) IS
    'Recent failed logins for an address. EXECUTE: ai_smm_auth.';

-- Owner, and nothing for PUBLIC. A function created by this (superuser)
-- session is owned by it until reassigned here; PostgreSQL's default
-- EXECUTE for PUBLIC is taken away in the same transaction, before
-- anyone could use it.
DO $owner$
DECLARE
    owner_role name := pg_get_userbyid(
        (SELECT relowner FROM pg_class WHERE oid = 'public.users'::regclass));
    fn regprocedure;
BEGIN
    EXECUTE format('ALTER SCHEMA smm_auth OWNER TO %I', owner_role);
    EXECUTE 'REVOKE ALL ON SCHEMA smm_auth FROM PUBLIC';

    FOR fn IN
        SELECT p.oid::regprocedure
          FROM pg_proc p
         WHERE p.pronamespace = 'smm_auth'::regnamespace
    LOOP
        EXECUTE format('ALTER FUNCTION %s OWNER TO %I', fn, owner_role);
        EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC', fn);
    END LOOP;
END
$owner$;

-- Fail closed on what this file cannot see coming: default privileges or
-- earlier grants that hand the schema or a helper to someone else. The
-- only grantee besides the owner may be ai_smm_auth, and only on the five
-- contracts (a re-run after ai_smm_auth.grants.sql).
DO $postcheck$
DECLARE
    owner_oid oid := (SELECT relowner FROM pg_class WHERE oid = 'public.users'::regclass);
    -- 0 is PUBLIC's grantee oid, so an absent role can never match a grant.
    auth_oid oid := coalesce(to_regrole('ai_smm_auth')::oid, 0);
    bad text[];
BEGIN
    SELECT array_agg(format('%s %s -> %s', g.privilege, g.obj,
                            CASE g.grantee WHEN 0 THEN 'PUBLIC'
                                 ELSE pg_get_userbyid(g.grantee) END))
      INTO bad
      FROM (
          SELECT 'schema smm_auth' AS obj, false AS contract,
                 a.grantee, a.privilege_type AS privilege
            FROM pg_namespace n, aclexplode(n.nspacl) a
           WHERE n.nspname = 'smm_auth'
          UNION ALL
          SELECT p.oid::regprocedure::text,
                 p.proname NOT IN ('token_digest', 'failure_subject'),
                 a.grantee, a.privilege_type
            FROM pg_proc p, aclexplode(p.proacl) a
           WHERE p.pronamespace = 'smm_auth'::regnamespace
      ) AS g
     WHERE g.grantee <> owner_oid
       AND NOT (auth_oid <> 0 AND g.grantee = auth_oid AND (
                    (g.obj = 'schema smm_auth' AND g.privilege = 'USAGE')
                 OR (g.contract AND g.privilege = 'EXECUTE')));

    IF bad IS NOT NULL THEN
        RAISE EXCEPTION 'smm_auth: unexpected grants: %', bad;
    END IF;

    IF EXISTS (
        SELECT 1 FROM pg_proc p
         WHERE p.pronamespace = 'smm_auth'::regnamespace
           AND (p.proacl IS NULL
                OR has_function_privilege('public', p.oid, 'EXECUTE'))
    ) THEN
        RAISE EXCEPTION 'smm_auth: PUBLIC can still execute a function';
    END IF;

    -- The definer functions call the helpers as the owner: the owner must
    -- be able to use the schema and execute all seven functions.
    IF NOT has_schema_privilege(owner_oid, 'smm_auth', 'USAGE')
       OR EXISTS (
           SELECT 1 FROM pg_proc p
            WHERE p.pronamespace = 'smm_auth'::regnamespace
              AND (p.proowner <> owner_oid
                   OR NOT has_function_privilege(owner_oid, p.oid, 'EXECUTE'))
       )
       OR (SELECT count(*) FROM pg_proc p
            WHERE p.pronamespace = 'smm_auth'::regnamespace) <> 7
    THEN
        RAISE EXCEPTION 'smm_auth: the owner cannot use the schema or execute every function in it';
    END IF;
END
$postcheck$;

COMMIT;
