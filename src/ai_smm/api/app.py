"""Application factory.

    uvicorn ai_smm.api.app:create_app --factory

Importing this module must stay free of side effects: no engine, no
connection, no outbound call. Everything that touches the database happens
inside a request, which is what makes the factory safe to import from a
test, a migration shell or a reload loop.

The API is the control plane. It reads and edits the metadata around the
publication queue -- identity, membership, project settings -- and it never
publishes: there is no endpoint that calls ThreadsPublisher, and the worker
remains the only process that can create a post.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, FastAPI
from sqlalchemy.orm import Session, sessionmaker

from ai_smm.api.csrf import csrf_guard
from ai_smm.api.errors import install_error_handlers
from ai_smm.api.request_id import RequestIdMiddleware
from ai_smm.api.routers import auth as auth_router
from ai_smm.api.routers import health as health_router
from ai_smm.api.routers import operations as operations_router
from ai_smm.api.routers import projects as projects_router
from ai_smm.api.routers import publications as publications_router
from ai_smm.api.routers import series as series_router
from ai_smm.config import Settings, get_settings
from ai_smm.logging_setup import setup_logging


API_V1_PREFIX = "/api/v1"

DESCRIPTION = (
    "Control plane for the AI SMM publication queue. This API never "
    "publishes: scheduled publications are executed by the worker."
)


def create_app(
    settings: Settings | None = None,
    *,
    session_factory: sessionmaker[Session] | None = None,
    access_log: bool = True,
) -> FastAPI:
    """Build the application.

    settings and session_factory are injectable so that a test can run
    the real app against a disposable database without touching the
    process-wide engine cache.
    """

    settings = settings or get_settings()
    setup_logging(settings)

    app = FastAPI(
        title="AI SMM Department API",
        description=DESCRIPTION,
        version="0.1.0",
    )

    app.state.settings = settings
    app.state.session_factory = session_factory

    app.add_middleware(RequestIdMiddleware, access_log=access_log)
    install_error_handlers(app)

    # Unauthenticated and outside /api/v1 on purpose: a probe must not
    # need a session, and its path must not move when the API is versioned.
    app.include_router(health_router.router)

    # CSRF is a dependency of the whole versioned router, so a route added
    # later is protected without anyone having to remember to ask.
    v1 = APIRouter(prefix=API_V1_PREFIX, dependencies=[Depends(csrf_guard)])
    v1.include_router(auth_router.router)
    v1.include_router(projects_router.router)
    v1.include_router(publications_router.router)
    v1.include_router(series_router.router)
    v1.include_router(operations_router.router)

    app.include_router(v1)

    return app
