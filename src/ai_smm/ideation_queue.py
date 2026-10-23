"""Topic-ideation queue operations (SMM-035B3B1): the three
topic_ideation_* contracts, and nothing else.

The ideation worker's role, ai_smm_ideation, may EXECUTE exactly these
SECURITY DEFINER functions (ai_smm.db.ideation_contracts) and read no
table, so a claim comes back as a plain value object rather than an ORM
row. Lease, attempts, recovery of expired claims, backoff, the
all-or-nothing saving of ideas and the audit trail are the database's;
this module only passes arguments through.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session


#: Retry backoff handed to topic_ideation_fail; both within its 1..3600.
BACKOFF_BASE_SECONDS = 30
BACKOFF_CAP_SECONDS = 600


@dataclass(frozen=True)
class ClaimedIdeationJob:
    id: uuid.UUID
    project_id: str
    requested_by_user_id: uuid.UUID
    idea_count: int
    topic_hint: str | None
    params: dict[str, Any]
    attempt_count: int
    max_attempts: int
    claimed_by: str
    lease_expires_at: datetime


def claim_ideation_job(
    session: Session, *, worker_id: str, lease_seconds: int
) -> ClaimedIdeationJob | None:
    """Reserve the oldest due job (FOR UPDATE SKIP LOCKED), or None."""

    row = session.execute(
        text("SELECT * FROM public.topic_ideation_claim(:worker_id, :lease)"),
        {"worker_id": worker_id, "lease": lease_seconds},
    ).mappings().one_or_none()

    if row is None:
        return None

    return ClaimedIdeationJob(
        id=row["id"],
        project_id=row["project_id"],
        requested_by_user_id=row["requested_by_user_id"],
        idea_count=row["idea_count"],
        topic_hint=row["topic_hint"],
        params=dict(row["params"] or {}),
        attempt_count=row["attempt_count"],
        max_attempts=row["max_attempts"],
        claimed_by=row["claimed_by"],
        lease_expires_at=row["lease_expires_at"],
    )


def complete_ideation_job(
    session: Session,
    *,
    job_id: uuid.UUID,
    worker_id: str,
    ideas_json: str,
    result_json: str,
) -> dict[str, Any]:
    """Save every idea and mark the job succeeded, or nothing at all.

    A claim this worker no longer holds (expired, cancelled, completed)
    is a no-op that reports the job's status."""

    value = session.execute(
        text(
            "SELECT public.topic_ideation_complete("
            ":job_id, :worker_id, CAST(:ideas AS jsonb), CAST(:result AS jsonb))"
        ),
        {
            "job_id": job_id,
            "worker_id": worker_id,
            "ideas": ideas_json,
            "result": result_json,
        },
    ).scalar_one()

    return dict(value)


def fail_ideation_job(
    session: Session,
    *,
    job_id: uuid.UUID,
    worker_id: str,
    reason: str,
    retryable: bool,
    error_type: str | None = None,
) -> str:
    """Requeue with backoff, or fail for good; the job's new status."""

    return str(
        session.execute(
            text(
                "SELECT public.topic_ideation_fail("
                ":job_id, :worker_id, :reason, :retryable, :error_type, "
                ":base, :cap)"
            ),
            {
                "job_id": job_id,
                "worker_id": worker_id,
                "reason": reason,
                "retryable": retryable,
                "error_type": error_type,
                "base": BACKOFF_BASE_SECONDS,
                "cap": BACKOFF_CAP_SECONDS,
            },
        ).scalar_one()
    )
