-- ai_smm_cc -- read-only verification of the Control Center API's role
-- (SMM-024C-2). One SELECT; it changes nothing.
--
-- Run as the instance's superuser (it reads pg_authid), connected to the
-- application database, in a read-only transaction:
--
--   PGOPTIONS='-c default_transaction_read_only=on' \
--     psql -X -v ON_ERROR_STOP=1 -d ai_smm -tA -f ai_smm_cc.verify.sql
--
-- Output: one line per check, `STATUS|check|detail`, FAIL first.
--   PASS  as designed
--   FAIL  not as designed: do not point the API at this role
--   INFO  a fact to record that this role cannot change (PUBLIC grants)
-- The last line is always `END|checks|<n>`; a run without it did not
-- complete. cc-gate.sh runs this file before `up` and `restart smm-api`
-- and refuses on any FAIL.
--
-- EFFECTIVE privileges are checked (has_*_privilege: own grants, PUBLIC,
-- and anything inherited), for EVERY table, column and sequence in schema
-- public -- not only the ones ai_smm_cc.grants.sql names -- so a privilege
-- that arrives by another path, or a table added later, is caught.

WITH
role AS (
    SELECT r.*
      FROM pg_roles r
     WHERE r.rolname = 'ai_smm_cc'
),
-- The designed matrix. Anything not listed here must be false.
table_grants(relname, privilege) AS (
    VALUES
        ('projects', 'SELECT'), ('project_memberships', 'SELECT'),
        ('project_settings', 'SELECT'), ('publications', 'SELECT'),
        ('publication_attempts', 'SELECT'), ('content_series', 'SELECT'),
        ('content_items', 'SELECT'), ('content_revisions', 'SELECT'),
        ('content_approvals', 'SELECT'),
        ('content_publication_links', 'SELECT'),
        ('audit_log', 'SELECT'), ('users', 'SELECT'),
        ('user_sessions', 'SELECT')
),
column_grants(relname, attname, privilege) AS (
    VALUES
        ('user_sessions', 'id', 'INSERT'),
        ('user_sessions', 'user_id', 'INSERT'),
        ('user_sessions', 'token_hash', 'INSERT'),
        ('user_sessions', 'expires_at', 'INSERT'),
        ('user_sessions', 'last_seen_at', 'INSERT'),
        ('user_sessions', 'created_at', 'INSERT'),
        ('user_sessions', 'revoked_at', 'INSERT'),
        ('user_sessions', 'last_seen_at', 'UPDATE'),
        ('user_sessions', 'revoked_at', 'UPDATE'),
        ('users', 'last_login_at', 'UPDATE'),
        ('users', 'updated_at', 'UPDATE'),
        ('audit_log', 'actor', 'INSERT'),
        ('audit_log', 'action', 'INSERT'),
        ('audit_log', 'subject', 'INSERT'),
        ('audit_log', 'details', 'INSERT')
),
tables AS (
    SELECT c.oid, c.relname
      FROM pg_class c
     WHERE c.relnamespace = 'public'::regnamespace
       AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
),
checks(status, name, detail) AS (
    -- the role itself
    SELECT CASE WHEN EXISTS (SELECT 1 FROM role) THEN 'PASS' ELSE 'FAIL' END,
           'role.exists', 'ai_smm_cc'
    UNION ALL
    SELECT CASE WHEN NOT (rolsuper OR rolcreaterole OR rolcreatedb
                          OR rolreplication OR rolbypassrls OR rolinherit)
                 AND rolcanlogin AND rolconnlimit = 10
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
      FROM pg_authid WHERE rolname = 'ai_smm_cc'
    UNION ALL
    SELECT CASE WHEN rolvaliduntil IS NULL OR rolvaliduntil > now() + interval '30 days'
                THEN 'PASS' ELSE 'FAIL' END,
           'role.valid_until', coalesce(rolvaliduntil::text, 'none')
      FROM role
    UNION ALL
    -- member of nothing: no inherited or SET ROLE-able privileges
    SELECT CASE WHEN count(m.roleid) = 0 THEN 'PASS' ELSE 'FAIL' END,
           'role.member_of',
           coalesce(string_agg(pg_get_userbyid(m.roleid), ','), 'none')
      FROM role LEFT JOIN pg_auth_members m ON m.member = role.oid
    UNION ALL
    -- no one acts as ai_smm_cc either
    SELECT CASE WHEN count(m.member) = 0 THEN 'PASS' ELSE 'FAIL' END,
           'role.members',
           coalesce(string_agg(pg_get_userbyid(m.member), ','), 'none')
      FROM role LEFT JOIN pg_auth_members m ON m.roleid = role.oid
    UNION ALL
    SELECT CASE WHEN coalesce(array_length(setconfig, 1), 0) = 3
                 AND setconfig @> ARRAY['statement_timeout=15s', 'lock_timeout=5s',
                                        'idle_in_transaction_session_timeout=60s']
                THEN 'PASS' ELSE 'FAIL' END,
           'role.settings', coalesce(array_to_string(setconfig, ' '), 'none')
      FROM role
      LEFT JOIN pg_db_role_setting s ON s.setrole = role.oid AND s.setdatabase = 0
    UNION ALL
    SELECT CASE WHEN count(*) = 0 THEN 'PASS' ELSE 'FAIL' END,
           'role.db_specific_settings', count(*)::text
      FROM role JOIN pg_db_role_setting s ON s.setrole = role.oid AND s.setdatabase <> 0
    UNION ALL
    -- owns nothing in this database: no implicit rights, no DDL
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
                 OR d.defaclacl::text LIKE '%ai_smm_cc=%'

    -- database and schema
    UNION ALL
    SELECT CASE WHEN has_database_privilege('ai_smm_cc', current_database(), 'CONNECT')
                THEN 'PASS' ELSE 'FAIL' END,
           'database.connect', current_database()
    UNION ALL
    SELECT CASE WHEN has_database_privilege('ai_smm_cc', current_database(), 'CREATE')
                THEN 'FAIL' ELSE 'PASS' END,
           'database.create', current_database()
    UNION ALL
    SELECT 'INFO', 'database.temporary',
           CASE WHEN has_database_privilege('ai_smm_cc', current_database(), 'TEMPORARY')
                THEN 'may create temporary tables (PUBLIC default)'
                ELSE 'no temporary tables' END
    UNION ALL
    SELECT CASE WHEN has_schema_privilege('ai_smm_cc', 'public', 'USAGE')
                THEN 'PASS' ELSE 'FAIL' END, 'schema.public.usage', ''
    UNION ALL
    SELECT CASE WHEN has_schema_privilege('ai_smm_cc', 'public', 'CREATE')
                THEN 'FAIL' ELSE 'PASS' END, 'schema.public.create', ''
    UNION ALL
    SELECT CASE WHEN has_schema_privilege('ai_smm_cc', n.oid, 'USAGE')
                THEN 'FAIL' ELSE 'PASS' END,
           'schema.other.usage', n.nspname
      FROM pg_namespace n
     WHERE n.nspname NOT IN ('public', 'pg_catalog', 'information_schema', 'pg_toast')
       AND n.nspname NOT LIKE 'pg_temp_%' AND n.nspname NOT LIKE 'pg_toast_temp_%'
    UNION ALL
    SELECT 'INFO', 'instance.other_database_connect', d.datname
      FROM pg_database d
     WHERE d.datname <> current_database() AND d.datallowconn
       AND has_database_privilege('ai_smm_cc', d.oid, 'CONNECT')

    -- every application table, at table level
    UNION ALL
    SELECT CASE WHEN has_table_privilege('ai_smm_cc', t.oid, p.privilege)
                     = (g.relname IS NOT NULL)
                THEN 'PASS' ELSE 'FAIL' END,
           format('table.%s.%s', t.relname, lower(p.privilege)),
           CASE WHEN g.relname IS NOT NULL THEN 'granted' ELSE 'denied' END
      FROM tables t
     CROSS JOIN (VALUES ('SELECT'), ('INSERT'), ('UPDATE'), ('DELETE'),
                        ('TRUNCATE'), ('REFERENCES'), ('TRIGGER')) AS p(privilege)
      LEFT JOIN table_grants g
        ON g.relname = t.relname AND g.privilege = p.privilege
     WHERE EXISTS (SELECT 1 FROM role)

    -- every column: SELECT follows the table; INSERT/UPDATE only as listed
    UNION ALL
    SELECT CASE WHEN has_column_privilege('ai_smm_cc', t.oid, a.attnum, p.privilege)
                     = (tg.relname IS NOT NULL OR cg.relname IS NOT NULL)
                THEN 'PASS' ELSE 'FAIL' END,
           format('column.%s.%s.%s', t.relname, a.attname, lower(p.privilege)),
           CASE WHEN tg.relname IS NOT NULL OR cg.relname IS NOT NULL
                THEN 'granted' ELSE 'denied' END
      FROM tables t
      JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum > 0 AND NOT a.attisdropped
     CROSS JOIN (VALUES ('SELECT'), ('INSERT'), ('UPDATE'), ('REFERENCES')) AS p(privilege)
      LEFT JOIN table_grants tg
        ON tg.relname = t.relname AND tg.privilege = p.privilege
      LEFT JOIN column_grants cg
        ON cg.relname = t.relname AND cg.attname = a.attname
       AND cg.privilege = p.privilege
     WHERE EXISTS (SELECT 1 FROM role)

    -- every sequence: USAGE on audit_log's only
    UNION ALL
    SELECT CASE WHEN has_sequence_privilege('ai_smm_cc', s.oid, p.privilege)
                     = (p.privilege = 'USAGE'
                        AND s.oid = pg_get_serial_sequence('public.audit_log', 'id')::regclass)
                THEN 'PASS' ELSE 'FAIL' END,
           format('sequence.%s.%s', s.relname, lower(p.privilege)),
           CASE WHEN p.privilege = 'USAGE'
                 AND s.oid = pg_get_serial_sequence('public.audit_log', 'id')::regclass
                THEN 'granted' ELSE 'denied' END
      FROM pg_class s
     CROSS JOIN (VALUES ('USAGE'), ('SELECT'), ('UPDATE')) AS p(privilege)
     WHERE s.relnamespace = 'public'::regnamespace AND s.relkind = 'S'
       AND EXISTS (SELECT 1 FROM role)

    -- every designed grant names a table that exists (a renamed table would
    -- otherwise just look "denied")
    UNION ALL
    SELECT CASE WHEN to_regclass(format('public.%I', g.relname)) IS NOT NULL
                THEN 'PASS' ELSE 'FAIL' END,
           format('matrix.%s', g.relname), 'table present'
      FROM (SELECT relname FROM table_grants
            UNION SELECT relname FROM column_grants) g
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
