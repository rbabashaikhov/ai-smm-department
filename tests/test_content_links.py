"""content_publication_links: the editorial -> delivery lineage.

Semantics under test: one row per Publication, describing which revision
its *current* snapshot came from.

* PK (content_item_id, revision_id, platform): a revision is delivered at
  most once per platform.
* UNIQUE(publication_id): a Publication is never attributed to two
  revisions.
* composite FK onto content_revisions: the revision belongs to the item.
* trigger: the Publication is in the item's project and on the platform.
* a safe rewrite replaces the Publication's row in the same transaction;
  a cancelled Publication keeps its row as historical lineage.

The link is authoritative; publications.source_ref and idempotency_key
are a trace and a second barrier, and the tests below tamper with them to
prove nothing decides from them.
"""
from __future__ import annotations

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


# -- 7: a safe rewrite replaces the lineage -----------------------------


def test_a_safe_rewrite_leaves_exactly_one_link_naming_the_new_revision(
    client, session: Session, approved, csrf
) -> None:
    item, first_revision = approved
    publication_id = _post(client, item.id, "materialize", csrf).json()[
        "publication"
    ]["id"]

    second = _revise_and_approve(client, item.id, csrf, "Вторая редакция.")
    response = _post(client, item.id, "materialize", csrf).json()

    assert response["result"] == "updated"
    assert response["publication"]["id"] == publication_id

    links = _links(session, publication_id=publication_id)

    assert len(links) == 1
    assert str(links[0].revision_id) == second

    # The old revision is no longer attributed to any Publication: the
    # database answers "where did this snapshot come from" with one row.
    assert _links(session, revision_id=first_revision.id) == []
    assert session.get(Publication, publication_id).body == "Вторая редакция."


def test_the_replaced_revision_is_recorded_in_the_audit(
    client, session: Session, approved, csrf
) -> None:
    item, first_revision = approved
    _post(client, item.id, "materialize", csrf)
    second = _revise_and_approve(client, item.id, csrf, "Вторая редакция.")
    _post(client, item.id, "materialize", csrf)

    session.expire_all()
    entries = list(
        session.scalars(
            select(AuditLog)
            .where(
                AuditLog.subject == f"content_item:{item.id}",
                AuditLog.action == "content.materialized",
            )
            .order_by(AuditLog.id)
        )
    )

    assert [entry.details["result"] for entry in entries] == ["created", "updated"]
    assert entries[0].details["previous_revision_id"] is None
    assert entries[1].details["previous_revision_id"] == str(first_revision.id)
    assert entries[1].details["revision_id"] == second


def test_the_approval_history_survives_a_rewrite(
    client, session: Session, approved, csrf
) -> None:
    item, first_revision = approved
    _post(client, item.id, "materialize", csrf)
    _revise_and_approve(client, item.id, csrf, "Вторая редакция.")
    _post(client, item.id, "materialize", csrf)

    approvals = client.get(f"/api/v1/content-items/{item.id}/approvals").json()

    assert approvals["items"][0]["revision_id"] == str(first_revision.id)
    assert approvals["total"] == 2


# -- 8, 9: cancelled Publications keep their lineage --------------------


def test_a_cancelled_publication_keeps_its_historical_link(
    client, session: Session, approved, csrf
) -> None:
    item, first_revision = approved
    old_id = _post(client, item.id, "materialize", csrf).json()["publication"]["id"]
    old = session.get(Publication, old_id)
    old.status = PublicationStatus.CANCELLED
    session.commit()

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
    old = session.get(Publication, old_id)
    old.status = PublicationStatus.CANCELLED
    session.commit()

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

    _post(client, item.id, "materialize", csrf)
    _revise_and_approve(client, item.id, csrf, "вторая")
    _post(client, item.id, "materialize", csrf)

    session.expire_all()
    session.refresh(legacy)

    assert (
        legacy.body,
        legacy.idempotency_key,
        legacy.source_ref,
        legacy.status,
    ) == before
