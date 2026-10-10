"""schedule, reschedule, cancel: the only writes, and what they refuse.

Each command moves a row inside the queue. None of them publishes, and
none of them touches a row whose outcome is unknown -- that is the
duplicate-post barrier the queue exists for.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_smm.db.models import AuditLog, MembershipRole, PublicationStatus


def _when(minutes: int = 30) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(minutes=minutes)
    ).isoformat()


def _post(client, publication_id: int, command: str, csrf: str, **body):
    return client.post(
        f"/api/v1/publications/{publication_id}/{command}",
        json=body,
        headers={"X-CSRF-Token": csrf},
    )


# -- schedule ------------------------------------------------------------


def test_admin_can_schedule_an_approved_publication(
    client, session: Session, login, member, project: str, make_publication
) -> None:
    publication = make_publication(
        status=PublicationStatus.APPROVED, scheduled_at=None
    )
    csrf = login(member(MembershipRole.ADMIN))
    when = _when()

    response = _post(
        client, publication.id, "schedule", csrf, scheduled_at=when
    )

    assert response.status_code == 200, response.text

    body = response.json()

    assert body["status"] == "scheduled"
    assert body["scheduled_at"] is not None

    session.expire_all()
    session.refresh(publication)

    assert publication.status is PublicationStatus.SCHEDULED
    assert publication.scheduled_at == datetime.fromisoformat(when)
    # The row stays reviewed, unclaimed and unpublished.
    assert publication.human_reviewed is True
    assert publication.claimed_by is None
    assert publication.threads_post_id is None


def test_owner_can_schedule_too(
    client, login, member, project: str, make_publication
) -> None:
    publication = make_publication(status=PublicationStatus.APPROVED)
    csrf = login(member(MembershipRole.OWNER))

    assert (
        _post(
            client, publication.id, "schedule", csrf, scheduled_at=_when()
        ).status_code
        == 200
    )


def test_scheduling_a_failed_row_clears_the_failure(
    client, session: Session, login, member, project: str, make_publication
) -> None:
    publication = make_publication(
        status=PublicationStatus.FAILED,
        attempt_count=3,
        last_error="HTTPError: 500",
    )
    csrf = login(member(MembershipRole.ADMIN))

    response = _post(
        client,
        publication.id,
        "schedule",
        csrf,
        scheduled_at=_when(),
        reset_attempts=True,
    )

    assert response.status_code == 200

    session.expire_all()
    session.refresh(publication)

    assert publication.status is PublicationStatus.SCHEDULED
    assert publication.last_error is None
    assert publication.attempt_count == 0


def test_a_naive_timestamp_is_refused(
    client, login, member, project: str, make_publication
) -> None:
    """An API must not guess a timezone; a wrong guess moves a publish."""

    publication = make_publication(status=PublicationStatus.APPROVED)
    csrf = login(member(MembershipRole.ADMIN))

    response = _post(
        client,
        publication.id,
        "schedule",
        csrf,
        scheduled_at="2026-12-01T09:00:00",
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_an_unknown_body_field_is_refused(
    client, login, member, project: str, make_publication
) -> None:
    publication = make_publication(status=PublicationStatus.APPROVED)
    csrf = login(member(MembershipRole.ADMIN))

    response = _post(
        client,
        publication.id,
        "schedule",
        csrf,
        scheduled_at=_when(),
        project_id="other-project",
    )

    assert response.status_code == 422


# -- reschedule ----------------------------------------------------------


def test_reschedule_moves_a_scheduled_row(
    client, session: Session, login, member, project: str, make_publication
) -> None:
    publication = make_publication(status=PublicationStatus.SCHEDULED)
    csrf = login(member(MembershipRole.ADMIN))
    when = _when(120)

    response = _post(
        client,
        publication.id,
        "reschedule",
        csrf,
        scheduled_at=when,
        note="moved to the morning slot",
    )

    assert response.status_code == 200

    session.expire_all()
    session.refresh(publication)

    assert publication.scheduled_at == datetime.fromisoformat(when)
    assert publication.status is PublicationStatus.SCHEDULED


def test_reschedule_refuses_an_approved_row(
    client, login, member, project: str, make_publication
) -> None:
    """Nothing to move yet: that is what schedule is for."""

    publication = make_publication(status=PublicationStatus.APPROVED)
    csrf = login(member(MembershipRole.ADMIN))

    response = _post(
        client, publication.id, "reschedule", csrf, scheduled_at=_when()
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INVALID_STATE_TRANSITION"


# -- cancel --------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [
        PublicationStatus.DRAFT,
        PublicationStatus.APPROVED,
        PublicationStatus.SCHEDULED,
        PublicationStatus.FAILED,
    ],
)
def test_cancel_takes_a_pending_row_out_of_the_queue(
    client, session: Session, login, member, project, make_publication, status
) -> None:
    publication = make_publication(status=status)
    csrf = login(member(MembershipRole.ADMIN))

    response = _post(
        client, publication.id, "cancel", csrf, note="not needed"
    )

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"

    session.expire_all()
    session.refresh(publication)

    assert publication.status is PublicationStatus.CANCELLED
    assert publication.claimed_by is None
    assert publication.next_attempt_at is None


def test_cancelling_twice_is_refused(
    client, login, member, project: str, make_publication
) -> None:
    publication = make_publication(status=PublicationStatus.CANCELLED)
    csrf = login(member(MembershipRole.ADMIN))

    response = _post(client, publication.id, "cancel", csrf)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INVALID_STATE_TRANSITION"


# -- refused transitions -------------------------------------------------


@pytest.mark.parametrize("command", ["schedule", "reschedule", "cancel"])
@pytest.mark.parametrize(
    "status",
    [
        PublicationStatus.CLAIMED,
        PublicationStatus.PUBLISHING,
        PublicationStatus.NEEDS_REVIEW,
        PublicationStatus.PUBLISHED,
    ],
)
def test_no_command_touches_an_in_flight_or_finished_row(
    client,
    session: Session,
    login,
    member,
    project: str,
    make_publication,
    command: str,
    status: PublicationStatus,
) -> None:
    """The heart of it: claimed, publishing, needs_review and published.

    A worker owns the first, the outcome of the next two is unknown, and
    the last one is live on Threads. Moving any of them from a web
    request is how a duplicate post happens.
    """

    extra = {}

    if status is PublicationStatus.PUBLISHED:
        extra = {
            "threads_post_id": "17900000000000002",
            "published_at": datetime.now(timezone.utc),
        }

    publication = make_publication(status=status, **extra)
    before = publication.status
    csrf = login(member(MembershipRole.ADMIN))

    response = _post(
        client,
        publication.id,
        command,
        csrf,
        **({"scheduled_at": _when()} if command != "cancel" else {}),
    )

    assert response.status_code == 409, response.text

    error = response.json()["error"]

    assert error["code"] == "INVALID_STATE_TRANSITION"
    assert error["details"]["status"] == status.value
    assert error["details"]["command"] == command
    assert error["details"]["allowed_from"]

    session.expire_all()
    session.refresh(publication)

    assert publication.status is before


def test_needs_review_is_pointed_at_the_operator_flow(
    client, login, member, project: str, make_publication
) -> None:
    """It must never be retried automatically."""

    publication = make_publication(status=PublicationStatus.NEEDS_REVIEW)
    csrf = login(member(MembershipRole.ADMIN))

    response = _post(
        client, publication.id, "schedule", csrf, scheduled_at=_when()
    )

    assert response.status_code == 409
    assert "reconcile" in response.json()["error"]["message"]


def test_a_row_carrying_publish_metadata_is_refused(
    client, session: Session, login, member, project: str, make_publication
) -> None:
    """queue.reschedule's own barrier, surfaced as the same conflict.

    The status says failed, but the row has a Threads post id: something
    did reach the platform, so rescheduling it would duplicate a live
    post.
    """

    publication = make_publication(
        status=PublicationStatus.FAILED,
        threads_post_id="17900000000000003",
    )
    csrf = login(member(MembershipRole.ADMIN))

    response = _post(
        client, publication.id, "schedule", csrf, scheduled_at=_when()
    )

    assert response.status_code == 409

    message = response.json()["error"]["message"]

    assert "publish metadata" in message or "duplicate" in message

    session.expire_all()
    session.refresh(publication)

    assert publication.status is PublicationStatus.FAILED
    assert publication.threads_post_id == "17900000000000003"


def test_the_draft_hint_names_the_approval_step(
    client, login, member, project: str, make_publication
) -> None:
    publication = make_publication(status=PublicationStatus.DRAFT)
    csrf = login(member(MembershipRole.ADMIN))

    response = _post(
        client, publication.id, "schedule", csrf, scheduled_at=_when()
    )

    assert response.status_code == 409
    assert "approve" in response.json()["error"]["message"].lower()


# -- RBAC ----------------------------------------------------------------


@pytest.mark.parametrize("command", ["schedule", "reschedule", "cancel"])
@pytest.mark.parametrize(
    "role", [MembershipRole.VIEWER, MembershipRole.EDITOR]
)
def test_viewer_and_editor_cannot_run_a_command(
    client,
    session: Session,
    login,
    member,
    project: str,
    make_publication,
    command: str,
    role: MembershipRole,
) -> None:
    publication = make_publication(status=PublicationStatus.SCHEDULED)
    before = publication.status
    csrf = login(member(role))

    response = _post(
        client,
        publication.id,
        command,
        csrf,
        **({"scheduled_at": _when()} if command != "cancel" else {}),
    )

    assert response.status_code == 403

    error = response.json()["error"]

    assert error["code"] == "FORBIDDEN"
    assert error["details"] == {
        "required_role": "admin",
        "your_role": role.value,
    }

    session.expire_all()
    session.refresh(publication)

    assert publication.status is before


@pytest.mark.parametrize("command", ["schedule", "reschedule", "cancel"])
def test_a_command_needs_the_csrf_token(
    client, login, member, project: str, make_publication, command: str
) -> None:
    publication = make_publication(status=PublicationStatus.SCHEDULED)
    login(member(MembershipRole.ADMIN))

    response = client.post(
        f"/api/v1/publications/{publication.id}/{command}",
        json={"scheduled_at": _when()},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CSRF_REQUIRED"


@pytest.mark.parametrize("command", ["schedule", "reschedule", "cancel"])
def test_a_command_needs_authentication(
    client, project: str, make_publication, command: str
) -> None:
    publication = make_publication(status=PublicationStatus.SCHEDULED)

    response = client.post(
        f"/api/v1/publications/{publication.id}/{command}",
        json={"scheduled_at": _when()},
    )

    assert response.status_code == 401


def test_a_command_cannot_reach_another_projects_publication(
    client,
    session: Session,
    login,
    member,
    make_other_project,
) -> None:
    from ai_smm.db.models import Publication

    other = make_other_project
    foreign = Publication(
        project_id=other,
        platform="threads",
        ordinal=1,
        title="Foreign",
        format="text",
        body="not yours",
        images=[],
        items=[],
        status=PublicationStatus.SCHEDULED,
        idempotency_key="other-project:threads:7:fixture",
    )
    session.add(foreign)
    session.commit()

    csrf = login(member(MembershipRole.OWNER))

    for command, body in (
        ("schedule", {"scheduled_at": _when()}),
        ("reschedule", {"scheduled_at": _when()}),
        ("cancel", {}),
    ):
        response = _post(client, foreign.id, command, csrf, **body)

        assert response.status_code == 404, command

    session.expire_all()
    session.refresh(foreign)

    assert foreign.status is PublicationStatus.SCHEDULED


# -- audit ---------------------------------------------------------------


def _entries(session: Session, action: str) -> list[AuditLog]:
    return list(
        session.scalars(
            select(AuditLog).where(AuditLog.action == action)
        ).all()
    )


def test_a_successful_schedule_is_audited_as_the_user(
    client, session: Session, login, member, project: str, make_publication
) -> None:
    publication = make_publication(status=PublicationStatus.APPROVED)
    user = member(MembershipRole.ADMIN)
    csrf = login(user)

    _post(
        client,
        publication.id,
        "schedule",
        csrf,
        scheduled_at=_when(),
        note="morning slot",
    )

    session.expire_all()
    entry = _entries(session, "rescheduled")[0]

    assert entry.actor == f"user:{user.id}"
    assert entry.subject == f"publication:{publication.id}"
    assert entry.details["note"] == "morning slot"
    assert entry.details["scheduled_at"]


def test_a_successful_cancel_is_audited(
    client, session: Session, login, member, project: str, make_publication
) -> None:
    publication = make_publication(status=PublicationStatus.SCHEDULED)
    user = member(MembershipRole.OWNER)
    csrf = login(user)

    _post(client, publication.id, "cancel", csrf, note="duplicate")

    session.expire_all()
    entry = _entries(session, "cancelled")[0]

    assert entry.actor == f"user:{user.id}"
    assert entry.subject == f"publication:{publication.id}"
    assert entry.details == {"note": "duplicate"}


def test_a_refused_command_writes_no_audit_entry(
    client, session: Session, login, member, project: str, make_publication
) -> None:
    publication = make_publication(status=PublicationStatus.NEEDS_REVIEW)
    csrf = login(member(MembershipRole.ADMIN))

    _post(client, publication.id, "schedule", csrf, scheduled_at=_when())

    session.expire_all()

    assert _entries(session, "rescheduled") == []


def test_a_forbidden_command_writes_no_audit_entry(
    client, session: Session, login, member, project: str, make_publication
) -> None:
    publication = make_publication(status=PublicationStatus.SCHEDULED)
    csrf = login(member(MembershipRole.VIEWER))

    _post(client, publication.id, "cancel", csrf)

    session.expire_all()

    assert _entries(session, "cancelled") == []
