-- ai_smm_auth -- retire the smm-auth service's role (SMM-024C-3B-1
-- rollback; docs/adr/ADR-024C-3A-auth-service-isolation.md §14.4, P3/P4).
--
-- Run only AFTER smm-auth no longer runs (the scoped P5 rollback is
-- complete), as the instance's superuser, connected to the application
-- database:
--
--   psql -X -v ON_ERROR_STOP=1 -d ai_smm -f ai_smm_auth.revoke.sql
--
-- Then, if wanted, smm_auth.drop.sql removes the functions.
--
-- Refuses if the role owns anything, is a member of or has members in
-- another role, or still has open sessions. It touches no other role: not
-- ai_smm_cc, the owner, the worker or PUBLIC.

BEGIN;

SET LOCAL lock_timeout = '5s';

DO $retire$
DECLARE
    role_oid oid;
    owned bigint;
    memberships bigint;
BEGIN
    SELECT oid INTO role_oid FROM pg_roles WHERE rolname = 'ai_smm_auth';

    IF role_oid IS NULL THEN
        RAISE NOTICE 'ai_smm_auth does not exist; nothing to do';
        RETURN;
    END IF;

    SELECT (SELECT count(*) FROM pg_class WHERE relowner = role_oid)
         + (SELECT count(*) FROM pg_namespace WHERE nspowner = role_oid)
         + (SELECT count(*) FROM pg_proc WHERE proowner = role_oid)
         + (SELECT count(*) FROM pg_type WHERE typowner = role_oid)
         + (SELECT count(*) FROM pg_database WHERE datdba = role_oid)
      INTO owned;

    IF owned <> 0 THEN
        RAISE EXCEPTION 'ai_smm_auth owns % objects; refusing to drop it', owned;
    END IF;

    SELECT count(*) INTO memberships
      FROM pg_auth_members
     WHERE member = role_oid OR roleid = role_oid;

    IF memberships <> 0 THEN
        RAISE EXCEPTION 'ai_smm_auth has % role memberships; refusing to drop it', memberships;
    END IF;

    -- Sessions are never terminated here: one that remains means smm-auth
    -- (or something else) still uses the role.
    IF EXISTS (SELECT 1 FROM pg_stat_activity WHERE usesysid = role_oid) THEN
        RAISE EXCEPTION 'ai_smm_auth still has open sessions; stop smm-auth first';
    END IF;

    ALTER ROLE ai_smm_auth NOLOGIN;

    -- Every privilege it holds in this database and on the database
    -- itself; it owns nothing, so this drops no object.
    DROP OWNED BY ai_smm_auth;
    DROP ROLE ai_smm_auth;
END
$retire$;

COMMIT;
