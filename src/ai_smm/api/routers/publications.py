"""Reading the publication queue, and the three safe commands.

**There is no publish endpoint here, and there will not be one.** The API
is the control plane: it can tell the queue *when* a reviewed row should
go out, and it can take a row out of the queue, but the row is sent to
Threads by the worker and by nothing else. That is what keeps the
duplicate barriers meaningful -- a second publisher would reduce all of
them to advice.

Nor is anything retried behind an operator's back: a row whose outcome is
unknown (publishing, needs_review) is refused by every command with 409,
because only a human who has looked at the real account can say what
happened to it.

Every route addressed by a publication id loads the row first and
authorises against the project on that row; the request body never names
a project.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from ai_smm.api.dependencies import (
    AdminPublication,
    DbSession,
    PageParams,
    ViewerProject,
    ViewerPublication,
)
from ai_smm.api.errors import ApiError, ErrorCode, not_found
from ai_smm.api.schemas import (
    AttemptList,
    AttemptOut,
    CancelRequest,
    PublicationDetail,
    PublicationList,
    PublicationPreviewOut,
    PublicationSummary,
    ScheduleRequest,
)
from ai_smm.application.publications import (
    InvalidTransition,
    PublicationLocked,
    PublicationUnavailable,
    cancel_publication,
    list_attempts,
    list_publications_page,
    preview_publication,
    reschedule_publication,
    schedule_publication,
)
from ai_smm.db.models import Publication, PublicationStatus


router = APIRouter(tags=["publications"])

#: Filters on the list endpoint. Repeating ?status= narrows to a set, as
#: `ai-smm queue --status` does.
StatusFilter = Annotated[list[PublicationStatus] | None, Query(alias="status")]
FormatFilter = Annotated[
    str | None, Query(alias="format", max_length=40)
]
SeriesFilter = Annotated[int | None, Query(alias="series_id", ge=1)]
ReviewedFilter = Annotated[bool | None, Query(alias="human_reviewed")]


# -- reads ---------------------------------------------------------------


@router.get(
    "/projects/{project_id}/publications",
    response_model=PublicationList,
    summary="Publications of one project (viewer and above)",
)
def list_project_publications(
    db: DbSession,
    page: PageParams,
    context: ViewerProject,
    status: StatusFilter = None,
    publication_format: FormatFilter = None,
    series_id: SeriesFilter = None,
    human_reviewed: ReviewedFilter = None,
) -> PublicationList:
    result = list_publications_page(
        db,
        project_id=context.project.id,
        statuses=status,
        publication_format=publication_format,
        series_id=series_id,
        human_reviewed=human_reviewed,
        limit=page.limit,
        offset=page.offset,
    )

    return PublicationList(
        items=[_summary(row) for row in result.items],
        total=result.total,
        limit=result.limit,
        offset=result.offset,
    )


@router.get(
    "/publications/{publication_id}",
    response_model=PublicationDetail,
    summary="One publication (viewer and above)",
)
def get_publication(context: ViewerPublication) -> PublicationDetail:
    return _detail(context.publication)


@router.get(
    "/publications/{publication_id}/preview",
    response_model=PublicationPreviewOut,
    summary="What the worker would send, and whether it could send it now",
)
def preview(
    db: DbSession, context: ViewerPublication
) -> PublicationPreviewOut:
    """Runs the existing preflight. Sends nothing, publishes nothing."""

    result = preview_publication(db, publication=context.publication)

    return PublicationPreviewOut(
        publication=_detail(result.publication),
        content_valid=result.content_valid,
        content_error=result.content_error,
        human_reviewed=result.human_reviewed,
        series_ready=result.series_ready,
        series_reason=result.series_reason,
        blocking_parts=result.blocking_parts,
        publishable_now=result.publishable_now,
    )


@router.get(
    "/publications/{publication_id}/attempts",
    response_model=AttemptList,
    summary="Attempt history of one publication (viewer and above)",
)
def list_publication_attempts(
    db: DbSession, page: PageParams, context: ViewerPublication
) -> AttemptList:
    result = list_attempts(
        db,
        publication=context.publication,
        limit=page.limit,
        offset=page.offset,
    )

    return AttemptList(
        items=[AttemptOut(**_attempt_fields(row)) for row in result.items],
        total=result.total,
        limit=result.limit,
        offset=result.offset,
    )


# -- safe commands -------------------------------------------------------


@router.post(
    "/publications/{publication_id}/schedule",
    response_model=PublicationDetail,
    summary="Put an approved or failed publication on the schedule (admin+)",
)
def schedule(
    payload: ScheduleRequest, db: DbSession, context: AdminPublication
) -> PublicationDetail:
    return _run_command(
        lambda: schedule_publication(
            db,
            publication_id=context.publication.id,
            expected_project_id=context.project.id,
            scheduled_at=payload.scheduled_at,
            actor=context.actor,
            note=payload.note,
            reset_attempts=payload.reset_attempts,
        )
    )


@router.post(
    "/publications/{publication_id}/reschedule",
    response_model=PublicationDetail,
    summary="Move the schedule of a scheduled or failed publication (admin+)",
)
def reschedule(
    payload: ScheduleRequest, db: DbSession, context: AdminPublication
) -> PublicationDetail:
    return _run_command(
        lambda: reschedule_publication(
            db,
            publication_id=context.publication.id,
            expected_project_id=context.project.id,
            scheduled_at=payload.scheduled_at,
            actor=context.actor,
            note=payload.note,
            reset_attempts=payload.reset_attempts,
        )
    )


@router.post(
    "/publications/{publication_id}/cancel",
    response_model=PublicationDetail,
    summary="Take a publication out of the queue for good (admin+)",
)
def cancel(
    payload: CancelRequest, db: DbSession, context: AdminPublication
) -> PublicationDetail:
    return _run_command(
        lambda: cancel_publication(
            db,
            publication_id=context.publication.id,
            expected_project_id=context.project.id,
            actor=context.actor,
            note=payload.note,
        )
    )


def _run_command(command) -> PublicationDetail:
    """Run an application command, mapping its refusals to status codes.

    The decisions themselves are made in
    ai_smm.application.publications, which is also where the transition
    table and the row lock live. This function only translates.

    The status the conflict reports is the one read under the lock, not
    the one the authorisation dependency saw, so a row a worker claimed
    in between is reported as claimed.
    """

    try:
        publication = command()
    except InvalidTransition as exc:
        raise ApiError(
            status_code=409,
            code=ErrorCode.INVALID_STATE_TRANSITION,
            message=str(exc),
            details={
                "command": exc.command,
                "status": exc.status.value,
                "allowed_from": [status.value for status in exc.allowed],
            },
        ) from exc
    except PublicationUnavailable as exc:
        # The row was deleted, or is not in the project authorisation
        # approved. Same answer as an id that never existed.
        raise not_found("Publication not found.") from exc
    except PublicationLocked as exc:
        # Nothing was read and nothing was changed: the right answer is
        # "come back", not a verdict about a state we could not see.
        raise ApiError(
            status_code=503,
            code=ErrorCode.DEPENDENCY_UNAVAILABLE,
            message=(
                "This publication is being worked on right now; retry in "
                "a moment."
            ),
            details={"reason": "publication_locked"},
            headers={"Retry-After": "2"},
        ) from exc

    return _detail(publication)


# -- projections ---------------------------------------------------------


def _summary_fields(row: Publication) -> dict[str, object]:
    return {
        "id": row.id,
        "project_id": row.project_id,
        "platform": row.platform,
        "ordinal": row.ordinal,
        "title": row.title,
        "format": row.format,
        "status": row.status,
        "scheduled_at": row.scheduled_at,
        "published_at": row.published_at,
        "threads_post_id": row.threads_post_id,
        "attempt_count": row.attempt_count,
        "human_reviewed": row.human_reviewed,
        "series_id": row.series_id,
        "series_position": row.series_position,
        "series_total": row.series_total,
        "image_count": len(row.images or []),
        "body_chars": len(row.body or ""),
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _summary(row: Publication) -> PublicationSummary:
    return PublicationSummary(**_summary_fields(row))


def _detail(row: Publication) -> PublicationDetail:
    return PublicationDetail(
        **_summary_fields(row),
        body=row.body,
        images=list(row.images or []),
        items=list(row.items or []),
        editor_score=(
            float(row.editor_score) if row.editor_score is not None else None
        ),
        last_error=row.last_error,
        next_attempt_at=row.next_attempt_at,
        claimed_by=row.claimed_by,
        claimed_at=row.claimed_at,
        lease_expires_at=row.lease_expires_at,
        parent_publication_id=row.parent_publication_id,
        previous_publication_id=row.previous_publication_id,
        source_ref=row.source_ref,
        idempotency_key=row.idempotency_key,
    )


def _attempt_fields(row) -> dict[str, object]:
    return {
        "id": row.id,
        "publication_id": row.publication_id,
        "attempt_number": row.attempt_number,
        "worker_id": row.worker_id,
        "phase": row.phase,
        "outcome": row.outcome,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
        "threads_creation_id": row.threads_creation_id,
        "threads_post_id": row.threads_post_id,
        "http_status": row.http_status,
        "error_type": row.error_type,
        "error_message": row.error_message,
        "details": dict(row.details or {}),
    }


#: Re-exported so the series and content routers render publications
#: the same way.
summary_of = _summary
detail_of = _detail
