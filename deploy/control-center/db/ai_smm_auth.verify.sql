-- ai_smm_auth -- read-only verification of the smm-auth service's role and
-- of the smm_auth functions (SMM-024C-3B-1). One SELECT; it changes nothing.
--
-- Run as the instance's superuser (it reads pg_authid), connected to the
-- application database, in a read-only transaction:
--
--   PGOPTIONS='-c default_transaction_read_only=on' \
--     psql -X -v ON_ERROR_STOP=1 -d ai_smm -tA -f ai_smm_auth.verify.sql
--
-- Output: one line per check, `STATUS|check|detail`, FAIL first.
--   PASS  as designed
--   FAIL  not as designed: do not point smm-auth at this role
--   INFO  a fact to record that this role cannot change (PUBLIC grants)
-- The last line is always `END|checks|<n>`; a run without it did not
-- complete.
--
-- EFFECTIVE privileges are checked (has_*_privilege: own grants, PUBLIC,
-- anything inherited) for EVERY relation, column and sequence in schema
-- public and EVERY executable routine -- function, procedure, aggregate,
-- window function -- in public and smm_auth -- not only the ones
-- ai_smm_auth.grants.sql names. Raw ACL entries are checked as well, so a
-- column grant hidden under another privilege is caught too. Each
-- smm_auth function is pinned: owner, SECURITY DEFINER or not,
-- search_path, volatility, and md5 of its body. Changing a body in
-- smm_auth.functions.sql means changing its pin here (a test keeps the two
-- in sync).
--
-- Also checked: PUBLIC and ai_smm_cc can neither use the schema nor
-- execute any function in it.

WITH
role AS (
    SELECT r.*
      FROM pg_roles r
     WHERE r.rolname = 'ai_smm_auth'
),
owner_role AS (
    SELECT c.relowner AS oid
      FROM pg_class c
     WHERE c.oid = to_regclass('public.users')
),
-- The designed column grants. Anything not listed here must be false.
column_grants(relname, attname, privilege) AS (
    VALUES
        ('users', 'id', 'SELECT'),
        ('users', 'email', 'SELECT'),
        ('users', 'password_hash', 'SELECT'),
        ('users', 'display_name', 'SELECT'),
        ('users', 'is_active', 'SELECT'),
        ('users', 'last_login_at', 'SELECT'),
        ('users', 'created_at', 'SELECT'),
        ('users', 'updated_at', 'SELECT')
),
-- Every function smm_auth.functions.sql makes, as it makes it.
designed_functions(signature, secdef, volatility, granted, body_md5) AS (
    VALUES
        ('smm_auth.token_digest(text)', false, 's', false,
         '3e937240919357b6caee8a90615bb636'),
        ('smm_auth.failure_subject(text)', false, 'i', false,
         'f9a352be93f716c6683e2599b75bbfd3'),
        ('smm_auth.session_create(uuid,text)', true, 'v', true,
         '98af1a2bad7aa04dca09300f7dfce9bd'),
        ('smm_auth.session_resolve(text)', true, 'v', true,
         '50e3853e6489b86521ea9d48461ba95b'),
        ('smm_auth.session_revoke(text)', true, 'v', true,
         '093ef68df9df9c2bf4d52961d06a2cd5'),
        ('smm_auth.record_login_failure(text)', true, 'v', true,
         '67605c5e640b83ce786068f54c6bc55a'),
        ('smm_auth.recent_login_failures(text)', true, 's', true,
         'd7ced661a6f4e7912831fb9598367e73'),
        -- SMM-025D: the step-up approval
        ('smm_auth.approval_preview(text,uuid)', true, 's', true,
         '0ea560184506b745d4e68d7a355f477c'),
        ('smm_auth.approval_grant_issue(text,uuid)', true, 'v', true,
         'c988d723ac4269856205f320b0fad238')
),
tables AS (
    SELECT c.oid, c.relname
      FROM pg_class c
     WHERE c.relnamespace = 'public'::regnamespace
       AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
),
-- Every executable routine in the application's schemas, of every kind
-- pg_proc holds: functions (f), procedures (p), aggregates (a) and window
-- functions (w). EXECUTE governs all four, and PUBLIC holds it on each by
-- default, so each must be denied unless designed. Trigger and event
-- trigger functions are left out: PostgreSQL refuses to call them
-- directly, so the default EXECUTE gives nothing (reported as INFO).
functions AS (
    SELECT p.oid, p.oid::regprocedure::text AS signature,
           CASE p.prokind WHEN 'f' THEN 'function' WHEN 'p' THEN 'procedure'
                          WHEN 'a' THEN 'aggregate' WHEN 'w' THEN 'window' END AS kind
      FROM pg_proc p
     WHERE p.pronamespace IN ('public'::regnamespace::oid,
                              coalesce(to_regnamespace('smm_auth')::oid, 0::oid))
       AND p.prokind IN ('f', 'p', 'a', 'w')
       AND p.prorettype NOT IN ('trigger'::regtype, 'event_trigger'::regtype)
),
checks(status, name, detail) AS (
    -- the role itself
    SELECT CASE WHEN EXISTS (SELECT 1 FROM role) THEN 'PASS' ELSE 'FAIL' END,
           'role.exists', 'ai_smm_auth'
    UNION ALL
    SELECT CASE WHEN NOT (rolsuper OR rolcreaterole OR rolcreatedb
                          OR rolreplication OR rolbypassrls OR rolinherit)
                 AND rolcanlogin AND rolconnlimit = 5
                THEN 'PASS' ELSE 'FAIL' END,
           'role.attributes',
           format('super=%s createrole=%s createdb=%s replication=%s bypassrls=%s inherit=%s login=%s connlimit=%s',
                  rolsuper, rolcreaterole, rolcreatedb, rolreplication,
                  rolbypassrls, rolinherit, rolcanlogin, rolconnlimit)
      FROM role
    UNION ALL
    SELECT CASE WHEN rolpassword LIKE 'SCRAM-SHA-256$%' THEN 'PASS' ELSE 'FAIL' END,
           'role.password',
           CASE WHEN rolpassword IS NULL THEN 'not set: the role cannot log in'
                WHEN rolpassword LIKE 'SCRAM-SHA-256$%' THEN 'set, SCRAM-SHA-256'
                ELSE 'set, NOT SCRAM-SHA-256' END
      FROM pg_authid WHERE rolname = 'ai_smm_auth'
    UNION ALL
    SELECT CASE WHEN rolvaliduntil IS NULL OR rolvaliduntil > now() + interval '30 days'
                THEN 'PASS' ELSE 'FAIL' END,
           'role.valid_until', coalesce(rolvaliduntil::text, 'none')
      FROM role
    UNION ALL
    SELECT CASE WHEN count(m.roleid) = 0 THEN 'PASS' ELSE 'FAIL' END,
           'role.member_of',
           coalesce(string_agg(pg_get_userbyid(m.roleid), ','), 'none')
      FROM role LEFT JOIN pg_auth_members m ON m.member = role.oid
    UNION ALL
    SELECT CASE WHEN count(m.member) = 0 THEN 'PASS' ELSE 'FAIL' END,
           'role.members',
           coalesce(string_agg(pg_get_userbyid(m.member), ','), 'none')
      FROM role LEFT JOIN pg_auth_members m ON m.roleid = role.oid
    UNION ALL
    SELECT CASE WHEN coalesce(array_length(setconfig, 1), 0) = 5
                 AND setconfig @> ARRAY['statement_timeout=5s', 'lock_timeout=2s',
                                        'idle_in_transaction_session_timeout=30s',
                                        'log_parameter_max_length=0',
                                        'log_parameter_max_length_on_error=0']
                THEN 'PASS' ELSE 'FAIL' END,
           'role.settings', coalesce(array_to_string(setconfig, ' '), 'none')
      FROM role
      LEFT JOIN pg_db_role_setting s ON s.setrole = role.oid AND s.setdatabase = 0
    UNION ALL
    SELECT CASE WHEN count(*) = 0 THEN 'PASS' ELSE 'FAIL' END,
           'role.db_specific_settings', count(*)::text
      FROM role JOIN pg_db_role_setting s ON s.setrole = role.oid AND s.setdatabase <> 0
    UNION ALL
    SELECT CASE WHEN n = 0 THEN 'PASS' ELSE 'FAIL' END, 'role.owns_nothing',
           n::text || ' owned objects'
      FROM (SELECT (SELECT count(*) FROM pg_class WHERE relowner = role.oid)
                 + (SELECT count(*) FROM pg_namespace WHERE nspowner = role.oid)
                 + (SELECT count(*) FROM pg_proc WHERE proowner = role.oid)
                 + (SELECT count(*) FROM pg_type WHERE typowner = role.oid)
                 + (SELECT count(*) FROM pg_database WHERE datdba = role.oid) AS n
              FROM role) o
    UNION ALL
    SELECT CASE WHEN count(*) = 0 THEN 'PASS' ELSE 'FAIL' END,
           'role.default_privileges', count(*)::text || ' entries'
      FROM role JOIN pg_default_acl d ON d.defaclrole = role.oid
                 OR d.defaclacl::text LIKE '%ai_smm_auth=%'

    -- database and schemas
    UNION ALL
    SELECT CASE WHEN has_database_privilege('ai_smm_auth', current_database(), 'CONNECT')
                THEN 'PASS' ELSE 'FAIL' END,
           'database.connect', current_database()
      FROM role
    UNION ALL
    SELECT CASE WHEN has_database_privilege('ai_smm_auth', current_database(), 'CREATE')
                THEN 'FAIL' ELSE 'PASS' END,
           'database.create', current_database()
      FROM role
    UNION ALL
    SELECT 'INFO', 'database.temporary',
           CASE WHEN has_database_privilege('ai_smm_auth', current_database(), 'TEMPORARY')
                THEN 'may create temporary tables (PUBLIC default)'
                ELSE 'no temporary tables' END
      FROM role
    UNION ALL
    SELECT CASE WHEN has_schema_privilege('ai_smm_auth', 'public', 'USAGE')
                THEN 'PASS' ELSE 'FAIL' END, 'schema.public.usage', ''
      FROM role
    UNION ALL
    SELECT CASE WHEN has_schema_privilege('ai_smm_auth', 'public', 'CREATE')
                THEN 'FAIL' ELSE 'PASS' END, 'schema.public.create', ''
      FROM role
    UNION ALL
    SELECT CASE WHEN to_regnamespace('smm_auth') IS NOT NULL
                 AND (SELECT nspowner FROM pg_namespace WHERE nspname = 'smm_auth')
                     = (SELECT oid FROM owner_role)
                THEN 'PASS' ELSE 'FAIL' END,
           'schema.smm_auth.owner',
           coalesce((SELECT pg_get_userbyid(nspowner) FROM pg_namespace
                      WHERE nspname = 'smm_auth'), 'missing')
    UNION ALL
    SELECT CASE WHEN to_regnamespace('smm_auth') IS NOT NULL
                 AND has_schema_privilege('ai_smm_auth', 'smm_auth', 'USAGE')
                THEN 'PASS' ELSE 'FAIL' END, 'schema.smm_auth.usage', ''
      FROM role
    UNION ALL
    SELECT CASE WHEN to_regnamespace('smm_auth') IS NOT NULL
                 AND NOT has_schema_privilege('ai_smm_auth', 'smm_auth', 'CREATE')
                THEN 'PASS' ELSE 'FAIL' END, 'schema.smm_auth.create', ''
      FROM role
    UNION ALL
    -- nobody but the owner and ai_smm_auth (USAGE) on the schema
    SELECT CASE WHEN count(*) = 0 THEN 'PASS' ELSE 'FAIL' END,
           'schema.smm_auth.acl',
           coalesce(string_agg(DISTINCT CASE a.grantee WHEN 0 THEN 'PUBLIC'
                                         ELSE pg_get_userbyid(a.grantee) END
                               || '=' || a.privilege_type, ','), 'owner and ai_smm_auth USAGE only')
      FROM pg_namespace n, aclexplode(coalesce(n.nspacl, acldefault('n', n.nspowner))) a
     WHERE n.nspname = 'smm_auth'
       AND a.grantee <> n.nspowner
       AND NOT (a.grantee = to_regrole('ai_smm_auth')::oid AND a.privilege_type = 'USAGE')
    UNION ALL
    SELECT CASE WHEN has_schema_privilege('ai_smm_auth', n.oid, 'USAGE')
                  OR has_schema_privilege('ai_smm_auth', n.oid, 'CREATE')
                THEN 'FAIL' ELSE 'PASS' END,
           'schema.other.usage', n.nspname
      FROM pg_namespace n, role
     WHERE n.nspname NOT IN ('public', 'smm_auth', 'pg_catalog',
                             'information_schema', 'pg_toast')
       AND n.nspname NOT LIKE 'pg_temp_%' AND n.nspname NOT LIKE 'pg_toast_temp_%'
    UNION ALL
    SELECT 'INFO', 'instance.other_database_connect', d.datname
      FROM pg_database d, role
     WHERE d.datname <> current_database() AND d.datallowconn
       AND has_database_privilege('ai_smm_auth', d.oid, 'CONNECT')

    -- every relation in public, at table level: nothing at all
    UNION ALL
    SELECT CASE WHEN has_table_privilege('ai_smm_auth', t.oid, p.privilege)
                THEN 'FAIL' ELSE 'PASS' END,
           format('table.%s.%s', t.relname, lower(p.privilege)), 'denied'
      FROM tables t, role
     CROSS JOIN (VALUES ('SELECT'), ('INSERT'), ('UPDATE'), ('DELETE'),
                        ('TRUNCATE'), ('REFERENCES'), ('TRIGGER')) AS p(privilege)

    -- every column: SELECT on the eight named users columns, nothing else
    UNION ALL
    SELECT CASE WHEN has_column_privilege('ai_smm_auth', t.oid, a.attnum, p.privilege)
                     = (g.relname IS NOT NULL)
                THEN 'PASS' ELSE 'FAIL' END,
           format('column.%s.%s.%s', t.relname, a.attname, lower(p.privilege)),
           CASE WHEN g.relname IS NOT NULL THEN 'granted' ELSE 'denied' END
      FROM tables t
      JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum > 0 AND NOT a.attisdropped
     CROSS JOIN role
     CROSS JOIN (VALUES ('SELECT'), ('INSERT'), ('UPDATE'), ('REFERENCES')) AS p(privilege)
      LEFT JOIN column_grants g
        ON g.relname = t.relname AND g.attname = a.attname AND g.privilege = p.privilege

    -- raw ACL: no grant to ai_smm_auth on any relation, and on columns only
    -- the designed ones (catches a grant shadowed by another privilege)
    UNION ALL
    SELECT CASE WHEN count(*) = 0 THEN 'PASS' ELSE 'FAIL' END,
           'acl.relations',
           coalesce(string_agg(c.oid::regclass::text || '=' || a.privilege_type, ','), 'none')
      FROM pg_class c, aclexplode(c.relacl) a
     WHERE a.grantee = to_regrole('ai_smm_auth')::oid
    UNION ALL
    SELECT CASE WHEN count(*) = 0 THEN 'PASS' ELSE 'FAIL' END,
           'acl.columns.unexpected',
           coalesce(string_agg(att.attrelid::regclass::text || '.' || att.attname
                               || '=' || a.privilege_type, ','), 'none')
      FROM pg_attribute att, aclexplode(att.attacl) a
     WHERE att.attacl IS NOT NULL
       AND a.grantee = to_regrole('ai_smm_auth')::oid
       AND NOT EXISTS (
           SELECT 1 FROM column_grants g
            WHERE att.attrelid = to_regclass('public.' || g.relname)
              AND g.attname = att.attname AND g.privilege = a.privilege_type)

    -- every sequence in public: nothing
    UNION ALL
    SELECT CASE WHEN has_sequence_privilege('ai_smm_auth', s.oid, p.privilege)
                THEN 'FAIL' ELSE 'PASS' END,
           format('sequence.%s.%s', s.relname, lower(p.privilege)), 'denied'
      FROM pg_class s, role
     CROSS JOIN (VALUES ('USAGE'), ('SELECT'), ('UPDATE')) AS p(privilege)
     WHERE s.relnamespace = 'public'::regnamespace AND s.relkind = 'S'

    -- every executable routine: EXECUTE (effective: own grants, PUBLIC,
    -- membership) for ai_smm_auth only where designed
    UNION ALL
    SELECT CASE WHEN has_function_privilege('ai_smm_auth', f.oid, 'EXECUTE')
                     = coalesce(d.granted, false)
                THEN 'PASS' ELSE 'FAIL' END,
           format('%s.%s.execute', f.kind, f.signature),
           CASE WHEN coalesce(d.granted, false) THEN 'granted' ELSE 'denied' END
      FROM functions f
     CROSS JOIN role
      LEFT JOIN designed_functions d ON d.signature = f.signature
    UNION ALL
    -- raw ACL, in every schema: no routine names ai_smm_auth but the seven
    -- contracts (a grant in a schema the role cannot use is still a grant)
    SELECT CASE WHEN count(*) = 0 THEN 'PASS' ELSE 'FAIL' END,
           'acl.routines.unexpected',
           coalesce(string_agg(p.oid::regprocedure::text || '=' || a.privilege_type, ','),
                    'none')
      FROM pg_proc p, aclexplode(p.proacl) a
     WHERE a.grantee = to_regrole('ai_smm_auth')::oid
       AND NOT EXISTS (SELECT 1 FROM designed_functions d
                        WHERE d.granted AND d.signature = p.oid::regprocedure::text
                          AND a.privilege_type = 'EXECUTE')
    UNION ALL
    SELECT 'INFO', 'function.trigger_functions',
           count(*)::text || ' trigger or event trigger functions (not callable directly)'
      FROM pg_proc p
     WHERE p.pronamespace IN ('public'::regnamespace::oid,
                              coalesce(to_regnamespace('smm_auth')::oid, 0::oid))
       AND p.prorettype IN ('trigger'::regtype, 'event_trigger'::regtype)

    -- each smm_auth function: present, exactly as designed, PUBLIC and
    -- ai_smm_cc shut out
    UNION ALL
    SELECT CASE WHEN p.oid IS NOT NULL
                 AND p.proowner = (SELECT oid FROM owner_role)
                 AND p.prosecdef = d.secdef
                 AND p.provolatile::text = d.volatility
                 AND p.proisstrict
                 AND p.proconfig = ARRAY['search_path=pg_catalog, pg_temp']
                 AND md5(p.prosrc) = d.body_md5
                THEN 'PASS' ELSE 'FAIL' END,
           format('function.%s.definition', d.signature),
           CASE WHEN p.oid IS NULL THEN 'missing'
                ELSE format('owner=%s secdef=%s volatility=%s strict=%s config=%s body_md5=%s',
                            pg_get_userbyid(p.proowner), p.prosecdef, p.provolatile,
                            p.proisstrict, array_to_string(p.proconfig, ';'),
                            md5(p.prosrc)) END
      FROM designed_functions d
      LEFT JOIN pg_proc p ON p.oid = to_regprocedure(d.signature)
    UNION ALL
    SELECT CASE WHEN p.oid IS NOT NULL
                 AND NOT has_function_privilege('public', p.oid, 'EXECUTE')
                THEN 'PASS' ELSE 'FAIL' END,
           format('function.%s.public_execute', d.signature), 'denied'
      FROM designed_functions d
      LEFT JOIN pg_proc p ON p.oid = to_regprocedure(d.signature)
    UNION ALL
    SELECT CASE WHEN p.oid IS NOT NULL
                 AND (to_regrole('ai_smm_cc') IS NULL
                      OR NOT has_function_privilege('ai_smm_cc', p.oid, 'EXECUTE'))
                THEN 'PASS' ELSE 'FAIL' END,
           format('function.%s.ai_smm_cc_execute', d.signature),
           CASE WHEN to_regrole('ai_smm_cc') IS NULL THEN 'role absent' ELSE 'denied' END
      FROM designed_functions d
      LEFT JOIN pg_proc p ON p.oid = to_regprocedure(d.signature)
    UNION ALL
    SELECT CASE WHEN to_regnamespace('smm_auth') IS NOT NULL
                 AND (to_regrole('ai_smm_cc') IS NULL
                      OR NOT has_schema_privilege('ai_smm_cc', 'smm_auth', 'USAGE'))
                THEN 'PASS' ELSE 'FAIL' END,
           'schema.smm_auth.ai_smm_cc_usage',
           CASE WHEN to_regrole('ai_smm_cc') IS NULL THEN 'role absent' ELSE 'denied' END
    UNION ALL
    -- nothing in smm_auth but the designed functions
    SELECT CASE WHEN count(*) = 0 THEN 'PASS' ELSE 'FAIL' END,
           'schema.smm_auth.objects',
           coalesce(string_agg(o, ','), 'designed functions only')
      FROM (
          SELECT p.oid::regprocedure::text AS o
            FROM pg_proc p
           WHERE p.pronamespace = to_regnamespace('smm_auth')
             AND p.oid::regprocedure::text NOT IN (SELECT signature FROM designed_functions)
          UNION ALL
          SELECT c.oid::regclass::text
            FROM pg_class c WHERE c.relnamespace = to_regnamespace('smm_auth')
          UNION ALL
          SELECT t.oid::regtype::text
            FROM pg_type t
           WHERE t.typnamespace = to_regnamespace('smm_auth')
             AND t.typrelid = 0 AND t.typelem = 0
      ) extra
)
SELECT status || '|' || name || '|' || detail
  FROM (
      SELECT status, name, detail,
             CASE status WHEN 'FAIL' THEN 0 WHEN 'INFO' THEN 1 ELSE 2 END AS rank
        FROM checks
      UNION ALL
      SELECT 'END', 'checks', count(*)::text, 3 FROM checks
  ) ordered
 ORDER BY rank, name, detail;
