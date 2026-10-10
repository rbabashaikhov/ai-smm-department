"""The commands must never overwrite a claim a worker has just taken.

The hazard is a stale snapshot. Authorisation loads the publication, and
between that read and the write a worker can claim the row -- the queue
is built so that this happens without anyone coordinating. Acting on the
snapshot would then set the status back to scheduled and clear
claimed_by, claimed_at and lease_expires_at while the worker was already
publishing it, which is how one post goes out twice.

Nothing here mocks PostgreSQL. The claim is the real
queue.claim_due_publication with its FOR UPDATE SKIP LOCKED, every
participant has its own connection, and the locking is the database's.
"""
from __future__ import annotations

import contextlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ai_smm.application.publications import (
    InvalidTransition,
    _lock_publication,
    cancel_publication,
    schedule_publication,
)
from ai_smm.db.models import AuditLog, MembershipRole, Publication, PublicationStatus
from ai_smm.queue import claim_due_publication


WORKER_ID = "concurrent-worker"
LEASE_SECONDS = 900


def _when(minutes: int = 30) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(minutes=minutes)
    ).isoformat()


def _claim_on_its_own_connection(
    session_factory: sessionmaker[Session], *, commit: bool = True
) -> Publication:
    """Claim the next due row exactly as the worker does, and commit it."""

    worker_session = session_factory()

    try:
        claimed = claim_due_publication(
            worker_session,
            worker_id=WORKER_ID,
            lease_seconds=LEASE_SECONDS,
        )

        assert claimed is not None, "nothing was due to claim"

        if commit:
            worker_session.commit()

        return claimed
    finally:
        if commit:
            worker_session.close()


def _lease_fields(session: Session, publication_id: int) -> tuple:
    """Read the lease straight from the database, bypassing any cache."""

    row = session.execute(
        select(
            Publication.status,
            Publication.claimed_by,
            Publication.claimed_at,
            Publication.lease_expires_at,
        ).where(Publication.id == publication_id)
    ).one()

    return tuple(row)


def _command_audit(session: Session, publication_id: int) -> list[str]:
    return [
        entry.action
        for entry in session.scalars(
            select(AuditLog).where(
                AuditLog.subject == f"publication:{publication_id}",
                AuditLog.action.in_(["rescheduled", "cancelled"]),
            )
        ).all()
    ]


# -- the reported hazard, at the application layer -----------------------


@pytest.mark.parametrize("command", ["schedule", "reschedule", "cancel"])
def test_a_stale_snapshot_cannot_overwrite_a_claim(
    session_factory: sessionmaker[Session],
    session: Session,
    project: str,
    make_publication,
    command: str,
) -> None:
    """The regression test for the bug itself.

    The command's session has already read the row -- that is what the
    authorisation dependency does, and the instance stays in the identity
    map. A worker then claims the row on another connection and commits.
    The command must see claimed, not the snapshot it is holding.
    """

    publication = make_publication(status=PublicationStatus.SCHEDULED)
    publication_id = publication.id

    api_session = session_factory()

    try:
        # What require_publication_role does: load the row, authorise it.
        snapshot = api_session.get(Publication, publication_id)

        assert snapshot is not None
        assert snapshot.status is PublicationStatus.SCHEDULED

        # The worker gets there first, on its own connection.
        _claim_on_its_own_connection(session_factory)

        # The stale instance still says scheduled...
        assert snapshot.status is PublicationStatus.SCHEDULED

        # ...but the command reads the row under its lock and refuses.
        with pytest.raises(InvalidTransition) as refusal:
            if command == "cancel":
                cancel_publication(
                    api_session,
                    publication_id=publication_id,
                    expected_project_id=project,
                    actor="user:test",
                )
            else:
                schedule_publication(
                    api_session,
                    publication_id=publication_id,
                    expected_project_id=project,
                    scheduled_at=datetime.now(timezone.utc)
                    + timedelta(minutes=30),
                    actor="user:test",
                    command=command,
                )

        assert refusal.value.status is PublicationStatus.CLAIMED

        api_session.rollback()
    finally:
        api_session.close()

    # The worker's claim is intact, field by field.
    status, claimed_by, claimed_at, lease_expires_at = _lease_fields(
        session, publication_id
    )

    assert status is PublicationStatus.CLAIMED
    assert claimed_by == WORKER_ID
    assert claimed_at is not None
    assert lease_expires_at is not None
    assert _command_audit(session, publication_id) == []


# -- the same hazard over HTTP -------------------------------------------


@pytest.mark.parametrize("command", ["schedule", "reschedule"])
def test_a_concurrent_reschedule_refuses_a_claimed_row(
    client,
    session: Session,
    session_factory: sessionmaker[Session],
    login,
    member,
    project: str,
    make_publication,
    command: str,
) -> None:
    publication = make_publication(status=PublicationStatus.SCHEDULED)
    publication_id = publication.id
    csrf = login(member(MembershipRole.ADMIN))

    claimed = _claim_on_its_own_connection(session_factory)

    assert claimed.id == publication_id

    response = client.post(
        f"/api/v1/publications/{publication_id}/{command}",
        json={"scheduled_at": _when()},
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 409

    error = response.json()["error"]

    assert error["code"] == "INVALID_STATE_TRANSITION"
    assert error["details"]["status"] == "claimed"
    assert error["details"]["command"] == command
    assert "lease" in error["message"] or "worker" in error["message"]

    status, claimed_by, claimed_at, lease_expires_at = _lease_fields(
        session, publication_id
    )

    assert status is PublicationStatus.CLAIMED
    assert claimed_by == WORKER_ID
    assert claimed_at is not None
    assert lease_expires_at is not None
    assert _command_audit(session, publication_id) == []


def test_a_concurrent_cancel_refuses_a_claimed_row(
    client,
    session: Session,
    session_factory: sessionmaker[Session],
    login,
    member,
    project: str,
    make_publication,
) -> None:
    publication = make_publication(status=PublicationStatus.SCHEDULED)
    publication_id = publication.id
    csrf = login(member(MembershipRole.ADMIN))

    _claim_on_its_own_connection(session_factory)

    response = client.post(
        f"/api/v1/publications/{publication_id}/cancel",
        json={"note": "too late"},
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 409
    assert response.json()["error"]["details"]["status"] == "claimed"

    status, claimed_by, claimed_at, lease_expires_at = _lease_fields(
        session, publication_id
    )

    assert status is PublicationStatus.CLAIMED
    assert claimed_by == WORKER_ID
    assert claimed_at is not None
    assert lease_expires_at is not None
    assert _command_audit(session, publication_id) == []


# -- the other direction: the command's lock blocks the claim ------------


def test_the_command_lock_makes_the_worker_skip_the_row(
    session_factory: sessionmaker[Session],
    session: Session,
    project: str,
    make_publication,
) -> None:
    """FOR UPDATE here means SKIP LOCKED there.

    While a command holds the row, the worker's candidate query does not
    block on it and does not claim it -- it sees no row at all, because
    this one publication is the only due candidate.
    """

    publication = make_publication(status=PublicationStatus.SCHEDULED)
    publication_id = publication.id

    api_session = session_factory()
    worker_session = session_factory()

    try:
        locked = _lock_publication(
            api_session,
            publication_id=publication_id,
            expected_project_id=project,
        )

        assert locked.id == publication_id

        # The worker runs its real claim query against the locked row.
        assert (
            claim_due_publication(
                worker_session,
                worker_id=WORKER_ID,
                lease_seconds=LEASE_SECONDS,
            )
            is None
        )

        worker_session.rollback()

        # Releasing the lock makes the row claimable again, which proves
        # the skip above was the lock and not some other filter.
        api_session.rollback()

        reclaimed = claim_due_publication(
            worker_session,
            worker_id=WORKER_ID,
            lease_seconds=LEASE_SECONDS,
        )

        assert reclaimed is not None
        assert reclaimed.id == publication_id

        worker_session.commit()
    finally:
        api_session.close()
        worker_session.close()


def test_the_lock_does_not_starve_a_second_due_publication(
    session_factory: sessionmaker[Session],
    session: Session,
    project: str,
    make_publication,
) -> None:
    """SKIP LOCKED, not blocked: the worker moves on to the next row."""

    held = make_publication(
        status=PublicationStatus.SCHEDULED,
        scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=10),
    )
    other = make_publication(
        status=PublicationStatus.SCHEDULED,
        scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )

    api_session = session_factory()
    worker_session = session_factory()

    try:
        _lock_publication(
            api_session,
            publication_id=held.id,
            expected_project_id=project,
        )

        claimed = claim_due_publication(
            worker_session,
            worker_id=WORKER_ID,
            lease_seconds=LEASE_SECONDS,
        )

        assert claimed is not None
        assert claimed.id == other.id

        worker_session.commit()
        api_session.rollback()
    finally:
        api_session.close()
        worker_session.close()


# -- a real interleaving, with both sides contending ---------------------


def test_a_command_that_waited_for_the_lock_sees_the_claim(
    session_factory: sessionmaker[Session],
    session: Session,
    project: str,
    make_publication,
) -> None:
    """The worst ordering: the command asks for the lock while a worker holds it.

    The command blocks on the row lock, the worker commits its claim and
    releases it, and the command then wakes up. Because it re-reads the
    row under the lock it has just been given, it sees claimed and
    refuses -- which is the whole point. Before the fix it would have
    woken up holding a snapshot that said scheduled.
    """

    publication = make_publication(status=PublicationStatus.SCHEDULED)
    publication_id = publication.id

    worker_session = session_factory()
    api_session = session_factory()

    def run_command() -> InvalidTransition | None:
        # The authorisation read, before the lock is even asked for.
        api_session.get(Publication, publication_id)

        try:
            schedule_publication(
                api_session,
                publication_id=publication_id,
                expected_project_id=project,
                scheduled_at=datetime.now(timezone.utc)
                + timedelta(minutes=30),
                actor="user:test",
                command="reschedule",
            )
        except InvalidTransition as exc:
            return exc
        finally:
            api_session.rollback()

        return None

    try:
        # The worker claims the row and holds the lock, uncommitted.
        claimed = claim_due_publication(
            worker_session,
            worker_id=WORKER_ID,
            lease_seconds=LEASE_SECONDS,
        )

        assert claimed is not None

        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(run_command)

            # Give the command time to reach the lock and block on it.
            # lock_timeout is 3s, so this has to be well under that.
            with contextlib.suppress(Exception):
                pending.result(timeout=0.5)

            assert not pending.done(), (
                "the command did not block on the row lock, so it was "
                "never actually contending for it"
            )

            # Releasing the claim hands the lock to the waiting command.
            worker_session.commit()

            refusal = pending.result(timeout=10)

        assert isinstance(refusal, InvalidTransition)
        assert refusal.status is PublicationStatus.CLAIMED
    finally:
        worker_session.close()
        api_session.close()

    status, claimed_by, claimed_at, lease_expires_at = _lease_fields(
        session, publication_id
    )

    assert status is PublicationStatus.CLAIMED
    assert claimed_by == WORKER_ID
    assert claimed_at is not None
    assert lease_expires_at is not None
    assert _command_audit(session, publication_id) == []


# -- the lock is re-checked against the authorised project ---------------


def test_the_locked_row_must_still_belong_to_the_authorised_project(
    session_factory: sessionmaker[Session],
    session: Session,
    project: str,
    make_publication,
    make_other_project,
) -> None:
    """Step four: the project is re-confirmed after the lock, not before."""

    from ai_smm.application.publications import PublicationUnavailable

    publication = make_publication(status=PublicationStatus.SCHEDULED)
    api_session = session_factory()

    try:
        with pytest.raises(PublicationUnavailable):
            cancel_publication(
                api_session,
                publication_id=publication.id,
                expected_project_id=make_other_project,
                actor="user:test",
            )

        api_session.rollback()
    finally:
        api_session.close()

    assert _lease_fields(session, publication.id)[0] is (
        PublicationStatus.SCHEDULED
    )


def test_a_row_deleted_before_the_lock_is_reported_as_missing(
    session_factory: sessionmaker[Session],
    session: Session,
    project: str,
    make_publication,
) -> None:
    from ai_smm.application.publications import PublicationUnavailable

    publication = make_publication(status=PublicationStatus.SCHEDULED)
    publication_id = publication.id

    api_session = session_factory()

    try:
        api_session.get(Publication, publication_id)

        session.delete(publication)
        session.commit()

        with pytest.raises(PublicationUnavailable):
            cancel_publication(
                api_session,
                publication_id=publication_id,
                expected_project_id=project,
                actor="user:test",
            )

        api_session.rollback()
    finally:
        api_session.close()


# -- a lock we cannot get in time ----------------------------------------


def test_a_command_that_cannot_get_the_lock_answers_503(
    client,
    session: Session,
    session_factory: sessionmaker[Session],
    login,
    member,
    project: str,
    make_publication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A web request must not hang on a row lock forever.

    The wait is bounded by lock_timeout. Nothing was read and nothing was
    changed when it fires, so the answer is "come back", not a verdict
    about a state the request could not see. The timeout is shortened
    here; in the application it is three seconds.
    """

    import ai_smm.application.publications as publications_module

    monkeypatch.setattr(publications_module, "LOCK_TIMEOUT_MS", 150)

    publication = make_publication(status=PublicationStatus.SCHEDULED)
    csrf = login(member(MembershipRole.ADMIN))

    holder = session_factory()

    try:
        # Hold the row from another connection, without committing.
        holder.execute(
            select(Publication)
            .where(Publication.id == publication.id)
            .with_for_update()
        ).one()

        response = client.post(
            f"/api/v1/publications/{publication.id}/cancel",
            json={},
            headers={"X-CSRF-Token": csrf},
        )
    finally:
        holder.rollback()
        holder.close()

    assert response.status_code == 503

    error = response.json()["error"]

    assert error["code"] == "DEPENDENCY_UNAVAILABLE"
    assert error["details"] == {"reason": "publication_locked"}
    assert response.headers["Retry-After"] == "2"

    # The row is untouched and no audit entry was written.
    assert _lease_fields(session, publication.id)[0] is (
        PublicationStatus.SCHEDULED
    )
    assert _command_audit(session, publication.id) == []
