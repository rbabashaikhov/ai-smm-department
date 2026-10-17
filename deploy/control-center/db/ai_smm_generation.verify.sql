-- Read-only verification. Output contains no FAIL line when the role is exact.
WITH role AS (
    SELECT * FROM pg_roles WHERE rolname = 'ai_smm_generation'
), checks(name, ok, detail) AS (
    SELECT 'role_attributes', count(*) = 1 AND bool_and(
        rolcanlogin AND NOT rolsuper AND NOT rolinherit AND NOT rolcreaterole
        AND NOT rolcreatedb AND NOT rolreplication AND NOT rolbypassrls
        AND rolconnlimit = 5), 'LOGIN NOINHERIT, non-privileged, limit 5'
      FROM role
    UNION ALL
    SELECT 'no_memberships', NOT EXISTS (
        SELECT 1 FROM pg_auth_members m JOIN role r ON m.member = r.oid
    ), 'worker inherits no role'
    UNION ALL
    SELECT 'no_table_privileges', NOT EXISTS (
        SELECT 1 FROM pg_class c, role r
         WHERE c.relnamespace = 'public'::regnamespace
           AND c.relkind IN ('r','p','v','m','S')
           AND (has_table_privilege(r.rolname, c.oid, 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                OR (c.relkind = 'S' AND has_sequence_privilege(r.rolname, c.oid, 'USAGE,SELECT,UPDATE')))
    ), 'no table/view/sequence access'
    UNION ALL
    SELECT 'contract_execute', count(*) = 3 AND bool_and(
        has_function_privilege('ai_smm_generation', p.oid, 'EXECUTE')
        AND p.prosecdef
        AND p.proconfig @> ARRAY['search_path=pg_catalog, pg_temp']
        AND NOT owner_role.rolsuper
        AND owner_role.rolname <> 'ai_smm_generation')
      , 'three SECURITY DEFINER contracts with fixed search_path'
      FROM pg_proc p
      JOIN pg_roles owner_role ON owner_role.oid = p.proowner
     WHERE p.oid IN (
        'public.generation_claim(text,integer,integer)'::regprocedure,
        'public.generation_complete(uuid,text,uuid,jsonb,jsonb)'::regprocedure,
        'public.generation_fail(uuid,text,text,boolean,integer,text,integer,integer)'::regprocedure
     )
    UNION ALL
    SELECT 'public_cannot_execute', NOT EXISTS (
        SELECT 1 FROM information_schema.routine_privileges rp
         WHERE rp.routine_schema = 'public' AND rp.grantee = 'PUBLIC'
           AND rp.routine_name IN ('generation_claim', 'generation_complete', 'generation_fail')
    ), 'contracts are not PUBLIC'
)
SELECT (CASE WHEN ok THEN 'PASS' ELSE 'FAIL' END) || '|' || name || '|' || detail
  FROM checks ORDER BY ok, name;
