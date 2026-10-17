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
from ai_smm.application.publications import Page
from ai_smm.db.models import ContentGenerationJob, GenerationJobStatus, User
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

PUBLISHING_STRATEGIES = frozenset({"standalone_series", "reply_thread"})


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
    *, series_size: int, publishing_strategy: str | None
) -> None:
    if not MIN_SERIES_SIZE <= series_size <= MAX_SERIES_SIZE:
        raise InvalidGenerationParams(
            f"series_size must be between {MIN_SERIES_SIZE} and "
            f"{MAX_SERIES_SIZE}."
        )

    if (
        publishing_strategy is not None
        and publishing_strategy not in PUBLISHING_STRATEGIES
    ):
        raise InvalidGenerationParams(
            "publishing_strategy must be one of "
            f"{sorted(PUBLISHING_STRATEGIES)} or omitted."
        )


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
            f"Generation job {job_id} is locked by another transaction; "
            "try again."
        ) from exc

    if job is None:
        raise GenerationUnavailable(
            f"Generation job {job_id} no longer exists."
        )

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
    series_size: int = 3,
    publishing_strategy: str | None = None,
    topic_hint: str | None = None,
    now: datetime | None = None,
) -> ContentGenerationJob:
    """Queue a job. Nothing is claimed, generated or written beyond this
    one row until a generation worker picks it up."""

    require_mutations_enabled(mutations_enabled, command="start generation")
    _validate_params(
        series_size=series_size, publishing_strategy=publishing_strategy
    )

    now = now or utcnow()

    job = ContentGenerationJob(
        id=uuid.uuid4(),
        project_id=project_id,
        requested_by_user_id=requested_by_user.id,
        status=GenerationJobStatus.QUEUED,
        params={
            "series_size": series_size,
            "publishing_strategy": publishing_strategy,
            "topic_hint": (topic_hint or "").strip() or None,
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
        db.execute(
            select(func.count()).select_from(stmt.subquery())
        ).scalar_one()
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
