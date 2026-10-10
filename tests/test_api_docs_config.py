"""AI_SMM_API_DOCS_ENABLED: the interactive docs can be switched off.

None of these tests needs a database: the docs routes, the health probe
and a 404 for an unknown route are all answered without a session.
"""
from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from ai_smm.api.app import create_app
from ai_smm.config import load_settings


DOC_PATHS = ("/docs", "/redoc", "/openapi.json")


def _client(*, docs_enabled: bool) -> TestClient:
    settings = replace(load_settings(), api_docs_enabled=docs_enabled)
    app = create_app(settings, session_factory=None, access_log=False)

    return TestClient(app)


def test_docs_are_enabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Local development keeps the behaviour it had before the setting."""

    monkeypatch.delenv("AI_SMM_API_DOCS_ENABLED", raising=False)

    assert load_settings().api_docs_enabled is True


@pytest.mark.parametrize(
    ("value", "expected"),
    [("true", True), ("1", True), ("false", False), ("0", False)],
)
def test_docs_setting_parsing(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: bool
) -> None:
    monkeypatch.setenv("AI_SMM_API_DOCS_ENABLED", value)

    assert load_settings().api_docs_enabled is expected


def test_invalid_docs_setting_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AI_SMM_API_DOCS_ENABLED", "sometimes")

    with pytest.raises(ValueError, match="must be a boolean"):
        load_settings()


@pytest.mark.parametrize("path", DOC_PATHS)
def test_docs_routes_are_served_when_enabled(path: str) -> None:
    with _client(docs_enabled=True) as client:
        response = client.get(path)

    assert response.status_code == 200


def test_openapi_schema_lists_the_api_when_enabled() -> None:
    with _client(docs_enabled=True) as client:
        paths = client.get("/openapi.json").json()["paths"]

    assert "/health/live" in paths
    assert "/api/v1/auth/login" in paths


@pytest.mark.parametrize("path", DOC_PATHS)
def test_docs_routes_are_404_when_disabled(path: str) -> None:
    """The API's own error envelope, not an HTML page."""

    with _client(docs_enabled=False) as client:
        response = client.get(path)

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_disabling_docs_leaves_the_api_untouched() -> None:
    """Only the docs go away: probes and routing behave as before."""

    with _client(docs_enabled=False) as client:
        live = client.get("/health/live")
        unknown = client.get("/api/v1/not-existing")

    assert live.status_code == 200
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "NOT_FOUND"


def test_schema_is_still_generated_in_code_when_disabled() -> None:
    """OpenAPI support stays in the code; only its route is off."""

    app = create_app(
        replace(load_settings(), api_docs_enabled=False),
        session_factory=None,
        access_log=False,
    )

    assert "/api/v1/auth/login" in app.openapi()["paths"]
