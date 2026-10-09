"""Queue operations against PostgreSQL.

The rules that matter are enforced here, not in the worker:

* claiming is a single atomic UPDATE with FOR UPDATE SKIP LOCKED, so any
  number of workers can run without ever selecting the same row;
* a lease bounds how long a claim is valid, and an expired claim is released
  back to scheduled -- but a row in publishing is never released, because the
  state of the external system is unknown;
* an attempt row is written before the external call, so an ambiguous outcome
  is always reconstructable.
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from ai_smm.db.models import (
    AttemptOutcome,
    AttemptPhase,
    AuditLog,
    Project,
    Publication,
    PublicationAttempt,
    PublicationStatus,
)
from ai_smm.logging_setup import get_logger


logger = get_logger(__name__)

#: Advisory lock key for "only one publishing cycle at a time" on top of the
#: row level guarantees. Arbitrary but stable.
PUBLISH_CYCLE_LOCK_KEY = 0x4149_534D


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def record_audit(
    session: Session,
    *,
    actor: str,
    action: str,
    subject: str | None = None,
    details: dict[str, Any] | None = None,
) -> AuditLog:
    entry = AuditLog(
        actor=actor,
        action=action,
        subject=subject,
        details=details or {},
    )

    session.add(entry)

    return entry


def ensure_project(
    session: Session,
    *,
    project_id: str,
    display_name: str,
    knowledge_path: str,
    default_platform: str = "threads",
) -> Project:
    project = session.get(Project, project_id)

    if project is None:
        project = Project(
            id=project_id,
            display_name=display_name,
            knowledge_path=knowledge_path,
            default_platform=default_platform,
        )
        session.add(project)
        session.flush()

        return project

    project.display_name = display_name
    project.knowledge_path = knowledge_path
    project.updated_at = utcnow()

    return project


def list_publications(
    session: Session,
    *,
    project_id: str | None = None,
    statuses: Sequence[PublicationStatus] | None = None,
    limit: int = 100,
) -> list[Publication]:
    stmt = select(Publication).order_by(
        Publication.project_id,
        Publication.ordinal,
    )

    if project_id:
        stmt = stmt.where(Publication.project_id == project_id)

    if statuses:
        stmt = stmt.where(Publication.status.in_(list(statuses)))

    return list(session.execute(stmt.limit(limit)).scalars())


def get_publication(
    session: Session, publication_id: int
) -> Publication | None:
    return session.get(Publication, publication_id)


def count_published_since(
    session: Session,
    *,
    platform: str,
    since: datetime,
) -> int:
    stmt = select(func.count()).where(
        Publication.platform == platform,
        Publication.status == PublicationStatus.PUBLISHED,
        Publication.published_at.is_not(None),
        Publication.published_at >= since,
    )

    return int(session.execute(stmt).scalar_one())


def last_published_at(
    session: Session, *, platform: str
) -> datetime | None:
    stmt = select(func.max(Publication.published_at)).where(
        Publication.platform == platform,
        Publication.status == PublicationStatus.PUBLISHED,
    )

    return session.execute(stmt).scalar_one()


def release_expired_leases(
    session: Session,
    *,
    actor: str,
    now: datetime | None = None,
) -> list[int]:
    """Return claimed-but-abandoned rows to scheduled.

    Only status == claimed is released. A row in publishing means an external
    call was already in flight: its outcome is unknown, so it goes to
    needs_review instead (see quarantine_abandoned_publishing).
    """

    now = now or utcnow()

    stmt = (
        update(Publication)
        .where(
            Publication.status == PublicationStatus.CLAIMED,
            Publication.lease_expires_at.is_not(None),
            Publication.lease_expires_at <= now,
        )
        .values(
            status=PublicationStatus.SCHEDULED,
            claimed_by=None,
            claimed_at=None,
            lease_expires_at=None,
            updated_at=now,
        )
        .returning(Publication.id)
        .execution_options(synchronize_session="fetch")
    )

    released = [int(row) for row in session.execute(stmt).scalars()]

    for publication_id in released:
        record_audit(
            session,
            actor=actor,
            action="lease_released",
            subject=f"publication:{publication_id}",
            details={"reason": "lease_expired"},
        )

    return released


def quarantine_abandoned_publishing(
    session: Session,
    *,
    actor: str,
    now: datetime | None = None,
) -> list[int]:
    """Move rows left in publishing by a dead worker into needs_review.

    This is the crash path. The worker died after it started talking to
    Threads, so the post may or may not exist. Automatic retry is forbidden.
    """

    now = now or utcnow()

    stmt = (
        update(Publication)
        .where(
            Publication.status == PublicationStatus.PUBLISHING,
            or_(
                Publication.lease_expires_at.is_(None),
                Publication.lease_expires_at <= now,
            ),
        )
        .values(
            status=PublicationStatus.NEEDS_REVIEW,
            claimed_by=None,
            claimed_at=None,
            lease_expires_at=None,
            last_error=(
                "Worker stopped while publishing. The Threads result is "
                "unknown: verify the account before any retry."
            ),
            updated_at=now,
        )
        .returning(Publication.id)
        .execution_options(synchronize_session="fetch")
    )

    quarantined = [int(row) for row in session.execute(stmt).scalars()]

    for publication_id in quarantined:
        open_attempt = session.execute(
            select(PublicationAttempt)
            .where(
                PublicationAttempt.publication_id == publication_id,
                PublicationAttempt.outcome.is_(None),
            )
            .order_by(PublicationAttempt.attempt_number.desc())
            .limit(1)
        ).scalar_one_or_none()

        if open_attempt is not None:
            open_attempt.outcome = AttemptOutcome.UNKNOWN
            open_attempt.finished_at = now
            open_attempt.error_type = "WorkerCrash"
            open_attempt.error_message = (
                "Worker stopped mid-publish; outcome unknown."
            )

        record_audit(
            session,
            actor=actor,
            action="quarantined_needs_review",
            subject=f"publication:{publication_id}",
            details={"reason": "worker_crash_during_publishing"},
        )

        logger.error(
            "Publication quarantined after worker crash",
            extra={
                "context": {
                    "publication_id": publication_id,
                    "status": PublicationStatus.NEEDS_REVIEW.value,
                }
            },
        )

    return quarantined


def claim_due_publication(
    session: Session,
    *,
    worker_id: str,
    lease_seconds: int,
    platform: str = "threads",
    now: datetime | None = None,
) -> Publication | None:
    """Atomically reserve one due publication, or return None.

    FOR UPDATE SKIP LOCKED means a concurrent worker running the same
    statement simply sees no row instead of blocking or double-claiming.
    """

    now = now or utcnow()

    candidate_stmt = (
        select(Publication.id)
        .where(
            Publication.status == PublicationStatus.SCHEDULED,
            Publication.platform == platform,
            Publication.scheduled_at.is_not(None),
            Publication.scheduled_at <= now,
            or_(
                Publication.next_attempt_at.is_(None),
                Publication.next_attempt_at <= now,
            ),
            # Belt and braces: an already published row is never a candidate.
            Publication.threads_post_id.is_(None),
            Publication.published_at.is_(None),
        )
        .order_by(Publication.scheduled_at, Publication.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )

    publication_id = session.execute(candidate_stmt).scalar_one_or_none()

    if publication_id is None:
        return None

    claim_stmt = (
        update(Publication)
        .where(
            Publication.id == publication_id,
            Publication.status == PublicationStatus.SCHEDULED,
        )
        .values(
            status=PublicationStatus.CLAIMED,
            claimed_by=worker_id,
            claimed_at=now,
            lease_expires_at=now + timedelta(seconds=lease_seconds),
            updated_at=now,
        )
        .returning(Publication.id)
    )

    claimed_id = session.execute(claim_stmt).scalar_one_or_none()

    if claimed_id is None:
        # Another worker won the race between select and update.
        return None

    record_audit(
        session,
        actor=f"worker:{worker_id}",
        action="claimed",
        subject=f"publication:{claimed_id}",
        details={"lease_seconds": lease_seconds},
    )

    session.flush()

    # The claim was a Core UPDATE, so an instance already in the identity map
    # still carries the pre-claim status. Re-select with populate_existing so
    # the caller (and any later ORM write) works from the real row.
    return session.execute(
        select(Publication)
        .where(Publication.id == claimed_id)
        .execution_options(populate_existing=True)
    ).scalar_one()


def release_claim(
    session: Session,
    publication: Publication,
    *,
    actor: str,
    reason: str,
    now: datetime | None = None,
) -> None:
    """Give a claimed row back without touching attempt bookkeeping."""

    now = now or utcnow()

    publication.status = PublicationStatus.SCHEDULED
    publication.claimed_by = None
    publication.claimed_at = None
    publication.lease_expires_at = None
    publication.updated_at = now

    record_audit(
        session,
        actor=actor,
        action="claim_released",
        subject=f"publication:{publication.id}",
        details={"reason": reason},
    )


def start_attempt(
    session: Session,
    publication: Publication,
    *,
    worker_id: str,
    phase: AttemptPhase,
    now: datetime | None = None,
) -> PublicationAttempt:
    """Open an attempt row before any external call is made."""

    now = now or utcnow()

    publication.attempt_count += 1
    publication.updated_at = now

    attempt = PublicationAttempt(
        publication_id=publication.id,
        attempt_number=publication.attempt_count,
        worker_id=worker_id,
        phase=phase,
        started_at=now,
    )

    session.add(attempt)
    session.flush()

    return attempt


def mark_publishing(
    session: Session,
    publication: Publication,
    *,
    now: datetime | None = None,
) -> None:
    """The point of no return: from here on, retry requires a human."""

    now = now or utcnow()

    publication.status = PublicationStatus.PUBLISHING
    publication.updated_at = now


def mark_published(
    session: Session,
    publication: Publication,
    *,
    attempt: PublicationAttempt,
    threads_post_id: str,
    actor: str,
    now: datetime | None = None,
) -> None:
    now = now or utcnow()

    publication.status = PublicationStatus.PUBLISHED
    publication.threads_post_id = threads_post_id
    publication.published_at = now
    publication.claimed_by = None
    publication.claimed_at = None
    publication.lease_expires_at = None
    publication.next_attempt_at = None
    publication.last_error = None
    publication.updated_at = now

    attempt.outcome = AttemptOutcome.SUCCESS
    attempt.threads_post_id = threads_post_id
    attempt.finished_at = now

    record_audit(
        session,
        actor=actor,
        action="published",
        subject=f"publication:{publication.id}",
        details={"threads_post_id": threads_post_id},
    )


def mark_needs_review(
    session: Session,
    publication: Publication,
    *,
    attempt: PublicationAttempt | None,
    reason: str,
    actor: str,
    outcome: AttemptOutcome = AttemptOutcome.UNKNOWN,
    error_type: str | None = None,
    now: datetime | None = None,
) -> None:
    """Terminal until a human decides. Never scheduled again automatically."""

    now = now or utcnow()

    publication.status = PublicationStatus.NEEDS_REVIEW
    publication.claimed_by = None
    publication.claimed_at = None
    publication.lease_expires_at = None
    publication.next_attempt_at = None
    publication.last_error = reason[:4000]
    publication.updated_at = now

    if attempt is not None:
        attempt.outcome = outcome
        attempt.finished_at = now
        attempt.error_type = error_type
        attempt.error_message = reason[:4000]

    record_audit(
        session,
        actor=actor,
        action="needs_review",
        subject=f"publication:{publication.id}",
        details={"outcome": outcome.value, "error_type": error_type},
    )

    logger.error(
        "Publication requires manual review",
        extra={
            "context": {
                "publication_id": publication.id,
                "outcome": outcome.value,
                "error_type": error_type,
            }
        },
    )


def mark_retryable_failure(
    session: Session,
    publication: Publication,
    *,
    attempt: PublicationAttempt,
    reason: str,
    actor: str,
    max_attempts: int,
    backoff_base_seconds: int = 120,
    backoff_cap_seconds: int = 3600,
    error_type: str | None = None,
    now: datetime | None = None,
) -> None:
    """Schedule another try. Only safe when nothing reached Threads."""

    now = now or utcnow()

    attempt.outcome = AttemptOutcome.ERROR
    attempt.finished_at = now
    attempt.error_type = error_type
    attempt.error_message = reason[:4000]

    publication.last_error = reason[:4000]
    publication.claimed_by = None
    publication.claimed_at = None
    publication.lease_expires_at = None
    publication.updated_at = now

    if publication.attempt_count >= max_attempts:
        publication.status = PublicationStatus.FAILED
        publication.next_attempt_at = None

        record_audit(
            session,
            actor=actor,
            action="failed_max_attempts",
            subject=f"publication:{publication.id}",
            details={
                "attempt_count": publication.attempt_count,
                "max_attempts": max_attempts,
                "error_type": error_type,
            },
        )

        return

    delay = min(
        backoff_base_seconds * (2 ** (publication.attempt_count - 1)),
        backoff_cap_seconds,
    )

    publication.status = PublicationStatus.SCHEDULED
    publication.next_attempt_at = now + timedelta(seconds=delay)

    record_audit(
        session,
        actor=actor,
        action="retry_scheduled",
        subject=f"publication:{publication.id}",
        details={
            "delay_seconds": delay,
            "attempt_count": publication.attempt_count,
            "error_type": error_type,
        },
    )


def mark_permanent_failure(
    session: Session,
    publication: Publication,
    *,
    attempt: PublicationAttempt,
    reason: str,
    actor: str,
    error_type: str | None = None,
    now: datetime | None = None,
) -> None:
    """A request Threads rejected outright; retrying it changes nothing."""

    now = now or utcnow()

    attempt.outcome = AttemptOutcome.ERROR
    attempt.finished_at = now
    attempt.error_type = error_type
    attempt.error_message = reason[:4000]

    publication.status = PublicationStatus.FAILED
    publication.claimed_by = None
    publication.claimed_at = None
    publication.lease_expires_at = None
    publication.next_attempt_at = None
    publication.last_error = reason[:4000]
    publication.updated_at = now

    record_audit(
        session,
        actor=actor,
        action="failed_permanent",
        subject=f"publication:{publication.id}",
        details={"error_type": error_type},
    )


def mark_dry_run(
    session: Session,
    publication: Publication,
    *,
    attempt: PublicationAttempt,
    actor: str,
    details: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> None:
    """Record that the row was due and would have been published."""

    now = now or utcnow()

    attempt.outcome = AttemptOutcome.DRY_RUN
    attempt.finished_at = now
    attempt.details = details or {}

    publication.status = PublicationStatus.SCHEDULED
    publication.claimed_by = None
    publication.claimed_at = None
    publication.lease_expires_at = None
    # Do not spin on the same row every poll.
    publication.next_attempt_at = now + timedelta(hours=1)
    publication.updated_at = now

    record_audit(
        session,
        actor=actor,
        action="dry_run",
        subject=f"publication:{publication.id}",
        details=details or {},
    )


def reconcile_as_published(
    session: Session,
    publication: Publication,
    *,
    threads_post_id: str,
    actor: str,
    note: str | None = None,
    now: datetime | None = None,
) -> None:
    """Operator confirmed the post exists in Threads."""

    now = now or utcnow()

    publication.status = PublicationStatus.PUBLISHED
    publication.threads_post_id = threads_post_id
    publication.published_at = publication.published_at or now
    publication.last_error = None
    publication.next_attempt_at = None
    publication.updated_at = now

    record_audit(
        session,
        actor=actor,
        action="reconciled_published",
        subject=f"publication:{publication.id}",
        details={"threads_post_id": threads_post_id, "note": note},
    )


def reschedule(
    session: Session,
    publication: Publication,
    *,
    scheduled_at: datetime,
    actor: str,
    note: str | None = None,
    reset_attempts: bool = False,
    now: datetime | None = None,
) -> None:
    """Put a reviewed row back on the schedule.

    Refuses a row that already carries publish metadata: that is the state
    where a retry would duplicate a live post.
    """

    now = now or utcnow()

    if publication.threads_post_id or publication.published_at:
        raise ValueError(
            f"Publication {publication.id} already has publish metadata "
            "(threads_post_id/published_at); rescheduling it would risk a "
            "duplicate post."
        )

    if scheduled_at.tzinfo is None:
        raise ValueError("scheduled_at must be timezone-aware.")

    publication.status = PublicationStatus.SCHEDULED
    publication.scheduled_at = scheduled_at.astimezone(timezone.utc)
    publication.next_attempt_at = None
    publication.claimed_by = None
    publication.claimed_at = None
    publication.lease_expires_at = None
    publication.last_error = None
    publication.updated_at = now

    if reset_attempts:
        publication.attempt_count = 0

    record_audit(
        session,
        actor=actor,
        action="rescheduled",
        subject=f"publication:{publication.id}",
        details={
            "scheduled_at": publication.scheduled_at.isoformat(),
            "reset_attempts": reset_attempts,
            "note": note,
        },
    )


def cancel(
    session: Session,
    publication: Publication,
    *,
    actor: str,
    note: str | None = None,
    now: datetime | None = None,
) -> None:
    now = now or utcnow()

    publication.status = PublicationStatus.CANCELLED
    publication.claimed_by = None
    publication.claimed_at = None
    publication.lease_expires_at = None
    publication.next_attempt_at = None
    publication.updated_at = now

    record_audit(
        session,
        actor=actor,
        action="cancelled",
        subject=f"publication:{publication.id}",
        details={"note": note},
    )
