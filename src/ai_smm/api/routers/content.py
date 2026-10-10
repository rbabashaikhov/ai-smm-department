"""The editorial layer over HTTP: content, revisions, human approval.

Viewer and above read; editor and above write. The bridge into delivery,
/materialize, creates a Publication from the approved revision -- never
rewrites an existing one -- and stops there: scheduling stays an admin command
(/publications/{id}/schedule), and only the worker publishes. There is
no publish endpoint here either.

No route updates a revision. Changing content means POSTing a new one,
which returns the item to draft and ends any approval in effect.

Every route addressed by a content item id loads the row first and
authorises against the project on that row. The request body never
names a project.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Response

from ai_smm.api.dependencies import (
    AppSettings,
    DbSession,
    EditorContentItem,
    EditorProject,
    PageParams,
    ViewerContentItem,
    ViewerProject,
)
from ai_smm.api.errors import ApiError, ErrorCode, not_found
from ai_smm.api.mutation_gate import mutations_disabled
from ai_smm.api.routers.publications import detail_of
from ai_smm.api.schemas import (
    ApprovalList,
    ApprovalOut,
    ContentItemCreate,
    ContentItemDetail,
    ContentItemList,
    ContentItemSummary,
    DecisionOut,
    MaterializeIn,
    MaterializeOut,
    RevisionCreate,
    RevisionCreated,
    RevisionDecisionIn,
    RevisionIn,
    RevisionList,
    RevisionOut,
)
from ai_smm.application.content import (
    ContentLocked,
    ContentUnavailable,
    ContentVersionConflict,
    InvalidContentTransition,
    NotDeliverable,
    PublicationNotEditable,
    RevisionInput,
    StaleRevision,
    approve_revision,
    create_content_item,
    create_revision,
    get_revision,
    list_approvals_page,
    list_content_items_page,
    list_revisions_page,
    materialize_approved_revision,
    reject_revision,
    submit_for_review,
)
from ai_smm.application.control_plane import MutationsDisabled
from ai_smm.db.models import (
    ContentApproval,
    ContentItem,
    ContentRevision,
    ContentStatus,
)


router = APIRouter(tags=["content"])

StatusFilter = Annotated[list[ContentStatus] | None, Query(alias="status")]
TypeFilter = Annotated[
    str | None, Query(alias="content_type", max_length=40)
]


# -- reads ---------------------------------------------------------------


@router.get(
    "/projects/{project_id}/content-items",
    response_model=ContentItemList,
    summary="Content items of one project (viewer and above)",
)
def list_items(
    db: DbSession,
    page: PageParams,
    context: ViewerProject,
    status: StatusFilter = None,
    content_type: TypeFilter = None,
) -> ContentItemList:
    result = list_content_items_page(
        db,
        project_id=context.project.id,
        statuses=status,
        content_type=content_type,
        limit=page.limit,
        offset=page.offset,
    )

    return ContentItemList(
        items=[_item_summary(item) for item in result.items],
        total=result.total,
        limit=result.limit,
        offset=result.offset,
    )


@router.get(
    "/content-items/{content_item_id}",
    response_model=ContentItemDetail,
    summary="One content item with its current revision (viewer and above)",
)
def get_item(db: DbSession, context: ViewerContentItem) -> ContentItemDetail:
    return _item_detail(db, context.item)


@router.get(
    "/content-items/{content_item_id}/revisions",
    response_model=RevisionList,
    summary="Every revision, oldest first (viewer and above)",
)
def list_revisions(
    db: DbSession, page: PageParams, context: ViewerContentItem
) -> RevisionList:
    result = list_revisions_page(
        db, item=context.item, limit=page.limit, offset=page.offset
    )

    return RevisionList(
        items=[_revision_out(row) for row in result.items],
        total=result.total,
        limit=result.limit,
        offset=result.offset,
    )


@router.get(
    "/content-items/{content_item_id}/approvals",
    response_model=ApprovalList,
    summary="Every human decision, oldest first (viewer and above)",
)
def list_approvals(
    db: DbSession, page: PageParams, context: ViewerContentItem
) -> ApprovalList:
    result = list_approvals_page(
        db, item=context.item, limit=page.limit, offset=page.offset
    )

    return ApprovalList(
        items=[_approval_out(row) for row in result.items],
        total=result.total,
        limit=result.limit,
        offset=result.offset,
    )


# -- writes --------------------------------------------------------------


@router.post(
    "/projects/{project_id}/content-items",
    response_model=ContentItemDetail,
    status_code=201,
    summary="Create a content item with its first revision (editor+)",
)
def create_item(
    payload: ContentItemCreate, db: DbSession, context: EditorProject
) -> ContentItemDetail:
    item, _ = _run(
        lambda: create_content_item(
            db,
            project_id=context.project.id,
            title=payload.title,
            content_type=payload.content_type,
            revision=_revision_input(payload.revision),
            created_by_user=context.authenticated.user,
        )
    )

    return _item_detail(db, item)


@router.post(
    "/content-items/{content_item_id}/revisions",
    response_model=RevisionCreated,
    status_code=201,
    summary="Append a revision; the item returns to draft (editor+)",
)
def add_revision(
    payload: RevisionCreate, db: DbSession, context: EditorContentItem
) -> RevisionCreated:
    item, revision = _run(
        lambda: create_revision(
            db,
            content_item_id=context.item.id,
            expected_project_id=context.project.id,
            expected_item_version=payload.expected_item_version,
            revision=_revision_input(payload),
            created_by_user=context.user,
        )
    )

    return RevisionCreated(
        item=_item_summary(item), revision=_revision_out(revision)
    )


@router.post(
    "/content-items/{content_item_id}/submit-review",
    response_model=ContentItemSummary,
    summary="draft -> in_review for the named revision (editor+)",
)
def submit(
    payload: RevisionDecisionIn, db: DbSession, context: EditorContentItem
) -> ContentItemSummary:
    item = _run(
        lambda: submit_for_review(
            db,
            content_item_id=context.item.id,
            expected_project_id=context.project.id,
            revision_id=payload.revision_id,
            actor_user=context.user,
            expected_item_version=payload.expected_item_version,
            note=payload.note,
        )
    )

    return _item_summary(item)


@router.post(
    "/content-items/{content_item_id}/approve",
    response_model=DecisionOut,
    summary="Approve exactly the named, current revision (editor+)",
)
def approve(
    payload: RevisionDecisionIn, db: DbSession, context: EditorContentItem
) -> DecisionOut:
    item, approval = _run(
        lambda: approve_revision(
            db,
            content_item_id=context.item.id,
            expected_project_id=context.project.id,
            revision_id=payload.revision_id,
            actor_user=context.user,
            expected_item_version=payload.expected_item_version,
            note=payload.note,
        )
    )

    return DecisionOut(
        item=_item_summary(item), approval=_approval_out(approval)
    )


@router.post(
    "/content-items/{content_item_id}/reject",
    response_model=DecisionOut,
    summary="Reject exactly the named, current revision (editor+)",
)
def reject(
    payload: RevisionDecisionIn, db: DbSession, context: EditorContentItem
) -> DecisionOut:
    item, approval = _run(
        lambda: reject_revision(
            db,
            content_item_id=context.item.id,
            expected_project_id=context.project.id,
            revision_id=payload.revision_id,
            actor_user=context.user,
            expected_item_version=payload.expected_item_version,
            note=payload.note,
        )
    )

    return DecisionOut(
        item=_item_summary(item), approval=_approval_out(approval)
    )


@router.post(
    "/content-items/{content_item_id}/materialize",
    response_model=MaterializeOut,
    summary=(
        "Create the delivery Publication for the approved revision; "
        "never rewrites one, never schedules, never publishes (editor+)"
    ),
)
def materialize(
    payload: MaterializeIn,
    response: Response,
    db: DbSession,
    settings: AppSettings,
    context: EditorContentItem,
) -> MaterializeOut:
    result = _run(
        lambda: materialize_approved_revision(
            db,
            content_item_id=context.item.id,
            expected_project_id=context.project.id,
            actor_user=context.user,
            mutations_enabled=settings.api_mutations_enabled,
            revision_id=payload.revision_id,
        )
    )

    response.status_code = 201 if result.result == "created" else 200

    return MaterializeOut(
        result=result.result,
        platform=result.platform,
        revision_id=result.revision.id,
        publication=detail_of(result.publication),
    )


# -- translation ---------------------------------------------------------


def _run(command):
    """Run an application command and translate its refusals.

    The decisions are made in ai_smm.application.content. This function
    maps each refusal to a status code and nothing else.
    """

    try:
        return command()
    except MutationsDisabled as exc:
        # Second barrier behind the router-level gate; see
        # ai_smm.application.control_plane.
        raise mutations_disabled(exc.command) from exc
    except ContentUnavailable as exc:
        raise not_found(str(exc)) from exc
    except ContentVersionConflict as exc:
        raise ApiError(
            status_code=409,
            code=ErrorCode.VERSION_CONFLICT,
            message=str(exc),
            details={
                "expected_version": exc.expected,
                "current_version": exc.current,
            },
        ) from exc
    except StaleRevision as exc:
        raise ApiError(
            status_code=409,
            code=ErrorCode.STALE_REVISION,
            message=str(exc),
            details={
                "requested_revision_id": str(exc.requested),
                "current_revision_id": str(exc.current),
            },
        ) from exc
    except InvalidContentTransition as exc:
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
    except PublicationNotEditable as exc:
        raise ApiError(
            status_code=409,
            code=ErrorCode.PUBLICATION_NOT_EDITABLE,
            message=str(exc),
            details={
                "command": "materialize",
                "publication_id": exc.publication_id,
                "publication_status": exc.status.value,
                "previous_revision_id": (
                    str(exc.previous_revision_id)
                    if exc.previous_revision_id
                    else None
                ),
            },
        ) from exc
    except NotDeliverable as exc:
        raise ApiError(
            status_code=422,
            code=ErrorCode.VALIDATION_ERROR,
            message=(
                "The approved revision would be refused by the delivery "
                "preflight."
            ),
            details={"content_error": str(exc)},
        ) from exc
    except ContentLocked as exc:
        raise ApiError(
            status_code=503,
            code=ErrorCode.DEPENDENCY_UNAVAILABLE,
            message="This content item is being changed; retry in a moment.",
            details={"reason": "content_locked"},
            headers={"Retry-After": "2"},
        ) from exc
    except ValueError as exc:
        raise ApiError(
            status_code=422,
            code=ErrorCode.VALIDATION_ERROR,
            message=str(exc),
        ) from exc


def _revision_input(payload: RevisionIn) -> RevisionInput:
    return RevisionInput(
        title=payload.title,
        body=payload.body,
        format=payload.format,
        images=list(payload.images),
        items=list(payload.items),
        metadata=dict(payload.metadata),
        source=payload.source,
        source_ref=payload.source_ref,
        editor_score=payload.editor_score,
        editor_notes=payload.editor_notes,
    )


def _item_summary(item: ContentItem) -> ContentItemSummary:
    return ContentItemSummary(
        id=item.id,
        project_id=item.project_id,
        title=item.title,
        content_type=item.content_type,
        status=item.status,
        current_revision_id=item.current_revision_id,
        approved_revision_id=item.approved_revision_id,
        version=item.version,
        created_by_user_id=item.created_by_user_id,
        created_at=item.created_at,
        updated_at=item.updated_at,
        archived_at=item.archived_at,
    )


def _item_detail(db, item: ContentItem) -> ContentItemDetail:
    current = get_revision(db, revision_id=item.current_revision_id)
    assert current is not None

    return ContentItemDetail(
        **_item_summary(item).model_dump(),
        current_revision=_revision_out(current),
        approval_in_effect=(
            item.status is ContentStatus.APPROVED
            and item.approved_revision_id == item.current_revision_id
        ),
    )


def _revision_out(row: ContentRevision) -> RevisionOut:
    return RevisionOut(
        id=row.id,
        content_item_id=row.content_item_id,
        revision_number=row.revision_number,
        title=row.title,
        body=row.body,
        format=row.format,
        images=list(row.images or []),
        items=list(row.items or []),
        metadata=dict(row.meta or {}),
        source=row.source,
        source_ref=row.source_ref,
        editor_score=(
            float(row.editor_score) if row.editor_score is not None else None
        ),
        editor_notes=row.editor_notes,
        content_hash=row.content_hash,
        created_by_user_id=row.created_by_user_id,
        created_at=row.created_at,
    )


def _approval_out(row: ContentApproval) -> ApprovalOut:
    return ApprovalOut(
        id=row.id,
        content_item_id=row.content_item_id,
        revision_id=row.revision_id,
        decision=row.decision,
        actor_user_id=row.actor_user_id,
        note=row.note,
        created_at=row.created_at,
    )
