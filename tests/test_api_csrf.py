"""Unsafe, cookie-authenticated requests must carry the session's CSRF token."""
from __future__ import annotations

from ai_smm.api.csrf import CSRF_EXEMPT_PATHS, SAFE_METHODS
from ai_smm.db.models import MembershipRole
from tests.conftest import TEST_PASSWORD


def _patch(client, *, csrf: str | None, project: str):
    return _patch_at(client, csrf=csrf, project=project, version=0)


def _patch_at(client, *, csrf: str | None, project: str, version: int):
    headers = {"X-CSRF-Token": csrf} if csrf is not None else {}

    return client.patch(
        f"/api/v1/projects/{project}/settings",
        json={
            "expected_version": version,
            "content_config": {"tone": "calm"},
        },
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


def test_fetching_the_token_again_returns_the_same_one(
    client, login, member, project: str
) -> None:
    """Regression: a second GET /csrf used to retire the first token."""

    at_login = login(member(MembershipRole.ADMIN))

    first = client.get("/api/v1/auth/csrf").json()["csrf_token"]
    second = client.get("/api/v1/auth/csrf").json()["csrf_token"]

    assert at_login == first == second
    assert _patch(client, csrf=at_login, project=project).status_code == 200


def test_two_tabs_of_one_session_both_keep_working(
    client, login, member, project: str
) -> None:
    """Regression: one tab fetching a token must not break the other.

    Two tabs share the cookie jar, which is what a browser does, and each
    fetches its own token. Both tokens must stay usable, in either order
    and after the other tab has fetched again.
    """

    login(member(MembershipRole.ADMIN))

    tab_one = client.get("/api/v1/auth/csrf").json()["csrf_token"]
    tab_two = client.get("/api/v1/auth/csrf").json()["csrf_token"]

    assert tab_one == tab_two

    version = 0

    for token in (tab_one, tab_two, tab_one):
        response = _patch_at(
            client, csrf=token, project=project, version=version
        )

        assert response.status_code == 200, response.text

        version = response.json()["version"]

        # The other tab refetching changes nothing for this one.
        client.get("/api/v1/auth/csrf")

    assert version == 3


def test_a_token_from_an_earlier_session_stops_working(
    client, login, make_user, member, project: str
) -> None:
    """Stability must not outlive the session it belongs to."""

    user = member(MembershipRole.ADMIN)
    first_session_token = login(user)

    client.post(
        "/api/v1/auth/logout", headers={"X-CSRF-Token": first_session_token}
    )

    second_session_token = login(user)

    assert second_session_token != first_session_token

    stale = _patch(client, csrf=first_session_token, project=project)

    assert stale.status_code == 403
    assert stale.json()["error"]["code"] == "CSRF_INVALID"
    assert (
        _patch(client, csrf=second_session_token, project=project).status_code
        == 200
    )


def test_a_revoked_session_invalidates_its_csrf_token(
    client, session, login, member, project: str
) -> None:
    """Revoking the session is what retires the token."""

    from datetime import datetime, timezone

    from sqlalchemy import select

    from ai_smm.db.models import UserSession

    csrf = login(member(MembershipRole.ADMIN))

    session.expire_all()
    row = session.scalars(select(UserSession)).one()
    row.revoked_at = datetime.now(timezone.utc)
    session.commit()

    response = _patch(client, csrf=csrf, project=project)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_REQUIRED"


def test_the_csrf_token_is_not_the_session_token(
    client, login, make_user
) -> None:
    """It is derived one way, so holding it must not grant the session."""

    from ai_smm.application.auth import SESSION_COOKIE_NAME

    csrf = login(make_user())
    session_token = client.cookies[SESSION_COOKIE_NAME]

    assert csrf != session_token
    assert session_token not in csrf
    assert csrf not in session_token

    # Presenting the CSRF token as the session cookie authenticates nobody.
    client.cookies.set(SESSION_COOKIE_NAME, csrf)

    assert client.get("/api/v1/auth/me").status_code == 401


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
