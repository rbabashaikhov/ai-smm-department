"""The API's business-command suites, re-run connected as ai_smm_cc_rw.

Every module here star-imports one suite that sends business commands
with mutations ON. This conftest switches only the API's connection to
the mutation role (SMM-025C, FU-025B-1); fixtures keep seeding data as the
owner, as the worker and the CLI do in production. Passing here is the
proof that the role's grants -- and the database guards of SMM-025D
(ai_smm.db.guards) -- allow every command the API makes;
tests/test_db_mutation_role.py and tests/test_db_guards.py are the proof
that they allow nothing more.

Approvals. Under the guards, an approval by this role consumes a step-up
grant that only smm-auth issues, after re-checking the owner's password
(tests/test_auth_service.py drives that path for real). These suites test
the business commands, not the password prompt, so the client below plays
smm-auth's part: right before each approve request it issues the grant,
as the owner role, for the session in the client's cookie. Nothing else
is changed, so a missing or wrong grant would still be refused.

Sign-in. In mutation mode smm-api serves no login: smm-auth does, through
SECURITY DEFINER functions, and the guards refuse a business role that
writes sign-in audit itself. The `login` fixture below therefore opens the
session the way smm-auth does -- the password checked, the session row and
auth.login written by a role allowed to -- and hands the client the cookie.
The login endpoint's own behaviour is tested in tests/test_api_auth.py
(owner role) and tests/test_auth_service.py (smm-auth).

Skipped without AI_SMM_TEST_ADMIN_DATABASE_URL, like the role itself.
"""
from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ai_smm.application.auth import (
    SESSION_COOKIE_NAME,
    create_session,
    csrf_token_for,
)
from ai_smm.config import Settings
from ai_smm.db.models import (
    ApprovalGrant,
    ContentRevision,
    SocialAccount,
    User,
    UserSession,
)
from ai_smm.queue import record_audit
from ai_smm.security.passwords import verify_password
from ai_smm.security.tokens import hash_token
from tests.conftest import TEST_PASSWORD


_APPROVE = re.compile(r"^/api/v1/content-items/([0-9a-fA-F-]{36})/approve$")


@pytest.fixture
def api_session_factory(
    mutation_session_factory: sessionmaker[Session],
) -> sessionmaker[Session]:
    return mutation_session_factory


def issue_test_grant(
    factory: sessionmaker[Session], *, token: str | None, item_id: str, revision_id: str
) -> None:
    """What smm_auth.approval_grant_issue writes, as the owner role."""

    if not token:
        return

    try:
        item_uuid = uuid.UUID(item_id)
        revision_uuid = uuid.UUID(revision_id)
    except (TypeError, ValueError):
        return

    with factory() as db:
        user_session = db.scalars(
            select(UserSession).where(UserSession.token_hash == hash_token(token))
        ).one_or_none()
        revision = db.get(ContentRevision, revision_uuid)

        if (
            user_session is None
            or revision is None
            or revision.content_item_id != item_uuid
        ):
            # The request is meant to fail; let the API and the guards say how.
            return

        account = (
            db.get(SocialAccount, revision.social_account_id)
            if revision.social_account_id
            else None
        )
        now = datetime.now(timezone.utc)
        db.add(
            ApprovalGrant(
                id=uuid.uuid4(),
                user_id=user_session.user_id,
                session_id=user_session.id,
                content_item_id=item_uuid,
                revision_id=revision_uuid,
                social_account_id=revision.social_account_id,
                external_account_id=account.external_account_id if account else None,
                content_hash=revision.content_hash,
                created_at=now,
                expires_at=now + timedelta(minutes=5),
            )
        )
        db.commit()


@pytest.fixture
def client(api_app, session_factory: sessionmaker[Session]) -> Iterator[Any]:
    from fastapi.testclient import TestClient

    class StepUpClient(TestClient):
        def post(self, url, *args, **kwargs):  # type: ignore[override]
            match = _APPROVE.match(str(url))
            body = kwargs.get("json")

            if match and isinstance(body, dict):
                issue_test_grant(
                    session_factory,
                    token=self.cookies.get(SESSION_COOKIE_NAME),
                    item_id=match.group(1),
                    revision_id=str(body.get("revision_id")),
                )

            return super().post(url, *args, **kwargs)

    with StepUpClient(api_app) as test_client:
        yield test_client


def _open_session(
    factory: sessionmaker[Session], settings: Settings, user: User, password: str
) -> str:
    """The session smm-auth opens: password checked, row and auth.login
    written by a role allowed to write them. Returns the raw token."""

    with factory() as db:
        stored = db.get(User, user.id)

        assert stored is not None and stored.is_active
        assert verify_password(stored.password_hash, password)

        user_session, token = create_session(db, user=stored, settings=settings)
        record_audit(
            db,
            actor=f"user:{stored.id}",
            action="auth.login",
            subject=f"user:{stored.id}",
            details={"session_id": str(user_session.id)},
        )
        db.commit()

    return token


@pytest.fixture
def open_session(session_factory: sessionmaker[Session], api_settings: Settings):
    """Sign in as smm-auth does, and hand the test client the cookie."""

    def do(test_client, user: User, password: str = TEST_PASSWORD) -> str:
        token = _open_session(session_factory, api_settings, user, password)
        test_client.cookies.set(SESSION_COOKIE_NAME, token)

        return csrf_token_for(token)

    return do
