"""Operational views: what the queue is doing, and what needs a human.

The attention view is the HTTP counterpart of `ai-smm errors`: failures
that ran out of attempts, and rows whose outcome is unknown. Listing one
does not retry it. A needs_review row is resolved by an operator who has
checked the real Threads account, with `ai-smm reconcile`, and no command
in this API will touch it.

The audit view is admin-and-above, because an audit trail names who did
what and a viewer has no business reading it.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from ai_smm.api.dependencies import (
    AdminProjectAudit,
    AppSettings,
    DbSession,
    PageParams,
    ViewerProject,
)
from ai_smm.api.routers.publications import summary_of
from ai_smm.api.schemas import (
    AuditEntryOut,
    AuditList,
    OperationsSummaryOut,
    PublicationList,
)
from ai_smm.application.audit_views import list_project_audit_page
from ai_smm.application.publications import (
    list_attention_page,
    operations_summary,
)


router = APIRouter(prefix="/projects/{project_id}", tags=["operations"])

ActionFilter = Annotated[str | None, Query(alias="action", max_length=120)]


@router.get(
    "/operations/summary",
    response_model=OperationsSummaryOut,
    summary="Queue counters for one project (viewer and above)",
)
def summary(
    db: DbSession, settings: AppSettings, context: ViewerProject
) -> OperationsSummaryOut:
    result = operations_summary(
        db,
        project_id=context.project.id,
        dry_run=settings.dry_run,
    )

    return OperationsSummaryOut(**vars(result))


@router.get(
    "/operations/attention",
    response_model=PublicationList,
    summary="Failed and needs_review publications (viewer and above)",
)
def attention(
    db: DbSession, page: PageParams, context: ViewerProject
) -> PublicationList:
    """Read-only. Listing a row here never retries or reschedules it."""

    result = list_attention_page(
        db,
        project_id=context.project.id,
        limit=page.limit,
        offset=page.offset,
    )

    return PublicationList(
        items=[summary_of(row) for row in result.items],
        total=result.total,
        limit=result.limit,
        offset=result.offset,
    )


@router.get(
    "/audit",
    response_model=AuditList,
    summary="Audit entries about this project (admin and above)",
)
def audit(
    db: DbSession,
    page: PageParams,
    context: AdminProjectAudit,
    action: ActionFilter = None,
) -> AuditList:
    result = list_project_audit_page(
        db,
        project_id=context.project.id,
        action=action,
        limit=page.limit,
        offset=page.offset,
    )

    return AuditList(
        items=[
            AuditEntryOut(
                id=entry.id,
                at=entry.at,
                actor=entry.actor,
                action=entry.action,
                subject=entry.subject,
                details=dict(entry.details or {}),
            )
            for entry in result.items
        ],
        total=result.total,
        limit=result.limit,
        offset=result.offset,
    )
