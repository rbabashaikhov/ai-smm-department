"""Social accounts and the write-only credentials manager (SMM-025B B1).

The token values are random test strings. Every connection test goes to
FakeIdentityService; nothing reaches a platform.
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_smm.db.models import (
    AuditLog,
    MembershipRole,
    SocialAccountCredential,
)
from tests.conftest import fake_token


def _csrf(token: str) -> dict[str, str]:
    return {"X-CSRF-Token": token}


@pytest.fixture
def accounts_app(api_settings, api_session_factory, credential_keys, identity_service):
    from ai_smm.api.app import create_app

    return create_app(
        replace(api_settings, credentials_public_key=credential_keys.public_b64),
        session_factory=api_session_factory,
        access_log=False,
        identity_fetcher=identity_service,
    )


@pytest.fixture
def api(accounts_app):
    with TestClient(accounts_app) as test_client:
        yield test_client


@pytest.fixture
def sign_in(api, session: Session):
    from tests.conftest import TEST_PASSWORD

    def do(user) -> str:
        response = api.post(
            "/api/v1/auth/login", json={"email": user.email, "password": TEST_PASSWORD}
        )
        assert response.status_code == 200
        session.expire_all()

        return response.json()["csrf_token"]

    return do


@pytest.fixture
def owner(member):
    return member(MembershipRole.OWNER)


def _new_account(api, csrf, name="Личный блог", project="test-project"):
    response = api.post(
        f"/api/v1/projects/{project}/social-accounts",
        json={"platform": "threads", "display_name": name},
        headers=_csrf(csrf),
    )
    assert response.status_code == 201, response.text

    return response.json()


def _put_token(api, csrf, account_id, token, **extra):
    return api.post(
        f"/api/v1/social-accounts/{account_id}/credentials",
        json={"access_token": token, **extra},
        headers=_csrf(csrf),
    )


# -- accounts -----------------------------------------------------------------


def test_an_owner_adds_several_accounts_of_one_platform(api, sign_in, owner) -> None:
    csrf = sign_in(owner)

    first = _new_account(api, csrf, "Блог")
    second = _new_account(api, csrf, "Эксперименты")
    listed = api.get("/api/v1/projects/test-project/social-accounts").json()

    assert [a["id"] for a in listed] == [first["id"], second["id"]]
    assert all(a["platform"] == "threads" for a in listed)
    assert all(a["publishing_ready"] is False for a in listed)
    assert listed[0]["not_ready_reason"] == "account_identity_unknown"
    assert listed[0]["credential"] is None


@pytest.mark.parametrize(
    "role", [MembershipRole.VIEWER, MembershipRole.EDITOR, MembershipRole.ADMIN]
)
def test_only_the_owner_manages_accounts_but_members_can_read(
    api, sign_in, owner, member, role
) -> None:
    account = _new_account(api, sign_in(owner))
    csrf = sign_in(member(role))

    assert api.get("/api/v1/projects/test-project/social-accounts").status_code == 200
    assert api.get(f"/api/v1/social-accounts/{account['id']}").status_code == 200

    forbidden = [
        api.post(
            "/api/v1/projects/test-project/social-accounts",
            json={"display_name": "x"},
            headers=_csrf(csrf),
        ),
        api.patch(
            f"/api/v1/social-accounts/{account['id']}",
            json={"expected_version": 1, "display_name": "x"},
            headers=_csrf(csrf),
        ),
        api.post(
            f"/api/v1/social-accounts/{account['id']}/disable",
            json={"expected_version": 1},
            headers=_csrf(csrf),
        ),
        _put_token(api, csrf, account["id"], fake_token()),
    ]

    assert [r.status_code for r in forbidden] == [403] * 4


def test_accounts_of_another_project_are_invisible(
    api, sign_in, owner, make_user, make_membership, make_other_project
) -> None:
    account = _new_account(api, sign_in(owner))
    stranger = make_user()
    make_membership(
        user=stranger, project_id=make_other_project, role=MembershipRole.OWNER
    )
    csrf = sign_in(stranger)

    assert api.get("/api/v1/projects/test-project/social-accounts").status_code == 404
    assert api.get(f"/api/v1/social-accounts/{account['id']}").status_code == 404
    assert _put_token(api, csrf, account["id"], fake_token()).status_code == 404
    assert (
        api.post(
            f"/api/v1/social-accounts/{account['id']}/disable",
            json={"expected_version": 1},
            headers=_csrf(csrf),
        ).status_code
        == 404
    )


def test_rename_disable_enable_with_versions(api, sign_in, owner) -> None:
    csrf = sign_in(owner)
    account = _new_account(api, csrf)

    renamed = api.patch(
        f"/api/v1/social-accounts/{account['id']}",
        json={"expected_version": 1, "display_name": "Новое имя"},
        headers=_csrf(csrf),
    )
    assert renamed.status_code == 200
    assert renamed.json()["version"] == 2

    stale = api.post(
        f"/api/v1/social-accounts/{account['id']}/disable",
        json={"expected_version": 1},
        headers=_csrf(csrf),
    )
    assert stale.status_code == 409

    disabled = api.post(
        f"/api/v1/social-accounts/{account['id']}/disable",
        json={"expected_version": 2},
        headers=_csrf(csrf),
    )
    assert disabled.json()["status"] == "disabled"
    assert disabled.json()["not_ready_reason"] == "account_disabled"

    enabled = api.post(
        f"/api/v1/social-accounts/{account['id']}/enable",
        json={"expected_version": 3},
        headers=_csrf(csrf),
    )
    assert enabled.json()["status"] == "active"


def test_disabling_reports_the_rows_that_will_not_be_published(
    api, sign_in, owner, make_account, make_publication
) -> None:
    account, _ = make_account()
    make_publication(social_account_id=account.id)

    response = api.post(
        f"/api/v1/social-accounts/{account.id}/disable",
        json={"expected_version": 1},
        headers=_csrf(sign_in(owner)),
    )

    assert response.json()["scheduled_publications"] == 1


# -- credentials --------------------------------------------------------------


def test_a_verified_token_binds_the_account_and_is_never_returned(
    api, sign_in, owner, identity_service, session: Session
) -> None:
    csrf = sign_in(owner)
    account = _new_account(api, csrf)
    token = fake_token("bind")
    identity_service.register(token, "1789", "travel_ru")

    response = _put_token(
        api, csrf, account["id"], token, expires_at="2026-12-12T00:00:00+00:00"
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "active"
    assert body["verification_status"] == "verified"
    assert len(body["fingerprint"]) == 12
    assert token not in response.text

    detail = api.get(f"/api/v1/social-accounts/{account['id']}")
    assert detail.json()["external_account_id"] == "1789"
    assert detail.json()["external_username"] == "travel_ru"
    assert detail.json()["publishing_ready"] is True
    assert token not in detail.text

    session.expire_all()
    stored = session.scalars(select(SocialAccountCredential)).one()
    assert token.encode() not in bytes(stored.ciphertext)
    assert stored.key_id


def test_a_replacement_supersedes_and_wipes_the_previous_secret(
    api, sign_in, owner, identity_service, session: Session
) -> None:
    csrf = sign_in(owner)
    account = _new_account(api, csrf)
    first, second = fake_token("one"), fake_token("two")
    identity_service.register(first, "1789")
    identity_service.register(second, "1789")

    old = _put_token(api, csrf, account["id"], first).json()
    new = _put_token(api, csrf, account["id"], second).json()

    session.expire_all()
    rows = {str(r.id): r for r in session.scalars(select(SocialAccountCredential))}

    assert rows[old["id"]].status == "superseded"
    assert rows[old["id"]].ciphertext is None
    assert rows[old["id"]].nonce is None
    assert rows[new["id"]].status == "active"

    history = api.get(f"/api/v1/social-accounts/{account['id']}").json()["credentials"]
    assert [c["status"] for c in history] == ["active", "superseded"]


def test_a_token_of_another_identity_is_refused_and_nothing_changes(
    api, sign_in, owner, identity_service, session: Session
) -> None:
    csrf = sign_in(owner)
    account = _new_account(api, csrf)
    right, wrong = fake_token("right"), fake_token("wrong")
    identity_service.register(right, "1789")
    identity_service.register(wrong, "4242")
    kept = _put_token(api, csrf, account["id"], right).json()

    response = _put_token(api, csrf, account["id"], wrong)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CREDENTIAL_ACCOUNT_MISMATCH"
    assert wrong not in response.text
    session.expire_all()
    active = session.scalars(
        select(SocialAccountCredential).where(
            SocialAccountCredential.status == "active"
        )
    ).one()
    assert str(active.id) == kept["id"]


def test_one_identity_cannot_be_connected_twice_in_a_project(
    api, sign_in, owner, identity_service
) -> None:
    csrf = sign_in(owner)
    first = _new_account(api, csrf, "A")
    second = _new_account(api, csrf, "B")
    token_a, token_b = fake_token("a"), fake_token("b")
    identity_service.register(token_a, "1789")
    identity_service.register(token_b, "1789")
    _put_token(api, csrf, first["id"], token_a)

    response = _put_token(api, csrf, second["id"], token_b)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ACCOUNT_ALREADY_CONNECTED"


def test_a_rejected_token_or_an_unreachable_platform_stores_nothing(
    api, sign_in, owner, identity_service, session: Session
) -> None:
    csrf = sign_in(owner)
    account = _new_account(api, csrf)
    token = fake_token("x")

    rejected = _put_token(api, csrf, account["id"], token)
    identity_service.unavailable = True
    unreachable = _put_token(api, csrf, account["id"], token)

    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "CREDENTIAL_REJECTED"
    assert unreachable.status_code == 502
    for response in (rejected, unreachable):
        assert token not in response.text
    session.expire_all()
    assert session.scalars(select(SocialAccountCredential)).all() == []


def test_an_unverified_token_is_stored_but_does_not_make_the_account_publishable(
    api, sign_in, owner, identity_service
) -> None:
    csrf = sign_in(owner)
    account = _new_account(api, csrf)

    response = _put_token(api, csrf, account["id"], fake_token(), verify=False)

    assert response.status_code == 201
    assert response.json()["verification_status"] == "unverified"
    assert identity_service.calls == []
    detail = api.get(f"/api/v1/social-accounts/{account['id']}").json()
    assert detail["publishing_ready"] is False
    assert detail["not_ready_reason"] == "account_identity_unknown"


def test_revoking_destroys_the_ciphertext(
    api, sign_in, owner, identity_service, session: Session
) -> None:
    csrf = sign_in(owner)
    account = _new_account(api, csrf)
    token = fake_token()
    identity_service.register(token, "1789")
    credential = _put_token(api, csrf, account["id"], token).json()

    revoked = api.post(
        f"/api/v1/social-account-credentials/{credential['id']}/revoke",
        headers=_csrf(csrf),
    )
    again = api.post(
        f"/api/v1/social-account-credentials/{credential['id']}/revoke",
        headers=_csrf(csrf),
    )

    assert revoked.status_code == 200
    assert revoked.json()["status"] == "revoked"
    assert again.status_code == 409
    session.expire_all()
    row = session.get(SocialAccountCredential, credential["id"])
    assert (row.ciphertext, row.nonce, row.ephemeral_public_key) == (None, None, None)
    assert (
        api.get(f"/api/v1/social-accounts/{account['id']}").json()["not_ready_reason"]
        == "no_active_credential"
    )


def test_the_database_refuses_key_material_on_a_retired_credential(
    session: Session, make_account
) -> None:
    from sqlalchemy.exc import IntegrityError

    make_account()
    row = session.scalars(select(SocialAccountCredential)).one()
    row.status = "revoked"

    with pytest.raises(IntegrityError, match="ck_social_account_credentials"):
        session.flush()

    session.rollback()


def test_a_second_active_credential_is_refused_by_the_database(
    session: Session, make_account
) -> None:
    from sqlalchemy.exc import IntegrityError

    account, _ = make_account()
    existing = session.scalars(select(SocialAccountCredential)).one()
    session.add(
        SocialAccountCredential(
            social_account_id=account.id,
            status="active",
            scheme=existing.scheme,
            key_id=existing.key_id,
            ephemeral_public_key=b"x" * 32,
            nonce=b"x" * 12,
            ciphertext=b"x" * 40,
            fingerprint="0" * 12,
        )
    )

    with pytest.raises(
        IntegrityError, match="uq_social_account_credentials_one_active"
    ):
        session.flush()

    session.rollback()


def test_no_public_key_means_nothing_is_stored(
    api_settings, api_session_factory, owner, identity_service, session: Session
) -> None:
    from ai_smm.api.app import create_app
    from tests.conftest import TEST_PASSWORD

    app = create_app(
        api_settings,
        session_factory=api_session_factory,
        access_log=False,
        identity_fetcher=identity_service,
    )

    with TestClient(app) as client:
        csrf = client.post(
            "/api/v1/auth/login", json={"email": owner.email, "password": TEST_PASSWORD}
        ).json()["csrf_token"]
        account = _new_account(client, csrf)
        response = _put_token(client, csrf, account["id"], fake_token())

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "CREDENTIALS_NOT_CONFIGURED"
    session.expire_all()
    assert session.scalars(select(SocialAccountCredential)).all() == []


@pytest.mark.parametrize(
    "token", ["abc12", "has a space in the middle of it", "x" * 5000]
)
def test_a_malformed_token_is_refused_without_echoing_it(
    api, sign_in, owner, token
) -> None:
    csrf = sign_in(owner)
    account = _new_account(api, csrf)

    response = _put_token(api, csrf, account["id"], token)

    assert response.status_code == 422
    assert token not in response.text


# -- leaks ---------------------------------------------------------------------


def test_no_response_schema_can_carry_a_secret(accounts_app) -> None:
    """Walk every response model of the API for a secret-shaped field."""

    forbidden = {
        "access_token",
        "token",
        "ciphertext",
        "nonce",
        "ephemeral_public_key",
        "password",
        "password_hash",
    }
    schema = accounts_app.openapi()
    request_models = set()

    for path in schema["paths"].values():
        for operation in path.values():
            body = (
                operation.get("requestBody", {})
                .get("content", {})
                .get("application/json", {})
            )
            ref = body.get("schema", {}).get("$ref")
            if ref:
                request_models.add(ref.rsplit("/", 1)[-1])

    for name, model in schema["components"]["schemas"].items():
        if name in request_models:
            continue

        assert not (set(model.get("properties", {})) & forbidden), name


def test_the_token_never_reaches_audit_or_logs(
    api, sign_in, owner, identity_service, session: Session, caplog
) -> None:
    csrf = sign_in(owner)
    account = _new_account(api, csrf)
    token = fake_token("sentinel")
    identity_service.register(token, "1789")

    with caplog.at_level(logging.DEBUG):
        credential = _put_token(api, csrf, account["id"], token).json()
        api.post(
            f"/api/v1/social-account-credentials/{credential['id']}/revoke",
            headers=_csrf(csrf),
        )
        _put_token(api, csrf, account["id"], "short")

    session.expire_all()
    audit = session.scalars(select(AuditLog)).all()

    assert token not in caplog.text
    assert all(
        token not in json.dumps(row.details, ensure_ascii=False) for row in audit
    )
    assert any(row.action == "social_account.credential_set" for row in audit)


def test_a_decrypted_token_is_masked_in_worker_logs(caplog) -> None:
    from ai_smm.logging_setup import get_logger, register_runtime_secret

    token = fake_token("runtime")
    register_runtime_secret(token)
    logger = get_logger("tests.redaction")

    with caplog.at_level(logging.INFO):
        logger.info("upstream said %s", token)

    assert token not in caplog.text
    assert "REDACTED" in caplog.text
