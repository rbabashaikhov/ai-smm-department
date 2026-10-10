"""Read projections of the publication queue, and the three safe commands.

Two things live here and nothing else.

**Read projections.** Paginated, filtered views of rows the worker owns.
They are read models for the API: they never mutate anything, and every
one of them is scoped to a single project, because the HTTP layer must
not be able to ask a question whose answer spans projects.

**Safe commands.** schedule, reschedule and cancel. The state rules are
not reimplemented here: the mutation is performed by ai_smm.queue, the
same functions the operator CLI and the worker use, and this module only
decides whether the transition is permitted from the row's current
status. That decision is written once, in COMMAND_POLICY, so a router
never compares a status by hand.

Every command first checks the control plane's mutation switch, which
the caller must pass explicitly (ai_smm.application.control_plane): with
the API read-only, a command is refused before the row is even read.

Every command takes a row-level lock before it looks at the status. The
row the authorisation dependency loaded is a snapshot, and a worker can
claim the publication between that read and the write: acting on the
snapshot would then overwrite the claim and wipe the worker's lease while
it was publishing. So a command re-selects the row FOR UPDATE with
populate_existing, re-confirms it still belongs to the project
authorisation approved, and only then checks the transition. See
_lock_publication.

What is deliberately absent: anything that publishes. There is no code
path from here to ThreadsPublisher. The worker remains the only process
that creates a post, and the only publishing-layer function this module
imports is the pure content validator used by the preview.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import Select, func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from ai_smm.application.control_plane import (
    WorkerMode,
    observed_worker_mode,
    require_mutations_enabled,
)
from ai_smm.db.models import (
    MANUAL_ONLY_STATUSES,
    TERMINAL_STATUSES,
    ContentSeries,
    Publication,
    PublicationAttempt,
    PublicationStatus,
)
from ai_smm.publishing.service import PublishBlocked, validate_ready_to_publish
from ai_smm.queue import cancel as queue_cancel
from ai_smm.queue import reschedule as queue_reschedule
from ai_smm.series import check_series_readiness, unpublished_earlier_parts


#: Statuses that mean "a human has to decide what happened", and so can
#: never be moved by an API command. Reconciling one needs the operator to
#: check the real Threads account first, which is a CLI job
#: (`ai-smm reconcile`). Retrying one automatically is exactly the
#: duplicate-post risk the queue was built to avoid.
NEEDS_RECONCILE_HINT = (
    "A human must check the Threads account first; resolve it with "
    "`ai-smm reconcile`."
)


#: How long a command waits for the row lock before giving up. The
#: worker commits its claim before it makes any network call, so the
#: lock it holds lives for one short transaction and this wait is
#: normally microseconds. The bound exists so that an HTTP request can
#: never hang on a row that something else is holding unexpectedly --
#: a stalled transaction, or an operator in a psql session.
LOCK_TIMEOUT_MS = 3000


class PublicationUnavailable(Exception):
    """The row vanished, or moved, between authorisation and the lock."""


class PublicationLocked(Exception):
    """Something else holds the row and did not let go in time."""


class InvalidTransition(Exception):
    """The command is not allowed from the row's current status."""

    def __init__(
        self,
        *,
        command: str,
        status: PublicationStatus,
        allowed: tuple[PublicationStatus, ...],
        hint: str = "",
    ) -> None:
        message = (
            f"Cannot {command} a publication with status "
            f"{status.value}."
        )

        if hint:
            message = f"{message} {hint}"

        super().__init__(message)

        self.command = command
        self.status = status
        self.allowed = allowed
        self.hint = hint


#: From which statuses each command is allowed. Derived from the
#: transition table documented on PublicationStatus:
#:
#:     approved -> scheduled        (schedule)
#:     failed   -> scheduled        (manual reschedule)
#:     scheduled -> scheduled       (moving an existing schedule)
#:     draft|approved|scheduled|failed -> cancelled
#:
#: Absent on purpose: draft cannot be scheduled (it has to be approved
#: first, which is a CLI action and not part of this stage); claimed is
#: held by a worker right now; publishing and needs_review are
#: MANUAL_ONLY; published and cancelled are terminal.
COMMAND_POLICY: dict[str, tuple[PublicationStatus, ...]] = {
    "schedule": (
        PublicationStatus.APPROVED,
        PublicationStatus.FAILED,
    ),
    "reschedule": (
        PublicationStatus.SCHEDULED,
        PublicationStatus.FAILED,
    ),
    "cancel": (
        PublicationStatus.DRAFT,
        PublicationStatus.APPROVED,
        PublicationStatus.SCHEDULED,
        PublicationStatus.FAILED,
    ),
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _lock_publication(
    db: Session, *, publication_id: int, expected_project_id: str
) -> Publication:
    """Re-read the row under a row-level lock, and return the real state.

    Three things happen here, and the order is the point.

    1. ``SELECT ... FOR UPDATE`` takes the row lock. The worker claims a
       publication with ``FOR UPDATE SKIP LOCKED``, so while this lock is
       held the worker does not block on the row -- it simply does not
       see it as a candidate, and goes on to the next one.
    2. ``populate_existing=True`` overwrites the instance already in this
       session's identity map. The authorisation dependency loaded the
       row a moment ago; without this the ORM would hand back that stale
       snapshot, and a later flush would write its fields -- including a
       status of scheduled and a cleared lease -- over whatever the
       worker has since committed. That is the bug this function exists
       to prevent, and nothing else in the call path can prevent it.
    3. The project on the freshly read row is compared with the project
       authorisation approved, so a decision made about one project
       cannot be applied to a row that belongs to another.

    The lock is held until the request transaction commits or rolls
    back, which covers the whole of the command that follows.
    """

    # SET LOCAL is scoped to this transaction, so it cannot leak to the
    # next request that borrows this connection from the pool. The value
    # is an int constant in this module, never anything a client sends.
    db.execute(text(f"SET LOCAL lock_timeout = '{int(LOCK_TIMEOUT_MS)}ms'"))

    try:
        publication = db.scalars(
            select(Publication)
            .where(Publication.id == publication_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one_or_none()
    except OperationalError as exc:
        # lock_timeout fired: the row is busy. Retryable, and nothing was
        # changed, so the caller is told to come back rather than given a
        # verdict about a state we could not read.
        raise PublicationLocked(
            f"Publication {publication_id} is locked by another "
            "transaction; try again."
        ) from exc

    if publication is None:
        raise PublicationUnavailable(
            f"Publication {publication_id} no longer exists."
        )

    if publication.project_id != expected_project_id:
        raise PublicationUnavailable(
            f"Publication {publication_id} does not belong to project "
            f"{expected_project_id!r}."
        )

    return publication


# -- read projections ----------------------------------------------------


@dataclass(frozen=True)
class Page:
    """One page of rows plus the total the filter matches."""

    items: list[Any]
    total: int
    limit: int
    offset: int


def _project_publications(project_id: str) -> Select[tuple[Publication]]:
    """Every publications query starts here, so none can span projects."""

    return select(Publication).where(Publication.project_id == project_id)


def list_publications_page(
    db: Session,
    *,
    project_id: str,
    statuses: list[PublicationStatus] | None = None,
    publication_format: str | None = None,
    series_id: int | None = None,
    human_reviewed: bool | None = None,
    limit: int,
    offset: int,
) -> Page:
    """Publications of one project, filtered and paginated.

    Ordering is by ordinal, as in `ai-smm queue`, so the API and the CLI
    show an operator the same sequence.
    """

    stmt = _project_publications(project_id)

    if statuses:
        stmt = stmt.where(Publication.status.in_(statuses))

    if publication_format:
        stmt = stmt.where(Publication.format == publication_format)

    if series_id is not None:
        stmt = stmt.where(Publication.series_id == series_id)

    if human_reviewed is not None:
        stmt = stmt.where(Publication.human_reviewed.is_(human_reviewed))

    total = int(
        db.execute(
            select(func.count()).select_from(stmt.subquery())
        ).scalar_one()
    )

    rows = list(
        db.execute(
            stmt.order_by(Publication.ordinal, Publication.id)
            .limit(limit)
            .offset(offset)
        ).scalars()
    )

    return Page(items=rows, total=total, limit=limit, offset=offset)


def get_publication_in_project(
    db: Session, *, publication_id: int
) -> Publication | None:
    """The row, whatever project it belongs to.

    The caller derives project_id from the row it gets back and checks
    membership against that: the id in the URL says nothing about who may
    read it, and the body is never consulted.
    """

    return db.get(Publication, publication_id)


def list_attempts(
    db: Session, *, publication: Publication, limit: int, offset: int
) -> Page:
    stmt = select(PublicationAttempt).where(
        PublicationAttempt.publication_id == publication.id
    )

    total = int(
        db.execute(
            select(func.count()).select_from(stmt.subquery())
        ).scalar_one()
    )

    rows = list(
        db.execute(
            stmt.order_by(PublicationAttempt.attempt_number)
            .limit(limit)
            .offset(offset)
        ).scalars()
    )

    return Page(items=rows, total=total, limit=limit, offset=offset)


@dataclass(frozen=True)
class PublicationPreview:
    """What the worker would send, and whether it could send it now.

    `content_valid` is the existing preflight from the publishing
    service, run with the claim and review gates relaxed exactly as the
    CLI preview relaxes them: an operator inspecting a record holds no
    claim, and an unreviewed record is the normal thing to preview --
    that is how they decide whether to approve it.

    Nothing here contacts Threads. Running the validator is the whole of
    it; it is a pure function of the row.
    """

    publication: Publication
    content_valid: bool
    content_error: str | None
    human_reviewed: bool
    series_ready: bool
    series_reason: str | None
    blocking_parts: list[int]
    publishable_now: bool


def preview_publication(
    db: Session, *, publication: Publication
) -> PublicationPreview:
    content_error: str | None = None

    try:
        validate_ready_to_publish(
            publication,
            require_claimed=False,
            require_human_review=False,
        )
    except PublishBlocked as exc:
        content_error = str(exc)

    readiness = check_series_readiness(db, publication)
    blocking = [
        part.id for part in unpublished_earlier_parts(db, publication)
    ]

    return PublicationPreview(
        publication=publication,
        content_valid=content_error is None,
        content_error=content_error,
        human_reviewed=publication.human_reviewed,
        series_ready=bool(readiness),
        series_reason=readiness.reason or None,
        blocking_parts=blocking,
        publishable_now=(
            content_error is None
            and publication.human_reviewed
            and bool(readiness)
        ),
    )


# -- safe commands -------------------------------------------------------


def _require_transition(
    *, command: str, publication: Publication
) -> None:
    allowed = COMMAND_POLICY[command]

    if publication.status in allowed:
        return

    hint = ""

    if publication.status in MANUAL_ONLY_STATUSES:
        hint = NEEDS_RECONCILE_HINT
    elif publication.status in TERMINAL_STATUSES:
        hint = "That status is terminal."
    elif publication.status is PublicationStatus.CLAIMED:
        hint = (
            "A worker holds a lease on it; it returns to scheduled by "
            "itself when the lease expires."
        )
    elif publication.status is PublicationStatus.DRAFT:
        hint = (
            "Approve the exact text first (`ai-smm approve`), which also "
            "moves it to approved."
        )

    raise InvalidTransition(
        command=command,
        status=publication.status,
        allowed=allowed,
        hint=hint,
    )


def schedule_publication(
    db: Session,
    *,
    publication_id: int,
    expected_project_id: str,
    scheduled_at: datetime,
    actor: str,
    mutations_enabled: bool,
    note: str | None = None,
    reset_attempts: bool = False,
    command: str = "schedule",
) -> Publication:
    """Put an approved or failed row on the schedule.

    The row is locked and re-read first, so the status the transition is
    checked against is the one in the database now, not the one the
    authorisation dependency saw.

    The mutation and its audit entry are ai_smm.queue.reschedule, exactly
    as the CLI does it; the only thing added here is the check that the
    transition is one this command is allowed to make. queue.reschedule
    keeps its own refusal for a row that already carries publish
    metadata, which is the barrier that matters, and that refusal is
    surfaced as the same conflict.
    """

    require_mutations_enabled(mutations_enabled, command=command)

    publication = _lock_publication(
        db,
        publication_id=publication_id,
        expected_project_id=expected_project_id,
    )

    _require_transition(command=command, publication=publication)

    if scheduled_at.tzinfo is None:
        # The CLI reads a naive time in the operator timezone; an API must
        # not guess one, so the caller states the offset.
        raise ValueError("scheduled_at must include a timezone offset.")

    try:
        queue_reschedule(
            db,
            publication,
            scheduled_at=scheduled_at,
            actor=actor,
            note=note,
            reset_attempts=reset_attempts,
        )
    except ValueError as exc:
        raise InvalidTransition(
            command=command,
            status=publication.status,
            allowed=COMMAND_POLICY[command],
            hint=str(exc),
        ) from exc

    return publication


def reschedule_publication(
    db: Session,
    *,
    publication_id: int,
    expected_project_id: str,
    scheduled_at: datetime,
    actor: str,
    mutations_enabled: bool,
    note: str | None = None,
    reset_attempts: bool = False,
) -> Publication:
    """Move the schedule of a row that is already scheduled, or retry a failure."""

    return schedule_publication(
        db,
        publication_id=publication_id,
        expected_project_id=expected_project_id,
        scheduled_at=scheduled_at,
        actor=actor,
        mutations_enabled=mutations_enabled,
        note=note,
        reset_attempts=reset_attempts,
        command="reschedule",
    )


def cancel_publication(
    db: Session,
    *,
    publication_id: int,
    expected_project_id: str,
    actor: str,
    mutations_enabled: bool,
    note: str | None = None,
) -> Publication:
    """Take a row out of the queue for good.

    queue.cancel itself cancels whatever it is given -- it is also the
    tail of the operator's reconcile flow, where cancelling an ambiguous
    row is the right answer. Over HTTP there is no such context, so the
    policy below refuses the ambiguous and terminal statuses before the
    mutation is reached, against the locked and freshly read row.
    """

    require_mutations_enabled(mutations_enabled, command="cancel")

    publication = _lock_publication(
        db,
        publication_id=publication_id,
        expected_project_id=expected_project_id,
    )

    _require_transition(command="cancel", publication=publication)

    queue_cancel(db, publication, actor=actor, note=note)

    return publication


# -- operations views ----------------------------------------------------

#: What "needs a human" means in the attention view: a failure that ran
#: out of attempts, and a row whose outcome is unknown. Neither is ever
#: retried automatically.
ATTENTION_STATUSES = (
    PublicationStatus.FAILED,
    PublicationStatus.NEEDS_REVIEW,
)


@dataclass(frozen=True)
class OperationsSummary:
    project_id: str
    total: int
    by_status: dict[str, int]
    due_now: int
    needs_attention: int
    published_last_24h: int
    last_published_at: datetime | None
    next_scheduled_at: datetime | None
    series_total: int
    series_active: int
    #: This API process's own AI_SMM_DRY_RUN. It gates nothing the API
    #: does and says nothing about the worker; reported so it cannot be
    #: mistaken for the worker's mode.
    api_dry_run: bool
    api_mutations_enabled: bool
    #: What the worker will do with a due publication, as far as a
    #: verifiable source says; UNKNOWN when there is none.
    worker_mode: WorkerMode
    worker_mode_source: str | None


def operations_summary(
    db: Session,
    *,
    project_id: str,
    api_dry_run: bool,
    api_mutations_enabled: bool,
    now: datetime | None = None,
) -> OperationsSummary:
    """Counters for one project.

    Every aggregate is filtered by project_id. The platform-wide helpers
    in ai_smm.queue (count_published_since, last_published_at) are
    deliberately not used here: they answer across all projects, which
    over HTTP would leak one project's activity into another's summary.
    """

    now = now or utcnow()

    counts = {
        status.value: 0 for status in PublicationStatus
    }

    for status, count in db.execute(
        select(Publication.status, func.count())
        .where(Publication.project_id == project_id)
        .group_by(Publication.status)
    ).all():
        counts[status.value] = int(count)

    due_now = int(
        db.execute(
            select(func.count()).where(
                Publication.project_id == project_id,
                Publication.status == PublicationStatus.SCHEDULED,
                Publication.scheduled_at.is_not(None),
                Publication.scheduled_at <= now,
            )
        ).scalar_one()
    )

    published_last_24h = int(
        db.execute(
            select(func.count()).where(
                Publication.project_id == project_id,
                Publication.status == PublicationStatus.PUBLISHED,
                Publication.published_at.is_not(None),
                Publication.published_at >= now - timedelta(hours=24),
            )
        ).scalar_one()
    )

    last_published = db.execute(
        select(func.max(Publication.published_at)).where(
            Publication.project_id == project_id,
            Publication.status == PublicationStatus.PUBLISHED,
        )
    ).scalar_one()

    next_scheduled = db.execute(
        select(func.min(Publication.scheduled_at)).where(
            Publication.project_id == project_id,
            Publication.status == PublicationStatus.SCHEDULED,
        )
    ).scalar_one()

    series_total = int(
        db.execute(
            select(func.count()).where(
                ContentSeries.project_id == project_id
            )
        ).scalar_one()
    )
    series_active = int(
        db.execute(
            select(func.count()).where(
                ContentSeries.project_id == project_id,
                ContentSeries.status == "active",
            )
        ).scalar_one()
    )

    # Never derived from api_dry_run: see ai_smm.application.control_plane.
    worker = observed_worker_mode()

    return OperationsSummary(
        project_id=project_id,
        total=sum(counts.values()),
        by_status=counts,
        due_now=due_now,
        needs_attention=sum(
            counts[status.value] for status in ATTENTION_STATUSES
        ),
        published_last_24h=published_last_24h,
        last_published_at=last_published,
        next_scheduled_at=next_scheduled,
        series_total=series_total,
        series_active=series_active,
        api_dry_run=api_dry_run,
        api_mutations_enabled=api_mutations_enabled,
        worker_mode=worker.mode,
        worker_mode_source=worker.source,
    )


def list_attention_page(
    db: Session, *, project_id: str, limit: int, offset: int
) -> Page:
    """Rows waiting for a human: failed, and outcome unknown."""

    stmt = _project_publications(project_id).where(
        Publication.status.in_(ATTENTION_STATUSES)
    )

    total = int(
        db.execute(
            select(func.count()).select_from(stmt.subquery())
        ).scalar_one()
    )

    rows = list(
        db.execute(
            stmt.order_by(Publication.updated_at.desc(), Publication.id)
            .limit(limit)
            .offset(offset)
        ).scalars()
    )

    return Page(items=rows, total=total, limit=limit, offset=offset)
