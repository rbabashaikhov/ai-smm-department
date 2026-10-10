"""Unsafe, cookie-authenticated requests must carry the session's CSRF token."""
from __future__ import annotations

from ai_smm.api.csrf import CSRF_EXEMPT_PATHS, SAFE_METHODS
from ai_smm.db.models import MembershipRole
from tests.conftest import TEST_PASSWORD


def _patch(client, *, csrf: str | None, project: str):
    headers = {"X-CSRF-Token": csrf} if csrf is not None else {}

    return client.patch(
        f"/api/v1/projects/{project}/settings",
        json={"expected_version": 0, "content_config": {"tone": "calm"}},
        headers=headers,
    )


def test_missing_csrf_token_is_rejected(
    client, login, member, project: str
) -> None:
    login(member(MembershipRole.ADMIN))

    response = _patch(client, csrf=None, project=project)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CSRF_REQUIRED"


def test_wrong_csrf_token_is_rejected(
    client, login, member, project: str
) -> None:
    login(member(MembershipRole.ADMIN))

    response = _patch(client, csrf="not-the-token", project=project)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CSRF_INVALID"


def test_correct_csrf_token_is_accepted(
    client, login, member, project: str
) -> None:
    csrf = login(member(MembershipRole.ADMIN))

    response = _patch(client, csrf=csrf, project=project)

    assert response.status_code == 200
    assert response.json()["content_config"] == {"tone": "calm"}


def test_another_sessions_csrf_token_is_rejected(
    client, login, member, project: str, make_user, make_membership
) -> None:
    """A token is bound to one session, not merely to a valid shape."""

    stranger = make_user(email="stranger@example.com")
    make_membership(
        user=stranger, project_id=project, role=MembershipRole.ADMIN
    )

    stranger_csrf = login(stranger)
    client.cookies.clear()

    login(member(MembershipRole.ADMIN))

    response = _patch(client, csrf=stranger_csrf, project=project)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CSRF_INVALID"


def test_the_csrf_endpoint_requires_a_session(client) -> None:
    response = client.get("/api/v1/auth/csrf")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_REQUIRED"


def test_the_csrf_endpoint_returns_a_usable_token(
    client, login, member, project: str
) -> None:
    login(member(MembershipRole.ADMIN))

    issued = client.get("/api/v1/auth/csrf")

    assert issued.status_code == 200

    body = issued.json()

    assert body["header_name"] == "X-CSRF-Token"
    assert _patch(client, csrf=body["csrf_token"], project=project).status_code == 200


def test_issuing_a_new_token_retires_the_previous_one(
    client, login, member, project: str
) -> None:
    old = login(member(MembershipRole.ADMIN))
    client.get("/api/v1/auth/csrf")

    response = _patch(client, csrf=old, project=project)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CSRF_INVALID"


def test_an_unsafe_request_without_a_session_is_401_not_403(
    client, project: str
) -> None:
    """Authentication is reported before CSRF: there is no session to bind to."""

    response = _patch(client, csrf=None, project=project)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_REQUIRED"


def test_safe_methods_need_no_token(client, login, member, project: str) -> None:
    login(member(MembershipRole.VIEWER))

    assert client.get("/api/v1/auth/me").status_code == 200
    assert client.get(f"/api/v1/projects/{project}").status_code == 200
    assert (
        client.get(f"/api/v1/projects/{project}/settings").status_code == 200
    )


def test_login_is_the_only_exempt_path(client, make_user) -> None:
    """Login cannot present a token, and no other route is excused."""

    assert set(CSRF_EXEMPT_PATHS) == {"/api/v1/auth/login"}
    assert "POST" not in SAFE_METHODS

    user = make_user()
    response = client.post(
        "/api/v1/auth/login",
        json={"email": user.email, "password": TEST_PASSWORD},
    )

    assert response.status_code == 200


def test_logout_needs_the_csrf_token(client, login, make_user) -> None:
    login(make_user())

    missing = client.post("/api/v1/auth/logout")

    assert missing.status_code == 403
    assert missing.json()["error"]["code"] == "CSRF_REQUIRED"

    # Still signed in: the rejected request changed nothing.
    assert client.get("/api/v1/auth/me").status_code == 200
