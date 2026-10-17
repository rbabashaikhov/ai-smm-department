BEGIN;
SET LOCAL lock_timeout = '5s';
DO $retire$
DECLARE role_oid oid; owned bigint; memberships bigint;
BEGIN
    SELECT oid INTO role_oid FROM pg_roles WHERE rolname = 'ai_smm_generation';
    IF role_oid IS NULL THEN RETURN; END IF;
    SELECT (SELECT count(*) FROM pg_class WHERE relowner = role_oid)
         + (SELECT count(*) FROM pg_namespace WHERE nspowner = role_oid)
         + (SELECT count(*) FROM pg_proc WHERE proowner = role_oid)
         + (SELECT count(*) FROM pg_type WHERE typowner = role_oid)
      INTO owned;
    SELECT count(*) INTO memberships FROM pg_auth_members
     WHERE member = role_oid OR roleid = role_oid;
    IF owned <> 0 OR memberships <> 0 THEN
        RAISE EXCEPTION 'ai_smm_generation owns objects or has memberships; refusing';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_stat_activity WHERE usesysid = role_oid) THEN
        RAISE EXCEPTION 'ai_smm_generation still has open sessions';
    END IF;
    ALTER ROLE ai_smm_generation NOLOGIN;
    DROP OWNED BY ai_smm_generation;
    DROP ROLE ai_smm_generation;
END
$retire$;
COMMIT;
