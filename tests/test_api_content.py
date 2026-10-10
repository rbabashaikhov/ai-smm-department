"""The editorial layer over HTTP: items, revisions, review, approval."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ai_smm.db.models import (
    AuditLog,
    ContentApproval,
    ContentItem,
    ContentRevision,
    ContentStatus,
    MembershipRole,
)


def _create(client, project: str, csrf: str, **overrides):
    body = {
        "title": "Как мы деплоим",
        "content_type": "post",
        "revision": {"body": "Первая версия.", "format": "text"},
    }
    body.update(overrides)

    return client.post(
        f"/api/v1/projects/{project}/content-items",
        json=body,
        headers={"X-CSRF-Token": csrf},
    )


def _post(client, item_id, action: str, csrf: str, **body):
    return client.post(
        f"/api/v1/content-items/{item_id}/{action}",
        json=body,
        headers={"X-CSRF-Token": csrf},
    )


def _revise(client, item_id, csrf: str, version: int, body: str = "Новая версия."):
    return _post(
        client,
        item_id,
        "revisions",
        csrf,
        expected_item_version=version,
        body=body,
        format="text",
    )


def _approve_current(client, item_id, csrf: str) -> dict:
    """Submit and approve whatever revision is current. Returns the item."""

    item = client.get(f"/api/v1/content-items/{item_id}").json()
    revision_id = item["current_revision_id"]

    assert _post(
        client, item_id, "submit-review", csrf, revision_id=revision_id
    ).status_code == 200

    response = _post(client, item_id, "approve", csrf, revision_id=revision_id)

    assert response.status_code == 200, response.text

    return response.json()["item"]


def _audit_actions(session: Session, item_id) -> list[str]:
    session.expire_all()

    return [
        entry.action
        for entry in session.scalars(
            select(AuditLog)
            .where(AuditLog.subject == f"content_item:{item_id}")
            .order_by(AuditLog.id)
        )
    ]


# -- create --------------------------------------------------------------


def test_an_editor_creates_an_item_with_its_first_revision(
    client, session: Session, login, member, project: str
) -> None:
    user = member(MembershipRole.EDITOR)
    csrf = login(user)

    response = _create(client, project, csrf)

    assert response.status_code == 201, response.text

    body = response.json()

    assert body["project_id"] == project
    assert body["status"] == "draft"
    assert body["version"] == 1
    assert body["approved_revision_id"] is None
    assert body["approval_in_effect"] is False
    assert body["created_by_user_id"] == str(user.id)

    revision = body["current_revision"]

    assert revision["revision_number"] == 1
    assert revision["body"] == "Первая версия."
    assert revision["source"] == "human"
    assert revision["created_by_user_id"] == str(user.id)
    assert len(revision["content_hash"]) == 64
    assert body["current_revision_id"] == revision["id"]


def test_the_item_itself_holds_no_text(
    client, session: Session, login, member, project: str
) -> None:
    """The body lives only in revisions."""

    csrf = login(member(MembershipRole.EDITOR))
    item_id = _create(client, project, csrf).json()["id"]

    columns = set(ContentItem.__table__.columns.keys())

    assert "body" not in columns
    assert session.get(ContentItem, uuid.UUID(item_id)) is not None


def test_create_cannot_name_another_project_in_the_body(
    client, login, member, project: str, make_other_project
) -> None:
    csrf = login(member(MembershipRole.EDITOR))

    response = _create(client, project, csrf, project_id=make_other_project)

    assert response.status_code == 422


@pytest.mark.parametrize("bad", ["", "   "])
def test_an_empty_body_is_refused(
    client, login, member, project: str, bad: str
) -> None:
    csrf = login(member(MembershipRole.EDITOR))

    response = _create(
        client, project, csrf, revision={"body": bad, "format": "text"}
    )

    assert response.status_code == 422


def test_an_unknown_format_is_refused(
    client, login, member, project: str
) -> None:
    csrf = login(member(MembershipRole.EDITOR))

    response = _create(
        client, project, csrf, revision={"body": "x", "format": "video"}
    )

    assert response.status_code == 422


# -- RBAC ----------------------------------------------------------------


def test_a_viewer_reads_but_cannot_write(
    client, login, member, project: str, make_content
) -> None:
    item, revision = make_content()
    csrf = login(member(MembershipRole.VIEWER))

    assert client.get(f"/api/v1/content-items/{item.id}").status_code == 200
    assert (
        client.get(f"/api/v1/projects/{project}/content-items").status_code
        == 200
    )
    assert (
        client.get(f"/api/v1/content-items/{item.id}/revisions").status_code
        == 200
    )
    assert (
        client.get(f"/api/v1/content-items/{item.id}/approvals").status_code
        == 200
    )

    for response in (
        _create(client, project, csrf),
        _revise(client, item.id, csrf, 1),
        _post(client, item.id, "submit-review", csrf, revision_id=str(revision.id)),
        _post(client, item.id, "approve", csrf, revision_id=str(revision.id)),
        _post(client, item.id, "reject", csrf, revision_id=str(revision.id)),
        _post(client, item.id, "materialize", csrf),
    ):
        assert response.status_code == 403, response.text
        assert response.json()["error"]["details"]["required_role"] == "editor"


@pytest.mark.parametrize(
    "role",
    [MembershipRole.EDITOR, MembershipRole.ADMIN, MembershipRole.OWNER],
)
def test_editor_and_above_run_the_whole_editorial_flow(
    client, login, member, project: str, role: MembershipRole
) -> None:
    csrf = login(member(role))
    item_id = _create(client, project, csrf).json()["id"]

    approved = _approve_current(client, item_id, csrf)

    assert approved["status"] == "approved"

    materialized = _post(client, item_id, "materialize", csrf)

    assert materialized.status_code == 201


def test_writes_need_the_csrf_token(
    client, login, member, project: str
) -> None:
    login(member(MembershipRole.EDITOR))

    response = client.post(
        f"/api/v1/projects/{project}/content-items",
        json={"title": "t", "revision": {"body": "b", "format": "text"}},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CSRF_REQUIRED"


# -- project isolation ---------------------------------------------------


def test_another_projects_item_is_indistinguishable_from_a_missing_one(
    client, login, member, make_other_project, make_content
) -> None:
    other = make_other_project
    foreign, revision = make_content(project_id=other)
    csrf = login(member(MembershipRole.OWNER))
    missing = uuid.uuid4()

    for path in ("", "/revisions", "/approvals"):
        hidden = client.get(f"/api/v1/content-items/{foreign.id}{path}")
        absent = client.get(f"/api/v1/content-items/{missing}{path}")

        assert hidden.status_code == absent.status_code == 404
        assert hidden.json()["error"]["message"] == absent.json()["error"]["message"]

    for action, body in (
        ("revisions", {"expected_item_version": 1, "body": "x", "format": "text"}),
        ("submit-review", {"revision_id": str(revision.id)}),
        ("approve", {"revision_id": str(revision.id)}),
        ("reject", {"revision_id": str(revision.id)}),
        ("materialize", {}),
    ):
        assert _post(client, foreign.id, action, csrf, **body).status_code == 404


def test_a_list_never_includes_another_projects_items(
    client, login, member, project: str, make_other_project, make_content
) -> None:
    mine, _ = make_content()
    make_content(project_id=make_other_project)
    login(member(MembershipRole.VIEWER))

    body = client.get(f"/api/v1/projects/{project}/content-items").json()

    assert [item["id"] for item in body["items"]] == [str(mine.id)]
    assert (
        client.get(
            f"/api/v1/projects/{make_other_project}/content-items"
        ).status_code
        == 404
    )


def test_a_revision_of_another_item_cannot_be_approved(
    client, login, member, project: str, make_content
) -> None:
    """The revision must belong to the item in the URL."""

    first, _ = make_content()
    _, other_revision = make_content()
    csrf = login(member(MembershipRole.EDITOR))

    _post(
        client,
        first.id,
        "submit-review",
        csrf,
        revision_id=str(first.current_revision_id),
    )

    response = _post(
        client, first.id, "approve", csrf, revision_id=str(other_revision.id)
    )

    assert response.status_code == 404


# -- revisions: numbering, immutability, optimistic locking --------------


def test_revisions_are_numbered_without_gaps(
    client, login, member, project: str
) -> None:
    csrf = login(member(MembershipRole.EDITOR))
    item_id = _create(client, project, csrf).json()["id"]

    for version in (1, 2, 3):
        response = _revise(client, item_id, csrf, version, f"v{version + 1}")

        assert response.status_code == 201
        assert response.json()["revision"]["revision_number"] == version + 1
        assert response.json()["item"]["version"] == version + 1

    revisions = client.get(f"/api/v1/content-items/{item_id}/revisions").json()

    assert [r["revision_number"] for r in revisions["items"]] == [1, 2, 3, 4]
    assert revisions["total"] == 4


def test_a_stale_item_version_is_refused(
    client, session: Session, login, member, project: str
) -> None:
    csrf = login(member(MembershipRole.EDITOR))
    item_id = _create(client, project, csrf).json()["id"]

    assert _revise(client, item_id, csrf, 1).status_code == 201

    stale = _revise(client, item_id, csrf, 1, "lost update")

    assert stale.status_code == 409

    error = stale.json()["error"]

    assert error["code"] == "VERSION_CONFLICT"
    assert error["details"] == {"expected_version": 1, "current_version": 2}

    session.expire_all()
    count = session.scalar(
        select(func.count()).where(
            ContentRevision.content_item_id == uuid.UUID(item_id)
        )
    )

    assert count == 2


def test_there_is_no_endpoint_that_edits_a_revision(
    client, api_app, login, member, make_content
) -> None:
    item, revision = make_content()
    csrf = login(member(MembershipRole.OWNER))

    for method in ("put", "patch", "delete"):
        response = getattr(client, method)(
            f"/api/v1/content-items/{item.id}/revisions",
            headers={"X-CSRF-Token": csrf},
        )

        assert response.status_code in (404, 405)

    for path, methods in api_app.openapi()["paths"].items():
        if "revision" in path:
            assert set(methods) <= {"get", "post"}, path


def test_the_database_refuses_to_update_a_revision(
    session: Session, make_content
) -> None:
    """Immutability holds for every client, not just this API."""

    _, revision = make_content()

    with pytest.raises(IntegrityError, match="append-only"):
        session.execute(
            text("UPDATE content_revisions SET body = 'tampered' WHERE id = :id"),
            {"id": revision.id},
        )

    session.rollback()


def test_the_database_refuses_to_delete_a_revision(
    session: Session, make_content
) -> None:
    _, revision = make_content()

    with pytest.raises(IntegrityError, match="append-only"):
        session.execute(
            text("DELETE FROM content_revisions WHERE id = :id"),
            {"id": revision.id},
        )

    session.rollback()


def test_an_earlier_revision_is_unchanged_by_a_new_one(
    client, session: Session, login, member, project: str
) -> None:
    csrf = login(member(MembershipRole.EDITOR))
    created = _create(client, project, csrf).json()
    first = created["current_revision"]

    _revise(client, created["id"], csrf, 1, "Совсем другой текст.")

    session.expire_all()
    stored = session.get(ContentRevision, uuid.UUID(first["id"]))

    assert stored.body == first["body"]
    assert stored.content_hash == first["content_hash"]


def test_the_content_hash_tracks_the_deliverable_snapshot(
    client, login, member, project: str
) -> None:
    csrf = login(member(MembershipRole.EDITOR))
    item = _create(client, project, csrf).json()
    first_hash = item["current_revision"]["content_hash"]

    same = _post(
        client,
        item["id"],
        "revisions",
        csrf,
        expected_item_version=1,
        body="Первая версия.",
        format="text",
        editor_notes="only the notes changed",
    ).json()["revision"]

    different = _revise(client, item["id"], csrf, 2, "Другой текст.").json()[
        "revision"
    ]

    # Notes are about the revision, not the post: same hash.
    assert same["content_hash"] == first_hash
    assert different["content_hash"] != first_hash


def test_an_agent_can_be_recorded_as_the_source(
    client, login, member, project: str
) -> None:
    csrf = login(member(MembershipRole.EDITOR))

    body = _create(
        client,
        project,
        csrf,
        revision={
            "body": "Черновик от copywriter.",
            "format": "text",
            "source": "copywriter",
            "source_ref": "langfuse:trace/abc123",
        },
    ).json()

    assert body["current_revision"]["source"] == "copywriter"
    assert body["current_revision"]["source_ref"] == "langfuse:trace/abc123"
    # Provenance grants nothing: still a draft.
    assert body["status"] == "draft"


# -- review and approval -------------------------------------------------


def test_submit_moves_draft_to_in_review(
    client, login, member, project: str
) -> None:
    csrf = login(member(MembershipRole.EDITOR))
    item = _create(client, project, csrf).json()

    response = _post(
        client, item["id"], "submit-review", csrf,
        revision_id=item["current_revision_id"],
    )

    assert response.status_code == 200
    assert response.json()["status"] == "in_review"
    assert response.json()["version"] == 2


def test_submitting_twice_is_refused(
    client, login, member, project: str
) -> None:
    csrf = login(member(MembershipRole.EDITOR))
    item = _create(client, project, csrf).json()
    revision_id = item["current_revision_id"]

    _post(client, item["id"], "submit-review", csrf, revision_id=revision_id)
    again = _post(client, item["id"], "submit-review", csrf, revision_id=revision_id)

    assert again.status_code == 409
    assert again.json()["error"]["code"] == "INVALID_STATE_TRANSITION"


def test_a_draft_cannot_be_approved_without_review(
    client, login, member, project: str
) -> None:
    csrf = login(member(MembershipRole.EDITOR))
    item = _create(client, project, csrf).json()

    response = _post(
        client, item["id"], "approve", csrf,
        revision_id=item["current_revision_id"],
    )

    assert response.status_code == 409
    assert response.json()["error"]["details"]["status"] == "draft"


def test_approval_names_and_records_the_exact_revision(
    client, session: Session, login, member, project: str
) -> None:
    user = member(MembershipRole.EDITOR)
    csrf = login(user)
    item = _create(client, project, csrf).json()
    revision_id = item["current_revision_id"]

    _post(client, item["id"], "submit-review", csrf, revision_id=revision_id)
    response = _post(
        client, item["id"], "approve", csrf,
        revision_id=revision_id, note="Можно публиковать.",
    )

    assert response.status_code == 200

    body = response.json()

    assert body["item"]["status"] == "approved"
    assert body["item"]["approved_revision_id"] == revision_id
    assert body["approval"]["revision_id"] == revision_id
    assert body["approval"]["decision"] == "approved"
    assert body["approval"]["actor_user_id"] == str(user.id)
    assert body["approval"]["note"] == "Можно публиковать."

    detail = client.get(f"/api/v1/content-items/{item['id']}").json()

    assert detail["approval_in_effect"] is True


def test_approving_a_stale_revision_is_refused(
    client, session: Session, login, member, project: str
) -> None:
    """The reviewer read revision 1; revision 2 now exists."""

    csrf = login(member(MembershipRole.EDITOR))
    item = _create(client, project, csrf).json()
    read_by_reviewer = item["current_revision_id"]

    revised = _revise(client, item["id"], csrf, 1, "Изменили после чтения.").json()
    current = revised["revision"]["id"]

    _post(client, item["id"], "submit-review", csrf, revision_id=current)

    response = _post(
        client, item["id"], "approve", csrf, revision_id=read_by_reviewer
    )

    assert response.status_code == 409

    error = response.json()["error"]

    assert error["code"] == "STALE_REVISION"
    assert error["details"] == {
        "requested_revision_id": read_by_reviewer,
        "current_revision_id": current,
    }

    session.expire_all()

    assert session.scalars(select(ContentApproval)).all() == []
    assert session.get(ContentItem, uuid.UUID(item["id"])).status is (
        ContentStatus.IN_REVIEW
    )


def test_reject_records_the_decision(
    client, login, member, project: str
) -> None:
    user = member(MembershipRole.EDITOR)
    csrf = login(user)
    item = _create(client, project, csrf).json()
    revision_id = item["current_revision_id"]

    _post(client, item["id"], "submit-review", csrf, revision_id=revision_id)
    response = _post(
        client, item["id"], "reject", csrf,
        revision_id=revision_id, note="Слишком длинно.",
    )

    assert response.status_code == 200

    body = response.json()

    assert body["item"]["status"] == "rejected"
    assert body["item"]["approved_revision_id"] is None
    assert body["approval"]["decision"] == "rejected"
    assert body["approval"]["note"] == "Слишком длинно."


def test_a_rejected_revision_is_not_resubmitted(
    client, login, member, project: str
) -> None:
    csrf = login(member(MembershipRole.EDITOR))
    item = _create(client, project, csrf).json()
    revision_id = item["current_revision_id"]

    _post(client, item["id"], "submit-review", csrf, revision_id=revision_id)
    _post(client, item["id"], "reject", csrf, revision_id=revision_id)

    response = _post(client, item["id"], "submit-review", csrf, revision_id=revision_id)

    assert response.status_code == 409
    assert "new revision" in response.json()["error"]["message"]

    # A new revision is the way forward, and it starts in draft.
    revised = _revise(client, item["id"], csrf, 3).json()

    assert revised["item"]["status"] == "draft"


def test_a_new_revision_ends_the_approval(
    client, session: Session, login, member, project: str
) -> None:
    """Approval of revision 1 says nothing about revision 2."""

    csrf = login(member(MembershipRole.EDITOR))
    item = _create(client, project, csrf).json()
    approved = _approve_current(client, item["id"], csrf)

    assert approved["approved_revision_id"] == item["current_revision_id"]

    revised = _revise(client, item["id"], csrf, approved["version"]).json()

    assert revised["item"]["status"] == "draft"
    assert revised["item"]["approved_revision_id"] is None

    detail = client.get(f"/api/v1/content-items/{item['id']}").json()

    assert detail["approval_in_effect"] is False

    # And it cannot be delivered until the new revision is approved.
    response = _post(client, item["id"], "materialize", csrf)

    assert response.status_code == 409
    assert response.json()["error"]["details"]["status"] == "draft"

    # The earlier approval is still in the history, for the old revision.
    approvals = client.get(f"/api/v1/content-items/{item['id']}/approvals").json()

    assert [a["revision_id"] for a in approvals["items"]] == [
        item["current_revision_id"]
    ]


def test_the_database_will_not_hold_an_approval_of_an_old_revision(
    session: Session, make_content
) -> None:
    """The check constraint, independently of the service."""

    from ai_smm.application.content import RevisionInput, create_revision

    item, first = make_content()
    create_revision(
        session,
        content_item_id=item.id,
        expected_project_id=item.project_id,
        expected_item_version=1,
        revision=RevisionInput(body="second", format="text"),
        created_by_user=None,
    )
    session.commit()

    with pytest.raises(IntegrityError, match="approval_is_current"):
        session.execute(
            text(
                "UPDATE content_items SET status = 'approved', "
                "approved_revision_id = :old WHERE id = :id"
            ),
            {"old": first.id, "id": item.id},
        )

    session.rollback()


# -- history -------------------------------------------------------------


def test_the_approval_history_is_append_only(
    client, session: Session, login, member, project: str
) -> None:
    csrf = login(member(MembershipRole.EDITOR))
    item = _create(client, project, csrf).json()

    # reject r1, revise, approve r2: two decisions, both kept.
    first = item["current_revision_id"]
    _post(client, item["id"], "submit-review", csrf, revision_id=first)
    _post(client, item["id"], "reject", csrf, revision_id=first)
    second = _revise(client, item["id"], csrf, 3).json()["revision"]["id"]
    _post(client, item["id"], "submit-review", csrf, revision_id=second)
    _post(client, item["id"], "approve", csrf, revision_id=second)

    history = client.get(f"/api/v1/content-items/{item['id']}/approvals").json()

    assert [(a["revision_id"], a["decision"]) for a in history["items"]] == [
        (first, "rejected"),
        (second, "approved"),
    ]

    for statement in (
        "UPDATE content_approvals SET decision = 'approved'",
        "DELETE FROM content_approvals",
    ):
        with pytest.raises(IntegrityError, match="append-only"):
            session.execute(text(statement))

        session.rollback()


def test_an_approval_requires_a_human_actor(
    session: Session, make_content
) -> None:
    """An agent has no user, and an approval without one cannot be stored."""

    from ai_smm.application.content import approve_revision

    item, revision = make_content()

    with pytest.raises(PermissionError):
        approve_revision(
            session,
            content_item_id=item.id,
            expected_project_id=item.project_id,
            revision_id=revision.id,
            actor_user=None,  # type: ignore[arg-type]
        )

    # And at the database level, too.
    with pytest.raises(IntegrityError):
        session.add(
            ContentApproval(
                content_item_id=item.id,
                revision_id=revision.id,
                decision="approved",
                actor_user_id=None,
            )
        )
        session.flush()

    session.rollback()


# -- audit ---------------------------------------------------------------


def test_every_editorial_step_is_audited(
    client, session: Session, login, member, project: str
) -> None:
    user = member(MembershipRole.EDITOR)
    csrf = login(user)
    item = _create(client, project, csrf).json()
    _revise(client, item["id"], csrf, 1)
    _approve_current(client, item["id"], csrf)
    _post(client, item["id"], "materialize", csrf)

    assert _audit_actions(session, item["id"]) == [
        "content.created",
        "content.revision_created",
        "content.revision_created",
        "content.submitted_for_review",
        "content.approved",
        "content.materialized",
    ]

    entries = session.scalars(
        select(AuditLog).where(
            AuditLog.subject == f"content_item:{item['id']}"
        )
    ).all()

    assert {entry.actor for entry in entries} == {f"user:{user.id}"}


def test_rejection_is_audited(client, session: Session, login, member, project) -> None:
    csrf = login(member(MembershipRole.EDITOR))
    item = _create(client, project, csrf).json()
    revision_id = item["current_revision_id"]
    _post(client, item["id"], "submit-review", csrf, revision_id=revision_id)
    _post(client, item["id"], "reject", csrf, revision_id=revision_id)

    assert "content.rejected" in _audit_actions(session, item["id"])


def test_content_entries_appear_in_the_project_audit_view(
    client, login, member, project: str
) -> None:
    csrf = login(member(MembershipRole.ADMIN))
    item = _create(client, project, csrf).json()

    actions = [
        entry["action"]
        for entry in client.get(f"/api/v1/projects/{project}/audit").json()["items"]
    ]

    assert "content.created" in actions
    assert any(
        entry["subject"] == f"content_item:{item['id']}"
        for entry in client.get(f"/api/v1/projects/{project}/audit").json()["items"]
    )


# -- listing -------------------------------------------------------------


def test_items_can_be_filtered_by_status_and_paginated(
    client, login, member, project: str
) -> None:
    csrf = login(member(MembershipRole.EDITOR))
    ids = [_create(client, project, csrf, title=f"t{i}").json()["id"] for i in range(3)]
    _approve_current(client, ids[0], csrf)

    approved = client.get(
        f"/api/v1/projects/{project}/content-items?status=approved"
    ).json()

    assert [item["id"] for item in approved["items"]] == [ids[0]]

    page = client.get(
        f"/api/v1/projects/{project}/content-items?limit=2"
    ).json()

    assert page["total"] == 3
    assert len(page["items"]) == 2
