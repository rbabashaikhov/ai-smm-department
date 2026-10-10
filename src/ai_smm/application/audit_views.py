"""Project-scoped reads of the existing audit log.

audit_log is written by the worker, the CLI, the importer and the API,
and it has no project_id column: an entry names its subject, as
`publication:<id>`, `series:<id>`, `content_item:<uuid>`,
`project:<id>` or `user:<uuid>`.

So "the audit trail of this project" is derived rather than stored: the
subjects belonging to the project are selected in SQL from publications,
content_series and content_items, and an entry matches when its subject is one of them
or the project itself. That keeps the view honest without a migration;
it also means an entry about a user (a login, the owner bootstrap) is not
attributed to any project, which is correct -- a user is not owned by
one.
"""
from __future__ import annotations

from sqlalchemy import String, cast, func, literal, select
from sqlalchemy.orm import Session

from ai_smm.application.publications import Page
from ai_smm.db.models import AuditLog, ContentItem, ContentSeries, Publication


def _project_subjects(project_id: str):
    """The subject strings that belong to this project."""

    publication_subjects = select(
        literal("publication:").concat(cast(Publication.id, String))
    ).where(Publication.project_id == project_id)

    series_subjects = select(
        literal("series:").concat(cast(ContentSeries.id, String))
    ).where(ContentSeries.project_id == project_id)

    content_subjects = select(
        literal("content_item:").concat(cast(ContentItem.id, String))
    ).where(ContentItem.project_id == project_id)

    return publication_subjects, series_subjects, content_subjects


def list_project_audit_page(
    db: Session,
    *,
    project_id: str,
    action: str | None = None,
    limit: int,
    offset: int,
) -> Page:
    publication_subjects, series_subjects, content_subjects = (
        _project_subjects(project_id)
    )

    stmt = select(AuditLog).where(
        AuditLog.subject.is_not(None),
        AuditLog.subject.in_(
            publication_subjects.union_all(series_subjects, content_subjects)
        )
        | (AuditLog.subject == f"project:{project_id}"),
    )

    if action:
        stmt = stmt.where(AuditLog.action == action)

    total = int(
        db.execute(
            select(func.count()).select_from(stmt.subquery())
        ).scalar_one()
    )

    rows = list(
        db.execute(
            stmt.order_by(AuditLog.at.desc(), AuditLog.id.desc())
            .limit(limit)
            .offset(offset)
        ).scalars()
    )

    return Page(items=rows, total=total, limit=limit, offset=offset)
