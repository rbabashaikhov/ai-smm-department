"""Narrow PostgreSQL contracts for a future topic-ideation worker (SMM-035B3A).

Independent of ai_smm.db.generation_contracts: their own queue
(topic_ideation_jobs), their own functions, and nothing here changes what
generation_claim/complete/fail do or who may call them. A content
generation worker can therefore never claim an ideation job, and an
ideation worker can never write a draft.

Each function is SECURITY DEFINER, has a fixed search_path, uses fully
qualified objects, and derives project and provenance from the locked job
-- never from its arguments. EXECUTE is taken away from PUBLIC and granted
to no role yet: the worker that will call these, and its role, are a later
stage. Until then only the table owner (migrations, tests) can run them.
"""
from __future__ import annotations

from ai_smm.db.guards import GuardFunction


MAX_IDEATION_LEASE_SECONDS = 3600
MAX_IDEATION_BACKOFF_SECONDS = 3600

#: Requested ideas per job; also the ceiling of a job's idea_count column.
MIN_IDEAS_PER_JOB = 1
MAX_IDEAS_PER_JOB = 5

MAX_TOPIC_HINT_LENGTH = 500
MAX_IDEA_TITLE_LENGTH = 200
MAX_IDEA_DESCRIPTION_LENGTH = 2000
MAX_IDEA_ANGLE_LENGTH = 1000
#: Serialised size of the worker's result object (usage, model, ...).
MAX_IDEATION_RESULT_BYTES = 16 * 1024
MAX_IDEATION_ERROR_LENGTH = 4000

#: The only keys an idea in a completion payload may carry. Project, job,
#: position and status are the database's to decide.
IDEA_PAYLOAD_KEYS = ("title", "description", "angle")


_HEADER = """LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
"""

# Leading and trailing whitespace of every kind, not only spaces (btrim).
_TRIM = "regexp_replace({}, '^[[:space:]]+|[[:space:]]+$', '', 'g')"


_CLAIM = f"""
CREATE OR REPLACE FUNCTION public.topic_ideation_claim(
    p_worker_id text, p_lease_seconds integer
) RETURNS TABLE (
    id uuid, project_id text, requested_by_user_id uuid, status text,
    idea_count integer, topic_hint text, attempt_count integer,
    max_attempts integer, claimed_by text, claimed_at timestamptz,
    lease_expires_at timestamptz
)
{_HEADER}AS $contract$
DECLARE
    v_job_id uuid;
BEGIN
    IF p_worker_id IS NULL OR btrim(p_worker_id) = '' OR length(p_worker_id) > 120
       OR p_lease_seconds IS NULL
       OR p_lease_seconds NOT BETWEEN 1 AND {MAX_IDEATION_LEASE_SECONDS} THEN
        RAISE EXCEPTION 'ideation contract: invalid claim parameters'
            USING ERRCODE = '22023';
    END IF;

    -- Recovery first. A claim consumes one attempt, so an expired final
    -- claim is terminal and can never be billed again after a crash.
    UPDATE public.topic_ideation_jobs AS j
       SET status = 'failed', claimed_by = NULL, claimed_at = NULL,
           lease_expires_at = NULL, next_attempt_at = NULL,
           last_error = coalesce(
               j.last_error, 'ideation lease expired after final attempt'),
           updated_at = now(), finished_at = now()
     WHERE j.status = 'running' AND j.lease_expires_at <= now()
       AND j.attempt_count >= j.max_attempts;

    UPDATE public.topic_ideation_jobs AS j
       SET status = 'queued', claimed_by = NULL, claimed_at = NULL,
           lease_expires_at = NULL, next_attempt_at = NULL, updated_at = now()
     WHERE j.status = 'running' AND j.lease_expires_at <= now()
       AND j.attempt_count < j.max_attempts;

    UPDATE public.topic_ideation_jobs AS j
       SET status = 'failed', next_attempt_at = NULL,
           last_error = coalesce(j.last_error, 'ideation attempts exhausted'),
           updated_at = now(), finished_at = now()
     WHERE j.status = 'queued' AND j.attempt_count >= j.max_attempts;

    SELECT j.id INTO v_job_id
      FROM public.topic_ideation_jobs AS j
     WHERE j.status = 'queued'
       AND j.attempt_count < j.max_attempts
       AND (j.next_attempt_at IS NULL OR j.next_attempt_at <= now())
     ORDER BY j.created_at, j.id
     LIMIT 1 FOR UPDATE SKIP LOCKED;

    IF v_job_id IS NULL THEN
        RETURN;
    END IF;

    UPDATE public.topic_ideation_jobs AS j
       SET status = 'running', claimed_by = p_worker_id, claimed_at = now(),
           lease_expires_at = now() + make_interval(secs => p_lease_seconds),
           attempt_count = j.attempt_count + 1, next_attempt_at = NULL,
           updated_at = now()
     WHERE j.id = v_job_id;

    INSERT INTO public.audit_log (actor, action, subject, details)
    VALUES ('worker:' || p_worker_id, 'generation.ideation_claimed',
            'topic_ideation_job:' || v_job_id::text,
            jsonb_build_object('lease_seconds', p_lease_seconds));

    RETURN QUERY
    SELECT j.id, j.project_id::text, j.requested_by_user_id, j.status::text,
           j.idea_count, j.topic_hint, j.attempt_count, j.max_attempts,
           j.claimed_by::text, j.claimed_at, j.lease_expires_at
      FROM public.topic_ideation_jobs AS j WHERE j.id = v_job_id;
END
$contract$
"""


_COMPLETE = f"""
CREATE OR REPLACE FUNCTION public.topic_ideation_complete(
    p_job_id uuid, p_worker_id text, p_ideas jsonb, p_result jsonb
) RETURNS jsonb
{_HEADER}AS $contract$
DECLARE
    v_job public.topic_ideation_jobs%ROWTYPE;
    v_idea jsonb;
    v_position integer;
    v_count integer;
    v_title text;
    v_description text;
    v_angle text;
    v_seen text[] := ARRAY[]::text[];
    v_idea_id uuid;
    v_ids jsonb := '[]'::jsonb;
BEGIN
    SELECT * INTO v_job FROM public.topic_ideation_jobs AS j
     WHERE j.id = p_job_id FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'ideation contract: job not found' USING ERRCODE = 'P0002';
    END IF;

    -- Cancellation or a newer claimant wins. A repeated completion is a
    -- no-op that reports what the first one saved: the duplicate backstop.
    IF v_job.status <> 'running' OR v_job.claimed_by IS DISTINCT FROM p_worker_id
       OR v_job.lease_expires_at <= now() THEN
        RETURN jsonb_build_object(
            'status', v_job.status::text,
            'idea_ids', coalesce((
                SELECT jsonb_agg(i.id::text ORDER BY i.position)
                  FROM public.content_ideas AS i
                 WHERE i.ideation_job_id = v_job.id), '[]'::jsonb));
    END IF;

    -- Every check below raises, which aborts the whole transaction: a
    -- malformed payload never leaves some of its ideas behind.
    IF p_ideas IS NULL OR p_result IS NULL
       OR jsonb_typeof(p_ideas) <> 'array' OR jsonb_typeof(p_result) <> 'object'
       OR octet_length(p_result::text) > {MAX_IDEATION_RESULT_BYTES} THEN
        RAISE EXCEPTION 'ideation contract: ideas/result have invalid shape'
            USING ERRCODE = '22023';
    END IF;

    v_count := jsonb_array_length(p_ideas);
    IF v_count < 1 OR v_count > v_job.idea_count THEN
        RAISE EXCEPTION 'ideation contract: idea count is outside 1..%',
            v_job.idea_count USING ERRCODE = '22023';
    END IF;

    FOR v_idea, v_position IN
        SELECT e.value, e.ordinality::integer
          FROM jsonb_array_elements(p_ideas) WITH ORDINALITY AS e(value, ordinality)
    LOOP
        IF jsonb_typeof(v_idea) <> 'object'
           OR EXISTS (
               SELECT 1 FROM jsonb_object_keys(v_idea) AS k(key)
                WHERE k.key NOT IN ('title', 'description', 'angle'))
           OR jsonb_typeof(v_idea->'title') IS DISTINCT FROM 'string'
           OR jsonb_typeof(v_idea->'description') IS DISTINCT FROM 'string'
           OR coalesce(jsonb_typeof(v_idea->'angle'), 'null')
              NOT IN ('string', 'null') THEN
            RAISE EXCEPTION 'ideation contract: invalid idea payload'
                USING ERRCODE = '22023';
        END IF;

        v_title := {_TRIM.format("v_idea->>'title'")};
        v_description := {_TRIM.format("v_idea->>'description'")};
        v_angle := nullif({_TRIM.format("coalesce(v_idea->>'angle', '')")}, '');

        IF v_title = '' OR char_length(v_title) > {MAX_IDEA_TITLE_LENGTH}
           OR v_description = ''
           OR char_length(v_description) > {MAX_IDEA_DESCRIPTION_LENGTH}
           OR char_length(coalesce(v_angle, '')) > {MAX_IDEA_ANGLE_LENGTH} THEN
            RAISE EXCEPTION 'ideation contract: idea text is empty or too long'
                USING ERRCODE = '22023';
        END IF;

        IF lower(v_title) = ANY (v_seen) THEN
            RAISE EXCEPTION 'ideation contract: duplicate idea title'
                USING ERRCODE = '22023';
        END IF;
        v_seen := v_seen || lower(v_title);

        -- Project and job come from the locked row, never from the payload.
        v_idea_id := gen_random_uuid();
        INSERT INTO public.content_ideas
            (id, project_id, ideation_job_id, position, title, description,
             angle, status, generation_job_id, created_at, updated_at)
        VALUES
            (v_idea_id, v_job.project_id, v_job.id, v_position, v_title,
             v_description, v_angle, 'proposed', NULL, now(), now());

        v_ids := v_ids || jsonb_build_array(v_idea_id::text);
    END LOOP;

    UPDATE public.topic_ideation_jobs AS j
       SET status = 'succeeded', claimed_by = NULL, claimed_at = NULL,
           lease_expires_at = NULL, next_attempt_at = NULL, last_error = NULL,
           result = p_result || jsonb_build_object('idea_ids', v_ids),
           updated_at = now(), finished_at = now()
     WHERE j.id = v_job.id;

    INSERT INTO public.audit_log (actor, action, subject, details)
    VALUES ('worker:' || p_worker_id, 'generation.ideation_succeeded',
            'topic_ideation_job:' || v_job.id::text,
            jsonb_build_object('project_id', v_job.project_id,
                               'idea_ids', v_ids));

    RETURN jsonb_build_object('status', 'succeeded', 'idea_ids', v_ids);
END
$contract$
"""


_FAIL = f"""
CREATE OR REPLACE FUNCTION public.topic_ideation_fail(
    p_job_id uuid, p_worker_id text, p_reason text, p_retryable boolean,
    p_error_type text, p_backoff_base_seconds integer,
    p_backoff_cap_seconds integer
) RETURNS text
{_HEADER}AS $contract$
DECLARE
    v_job public.topic_ideation_jobs%ROWTYPE;
    v_delay integer;
BEGIN
    IF p_retryable IS NULL
       OR p_backoff_base_seconds IS NULL OR p_backoff_cap_seconds IS NULL
       OR p_backoff_base_seconds NOT BETWEEN 1 AND {MAX_IDEATION_BACKOFF_SECONDS}
       OR p_backoff_cap_seconds NOT BETWEEN 1 AND {MAX_IDEATION_BACKOFF_SECONDS}
       OR p_backoff_base_seconds > p_backoff_cap_seconds THEN
        RAISE EXCEPTION 'ideation contract: invalid retry parameters'
            USING ERRCODE = '22023';
    END IF;

    SELECT * INTO v_job FROM public.topic_ideation_jobs AS j
     WHERE j.id = p_job_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ideation contract: job not found' USING ERRCODE = 'P0002';
    END IF;
    IF v_job.status <> 'running' OR v_job.claimed_by IS DISTINCT FROM p_worker_id
       OR v_job.lease_expires_at <= now() THEN
        RETURN v_job.status::text;
    END IF;

    IF NOT p_retryable OR v_job.attempt_count >= v_job.max_attempts THEN
        UPDATE public.topic_ideation_jobs AS j
           SET status = 'failed', claimed_by = NULL, claimed_at = NULL,
               lease_expires_at = NULL, next_attempt_at = NULL,
               last_error = left(coalesce(p_reason, 'unknown error'),
                                 {MAX_IDEATION_ERROR_LENGTH}),
               updated_at = now(), finished_at = now()
         WHERE j.id = v_job.id;
        INSERT INTO public.audit_log (actor, action, subject, details)
        VALUES ('worker:' || p_worker_id, 'generation.ideation_failed',
                'topic_ideation_job:' || v_job.id::text,
                jsonb_build_object('attempt_count', v_job.attempt_count,
                                   'max_attempts', v_job.max_attempts,
                                   'error_type', left(p_error_type, 120),
                                   'retryable', p_retryable));
        RETURN 'failed';
    END IF;

    v_delay := least(
        p_backoff_base_seconds * (2 ^ (v_job.attempt_count - 1))::integer,
        p_backoff_cap_seconds);
    UPDATE public.topic_ideation_jobs AS j
       SET status = 'queued', claimed_by = NULL, claimed_at = NULL,
           lease_expires_at = NULL,
           next_attempt_at = now() + make_interval(secs => v_delay),
           last_error = left(coalesce(p_reason, 'unknown error'),
                             {MAX_IDEATION_ERROR_LENGTH}),
           updated_at = now()
     WHERE j.id = v_job.id;
    INSERT INTO public.audit_log (actor, action, subject, details)
    VALUES ('worker:' || p_worker_id, 'generation.ideation_retry_scheduled',
            'topic_ideation_job:' || v_job.id::text,
            jsonb_build_object('delay_seconds', v_delay,
                               'attempt_count', v_job.attempt_count,
                               'max_attempts', v_job.max_attempts,
                               'error_type', left(p_error_type, 120)));
    RETURN 'queued';
END
$contract$
"""


TOPIC_IDEATION_CONTRACTS: tuple[GuardFunction, ...] = (
    GuardFunction("topic_ideation_claim", "text, integer", _CLAIM),
    GuardFunction("topic_ideation_complete", "uuid, text, jsonb, jsonb", _COMPLETE),
    GuardFunction(
        "topic_ideation_fail",
        "uuid, text, text, boolean, text, integer, integer",
        _FAIL,
    ),
)
