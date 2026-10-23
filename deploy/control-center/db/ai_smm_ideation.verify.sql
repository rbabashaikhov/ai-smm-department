-- Read-only verification. Output contains no FAIL line when the role is exact.
WITH role AS (
    SELECT * FROM pg_roles WHERE rolname = 'ai_smm_ideation'
), contracts(oid) AS (
    SELECT to_regprocedure(f) FROM unnest(ARRAY[
        'public.topic_ideation_claim(text,integer)',
        'public.topic_ideation_complete(uuid,text,jsonb,jsonb)',
        'public.topic_ideation_fail(uuid,text,text,boolean,text,integer,integer)'
    ]) AS f
), checks(name, ok, detail) AS (
    SELECT 'role_attributes', count(*) = 1 AND bool_and(
        rolcanlogin AND NOT rolsuper AND NOT rolinherit AND NOT rolcreaterole
        AND NOT rolcreatedb AND NOT rolreplication AND NOT rolbypassrls
        AND rolconnlimit = 5), 'LOGIN NOINHERIT, non-privileged, limit 5'
      FROM role
    UNION ALL
    -- Any membership edge touching the role, in either direction, fails.
    -- Transitive reach needs a direct edge, so a direct check is complete;
    -- the recursive form below names the whole chain for the operator.
    SELECT 'no_memberships', NOT EXISTS (
        SELECT 1 FROM pg_auth_members m JOIN role r
            ON m.member = r.oid OR m.roleid = r.oid
    ), 'worker belongs to no role and no role belongs to it'
    UNION ALL
    SELECT 'no_transitive_membership', NOT EXISTS (
        WITH RECURSIVE up(oid) AS (
            SELECT m.roleid FROM pg_auth_members m JOIN role r ON m.member = r.oid
            UNION
            SELECT m.roleid FROM pg_auth_members m JOIN up ON m.member = up.oid
        ), down(oid) AS (
            SELECT m.member FROM pg_auth_members m JOIN role r ON m.roleid = r.oid
            UNION
            SELECT m.member FROM pg_auth_members m JOIN down ON m.roleid = down.oid
        )
        SELECT 1 FROM up UNION ALL SELECT 1 FROM down
    ), 'no role reaches the worker, and it reaches no role, through any chain'
    UNION ALL
    SELECT 'no_table_privileges', NOT EXISTS (
        SELECT 1 FROM pg_class c, role r
         WHERE c.relnamespace = 'public'::regnamespace
           AND c.relkind IN ('r','p','v','m','S')
           AND (has_table_privilege(r.rolname, c.oid, 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                OR has_any_column_privilege(r.rolname, c.oid, 'SELECT,INSERT,UPDATE,REFERENCES')
                OR (c.relkind = 'S' AND has_sequence_privilege(r.rolname, c.oid, 'USAGE,SELECT,UPDATE')))
    ), 'no table/column/view/sequence access'
    UNION ALL
    SELECT 'contract_execute', count(*) = 3 AND bool_and(
        has_function_privilege('ai_smm_ideation', p.oid, 'EXECUTE')
        AND p.prosecdef
        AND p.proconfig @> ARRAY['search_path=pg_catalog, pg_temp']
        AND NOT owner_role.rolsuper
        AND owner_role.rolname <> 'ai_smm_ideation')
      , 'three SECURITY DEFINER ideation contracts with fixed search_path'
      FROM pg_proc p
      JOIN pg_roles owner_role ON owner_role.oid = p.proowner
     WHERE p.oid IN (SELECT oid FROM contracts)
    UNION ALL
    SELECT 'claim_returns_params', EXISTS (
        SELECT 1 FROM pg_proc p
         WHERE p.oid = to_regprocedure('public.topic_ideation_claim(text,integer)')
           AND 'params' = ANY (p.proargnames)
    ), 'topic_ideation_claim is the 1c9e4b7d2a05 version'
    UNION ALL
    -- Trigger functions are left out: PostgreSQL never lets them be called
    -- directly, whoever holds EXECUTE.
    SELECT 'no_other_functions', NOT EXISTS (
        SELECT 1 FROM pg_proc p, role r
         WHERE p.pronamespace = 'public'::regnamespace
           AND p.prorettype <> 'trigger'::regtype
           AND p.oid NOT IN (SELECT oid FROM contracts WHERE oid IS NOT NULL)
           AND has_function_privilege(r.rolname, p.oid, 'EXECUTE')
    ), 'no generation, publication or other callable public function'
    UNION ALL
    SELECT 'generation_role_excluded', NOT EXISTS (
        SELECT 1 FROM pg_roles g, contracts c
         WHERE g.rolname = 'ai_smm_generation' AND c.oid IS NOT NULL
           AND has_function_privilege(g.oid, c.oid, 'EXECUTE')
    ), 'ai_smm_generation cannot call an ideation contract'
    UNION ALL
    SELECT 'public_cannot_execute', NOT EXISTS (
        SELECT 1 FROM information_schema.routine_privileges rp
         WHERE rp.routine_schema = 'public' AND rp.grantee = 'PUBLIC'
           AND rp.routine_name IN ('topic_ideation_claim', 'topic_ideation_complete',
                                   'topic_ideation_fail')
    ), 'contracts are not PUBLIC'
)
SELECT (CASE WHEN ok THEN 'PASS' ELSE 'FAIL' END) || '|' || name || '|' || detail
  FROM checks ORDER BY ok, name;
