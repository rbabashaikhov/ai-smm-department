"""smm-auth (ai_smm.api.auth_service), connected as ai_smm_auth.

The service runs against the real test database with the role and the
smm_auth functions installed exactly as the operator installs them
(tests/test_db_auth_contracts.py's auth_stack). Nothing is mocked: the
password is Argon2-verified, sessions and grants are written by the
SECURITY DEFINER functions, and every assertion about what was written is
read back as the owner.

Skipped without AI_SMM_TEST_ADMIN_DATABASE_URL, like the roles.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from ai_smm.api.auth_service import LOGIN_FAILURE_LIMIT, create_auth_app
from ai_smm.application.auth import SESSION_COOKIE_NAME
from ai_smm.application.content import submit_for_review
from ai_smm.db.models import (
    ApprovalGrant,
    AuditLog,
    MembershipRole,
    User,
    UserSession,
)
from ai_smm.db.session import create_db_engine
from tests.conftest import TEST_PASSWORD
from tests.test_db_auth_contracts import AuthStack, auth_stack  # noqa: F401


@pytest.fixture
def auth_factory(auth_stack: AuthStack, settings) -> Iterator[sessionmaker[Session]]:  # noqa: F811
    engine = create_db_engine(auth_stack.url, settings=settings)
    try:
        yield sessionmaker(bind=engine, expire_on_commit=False)
    finally:
        engine.dispose()


@pytest.fixture
def auth_app(auth_factory, settings):
    return create_auth_app(
        replace(settings, api_cookie_secure=False),
        session_factory=auth_factory,
        access_log=False,
    )


@pytest.fixture
def auth_client(auth_app) -> Iterator[TestClient]:
    with TestClient(auth_app) as client:
        yield client


@pytest.fixture
def owner(make_user, make_membership, project: str) -> User:
    user = make_user()
    make_membership(user=user, project_id=project, role=MembershipRole.OWNER)
    return user


def _login(client: TestClient, user: User, password: str = TEST_PASSWORD):
    return client.post(
        "/api/v1/auth/login", json={"email": user.email, "password": password}
    )


# -- the surface -------------------------------------------------------------------


def test_only_authentication_and_health_are_mounted(
    auth_app, auth_client: TestClient
) -> None:
    """Every route and method this service serves -- nothing business."""

    from fastapi.routing import iter_route_contexts

    # FastAPI 0.143 keeps included routers as _IncludedRouter objects;
    # iter_route_contexts walks them down to the real routes.
    routes = {
        (context.route.path, method)
        for context in iter_route_contexts(auth_app.routes)
        for method in context.route.methods
    }

    assert routes == {
        ("/health/live", "GET"),
        ("/health/ready", "GET"),
        ("/api/v1/auth/login", "POST"),
        ("/api/v1/auth/logout", "POST"),
        ("/api/v1/auth/step-up/approvals", "POST"),
        ("/api/v1/auth/step-up/approvals/{revision_id}", "GET"),
    }
    for path in ("/docs", "/openapi.json", "/api/v1/projects", "/api/v1/auth/me"):
        assert auth_client.get(path).status_code == 404


@pytest.mark.parametrize(
    ("peer", "allowed"),
    [
        ("10.231.18.3", True),
        ("10.231.18.2", True),
        ("10.231.18.9", False),
        ("172.18.0.5", False),
        ("testclient", False),
    ],
)
def test_peers_outside_the_allowlist_are_refused_before_routing(
    settings, peer: str, allowed: bool
) -> None:
    """The second layer under the bind address: nothing else is served."""

    app = create_auth_app(
        replace(settings, auth_allowed_peers=("10.231.18.0/29",)),
        access_log=False,
    )

    with TestClient(app, client=(peer, 40000)) as client:
        live = client.get("/health/live")
        login = client.post(
            "/api/v1/auth/login", json={"email": "a@example.com", "password": "x"}
        )

    if allowed:
        assert live.status_code == 200
    else:
        assert live.status_code == 403
        assert live.json()["error"]["code"] == "FORBIDDEN"
        # Refused before the route ran: no database was ever needed.
        assert login.status_code == 403


def test_ready_needs_the_database(auth_client: TestClient) -> None:
    assert auth_client.get("/health/ready").status_code == 200


# -- sign-in ------------------------------------------------------------------------


def test_sign_in_opens_a_session_written_by_the_function(
    auth_client: TestClient, session: Session, owner: User
) -> None:
    response = _login(auth_client, owner)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["user"]["email"] == owner.email
    assert "password_hash" not in response.text
    assert body["csrf_token"]

    cookie = response.cookies.get(SESSION_COOKIE_NAME)
    assert cookie and len(cookie) == 43

    session.expire_all()
    row = session.scalars(
        select(UserSession).where(UserSession.user_id == owner.id)
    ).one()
    assert row.revoked_at is None
    assert (row.expires_at - row.created_at).days == 7

    audit = session.scalars(
        select(AuditLog).where(AuditLog.action == "auth.login")
    ).one()
    assert audit.actor == f"user:{owner.id}"
    # Provenance: written by the auth service's role, inside the function.
    assert audit.written_by == "ai_smm_auth"


@pytest.mark.parametrize("case", ["wrong_password", "unknown", "inactive", "malformed"])
def test_every_failed_sign_in_looks_the_same(
    auth_client: TestClient, session: Session, owner: User, case: str
) -> None:
    email, password = owner.email, TEST_PASSWORD

    if case == "wrong_password":
        password = "not-the-password"
    elif case == "unknown":
        email = "nobody@example.com"
    elif case == "inactive":
        session.get(User, owner.id).is_active = False
        session.commit()
    elif case == "malformed":
        email = "not an address"

    response = auth_client.post(
        "/api/v1/auth/login", json={"email": email, "password": password}
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"
    assert SESSION_COOKIE_NAME not in response.cookies
    assert password not in response.text
    session.expire_all()
    assert session.scalars(select(UserSession)).all() == []


def test_repeated_failures_stop_the_password_check(
    auth_client: TestClient, session: Session, owner: User
) -> None:
    for _ in range(LOGIN_FAILURE_LIMIT):
        assert _login(auth_client, owner, "wrong").status_code == 401

    # Even the right password is not checked while the window holds.
    response = _login(auth_client, owner)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"
    session.expire_all()
    assert session.scalars(select(UserSession)).all() == []


def test_sign_out_needs_the_csrf_token_and_revokes_once(
    auth_client: TestClient, session: Session, owner: User
) -> None:
    csrf = _login(auth_client, owner).json()["csrf_token"]
    token = auth_client.cookies.get(SESSION_COOKIE_NAME)

    assert auth_client.post("/api/v1/auth/logout").status_code == 403
    assert (
        auth_client.post(
            "/api/v1/auth/logout", headers={"X-CSRF-Token": "x" * 64}
        ).status_code
        == 403
    )

    response = auth_client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf})

    assert response.status_code == 204
    session.expire_all()
    assert session.scalars(select(UserSession)).one().revoked_at is not None
    assert (
        len(
            session.scalars(
                select(AuditLog).where(AuditLog.action == "auth.logout")
            ).all()
        )
        == 1
    )

    # Again, with the same (now revoked) cookie: 401, no second audit row.
    auth_client.cookies.set(SESSION_COOKIE_NAME, token)
    again = auth_client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf})

    assert again.status_code == 401
    session.expire_all()
    assert (
        len(
            session.scalars(
                select(AuditLog).where(AuditLog.action == "auth.logout")
            ).all()
        )
        == 1
    )


# -- the step-up approval -----------------------------------------------------------


@pytest.fixture
def in_review(session: Session, owner: User, make_content, project: str):
    item, revision = make_content(user=owner)
    submit_for_review(
        session,
        content_item_id=item.id,
        expected_project_id=project,
        revision_id=revision.id,
        actor_user=owner,
        legacy_projects=(project,),
    )
    session.commit()
    return item, revision


def test_the_preview_is_what_would_be_published(
    auth_client: TestClient, owner: User, in_review
) -> None:
    item, revision = in_review
    _login(auth_client, owner)

    response = auth_client.get(f"/api/v1/auth/step-up/approvals/{revision.id}")

    assert response.status_code == 200, response.text
    preview = response.json()
    assert preview["content_item_id"] == str(item.id)
    assert preview["body"] == revision.body
    assert preview["is_current"] is True
    assert preview["item_status"] == "in_review"
    assert preview["content_hash"] == revision.content_hash


@pytest.mark.parametrize(
    "role", [MembershipRole.VIEWER, MembershipRole.EDITOR, MembershipRole.ADMIN]
)
def test_only_the_owner_sees_or_confirms(
    auth_client: TestClient,
    make_user,
    make_membership,
    project: str,
    in_review,
    role,
    session,
) -> None:
    _, revision = in_review
    member = make_user()
    make_membership(user=member, project_id=project, role=role)
    csrf = _login(auth_client, member).json()["csrf_token"]

    assert (
        auth_client.get(f"/api/v1/auth/step-up/approvals/{revision.id}").status_code
        == 404
    )
    response = auth_client.post(
        "/api/v1/auth/step-up/approvals",
        json={"revision_id": str(revision.id), "password": TEST_PASSWORD},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 404
    session.expire_all()
    assert session.scalars(select(ApprovalGrant)).all() == []


def test_a_confirmation_issues_one_short_lived_grant(
    auth_client: TestClient, session: Session, owner: User, in_review
) -> None:
    item, revision = in_review
    csrf = _login(auth_client, owner).json()["csrf_token"]

    response = auth_client.post(
        "/api/v1/auth/step-up/approvals",
        json={"revision_id": str(revision.id), "password": TEST_PASSWORD},
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 201, response.text
    session.expire_all()
    grant = session.scalars(select(ApprovalGrant)).one()
    assert grant.user_id == owner.id
    assert grant.revision_id == revision.id
    assert grant.content_hash == revision.content_hash
    assert grant.consumed_at is None
    assert 0 < (grant.expires_at - grant.created_at).total_seconds() <= 300

    confirmed = session.scalars(
        select(AuditLog).where(AuditLog.action == "content.approval_confirmed")
    ).one()
    assert confirmed.written_by == "ai_smm_auth"
    assert confirmed.subject == f"content_item:{item.id}"


@pytest.mark.parametrize("problem", ["password", "csrf", "no_session"])
def test_a_confirmation_needs_session_csrf_and_password(
    auth_client: TestClient, session: Session, owner: User, in_review, problem: str
) -> None:
    _, revision = in_review
    csrf = _login(auth_client, owner).json()["csrf_token"]
    headers = {"X-CSRF-Token": csrf}
    password = TEST_PASSWORD

    if problem == "password":
        password = "not-the-password"
    elif problem == "csrf":
        headers = {}
    elif problem == "no_session":
        auth_client.cookies.clear()

    response = auth_client.post(
        "/api/v1/auth/step-up/approvals",
        json={"revision_id": str(revision.id), "password": password},
        headers=headers,
    )

    assert response.status_code in {401, 403}
    assert password not in response.text
    session.expire_all()
    assert session.scalars(select(ApprovalGrant)).all() == []


def test_a_revision_not_under_review_cannot_be_confirmed(
    auth_client: TestClient, session: Session, owner: User, make_content
) -> None:
    _, revision = make_content(user=owner)  # a draft
    csrf = _login(auth_client, owner).json()["csrf_token"]

    response = auth_client.post(
        "/api/v1/auth/step-up/approvals",
        json={"revision_id": str(revision.id), "password": TEST_PASSWORD},
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 409
    session.expire_all()
    assert session.scalars(select(ApprovalGrant)).all() == []


def test_the_role_reads_no_business_table_and_writes_nothing_directly(
    auth_factory: sessionmaker[Session],
) -> None:
    from sqlalchemy.exc import DBAPIError

    for statement in (
        "SELECT * FROM content_revisions",
        "SELECT * FROM approval_grants",
        "INSERT INTO approval_grants (id) VALUES (gen_random_uuid())",
        "INSERT INTO user_sessions (id) VALUES (gen_random_uuid())",
        "UPDATE users SET password_hash = 'x'",
        "SELECT public.guard_bind_session('x')",
    ):
        with auth_factory() as db, pytest.raises(DBAPIError) as raised:
            db.execute(text(statement))

        assert raised.value.orig.sqlstate == "42501", statement
