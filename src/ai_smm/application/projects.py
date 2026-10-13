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

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ai_smm.application.project_ids import require_valid_project_id
from ai_smm.db.models import (
    DEFAULT_PROJECT_LANGUAGE,
    DEFAULT_PROJECT_TIMEZONE,
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

    changes = validate_settings_sections(changes)

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


# -- typed settings keys -------------------------------------------------
# The sections stay free JSON; the keys the Control Center edits are
# validated here. Unknown keys are left alone, so settings written before
# this existed keep working.

MAX_TOPICS = 10
MAX_TOPIC_LENGTH = 80
MAX_TONE_LENGTH = 4000
#: Serialised size limit of one section.
MAX_SECTION_BYTES = 16 * 1024


def normalize_topics(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        raise ValueError("content_config.topics must be a list of strings.")

    if len(raw) > MAX_TOPICS:
        raise ValueError(f"At most {MAX_TOPICS} topics are allowed.")

    topics: list[str] = []
    seen: set[str] = set()

    for item in raw:
        if not isinstance(item, str):
            raise ValueError("Every topic must be a string.")

        topic = " ".join(item.split())

        if not topic or len(topic) > MAX_TOPIC_LENGTH:
            raise ValueError(
                f"Every topic must be 1-{MAX_TOPIC_LENGTH} characters."
            )

        if topic.casefold() in seen:
            raise ValueError(f"Topic {topic!r} is listed twice.")

        seen.add(topic.casefold())
        topics.append(topic)

    return topics


def normalize_tone(raw: Any) -> str:
    if not isinstance(raw, str):
        raise ValueError("brand_config.tone_of_voice must be a string.")

    tone = raw.strip()

    if len(tone) > MAX_TONE_LENGTH:
        raise ValueError(
            f"Tone of voice must be at most {MAX_TONE_LENGTH} characters."
        )

    return tone


def validate_settings_sections(
    sections: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Check the owned keys and the size of every supplied section."""

    import json

    result: dict[str, dict[str, Any]] = {}

    for name, value in sections.items():
        section = dict(value)

        if name == "content_config" and "topics" in section:
            section["topics"] = normalize_topics(section["topics"])

        if name == "brand_config" and "tone_of_voice" in section:
            section["tone_of_voice"] = normalize_tone(section["tone_of_voice"])

        size = len(
            json.dumps(section, ensure_ascii=False).encode("utf-8")
        )

        if size > MAX_SECTION_BYTES:
            raise ValueError(
                f"{name} is too large ({size} bytes; the limit is "
                f"{MAX_SECTION_BYTES})."
            )

        result[name] = section

    return result


# -- the project record ----------------------------------------------------

#: A guard against a runaway client, not a product limit.
MAX_OWNED_PROJECTS = 20
MAX_DISPLAY_NAME_LENGTH = 120
MAX_DESCRIPTION_LENGTH = 2000

_LANGUAGE_RE = re.compile(r"^[a-z]{2}(-[A-Z]{2})?$")


class ProjectAlreadyExists(Exception):
    def __init__(self, project_id: str) -> None:
        super().__init__(f"A project with id {project_id!r} already exists.")

        self.project_id = project_id


class ProjectNameTaken(Exception):
    def __init__(self, display_name: str) -> None:
        super().__init__(
            f"You already have a project named {display_name!r}."
        )

        self.display_name = display_name


class ProjectLimitReached(Exception):
    pass


class ProjectCreationForbidden(Exception):
    pass


def normalize_display_name(raw: str) -> str:
    name = " ".join((raw or "").split())

    if not name:
        raise ValueError("Project name must not be empty.")

    if len(name) > MAX_DISPLAY_NAME_LENGTH:
        raise ValueError(
            f"Project name must be at most {MAX_DISPLAY_NAME_LENGTH} "
            "characters."
        )

    return name


def normalize_description(raw: str | None) -> str:
    description = (raw or "").strip()

    if len(description) > MAX_DESCRIPTION_LENGTH:
        raise ValueError(
            f"Description must be at most {MAX_DESCRIPTION_LENGTH} "
            "characters."
        )

    if any(
        not ch.isprintable() and ch not in "\n\t" for ch in description
    ):
        raise ValueError("Description contains control characters.")

    return description


def normalize_timezone(raw: str | None) -> str:
    name = (raw or DEFAULT_PROJECT_TIMEZONE).strip()

    if len(name) > 64:
        raise ValueError("Timezone name is too long.")

    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"Unknown timezone {name!r}.") from exc

    return name


def normalize_language(raw: str | None) -> str:
    language = (raw or DEFAULT_PROJECT_LANGUAGE).strip()

    if not _LANGUAGE_RE.match(language):
        raise ValueError(
            "Language must look like 'ru' or 'en-US'."
        )

    return language


@dataclass(frozen=True)
class ProjectInput:
    id: str
    display_name: str
    description: str = ""
    default_timezone: str | None = None
    default_language: str | None = None
    topics: list[str] | None = None
    tone_of_voice: str | None = None


@dataclass(frozen=True)
class CreateProjectResult:
    project: Project
    created: bool


def user_owns_any_project(db: Session, *, user: User) -> bool:
    return bool(
        db.scalar(
            select(func.count())
            .select_from(ProjectMembership)
            .where(
                ProjectMembership.user_id == user.id,
                ProjectMembership.role == MembershipRole.OWNER,
            )
        )
    )


def _name_taken(
    db: Session, *, user: User, name: str, exclude: str | None = None
) -> bool:
    stmt = (
        select(Project.id)
        .join(ProjectMembership, ProjectMembership.project_id == Project.id)
        .where(
            ProjectMembership.user_id == user.id,
            func.lower(Project.display_name) == name.casefold(),
        )
    )

    if exclude is not None:
        stmt = stmt.where(Project.id != exclude)

    return db.scalar(stmt.limit(1)) is not None


def _replay(
    db: Session, *, user: User, project_id: str, display_name: str
) -> CreateProjectResult:
    """The id exists. A repeat of the owner's own create returns it."""

    existing = db.get(Project, project_id, populate_existing=True)
    assert existing is not None

    membership = get_membership(db, project_id=project_id, user=user)

    if (
        membership is not None
        and membership.role is MembershipRole.OWNER
        and existing.display_name.casefold() == display_name.casefold()
    ):
        return CreateProjectResult(project=existing, created=False)

    raise ProjectAlreadyExists(project_id)


def create_project(
    db: Session,
    *,
    actor_user: User,
    data: ProjectInput,
    actor: str | None = None,
    require_existing_ownership: bool = True,
    now: datetime | None = None,
) -> CreateProjectResult:
    """Create a project with its owner, settings and audit, or nothing.

    The project row, the caller's owner membership, the settings row and
    the audit entry are written in the caller's transaction, which
    commits once: a failure anywhere leaves none of them. The owner is
    always actor_user; nothing in the input can name another user or a
    role.

    Idempotent on the id: the owner repeating the same create (same id,
    same name) gets the existing project back and nothing is written. A
    concurrent create of the same id loses on the primary key inside a
    savepoint and is answered by the same rule.

    require_existing_ownership is the API rule (only someone who already
    owns a project may create one). The operator CLI, which bootstraps
    the first project, passes False.
    """

    project_id = require_valid_project_id(data.id)
    name = normalize_display_name(data.display_name)
    description = normalize_description(data.description)
    timezone_name = normalize_timezone(data.default_timezone)
    language = normalize_language(data.default_language)
    topics = normalize_topics(data.topics) if data.topics is not None else []
    tone = normalize_tone(data.tone_of_voice) if data.tone_of_voice else ""
    now = now or datetime.now(timezone.utc)
    actor = actor or f"user:{actor_user.id}"

    if not actor_user.is_active:
        raise ProjectCreationForbidden("The user is not active.")

    if require_existing_ownership and not user_owns_any_project(
        db, user=actor_user
    ):
        raise ProjectCreationForbidden(
            "Only the owner of an existing project may create a project."
        )

    if db.get(Project, project_id) is not None:
        return _replay(
            db, user=actor_user, project_id=project_id, display_name=name
        )

    if _name_taken(db, user=actor_user, name=name):
        raise ProjectNameTaken(name)

    owned = db.scalar(
        select(func.count())
        .select_from(ProjectMembership)
        .where(
            ProjectMembership.user_id == actor_user.id,
            ProjectMembership.role == MembershipRole.OWNER,
        )
    )

    if owned >= MAX_OWNED_PROJECTS:
        raise ProjectLimitReached(
            f"You already own {owned} projects (limit {MAX_OWNED_PROJECTS})."
        )

    # Anything pending belongs to the outer transaction; flush it before
    # the savepoint so a failure inside is only ever this insert's.
    db.flush()

    try:
        with db.begin_nested():
            db.add(
                Project(
                    id=project_id,
                    display_name=name,
                    description=description,
                    knowledge_path=f"knowledge/projects/{project_id}",
                    default_platform="threads",
                    is_active=True,
                    default_timezone=timezone_name,
                    default_language=language,
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
            )
            db.flush()
    except IntegrityError:
        # Created concurrently between the check and the insert.
        return _replay(
            db, user=actor_user, project_id=project_id, display_name=name
        )

    db.add(
        ProjectMembership(
            project_id=project_id,
            user_id=actor_user.id,
            role=MembershipRole.OWNER,
            created_at=now,
            updated_at=now,
        )
    )
    db.add(
        ProjectSettings(
            project_id=project_id,
            content_config={"topics": topics},
            publishing_config={},
            brand_config={"tone_of_voice": tone},
            version=1,
            updated_at=now,
        )
    )
    db.flush()

    record_audit(
        db,
        actor=actor,
        action="project.create",
        subject=f"project:{project_id}",
        details={
            "display_name": name,
            "owner_user_id": str(actor_user.id),
            "default_timezone": timezone_name,
            "default_language": language,
            "topics": len(topics),
        },
    )

    project = db.get(Project, project_id)
    assert project is not None

    return CreateProjectResult(project=project, created=True)


#: Fields PATCH /projects/{id} may change, in audit order.
EDITABLE_PROJECT_FIELDS = (
    "display_name",
    "description",
    "default_timezone",
    "default_language",
)


def update_project_record(
    db: Session,
    *,
    project: Project,
    expected_version: int,
    actor_user: User,
    actor: str,
    changes: dict[str, Any],
    now: datetime | None = None,
) -> Project:
    """Change the editable fields if the caller read the current version."""

    unknown = set(changes) - set(EDITABLE_PROJECT_FIELDS)

    if unknown:
        raise ValueError(f"Not editable: {', '.join(sorted(unknown))}.")

    if not changes:
        raise ValueError("Nothing to change.")

    normalizers = {
        "display_name": normalize_display_name,
        "description": normalize_description,
        "default_timezone": normalize_timezone,
        "default_language": normalize_language,
    }
    values = {
        name: normalizers[name](value) for name, value in changes.items()
    }

    if "display_name" in values and _name_taken(
        db, user=actor_user, name=values["display_name"], exclude=project.id
    ):
        raise ProjectNameTaken(values["display_name"])

    current = db.get(Project, project.id, populate_existing=True)
    assert current is not None

    before = {name: getattr(current, name) for name in values}
    now = now or datetime.now(timezone.utc)

    result = db.execute(
        update(Project)
        .where(Project.id == project.id, Project.version == expected_version)
        .values(version=expected_version + 1, updated_at=now, **values)
    )

    if result.rowcount == 0:
        db.expire(current)
        fresh = db.get(Project, project.id)

        raise VersionConflictError(fresh.version if fresh else 0)

    record_audit(
        db,
        actor=actor,
        action="project.update",
        subject=f"project:{project.id}",
        details={
            "version": expected_version + 1,
            "changed": {
                name: [before[name], values[name]]
                for name in EDITABLE_PROJECT_FIELDS
                if name in values and before[name] != values[name]
            },
        },
    )

    db.expire(current)

    fresh = db.get(Project, project.id)
    assert fresh is not None

    return fresh
