"""Topic ideation over HTTP: queue a job, read jobs and suggested ideas
(SMM-035B3A).

The same split ai_smm.application.generation makes.

**Read projections.** Paginated views of topic_ideation_jobs and
content_ideas, always filtered by the project the caller was authorised
for: a row of another project is never returned, not even by id.

**One safe command.** create. It writes the single row a guarded role may
write (guard_topic_ideation_jobs_insert): a freshly queued job. Claiming,
outcomes and every content_ideas row belong to the topic_ideation_*
SECURITY DEFINER contracts (ai_smm.db.ideation_contracts), which nothing
in this module calls. There is no reject or use command yet.

Creation is closed twice: by the control plane's mutation switch, like
every command, and by Settings.topic_ideation_jobs_enabled, which no
runtime can turn on until an ideation worker exists.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Select, func, insert, select
from sqlalchemy.orm import Session

from ai_smm.application.control_plane import require_mutations_enabled
from ai_smm.application.publications import Page
from ai_smm.db.ideation_contracts import (
    MAX_IDEAS_PER_JOB,
    MAX_TOPIC_HINT_LENGTH,
    MIN_IDEAS_PER_JOB,
)
from ai_smm.db.models import (
    ContentIdea,
    ContentIdeaStatus,
    Project,
    TopicIdeationJob,
    TopicIdeationJobStatus,
    User,
)
from ai_smm.queue import record_audit


#: The retry ceiling every new ideation job gets; the same default a
#: generation job has. Bounded 1..10 by the table and the insert guard.
DEFAULT_IDEATION_MAX_ATTEMPTS = 2


class TopicIdeationDisabled(Exception):
    """Queuing ideation jobs is switched off on this deployment."""


class TopicIdeationProjectInactive(Exception):
    """The project is deactivated; nothing new is started for it."""


class InvalidTopicIdeationParams(ValueError):
    """idea_count or topic_hint is outside what is accepted."""


def require_topic_ideation_enabled(enabled: bool) -> None:
    if not enabled:
        raise TopicIdeationDisabled(
            "Topic ideation is not enabled on this deployment."
        )


def _validate_params(*, idea_count: object, topic_hint: object) -> str | None:
    # bool is an int in Python; it is never a count.
    if (
        not isinstance(idea_count, int)
        or isinstance(idea_count, bool)
        or not MIN_IDEAS_PER_JOB <= idea_count <= MAX_IDEAS_PER_JOB
    ):
        raise InvalidTopicIdeationParams(
            f"idea_count must be between {MIN_IDEAS_PER_JOB} and "
            f"{MAX_IDEAS_PER_JOB}."
        )

    if topic_hint is None:
        return None

    if not isinstance(topic_hint, str):
        raise InvalidTopicIdeationParams("topic_hint must be a string.")

    hint = topic_hint.strip()

    if len(hint) > MAX_TOPIC_HINT_LENGTH:
        raise InvalidTopicIdeationParams(
            f"topic_hint must be at most {MAX_TOPIC_HINT_LENGTH} characters."
        )

    return hint or None


def create_topic_ideation_job(
    db: Session,
    *,
    project: Project,
    requested_by_user: User,
    mutations_enabled: bool,
    ideation_enabled: bool,
    idea_count: int,
    topic_hint: str | None = None,
) -> TopicIdeationJob:
    """Queue a job. Both switches are checked before anything is read or
    written; nothing is claimed or generated until an ideation worker
    (a later stage) picks the row up."""

    require_mutations_enabled(mutations_enabled, command="start topic ideation")
    require_topic_ideation_enabled(ideation_enabled)

    hint = _validate_params(idea_count=idea_count, topic_hint=topic_hint)

    if not project.is_active:
        raise TopicIdeationProjectInactive(
            f"Project {project.id!r} is not active."
        )

    # The request columns only, not an ORM add(): that would name every
    # column, the worker-owned ones as NULL, and need a grant on each
    # (deploy/control-center/db/ai_smm_cc_rw.grants.sql grants exactly
    # these). Timestamps are the database's (the insert guard sets them).
    job = db.scalars(
        insert(TopicIdeationJob)
        .values(
            id=uuid.uuid4(),
            project_id=project.id,
            requested_by_user_id=requested_by_user.id,
            status=TopicIdeationJobStatus.QUEUED,
            idea_count=idea_count,
            topic_hint=hint,
            max_attempts=DEFAULT_IDEATION_MAX_ATTEMPTS,
        )
        .returning(TopicIdeationJob)
    ).one()

    record_audit(
        db,
        actor=f"user:{requested_by_user.id}",
        action="generation.ideation_queued",
        subject=f"topic_ideation_job:{job.id}",
        details={
            "project_id": project.id,
            "idea_count": idea_count,
            "max_attempts": job.max_attempts,
        },
    )

    return job


# -- reads -----------------------------------------------------------------


def get_topic_ideation_job(
    db: Session, *, project_id: str, job_id: uuid.UUID
) -> TopicIdeationJob | None:
    """The job only if it belongs to project_id; another project's job
    is indistinguishable from one that does not exist."""

    return db.scalars(
        select(TopicIdeationJob).where(
            TopicIdeationJob.id == job_id,
            TopicIdeationJob.project_id == project_id,
        )
    ).one_or_none()


def _page(db: Session, stmt: Select, *, order_by, limit: int, offset: int) -> Page:
    total = int(
        db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    )
    rows = list(db.scalars(stmt.order_by(*order_by).limit(limit).offset(offset)))

    return Page(items=rows, total=total, limit=limit, offset=offset)


def list_topic_ideation_jobs_page(
    db: Session,
    *,
    project_id: str,
    statuses: list[TopicIdeationJobStatus] | None = None,
    limit: int,
    offset: int,
) -> Page:
    stmt = select(TopicIdeationJob).where(TopicIdeationJob.project_id == project_id)

    if statuses:
        stmt = stmt.where(TopicIdeationJob.status.in_(statuses))

    return _page(
        db,
        stmt,
        order_by=(TopicIdeationJob.created_at.desc(), TopicIdeationJob.id),
        limit=limit,
        offset=offset,
    )


def list_content_ideas_page(
    db: Session,
    *,
    project_id: str,
    statuses: list[ContentIdeaStatus] | None = None,
    ideation_job_id: uuid.UUID | None = None,
    limit: int,
    offset: int,
) -> Page:
    stmt = select(ContentIdea).where(ContentIdea.project_id == project_id)

    if statuses:
        stmt = stmt.where(ContentIdea.status.in_(statuses))

    if ideation_job_id is not None:
        stmt = stmt.where(ContentIdea.ideation_job_id == ideation_job_id)

    return _page(
        db,
        stmt,
        order_by=(
            ContentIdea.created_at.desc(),
            ContentIdea.ideation_job_id,
            ContentIdea.position,
        ),
        limit=limit,
        offset=offset,
    )
