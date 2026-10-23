-- Dedicated least-privilege login for the topic ideation worker (SMM-035B3B1).
-- Run against the application database as a role with CREATEROLE after the
-- 1c9e4b7d2a05 migration -- and again after any migration that recreates a
-- topic_ideation_* contract, which drops its EXECUTE grant. No password is
-- stored here; set it interactively.
BEGIN;
SET LOCAL lock_timeout = '5s';

DO $preflight$
DECLARE
    required text[] := ARRAY[
        'public.topic_ideation_claim(text,integer)',
        'public.topic_ideation_complete(uuid,text,jsonb,jsonb)',
        'public.topic_ideation_fail(uuid,text,text,boolean,text,integer,integer)'
    ];
    missing text[];
    owners text[];
    owner_is_super boolean;
    existing record;
BEGIN
    SELECT array_agg(f) INTO missing FROM unnest(required) AS f
     WHERE to_regprocedure(f) IS NULL;
    IF missing IS NOT NULL THEN
        RAISE EXCEPTION 'ai_smm_ideation: contract functions missing: %', missing;
    END IF;

    -- The claim must be the one that returns params (1c9e4b7d2a05).
    IF NOT EXISTS (
        SELECT 1 FROM pg_proc p
         WHERE p.oid = to_regprocedure('public.topic_ideation_claim(text,integer)')
           AND 'params' = ANY (p.proargnames)
    ) THEN
        RAISE EXCEPTION 'ai_smm_ideation: topic_ideation_claim predates 1c9e4b7d2a05';
    END IF;

    SELECT array_agg(DISTINCT pg_get_userbyid(p.proowner)) INTO owners
      FROM pg_proc p WHERE p.oid = ANY (
          SELECT to_regprocedure(f) FROM unnest(required) AS f
      );
    IF array_length(owners, 1) <> 1 OR owners[1] = 'ai_smm_ideation' THEN
        RAISE EXCEPTION 'ai_smm_ideation: contracts need one independent owner: %', owners;
    END IF;
    SELECT rolsuper INTO owner_is_super FROM pg_roles WHERE rolname = owners[1];
    IF owner_is_super THEN
        RAISE EXCEPTION 'ai_smm_ideation: contract owner % is superuser; refusing', owners[1];
    END IF;

    SELECT r.rolsuper, r.rolinherit, r.rolcreaterole, r.rolcreatedb,
           r.rolreplication, r.rolbypassrls, r.rolcanlogin, r.rolconnlimit,
           -- Both directions: roles it belongs to (incoming), and roles that
           -- belong to it (outgoing -- they would inherit its EXECUTE or could
           -- SET ROLE to it). Never revoked here: refuse and let the operator decide.
           (SELECT count(*) FROM pg_auth_members m
             WHERE m.member = r.oid OR m.roleid = r.oid) memberships
      INTO existing FROM pg_roles r WHERE r.rolname = 'ai_smm_ideation';
    IF FOUND THEN
        IF existing.rolsuper OR existing.rolinherit OR existing.rolcreaterole
           OR existing.rolcreatedb OR existing.rolreplication
           OR existing.rolbypassrls OR NOT existing.rolcanlogin
           OR existing.rolconnlimit <> 5 OR existing.memberships <> 0 THEN
            RAISE EXCEPTION 'ai_smm_ideation exists with unsafe attributes or role memberships (either direction): %', existing;
        END IF;
    ELSE
        CREATE ROLE ai_smm_ideation WITH LOGIN NOSUPERUSER NOINHERIT
            NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
            CONNECTION LIMIT 5;
    END IF;
END
$preflight$;

COMMENT ON ROLE ai_smm_ideation IS
    'Topic ideation worker; EXECUTE on three ideation contracts, no table access.';
ALTER ROLE ai_smm_ideation SET statement_timeout = '30s';
ALTER ROLE ai_smm_ideation SET lock_timeout = '5s';
ALTER ROLE ai_smm_ideation SET idle_in_transaction_session_timeout = '60s';

DO $database$
BEGIN
    EXECUTE format('REVOKE ALL ON DATABASE %I FROM ai_smm_ideation', current_database());
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO ai_smm_ideation', current_database());
END
$database$;

REVOKE ALL ON SCHEMA public FROM ai_smm_ideation;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM ai_smm_ideation;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM ai_smm_ideation;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM ai_smm_ideation;
GRANT USAGE ON SCHEMA public TO ai_smm_ideation;
GRANT EXECUTE ON FUNCTION
    public.topic_ideation_claim(text, integer),
    public.topic_ideation_complete(uuid, text, jsonb, jsonb),
    public.topic_ideation_fail(uuid, text, text, boolean, text, integer, integer)
TO ai_smm_ideation;

DO $postcheck$
BEGIN
    IF has_schema_privilege('ai_smm_ideation', 'public', 'CREATE')
       OR has_database_privilege('ai_smm_ideation', current_database(), 'CREATE') THEN
        RAISE EXCEPTION 'ai_smm_ideation has CREATE through PUBLIC; refusing';
    END IF;
END
$postcheck$;
COMMIT;
