"""Topic ideation over HTTP: queue a job, read jobs and suggested ideas
(SMM-035B3A).

The same split ai_smm.application.generation makes.

**Read projections.** Paginated views of topic_ideation_jobs and
content_ideas, always filtered by the project the caller was authorised
for: a row of another project is never returned, not even by id.

**One safe command.** create. It writes the single row a guarded role may
write (guard_topic_ideation_jobs_insert): a freshly queued job, with the
project's profile snapshot and recent idea titles in its params
(SMM-035B3B1) -- the ideation worker reads no table. Claiming,
outcomes and every content_ideas row belong to the topic_ideation_*
SECURITY DEFINER contracts (ai_smm.db.ideation_contracts), which nothing
in this module calls.

**Two idea commands** (SMM-035B3B2), through the content_idea_* contracts
(ai_smm.db.idea_selection_contracts) -- the business role cannot write
content_ideas itself:

* reject_content_idea: proposed -> rejected. No job, no content.
* use_content_idea: proposed -> used with exactly one new generation job,
  created by the ordinary create_generation_job (same validation, profile
  snapshot and audit) in the same transaction. The idea row is locked from
  the first contract call to COMMIT, so a concurrent use or reject waits
  and then sees the terminal state; a repeated use returns the job the
  first one created. Nothing here calls an LLM or creates content.

Creation is closed twice: by the control plane's mutation switch, like
every command, and by Settings.topic_ideation_jobs_enabled, which no
runtime can turn on yet (the worker exists since SMM-035B3B1; enabling
the API is a separately approved stage).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import Select, func, insert, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from ai_smm.application.control_plane import require_mutations_enabled
from ai_smm.application.generation import (
    LOCK_TIMEOUT_MS,
    InvalidGenerationParams,
    create_generation_job,
    get_generation_job,
    project_profile_snapshot,
)
from ai_smm.application.generation import (
    MAX_TOPIC_HINT_LENGTH as MAX_GENERATION_TOPIC_HINT_LENGTH,
)
from ai_smm.application.publications import Page
from ai_smm.db.ideation_contracts import (
    IDEATION_PROFILE_PARAM,
    IDEATION_RECENT_TITLES_PARAM,
    MAX_IDEAS_PER_JOB,
    MAX_RECENT_IDEA_TITLES,
    MAX_TOPIC_HINT_LENGTH,
    MIN_IDEAS_PER_JOB,
)
from ai_smm.db.models import (
    ContentGenerationJob,
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


class IdeaUnavailable(Exception):
    """The idea does not exist in the authorised project."""


class IdeaLocked(Exception):
    """The idea's row lock could not be taken in time."""


class InvalidIdeaTransition(Exception):
    def __init__(self, *, command: str, status: ContentIdeaStatus) -> None:
        super().__init__(f"Cannot {command} an idea with status {status.value}.")

        self.command = command
        self.status = status


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
    (ai_smm.ideation_worker) picks the row up."""

    require_mutations_enabled(mutations_enabled, command="start topic ideation")
    require_topic_ideation_enabled(ideation_enabled)

    hint = _validate_params(idea_count=idea_count, topic_hint=topic_hint)

    if not project.is_active:
        raise TopicIdeationProjectInactive(
            f"Project {project.id!r} is not active."
        )

    # Captured once, here (SMM-035B3B1): the worker reads no table, and a
    # retry or a later settings edit never changes what the job runs on.
    try:
        profile = project_profile_snapshot(db, project_id=project.id)
    except InvalidGenerationParams as exc:
        raise InvalidTopicIdeationParams(str(exc)) from exc

    recent_titles = _recent_idea_titles(db, project_id=project.id)

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
            params={
                IDEATION_PROFILE_PARAM: profile,
                IDEATION_RECENT_TITLES_PARAM: recent_titles,
            },
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
            "profile_settings_version": profile["settings_version"],
            "recent_idea_titles": len(recent_titles),
        },
    )

    return job


def _recent_idea_titles(db: Session, *, project_id: str) -> list[str]:
    """The project's newest idea titles, whatever their status, so the
    model is told what not to suggest again. Only this project's; at most
    MAX_RECENT_IDEA_TITLES, case-insensitively distinct."""

    rows = db.scalars(
        select(ContentIdea.title)
        .where(ContentIdea.project_id == project_id)
        .order_by(
            ContentIdea.created_at.desc(),
            ContentIdea.ideation_job_id,
            ContentIdea.position,
        )
        .limit(MAX_RECENT_IDEA_TITLES * 5)
    )

    titles: list[str] = []
    seen: set[str] = set()

    for title in rows:
        key = title.casefold()

        if key in seen:
            continue

        seen.add(key)
        titles.append(title)

        if len(titles) == MAX_RECENT_IDEA_TITLES:
            break

    return titles


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


# -- idea commands (SMM-035B3B2) --------------------------------------------

_BEGIN_USE = text(
    "SELECT idea_id, idea_status, title, description, angle, generation_job_id "
    "FROM public.content_idea_begin_use(:project_id, :idea_id)"
)
_FINISH_USE = text(
    "SELECT public.content_idea_finish_use(:project_id, :idea_id, :job_id)"
)
_REJECT = text("SELECT public.content_idea_reject(:project_id, :idea_id)")


@dataclass(frozen=True)
class IdeaUse:
    idea: ContentIdea
    job: ContentGenerationJob
    #: True when the idea was already used: nothing was created.
    replayed: bool
    #: Which of title/description/angle went into the job's topic_hint.
    topic_includes: tuple[str, ...]


def _set_lock_timeout(db: Session) -> None:
    db.execute(text(f"SET LOCAL lock_timeout = '{int(LOCK_TIMEOUT_MS)}ms'"))


def _reload_idea(db: Session, *, idea_id: uuid.UUID) -> ContentIdea:
    return db.scalars(
        select(ContentIdea)
        .where(ContentIdea.id == idea_id)
        .execution_options(populate_existing=True)
    ).one()


def idea_generation_topic(
    *, title: str, description: str, angle: str | None
) -> tuple[str, tuple[str, ...]]:
    """The generation job's topic_hint for an idea, and what it carries.

    The title always goes in whole (an idea title is at most 200
    characters, a topic_hint 500). Description, then angle, are added only
    while the whole text still fits; a part that would not fit is left out
    entirely rather than cut, and the result says so (topic_includes is in
    the audit entry and the API response), so nothing is lost silently.
    """

    hint = title.strip()
    included = ["title"]

    for name, part in (("description", description), ("angle", angle)):
        part = (part or "").strip()

        if not part:
            continue

        candidate = f"{hint}\n\n{part}"

        if len(candidate) > MAX_GENERATION_TOPIC_HINT_LENGTH:
            continue

        hint = candidate
        included.append(name)

    return hint, tuple(included)


def reject_content_idea(
    db: Session,
    *,
    project: Project,
    idea_id: uuid.UUID,
    actor_user: User,
    mutations_enabled: bool,
) -> ContentIdea:
    """proposed -> rejected. Another project's idea is not found; a
    rejected or used idea is an invalid transition."""

    require_mutations_enabled(mutations_enabled, command="reject a topic idea")

    _set_lock_timeout(db)

    try:
        previous = db.execute(
            _REJECT, {"project_id": project.id, "idea_id": idea_id}
        ).scalar_one()
    except OperationalError as exc:
        raise IdeaLocked(
            f"Idea {idea_id} is locked by another transaction; try again."
        ) from exc

    if previous is None:
        raise IdeaUnavailable(f"Idea {idea_id} does not exist in this project.")

    if previous != ContentIdeaStatus.PROPOSED.value:
        raise InvalidIdeaTransition(
            command="reject", status=ContentIdeaStatus(previous)
        )

    record_audit(
        db,
        actor=f"user:{actor_user.id}",
        action="generation.idea_rejected",
        subject=f"content_idea:{idea_id}",
        details={"project_id": project.id, "previous_status": previous},
    )

    return _reload_idea(db, idea_id=idea_id)


def use_content_idea(
    db: Session,
    *,
    project: Project,
    idea_id: uuid.UUID,
    actor_user: User,
    mutations_enabled: bool,
    max_attempts: int,
) -> IdeaUse:
    """proposed -> used, with exactly one queued generation job.

    One transaction: if the job cannot be created (or anything after it
    fails), the idea stays proposed and no job exists. An idea that is
    already used returns its job and creates nothing; a rejected one is an
    invalid transition.
    """

    require_mutations_enabled(mutations_enabled, command="use a topic idea")

    _set_lock_timeout(db)

    try:
        row = db.execute(
            _BEGIN_USE, {"project_id": project.id, "idea_id": idea_id}
        ).one_or_none()
    except OperationalError as exc:
        raise IdeaLocked(
            f"Idea {idea_id} is locked by another transaction; try again."
        ) from exc

    if row is None:
        raise IdeaUnavailable(f"Idea {idea_id} does not exist in this project.")

    status = ContentIdeaStatus(row.idea_status)

    if status is ContentIdeaStatus.USED:
        job = get_generation_job(db, job_id=row.generation_job_id)

        if job is None or job.project_id != project.id:
            # The composite key makes this impossible; fail closed anyway.
            raise IdeaUnavailable(f"Idea {idea_id} has no generation job.")

        return IdeaUse(
            idea=_reload_idea(db, idea_id=idea_id),
            job=job,
            replayed=True,
            topic_includes=(),
        )

    if status is not ContentIdeaStatus.PROPOSED:
        raise InvalidIdeaTransition(command="use", status=status)

    topic_hint, topic_includes = idea_generation_topic(
        title=row.title, description=row.description, angle=row.angle
    )

    job = create_generation_job(
        db,
        project_id=project.id,
        requested_by_user=actor_user,
        mutations_enabled=mutations_enabled,
        max_attempts=max_attempts,
        series_size=1,
        publishing_strategy="standalone_series",
        topic_hint=topic_hint,
    )

    db.execute(
        _FINISH_USE,
        {"project_id": project.id, "idea_id": idea_id, "job_id": job.id},
    )

    record_audit(
        db,
        actor=f"user:{actor_user.id}",
        action="generation.idea_used",
        subject=f"content_idea:{idea_id}",
        details={
            "project_id": project.id,
            "generation_job_id": str(job.id),
            "topic_includes": list(topic_includes),
        },
    )

    return IdeaUse(
        idea=_reload_idea(db, idea_id=idea_id),
        job=job,
        replayed=False,
        topic_includes=topic_includes,
    )
