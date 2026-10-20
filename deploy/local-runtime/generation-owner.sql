-- LOCAL disposable runtime database ONLY (compose.runtime.yml).
--
-- Production's schema owner is a non-superuser login; the local runtime's
-- bootstrap user ai_smm_local is a superuser, so the generation contracts
-- it migrated are superuser-owned and ai_smm_generation.grants.sql rightly
-- refuses to grant them. This gives the local schema the production shape:
-- one non-superuser, NOLOGIN owner of the public tables, sequences and the
-- generation contracts. The superuser keeps full access (API, migrate,
-- cli are unchanged); CREATE OR REPLACE in a later migration keeps the
-- owner. Idempotent; re-run after a migration that adds a table.
BEGIN;
SET LOCAL lock_timeout = '5s';

DO $local_owner$
DECLARE
    obj record;
BEGIN
    IF current_database() <> 'ai_smm_runtime_local' THEN
        RAISE EXCEPTION 'generation-owner.sql is for ai_smm_runtime_local only, not %',
            current_database();
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ai_smm_local_owner') THEN
        CREATE ROLE ai_smm_local_owner NOLOGIN NOSUPERUSER NOINHERIT
            NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
    END IF;

    FOR obj IN
        SELECT c.oid::regclass AS name, c.relkind
          FROM pg_class AS c
         WHERE c.relnamespace = 'public'::regnamespace
           AND c.relkind IN ('r', 'p', 'v', 'm', 'S')
           AND c.relowner <> 'ai_smm_local_owner'::regrole
           -- an owned sequence follows its table
           AND NOT EXISTS (
               SELECT 1 FROM pg_depend AS d
                WHERE d.objid = c.oid AND d.classid = 'pg_class'::regclass
                  AND d.deptype IN ('a', 'i') AND c.relkind = 'S'
           )
    LOOP
        EXECUTE format(
            'ALTER %s %s OWNER TO ai_smm_local_owner',
            CASE obj.relkind
                WHEN 'S' THEN 'SEQUENCE'
                WHEN 'v' THEN 'VIEW'
                WHEN 'm' THEN 'MATERIALIZED VIEW'
                ELSE 'TABLE'
            END,
            obj.name
        );
    END LOOP;

    FOR obj IN
        SELECT p.oid::regprocedure AS name
          FROM pg_proc AS p
         WHERE p.pronamespace = 'public'::regnamespace
           AND p.proname IN ('generation_claim', 'generation_complete', 'generation_fail')
           AND p.proowner <> 'ai_smm_local_owner'::regrole
    LOOP
        EXECUTE format('ALTER FUNCTION %s OWNER TO ai_smm_local_owner', obj.name);
    END LOOP;
END
$local_owner$;
COMMIT;
