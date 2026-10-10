"""The architectural rules the API must keep: no publishing, no leaks.

The control plane edits the metadata around the queue. Execution belongs
to the worker, and these tests fail if an endpoint ever starts to publish.
"""
from __future__ import annotations

import io
import json
import logging
from pathlib import Path

import pytest

from ai_smm.api.app import create_app
from ai_smm.db.models import MembershipRole
from ai_smm.security.rbac import role_satisfies
from tests.conftest import TEST_PASSWORD


API_SOURCE_DIR = Path(__file__).resolve().parents[1] / "src" / "ai_smm" / "api"


def _paths(app) -> list[str]:
    return list(app.openapi()["paths"])


def test_the_api_exposes_exactly_the_planned_routes(api_app) -> None:
    assert sorted(_paths(api_app)) == sorted(
        [
            "/health/live",
            "/health/ready",
            "/api/v1/auth/login",
            "/api/v1/auth/logout",
            "/api/v1/auth/me",
            "/api/v1/auth/csrf",
            "/api/v1/projects",
            "/api/v1/projects/{project_id}",
            "/api/v1/projects/{project_id}/settings",
            # SMM-022B: reading the existing publishing core, plus the
            # three commands that move a row inside the queue.
            "/api/v1/projects/{project_id}/publications",
            "/api/v1/projects/{project_id}/series",
            "/api/v1/projects/{project_id}/operations/summary",
            "/api/v1/projects/{project_id}/operations/attention",
            "/api/v1/projects/{project_id}/audit",
            "/api/v1/publications/{publication_id}",
            "/api/v1/publications/{publication_id}/preview",
            "/api/v1/publications/{publication_id}/attempts",
            "/api/v1/publications/{publication_id}/schedule",
            "/api/v1/publications/{publication_id}/reschedule",
            "/api/v1/publications/{publication_id}/cancel",
            "/api/v1/series/{series_id}",
        ]
    )


def test_there_is_no_publish_endpoint(api_app) -> None:
    for path in _paths(api_app):
        assert "publish" not in path.lower()


@pytest.mark.parametrize(
    "path",
    [
        "/publish",
        "/api/v1/publish",
        "/threads/publish",
        "/api/v1/threads/publish",
        "/api/v1/publications/1/publish-now",
        "/api/v1/publications/1/publish",
        "/api/v1/projects/p/publish",
        "/api/v1/publications/1/retry",
    ],
)
def test_no_route_answers_a_publish_request(client, path: str) -> None:
    response = client.post(path, json={})

    assert response.status_code == 404


def test_the_api_package_never_imports_the_publishing_layer() -> None:
    """A publish must stay impossible to reach from an HTTP request.

    Checked on the import graph rather than on the text, so a comment
    that explains the rule does not trip the test that enforces it.
    """

    import ast

    offenders: list[str] = []

    for source in sorted(API_SOURCE_DIR.rglob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"))

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue

            for name in names:
                if name.startswith(("ai_smm.publishing", "ai_smm.worker")):
                    offenders.append(f"{source.name}: {name}")

    assert offenders == []


#: The only publishing-layer names the control plane may import. Both are
#: pure: the validator is a function of one row and the exception it
#: raises. Everything that can create a post is absent.
PUBLISHING_IMPORT_ALLOWLIST = frozenset(
    {"validate_ready_to_publish", "PublishBlocked"}
)

APPLICATION_SOURCE_DIR = (
    Path(__file__).resolve().parents[1] / "src" / "ai_smm" / "application"
)


def _import_edges(directory: Path):
    """(file, module, imported names) for every import in a package."""

    import ast

    for source in sorted(directory.rglob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"))

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    yield source.name, alias.name, ()
            elif isinstance(node, ast.ImportFrom):
                yield (
                    source.name,
                    node.module or "",
                    tuple(alias.name for alias in node.names),
                )


def test_the_control_plane_cannot_reach_anything_that_publishes() -> None:
    """The invariant, checked transitively through the application layer.

    SMM-022B gave the API a preview, which reuses the publishing layer's
    content validator. That is the one permitted edge, and it is pure.
    Nothing that can create a post -- the publisher, the publish
    pipeline, the worker -- may be imported by either package.
    """

    offenders: list[str] = []

    for directory in (API_SOURCE_DIR, APPLICATION_SOURCE_DIR):
        for filename, module, names in _import_edges(directory):
            if module.startswith("ai_smm.worker"):
                offenders.append(f"{filename}: imports {module}")

                continue

            if not module.startswith("ai_smm.publishing"):
                continue

            forbidden = set(names) - PUBLISHING_IMPORT_ALLOWLIST

            if forbidden or not names:
                offenders.append(
                    f"{filename}: imports {sorted(forbidden) or module} "
                    f"from {module}"
                )

    assert offenders == []


def test_no_module_of_the_control_plane_names_the_publisher() -> None:
    """A belt-and-braces text check for the call, not the comment.

    Mentioning ThreadsPublisher in prose is how these rules get
    explained, so only an actual use -- an attribute access or a call --
    is treated as a violation.
    """

    import ast

    offenders: list[str] = []
    banned = {
        "ThreadsPublisher",
        "publish_claimed_publication",
        "claim_due_publication",
    }

    for directory in (API_SOURCE_DIR, APPLICATION_SOURCE_DIR):
        for source in sorted(directory.rglob("*.py")):
            tree = ast.parse(source.read_text(encoding="utf-8"))

            for node in ast.walk(tree):
                if isinstance(node, ast.Name) and node.id in banned:
                    offenders.append(f"{source.name}: {node.id}")
                elif isinstance(node, ast.Attribute) and node.attr in banned:
                    offenders.append(f"{source.name}: .{node.attr}")

    assert offenders == []


def test_importing_the_factory_opens_no_connection(monkeypatch) -> None:
    """`uvicorn --factory` must not touch the database at import time."""

    import ai_smm.db.session as session_module

    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("the API created an engine before a request")

    monkeypatch.setattr(session_module, "create_db_engine", explode)
    monkeypatch.setattr(session_module, "get_engine", explode)

    from ai_smm.config import load_settings

    create_app(load_settings(), access_log=False)


def test_the_role_hierarchy_is_ordered() -> None:
    order = [
        MembershipRole.VIEWER,
        MembershipRole.EDITOR,
        MembershipRole.ADMIN,
        MembershipRole.OWNER,
    ]

    for index, role in enumerate(order):
        for minimum in order[: index + 1]:
            assert role_satisfies(role, minimum) is True

        for minimum in order[index + 1 :]:
            assert role_satisfies(role, minimum) is False


def test_auth_secrets_never_reach_a_log_record(
    api_settings, session_factory, make_user, project: str
) -> None:
    """Drive a real login through the app with the log stream captured."""

    from dataclasses import replace

    from fastapi.testclient import TestClient

    from ai_smm.application.auth import SESSION_COOKIE_NAME
    from ai_smm.logging_setup import (
        JsonFormatter,
        RedactingFilter,
        get_redacting_filter,
    )

    password = "log-probe-password-9f3a"
    user = make_user(email="logprobe@example.com", password=password)

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactingFilter(secret_values=[password]))

    root = logging.getLogger()
    previous_handlers = root.handlers
    previous_level = root.level

    app = create_app(
        replace(api_settings, log_format="json"),
        session_factory=session_factory,
        # Access logging on: the line it writes is part of what must be
        # free of credentials.
        access_log=True,
    )

    root.handlers = [handler]
    root.setLevel(logging.DEBUG)
    root.addFilter(get_redacting_filter())

    try:
        with TestClient(app) as client:
            login = client.post(
                "/api/v1/auth/login",
                json={"email": user.email, "password": password},
            )
            session_token = client.cookies[SESSION_COOKIE_NAME]
            csrf_token = login.json()["csrf_token"]

            client.get("/api/v1/auth/me")
            client.get("/api/v1/auth/csrf")
            client.post(
                "/api/v1/auth/logout", headers={"X-CSRF-Token": csrf_token}
            )
    finally:
        root.handlers = previous_handlers
        root.setLevel(previous_level)

    logged = stream.getvalue()

    assert logged, "no log output was captured; the test proves nothing"

    for secret in (password, session_token, csrf_token):
        assert secret not in logged

    # And no cookie or authorisation header was logged at all.
    lowered = logged.lower()

    for forbidden in ("set-cookie", "smm_session=", "x-csrf-token"):
        assert forbidden not in lowered

    for line in logged.splitlines():
        if not line.strip():
            continue

        payload = json.loads(line)
        context = payload.get("context", {})

        assert "cookie" not in {key.lower() for key in context}


def test_an_unexpected_error_is_answered_without_internals(
    api_settings, session_factory, make_user, monkeypatch
) -> None:
    from fastapi.testclient import TestClient

    import ai_smm.api.routers.auth as auth_module

    def explode(*args: object, **kwargs: object) -> None:
        raise RuntimeError("secret-internal-detail at /srv/ai-smm/private")

    monkeypatch.setattr(auth_module, "authenticate", explode)

    user = make_user()
    app = create_app(
        api_settings, session_factory=session_factory, access_log=False
    )

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/api/v1/auth/login",
            json={"email": user.email, "password": TEST_PASSWORD},
        )

    assert response.status_code == 500

    error = response.json()["error"]

    assert error["code"] == "INTERNAL_ERROR"
    assert error["message"] == "Internal server error."
    assert error["request_id"]
    assert "secret-internal-detail" not in response.text
    assert "RuntimeError" not in response.text


def test_every_error_uses_the_same_envelope(client, project: str) -> None:
    responses = [
        client.get("/api/v1/auth/me"),
        client.get("/api/v1/projects/no-such-project"),
        client.post("/api/v1/auth/login", json={}),
        client.post("/api/v1/auth/logout"),
    ]

    for response in responses:
        assert response.status_code >= 400

        body = response.json()

        assert set(body) == {"error"}
        assert set(body["error"]) == {
            "code",
            "message",
            "details",
            "request_id",
        }
        assert body["error"]["request_id"]
        assert body["error"]["request_id"] == response.headers["X-Request-ID"]
