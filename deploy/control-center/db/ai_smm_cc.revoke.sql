-- ai_smm_cc -- retire the Control Center API's role (SMM-024C-2 rollback,
-- stage 2). NOT part of the configuration rollback: switching smm-api
-- back to the previous credentials does not need this file, and the role
-- can stay disabled-but-present for as long as the owner wants.
--
-- Run only AFTER smm-api no longer uses the role (the configuration
-- rollback is complete), as the instance's superuser, connected to the
-- application database:
--
--   psql -X -v ON_ERROR_STOP=1 -d ai_smm -f ai_smm_cc.revoke.sql
--
-- Refuses if the role owns anything or is a member of, or has members in,
-- another role: then it is no longer only what ai_smm_cc.grants.sql made,
-- and dropping it is a decision for whoever changed it. It touches no
-- other role: not the owner, the worker or PUBLIC.

BEGIN;

SET LOCAL lock_timeout = '5s';

DO $retire$
DECLARE
    role_oid oid;
    owned bigint;
    memberships bigint;
BEGIN
    SELECT oid INTO role_oid FROM pg_roles WHERE rolname = 'ai_smm_cc';

    IF role_oid IS NULL THEN
        RAISE NOTICE 'ai_smm_cc does not exist; nothing to do';
        RETURN;
    END IF;

    SELECT (SELECT count(*) FROM pg_class WHERE relowner = role_oid)
         + (SELECT count(*) FROM pg_namespace WHERE nspowner = role_oid)
         + (SELECT count(*) FROM pg_proc WHERE proowner = role_oid)
         + (SELECT count(*) FROM pg_type WHERE typowner = role_oid)
         + (SELECT count(*) FROM pg_database WHERE datdba = role_oid)
      INTO owned;

    IF owned <> 0 THEN
        RAISE EXCEPTION 'ai_smm_cc owns % objects; refusing to drop it', owned;
    END IF;

    SELECT count(*) INTO memberships
      FROM pg_auth_members
     WHERE member = role_oid OR roleid = role_oid;

    IF memberships <> 0 THEN
        RAISE EXCEPTION 'ai_smm_cc has % role memberships; refusing to drop it', memberships;
    END IF;

    -- Sessions are never terminated here: after the configuration rollback
    -- there should be none, and one that remains means something still
    -- uses the role. PostgreSQL itself would drop the role under it.
    IF EXISTS (SELECT 1 FROM pg_stat_activity WHERE usesysid = role_oid) THEN
        RAISE EXCEPTION 'ai_smm_cc still has open sessions; finish the configuration rollback first';
    END IF;

    ALTER ROLE ai_smm_cc NOLOGIN;

    -- Every privilege it holds in this database and on the database
    -- itself; it owns nothing, so this drops no object.
    DROP OWNED BY ai_smm_cc;
    DROP ROLE ai_smm_cc;
END
$retire$;

COMMIT;
