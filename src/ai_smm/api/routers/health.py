"""Liveness and readiness.

Liveness answers from the process alone: it must stay up while a
dependency is down, or an orchestrator would restart a container that is
merely waiting for the database.

Readiness checks exactly one dependency, the database, with SELECT 1. It
does not call OpenAI or Threads: a readiness probe that spends an external
quota, or that fails because a third party is slow, takes the service out
of rotation for no reason.
"""
from __future__ import annotations

import contextlib

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ai_smm.api.dependencies import get_app_settings
from ai_smm.api.errors import ErrorCode, error_response
from ai_smm.db.session import check_database, get_session_factory
from ai_smm.logging_setup import get_logger


logger = get_logger(__name__)

router = APIRouter(tags=["health"])


@router.get("/health/live", summary="Process liveness, no dependencies")
def live() -> dict[str, str]:
    return {"status": "live"}


@router.get("/health/ready", summary="Database readiness (SELECT 1)")
def ready(request: Request) -> JSONResponse:
    error = _probe_database(request)

    if error is not None:
        logger.warning(
            "Readiness probe failed",
            extra={
                "context": {
                    "request_id": getattr(request.state, "request_id", None),
                    "dependency": "database",
                    "error_type": error,
                }
            },
        )

        return error_response(
            status_code=503,
            code=ErrorCode.DEPENDENCY_UNAVAILABLE,
            message="Database is not available.",
            details={"dependency": "database"},
            request_id=getattr(request.state, "request_id", None),
        )

    return JSONResponse(
        status_code=200,
        content={"status": "ready", "checks": {"database": "ok"}},
    )


def _probe_database(request: Request) -> str | None:
    """Returns the exception class name on failure, None on success.

    The session is opened and closed here instead of through the request
    dependency: that dependency commits on the way out, which would raise
    a second, less useful error when the connection is the thing that is
    broken.
    """

    try:
        factory = getattr(request.app.state, "session_factory", None)

        if factory is None:
            factory = get_session_factory(get_app_settings(request))

        session = factory()
    except Exception as exc:
        return type(exc).__name__

    try:
        check_database(session)
    except Exception as exc:
        return type(exc).__name__
    finally:
        # A session whose connection never opened can fail to roll back;
        # the probe has already decided by this point.
        with contextlib.suppress(Exception):
            session.rollback()

        session.close()

    return None
