"""Topic ideation over HTTP (SMM-035B3A).

Reads for every member: the project's ideation jobs and the ideas they
suggested. Owner commands: queuing a job, which is closed on every
deployment until the feature is enabled
(Settings.topic_ideation_jobs_enabled); rejecting an idea; and using one,
which queues exactly one generation job (SMM-035B3B2). All are behind the
read-only mutation gate like every other command. No route here touches
content, approvals or publications, or calls a model.

Every route is addressed under its project, so isolation is the project
filter of the query itself: a job or idea of another project answers 404,
exactly like one that does not exist.
"""
from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Response

from ai_smm.api.dependencies import (
    AppSettings,
    DbSession,
    OwnerProject,
    PageParams,
    ViewerProject,
)
from ai_smm.api.errors import ApiError, ErrorCode, not_found
from ai_smm.api.mutation_gate import mutations_disabled
from ai_smm.api.routers.generation import _job_out as _generation_job_out
from ai_smm.api.schemas import (
    ContentIdeaList,
    ContentIdeaOut,
    ContentIdeaUseOut,
    TopicIdeationJobCreate,
    TopicIdeationJobList,
    TopicIdeationJobOut,
)
from ai_smm.application.control_plane import MutationsDisabled
from ai_smm.application.generation import (
    GenerationUnavailable,
    InvalidGenerationParams,
)
from ai_smm.application.topic_ideation import (
    IdeaLocked,
    IdeaUnavailable,
    InvalidIdeaTransition,
    InvalidTopicIdeationParams,
    TopicIdeationDisabled,
    TopicIdeationProjectInactive,
    create_topic_ideation_job,
    get_topic_ideation_job,
    list_content_ideas_page,
    list_topic_ideation_jobs_page,
    reject_content_idea,
    use_content_idea,
)
from ai_smm.db.models import (
    ContentIdea,
    ContentIdeaStatus,
    TopicIdeationJob,
    TopicIdeationJobStatus,
)


router = APIRouter(tags=["topic ideation"])

JobStatusFilter = Annotated[
    list[TopicIdeationJobStatus] | None, Query(alias="status")
]
IdeaStatusFilter = Annotated[list[ContentIdeaStatus] | None, Query(alias="status")]


@router.get(
    "/projects/{project_id}/topic-ideation-jobs",
    response_model=TopicIdeationJobList,
    summary="Topic ideation jobs of one project (viewer and above)",
)
def list_jobs(
    db: DbSession,
    page: PageParams,
    context: ViewerProject,
    status: JobStatusFilter = None,
) -> TopicIdeationJobList:
    result = list_topic_ideation_jobs_page(
        db,
        project_id=context.project.id,
        statuses=status,
        limit=page.limit,
        offset=page.offset,
    )

    return TopicIdeationJobList(
        items=[_job_out(row) for row in result.items],
        total=result.total,
        limit=result.limit,
        offset=result.offset,
    )


@router.get(
    "/projects/{project_id}/topic-ideation-jobs/{job_id}",
    response_model=TopicIdeationJobOut,
    summary="One topic ideation job (viewer and above)",
)
def get_job(
    job_id: uuid.UUID, db: DbSession, context: ViewerProject
) -> TopicIdeationJobOut:
    job = get_topic_ideation_job(db, project_id=context.project.id, job_id=job_id)

    if job is None:
        raise not_found("Topic ideation job not found.")

    return _job_out(job)


@router.post(
    "/projects/{project_id}/topic-ideation-jobs",
    response_model=TopicIdeationJobOut,
    status_code=202,
    summary=(
        "Queue a topic ideation job (owner); disabled on every deployment "
        "until an ideation worker exists"
    ),
)
def create_job(
    payload: TopicIdeationJobCreate,
    db: DbSession,
    settings: AppSettings,
    context: OwnerProject,
) -> TopicIdeationJobOut:
    try:
        job = create_topic_ideation_job(
            db,
            project=context.project,
            requested_by_user=context.authenticated.user,
            mutations_enabled=settings.api_mutations_enabled,
            ideation_enabled=settings.topic_ideation_jobs_enabled,
            idea_count=payload.idea_count,
            topic_hint=payload.topic_hint,
        )
    except MutationsDisabled as exc:
        raise mutations_disabled(exc.command) from exc
    except TopicIdeationDisabled as exc:
        raise ApiError(
            status_code=403,
            code=ErrorCode.FEATURE_DISABLED,
            message=str(exc) + " Nothing was changed.",
            details={"feature": "topic_ideation"},
        ) from exc
    except TopicIdeationProjectInactive as exc:
        raise ApiError(
            status_code=409,
            code=ErrorCode.INVALID_STATE_TRANSITION,
            message=str(exc),
            details={"reason": "project_inactive"},
        ) from exc
    except InvalidTopicIdeationParams as exc:
        raise ApiError(
            status_code=422, code=ErrorCode.VALIDATION_ERROR, message=str(exc)
        ) from exc

    return _job_out(job)


@router.get(
    "/projects/{project_id}/content-ideas",
    response_model=ContentIdeaList,
    summary="Suggested topics of one project (viewer and above)",
)
def list_ideas(
    db: DbSession,
    page: PageParams,
    context: ViewerProject,
    status: IdeaStatusFilter = None,
    ideation_job_id: uuid.UUID | None = None,
) -> ContentIdeaList:
    result = list_content_ideas_page(
        db,
        project_id=context.project.id,
        statuses=status,
        ideation_job_id=ideation_job_id,
        limit=page.limit,
        offset=page.offset,
    )

    return ContentIdeaList(
        items=[_idea_out(row) for row in result.items],
        total=result.total,
        limit=result.limit,
        offset=result.offset,
    )


@router.post(
    "/projects/{project_id}/content-ideas/{idea_id}/reject",
    response_model=ContentIdeaOut,
    summary="Reject a proposed topic; no job is created (owner)",
)
def reject_idea(
    idea_id: uuid.UUID,
    db: DbSession,
    settings: AppSettings,
    context: OwnerProject,
) -> ContentIdeaOut:
    idea = _idea_command(
        lambda: reject_content_idea(
            db,
            project=context.project,
            idea_id=idea_id,
            actor_user=context.authenticated.user,
            mutations_enabled=settings.api_mutations_enabled,
        )
    )

    return _idea_out(idea)


@router.post(
    "/projects/{project_id}/content-ideas/{idea_id}/use",
    response_model=ContentIdeaUseOut,
    status_code=202,
    summary=(
        "Use a proposed topic: queue exactly one generation job for it "
        "(owner); 200 with the same job when it was already used"
    ),
)
def use_idea(
    idea_id: uuid.UUID,
    response: Response,
    db: DbSession,
    settings: AppSettings,
    context: OwnerProject,
) -> ContentIdeaUseOut:
    used = _idea_command(
        lambda: use_content_idea(
            db,
            project=context.project,
            idea_id=idea_id,
            actor_user=context.authenticated.user,
            mutations_enabled=settings.api_mutations_enabled,
            max_attempts=settings.generation_max_attempts,
        )
    )

    if used.replayed:
        response.status_code = 200

    return ContentIdeaUseOut(
        idea=_idea_out(used.idea),
        generation_job_id=used.job.id,
        generation_job=_generation_job_out(used.job),
        replayed=used.replayed,
        topic_includes=list(used.topic_includes),
    )


# -- translation ---------------------------------------------------------


def _idea_command(command):
    try:
        return command()
    except MutationsDisabled as exc:
        raise mutations_disabled(exc.command) from exc
    except (IdeaUnavailable, GenerationUnavailable) as exc:
        # Another project's idea reads exactly like a missing one.
        raise not_found("Topic idea not found.") from exc
    except InvalidIdeaTransition as exc:
        raise ApiError(
            status_code=409,
            code=ErrorCode.INVALID_STATE_TRANSITION,
            message=str(exc),
            details={"command": exc.command, "status": exc.status.value},
        ) from exc
    except IdeaLocked as exc:
        raise ApiError(
            status_code=503,
            code=ErrorCode.DEPENDENCY_UNAVAILABLE,
            message="This idea is being changed; retry in a moment.",
            details={"reason": "idea_locked"},
            headers={"Retry-After": "2"},
        ) from exc
    except InvalidGenerationParams as exc:
        raise ApiError(
            status_code=422, code=ErrorCode.VALIDATION_ERROR, message=str(exc)
        ) from exc


def _job_out(job: TopicIdeationJob) -> TopicIdeationJobOut:
    return TopicIdeationJobOut(
        id=job.id,
        project_id=job.project_id,
        requested_by_user_id=job.requested_by_user_id,
        status=job.status,
        idea_count=job.idea_count,
        topic_hint=job.topic_hint,
        result=dict(job.result or {}),
        last_error=job.last_error,
        attempt_count=job.attempt_count,
        max_attempts=job.max_attempts,
        created_at=job.created_at,
        updated_at=job.updated_at,
        finished_at=job.finished_at,
    )


def _idea_out(idea: ContentIdea) -> ContentIdeaOut:
    return ContentIdeaOut(
        id=idea.id,
        project_id=idea.project_id,
        ideation_job_id=idea.ideation_job_id,
        position=idea.position,
        title=idea.title,
        description=idea.description,
        angle=idea.angle,
        status=idea.status,
        generation_job_id=idea.generation_job_id,
        created_at=idea.created_at,
        updated_at=idea.updated_at,
    )
