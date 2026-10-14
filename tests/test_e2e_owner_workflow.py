"""SMM-025D Phase 5: the owner's whole workflow, end to end, as packaged.

Three processes, each with its production role and nothing more:

* smm-auth  -- ai_smm.api.auth_service, connected as ai_smm_auth (the role
  and the smm_auth functions installed as the operator installs them);
* smm-api   -- ai_smm.api.app with business mutations on, connected as
  ai_smm_cc_rw, serving no login of its own (AI_SMM_API_AUTH_ROUTES=false),
  holding the credential PUBLIC key only;
* the worker -- the real claim and publishing service, as the owner role,
  holding the PRIVATE key.

Every request goes over HTTP to the application that serves it in
production; the session cookie is the one smm-auth set. Nothing reaches a
platform: GET /me is FakeIdentityService, publishing is FakePublisher.
The tokens are random test strings.

Skipped without AI_SMM_TEST_ADMIN_DATABASE_URL, like the roles.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ai_smm.api.auth_service import create_auth_app
from ai_smm.application.auth import SESSION_COOKIE_NAME
from ai_smm.db.models import (
    ApprovalGrant,
    AuditLog,
    MembershipRole,
    Publication,
    PublicationStatus,
    SocialAccount,
    User,
)
from ai_smm.db.session import create_db_engine
from ai_smm.publishing.routing import build_opener
from ai_smm.publishing.service import publish_claimed_publication
from ai_smm.queue import claim_due_publication
from tests.conftest import TEST_PASSWORD, fake_token
from tests.test_db_auth_contracts import AuthStack, auth_stack  # noqa: F401
from tests.test_publish_routing import AccountPublishers, LegacyFactory


SECOND = "e2e-second-project"


@pytest.fixture
def auth_client(auth_stack: AuthStack, settings) -> Iterator[TestClient]:  # noqa: F811
    engine = create_db_engine(auth_stack.url, settings=settings)
    app = create_auth_app(
        replace(settings, api_cookie_secure=False),
        session_factory=sessionmaker(bind=engine, expire_on_commit=False),
        access_log=False,
    )

    try:
        with TestClient(app) as client:
            yield client
    finally:
        engine.dispose()


@pytest.fixture
def api_client(
    api_settings,
    mutation_session_factory: sessionmaker[Session],
    credential_keys,
    identity_service,
) -> Iterator[TestClient]:
    from ai_smm.api.app import create_app

    app = create_app(
        replace(
            api_settings,
            api_auth_routes_enabled=False,
            credentials_public_key=credential_keys.public_b64,
            # The API never holds the private key.
            credentials_private_key_files=(),
            legacy_threads_projects=(),
        ),
        session_factory=mutation_session_factory,
        access_log=False,
        identity_fetcher=identity_service,
    )

    with TestClient(app) as client:
        yield client


@pytest.fixture
def owner(make_user, make_membership, project: str) -> User:
    user = make_user()
    make_membership(user=user, project_id=project, role=MembershipRole.OWNER)
    return user


def _ok(response, status: int = 200):
    assert response.status_code == status, response.text
    return response.json() if response.content else None


def test_the_owner_workflow_from_sign_in_to_the_worker(
    auth_client: TestClient,
    api_client: TestClient,
    owner: User,
    session: Session,
    account_settings,
    identity_service,
) -> None:
    # 1. Sign in at smm-auth; smm-api has no login route of its own.
    assert (
        api_client.post(
            "/api/v1/auth/login", json={"email": owner.email, "password": TEST_PASSWORD}
        ).status_code
        == 404
    )
    login = _ok(
        auth_client.post(
            "/api/v1/auth/login", json={"email": owner.email, "password": TEST_PASSWORD}
        )
    )
    token = auth_client.cookies.get(SESSION_COOKIE_NAME)
    assert token
    api_client.cookies.set(SESSION_COOKIE_NAME, token)
    csrf = {"X-CSRF-Token": login["csrf_token"]}
    assert _ok(api_client.get("/api/v1/auth/me"))["user"]["id"] == str(owner.id)

    # 2. A second project, with its settings.
    created = _ok(
        api_client.post(
            "/api/v1/projects",
            json={
                "id": SECOND,
                "display_name": "Второй проект",
                "default_timezone": "Europe/Moscow",
                "default_language": "ru",
                "topics": ["devops", "ai"],
                "tone_of_voice": "сдержанно",
            },
            headers=csrf,
        ),
        201,
    )
    assert created["id"] == SECOND
    project = _ok(api_client.get(f"/api/v1/projects/{SECOND}"))
    _ok(
        api_client.patch(
            f"/api/v1/projects/{SECOND}",
            json={
                "expected_version": project["version"],
                "default_timezone": "Asia/Yekaterinburg",
                "default_language": "en",
            },
            headers=csrf,
        )
    )
    settings_now = _ok(api_client.get(f"/api/v1/projects/{SECOND}/settings"))
    patched = _ok(
        api_client.patch(
            f"/api/v1/projects/{SECOND}/settings",
            json={
                "expected_version": settings_now["version"],
                "content_config": {
                    **settings_now["content_config"],
                    "topics": ["devops", "ai", "platform"],
                    "tone_of_voice": "дружелюбно, без жаргона",
                },
            },
            headers=csrf,
        )
    )
    assert patched["content_config"]["topics"] == ["devops", "ai", "platform"]
    project = _ok(api_client.get(f"/api/v1/projects/{SECOND}"))
    assert project["default_timezone"] == "Asia/Yekaterinburg"
    assert project["default_language"] == "en"
    assert project["legacy_publishing"] is False

    # 3. Two Threads accounts; a verified credential on the chosen one.
    chosen = _ok(
        api_client.post(
            f"/api/v1/projects/{SECOND}/social-accounts",
            json={"platform": "threads", "display_name": "Основной"},
            headers=csrf,
        ),
        201,
    )
    other = _ok(
        api_client.post(
            f"/api/v1/projects/{SECOND}/social-accounts",
            json={"platform": "threads", "display_name": "Другой"},
            headers=csrf,
        ),
        201,
    )
    assert chosen["publishing_ready"] is False

    chosen_token, other_token = fake_token("chosen"), fake_token("other")
    identity_service.register(chosen_token, "1001", "chosen_user")
    identity_service.register(other_token, "1002", "other_user")

    # A token of another identity is never bound to an account it is not.
    for account, value in ((chosen, chosen_token), (other, other_token)):
        _ok(
            api_client.post(
                f"/api/v1/social-accounts/{account['id']}/credentials",
                json={"access_token": value},
                headers=csrf,
            ),
            201,
        )

    ready = _ok(api_client.get(f"/api/v1/social-accounts/{chosen['id']}"))
    assert ready["publishing_ready"] is True
    assert ready["external_username"] == "chosen_user"
    # The API answers with a fingerprint, never the token.
    assert (
        chosen_token
        not in api_client.get(f"/api/v1/social-accounts/{chosen['id']}").text
    )

    # 4. Content with a destination, submitted for review.
    item = _ok(
        api_client.post(
            f"/api/v1/projects/{SECOND}/content-items",
            json={
                "title": "Как мы деплоим",
                "content_type": "post",
                "revision": {
                    "body": "Текст для основного аккаунта.",
                    "format": "text",
                    "social_account_id": chosen["id"],
                },
            },
            headers=csrf,
        ),
        201,
    )
    revision_id = item["current_revision_id"]
    _ok(
        api_client.post(
            f"/api/v1/content-items/{item['id']}/submit-review",
            json={"revision_id": revision_id},
            headers=csrf,
        )
    )

    # 5. Approving without the step-up is refused by the database.
    refused = api_client.post(
        f"/api/v1/content-items/{item['id']}/approve",
        json={"revision_id": revision_id},
        headers=csrf,
    )
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "APPROVAL_CONFIRMATION_REQUIRED"

    # 6. smm-auth's preview: this text, this account; then the password.
    preview = _ok(auth_client.get(f"/api/v1/auth/step-up/approvals/{revision_id}"))
    assert preview["body"] == "Текст для основного аккаунта."
    assert preview["social_account_id"] == chosen["id"]
    assert preview["account_external_username"] == "chosen_user"
    assert preview["account_ready"] is True

    wrong = auth_client.post(
        "/api/v1/auth/step-up/approvals",
        json={"revision_id": revision_id, "password": "not-the-password"},
        headers=csrf,
    )
    assert wrong.status_code == 401
    assert wrong.json()["error"]["code"] == "STEP_UP_FAILED"
    _ok(
        auth_client.post(
            "/api/v1/auth/step-up/approvals",
            json={"revision_id": revision_id, "password": TEST_PASSWORD},
            headers=csrf,
        ),
        201,
    )

    approved = _ok(
        api_client.post(
            f"/api/v1/content-items/{item['id']}/approve",
            json={"revision_id": revision_id, "note": "В основной."},
            headers=csrf,
        )
    )
    assert approved["item"]["status"] == "approved"
    assert approved["approval"]["actor_user_id"] == str(owner.id)
    session.expire_all()
    grant = session.scalars(
        select(ApprovalGrant).where(ApprovalGrant.revision_id == revision_id)
    ).one()
    assert grant.consumed_at is not None
    assert grant.external_account_id == "1001"

    # 7. Delivery snapshot and schedule.
    materialized = _ok(
        api_client.post(
            f"/api/v1/content-items/{item['id']}/materialize",
            json={"revision_id": revision_id},
            headers=csrf,
        ),
        201,
    )
    publication = materialized["publication"]
    assert publication["status"] == "approved"
    assert publication["social_account_id"] == chosen["id"]

    when = datetime.now(timezone.utc) - timedelta(minutes=1)
    scheduled = _ok(
        api_client.post(
            f"/api/v1/publications/{publication['id']}/schedule",
            json={"scheduled_at": when.isoformat()},
            headers=csrf,
        )
    )
    assert scheduled["status"] == "scheduled"

    # 8. The worker: the owner role, the private key, fakes for the platform.
    claimed = claim_due_publication(
        session, worker_id=account_settings.worker_id, lease_seconds=900
    )
    assert claimed is not None and claimed.id == publication["id"]
    session.commit()

    legacy, accounts = LegacyFactory(), AccountPublishers()
    outcome = publish_claimed_publication(
        session,
        claimed,
        settings=replace(account_settings, legacy_threads_projects=()),
        publisher_factory=legacy,
        media_root=Path.cwd(),
        account_publisher_factory=accounts,
        identity_fetcher=identity_service,
        opener=build_opener(account_settings),
    )
    session.expire_all()
    stored = session.get(Publication, publication["id"])

    assert outcome.status == "published"
    assert stored.status is PublicationStatus.PUBLISHED
    # The chosen account's token, and only that one; never the deployment's.
    assert accounts.tokens == [chosen_token]
    assert accounts.publish_calls == ["text"]
    assert legacy.calls == 0

    # 9. Provenance: who wrote each audit row, as PostgreSQL recorded it.
    # Sign-in and the approval's confirmation: smm-auth. Every business
    # change: the mutation role, as the owner. The worker: the owner role.
    session.expire_all()
    owner_role = session.bind.url.username
    writers = {
        (row.action, row.written_by) for row in session.scalars(select(AuditLog)).all()
    }
    by_action: dict[str, set[str]] = {}
    for action, writer in writers:
        by_action.setdefault(action, set()).add(writer)

    for action, who in by_action.items():
        if action.startswith("auth.") or action == "content.approval_confirmed":
            assert who == {"ai_smm_auth"}, (action, who)
        elif action.startswith(("content.", "project", "social_account")) or action in (
            # the queue commands, through the API
            "scheduled",
            "rescheduled",
            "cancelled",
        ):
            assert who == {"ai_smm_cc_rw"}, (action, who)
        else:
            # The worker's own rows (claimed, published, ...).
            assert who == {owner_role}, (action, who)

    assert {"auth.login", "content.approval_confirmed", "content.approved"} <= set(
        by_action
    )
    approval_rows = session.scalars(
        select(AuditLog).where(AuditLog.action == "content.approved")
    ).all()
    assert [r.actor for r in approval_rows] == [f"user:{owner.id}"]


def test_changing_the_destination_ends_the_approval_and_the_grant_does_not_carry_over(
    auth_client: TestClient,
    api_client: TestClient,
    owner: User,
    session: Session,
    identity_service,
    project: str,
) -> None:
    login = _ok(
        auth_client.post(
            "/api/v1/auth/login", json={"email": owner.email, "password": TEST_PASSWORD}
        )
    )
    api_client.cookies.set(
        SESSION_COOKIE_NAME, auth_client.cookies.get(SESSION_COOKIE_NAME)
    )
    csrf = {"X-CSRF-Token": login["csrf_token"]}

    accounts = []
    for name, external in (("A", "2001"), ("B", "2002")):
        account = _ok(
            api_client.post(
                f"/api/v1/projects/{project}/social-accounts",
                json={"platform": "threads", "display_name": name},
                headers=csrf,
            ),
            201,
        )
        value = fake_token(name)
        identity_service.register(value, external, f"user_{name}")
        _ok(
            api_client.post(
                f"/api/v1/social-accounts/{account['id']}/credentials",
                json={"access_token": value},
                headers=csrf,
            ),
            201,
        )
        accounts.append(account)

    item = _ok(
        api_client.post(
            f"/api/v1/projects/{project}/content-items",
            json={
                "title": "Смена аккаунта",
                "revision": {"body": "Текст.", "social_account_id": accounts[0]["id"]},
            },
            headers=csrf,
        ),
        201,
    )
    first = item["current_revision_id"]
    _ok(
        api_client.post(
            f"/api/v1/content-items/{item['id']}/submit-review",
            json={"revision_id": first},
            headers=csrf,
        )
    )
    _ok(
        auth_client.post(
            "/api/v1/auth/step-up/approvals",
            json={"revision_id": first, "password": TEST_PASSWORD},
            headers=csrf,
        ),
        201,
    )
    _ok(
        api_client.post(
            f"/api/v1/content-items/{item['id']}/approve",
            json={"revision_id": first},
            headers=csrf,
        )
    )

    # Same text, another account: a new revision, the approval ends.
    detail = _ok(api_client.get(f"/api/v1/content-items/{item['id']}"))
    revised = _ok(
        api_client.post(
            f"/api/v1/content-items/{item['id']}/revisions",
            json={
                "expected_item_version": detail["version"],
                "body": "Текст.",
                "social_account_id": accounts[1]["id"],
            },
            headers=csrf,
        ),
        201,
    )
    second = revised["revision"]["id"]
    detail = _ok(api_client.get(f"/api/v1/content-items/{item['id']}"))
    assert detail["status"] == "draft"
    assert detail["approval_in_effect"] is False

    # The old approval cannot be materialised for the new destination.
    stale = api_client.post(
        f"/api/v1/content-items/{item['id']}/materialize",
        json={"revision_id": second},
        headers=csrf,
    )
    assert stale.status_code in (409, 422), stale.text

    # And a grant issued for the first revision approves nothing else.
    _ok(
        api_client.post(
            f"/api/v1/content-items/{item['id']}/submit-review",
            json={"revision_id": second},
            headers=csrf,
        )
    )
    _ok(
        auth_client.post(
            "/api/v1/auth/step-up/approvals",
            json={"revision_id": first, "password": TEST_PASSWORD},
            headers=csrf,
        ),
        409,
    )
    refused = api_client.post(
        f"/api/v1/content-items/{item['id']}/approve",
        json={"revision_id": second},
        headers=csrf,
    )
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "APPROVAL_CONFIRMATION_REQUIRED"

    session.expire_all()
    assert session.get(SocialAccount, accounts[1]["id"]).external_account_id == "2002"
