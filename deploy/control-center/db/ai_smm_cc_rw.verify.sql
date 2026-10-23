-- ai_smm_cc_rw -- read-only verification of the Control Center API's
-- business-mutation role (SMM-025C). One SELECT; it changes nothing.
--
-- Run as the instance's superuser (it reads pg_authid), connected to the
-- application database, in a read-only transaction:
--
--   PGOPTIONS='-c default_transaction_read_only=on' \
--     psql -X -v ON_ERROR_STOP=1 -d ai_smm -tA -f ai_smm_cc_rw.verify.sql
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
-- public -- not only the ones ai_smm_cc_rw.grants.sql names -- so a privilege
-- that arrives by another path, or a table added later, is caught.

WITH
role AS (
    SELECT r.*
      FROM pg_roles r
     WHERE r.rolname = 'ai_smm_cc_rw'
),
-- SMM-025D: the guard functions this role may execute (ai_smm.db.guards).
guard_functions(signature) AS (
    VALUES ('guard_bind_session(text)'),
           ('guard_context_user()'),
           ('guard_context_session()'),
           ('guard_require_owner(text,text)'),
           ('guard_publication_problem(bigint)'),
           ('guard_consume_approval_grant(uuid,uuid,uuid)')
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
        ('audit_log', 'SELECT'),
        ('user_sessions', 'SELECT'),
        ('social_accounts', 'SELECT'),
        ('content_generation_jobs', 'SELECT'),
        -- SMM-035B3A
        ('topic_ideation_jobs', 'SELECT'),
        ('content_ideas', 'SELECT')
),
column_grants(relname, attname, privilege) AS (
    VALUES
        -- G2 (SMM-025D): sessions are smm-auth's; the read check only
        -- advances last_seen_at. users column by column, no password_hash.
        ('user_sessions', 'last_seen_at', 'UPDATE'),
        ('users', 'id', 'SELECT'),
        ('users', 'email', 'SELECT'),
        ('users', 'display_name', 'SELECT'),
        ('users', 'is_active', 'SELECT'),
        ('users', 'last_login_at', 'SELECT'),
        ('users', 'created_at', 'SELECT'),
        ('users', 'updated_at', 'SELECT'),
        ('audit_log', 'actor', 'INSERT'),
        ('audit_log', 'action', 'INSERT'),
        ('audit_log', 'subject', 'INSERT'),
        ('audit_log', 'details', 'INSERT'),
        ('social_account_credentials', 'id', 'SELECT'),
        ('social_account_credentials', 'social_account_id', 'SELECT'),
        ('social_account_credentials', 'status', 'SELECT'),
        ('social_account_credentials', 'scheme', 'SELECT'),
        ('social_account_credentials', 'key_id', 'SELECT'),
        ('social_account_credentials', 'fingerprint', 'SELECT'),
        ('social_account_credentials', 'verification_status', 'SELECT'),
        ('social_account_credentials', 'verified_at', 'SELECT'),
        ('social_account_credentials', 'expires_at', 'SELECT'),
        ('social_account_credentials', 'created_by_user_id', 'SELECT'),
        ('social_account_credentials', 'created_at', 'SELECT'),
        ('social_account_credentials', 'superseded_at', 'SELECT'),
        ('social_account_credentials', 'revoked_at', 'SELECT'),
        -- business writes (mutation mode)
        ('projects', 'id', 'INSERT'),
        ('projects', 'display_name', 'INSERT'),
        ('projects', 'description', 'INSERT'),
        ('projects', 'knowledge_path', 'INSERT'),
        ('projects', 'default_platform', 'INSERT'),
        ('projects', 'is_active', 'INSERT'),
        ('projects', 'default_timezone', 'INSERT'),
        ('projects', 'default_language', 'INSERT'),
        ('projects', 'version', 'INSERT'),
        ('projects', 'created_at', 'INSERT'),
        ('projects', 'updated_at', 'INSERT'),
        ('projects', 'display_name', 'UPDATE'),
        ('projects', 'description', 'UPDATE'),
        ('projects', 'default_timezone', 'UPDATE'),
        ('projects', 'default_language', 'UPDATE'),
        ('projects', 'version', 'UPDATE'),
        ('projects', 'updated_at', 'UPDATE'),
        ('project_memberships', 'id', 'INSERT'),
        ('project_memberships', 'project_id', 'INSERT'),
        ('project_memberships', 'user_id', 'INSERT'),
        ('project_memberships', 'role', 'INSERT'),
        ('project_memberships', 'created_at', 'INSERT'),
        ('project_memberships', 'updated_at', 'INSERT'),
        ('project_settings', 'project_id', 'INSERT'),
        ('project_settings', 'content_config', 'INSERT'),
        ('project_settings', 'publishing_config', 'INSERT'),
        ('project_settings', 'brand_config', 'INSERT'),
        ('project_settings', 'version', 'INSERT'),
        ('project_settings', 'updated_at', 'INSERT'),
        ('project_settings', 'content_config', 'UPDATE'),
        ('project_settings', 'publishing_config', 'UPDATE'),
        ('project_settings', 'brand_config', 'UPDATE'),
        ('project_settings', 'version', 'UPDATE'),
        ('project_settings', 'updated_at', 'UPDATE'),
        ('social_accounts', 'id', 'INSERT'),
        ('social_accounts', 'project_id', 'INSERT'),
        ('social_accounts', 'platform', 'INSERT'),
        ('social_accounts', 'display_name', 'INSERT'),
        ('social_accounts', 'status', 'INSERT'),
        ('social_accounts', 'external_account_id', 'INSERT'),
        ('social_accounts', 'external_username', 'INSERT'),
        ('social_accounts', 'version', 'INSERT'),
        ('social_accounts', 'created_by_user_id', 'INSERT'),
        ('social_accounts', 'created_at', 'INSERT'),
        ('social_accounts', 'updated_at', 'INSERT'),
        ('social_accounts', 'disabled_at', 'INSERT'),
        ('social_accounts', 'display_name', 'UPDATE'),
        ('social_accounts', 'status', 'UPDATE'),
        ('social_accounts', 'disabled_at', 'UPDATE'),
        ('social_accounts', 'external_account_id', 'UPDATE'),
        ('social_accounts', 'external_username', 'UPDATE'),
        ('social_accounts', 'version', 'UPDATE'),
        ('social_accounts', 'updated_at', 'UPDATE'),
        ('social_account_credentials', 'id', 'INSERT'),
        ('social_account_credentials', 'social_account_id', 'INSERT'),
        ('social_account_credentials', 'status', 'INSERT'),
        ('social_account_credentials', 'scheme', 'INSERT'),
        ('social_account_credentials', 'key_id', 'INSERT'),
        ('social_account_credentials', 'ephemeral_public_key', 'INSERT'),
        ('social_account_credentials', 'nonce', 'INSERT'),
        ('social_account_credentials', 'ciphertext', 'INSERT'),
        ('social_account_credentials', 'fingerprint', 'INSERT'),
        ('social_account_credentials', 'verification_status', 'INSERT'),
        ('social_account_credentials', 'verified_at', 'INSERT'),
        ('social_account_credentials', 'expires_at', 'INSERT'),
        ('social_account_credentials', 'created_by_user_id', 'INSERT'),
        ('social_account_credentials', 'created_at', 'INSERT'),
        ('social_account_credentials', 'superseded_at', 'INSERT'),
        ('social_account_credentials', 'revoked_at', 'INSERT'),
        ('social_account_credentials', 'status', 'UPDATE'),
        ('social_account_credentials', 'ephemeral_public_key', 'UPDATE'),
        ('social_account_credentials', 'nonce', 'UPDATE'),
        ('social_account_credentials', 'ciphertext', 'UPDATE'),
        ('social_account_credentials', 'superseded_at', 'UPDATE'),
        ('social_account_credentials', 'revoked_at', 'UPDATE'),
        ('content_items', 'id', 'INSERT'),
        ('content_items', 'project_id', 'INSERT'),
        ('content_items', 'title', 'INSERT'),
        ('content_items', 'content_type', 'INSERT'),
        ('content_items', 'status', 'INSERT'),
        ('content_items', 'current_revision_id', 'INSERT'),
        ('content_items', 'approved_revision_id', 'INSERT'),
        ('content_items', 'version', 'INSERT'),
        ('content_items', 'created_by_user_id', 'INSERT'),
        ('content_items', 'generation_job_id', 'INSERT'),
        ('content_items', 'created_at', 'INSERT'),
        ('content_items', 'updated_at', 'INSERT'),
        ('content_items', 'archived_at', 'INSERT'),
        ('content_items', 'current_revision_id', 'UPDATE'),
        ('content_items', 'approved_revision_id', 'UPDATE'),
        ('content_items', 'status', 'UPDATE'),
        ('content_items', 'version', 'UPDATE'),
        ('content_items', 'updated_at', 'UPDATE'),
        ('content_revisions', 'id', 'INSERT'),
        ('content_revisions', 'content_item_id', 'INSERT'),
        ('content_revisions', 'revision_number', 'INSERT'),
        ('content_revisions', 'title', 'INSERT'),
        ('content_revisions', 'body', 'INSERT'),
        ('content_revisions', 'format', 'INSERT'),
        ('content_revisions', 'images', 'INSERT'),
        ('content_revisions', 'items', 'INSERT'),
        ('content_revisions', 'metadata', 'INSERT'),
        ('content_revisions', 'source', 'INSERT'),
        ('content_revisions', 'source_ref', 'INSERT'),
        ('content_revisions', 'editor_score', 'INSERT'),
        ('content_revisions', 'editor_notes', 'INSERT'),
        ('content_revisions', 'content_hash', 'INSERT'),
        ('content_revisions', 'social_account_id', 'INSERT'),
        ('content_revisions', 'created_by_user_id', 'INSERT'),
        ('content_revisions', 'created_at', 'INSERT'),
        ('content_approvals', 'id', 'INSERT'),
        ('content_approvals', 'content_item_id', 'INSERT'),
        ('content_approvals', 'revision_id', 'INSERT'),
        ('content_approvals', 'decision', 'INSERT'),
        ('content_approvals', 'actor_user_id', 'INSERT'),
        ('content_approvals', 'note', 'INSERT'),
        ('content_approvals', 'created_at', 'INSERT'),
        ('content_publication_links', 'content_item_id', 'INSERT'),
        ('content_publication_links', 'revision_id', 'INSERT'),
        ('content_publication_links', 'platform', 'INSERT'),
        ('content_publication_links', 'publication_id', 'INSERT'),
        ('content_publication_links', 'created_at', 'INSERT'),
        ('content_generation_jobs', 'id', 'INSERT'),
        ('content_generation_jobs', 'project_id', 'INSERT'),
        ('content_generation_jobs', 'requested_by_user_id', 'INSERT'),
        ('content_generation_jobs', 'status', 'INSERT'),
        ('content_generation_jobs', 'params', 'INSERT'),
        ('content_generation_jobs', 'max_attempts', 'INSERT'),
        ('content_generation_jobs', 'last_error', 'INSERT'),
        ('content_generation_jobs', 'next_attempt_at', 'INSERT'),
        ('content_generation_jobs', 'claimed_by', 'INSERT'),
        ('content_generation_jobs', 'claimed_at', 'INSERT'),
        ('content_generation_jobs', 'lease_expires_at', 'INSERT'),
        ('content_generation_jobs', 'created_at', 'INSERT'),
        ('content_generation_jobs', 'updated_at', 'INSERT'),
        ('content_generation_jobs', 'finished_at', 'INSERT'),
        ('content_generation_jobs', 'status', 'UPDATE'),
        ('content_generation_jobs', 'updated_at', 'UPDATE'),
        ('content_generation_jobs', 'finished_at', 'UPDATE'),
        ('content_generation_jobs', 'claimed_by', 'UPDATE'),
        ('content_generation_jobs', 'claimed_at', 'UPDATE'),
        ('content_generation_jobs', 'lease_expires_at', 'UPDATE'),
        ('content_generation_jobs', 'next_attempt_at', 'UPDATE'),
        ('content_generation_jobs', 'attempt_count', 'UPDATE'),
        ('content_generation_jobs', 'result', 'UPDATE'),
        ('content_generation_jobs', 'last_error', 'UPDATE'),
        -- SMM-035B3A: queue a topic ideation job, request columns only
        ('topic_ideation_jobs', 'id', 'INSERT'),
        ('topic_ideation_jobs', 'project_id', 'INSERT'),
        ('topic_ideation_jobs', 'requested_by_user_id', 'INSERT'),
        ('topic_ideation_jobs', 'status', 'INSERT'),
        ('topic_ideation_jobs', 'idea_count', 'INSERT'),
        ('topic_ideation_jobs', 'topic_hint', 'INSERT'),
        ('topic_ideation_jobs', 'params', 'INSERT'),
        ('topic_ideation_jobs', 'max_attempts', 'INSERT'),
        ('publications', 'project_id', 'INSERT'),
        ('publications', 'platform', 'INSERT'),
        ('publications', 'ordinal', 'INSERT'),
        ('publications', 'title', 'INSERT'),
        ('publications', 'format', 'INSERT'),
        ('publications', 'body', 'INSERT'),
        ('publications', 'images', 'INSERT'),
        ('publications', 'items', 'INSERT'),
        ('publications', 'status', 'INSERT'),
        ('publications', 'scheduled_at', 'INSERT'),
        ('publications', 'idempotency_key', 'INSERT'),
        ('publications', 'threads_post_id', 'INSERT'),
        ('publications', 'published_at', 'INSERT'),
        ('publications', 'claimed_by', 'INSERT'),
        ('publications', 'claimed_at', 'INSERT'),
        ('publications', 'lease_expires_at', 'INSERT'),
        ('publications', 'attempt_count', 'INSERT'),
        ('publications', 'next_attempt_at', 'INSERT'),
        ('publications', 'last_error', 'INSERT'),
        ('publications', 'editor_score', 'INSERT'),
        ('publications', 'human_reviewed', 'INSERT'),
        ('publications', 'source_ref', 'INSERT'),
        ('publications', 'social_account_id', 'INSERT'),
        ('publications', 'series_id', 'INSERT'),
        ('publications', 'series_position', 'INSERT'),
        ('publications', 'series_total', 'INSERT'),
        ('publications', 'parent_publication_id', 'INSERT'),
        ('publications', 'previous_publication_id', 'INSERT'),
        ('publications', 'created_at', 'INSERT'),
        ('publications', 'updated_at', 'INSERT'),
        ('publications', 'status', 'UPDATE'),
        ('publications', 'scheduled_at', 'UPDATE'),
        ('publications', 'next_attempt_at', 'UPDATE'),
        ('publications', 'claimed_by', 'UPDATE'),
        ('publications', 'claimed_at', 'UPDATE'),
        ('publications', 'lease_expires_at', 'UPDATE'),
        ('publications', 'last_error', 'UPDATE'),
        ('publications', 'attempt_count', 'UPDATE'),
        ('publications', 'updated_at', 'UPDATE')
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
           'role.exists', 'ai_smm_cc_rw'
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
      FROM pg_authid WHERE rolname = 'ai_smm_cc_rw'
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
    -- no one acts as ai_smm_cc_rw either
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
                 OR d.defaclacl::text LIKE '%ai_smm_cc_rw=%'

    -- database and schema
    UNION ALL
    SELECT CASE WHEN has_database_privilege('ai_smm_cc_rw', current_database(), 'CONNECT')
                THEN 'PASS' ELSE 'FAIL' END,
           'database.connect', current_database()
    UNION ALL
    SELECT CASE WHEN has_database_privilege('ai_smm_cc_rw', current_database(), 'CREATE')
                THEN 'FAIL' ELSE 'PASS' END,
           'database.create', current_database()
    UNION ALL
    SELECT 'INFO', 'database.temporary',
           CASE WHEN has_database_privilege('ai_smm_cc_rw', current_database(), 'TEMPORARY')
                THEN 'may create temporary tables (PUBLIC default)'
                ELSE 'no temporary tables' END
    UNION ALL
    SELECT CASE WHEN has_schema_privilege('ai_smm_cc_rw', 'public', 'USAGE')
                THEN 'PASS' ELSE 'FAIL' END, 'schema.public.usage', ''
    UNION ALL
    SELECT CASE WHEN has_schema_privilege('ai_smm_cc_rw', 'public', 'CREATE')
                THEN 'FAIL' ELSE 'PASS' END, 'schema.public.create', ''
    UNION ALL
    SELECT CASE WHEN has_schema_privilege('ai_smm_cc_rw', n.oid, 'USAGE')
                THEN 'FAIL' ELSE 'PASS' END,
           'schema.other.usage', n.nspname
      FROM pg_namespace n
     WHERE n.nspname NOT IN ('public', 'pg_catalog', 'information_schema', 'pg_toast')
       AND n.nspname NOT LIKE 'pg_temp_%' AND n.nspname NOT LIKE 'pg_toast_temp_%'
    UNION ALL
    SELECT 'INFO', 'instance.other_database_connect', d.datname
      FROM pg_database d
     WHERE d.datname <> current_database() AND d.datallowconn
       AND has_database_privilege('ai_smm_cc_rw', d.oid, 'CONNECT')

    -- every application table, at table level
    UNION ALL
    SELECT CASE WHEN has_table_privilege('ai_smm_cc_rw', t.oid, p.privilege)
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
    SELECT CASE WHEN has_column_privilege('ai_smm_cc_rw', t.oid, a.attnum, p.privilege)
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

    -- every sequence: USAGE on audit_log's and publications' only
    UNION ALL
    SELECT CASE WHEN has_sequence_privilege('ai_smm_cc_rw', s.oid, p.privilege)
                     = (p.privilege = 'USAGE'
                        AND s.oid IN (pg_get_serial_sequence('public.audit_log', 'id')::regclass,
                                      pg_get_serial_sequence('public.publications', 'id')::regclass))
                THEN 'PASS' ELSE 'FAIL' END,
           format('sequence.%s.%s', s.relname, lower(p.privilege)),
           CASE WHEN p.privilege = 'USAGE'
                 AND s.oid IN (pg_get_serial_sequence('public.audit_log', 'id')::regclass,
                               pg_get_serial_sequence('public.publications', 'id')::regclass)
                THEN 'granted' ELSE 'denied' END
      FROM pg_class s
     CROSS JOIN (VALUES ('USAGE'), ('SELECT'), ('UPDATE')) AS p(privilege)
     WHERE s.relnamespace = 'public'::regnamespace AND s.relkind = 'S'
       AND EXISTS (SELECT 1 FROM role)

    -- SMM-025D: every function of the application owner in schema public
    -- (trigger functions cannot be called, so they are not listed):
    -- EXECUTE on exactly the guard functions this role calls
    UNION ALL
    SELECT CASE WHEN has_function_privilege('ai_smm_cc_rw', p.oid, 'EXECUTE')
                     = (p.oid::regprocedure::text IN (SELECT signature FROM guard_functions))
                THEN 'PASS' ELSE 'FAIL' END,
           format('function.%s.execute', p.proname),
           CASE WHEN p.oid::regprocedure::text IN (SELECT signature FROM guard_functions)
                THEN 'granted' ELSE 'denied' END
      FROM pg_proc p
     WHERE p.pronamespace = 'public'::regnamespace
       AND p.prokind = 'f'
       AND p.prorettype <> 'trigger'::regtype
       AND p.proowner = (SELECT c.relowner FROM pg_class c
                          WHERE c.oid = 'public.publications'::regclass)
       AND EXISTS (SELECT 1 FROM role)

    -- each of them exists (a dropped guard would otherwise just look "denied")
    UNION ALL
    SELECT CASE WHEN to_regprocedure(g.signature) IS NOT NULL
                THEN 'PASS' ELSE 'FAIL' END,
           format('matrix.function.%s', split_part(g.signature, '(', 1)), 'function present'
      FROM guard_functions g

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
