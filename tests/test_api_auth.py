"""Login, logout, /me and the lifetime of a server-side session."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_smm.application.auth import SESSION_COOKIE_NAME
from ai_smm.db.models import AuditLog, MembershipRole, UserSession
from ai_smm.security.tokens import hash_token
from tests.conftest import TEST_PASSWORD


def _session_rows(session: Session) -> list[UserSession]:
    return list(session.scalars(select(UserSession)).all())


def _audit_actions(session: Session) -> list[str]:
    return [
        entry.action
        for entry in session.scalars(
            select(AuditLog).order_by(AuditLog.id)
        ).all()
    ]


def test_login_sets_an_httponly_session_cookie(client, make_user) -> None:
    user = make_user(email="Owner@Example.COM".lower())

    response = client.post(
        "/api/v1/auth/login",
        json={"email": user.email, "password": TEST_PASSWORD},
    )

    assert response.status_code == 200

    body = response.json()

    assert body["user"]["email"] == user.email
    assert body["csrf_token"]
    assert "password_hash" not in body["user"]

    cookie = response.headers["set-cookie"]
    attributes = cookie.lower()

    assert f"{SESSION_COOKIE_NAME}=" in cookie
    assert "httponly" in attributes
    assert "samesite=lax" in attributes
    assert "path=/" in attributes
    # The test client speaks http, so this app has Secure switched off;
    # the production default is covered by its own test below.
    assert "secure" not in attributes


def test_the_cookie_is_secure_when_configured(
    settings, session_factory, make_user
) -> None:
    """The default (AI_SMM_API_COOKIE_SECURE unset) is Secure=true."""

    from dataclasses import replace

    from fastapi.testclient import TestClient

    from ai_smm.api.app import create_app

    assert settings.api_cookie_secure is True

    app = create_app(
        replace(settings, api_cookie_secure=True),
        session_factory=session_factory,
        access_log=False,
    )
    user = make_user()

    with TestClient(app) as secure_client:
        response = secure_client.post(
            "/api/v1/auth/login",
            json={"email": user.email, "password": TEST_PASSWORD},
        )

    assert response.status_code == 200
    assert "secure" in response.headers["set-cookie"].lower()


def test_login_normalizes_the_email(client, make_user) -> None:
    make_user(email="operator@example.com")

    response = client.post(
        "/api/v1/auth/login",
        json={"email": "  OPERATOR@Example.com  ", "password": TEST_PASSWORD},
    )

    assert response.status_code == 200


def test_login_stores_only_a_hash_of_the_session_token(
    client, session: Session, make_user
) -> None:
    user = make_user()

    response = client.post(
        "/api/v1/auth/login",
        json={"email": user.email, "password": TEST_PASSWORD},
    )
    raw_token = client.cookies[SESSION_COOKIE_NAME]

    assert response.status_code == 200

    session.expire_all()
    rows = _session_rows(session)

    assert len(rows) == 1
    assert rows[0].token_hash == hash_token(raw_token)
    assert raw_token not in rows[0].token_hash
    # The raw token must appear in no column of the row.
    stored_values = [
        getattr(rows[0], column.name)
        for column in rows[0].__table__.columns
    ]

    assert raw_token not in str(stored_values)


def test_login_records_last_login_and_an_audit_entry(
    client, session: Session, make_user
) -> None:
    user = make_user()

    client.post(
        "/api/v1/auth/login",
        json={"email": user.email, "password": TEST_PASSWORD},
    )

    session.expire_all()
    session.refresh(user)

    assert user.last_login_at is not None
    assert "auth.login" in _audit_actions(session)

    entry = session.scalars(
        select(AuditLog).where(AuditLog.action == "auth.login")
    ).one()

    assert entry.actor == f"user:{user.id}"
    # No credential of any kind in the audit details.
    assert TEST_PASSWORD not in str(entry.details)


def test_wrong_password_is_rejected_generically(client, make_user) -> None:
    user = make_user()

    response = client.post(
        "/api/v1/auth/login",
        json={"email": user.email, "password": "not-the-password"},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"
    assert SESSION_COOKIE_NAME not in response.headers.get("set-cookie", "")


def test_unknown_address_is_indistinguishable_from_a_wrong_password(
    client, make_user
) -> None:
    """Account enumeration: the two answers must be identical."""

    user = make_user()

    wrong_password = client.post(
        "/api/v1/auth/login",
        json={"email": user.email, "password": "not-the-password"},
    )
    unknown_user = client.post(
        "/api/v1/auth/login",
        json={"email": "nobody@example.com", "password": TEST_PASSWORD},
    )
    malformed = client.post(
        "/api/v1/auth/login",
        json={"email": "not-an-address", "password": TEST_PASSWORD},
    )

    assert (
        wrong_password.status_code
        == unknown_user.status_code
        == malformed.status_code
        == 401
    )

    def message(response) -> tuple[str, str]:
        error = response.json()["error"]

        return error["code"], error["message"]

    assert message(wrong_password) == message(unknown_user)
    assert message(wrong_password) == message(malformed)


def test_inactive_user_cannot_log_in(client, make_user) -> None:
    user = make_user(is_active=False)

    response = client.post(
        "/api/v1/auth/login",
        json={"email": user.email, "password": TEST_PASSWORD},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"


def test_me_requires_authentication(client) -> None:
    response = client.get("/api/v1/auth/me")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_REQUIRED"
    assert response.json()["error"]["request_id"]


def test_me_returns_safe_fields_and_memberships(
    client, login, member, project: str
) -> None:
    user = member(MembershipRole.EDITOR, display_name="Ira Editor")
    login(user)

    response = client.get("/api/v1/auth/me")

    assert response.status_code == 200

    body = response.json()

    assert body["user"]["display_name"] == "Ira Editor"
    assert body["user"]["email"] == user.email
    assert set(body["user"]) == {
        "id",
        "email",
        "display_name",
        "is_active",
        "last_login_at",
        "created_at",
    }
    assert body["projects"] == [
        {
            "project_id": project,
            "display_name": "Test Project",
            "role": "editor",
        }
    ]


def test_logout_revokes_the_session_and_clears_the_cookie(
    client, session: Session, login, make_user
) -> None:
    user = make_user()
    csrf = login(user)

    response = client.post(
        "/api/v1/auth/logout", headers={"X-CSRF-Token": csrf}
    )

    assert response.status_code == 204

    session.expire_all()
    row = _session_rows(session)[0]

    assert row.revoked_at is not None
    assert "auth.logout" in _audit_actions(session)

    # The cookie is gone, and the token no longer authenticates.
    assert client.get("/api/v1/auth/me").status_code == 401


def test_logout_requires_authentication(client) -> None:
    response = client.post("/api/v1/auth/logout")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_REQUIRED"


def test_a_revoked_session_token_is_refused(
    client, session: Session, login, make_user
) -> None:
    user = make_user()
    login(user)

    session.expire_all()
    row = _session_rows(session)[0]
    row.revoked_at = datetime.now(timezone.utc)
    session.commit()

    assert client.get("/api/v1/auth/me").status_code == 401


def test_a_session_past_its_absolute_lifetime_is_refused(
    client, session: Session, login, make_user
) -> None:
    user = make_user()
    login(user)

    session.expire_all()
    row = _session_rows(session)[0]
    row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    session.commit()

    response = client.get("/api/v1/auth/me")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_REQUIRED"


def test_an_idle_session_is_refused_before_it_expires(
    client, session: Session, login, make_user, api_settings
) -> None:
    """Still inside its 7 days, but untouched for longer than the idle window."""

    user = make_user()
    login(user)

    session.expire_all()
    row = _session_rows(session)[0]
    row.last_seen_at = datetime.now(timezone.utc) - timedelta(
        seconds=api_settings.api_session_idle_seconds + 60
    )
    session.commit()

    assert row.expires_at > datetime.now(timezone.utc)
    assert client.get("/api/v1/auth/me").status_code == 401


def test_a_valid_session_is_accepted_and_keeps_being_touched(
    client, session: Session, login, make_user
) -> None:
    user = make_user()
    login(user)

    session.expire_all()
    before = _session_rows(session)[0].last_seen_at

    assert client.get("/api/v1/auth/me").status_code == 200

    session.expire_all()

    assert _session_rows(session)[0].last_seen_at >= before


def test_deactivating_a_user_invalidates_their_live_session(
    client, session: Session, login, make_user
) -> None:
    user = make_user()
    login(user)

    assert client.get("/api/v1/auth/me").status_code == 200

    session.refresh(user)
    user.is_active = False
    session.commit()

    assert client.get("/api/v1/auth/me").status_code == 401


def test_a_forged_cookie_value_is_refused(client, make_user) -> None:
    make_user()
    client.cookies.set(SESSION_COOKIE_NAME, "clearly-not-a-real-token")

    assert client.get("/api/v1/auth/me").status_code == 401


def test_each_login_opens_its_own_session(
    client, session: Session, make_user
) -> None:
    user = make_user()

    for _ in range(2):
        client.post(
            "/api/v1/auth/login",
            json={"email": user.email, "password": TEST_PASSWORD},
        )

    session.expire_all()

    rows = _session_rows(session)

    assert len(rows) == 2
    assert len({row.token_hash for row in rows}) == 2


def test_a_failed_login_is_audited_without_the_password(
    client, session: Session, make_user
) -> None:
    user = make_user()

    client.post(
        "/api/v1/auth/login",
        json={"email": user.email, "password": "not-the-password"},
    )

    session.expire_all()
    entry = session.scalars(
        select(AuditLog).where(AuditLog.action == "auth.login_failed")
    ).one()

    assert entry.subject == f"email:{user.email}"
    assert "not-the-password" not in str(entry.details)


def test_login_rejects_an_unknown_body_field(client, make_user) -> None:
    user = make_user()

    response = client.post(
        "/api/v1/auth/login",
        json={
            "email": user.email,
            "password": TEST_PASSWORD,
            "role": "owner",
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_a_validation_error_never_echoes_the_password(client) -> None:
    response = client.post(
        "/api/v1/auth/login", json={"password": "super-secret-value"}
    )

    assert response.status_code == 422
    assert "super-secret-value" not in response.text
