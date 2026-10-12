-- Privileges of every role EXCEPT ai_smm_cc -- the owner (worker,
-- migrations, CLI), PUBLIC, and everyone else on the instance -- on the
-- application database, schema public and every relation and column in
-- it; plus every role's attributes and settings, all memberships and all
-- default privileges. Read-only.
--
-- Taken before and after ai_smm_cc.grants.sql (and ai_smm_cc.revoke.sql):
-- the two outputs must be identical. Run as the instance's superuser,
-- connected to the application database:
--
--   PGOPTIONS='-c default_transaction_read_only=on' \
--     psql -X -v ON_ERROR_STOP=1 -d ai_smm -tA -f acl-snapshot.sql > before.txt
--
-- tests/test_db_least_privilege.py runs this file around provisioning.

SELECT concat_ws('|', kind, object, grantee, privilege, grantable)
  FROM (
    SELECT 'database', d.datname,
           CASE a.grantee WHEN 0 THEN 'PUBLIC' ELSE a.grantee::regrole::text END,
           a.privilege_type, a.is_grantable
      FROM pg_database d, aclexplode(coalesce(d.datacl, acldefault('d', d.datdba))) a
     WHERE d.datname = current_database()
       AND a.grantee IS DISTINCT FROM to_regrole('ai_smm_cc')::oid
    UNION ALL
    SELECT 'schema', n.nspname,
           CASE a.grantee WHEN 0 THEN 'PUBLIC' ELSE a.grantee::regrole::text END,
           a.privilege_type, a.is_grantable
      FROM pg_namespace n, aclexplode(coalesce(n.nspacl, acldefault('n', n.nspowner))) a
     WHERE n.nspname = 'public'
       AND a.grantee IS DISTINCT FROM to_regrole('ai_smm_cc')::oid
    UNION ALL
    SELECT 'relation', c.relname,
           CASE a.grantee WHEN 0 THEN 'PUBLIC' ELSE a.grantee::regrole::text END,
           a.privilege_type, a.is_grantable
      FROM pg_class c,
           aclexplode(coalesce(c.relacl, acldefault(
               CASE c.relkind WHEN 'S' THEN 's' ELSE 'r' END::"char", c.relowner))) a
     WHERE c.relnamespace = 'public'::regnamespace
       AND a.grantee IS DISTINCT FROM to_regrole('ai_smm_cc')::oid
    UNION ALL
    SELECT 'column', c.relname || '.' || att.attname,
           CASE a.grantee WHEN 0 THEN 'PUBLIC' ELSE a.grantee::regrole::text END,
           a.privilege_type, a.is_grantable
      FROM pg_class c
      JOIN pg_attribute att ON att.attrelid = c.oid AND att.attacl IS NOT NULL,
           aclexplode(att.attacl) a
     WHERE c.relnamespace = 'public'::regnamespace
       AND a.grantee IS DISTINCT FROM to_regrole('ai_smm_cc')::oid
    UNION ALL
    SELECT 'owner', c.relname, pg_get_userbyid(c.relowner), '', false
      FROM pg_class c WHERE c.relnamespace = 'public'::regnamespace
    UNION ALL
    SELECT 'role', r.rolname,
           concat_ws(',', r.rolsuper, r.rolinherit, r.rolcreaterole,
                     r.rolcreatedb, r.rolcanlogin, r.rolreplication,
                     r.rolbypassrls, r.rolconnlimit),
           coalesce(array_to_string(s.setconfig, ' '), ''), false
      FROM pg_roles r
      LEFT JOIN pg_db_role_setting s ON s.setrole = r.oid AND s.setdatabase = 0
     WHERE r.rolname <> 'ai_smm_cc'
    UNION ALL
    SELECT 'membership', pg_get_userbyid(m.roleid), pg_get_userbyid(m.member), '', false
      FROM pg_auth_members m
    UNION ALL
    SELECT 'default_acl', d.defaclrole::regrole::text, d.defaclobjtype::text,
           coalesce(d.defaclacl::text, ''), false
      FROM pg_default_acl d
  ) AS snapshot(kind, object, grantee, privilege, grantable)
 ORDER BY 1;
