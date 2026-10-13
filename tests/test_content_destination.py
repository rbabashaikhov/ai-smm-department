"""The destination account is part of what a human approves (SMM-025B B2).

Approval names one exact revision; the account is a column of that
immutable revision and part of its content hash. Choosing another account
is a new revision, which returns the item to draft and clears the
approval -- so no approval of "this text to account A" can ever send it
to account B.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from sqlalchemy import update
from sqlalchemy.orm import Session

from ai_smm.application.content import (
    DestinationRequired,
    NotDeliverable,
    RevisionInput,
    approve_revision,
    content_hash,
    create_content_item,
    create_revision,
    materialize_approved_revision,
    submit_for_review,
    validate_image_paths,
)
from ai_smm.db.models import (
    ContentStatus,
    MembershipRole,
    Publication,
    SocialAccount,
)


@pytest.fixture
def editor(member):
    return member(MembershipRole.EDITOR)


def _create(
    session: Session, user, *, account_id=None, project_id="test-project", images=None
):
    item, revision = create_content_item(
        session,
        project_id=project_id,
        title="Пост",
        content_type="post",
        revision=RevisionInput(
            body="Текст поста.",
            format="text" if not images else "image",
            images=images or [],
            social_account_id=account_id,
        ),
        created_by_user=user,
    )
    session.commit()

    return item, revision


def _approve(session: Session, item, revision, user, legacy=None):
    submit_for_review(
        session,
        content_item_id=item.id,
        expected_project_id=item.project_id,
        revision_id=revision.id,
        actor_user=user,
        legacy_projects=legacy,
    )
    approve_revision(
        session,
        content_item_id=item.id,
        expected_project_id=item.project_id,
        revision_id=revision.id,
        actor_user=user,
    )
    session.commit()


def _materialize(session: Session, item, user, legacy=None):
    result = materialize_approved_revision(
        session,
        content_item_id=item.id,
        expected_project_id=item.project_id,
        actor_user=user,
        mutations_enabled=True,
        legacy_projects=legacy,
    )
    session.commit()

    return result


def test_the_account_is_part_of_the_revision_and_its_hash(
    session: Session, editor, make_account
) -> None:
    account, _ = make_account()
    _, plain = _create(session, editor)
    _, routed = _create(session, editor, account_id=account.id)

    assert routed.social_account_id == account.id
    assert routed.content_hash != plain.content_hash
    # Hashes of revisions written before accounts existed are unchanged.
    assert plain.content_hash == content_hash(
        title=None, body="Текст поста.", format="text", images=[], items=[]
    )


def test_changing_the_account_needs_a_new_approval(
    session: Session, editor, make_account
) -> None:
    first, _ = make_account()
    second, _ = make_account()
    item, revision = _create(session, editor, account_id=first.id)
    _approve(session, item, revision, editor)
    session.refresh(item)
    assert item.status is ContentStatus.APPROVED

    item, new_revision = create_revision(
        session,
        content_item_id=item.id,
        expected_project_id=item.project_id,
        expected_item_version=item.version,
        revision=RevisionInput(
            body="Текст поста.", format="text", social_account_id=second.id
        ),
        created_by_user=editor,
    )
    session.commit()

    assert item.status is ContentStatus.DRAFT
    assert item.approved_revision_id is None
    assert new_revision.social_account_id == second.id


def test_materialisation_carries_the_approved_account(
    session: Session, editor, make_account
) -> None:
    account, _ = make_account()
    item, revision = _create(session, editor, account_id=account.id)
    _approve(session, item, revision, editor)

    result = _materialize(session, item, editor)

    assert result.publication.social_account_id == account.id
    assert result.publication.human_reviewed is True
    assert result.publication.status.value == "approved"


def test_a_project_without_legacy_publishing_must_choose_an_account(
    session: Session, editor
) -> None:
    item, revision = _create(session, editor)

    with pytest.raises(DestinationRequired):
        submit_for_review(
            session,
            content_item_id=item.id,
            expected_project_id=item.project_id,
            revision_id=revision.id,
            actor_user=editor,
            legacy_projects=(),
        )

    session.rollback()


def test_materialisation_rechecks_the_destination(
    session: Session, editor, make_account
) -> None:
    account, _ = make_account()
    item, revision = _create(session, editor, account_id=account.id)
    _approve(session, item, revision, editor)
    session.execute(
        update(SocialAccount)
        .where(SocialAccount.id == account.id)
        .values(status="disabled", disabled_at=SocialAccount.updated_at)
    )
    session.commit()

    with pytest.raises(NotDeliverable, match="disabled"):
        _materialize(session, item, editor)

    session.rollback()
    assert session.query(Publication).count() == 0


def test_legacy_approval_is_not_materialised_after_leaving_the_allow_list(
    session: Session, editor
) -> None:
    item, revision = _create(session, editor)
    _approve(session, item, revision, editor)

    with pytest.raises(DestinationRequired):
        _materialize(session, item, editor, legacy=())

    session.rollback()


def test_an_account_of_another_project_is_refused(
    session: Session, editor, make_account, make_other_project
) -> None:
    foreign, _ = make_account(project_id=make_other_project)

    with pytest.raises(ValueError, match="not an account of this project"):
        _create(session, editor, account_id=foreign.id)

    session.rollback()


def test_a_disabled_account_cannot_be_chosen(
    session: Session, editor, make_account
) -> None:
    disabled, _ = make_account(status="disabled")

    with pytest.raises(ValueError, match="disabled"):
        _create(session, editor, account_id=disabled.id)

    session.rollback()


@pytest.mark.parametrize(
    "path",
    [
        "a.jpg",
        "/etc/passwd",
        "knowledge/projects/other-project/assets/x.jpg",
        "knowledge/projects/test-project/assets/../../other-project/assets/x.jpg",
        "knowledge/projects/test-project/assets/",
        "knowledge/projects/test-project/x.jpg",
    ],
)
def test_images_must_be_the_projects_own_assets(path: str) -> None:
    with pytest.raises(ValueError):
        validate_image_paths(
            [{"path": path, "alt_text": "x"}], project_id="test-project"
        )


def test_an_own_asset_is_accepted() -> None:
    validate_image_paths(
        [{"path": "knowledge/projects/test-project/assets/photo.jpg"}],
        project_id="test-project",
    )


def test_the_api_takes_the_destination_and_shows_it(
    client, login, member, make_account
) -> None:
    account, _ = make_account()
    csrf = login(member(MembershipRole.EDITOR))

    created = client.post(
        "/api/v1/projects/test-project/content-items",
        json={
            "title": "Пост",
            "revision": {"body": "Текст", "social_account_id": str(account.id)},
        },
        headers={"X-CSRF-Token": csrf},
    )

    assert created.status_code == 201, created.text
    assert created.json()["current_revision"]["social_account_id"] == str(account.id)


def test_the_api_refuses_submission_without_a_destination(
    api_settings, session_factory, member, session: Session
) -> None:
    from fastapi.testclient import TestClient

    from ai_smm.api.app import create_app
    from tests.conftest import TEST_PASSWORD

    user = member(MembershipRole.EDITOR)
    app = create_app(
        replace(api_settings, legacy_threads_projects=()),
        session_factory=session_factory,
        access_log=False,
    )

    with TestClient(app) as client:
        csrf = client.post(
            "/api/v1/auth/login", json={"email": user.email, "password": TEST_PASSWORD}
        ).json()["csrf_token"]
        created = client.post(
            "/api/v1/projects/test-project/content-items",
            json={"title": "Пост", "revision": {"body": "Текст"}},
            headers={"X-CSRF-Token": csrf},
        ).json()
        response = client.post(
            f"/api/v1/content-items/{created['id']}/submit-review",
            json={"revision_id": created["current_revision"]["id"]},
            headers={"X-CSRF-Token": csrf},
        )

    assert response.status_code == 422
    assert response.json()["error"]["details"]["reason"] == "destination_required"
