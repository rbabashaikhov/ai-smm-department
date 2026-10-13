-- smm_auth -- remove the authentication primitives (SMM-024C-3B-1
-- rollback of smm_auth.functions.sql; docs/adr/ADR-024C-3A-auth-service-
-- isolation.md §14.4, P3).
--
-- Run only AFTER ai_smm_auth.revoke.sql, as the instance's superuser,
-- connected to the application database:
--
--   psql -X -v ON_ERROR_STOP=1 -d ai_smm -f smm_auth.drop.sql
--
-- Refuses while ai_smm_auth exists, or while any role other than the
-- owner holds a privilege on the schema or a function in it. Drops exactly
-- the seven functions smm_auth.functions.sql makes, then the schema
-- without CASCADE: an object this file does not know about makes the drop
-- fail instead of disappearing with it. Touches no table and no role.

BEGIN;

SET LOCAL lock_timeout = '5s';

DO $drop$
DECLARE
    owner_oid oid;
    grantees text[];
    fn text;
BEGIN
    IF to_regnamespace('smm_auth') IS NULL THEN
        RAISE NOTICE 'schema smm_auth does not exist; nothing to do';
        RETURN;
    END IF;

    IF to_regrole('ai_smm_auth') IS NOT NULL THEN
        RAISE EXCEPTION 'ai_smm_auth still exists; run ai_smm_auth.revoke.sql first';
    END IF;

    SELECT nspowner INTO owner_oid FROM pg_namespace WHERE nspname = 'smm_auth';

    SELECT array_agg(DISTINCT CASE g.grantee WHEN 0 THEN 'PUBLIC'
                                   ELSE pg_get_userbyid(g.grantee) END)
      INTO grantees
      FROM (
          SELECT a.grantee
            FROM pg_namespace n, aclexplode(n.nspacl) a
           WHERE n.nspname = 'smm_auth'
          UNION ALL
          SELECT a.grantee
            FROM pg_proc p, aclexplode(p.proacl) a
           WHERE p.pronamespace = 'smm_auth'::regnamespace
      ) AS g
     WHERE g.grantee <> owner_oid;

    IF grantees IS NOT NULL THEN
        RAISE EXCEPTION 'smm_auth: privileges are still held by %; refusing to drop', grantees;
    END IF;

    FOREACH fn IN ARRAY ARRAY[
        'smm_auth.session_create(uuid, text)',
        'smm_auth.session_resolve(text)',
        'smm_auth.session_revoke(text)',
        'smm_auth.record_login_failure(text)',
        'smm_auth.recent_login_failures(text)',
        'smm_auth.failure_subject(text)',
        'smm_auth.token_digest(text)']
    LOOP
        EXECUTE 'DROP FUNCTION IF EXISTS ' || fn;
    END LOOP;

    DROP SCHEMA smm_auth;
END
$drop$;

COMMIT;
