"""The bridge from an approved revision into a delivery Publication.

It creates a Publication and stops: it never rewrites one, never
schedules and never publishes. A Publication is the immutable delivery
snapshot of one revision. These tests pin what it copies, that an earlier
live snapshot blocks a newer revision until it is cancelled, and that
repeating it never makes a second Publication.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ai_smm.application.content import publication_key, publication_link
from ai_smm.db.models import (
    AuditLog,
    ContentPublicationLink,
    MembershipRole,
    Publication,
    PublicationStatus,
)


def _post(client, item_id, action: str, csrf: str, **body):
    return client.post(
        f"/api/v1/content-items/{item_id}/{action}",
        json=body,
        headers={"X-CSRF-Token": csrf},
    )


def _approve(client, item_id, csrf: str) -> str:
    item = client.get(f"/api/v1/content-items/{item_id}").json()
    revision_id = item["current_revision_id"]

    _post(client, item_id, "submit-review", csrf, revision_id=revision_id)
    response = _post(client, item_id, "approve", csrf, revision_id=revision_id)

    assert response.status_code == 200, response.text

    return revision_id


def _revise(client, item_id, csrf: str, body: str, **extra) -> str:
    version = client.get(f"/api/v1/content-items/{item_id}").json()["version"]
    response = _post(
        client,
        item_id,
        "revisions",
        csrf,
        expected_item_version=version,
        body=body,
        format=extra.pop("format", "text"),
        **extra,
    )

    assert response.status_code == 201, response.text

    return response.json()["revision"]["id"]


def _content_publications(session: Session, item_id) -> list[Publication]:
    """Publications of an item, through the authoritative link table."""

    session.expire_all()

    return list(
        session.scalars(
            select(Publication)
            .join(
                ContentPublicationLink,
                ContentPublicationLink.publication_id == Publication.id,
            )
            .where(
                ContentPublicationLink.content_item_id
                == uuid.UUID(str(item_id))
            )
            .order_by(Publication.id)
        )
    )


@pytest.fixture
def editor_csrf(login, member) -> str:
    return login(member(MembershipRole.EDITOR))


@pytest.fixture
def approved_item(client, make_content, editor_csrf):
    """An item whose first revision has been approved by a human."""

    item, revision = make_content(
        title="Заголовок материала", body="Утверждённый текст."
    )
    _approve(client, item.id, editor_csrf)

    return item, revision


# -- preconditions -------------------------------------------------------


@pytest.mark.parametrize("stop_at", ["draft", "in_review", "rejected"])
def test_only_an_approved_item_is_materialised(
    client, session: Session, make_content, editor_csrf, stop_at: str
) -> None:
    item, revision = make_content()

    if stop_at != "draft":
        _post(
            client,
            item.id,
            "submit-review",
            editor_csrf,
            revision_id=str(revision.id),
        )

    if stop_at == "rejected":
        _post(client, item.id, "reject", editor_csrf, revision_id=str(revision.id))

    response = _post(client, item.id, "materialize", editor_csrf)

    assert response.status_code == 409
    assert response.json()["error"]["details"]["status"] == stop_at
    assert _content_publications(session, item.id) == []


def test_materialising_a_stale_revision_is_refused(
    client, approved_item, editor_csrf
) -> None:
    item, _ = approved_item

    response = _post(
        client, item.id, "materialize", editor_csrf, revision_id=str(uuid.uuid4())
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "STALE_REVISION"


def test_content_the_delivery_preflight_would_refuse_is_not_materialised(
    client, session: Session, make_content, editor_csrf
) -> None:
    """A carousel with one image passes review but cannot be delivered."""

    item, _ = make_content(
        publication_format="carousel",
        images=[{"path": "a.jpg", "alt_text": "a"}],
    )
    _approve(client, item.id, editor_csrf)

    response = _post(client, item.id, "materialize", editor_csrf)

    assert response.status_code == 422
    assert "carousel" in response.json()["error"]["details"]["content_error"]
    assert _content_publications(session, item.id) == []


# -- create and snapshot -------------------------------------------------


def test_the_first_materialisation_creates_a_safe_pre_delivery_publication(
    client, session: Session, approved_item, editor_csrf
) -> None:
    item, revision = approved_item

    response = _post(client, item.id, "materialize", editor_csrf)

    assert response.status_code == 201

    body = response.json()

    assert body["result"] == "created"
    assert body["platform"] == "threads"
    assert body["revision_id"] == str(revision.id)

    publication = body["publication"]

    # Ready for an admin to schedule -- and not one step further.
    assert publication["status"] == "approved"
    assert publication["human_reviewed"] is True
    assert publication["scheduled_at"] is None
    assert publication["claimed_by"] is None
    assert publication["threads_post_id"] is None
    assert publication["published_at"] is None
    assert publication["attempt_count"] == 0
    assert publication["idempotency_key"] == publication_key(
        content_item_id=item.id, revision_id=revision.id, platform="threads"
    )


def test_the_snapshot_is_copied_exactly(
    client, session: Session, make_content, editor_csrf
) -> None:
    images = [
        {"path": "knowledge/a.jpg", "alt_text": "первая"},
        {"path": "knowledge/b.jpg", "alt_text": "вторая"},
    ]
    item, _ = make_content(
        title="Item title",
        body="Текст карусели.",
        publication_format="carousel",
        images=images,
    )
    revision_id = _revise(
        client,
        item.id,
        editor_csrf,
        "Текст карусели, финальный.",
        format="carousel",
        images=images,
        title="Revision heading",
        editor_score=8.5,
    )
    _approve(client, item.id, editor_csrf)

    body = _post(client, item.id, "materialize", editor_csrf).json()
    publication = session.get(Publication, body["publication"]["id"])

    assert publication.title == "Revision heading"
    assert publication.body == "Текст карусели, финальный."
    assert publication.format == "carousel"
    assert publication.images == images
    assert publication.items == []
    assert float(publication.editor_score) == 8.5
    assert publication.source_ref == publication_link(item.id)
    assert revision_id in publication.idempotency_key


def test_the_item_title_is_used_when_the_revision_has_none(
    client, session: Session, approved_item, editor_csrf
) -> None:
    item, _ = approved_item

    body = _post(client, item.id, "materialize", editor_csrf).json()

    assert body["publication"]["title"] == "Заголовок материала"


def test_materialisation_never_schedules_or_publishes(
    client, session: Session, approved_item, editor_csrf, monkeypatch
) -> None:
    import ai_smm.publishing.threads as threads_module
    import ai_smm.queue as queue_module

    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("materialisation reached delivery")

    monkeypatch.setattr(threads_module.ThreadsPublisher, "__init__", explode)
    monkeypatch.setattr(queue_module, "claim_due_publication", explode)

    item, _ = approved_item

    response = _post(client, item.id, "materialize", editor_csrf)

    assert response.status_code == 201

    publication = session.get(Publication, response.json()["publication"]["id"])

    assert publication.status is PublicationStatus.APPROVED
    assert publication.scheduled_at is None


def test_the_ordinal_continues_the_projects_sequence(
    client, session: Session, make_publication, approved_item, editor_csrf
) -> None:
    """Legacy rows keep their ordinals; a materialised one comes after."""

    legacy = [make_publication() for _ in range(3)]
    item, _ = approved_item

    body = _post(client, item.id, "materialize", editor_csrf).json()

    assert body["publication"]["ordinal"] == max(p.ordinal for p in legacy) + 1


# -- idempotency ---------------------------------------------------------


def test_repeating_materialisation_never_creates_a_second_publication(
    client, session: Session, approved_item, editor_csrf
) -> None:
    item, _ = approved_item

    first = _post(client, item.id, "materialize", editor_csrf)
    second = _post(client, item.id, "materialize", editor_csrf)
    third = _post(client, item.id, "materialize", editor_csrf)

    assert first.status_code == 201
    assert second.status_code == third.status_code == 200
    assert second.json()["result"] == third.json()["result"] == "unchanged"

    ids = {r.json()["publication"]["id"] for r in (first, second, third)}

    assert len(ids) == 1
    assert len(_content_publications(session, item.id)) == 1


def test_a_replay_changes_nothing_even_after_delivery_moved_on(
    client, session: Session, approved_item, editor_csrf
) -> None:
    """The same revision, already scheduled: report it, never touch it."""

    item, _ = approved_item
    created = _post(client, item.id, "materialize", editor_csrf).json()
    publication = session.get(Publication, created["publication"]["id"])
    publication.status = PublicationStatus.SCHEDULED
    publication.scheduled_at = datetime.now(timezone.utc) + timedelta(hours=1)
    session.commit()
    before = (publication.status, publication.scheduled_at, publication.updated_at)

    replay = _post(client, item.id, "materialize", editor_csrf)

    assert replay.status_code == 200
    assert replay.json()["result"] == "unchanged"

    session.expire_all()
    session.refresh(publication)

    after = (publication.status, publication.scheduled_at, publication.updated_at)

    assert after == before


def test_a_replay_writes_no_second_audit_entry(
    client, session: Session, approved_item, editor_csrf
) -> None:
    item, _ = approved_item

    _post(client, item.id, "materialize", editor_csrf)
    _post(client, item.id, "materialize", editor_csrf)

    session.expire_all()
    entries = session.scalars(
        select(AuditLog).where(
            AuditLog.subject == f"content_item:{item.id}",
            AuditLog.action == "content.materialized",
        )
    ).all()

    assert len(entries) == 1
    assert entries[0].details["result"] == "created"


def test_the_database_itself_refuses_a_duplicate_key(
    session: Session, approved_item, client, editor_csrf
) -> None:
    """The guarantee does not rest on the service: the key is UNIQUE."""

    from sqlalchemy.exc import IntegrityError

    item, revision = approved_item
    created = _post(client, item.id, "materialize", editor_csrf).json()

    session.add(
        Publication(
            project_id=item.project_id,
            platform="threads",
            ordinal=999,
            title="dup",
            format="text",
            body="dup",
            images=[],
            items=[],
            idempotency_key=created["publication"]["idempotency_key"],
        )
    )

    with pytest.raises(IntegrityError, match="idempotency_key"):
        session.flush()

    session.rollback()


# -- a newer revision ----------------------------------------------------


def _snapshot(publication: Publication) -> tuple:
    """Every column of the row, for a byte-for-byte comparison."""

    return tuple(
        getattr(publication, column.key)
        for column in Publication.__table__.columns
    )


def test_a_newer_revision_is_refused_while_the_earlier_snapshot_is_approved(
    client, session: Session, approved_item, editor_csrf
) -> None:
    """approved is not editable either: it may already have been read."""

    item, first_revision = approved_item
    created = _post(client, item.id, "materialize", editor_csrf).json()
    publication = session.get(Publication, created["publication"]["id"])
    before = _snapshot(publication)

    second = _revise(client, item.id, editor_csrf, "Вторая редакция.")
    _approve(client, item.id, editor_csrf)

    response = _post(client, item.id, "materialize", editor_csrf)

    assert response.status_code == 409

    error = response.json()["error"]

    assert error["code"] == "PUBLICATION_NOT_EDITABLE"
    assert error["message"].startswith(
        "Previous delivery snapshot must be cancelled before a newer "
        "revision can be materialized."
    )
    assert error["details"] == {
        "command": "materialize",
        "publication_id": publication.id,
        "publication_status": "approved",
        "previous_revision_id": str(first_revision.id),
    }

    session.expire_all()
    session.refresh(publication)

    # Byte for byte: every column, including updated_at and the key.
    assert _snapshot(publication) == before
    assert second not in publication.idempotency_key
    # Nothing new was created beside it.
    assert len(_content_publications(session, item.id)) == 1


@pytest.mark.parametrize(
    "status",
    [
        PublicationStatus.DRAFT,
        PublicationStatus.APPROVED,
        PublicationStatus.SCHEDULED,
        PublicationStatus.CLAIMED,
        PublicationStatus.PUBLISHING,
        PublicationStatus.NEEDS_REVIEW,
        PublicationStatus.PUBLISHED,
        PublicationStatus.FAILED,
    ],
)
def test_every_non_cancelled_snapshot_blocks_a_newer_revision(
    client, session: Session, approved_item, editor_csrf, status
) -> None:
    item, _ = approved_item
    created = _post(client, item.id, "materialize", editor_csrf).json()
    publication = session.get(Publication, created["publication"]["id"])

    publication.status = status

    if status is PublicationStatus.PUBLISHED:
        publication.threads_post_id = "17900000000000051"
        publication.published_at = datetime.now(timezone.utc)

    if status is PublicationStatus.CLAIMED:
        publication.claimed_by = "worker-1"

    session.commit()
    before = _snapshot(publication)

    _revise(client, item.id, editor_csrf, "Поздняя правка.")
    _approve(client, item.id, editor_csrf)

    response = _post(client, item.id, "materialize", editor_csrf)

    assert response.status_code == 409, response.text

    error = response.json()["error"]

    assert error["code"] == "PUBLICATION_NOT_EDITABLE"
    assert error["details"]["publication_status"] == status.value
    assert error["details"]["publication_id"] == publication.id

    session.expire_all()
    session.refresh(publication)

    assert _snapshot(publication) == before
    # And no parallel Publication was created beside it.
    assert len(_content_publications(session, item.id)) == 1


def test_a_cancelled_publication_never_blocks_a_new_one(
    client, session: Session, approved_item, editor_csrf
) -> None:
    item, _ = approved_item
    created = _post(client, item.id, "materialize", editor_csrf).json()
    old = session.get(Publication, created["publication"]["id"])
    old.status = PublicationStatus.CANCELLED
    session.commit()

    _revise(client, item.id, editor_csrf, "Новая версия после отмены.")
    _approve(client, item.id, editor_csrf)

    response = _post(client, item.id, "materialize", editor_csrf)

    assert response.status_code == 201
    assert response.json()["result"] == "created"
    assert response.json()["publication"]["id"] != old.id

    session.expire_all()
    session.refresh(old)

    # History is left exactly as it was.
    assert old.status is PublicationStatus.CANCELLED
    assert old.body == "Утверждённый текст."


# -- the legacy flow, unchanged ------------------------------------------


def test_a_materialised_publication_flows_through_the_existing_queue(
    client,
    session: Session,
    login,
    member,
    approved_item,
    editor_csrf,
) -> None:
    """materialise (editor) -> schedule (admin) -> claim (worker), unchanged."""

    from ai_smm.queue import claim_due_publication

    item, _ = approved_item
    publication_id = _post(client, item.id, "materialize", editor_csrf).json()[
        "publication"
    ]["id"]

    # The editor cannot schedule: that is still an admin command.
    editor_attempt = client.post(
        f"/api/v1/publications/{publication_id}/schedule",
        json={"scheduled_at": datetime.now(timezone.utc).isoformat()},
        headers={"X-CSRF-Token": editor_csrf},
    )

    assert editor_attempt.status_code == 403

    client.cookies.clear()
    admin_csrf = login(member(MembershipRole.ADMIN))
    scheduled = client.post(
        f"/api/v1/publications/{publication_id}/schedule",
        json={
            "scheduled_at": (
                datetime.now(timezone.utc) - timedelta(minutes=1)
            ).isoformat()
        },
        headers={"X-CSRF-Token": admin_csrf},
    )

    assert scheduled.status_code == 200

    session.expire_all()
    claimed = claim_due_publication(
        session, worker_id="test-worker", lease_seconds=900
    )
    session.commit()

    assert claimed is not None
    assert claimed.id == publication_id
    assert claimed.human_reviewed is True


def test_legacy_publications_are_untouched_by_the_editorial_layer(
    client, session: Session, make_publication, approved_item, editor_csrf
) -> None:
    legacy = make_publication(status=PublicationStatus.SCHEDULED)
    before = (
        legacy.idempotency_key,
        legacy.body,
        legacy.status,
        legacy.source_ref,
        legacy.ordinal,
    )
    item, _ = approved_item

    _post(client, item.id, "materialize", editor_csrf)

    session.expire_all()
    session.refresh(legacy)

    assert (
        legacy.idempotency_key,
        legacy.body,
        legacy.status,
        legacy.source_ref,
        legacy.ordinal,
    ) == before
    assert session.scalar(select(func.count()).select_from(Publication)) == 2
