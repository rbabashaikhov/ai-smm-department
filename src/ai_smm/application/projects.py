"""Projects and their editorial settings, as the API sees them.

Authorisation input always starts from the database: a project is loaded
by the id in the URL and the caller's membership is read from
project_memberships. Nothing in a request body is trusted to name a
project.

Settings carry an optimistic concurrency token. A PATCH states the version
it read; the write is a conditional UPDATE on that version, so two editors
who loaded the same settings cannot silently overwrite each other -- the
second one is told to re-read.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ai_smm.db.models import (
    MembershipRole,
    Project,
    ProjectMembership,
    ProjectSettings,
    User,
)
from ai_smm.queue import record_audit


#: Version reported for a project that has no settings row yet. The first
#: PATCH must state this number and creates the row at version 1.
MISSING_SETTINGS_VERSION = 0

#: The three sections a PATCH may replace, in response order.
SETTINGS_SECTIONS = ("content_config", "publishing_config", "brand_config")


class VersionConflictError(Exception):
    """The settings changed since the caller read them."""

    def __init__(self, current_version: int) -> None:
        super().__init__(
            "Settings were modified by someone else; re-read and retry."
        )

        self.current_version = current_version


@dataclass(frozen=True)
class ProjectAccess:
    """A project the caller is allowed to see, with their role in it."""

    project: Project
    role: MembershipRole


@dataclass(frozen=True)
class SettingsView:
    """What GET returns, whether or not a row exists."""

    project_id: str
    content_config: dict[str, Any]
    publishing_config: dict[str, Any]
    brand_config: dict[str, Any]
    version: int
    updated_at: datetime | None


def list_user_projects(db: Session, *, user: User) -> list[ProjectAccess]:
    """Only the projects this user is a member of, in id order."""

    rows = db.execute(
        select(Project, ProjectMembership.role)
        .join(
            ProjectMembership,
            ProjectMembership.project_id == Project.id,
        )
        .where(ProjectMembership.user_id == user.id)
        .order_by(Project.id)
    ).all()

    return [ProjectAccess(project=project, role=role) for project, role in rows]


def get_membership(
    db: Session, *, project_id: str, user: User
) -> ProjectMembership | None:
    return db.scalars(
        select(ProjectMembership).where(
            ProjectMembership.project_id == project_id,
            ProjectMembership.user_id == user.id,
        )
    ).one_or_none()


def get_settings_view(db: Session, *, project: Project) -> SettingsView:
    """Read the settings, or the documented defaults when there is no row.

    The defaults are empty objects rather than a copy of the project's
    default_language / default_timezone: those are project fields, and
    duplicating them here would mean a PATCH that sets one section could
    silently drop a value the caller never saw.
    """

    settings = db.get(ProjectSettings, project.id)

    if settings is None:
        return SettingsView(
            project_id=project.id,
            content_config={},
            publishing_config={},
            brand_config={},
            version=MISSING_SETTINGS_VERSION,
            updated_at=None,
        )

    return SettingsView(
        project_id=project.id,
        content_config=dict(settings.content_config or {}),
        publishing_config=dict(settings.publishing_config or {}),
        brand_config=dict(settings.brand_config or {}),
        version=settings.version,
        updated_at=settings.updated_at,
    )


def update_settings(
    db: Session,
    *,
    project: Project,
    expected_version: int,
    actor: str,
    content_config: dict[str, Any] | None = None,
    publishing_config: dict[str, Any] | None = None,
    brand_config: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> SettingsView:
    """Replace the given sections, if the caller read the current version.

    A section that is not supplied is left untouched. A section that is
    supplied is replaced whole: merging two JSON documents whose shape the
    backend does not yet own would make it impossible to delete a key.
    """

    now = now or datetime.now(timezone.utc)

    changes: dict[str, dict[str, Any]] = {}

    if content_config is not None:
        changes["content_config"] = content_config

    if publishing_config is not None:
        changes["publishing_config"] = publishing_config

    if brand_config is not None:
        changes["brand_config"] = brand_config

    existing = db.get(ProjectSettings, project.id)

    if existing is None:
        if expected_version != MISSING_SETTINGS_VERSION:
            raise VersionConflictError(MISSING_SETTINGS_VERSION)

        settings = ProjectSettings(
            project_id=project.id,
            content_config=changes.get("content_config", {}),
            publishing_config=changes.get("publishing_config", {}),
            brand_config=changes.get("brand_config", {}),
            version=1,
            updated_at=now,
        )
        db.add(settings)

        try:
            db.flush()
        except IntegrityError as exc:
            # Someone else created the row between the read and this
            # insert. That is exactly the conflict the version is for.
            raise VersionConflictError(1) from exc

        new_version = 1
    else:
        result = db.execute(
            update(ProjectSettings)
            .where(
                ProjectSettings.project_id == project.id,
                ProjectSettings.version == expected_version,
            )
            .values(version=expected_version + 1, updated_at=now, **changes)
        )

        if result.rowcount == 0:
            # The row exists but not at that version: re-read to report
            # the number the caller should have used.
            db.expire(existing)
            current = db.get(ProjectSettings, project.id)

            raise VersionConflictError(
                current.version
                if current is not None
                else MISSING_SETTINGS_VERSION
            )

        db.expire(existing)
        new_version = expected_version + 1

    record_audit(
        db,
        actor=actor,
        action="project.settings.update",
        subject=f"project:{project.id}",
        details={
            "version": new_version,
            "sections": sorted(changes),
        },
    )

    return get_settings_view(db, project=project)
