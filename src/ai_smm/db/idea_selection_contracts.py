"""Narrow PostgreSQL contracts for choosing a suggested topic (SMM-035B3B2).

An owner may reject a `proposed` idea or use it for one generation job.
content_ideas is not writable by the Control Center's business role
(guard_topic_ideation_write refuses it), so the two commands are
SECURITY DEFINER functions, like the topic_ideation_* contracts the
ideation worker calls -- but with the opposite caller: EXECUTE goes to the
business role (deploy/control-center/db/ai_smm_cc_rw.grants.sql) and to
nobody else, and every function first demands what the guards demand of a
direct write: a transaction bound to a live session whose user OWNS the
named project (guard_require_owner). The project comes from the argument
and is matched against the row; an idea of another project is
indistinguishable from one that does not exist.

Using an idea is two calls in the API's one transaction, because the
generation job is inserted by the ordinary guarded path
(ai_smm.application.generation.create_generation_job) and not by SQL here:

1. content_idea_begin_use locks the idea row and returns it;
2. content_idea_finish_use, after the job exists, marks the idea used and
   links the job.

The row lock taken by (1) is held to COMMIT, so a concurrent use or reject
of the same idea waits and then sees the terminal state. The schema backs
it up independently of these functions: a composite foreign key keeps the
linked job in the idea's project, a unique constraint lets a job be linked
once, and a check ties status = 'used' to a linked job.

Each function is SECURITY DEFINER with a fixed search_path and fully
qualified objects; EXECUTE is revoked from PUBLIC by the migration.
"""
from __future__ import annotations

from ai_smm.db.guards import GuardFunction


_HEADER = """LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
"""

#: A state that does not allow the command (the application checks first;
#: this is the backstop for a caller that skips it).
STATE_ERRCODE = "55000"


_BEGIN_USE = f"""
CREATE OR REPLACE FUNCTION public.content_idea_begin_use(
    p_project_id text, p_idea_id uuid
) RETURNS TABLE (
    idea_id uuid, idea_status text, title text, description text,
    angle text, generation_job_id uuid
)
{_HEADER}AS $contract$
DECLARE
    v_idea public.content_ideas%ROWTYPE;
BEGIN
    PERFORM public.guard_require_owner(p_project_id, 'using a topic idea');

    SELECT * INTO v_idea FROM public.content_ideas AS i
     WHERE i.id = p_idea_id AND i.project_id = p_project_id
       FOR UPDATE;

    IF NOT FOUND THEN
        RETURN;
    END IF;

    RETURN QUERY
    SELECT v_idea.id, v_idea.status::text, v_idea.title, v_idea.description,
           v_idea.angle, v_idea.generation_job_id;
END
$contract$
"""


_FINISH_USE = f"""
CREATE OR REPLACE FUNCTION public.content_idea_finish_use(
    p_project_id text, p_idea_id uuid, p_generation_job_id uuid
) RETURNS text
{_HEADER}AS $contract$
DECLARE
    v_user uuid;
    v_idea public.content_ideas%ROWTYPE;
BEGIN
    v_user := public.guard_require_owner(p_project_id, 'using a topic idea');

    IF p_generation_job_id IS NULL THEN
        RAISE EXCEPTION 'idea contract: a generation job is required'
            USING ERRCODE = '22023';
    END IF;

    SELECT * INTO v_idea FROM public.content_ideas AS i
     WHERE i.id = p_idea_id AND i.project_id = p_project_id
       FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'idea contract: idea not found' USING ERRCODE = 'P0002';
    END IF;

    IF v_idea.status <> 'proposed' THEN
        RAISE EXCEPTION 'idea contract: idea % is %, not proposed',
            v_idea.id, v_idea.status USING ERRCODE = '{STATE_ERRCODE}';
    END IF;

    -- The job is the caller's own, in this project, and has not started:
    -- an idea is never linked to someone else's job or to one a worker
    -- already holds. (The composite foreign key re-checks the project.)
    IF NOT EXISTS (
        SELECT 1 FROM public.content_generation_jobs AS j
         WHERE j.id = p_generation_job_id
           AND j.project_id = p_project_id
           AND j.requested_by_user_id = v_user
           AND j.status = 'queued'
           AND j.attempt_count = 0
    ) THEN
        RAISE EXCEPTION 'idea contract: not a fresh generation job of this project'
            USING ERRCODE = '22023';
    END IF;

    UPDATE public.content_ideas AS i
       SET status = 'used', generation_job_id = p_generation_job_id,
           updated_at = now()
     WHERE i.id = v_idea.id;

    RETURN 'used';
END
$contract$
"""


# Returns the status the idea had when it was locked: 'proposed' means it
# has just been rejected; 'rejected' or 'used' means nothing changed;
# NULL means there is no such idea in the project.
_REJECT = f"""
CREATE OR REPLACE FUNCTION public.content_idea_reject(
    p_project_id text, p_idea_id uuid
) RETURNS text
{_HEADER}AS $contract$
DECLARE
    v_idea public.content_ideas%ROWTYPE;
BEGIN
    PERFORM public.guard_require_owner(p_project_id, 'rejecting a topic idea');

    SELECT * INTO v_idea FROM public.content_ideas AS i
     WHERE i.id = p_idea_id AND i.project_id = p_project_id
       FOR UPDATE;

    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    IF v_idea.status <> 'proposed' THEN
        RETURN v_idea.status::text;
    END IF;

    UPDATE public.content_ideas AS i
       SET status = 'rejected', updated_at = now()
     WHERE i.id = v_idea.id;

    RETURN 'proposed';
END
$contract$
"""


IDEA_SELECTION_CONTRACTS: tuple[GuardFunction, ...] = (
    GuardFunction(
        "content_idea_begin_use",
        "text, uuid",
        _BEGIN_USE,
        callable_by_business_role=True,
    ),
    GuardFunction(
        "content_idea_finish_use",
        "text, uuid, uuid",
        _FINISH_USE,
        callable_by_business_role=True,
    ),
    GuardFunction(
        "content_idea_reject",
        "text, uuid",
        _REJECT,
        callable_by_business_role=True,
    ),
)
