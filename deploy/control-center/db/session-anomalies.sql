-- Sessions that the API's own login did not produce, as far as the data
-- can tell (SMM-024C-2A-FIX). Read-only; a DETECTIVE control, not a
-- preventive one.
--
-- Why it exists: ai_smm_cc must be able to INSERT into user_sessions, so
-- whoever controls the API's connection can create a session for ANY user,
-- with a token they know and any expiry. Neither the foreign key (the user
-- must exist) nor the unique token_hash prevents that. A login made by the
-- application always leaves a matching pair -- the session row and an
-- auth.login audit row naming it -- with timestamps the server can vouch
-- for (audit_log.at is the database's now(), not settable by the API).
-- This query lists live sessions that break that pattern.
--
-- It catches a forged session that is not accompanied by a matching,
-- plausible auth.login row. It does NOT catch one that is: anyone who
-- controls the API's connection can write both rows (a now-dated session
-- within the lifetime plus a fresh auth.login entry). Preventing that
-- needs a migration (docs/least-privilege-role.md, §4.1).
--
-- Run as the instance's superuser (or any role that can read both tables),
-- connected to the application database:
--
--   PGOPTIONS='-c default_transaction_read_only=on' \
--     psql -X -v ON_ERROR_STOP=1 -d ai_smm -tA -f session-anomalies.sql
--
-- Output: one line per suspicious live session,
--   ANOMALY|<session id>|<user id>|<reasons, comma-separated>
-- then always `END|anomalies|<n>`. Expected: `END|anomalies|0`.
--
-- 7 days / 24 hours are the API's defaults (AI_SMM_API_SESSION_LIFETIME_
-- SECONDS, AI_SMM_API_SESSION_IDLE_SECONDS); production does not override
-- them. Change both literals below if it ever does.

WITH live AS (
    SELECT s.*
      FROM user_sessions s
     WHERE s.revoked_at IS NULL
       AND s.expires_at > now()
),
flagged AS (
    SELECT l.id, l.user_id,
           concat_ws(',',
               CASE WHEN l.expires_at - l.created_at > interval '7 days' + interval '1 minute'
                    THEN 'lifetime_over_maximum' END,
               CASE WHEN l.created_at > now() + interval '5 minutes'
                    THEN 'created_in_the_future' END,
               CASE WHEN l.last_seen_at > now() + interval '5 minutes'
                    THEN 'seen_in_the_future' END,
               CASE WHEN l.last_seen_at < l.created_at - interval '1 minute'
                    THEN 'seen_before_created' END,
               CASE WHEN NOT EXISTS (
                        SELECT 1
                          FROM audit_log a
                         WHERE a.action = 'auth.login'
                           AND a.subject = 'user:' || l.user_id
                           AND a.details ->> 'session_id' = l.id::text
                    )
                    THEN 'no_login_audit' END,
               CASE WHEN EXISTS (
                        SELECT 1
                          FROM audit_log a
                         WHERE a.action = 'auth.login'
                           AND a.details ->> 'session_id' = l.id::text
                           AND abs(extract(epoch FROM a.at - l.created_at)) > 300
                    )
                    THEN 'audit_time_mismatch' END,
               CASE WHEN NOT u.is_active THEN 'user_inactive' END
           ) AS reasons
      FROM live l
      JOIN users u ON u.id = l.user_id
)
SELECT line
  FROM (
      SELECT 'ANOMALY|' || id || '|' || user_id || '|' || reasons AS line, 0 AS rank
        FROM flagged
       WHERE reasons <> ''
      UNION ALL
      SELECT 'END|anomalies|' || count(*), 1
        FROM flagged
       WHERE reasons <> ''
  ) ordered
 ORDER BY rank, line;
