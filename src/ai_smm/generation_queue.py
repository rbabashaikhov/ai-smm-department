"""Generation-job queue operations against PostgreSQL.

The same pattern as ai_smm.queue, applied to content_generation_jobs
instead of publications:

* claiming is a single atomic UPDATE with FOR UPDATE SKIP LOCKED, so any
  number of generation workers can run without ever claiming the same job;
* a lease bounds how long a claim is valid; an expired claim is released
  back to queued -- unlike a publication, a generation job has no
  irreversible external side effect, so there is no needs_review-equivalent
  quarantine state. Nothing is written to content_items/content_revisions
  until the very end of processing, in the same transaction that marks the
  job succeeded, so a lost claim is always safe to retry from scratch;
* the generation worker invokes narrow SECURITY DEFINER contracts and has
  no table privileges. They are never called by the HTTP API, which only
  ever creates or cancels a job through ai_smm.application.generation, as
  the guarded ai_smm_cc_rw role.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from ai_smm.db.models import ContentGenerationJob, GenerationJobStatus
from ai_smm.logging_setup import get_logger
from ai_smm.queue import record_audit


logger = get_logger(__name__)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def claim_due_generation_job(
    session: Session,
    *,
    worker_id: str,
    lease_seconds: int,
    now: datetime | None = None,
) -> ContentGenerationJob | None:
    """Atomically reserve one due job, or return None.

    FOR UPDATE SKIP LOCKED means a concurrent generation worker running
    the same statement simply sees no row instead of blocking or
    double-claiming -- the same guarantee ai_smm.queue.claim_due_publication
    gives the publish worker.
    """

    # `now` remains accepted for source compatibility; the security contract
    # deliberately uses the database clock so a caller cannot forge leases.
    del now
    row = session.execute(
        text(
            "SELECT * FROM public.generation_claim("
            ":worker_id, :lease_seconds)"
        ),
        {
            "worker_id": worker_id,
            "lease_seconds": lease_seconds,
        },
    ).mappings().one_or_none()
    if row is None:
        return None

    # Owner-side tests and administrative callers retain normal ORM identity
    # semantics. The dedicated worker role has no SELECT and follows the
    # detached branch below.
    if session.execute(
        text(
            "SELECT has_table_privilege("
            "current_user, 'public.content_generation_jobs', 'SELECT')"
        )
    ).scalar_one():
        return session.execute(
            select(ContentGenerationJob)
            .where(ContentGenerationJob.id == row["id"])
            .execution_options(populate_existing=True)
        ).scalar_one()

    # A deliberately detached value object: the generation role cannot and
    # need not SELECT the table. The database remains the source of truth.
    return ContentGenerationJob(
        id=row["id"],
        project_id=row["project_id"],
        requested_by_user_id=row["requested_by_user_id"],
        status=GenerationJobStatus(row["status"]),
        params=row["params"],
        attempt_count=row["attempt_count"],
        max_attempts=row["max_attempts"],
        claimed_by=row["claimed_by"],
        claimed_at=row["claimed_at"],
        lease_expires_at=row["lease_expires_at"],
    )


def release_expired_generation_leases(
    session: Session,
    *,
    actor: str,
    now: datetime | None = None,
) -> list[str]:
    """Return claimed-but-abandoned jobs to queued.

    Safe unconditionally: a generation job never reaches a state where an
    external, irreversible effect might be in flight (contrast
    ai_smm.queue.quarantine_abandoned_publishing, which exists only
    because publishing does).
    """

    now = now or utcnow()

    terminal_stmt = (
        update(ContentGenerationJob)
        .where(
            ContentGenerationJob.status == GenerationJobStatus.CLAIMED,
            ContentGenerationJob.lease_expires_at.is_not(None),
            ContentGenerationJob.lease_expires_at <= now,
            ContentGenerationJob.attempt_count
            >= ContentGenerationJob.max_attempts,
        )
        .values(
            status=GenerationJobStatus.FAILED,
            claimed_by=None,
            claimed_at=None,
            lease_expires_at=None,
            next_attempt_at=None,
            last_error="generation lease expired after final attempt",
            updated_at=now,
            finished_at=now,
        )
        .returning(ContentGenerationJob.id)
        .execution_options(synchronize_session="fetch")
    )
    terminal = [str(row) for row in session.execute(terminal_stmt).scalars()]

    stmt = (
        update(ContentGenerationJob)
        .where(
            ContentGenerationJob.status == GenerationJobStatus.CLAIMED,
            ContentGenerationJob.lease_expires_at.is_not(None),
            ContentGenerationJob.lease_expires_at <= now,
            ContentGenerationJob.attempt_count
            < ContentGenerationJob.max_attempts,
        )
        .values(
            status=GenerationJobStatus.QUEUED,
            claimed_by=None,
            claimed_at=None,
            lease_expires_at=None,
            updated_at=now,
        )
        .returning(ContentGenerationJob.id)
        .execution_options(synchronize_session="fetch")
    )

    released = [str(row) for row in session.execute(stmt).scalars()]

    for job_id in released:
        record_audit(
            session,
            actor=actor,
            action="generation.lease_released",
            subject=f"generation_job:{job_id}",
            details={"reason": "lease_expired"},
        )

    for job_id in terminal:
        record_audit(
            session,
            actor=actor,
            action="generation.failed",
            subject=f"generation_job:{job_id}",
            details={"reason": "lease_expired", "retry_limit": "job"},
        )

    return released


def mark_generation_succeeded(
    session: Session,
    job: ContentGenerationJob,
    *,
    content_item_ids: list[str],
    series_title: str,
    revision_count: int,
    tokens: dict[str, int],
    cost_usd: float,
    seconds: float,
    actor: str,
    now: datetime | None = None,
) -> None:
    """Record success. Called in the same transaction that creates the
    content items -- see generation_worker.GenerationWorker._run_job --
    so a crash before commit leaves neither the items nor this row."""

    now = now or utcnow()

    job.status = GenerationJobStatus.SUCCEEDED
    job.claimed_by = None
    job.claimed_at = None
    job.lease_expires_at = None
    job.next_attempt_at = None
    job.last_error = None
    job.result = {
        "content_item_ids": content_item_ids,
        "series_title": series_title,
        "revision_count": revision_count,
        "tokens": tokens,
        "cost_usd": cost_usd,
        "seconds": seconds,
    }
    job.updated_at = now
    job.finished_at = now

    record_audit(
        session,
        actor=actor,
        action="generation.succeeded",
        subject=f"generation_job:{job.id}",
        details={
            "content_item_ids": content_item_ids,
            "revision_count": revision_count,
            "cost_usd": cost_usd,
        },
    )


def mark_generation_failure(
    session: Session,
    job: ContentGenerationJob,
    *,
    reason: str,
    actor: str,
    backoff_base_seconds: int = 30,
    backoff_cap_seconds: int = 600,
    error_type: str | None = None,
    retryable: bool = True,
    now: datetime | None = None,
) -> None:
    """Schedule a retry, or fail permanently.

    retryable=False short-circuits straight to permanent failure for
    errors retrying cannot fix (an unknown project, a malformed request) --
    the same ValueError-vs-everything-else split ai_smm.publishing.service
    makes for publications, simplified: there is no ambiguous/ unknown
    class here, because nothing external happens until the final commit.
    """

    del actor, now
    status = session.execute(
        text(
            "SELECT public.generation_fail("
            ":job_id, :worker_id, :reason, :retryable, "
            ":error_type, :base, :cap)"
        ),
        {
            "job_id": job.id,
            "worker_id": job.claimed_by,
            "reason": reason,
            "retryable": retryable,
            "error_type": error_type,
            "base": backoff_base_seconds,
            "cap": backoff_cap_seconds,
        },
    ).scalar_one()
    job.status = GenerationJobStatus(status)


def complete_generation_job(
    session: Session,
    *,
    job_id: Any,
    worker_id: str,
    run_id: uuid.UUID,
    drafts_json: str,
    result_json: str,
) -> dict[str, Any]:
    """Atomically save agent drafts and mark their job succeeded."""

    value = session.execute(
        text(
            "SELECT public.generation_complete("
            ":job_id, :worker_id, :run_id, CAST(:drafts AS jsonb), "
            "CAST(:result AS jsonb))"
        ),
        {
            "job_id": job_id,
            "worker_id": worker_id,
            "run_id": run_id,
            "drafts": drafts_json,
            "result": result_json,
        },
    ).scalar_one()
    return dict(value)


def get_generation_job(
    session: Session, *, job_id: Any
) -> ContentGenerationJob | None:
    return session.get(ContentGenerationJob, job_id)
