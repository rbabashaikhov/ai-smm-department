"""Narrow PostgreSQL contracts used by the unprivileged generation worker.

The worker role receives EXECUTE on these functions and no table or sequence
privileges.  Each function is SECURITY DEFINER, has a fixed search_path, uses
fully-qualified objects, and derives project/provenance from the locked job.
"""
from __future__ import annotations

from ai_smm.db.guards import GuardFunction


_HEADER = """LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
"""


_CLAIM = f"""
CREATE OR REPLACE FUNCTION public.generation_claim(
    p_worker_id text, p_lease_seconds integer, p_max_attempts integer
) RETURNS TABLE (
    id uuid, project_id text, requested_by_user_id uuid, status text,
    params jsonb, attempt_count integer, claimed_by text,
    claimed_at timestamptz, lease_expires_at timestamptz
)
{_HEADER}AS $contract$
DECLARE
    v_job_id uuid;
BEGIN
    IF p_worker_id IS NULL OR btrim(p_worker_id) = '' OR length(p_worker_id) > 120
       OR p_lease_seconds < 1 OR p_max_attempts < 1 THEN
        RAISE EXCEPTION 'generation contract: invalid claim parameters'
            USING ERRCODE = '22023';
    END IF;

    -- A claim consumes one attempt.  Therefore an expired final claim is
    -- terminal and can never be made billable again after crash recovery.
    UPDATE public.content_generation_jobs AS j
       SET status = 'failed', claimed_by = NULL, claimed_at = NULL,
           lease_expires_at = NULL, next_attempt_at = NULL,
           last_error = coalesce(
               j.last_error, 'generation lease expired after final attempt'),
           updated_at = now(), finished_at = now()
     WHERE j.status = 'claimed' AND j.lease_expires_at <= now()
       AND j.attempt_count >= p_max_attempts;

    UPDATE public.content_generation_jobs AS j
       SET status = 'queued', claimed_by = NULL, claimed_at = NULL,
           lease_expires_at = NULL, next_attempt_at = NULL, updated_at = now()
     WHERE j.status = 'claimed' AND j.lease_expires_at <= now()
       AND j.attempt_count < p_max_attempts;

    UPDATE public.content_generation_jobs AS j
       SET status = 'failed', next_attempt_at = NULL,
           last_error = coalesce(j.last_error, 'generation attempts exhausted'),
           updated_at = now(), finished_at = now()
     WHERE j.status = 'queued' AND j.attempt_count >= p_max_attempts;

    SELECT j.id INTO v_job_id
      FROM public.content_generation_jobs AS j
     WHERE j.status = 'queued'
       AND j.attempt_count < p_max_attempts
       AND (j.next_attempt_at IS NULL OR j.next_attempt_at <= now())
     ORDER BY j.created_at, j.id
     LIMIT 1 FOR UPDATE SKIP LOCKED;

    IF v_job_id IS NULL THEN
        RETURN;
    END IF;

    UPDATE public.content_generation_jobs AS j
       SET status = 'claimed', claimed_by = p_worker_id, claimed_at = now(),
           lease_expires_at = now() + make_interval(secs => p_lease_seconds),
           attempt_count = j.attempt_count + 1, next_attempt_at = NULL,
           updated_at = now()
     WHERE j.id = v_job_id;

    INSERT INTO public.audit_log (actor, action, subject, details)
    VALUES ('worker:' || p_worker_id, 'generation.claimed',
            'generation_job:' || v_job_id::text,
            jsonb_build_object('lease_seconds', p_lease_seconds));

    RETURN QUERY
    SELECT j.id, j.project_id::text, j.requested_by_user_id, j.status::text,
           j.params, j.attempt_count, j.claimed_by::text,
           j.claimed_at, j.lease_expires_at
      FROM public.content_generation_jobs AS j WHERE j.id = v_job_id;
END
$contract$
"""


_COMPLETE = f"""
CREATE OR REPLACE FUNCTION public.generation_complete(
    p_job_id uuid, p_worker_id text, p_run_id uuid,
    p_drafts jsonb, p_result jsonb
) RETURNS jsonb
{_HEADER}AS $contract$
DECLARE
    v_job public.content_generation_jobs%ROWTYPE;
    v_draft jsonb;
    v_item_id uuid;
    v_revision_id uuid;
    v_ids jsonb := '[]'::jsonb;
    v_metadata jsonb;
    v_count integer;
    v_canonical text;
    v_computed_hash text;
BEGIN
    SELECT * INTO v_job FROM public.content_generation_jobs AS j
     WHERE j.id = p_job_id FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'generation contract: job not found' USING ERRCODE = 'P0002';
    END IF;

    -- Cancellation or a newer claimant wins. Repeated completion is a
    -- no-op, which is the duplicate-draft backstop.
    IF v_job.status <> 'claimed' OR v_job.claimed_by IS DISTINCT FROM p_worker_id
       OR v_job.lease_expires_at <= now() THEN
        RETURN jsonb_build_object(
            'status', v_job.status::text,
            'content_item_ids',
            coalesce(v_job.result->'content_item_ids', '[]'::jsonb));
    END IF;

    IF jsonb_typeof(p_drafts) <> 'array' OR jsonb_typeof(p_result) <> 'object' THEN
        RAISE EXCEPTION 'generation contract: drafts/result have invalid shape'
            USING ERRCODE = '22023';
    END IF;
    v_count := jsonb_array_length(p_drafts);
    IF v_count < 1 OR v_count > 20 THEN
        RAISE EXCEPTION 'generation contract: draft count is outside 1..20'
            USING ERRCODE = '22023';
    END IF;

    FOR v_draft IN SELECT value FROM jsonb_array_elements(p_drafts)
    LOOP
        IF jsonb_typeof(v_draft) <> 'object'
           OR coalesce(v_draft->>'title', '') !~ '[^[:space:]]'
           OR coalesce(v_draft->>'body', '') !~ '[^[:space:]]'
           OR coalesce(v_draft->>'format', '') NOT IN
              ('text', 'image', 'carousel', 'thread')
           OR coalesce(v_draft->>'content_hash', '') !~ '^[0-9a-f]{{64}}$'
           OR jsonb_typeof(coalesce(v_draft->'images', '[]'::jsonb)) <> 'array'
           OR jsonb_typeof(coalesce(v_draft->'items', '[]'::jsonb)) <> 'array'
           OR coalesce(v_draft->'images', '[]'::jsonb) <> '[]'::jsonb
           OR coalesce(v_draft->'items', '[]'::jsonb) <> '[]'::jsonb
           OR jsonb_typeof(coalesce(
                  v_draft->'metadata', '{{}}'::jsonb)) <> 'object' THEN
            RAISE EXCEPTION 'generation contract: invalid draft payload'
                USING ERRCODE = '22023';
        END IF;

        -- ApprovalLineage uses Python json.dumps(sort_keys=True,
        -- ensure_ascii=False, separators=(',', ':')).  PostgreSQL jsonb text
        -- is not equivalent for every JSON number, so this contract accepts
        -- only the text-only revision shape the generation worker actually
        -- emits.  Constructing the ordered JSON here from JSON-quoted text
        -- makes the database, rather than the caller, the trusted source of
        -- the exact canonical bytes.  Rich media remains available through
        -- the ordinary human revision contract.
        v_canonical := '{{"body":' || to_json(v_draft->>'body')::text
            || ',"format":' || to_json(v_draft->>'format')::text
            || ',"images":[],"items":[],"title":' || 'null}}';

        v_computed_hash := encode(
            sha256(convert_to(v_canonical, 'UTF8')), 'hex');
        IF v_draft->>'content_hash' IS DISTINCT FROM v_computed_hash THEN
            RAISE EXCEPTION 'generation contract: content hash mismatch'
                USING ERRCODE = '22023';
        END IF;

        v_item_id := gen_random_uuid();
        v_revision_id := gen_random_uuid();
        v_metadata := coalesce(v_draft->'metadata', '{{}}'::jsonb)
            || jsonb_build_object('generation_job_id', v_job.id::text,
                                  'run_id', p_run_id::text);

        INSERT INTO public.content_items
            (id, project_id, title, content_type, status, current_revision_id,
             approved_revision_id, version, created_by_user_id,
             generation_job_id, created_at, updated_at, archived_at)
        VALUES
            (v_item_id, v_job.project_id, v_draft->>'title', 'post', 'draft',
             v_revision_id, NULL, 1, NULL, v_job.id, now(), now(), NULL);

        INSERT INTO public.content_revisions
            (id, content_item_id, revision_number, title, body, format,
             images, items, metadata, source, source_ref, editor_score,
             editor_notes, content_hash, social_account_id,
             created_by_user_id, created_at)
        VALUES
            (v_revision_id, v_item_id, 1, NULL, v_draft->>'body',
             v_draft->>'format', coalesce(v_draft->'images', '[]'::jsonb),
             coalesce(v_draft->'items', '[]'::jsonb), v_metadata, 'copywriter',
             'generation_job:' || v_job.id::text || ':run:' || p_run_id::text,
             NULLIF(v_draft->>'editor_score', '')::numeric,
             left(coalesce(v_draft->>'editor_notes', ''), 5000),
             v_computed_hash, NULL, NULL, now());

        INSERT INTO public.audit_log (actor, action, subject, details)
        VALUES ('worker:' || p_worker_id, 'content.created',
                'content_item:' || v_item_id::text,
                jsonb_build_object('project_id', v_job.project_id,
                                   'content_type', 'post',
                                   'revision_id', v_revision_id::text));
        INSERT INTO public.audit_log (actor, action, subject, details)
        VALUES ('worker:' || p_worker_id, 'content.revision_created',
                'content_item:' || v_item_id::text,
                jsonb_build_object('revision_id', v_revision_id::text,
                                   'revision_number', 1,
                                   'source', 'copywriter'));

        v_ids := v_ids || jsonb_build_array(v_item_id::text);
    END LOOP;

    p_result := p_result || jsonb_build_object('content_item_ids', v_ids);
    UPDATE public.content_generation_jobs AS j
       SET status = 'succeeded', claimed_by = NULL, claimed_at = NULL,
           lease_expires_at = NULL, next_attempt_at = NULL, last_error = NULL,
           result = p_result, updated_at = now(), finished_at = now()
     WHERE j.id = v_job.id;

    INSERT INTO public.audit_log (actor, action, subject, details)
    VALUES ('worker:' || p_worker_id, 'generation.succeeded',
            'generation_job:' || v_job.id::text,
            jsonb_build_object('content_item_ids', v_ids));

    RETURN jsonb_build_object('status', 'succeeded', 'content_item_ids', v_ids);
END
$contract$
"""


_FAIL = f"""
CREATE OR REPLACE FUNCTION public.generation_fail(
    p_job_id uuid, p_worker_id text, p_reason text, p_retryable boolean,
    p_max_attempts integer, p_error_type text,
    p_backoff_base_seconds integer, p_backoff_cap_seconds integer
) RETURNS text
{_HEADER}AS $contract$
DECLARE
    v_job public.content_generation_jobs%ROWTYPE;
    v_delay integer;
BEGIN
    IF p_max_attempts < 1 OR p_backoff_base_seconds < 1
       OR p_backoff_cap_seconds < 1 THEN
        RAISE EXCEPTION 'generation contract: invalid retry parameters'
            USING ERRCODE = '22023';
    END IF;

    SELECT * INTO v_job FROM public.content_generation_jobs AS j
     WHERE j.id = p_job_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'generation contract: job not found' USING ERRCODE = 'P0002';
    END IF;
    IF v_job.status <> 'claimed' OR v_job.claimed_by IS DISTINCT FROM p_worker_id
       OR v_job.lease_expires_at <= now() THEN
        RETURN v_job.status::text;
    END IF;

    IF NOT p_retryable OR v_job.attempt_count >= p_max_attempts THEN
        UPDATE public.content_generation_jobs AS j
           SET status = 'failed', claimed_by = NULL, claimed_at = NULL,
               lease_expires_at = NULL, next_attempt_at = NULL,
               last_error = left(p_reason, 4000), updated_at = now(),
               finished_at = now()
         WHERE j.id = v_job.id;
        INSERT INTO public.audit_log (actor, action, subject, details)
        VALUES ('worker:' || p_worker_id, 'generation.failed',
                'generation_job:' || v_job.id::text,
                jsonb_build_object('attempt_count', v_job.attempt_count,
                                   'max_attempts', p_max_attempts,
                                   'error_type', p_error_type,
                                   'retryable', p_retryable));
        RETURN 'failed';
    END IF;

    v_delay := least(p_backoff_base_seconds * (2 ^ (v_job.attempt_count - 1))::integer,
                     p_backoff_cap_seconds);
    UPDATE public.content_generation_jobs AS j
       SET status = 'queued', claimed_by = NULL, claimed_at = NULL,
           lease_expires_at = NULL,
           next_attempt_at = now() + make_interval(secs => v_delay),
           last_error = left(p_reason, 4000), updated_at = now()
     WHERE j.id = v_job.id;
    INSERT INTO public.audit_log (actor, action, subject, details)
    VALUES ('worker:' || p_worker_id, 'generation.retry_scheduled',
            'generation_job:' || v_job.id::text,
            jsonb_build_object('delay_seconds', v_delay,
                               'attempt_count', v_job.attempt_count,
                               'error_type', p_error_type));
    RETURN 'queued';
END
$contract$
"""


GENERATION_CONTRACTS: tuple[GuardFunction, ...] = (
    GuardFunction("generation_claim", "text, integer, integer", _CLAIM),
    GuardFunction("generation_complete", "uuid, text, uuid, jsonb, jsonb", _COMPLETE),
    GuardFunction(
        "generation_fail",
        "uuid, text, text, boolean, integer, text, integer, integer",
        _FAIL,
    ),
)
