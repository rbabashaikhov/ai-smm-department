-- Dedicated least-privilege login for the Agent -> Draft worker.
-- Run against the application database as a role with CREATEROLE after the
-- f7a91c2d5e04 migration. No password is stored here; set it interactively.
BEGIN;
SET LOCAL lock_timeout = '5s';

DO $preflight$
DECLARE
    required text[] := ARRAY[
        'public.generation_claim(text,integer)',
        'public.generation_complete(uuid,text,uuid,jsonb,jsonb)',
        'public.generation_fail(uuid,text,text,boolean,text,integer,integer)'
    ];
    missing text[];
    owners text[];
    owner_is_super boolean;
    existing record;
BEGIN
    SELECT array_agg(f) INTO missing FROM unnest(required) AS f
     WHERE to_regprocedure(f) IS NULL;
    IF missing IS NOT NULL THEN
        RAISE EXCEPTION 'ai_smm_generation: contract functions missing: %', missing;
    END IF;

    SELECT array_agg(DISTINCT pg_get_userbyid(p.proowner)) INTO owners
      FROM pg_proc p WHERE p.oid = ANY (
          SELECT to_regprocedure(f) FROM unnest(required) AS f
      );
    IF array_length(owners, 1) <> 1 OR owners[1] = 'ai_smm_generation' THEN
        RAISE EXCEPTION 'ai_smm_generation: contracts need one independent owner: %', owners;
    END IF;
    SELECT rolsuper INTO owner_is_super FROM pg_roles WHERE rolname = owners[1];
    IF owner_is_super THEN
        RAISE EXCEPTION 'ai_smm_generation: contract owner % is superuser; refusing', owners[1];
    END IF;

    SELECT r.rolsuper, r.rolinherit, r.rolcreaterole, r.rolcreatedb,
           r.rolreplication, r.rolbypassrls, r.rolcanlogin, r.rolconnlimit,
           (SELECT count(*) FROM pg_auth_members m WHERE m.member = r.oid) memberships
      INTO existing FROM pg_roles r WHERE r.rolname = 'ai_smm_generation';
    IF FOUND THEN
        IF existing.rolsuper OR existing.rolinherit OR existing.rolcreaterole
           OR existing.rolcreatedb OR existing.rolreplication
           OR existing.rolbypassrls OR NOT existing.rolcanlogin
           OR existing.rolconnlimit <> 5 OR existing.memberships <> 0 THEN
            RAISE EXCEPTION 'ai_smm_generation exists with unsafe attributes: %', existing;
        END IF;
    ELSE
        CREATE ROLE ai_smm_generation WITH LOGIN NOSUPERUSER NOINHERIT
            NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
            CONNECTION LIMIT 5;
    END IF;
END
$preflight$;

COMMENT ON ROLE ai_smm_generation IS
    'Agent-to-draft worker; EXECUTE-only DB contracts, no table access.';
ALTER ROLE ai_smm_generation SET statement_timeout = '30s';
ALTER ROLE ai_smm_generation SET lock_timeout = '5s';
ALTER ROLE ai_smm_generation SET idle_in_transaction_session_timeout = '60s';

DO $database$
BEGIN
    EXECUTE format('REVOKE ALL ON DATABASE %I FROM ai_smm_generation', current_database());
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO ai_smm_generation', current_database());
END
$database$;

REVOKE ALL ON SCHEMA public FROM ai_smm_generation;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM ai_smm_generation;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM ai_smm_generation;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM ai_smm_generation;
GRANT USAGE ON SCHEMA public TO ai_smm_generation;
GRANT EXECUTE ON FUNCTION
    public.generation_claim(text, integer),
    public.generation_complete(uuid, text, uuid, jsonb, jsonb),
    public.generation_fail(uuid, text, text, boolean, text, integer, integer)
TO ai_smm_generation;

DO $postcheck$
BEGIN
    IF has_schema_privilege('ai_smm_generation', 'public', 'CREATE')
       OR has_database_privilege('ai_smm_generation', current_database(), 'CREATE') THEN
        RAISE EXCEPTION 'ai_smm_generation has CREATE through PUBLIC; refusing';
    END IF;
END
$postcheck$;
COMMIT;
