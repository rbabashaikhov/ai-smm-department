"""Claiming, leases and retries of content_generation_jobs.

The same guarantees ai_smm.queue gives publications (tests/test_queue_
claiming.py), minus the ambiguous-outcome path: a generation job never
reaches a state where an external, irreversible effect might be in
flight, so there is no quarantine-equivalent here to test.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from ai_smm.db.models import ContentGenerationJob, GenerationJobStatus
from ai_smm.generation_queue import (
    claim_due_generation_job,
    mark_generation_failure,
    mark_generation_succeeded,
    release_expired_generation_leases,
)


def _make_job(
    session: Session,
    *,
    project_id: str,
    requested_by,
    status: GenerationJobStatus = GenerationJobStatus.QUEUED,
    next_attempt_at: datetime | None = None,
    attempt_count: int = 0,
) -> ContentGenerationJob:
    job = ContentGenerationJob(
        id=uuid.uuid4(),
        project_id=project_id,
        requested_by_user_id=requested_by.id,
        status=status,
        next_attempt_at=next_attempt_at,
        attempt_count=attempt_count,
    )
    if status is GenerationJobStatus.CLAIMED:
        job.claimed_by = "gw-0"
        job.claimed_at = datetime.now(timezone.utc)
        job.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
    if status is GenerationJobStatus.SUCCEEDED:
        job.finished_at = datetime.now(timezone.utc)
    session.add(job)
    session.commit()

    return job


def test_claim_picks_a_due_job(session: Session, project: str, make_user) -> None:
    requester = make_user()
    job = _make_job(session, project_id=project, requested_by=requester)

    claimed = claim_due_generation_job(session, worker_id="gw-1", lease_seconds=600)

    assert claimed is not None
    assert claimed.id == job.id
    assert claimed.status is GenerationJobStatus.CLAIMED
    assert claimed.claimed_by == "gw-1"
    assert claimed.lease_expires_at is not None
    assert claimed.attempt_count == 1


def test_claim_ignores_a_job_scheduled_for_the_future(
    session: Session, project: str, make_user
) -> None:
    requester = make_user()
    _make_job(
        session,
        project_id=project,
        requested_by=requester,
        next_attempt_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )

    assert (
        claim_due_generation_job(session, worker_id="gw-1", lease_seconds=600)
        is None
    )


def test_claim_ignores_non_queued_statuses(
    session: Session, project: str, make_user
) -> None:
    requester = make_user()

    for status in (
        GenerationJobStatus.CLAIMED,
        GenerationJobStatus.SUCCEEDED,
        GenerationJobStatus.FAILED,
        GenerationJobStatus.CANCELLED,
    ):
        _make_job(session, project_id=project, requested_by=requester, status=status)

    assert (
        claim_due_generation_job(session, worker_id="gw-1", lease_seconds=600)
        is None
    )


def test_claim_never_double_claims(session: Session, project: str, make_user) -> None:
    requester = make_user()
    _make_job(session, project_id=project, requested_by=requester)

    first = claim_due_generation_job(session, worker_id="gw-1", lease_seconds=600)
    second = claim_due_generation_job(session, worker_id="gw-2", lease_seconds=600)

    assert first is not None
    assert second is None


def test_claim_picks_the_oldest_job_first(
    session: Session, project: str, make_user
) -> None:
    requester = make_user()
    now = datetime.now(timezone.utc)

    newer = _make_job(session, project_id=project, requested_by=requester)
    newer.created_at = now
    older = _make_job(session, project_id=project, requested_by=requester)
    older.created_at = now - timedelta(minutes=5)
    session.commit()

    claimed = claim_due_generation_job(session, worker_id="gw-1", lease_seconds=600)

    assert claimed.id == older.id


def test_an_audit_row_records_the_claim(
    session: Session, project: str, make_user
) -> None:
    requester = make_user()
    job = _make_job(session, project_id=project, requested_by=requester)

    claim_due_generation_job(session, worker_id="gw-1", lease_seconds=600)
    session.commit()

    from ai_smm.db.models import AuditLog

    row = session.query(AuditLog).filter(
        AuditLog.subject == f"generation_job:{job.id}",
        AuditLog.action == "generation.claimed",
    ).one()

    assert row.actor == "worker:gw-1"


# -- leases -----------------------------------------------------------------


def test_an_expired_lease_is_released_to_queued(
    session: Session, project: str, make_user
) -> None:
    requester = make_user()
    job = _make_job(session, project_id=project, requested_by=requester)
    job.status = GenerationJobStatus.CLAIMED
    job.claimed_by = "gw-1"
    job.claimed_at = datetime.now(timezone.utc) - timedelta(hours=1)
    job.lease_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    session.commit()

    released = release_expired_generation_leases(session, actor="worker:gw-2")
    session.commit()

    assert str(job.id) in released
    session.expire_all()
    stored = session.get(ContentGenerationJob, job.id)
    assert stored.status is GenerationJobStatus.QUEUED
    assert stored.claimed_by is None
    assert stored.lease_expires_at is None


def test_a_live_lease_is_not_released(
    session: Session, project: str, make_user
) -> None:
    requester = make_user()
    job = _make_job(session, project_id=project, requested_by=requester)
    job.status = GenerationJobStatus.CLAIMED
    job.claimed_by = "gw-1"
    job.claimed_at = datetime.now(timezone.utc)
    job.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=30)
    session.commit()

    released = release_expired_generation_leases(session, actor="worker:gw-2")

    assert released == []


def test_a_released_job_can_be_reclaimed_and_retried(
    session: Session, project: str, make_user
) -> None:
    """The crash path: an abandoned claim is retried from scratch, with
    no reconciliation needed (contrast publications' needs_review)."""

    requester = make_user()
    job = _make_job(session, project_id=project, requested_by=requester)
    job.status = GenerationJobStatus.CLAIMED
    job.claimed_by = "gw-1"
    job.claimed_at = datetime.now(timezone.utc) - timedelta(hours=1)
    job.lease_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    session.commit()

    release_expired_generation_leases(session, actor="worker:gw-2")
    session.commit()

    reclaimed = claim_due_generation_job(session, worker_id="gw-2", lease_seconds=600)

    assert reclaimed is not None and reclaimed.id == job.id
    assert reclaimed.claimed_by == "gw-2"


def test_an_expired_final_claim_is_terminal_and_never_claimed_again(
    session: Session, project: str, make_user
) -> None:
    requester = make_user()
    job = _make_job(
        session, project_id=project, requested_by=requester, attempt_count=2
    )
    claimed = claim_due_generation_job(
        session, worker_id="gw-final", lease_seconds=600, max_attempts=3
    )
    assert claimed is not None and claimed.attempt_count == 3
    claimed.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    session.commit()

    assert claim_due_generation_job(
        session, worker_id="gw-next", lease_seconds=600, max_attempts=3
    ) is None
    session.expire_all()
    stored = session.get(ContentGenerationJob, job.id)
    assert stored.status is GenerationJobStatus.FAILED
    assert stored.attempt_count == 3
    assert stored.finished_at is not None


# -- outcomes -----------------------------------------------------------------


def test_mark_succeeded_clears_claim_and_stores_the_result(
    session: Session, project: str, make_user
) -> None:
    requester = make_user()
    job = _make_job(session, project_id=project, requested_by=requester)
    claimed = claim_due_generation_job(session, worker_id="gw-1", lease_seconds=600)
    session.commit()

    mark_generation_succeeded(
        session,
        claimed,
        content_item_ids=["a", "b", "c"],
        series_title="Series",
        revision_count=1,
        tokens={"input": 100, "output": 20, "cached": 0},
        cost_usd=0.001,
        seconds=12.3,
        actor="worker:gw-1",
    )
    session.commit()

    session.expire_all()
    stored = session.get(ContentGenerationJob, job.id)
    assert stored.status is GenerationJobStatus.SUCCEEDED
    assert stored.claimed_by is None
    assert stored.result["content_item_ids"] == ["a", "b", "c"]
    assert stored.result["cost_usd"] == 0.001
    assert stored.finished_at is not None


def test_a_retryable_failure_is_requeued_with_backoff(
    session: Session, project: str, make_user
) -> None:
    requester = make_user()
    job = _make_job(session, project_id=project, requested_by=requester)
    claimed = claim_due_generation_job(session, worker_id="gw-1", lease_seconds=600)
    session.commit()
    claimed.attempt_count = 1

    mark_generation_failure(
        session,
        claimed,
        reason="openai timeout",
        actor="worker:gw-1",
        max_attempts=3,
        error_type="TimeoutError",
    )
    session.commit()

    session.expire_all()
    stored = session.get(ContentGenerationJob, job.id)
    assert stored.status is GenerationJobStatus.QUEUED
    assert stored.next_attempt_at is not None
    assert stored.last_error == "openai timeout"
    assert stored.claimed_by is None


def test_attempts_exhausted_fails_permanently(
    session: Session, project: str, make_user
) -> None:
    requester = make_user()
    job = _make_job(session, project_id=project, requested_by=requester)
    claimed = claim_due_generation_job(session, worker_id="gw-1", lease_seconds=600)
    session.commit()
    claimed.attempt_count = 2

    mark_generation_failure(
        session,
        claimed,
        reason="openai timeout",
        actor="worker:gw-1",
        max_attempts=2,
        error_type="TimeoutError",
    )
    session.commit()

    session.expire_all()
    stored = session.get(ContentGenerationJob, job.id)
    assert stored.status is GenerationJobStatus.FAILED
    assert stored.next_attempt_at is None
    assert stored.finished_at is not None


def test_a_non_retryable_failure_skips_straight_to_failed(
    session: Session, project: str, make_user
) -> None:
    """An unknown project or missing knowledge: retrying changes nothing."""

    requester = make_user()
    job = _make_job(session, project_id=project, requested_by=requester)
    claimed = claim_due_generation_job(session, worker_id="gw-1", lease_seconds=600)
    session.commit()
    claimed.attempt_count = 1  # well under max_attempts

    mark_generation_failure(
        session,
        claimed,
        reason="knowledge unavailable: no such project",
        actor="worker:gw-1",
        max_attempts=5,
        retryable=False,
    )
    session.commit()

    session.expire_all()
    stored = session.get(ContentGenerationJob, job.id)
    assert stored.status is GenerationJobStatus.FAILED
    assert stored.next_attempt_at is None
