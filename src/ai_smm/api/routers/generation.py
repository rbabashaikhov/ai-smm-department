"""Agent generation jobs over HTTP (SMM-026A/2.1).

Starts the agent pipeline (Strategist -> Copywriter -> Editor) for a
project and, once a generation worker has picked the job up, turns its
result into draft content items -- nothing more. There is no route here
that approves, materialises, schedules or publishes anything: that is
exactly the existing content and publications routers, unchanged.

Viewer and above read; owner creates and cancels, the same tier every
other content mutation already requires (ai_smm.api.dependencies).
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from ai_smm.api.dependencies import (
    AppSettings,
    DbSession,
    OwnerGenerationJob,
    OwnerProject,
    PageParams,
    ViewerGenerationJob,
    ViewerProject,
)
from ai_smm.api.errors import ApiError, ErrorCode, not_found
from ai_smm.api.mutation_gate import mutations_disabled
from ai_smm.api.schemas import (
    GenerationJobCreate,
    GenerationJobList,
    GenerationJobOut,
)
from ai_smm.application.control_plane import MutationsDisabled
from ai_smm.application.generation import (
    GenerationLocked,
    GenerationUnavailable,
    InvalidGenerationParams,
    InvalidGenerationTransition,
    cancel_generation_job,
    create_generation_job,
    list_generation_jobs_page,
)
from ai_smm.db.models import ContentGenerationJob, GenerationJobStatus


router = APIRouter(tags=["generation"])

StatusFilter = Annotated[
    list[GenerationJobStatus] | None, Query(alias="status")
]


@router.get(
    "/projects/{project_id}/generation-jobs",
    response_model=GenerationJobList,
    summary="Generation jobs of one project (viewer and above)",
)
def list_jobs(
    db: DbSession,
    page: PageParams,
    context: ViewerProject,
    status: StatusFilter = None,
) -> GenerationJobList:
    result = list_generation_jobs_page(
        db,
        project_id=context.project.id,
        statuses=status,
        limit=page.limit,
        offset=page.offset,
    )

    return GenerationJobList(
        items=[_job_out(row) for row in result.items],
        total=result.total,
        limit=result.limit,
        offset=result.offset,
    )


@router.get(
    "/generation-jobs/{job_id}",
    response_model=GenerationJobOut,
    summary="One generation job (viewer and above)",
)
def get_job(context: ViewerGenerationJob) -> GenerationJobOut:
    return _job_out(context.job)


@router.post(
    "/projects/{project_id}/generation-jobs",
    response_model=GenerationJobOut,
    status_code=202,
    summary=(
        "Queue an agent-generation job; drafts appear once a generation "
        "worker picks it up (owner)"
    ),
)
def create_job(
    payload: GenerationJobCreate,
    db: DbSession,
    settings: AppSettings,
    context: OwnerProject,
) -> GenerationJobOut:
    job = _run(
        lambda: create_generation_job(
            db,
            project_id=context.project.id,
            requested_by_user=context.authenticated.user,
            mutations_enabled=settings.api_mutations_enabled,
            series_size=payload.series_size,
            publishing_strategy=payload.publishing_strategy,
            topic_hint=payload.topic_hint,
        )
    )

    return _job_out(job)


@router.post(
    "/generation-jobs/{job_id}/cancel",
    response_model=GenerationJobOut,
    summary=(
        "Cancel a job that has not finished; a claimed job's in-flight "
        "LLM calls still run to completion, but their result is "
        "discarded (owner)"
    ),
)
def cancel_job(
    db: DbSession, settings: AppSettings, context: OwnerGenerationJob
) -> GenerationJobOut:
    job = _run(
        lambda: cancel_generation_job(
            db,
            job_id=context.job.id,
            expected_project_id=context.project.id,
            actor_user=context.user,
            mutations_enabled=settings.api_mutations_enabled,
        )
    )

    return _job_out(job)


# -- translation ---------------------------------------------------------


def _run(command):
    try:
        return command()
    except MutationsDisabled as exc:
        raise mutations_disabled(exc.command) from exc
    except GenerationUnavailable as exc:
        raise not_found(str(exc)) from exc
    except InvalidGenerationTransition as exc:
        raise ApiError(
            status_code=409,
            code=ErrorCode.INVALID_STATE_TRANSITION,
            message=str(exc),
            details={"command": exc.command, "status": exc.status.value},
        ) from exc
    except GenerationLocked as exc:
        raise ApiError(
            status_code=503,
            code=ErrorCode.DEPENDENCY_UNAVAILABLE,
            message="This generation job is being changed; retry in a moment.",
            details={"reason": "generation_locked"},
            headers={"Retry-After": "2"},
        ) from exc
    except InvalidGenerationParams as exc:
        raise ApiError(
            status_code=422, code=ErrorCode.VALIDATION_ERROR, message=str(exc)
        ) from exc


def _job_out(job: ContentGenerationJob) -> GenerationJobOut:
    return GenerationJobOut(
        id=job.id,
        project_id=job.project_id,
        requested_by_user_id=job.requested_by_user_id,
        status=job.status,
        params=dict(job.params or {}),
        result=dict(job.result or {}),
        last_error=job.last_error,
        attempt_count=job.attempt_count,
        created_at=job.created_at,
        updated_at=job.updated_at,
        finished_at=job.finished_at,
    )
