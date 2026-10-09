"""Concurrent claiming, leases and the crash path."""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session, sessionmaker

from ai_smm.db.models import (
    AttemptOutcome,
    AttemptPhase,
    AuditLog,
    Publication,
    PublicationStatus,
)
from ai_smm.queue import (
    claim_due_publication,
    quarantine_abandoned_publishing,
    release_expired_leases,
    reschedule,
    start_attempt,
)


def test_claim_picks_a_due_publication(
    session: Session, make_publication
) -> None:
    publication = make_publication()

    claimed = claim_due_publication(
        session, worker_id="w1", lease_seconds=900
    )

    assert claimed is not None
    assert claimed.id == publication.id
    assert claimed.status is PublicationStatus.CLAIMED
    assert claimed.claimed_by == "w1"
    assert claimed.lease_expires_at is not None


def test_claim_ignores_future_schedule(
    session: Session, make_publication
) -> None:
    make_publication(
        scheduled_at=datetime.now(timezone.utc) + timedelta(hours=1)
    )

    assert claim_due_publication(
        session, worker_id="w1", lease_seconds=900
    ) is None


def test_claim_ignores_non_scheduled_statuses(
    session: Session, make_publication
) -> None:
    for status in (
        PublicationStatus.DRAFT,
        PublicationStatus.APPROVED,
        PublicationStatus.NEEDS_REVIEW,
        PublicationStatus.PUBLISHING,
        PublicationStatus.FAILED,
        PublicationStatus.CANCELLED,
    ):
        make_publication(status=status)

    assert claim_due_publication(
        session, worker_id="w1", lease_seconds=900
    ) is None


def test_claim_ignores_rows_with_publish_metadata(
    session: Session, make_publication
) -> None:
    """A row carrying a post id must never be reclaimed, whatever its status."""

    make_publication(
        status=PublicationStatus.SCHEDULED,
        threads_post_id="17900000000000001",
    )

    assert claim_due_publication(
        session, worker_id="w1", lease_seconds=900
    ) is None


def test_claim_respects_next_attempt_at_backoff(
    session: Session, make_publication
) -> None:
    make_publication(
        next_attempt_at=datetime.now(timezone.utc) + timedelta(minutes=30)
    )

    assert claim_due_publication(
        session, worker_id="w1", lease_seconds=900
    ) is None


def test_second_claim_in_same_session_sees_nothing(
    session: Session, make_publication
) -> None:
    make_publication()

    assert claim_due_publication(
        session, worker_id="w1", lease_seconds=900
    ) is not None
    assert claim_due_publication(
        session, worker_id="w1", lease_seconds=900
    ) is None


def test_concurrent_workers_claim_each_row_once(
    session: Session,
    session_factory: sessionmaker[Session],
    make_publication,
) -> None:
    """The core double-publish guard: SKIP LOCKED across real connections."""

    row_count = 6
    worker_count = 8

    for _ in range(row_count):
        make_publication()

    session.commit()

    claimed_ids: list[int] = []
    errors: list[BaseException] = []
    lock = threading.Lock()
    start = threading.Barrier(worker_count)

    def worker(index: int) -> None:
        try:
            start.wait(timeout=10)

            with session_factory() as worker_session:
                while True:
                    publication = claim_due_publication(
                        worker_session,
                        worker_id=f"w{index}",
                        lease_seconds=900,
                    )

                    if publication is None:
                        worker_session.commit()

                        return

                    publication_id = publication.id
                    worker_session.commit()

                    with lock:
                        claimed_ids.append(publication_id)
        except BaseException as exc:  # noqa: BLE001 - reported below
            with lock:
                errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(i,))
        for i in range(worker_count)
    ]

    for thread in threads:
        thread.start()

    for thread in threads:
        thread.join(timeout=30)

    assert not errors, f"worker errors: {errors}"
    assert sorted(claimed_ids) == sorted(set(claimed_ids)), (
        "a publication was claimed more than once"
    )
    assert len(claimed_ids) == row_count


def test_expired_lease_returns_row_to_scheduled(
    session: Session, make_publication
) -> None:
    publication = make_publication(
        status=PublicationStatus.CLAIMED,
        claimed_by="dead-worker",
        claimed_at=datetime.now(timezone.utc) - timedelta(hours=2),
        lease_expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )

    released = release_expired_leases(session, actor="test")
    session.commit()
    session.refresh(publication)

    assert released == [publication.id]
    assert publication.status is PublicationStatus.SCHEDULED
    assert publication.claimed_by is None
    assert publication.lease_expires_at is None


def test_valid_lease_is_not_released(
    session: Session, make_publication
) -> None:
    publication = make_publication(
        status=PublicationStatus.CLAIMED,
        claimed_by="busy-worker",
        claimed_at=datetime.now(timezone.utc),
        lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )

    assert release_expired_leases(session, actor="test") == []
    session.refresh(publication)
    assert publication.status is PublicationStatus.CLAIMED


def test_publishing_row_is_never_released_to_scheduled(
    session: Session, make_publication
) -> None:
    """The crash path: an in-flight publish is ambiguous, not retryable."""

    publication = make_publication(
        status=PublicationStatus.PUBLISHING,
        claimed_by="dead-worker",
        lease_expires_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )

    assert release_expired_leases(session, actor="test") == []

    quarantined = quarantine_abandoned_publishing(session, actor="test")
    session.commit()
    session.refresh(publication)

    assert quarantined == [publication.id]
    assert publication.status is PublicationStatus.NEEDS_REVIEW
    assert "unknown" in (publication.last_error or "").lower()

    # And it stays invisible to the scheduler.
    assert claim_due_publication(
        session, worker_id="w1", lease_seconds=900
    ) is None


def test_crash_closes_the_open_attempt_as_unknown(
    session: Session, make_publication
) -> None:
    publication = make_publication(status=PublicationStatus.PUBLISHING)

    attempt = start_attempt(
        session,
        publication,
        worker_id="dead-worker",
        phase=AttemptPhase.PUBLISH,
    )
    attempt.threads_creation_id = "cid-123"
    session.commit()

    quarantine_abandoned_publishing(session, actor="test")
    session.commit()
    session.refresh(attempt)

    assert attempt.outcome is AttemptOutcome.UNKNOWN
    assert attempt.finished_at is not None
    # The creation id survives, which is what makes reconciliation possible.
    assert attempt.threads_creation_id == "cid-123"


def test_reschedule_refuses_a_published_row(
    session: Session, make_publication
) -> None:
    publication = make_publication(
        status=PublicationStatus.NEEDS_REVIEW,
        threads_post_id="17900000000000002",
        published_at=datetime.now(timezone.utc),
    )

    with pytest.raises(ValueError, match="already has publish metadata"):
        reschedule(
            session,
            publication,
            scheduled_at=datetime.now(timezone.utc),
            actor="test",
        )


def test_reschedule_requires_timezone_aware_input(
    session: Session, make_publication
) -> None:
    publication = make_publication(status=PublicationStatus.FAILED)

    with pytest.raises(ValueError, match="timezone-aware"):
        reschedule(
            session,
            publication,
            scheduled_at=datetime(2026, 11, 1, 10, 0),  # noqa: DTZ001 - the point of the test
            actor="test",
        )


def test_claim_writes_audit_entry(
    session: Session, make_publication
) -> None:
    make_publication()

    claim_due_publication(session, worker_id="w1", lease_seconds=900)
    session.commit()

    actions = [row.action for row in session.query(AuditLog).all()]

    assert "claimed" in actions


def test_schedule_stored_in_utc(session: Session, make_publication) -> None:
    """Scheduling input in Europe/Moscow must land as the same instant."""

    from zoneinfo import ZoneInfo

    moscow_time = datetime(
        2026, 11, 1, 12, 0, tzinfo=ZoneInfo("Europe/Moscow")
    )

    publication = make_publication(status=PublicationStatus.FAILED)

    reschedule(
        session, publication, scheduled_at=moscow_time, actor="test"
    )
    session.commit()

    stored = session.get(Publication, publication.id)

    assert stored is not None
    assert stored.scheduled_at == moscow_time
    # 12:00 Moscow is 09:00 UTC.
    assert stored.scheduled_at.astimezone(timezone.utc).hour == 9
