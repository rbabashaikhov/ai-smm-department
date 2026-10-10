"""A 2xx must mean "committed".

The request session is a dependency with yield. FastAPI runs the exit code
of such a dependency after the response has been sent unless it is scoped
to the endpoint function, so the commit could land after the client had
already read a success: a browser that logged in and immediately asked
GET /auth/me was refused with 401 because its session row did not exist
yet, and a commit that failed at that point (the deferred revision key,
for instance) would have reached the client as a success.

These tests look at the database from a separate connection at the moment
the last byte of the response is handed to the server. TestClient waits
for the whole application to finish, so an ordinary assertion after the
call cannot see the ordering; this probe can.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from ai_smm.db.models import ContentItem, MembershipRole, UserSession
from tests.conftest import TEST_PASSWORD


class CommitProbe:
    """Run `check` when a response finishes, and keep what it saw."""

    def __init__(self, app: Any, check: Callable[[], int]) -> None:
        self.app = app
        self.check = check
        self.seen: dict[str, int] = {}

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def probing_send(message) -> None:
            if message["type"] == "http.response.body" and not message.get(
                "more_body", False
            ):
                self.seen[f"{scope['method']} {scope['path']}"] = self.check()

            await send(message)

        await self.app(scope, receive, probing_send)


def _count(factory: sessionmaker[Session], statement) -> int:
    with factory() as other:
        return other.scalar(statement) or 0


def test_login_session_is_committed_before_the_response_is_sent(
    api_app, session_factory, make_user
) -> None:
    user = make_user()
    probe = CommitProbe(
        api_app,
        lambda: _count(
            session_factory,
            select(func.count())
            .select_from(UserSession)
            .where(UserSession.user_id == user.id),
        ),
    )

    with TestClient(probe) as client:
        response = client.post(
            "/api/v1/auth/login",
            json={"email": user.email, "password": TEST_PASSWORD},
        )
        assert response.status_code == 200

        # What a browser does next, on the same session.
        assert client.get("/api/v1/auth/me").status_code == 200

    assert probe.seen["POST /api/v1/auth/login"] == 1


def test_a_command_is_committed_before_the_response_is_sent(
    api_app, session_factory, member, project: str
) -> None:
    user = member(MembershipRole.EDITOR)
    probe = CommitProbe(
        api_app,
        lambda: _count(
            session_factory,
            select(func.count())
            .select_from(ContentItem)
            .where(ContentItem.project_id == project),
        ),
    )

    with TestClient(probe) as client:
        csrf = client.post(
            "/api/v1/auth/login",
            json={"email": user.email, "password": TEST_PASSWORD},
        ).json()["csrf_token"]

        response = client.post(
            f"/api/v1/projects/{project}/content-items",
            headers={"X-CSRF-Token": csrf},
            json={
                "title": "Committed first",
                "revision": {"body": "Text", "format": "text"},
            },
        )

    assert response.status_code == 201
    assert probe.seen[f"POST /api/v1/projects/{project}/content-items"] == 1
