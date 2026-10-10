"""content_publication_links: the editorial -> delivery lineage.

Semantics under test: one row per Publication, describing which revision
its *current* snapshot came from.

* PK (content_item_id, revision_id, platform): a revision is delivered at
  most once per platform.
* UNIQUE(publication_id): a Publication is never attributed to two
  revisions.
* composite FK onto content_revisions: the revision belongs to the item.
* trigger: the Publication is in the item's project and on the platform.
* a Publication is an immutable delivery snapshot: its link is written
  once and never repointed. A newer revision needs the earlier snapshot
  cancelled first; the cancelled one keeps its link as history.

The link is authoritative; publications.source_ref and idempotency_key
are a trace and a second barrier, and the tests below tamper with them to
prove nothing decides from them.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

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

    assert (
        _post(client, item_id, "approve", csrf, revision_id=revision_id).status_code
        == 200
    )

    return revision_id


def _revise_and_approve(client, item_id, csrf: str, body: str) -> str:
    version = client.get(f"/api/v1/content-items/{item_id}").json()["version"]
    response = _post(
        client,
        item_id,
        "revisions",
        csrf,
        expected_item_version=version,
        body=body,
        format="text",
    )

    assert response.status_code == 201

    return _approve(client, item_id, csrf)


def _links(session: Session, **where) -> list[ContentPublicationLink]:
    session.expire_all()
    stmt = select(ContentPublicationLink).order_by(
        ContentPublicationLink.created_at
    )

    for column, value in where.items():
        stmt = stmt.where(getattr(ContentPublicationLink, column) == value)

    return list(session.scalars(stmt))


def _cancel(session: Session, publication_id: int) -> None:
    """Cancel through the queue's own command, as the operator would."""

    from ai_smm.queue import cancel

    publication = session.get(Publication, publication_id)
    cancel(session, publication, actor="test:operator", note="replace")
    session.commit()


@pytest.fixture
def csrf(login, member) -> str:
    return login(member(MembershipRole.EDITOR))


@pytest.fixture
def approved(client, make_content, csrf):
    item, revision = make_content(body="Утверждённый текст.")
    _approve(client, item.id, csrf)

    return item, revision


# -- 1, 2: creation and exactness ---------------------------------------


def test_materialising_creates_exactly_one_link(
    client, session: Session, approved, csrf
) -> None:
    item, revision = approved

    body = _post(client, item.id, "materialize", csrf).json()
    links = _links(session, content_item_id=item.id)

    assert len(links) == 1

    link = links[0]

    assert link.revision_id == revision.id
    assert link.platform == "threads"
    assert link.publication_id == body["publication"]["id"]
    assert link.created_at is not None


def test_the_link_names_the_exact_approved_revision(
    client, session: Session, make_content, csrf
) -> None:
    """With three revisions, the link is to the one that was approved."""

    item, first = make_content()
    _revise_and_approve(client, item.id, csrf, "вторая")
    third = _revise_and_approve(client, item.id, csrf, "третья")

    _post(client, item.id, "materialize", csrf)
    (link,) = _links(session, content_item_id=item.id)

    assert str(link.revision_id) == third
    assert link.revision_id != first.id

    publication = session.get(Publication, link.publication_id)

    assert publication.body == "третья"


# -- 3, 4, 5: what the database refuses ---------------------------------


def test_a_revision_of_another_item_cannot_be_linked(
    session: Session, approved, make_content, client, csrf
) -> None:
    item, _ = approved
    _, foreign_revision = make_content()
    publication_id = _post(client, item.id, "materialize", csrf).json()[
        "publication"
    ]["id"]

    with pytest.raises(IntegrityError, match="fk_content_publication_links_revision"):
        session.execute(
            text(
                "UPDATE content_publication_links SET revision_id = :rev "
                "WHERE publication_id = :pub"
            ),
            {"rev": foreign_revision.id, "pub": publication_id},
        )

    session.rollback()


def test_one_publication_cannot_belong_to_two_links(
    session: Session, make_content, client, csrf
) -> None:
    first_item, _ = make_content()
    second_item, second_revision = make_content()
    _approve(client, first_item.id, csrf)
    publication_id = _post(client, first_item.id, "materialize", csrf).json()[
        "publication"
    ]["id"]

    with pytest.raises(
        IntegrityError, match="uq_content_publication_links_publication"
    ):
        session.add(
            ContentPublicationLink(
                content_item_id=second_item.id,
                revision_id=second_revision.id,
                platform="threads",
                publication_id=publication_id,
            )
        )
        session.flush()

    session.rollback()


def test_one_revision_cannot_be_linked_to_two_publications(
    session: Session, approved, client, csrf, make_publication
) -> None:
    # Created first: the fixture numbers its own ordinals from 1, and the
    # materialised row continues the sequence after it.
    other = make_publication(status=PublicationStatus.DRAFT)
    item, revision = approved
    _post(client, item.id, "materialize", csrf)

    with pytest.raises(IntegrityError, match="content_publication_links_pkey"):
        session.add(
            ContentPublicationLink(
                content_item_id=item.id,
                revision_id=revision.id,
                platform="threads",
                publication_id=other.id,
            )
        )
        session.flush()

    session.rollback()


def test_a_link_to_another_projects_publication_is_refused(
    session: Session, approved, make_other_project
) -> None:
    item, revision = approved
    foreign = Publication(
        project_id=make_other_project,
        platform="threads",
        ordinal=1,
        title="Foreign",
        format="text",
        body="not yours",
        images=[],
        items=[],
        idempotency_key="other-project:threads:1:x",
    )
    session.add(foreign)
    session.commit()

    with pytest.raises(IntegrityError, match="not in the project"):
        session.add(
            ContentPublicationLink(
                content_item_id=item.id,
                revision_id=revision.id,
                platform="threads",
                publication_id=foreign.id,
            )
        )
        session.flush()

    session.rollback()


def test_a_link_on_the_wrong_platform_is_refused(
    session: Session, approved, make_publication
) -> None:
    item, revision = approved
    publication = make_publication(status=PublicationStatus.DRAFT)

    with pytest.raises(IntegrityError, match="not in the project"):
        session.add(
            ContentPublicationLink(
                content_item_id=item.id,
                revision_id=revision.id,
                platform="telegram",
                publication_id=publication.id,
            )
        )
        session.flush()

    session.rollback()


def test_a_linked_publication_cannot_be_deleted(
    session: Session, approved, client, csrf
) -> None:
    """RESTRICT: lineage is never left pointing at nothing."""

    item, _ = approved
    publication_id = _post(client, item.id, "materialize", csrf).json()[
        "publication"
    ]["id"]

    with pytest.raises(
        IntegrityError, match="fk_content_publication_links_publication"
    ):
        session.execute(
            text("DELETE FROM publications WHERE id = :id"), {"id": publication_id}
        )

    session.rollback()


# -- 6: repeats are answered from the link -------------------------------


def test_a_repeat_is_answered_from_the_link_not_the_trace_columns(
    client, session: Session, approved, csrf
) -> None:
    """Tamper with source_ref; the repeat still finds the same row."""

    item, _ = approved
    first = _post(client, item.id, "materialize", csrf).json()["publication"]["id"]

    session.execute(
        text("UPDATE publications SET source_ref = 'tampered' WHERE id = :id"),
        {"id": first},
    )
    session.commit()

    repeat = _post(client, item.id, "materialize", csrf)

    assert repeat.status_code == 200
    assert repeat.json()["result"] == "unchanged"
    assert repeat.json()["publication"]["id"] == first
    assert session.scalar(select(func.count()).select_from(Publication)) == 1
    assert len(_links(session)) == 1


def test_a_publication_with_this_key_but_no_link_is_refused(
    client, session: Session, approved, csrf
) -> None:
    """The secondary barrier: lineage tampered with, so refuse to guess."""

    from ai_smm.application.content import publication_key

    item, revision = approved
    session.add(
        Publication(
            project_id=item.project_id,
            platform="threads",
            ordinal=77,
            title="Orphan",
            format="text",
            body="orphan",
            images=[],
            items=[],
            idempotency_key=publication_key(
                content_item_id=item.id,
                revision_id=revision.id,
                platform="threads",
            ),
        )
    )
    session.commit()

    response = _post(client, item.id, "materialize", csrf)

    assert response.status_code == 409
    assert "no lineage link" in response.json()["error"]["message"]
    assert _links(session) == []


def test_a_publication_claiming_the_item_only_by_source_ref_is_ignored(
    client, session: Session, approved, csrf, make_publication
) -> None:
    """source_ref is a trace. A row without a link is not the item's."""

    from ai_smm.application.content import publication_link

    item, _ = approved
    impostor = make_publication(status=PublicationStatus.DRAFT)
    impostor.source_ref = publication_link(item.id)
    session.commit()
    before = (impostor.body, impostor.idempotency_key, impostor.status)

    response = _post(client, item.id, "materialize", csrf)

    assert response.json()["result"] == "created"
    assert response.json()["publication"]["id"] != impostor.id

    session.expire_all()
    session.refresh(impostor)

    assert (impostor.body, impostor.idempotency_key, impostor.status) == before


# -- 7: a snapshot is never rewritten or repointed -----------------------


def test_a_newer_revision_never_repoints_the_existing_link(
    client, session: Session, approved, csrf
) -> None:
    item, first_revision = approved
    publication_id = _post(client, item.id, "materialize", csrf).json()[
        "publication"
    ]["id"]
    (link_before,) = _links(session, publication_id=publication_id)
    created_at = link_before.created_at

    second = _revise_and_approve(client, item.id, csrf, "Вторая редакция.")
    response = _post(client, item.id, "materialize", csrf)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PUBLICATION_NOT_EDITABLE"

    (link_after,) = _links(session, publication_id=publication_id)

    assert link_after.revision_id == first_revision.id
    assert link_after.created_at == created_at
    assert _links(session, revision_id=uuid.UUID(second)) == []
    assert session.get(Publication, publication_id).body == "Утверждённый текст."


def test_materialisation_never_updates_an_existing_publication(
    client, session: Session, engine, approved, csrf
) -> None:
    """Structural: across create, replay and refusal, no UPDATE is issued.

    Watched at the engine, so it covers every statement the API sends,
    whatever code path it comes from.
    """

    from sqlalchemy import event

    updates: list[str] = []

    def watch(conn, cursor, statement, parameters, context, executemany):
        normalized = " ".join(statement.split()).upper()

        if normalized.startswith("UPDATE PUBLICATIONS"):
            updates.append(statement)

    item, _ = approved
    event.listen(engine, "before_cursor_execute", watch)

    try:
        _post(client, item.id, "materialize", csrf)  # created
        _post(client, item.id, "materialize", csrf)  # unchanged
        _revise_and_approve(client, item.id, csrf, "Вторая.")
        refused = _post(client, item.id, "materialize", csrf)  # 409
    finally:
        event.remove(engine, "before_cursor_execute", watch)

    assert refused.status_code == 409
    assert updates == []


def test_a_successful_materialisation_audits_exactly_the_agreed_fields(
    client, session: Session, approved, csrf
) -> None:
    item, revision = approved
    publication_id = _post(client, item.id, "materialize", csrf).json()[
        "publication"
    ]["id"]

    _post(client, item.id, "materialize", csrf)  # replay: nothing written
    _revise_and_approve(client, item.id, csrf, "Вторая.")
    _post(client, item.id, "materialize", csrf)  # refused: nothing written

    session.expire_all()
    entries = list(
        session.scalars(
            select(AuditLog).where(
                AuditLog.subject == f"content_item:{item.id}",
                AuditLog.action == "content.materialized",
            )
        )
    )

    assert len(entries) == 1
    assert entries[0].details == {
        "result": "created",
        "publication_id": publication_id,
        "revision_id": str(revision.id),
        "revision_number": 1,
        "content_hash": revision.content_hash,
        "platform": "threads",
    }


def test_the_approval_history_survives_a_cancel_and_a_new_snapshot(
    client, session: Session, approved, csrf
) -> None:
    item, first_revision = approved
    first = _post(client, item.id, "materialize", csrf).json()["publication"]["id"]
    _cancel(session, first)
    second = _revise_and_approve(client, item.id, csrf, "Вторая редакция.")

    assert _post(client, item.id, "materialize", csrf).status_code == 201

    approvals = client.get(f"/api/v1/content-items/{item.id}/approvals").json()

    assert [a["revision_id"] for a in approvals["items"]] == [
        str(first_revision.id),
        second,
    ]


# -- 8, 9: cancelled Publications keep their lineage --------------------


def test_a_cancelled_publication_keeps_its_historical_link(
    client, session: Session, approved, csrf
) -> None:
    item, first_revision = approved
    old_id = _post(client, item.id, "materialize", csrf).json()["publication"]["id"]
    _cancel(session, old_id)

    second = _revise_and_approve(client, item.id, csrf, "После отмены.")
    response = _post(client, item.id, "materialize", csrf)

    assert response.status_code == 201

    new_id = response.json()["publication"]["id"]

    assert new_id != old_id

    (old_link,) = _links(session, publication_id=old_id)
    (new_link,) = _links(session, publication_id=new_id)

    # The cancelled one still says it carried revision 1, and only that.
    assert old_link.revision_id == first_revision.id
    assert str(new_link.revision_id) == second
    assert len(_links(session, content_item_id=item.id)) == 2


def test_the_cancelled_revision_is_never_materialised_twice(
    client, session: Session, approved, csrf
) -> None:
    """Same revision after cancel: the link answers, no second row."""

    item, _ = approved
    old_id = _post(client, item.id, "materialize", csrf).json()["publication"]["id"]
    _cancel(session, old_id)

    repeat = _post(client, item.id, "materialize", csrf)

    assert repeat.status_code == 200
    assert repeat.json()["result"] == "unchanged"
    assert repeat.json()["publication"]["id"] == old_id
    assert len(_links(session)) == 1


# -- 10: revisions stay immutable ----------------------------------------


def test_a_linked_revision_still_cannot_be_updated_or_deleted(
    session: Session, approved, client, csrf
) -> None:
    item, revision = approved
    _post(client, item.id, "materialize", csrf)

    for statement in (
        "UPDATE content_revisions SET body = 'x' WHERE id = :id",
        "DELETE FROM content_revisions WHERE id = :id",
    ):
        with pytest.raises(IntegrityError, match="append-only"):
            session.execute(text(statement), {"id": revision.id})

        session.rollback()


# -- 11: legacy Publications are untouched -------------------------------


def test_legacy_publications_have_no_link_and_keep_working(
    client,
    session: Session,
    login,
    member,
    make_publication,
    approved,
    csrf,
) -> None:
    """A legacy row is invisible to the editorial layer and flows as before."""

    from ai_smm.queue import claim_due_publication

    legacy = make_publication(
        status=PublicationStatus.SCHEDULED,
        scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    item, _ = approved
    _post(client, item.id, "materialize", csrf)

    assert _links(session, publication_id=legacy.id) == []

    # The 022B commands still act on it...
    client.cookies.clear()
    admin_csrf = login(member(MembershipRole.ADMIN))
    moved = client.post(
        f"/api/v1/publications/{legacy.id}/reschedule",
        json={
            "scheduled_at": (
                datetime.now(timezone.utc) - timedelta(seconds=30)
            ).isoformat()
        },
        headers={"X-CSRF-Token": admin_csrf},
    )

    assert moved.status_code == 200

    # ...and the worker claims it exactly as before (the materialised row
    # is approved, not scheduled, so it is not a candidate).
    session.expire_all()
    claimed = claim_due_publication(
        session, worker_id="test-worker", lease_seconds=900
    )
    session.commit()

    assert claimed is not None
    assert claimed.id == legacy.id


def test_materialisation_never_touches_a_legacy_row(
    client, session: Session, make_publication, approved, csrf
) -> None:
    legacy = make_publication(status=PublicationStatus.DRAFT)
    before = (legacy.body, legacy.idempotency_key, legacy.source_ref, legacy.status)
    item, _ = approved

    assert _post(client, item.id, "materialize", csrf).status_code == 201
    _revise_and_approve(client, item.id, csrf, "вторая")
    # Blocked by the item's own live snapshot -- the legacy row plays no
    # part in that decision either way.
    assert _post(client, item.id, "materialize", csrf).status_code == 409

    session.expire_all()
    session.refresh(legacy)

    assert (
        legacy.body,
        legacy.idempotency_key,
        legacy.source_ref,
        legacy.status,
    ) == before



# -- the whole lifecycle, as specified ----------------------------------


def test_the_full_snapshot_lifecycle(
    client, session: Session, login, member, make_content, csrf
) -> None:
    """Steps 1-11 of the agreed semantics, in one continuous story."""

    item, revision_a = make_content(body="Ревизия A.")
    _approve(client, item.id, csrf)

    # 1. revision A -> Publication A + link A.
    first = _post(client, item.id, "materialize", csrf)

    assert first.status_code == 201
    assert first.json()["result"] == "created"

    publication_a = first.json()["publication"]["id"]
    (link_a,) = _links(session, publication_id=publication_a)

    assert link_a.revision_id == revision_a.id

    # 2. exact replay -> the same Publication, unchanged.
    replay = _post(client, item.id, "materialize", csrf)

    assert replay.status_code == 200
    assert replay.json()["result"] == "unchanged"
    assert replay.json()["publication"]["id"] == publication_a

    # 3. revision B is created and approved.
    revision_b = _revise_and_approve(client, item.id, csrf, "Ревизия B.")

    # 4. B cannot be materialised while A is approved...
    a_row = session.get(Publication, publication_a)
    a_before = tuple(
        getattr(a_row, c.key) for c in Publication.__table__.columns
    )
    blocked = _post(client, item.id, "materialize", csrf)

    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "PUBLICATION_NOT_EDITABLE"

    # 5. ...A is byte-for-byte the same snapshot...
    session.expire_all()
    a_row = session.get(Publication, publication_a)

    assert (
        tuple(getattr(a_row, c.key) for c in Publication.__table__.columns)
        == a_before
    )

    # 6. ...and link A is still there.
    assert _links(session, publication_id=publication_a)[0].revision_id == (
        revision_a.id
    )

    # 7. an admin cancels A through the queue command.
    client.cookies.clear()
    admin_csrf = login(member(MembershipRole.ADMIN))

    assert (
        client.post(
            f"/api/v1/publications/{publication_a}/cancel",
            json={"note": "replaced by revision B"},
            headers={"X-CSRF-Token": admin_csrf},
        ).status_code
        == 200
    )

    # 8. now B materialises into a new Publication B...
    second = _post(client, item.id, "materialize", admin_csrf)

    assert second.status_code == 201
    assert second.json()["result"] == "created"

    publication_b = second.json()["publication"]["id"]

    assert publication_b != publication_a

    # 9. ...link B names revision B...
    (link_b,) = _links(session, publication_id=publication_b)

    assert str(link_b.revision_id) == revision_b

    # 10. ...link A still names revision A...
    (link_a_after,) = _links(session, publication_id=publication_a)

    assert link_a_after.revision_id == revision_a.id

    # 11. ...and A is a cancelled historical snapshot of revision A.
    session.expire_all()
    a_row = session.get(Publication, publication_a)

    assert a_row.status is PublicationStatus.CANCELLED
    assert a_row.body == "Ревизия A."
    assert session.get(Publication, publication_b).body == "Ревизия B."


# -- 12: the stale-intent race that the immutability rule closes --------


def test_an_admin_always_schedules_exactly_what_they_read(
    client, session: Session, login, member, make_content, csrf
) -> None:
    """The race from review, replayed end to end.

    1. the admin opens Publication A while it is approved;
    2. an editor gets a newer revision B approved;
    3. materialising B must fail while A is not cancelled;
    4. so when the admin schedules A, the row still holds exactly what
       they read -- it cannot have been rewritten underneath them.
    """

    item, _ = make_content(body="Текст, который видел админ.")
    _approve(client, item.id, csrf)
    publication_id = _post(client, item.id, "materialize", csrf).json()[
        "publication"
    ]["id"]

    # 1. the admin reads it.
    client.cookies.clear()
    admin_csrf = login(member(MembershipRole.ADMIN))
    seen = client.get(f"/api/v1/publications/{publication_id}").json()

    assert seen["status"] == "approved"

    # 2. meanwhile an editor revises and approves B.
    client.cookies.clear()
    editor_csrf = login(member(MembershipRole.EDITOR))
    _revise_and_approve(client, item.id, editor_csrf, "Тихая правка редактора.")

    # 3. materialising B is refused while A is live.
    refused = _post(client, item.id, "materialize", editor_csrf)

    assert refused.status_code == 409
    assert refused.json()["error"]["details"]["publication_id"] == publication_id

    # 4. the admin's schedule goes through -- on the text they read.
    client.cookies.clear()
    admin_csrf = login(member(MembershipRole.ADMIN))
    scheduled = client.post(
        f"/api/v1/publications/{publication_id}/schedule",
        json={
            "scheduled_at": (
                datetime.now(timezone.utc) + timedelta(hours=1)
            ).isoformat()
        },
        headers={"X-CSRF-Token": admin_csrf},
    )

    assert scheduled.status_code == 200

    queued = scheduled.json()

    for field in ("body", "title", "format", "images", "items", "idempotency_key"):
        assert queued[field] == seen[field], field

    assert queued["body"] == "Текст, который видел админ."
