"""Generation jobs over HTTP: create one, cancel one, read them.

Two things live here, the same split ai_smm.application.publications
makes for the delivery queue.

**Read projections.** Paginated, project-scoped views of rows the
generation worker owns.

**Safe commands.** create and cancel. The actual claim, lease and content
creation are ai_smm.generation_queue's and ai_smm.application.content's;
this module only decides whether the HTTP caller may ask for that, and
writes the one row (content_generation_jobs) a guarded role is allowed to
touch directly. It never creates a content item, never approves one and
never schedules a publication -- a generation job can only ever reach
succeeded by way of the generation worker, which runs as the table owner
and is not reachable from this module.

Every command first checks the control plane's mutation switch
(ai_smm.application.control_plane), exactly like every content and
publication command does.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Select, func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from ai_smm.application.control_plane import require_mutations_enabled
from ai_smm.application.projects import (
    SETTINGS_SECTIONS,
    validate_settings_sections,
)
from ai_smm.application.publications import Page
from ai_smm.content_profile import PROFILE_PARAM, build_profile_snapshot
from ai_smm.db.models import (
    ContentGenerationJob,
    GenerationJobStatus,
    Project,
    ProjectSettings,
    User,
)
from ai_smm.queue import record_audit


#: Bound on how long a command waits for a job's row lock -- the same
#: value ai_smm.application.publications/content use.
LOCK_TIMEOUT_MS = 3000

#: A sensible ceiling regardless of what the caller asks for: the pilot
#: series is 3; nothing in the agent prompts or the knowledge base is
#: written for a much longer series, and a much larger request is either
#: a mistake or an attempt to run up the LLM bill unbounded.
MAX_SERIES_SIZE = 5
MIN_SERIES_SIZE = 1
MAX_GENERATION_ATTEMPTS = 10
MAX_TOPIC_HINT_LENGTH = 500

# Creation only: historical jobs and their execution retain their strategy.
PUBLISHING_STRATEGIES = frozenset({"standalone_series"})


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class GenerationUnavailable(Exception):
    """The job is gone, or not in the authorised project."""


class GenerationLocked(Exception):
    """The job's row lock could not be taken in time."""


class InvalidGenerationTransition(Exception):
    def __init__(self, *, command: str, status: GenerationJobStatus) -> None:
        super().__init__(
            f"Cannot {command} a generation job with status {status.value}."
        )

        self.command = command
        self.status = status


class InvalidGenerationParams(ValueError):
    """series_size or publishing_strategy is outside what is accepted."""


def _validate_params(
    *,
    series_size: int,
    publishing_strategy: str | None,
    max_attempts: int,
    topic_hint: str | None,
) -> None:
    if not MIN_SERIES_SIZE <= series_size <= MAX_SERIES_SIZE:
        raise InvalidGenerationParams(
            f"series_size must be between {MIN_SERIES_SIZE} and {MAX_SERIES_SIZE}."
        )

    if (
        publishing_strategy is not None
        and publishing_strategy not in PUBLISHING_STRATEGIES
    ):
        raise InvalidGenerationParams(
            "Only standalone_series is supported for new generation jobs."
        )

    if not 1 <= max_attempts <= MAX_GENERATION_ATTEMPTS:
        raise InvalidGenerationParams(
            f"max_attempts must be between 1 and {MAX_GENERATION_ATTEMPTS}."
        )

    if topic_hint is not None and (
        not isinstance(topic_hint, str) or len(topic_hint) > MAX_TOPIC_HINT_LENGTH
    ):
        raise InvalidGenerationParams(
            f"topic_hint must be at most {MAX_TOPIC_HINT_LENGTH} characters."
        )


def _profile_snapshot(db: Session, *, project_id: str) -> dict:
    """The project's editorial profile as it is now, for the job to keep.

    Read from the stored row as it is, not through get_settings_view,
    which coerces a section to a dict: a stored [] or false would become
    {} and its profile would silently vanish. Every section must be a JSON
    object, and the known fields are validated again, because a PATCH only
    checks the sections it supplies (docs/api.md).
    """

    project = db.get(Project, project_id)

    if project is None:
        raise GenerationUnavailable(f"Project {project_id!r} no longer exists.")

    settings = db.get(ProjectSettings, project_id)
    raw_sections = {
        name: getattr(settings, name) if settings is not None else {}
        for name in SETTINGS_SECTIONS
    }

    try:
        for name, value in raw_sections.items():
            if not isinstance(value, dict):
                raise ValueError(
                    f"{name} must be a JSON object, not "
                    f"{type(value).__name__}."
                )

        sections = validate_settings_sections(raw_sections)

        return build_profile_snapshot(
            content_config=sections["content_config"],
            brand_config=sections["brand_config"],
            default_language=project.default_language,
            settings_version=settings.version if settings is not None else None,
        )
    except ValueError as exc:
        raise InvalidGenerationParams(
            "The project content profile is invalid; fix it in the project "
            f"settings first: {exc}"
        ) from exc


def _lock_generation_job(
    db: Session, *, job_id: uuid.UUID, expected_project_id: str
) -> ContentGenerationJob:
    """Re-read the row under a row-level lock; see _lock_publication for
    why this, and not the instance the authorisation dependency loaded,
    is what every command acts on."""

    db.execute(text(f"SET LOCAL lock_timeout = '{int(LOCK_TIMEOUT_MS)}ms'"))

    try:
        job = db.scalars(
            select(ContentGenerationJob)
            .where(ContentGenerationJob.id == job_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one_or_none()
    except OperationalError as exc:
        raise GenerationLocked(
            f"Generation job {job_id} is locked by another transaction; try again."
        ) from exc

    if job is None:
        raise GenerationUnavailable(f"Generation job {job_id} no longer exists.")

    if job.project_id != expected_project_id:
        raise GenerationUnavailable(
            f"Generation job {job_id} does not belong to project "
            f"{expected_project_id!r}."
        )

    return job


def create_generation_job(
    db: Session,
    *,
    project_id: str,
    requested_by_user: User,
    mutations_enabled: bool,
    max_attempts: int = 2,
    series_size: int = 3,
    publishing_strategy: str | None = None,
    topic_hint: str | None = None,
    now: datetime | None = None,
) -> ContentGenerationJob:
    """Queue a job. Nothing is claimed, generated or written beyond this
    one row until a generation worker picks it up."""

    require_mutations_enabled(mutations_enabled, command="start generation")
    # Omitted/null inputs must not inherit an unsupported project strategy.
    if publishing_strategy is None:
        publishing_strategy = "standalone_series"
    _validate_params(
        series_size=series_size,
        publishing_strategy=publishing_strategy,
        max_attempts=max_attempts,
        topic_hint=topic_hint,
    )

    now = now or utcnow()
    # Captured once, here: retries and later settings edits never change it.
    profile = _profile_snapshot(db, project_id=project_id)

    job = ContentGenerationJob(
        id=uuid.uuid4(),
        project_id=project_id,
        requested_by_user_id=requested_by_user.id,
        status=GenerationJobStatus.QUEUED,
        max_attempts=max_attempts,
        params={
            "series_size": series_size,
            "publishing_strategy": publishing_strategy,
            "topic_hint": (topic_hint or "").strip() or None,
            PROFILE_PARAM: profile,
        },
        created_at=now,
        updated_at=now,
    )
    db.add(job)
    db.flush()

    record_audit(
        db,
        actor=f"user:{requested_by_user.id}",
        action="generation.queued",
        subject=f"generation_job:{job.id}",
        details={
            "project_id": project_id,
            "series_size": series_size,
            "publishing_strategy": publishing_strategy,
            "max_attempts": max_attempts,
            "profile_settings_version": profile["settings_version"],
        },
    )

    return job


def cancel_generation_job(
    db: Session,
    *,
    job_id: uuid.UUID,
    expected_project_id: str,
    actor_user: User,
    mutations_enabled: bool,
) -> ContentGenerationJob:
    """queued | claimed -> cancelled.

    A claimed job keeps running to completion in the generation worker's
    process (there is no way to interrupt an in-flight LLM call from
    here); cancelling it only means its result, when the worker finishes,
    is discarded rather than turned into drafts -- the worker checks the
    job's status itself before writing anything (generation_worker).
    """

    require_mutations_enabled(mutations_enabled, command="cancel generation")

    job = _lock_generation_job(
        db, job_id=job_id, expected_project_id=expected_project_id
    )

    if job.status not in (GenerationJobStatus.QUEUED, GenerationJobStatus.CLAIMED):
        raise InvalidGenerationTransition(command="cancel", status=job.status)

    previous_status = job.status
    now = utcnow()
    job.status = GenerationJobStatus.CANCELLED
    job.updated_at = now
    job.finished_at = now

    record_audit(
        db,
        actor=f"user:{actor_user.id}",
        action="generation.cancelled",
        subject=f"generation_job:{job.id}",
        details={"previous_status": previous_status.value},
    )

    return job


# -- reads -----------------------------------------------------------------


def get_generation_job(
    db: Session, *, job_id: uuid.UUID
) -> ContentGenerationJob | None:
    """The row, whatever project owns it; the caller authorises after."""

    return db.get(ContentGenerationJob, job_id)


def _project_generation_jobs(
    project_id: str,
) -> Select[tuple[ContentGenerationJob]]:
    return select(ContentGenerationJob).where(
        ContentGenerationJob.project_id == project_id
    )


def list_generation_jobs_page(
    db: Session,
    *,
    project_id: str,
    statuses: list[GenerationJobStatus] | None = None,
    limit: int,
    offset: int,
) -> Page:
    stmt = _project_generation_jobs(project_id)

    if statuses:
        stmt = stmt.where(ContentGenerationJob.status.in_(statuses))

    total = int(
        db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    )
    rows = list(
        db.scalars(
            stmt.order_by(
                ContentGenerationJob.created_at.desc(),
                ContentGenerationJob.id,
            )
            .limit(limit)
            .offset(offset)
        )
    )

    return Page(items=rows, total=total, limit=limit, offset=offset)
