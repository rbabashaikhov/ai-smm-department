-- Privileges of every role EXCEPT ai_smm_auth -- ai_smm_cc (its G1
-- grants), the owner (worker, migrations, CLI), PUBLIC, and everyone else
-- on the instance -- on the application database, on every schema but
-- smm_auth, and on every relation, column and function in them; plus
-- every role's attributes and settings, all memberships and all default
-- privileges. Read-only. (SMM-024C-3B-1.)
--
-- Taken before smm_auth.functions.sql + ai_smm_auth.grants.sql and after
-- them, and again after ai_smm_auth.revoke.sql + smm_auth.drop.sql: the
-- outputs must be identical. Schema smm_auth itself is left out -- it does
-- not exist before -- and ai_smm_auth.verify.sql checks it object by
-- object. Run as the instance's superuser, connected to the application
-- database:
--
--   PGOPTIONS='-c default_transaction_read_only=on' \
--     psql -X -v ON_ERROR_STOP=1 -d ai_smm -tA -f ai_smm_auth.acl-snapshot.sql > before.txt
--
-- Unlike acl-snapshot.sql (which leaves ai_smm_cc out, for that role's
-- own rollout), this one INCLUDES ai_smm_cc: it is the evidence that the
-- smm-auth rollout leaves G1 exactly as it was (ADR amendment A1). Raw
-- ACL entries are listed, so a column grant shadowed by a table grant
-- shows up too.

WITH schemas AS (
    SELECT n.oid, n.nspname, n.nspacl, n.nspowner
      FROM pg_namespace n
     WHERE n.nspname NOT IN ('smm_auth', 'pg_catalog', 'information_schema', 'pg_toast')
       AND n.nspname NOT LIKE 'pg_temp_%' AND n.nspname NOT LIKE 'pg_toast_temp_%'
)
SELECT concat_ws('|', kind, object, grantee, privilege, grantable)
  FROM (
    SELECT 'database', d.datname,
           CASE a.grantee WHEN 0 THEN 'PUBLIC' ELSE a.grantee::regrole::text END,
           a.privilege_type, a.is_grantable
      FROM pg_database d, aclexplode(coalesce(d.datacl, acldefault('d', d.datdba))) a
     WHERE d.datname = current_database()
       AND a.grantee IS DISTINCT FROM to_regrole('ai_smm_auth')::oid
    UNION ALL
    SELECT 'schema', s.nspname,
           CASE a.grantee WHEN 0 THEN 'PUBLIC' ELSE a.grantee::regrole::text END,
           a.privilege_type, a.is_grantable
      FROM schemas s, aclexplode(coalesce(s.nspacl, acldefault('n', s.nspowner))) a
     WHERE a.grantee IS DISTINCT FROM to_regrole('ai_smm_auth')::oid
    UNION ALL
    SELECT 'relation', c.oid::regclass::text,
           CASE a.grantee WHEN 0 THEN 'PUBLIC' ELSE a.grantee::regrole::text END,
           a.privilege_type, a.is_grantable
      FROM pg_class c
      JOIN schemas s ON s.oid = c.relnamespace,
           aclexplode(coalesce(c.relacl, acldefault(
               CASE c.relkind WHEN 'S' THEN 's' ELSE 'r' END::"char", c.relowner))) a
     WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')
       AND a.grantee IS DISTINCT FROM to_regrole('ai_smm_auth')::oid
    UNION ALL
    SELECT 'column', c.oid::regclass::text || '.' || att.attname,
           CASE a.grantee WHEN 0 THEN 'PUBLIC' ELSE a.grantee::regrole::text END,
           a.privilege_type, a.is_grantable
      FROM pg_class c
      JOIN schemas s ON s.oid = c.relnamespace
      JOIN pg_attribute att ON att.attrelid = c.oid AND att.attacl IS NOT NULL,
           aclexplode(att.attacl) a
     WHERE a.grantee IS DISTINCT FROM to_regrole('ai_smm_auth')::oid
    UNION ALL
    SELECT 'function', p.oid::regprocedure::text,
           CASE a.grantee WHEN 0 THEN 'PUBLIC' ELSE a.grantee::regrole::text END,
           a.privilege_type, a.is_grantable
      FROM pg_proc p
      JOIN schemas s ON s.oid = p.pronamespace,
           aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) a
     WHERE a.grantee IS DISTINCT FROM to_regrole('ai_smm_auth')::oid
    UNION ALL
    SELECT 'owner', c.oid::regclass::text, pg_get_userbyid(c.relowner), '', false
      FROM pg_class c JOIN schemas s ON s.oid = c.relnamespace
     WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')
    UNION ALL
    SELECT 'role', r.rolname,
           concat_ws(',', r.rolsuper, r.rolinherit, r.rolcreaterole,
                     r.rolcreatedb, r.rolcanlogin, r.rolreplication,
                     r.rolbypassrls, r.rolconnlimit),
           coalesce(array_to_string(s.setconfig, ' '), ''), false
      FROM pg_roles r
      LEFT JOIN pg_db_role_setting s ON s.setrole = r.oid AND s.setdatabase = 0
     WHERE r.rolname <> 'ai_smm_auth'
    UNION ALL
    SELECT 'membership', pg_get_userbyid(m.roleid), pg_get_userbyid(m.member), '', false
      FROM pg_auth_members m
    UNION ALL
    SELECT 'default_acl', d.defaclrole::regrole::text, d.defaclobjtype::text,
           coalesce(d.defaclacl::text, ''), false
      FROM pg_default_acl d
  ) AS snapshot(kind, object, grantee, privilege, grantable)
 ORDER BY 1;
