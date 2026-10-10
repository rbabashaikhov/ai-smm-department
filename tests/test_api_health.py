"""Liveness answers without the database; readiness answers only about it."""
from __future__ import annotations

from ai_smm.api.app import create_app
from ai_smm.api.request_id import REQUEST_ID_HEADER


def test_live_needs_no_database() -> None:
    """No session factory at all: liveness must still be 200."""

    from fastapi.testclient import TestClient

    from ai_smm.config import load_settings

    app = create_app(load_settings(), session_factory=None, access_log=False)

    with TestClient(app) as client:
        response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "live"}


def test_live_does_not_touch_openai_or_threads(monkeypatch, client) -> None:
    """A probe must not spend an external quota."""

    import ai_smm.publishing.threads as threads_module

    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("the health endpoint contacted Threads")

    monkeypatch.setattr(threads_module.ThreadsPublisher, "__init__", explode)

    assert client.get("/health/live").status_code == 200
    assert client.get("/health/ready").status_code == 200


def test_ready_reports_the_database(client) -> None:
    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "checks": {"database": "ok"},
    }


def test_ready_returns_503_when_the_database_is_unavailable(
    api_settings,
) -> None:
    """Point the app at a port where nothing listens."""

    from dataclasses import replace

    from fastapi.testclient import TestClient
    from sqlalchemy.orm import sessionmaker

    from ai_smm.db.session import create_db_engine

    dead_url = "postgresql+psycopg://nobody@127.0.0.1:1/ai_smm_test"
    engine = create_db_engine(
        dead_url, settings=replace(api_settings, database_url=dead_url)
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    app = create_app(
        api_settings, session_factory=factory, access_log=False
    )

    try:
        with TestClient(app) as client:
            response = client.get("/health/ready")
            live = client.get("/health/live")
    finally:
        engine.dispose()

    assert response.status_code == 503

    error = response.json()["error"]

    assert error["code"] == "DEPENDENCY_UNAVAILABLE"
    assert error["details"] == {"dependency": "database"}
    assert error["request_id"]

    # Liveness is unaffected: the process is healthy, its dependency is not.
    assert live.status_code == 200


def test_readiness_error_carries_no_internals(api_settings) -> None:
    """No host, no port, no driver text, no SQL in the response."""

    from dataclasses import replace

    from fastapi.testclient import TestClient
    from sqlalchemy.orm import sessionmaker

    from ai_smm.db.session import create_db_engine

    dead_url = "postgresql+psycopg://secret-user:hunter2@127.0.0.1:1/x_test"
    engine = create_db_engine(
        dead_url, settings=replace(api_settings, database_url=dead_url)
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    app = create_app(api_settings, session_factory=factory, access_log=False)

    try:
        with TestClient(app) as client:
            body = client.get("/health/ready").text
    finally:
        engine.dispose()

    for leak in ("hunter2", "secret-user", "psycopg", "SELECT", "Traceback"):
        assert leak not in body


def test_request_id_is_returned_and_echoed(client) -> None:
    generated = client.get("/health/live")

    assert generated.headers[REQUEST_ID_HEADER]

    echoed = client.get(
        "/health/live", headers={REQUEST_ID_HEADER: "trace-abc-123"}
    )

    assert echoed.headers[REQUEST_ID_HEADER] == "trace-abc-123"


def test_unacceptable_inbound_request_id_is_replaced(client) -> None:
    """A caller must not be able to inject anything into our logs."""

    response = client.get(
        "/health/live", headers={REQUEST_ID_HEADER: "a b;c" * 40}
    )

    assert response.headers[REQUEST_ID_HEADER] != "a b;c" * 40
    assert len(response.headers[REQUEST_ID_HEADER]) == 32
