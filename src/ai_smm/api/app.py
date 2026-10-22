"""Application factory.

    uvicorn ai_smm.api.app:create_app --factory

Importing this module must stay free of side effects: no engine, no
connection, no outbound call. Everything that touches the database happens
inside a request, which is what makes the factory safe to import from a
test, a migration shell or a reload loop.

The API is the control plane. It reads and edits the metadata around the
publication queue -- identity, membership, project settings -- and it never
publishes: there is no endpoint that calls ThreadsPublisher, and the worker
remains the only process that can create a post. The one outbound call it
makes is the read-only connection test (GET /me) when an owner stores a
credential.

Unless AI_SMM_API_MUTATIONS_ENABLED is on, the API is also read-only:
every business-state change is refused centrally (ai_smm.api.mutation_gate)
and again at the queue commands (ai_smm.application.control_plane).
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, FastAPI
from sqlalchemy.orm import Session, sessionmaker

from ai_smm.api.csrf import csrf_guard
from ai_smm.api.errors import install_error_handlers
from ai_smm.api.mutation_gate import mutation_gate
from ai_smm.api.request_id import RequestIdMiddleware
from ai_smm.api.routers import auth as auth_router
from ai_smm.api.routers import content as content_router
from ai_smm.api.routers import generation as generation_router
from ai_smm.api.routers import health as health_router
from ai_smm.api.routers import knowledge as knowledge_router
from ai_smm.api.routers import operations as operations_router
from ai_smm.api.routers import projects as projects_router
from ai_smm.api.routers import publications as publications_router
from ai_smm.api.routers import series as series_router
from ai_smm.api.routers import social_accounts as social_accounts_router
from ai_smm.api.routers import topic_ideation as topic_ideation_router
from ai_smm.api.session_binding import session_binding
from ai_smm.config import Settings, get_settings
from ai_smm.logging_setup import setup_logging


API_V1_PREFIX = "/api/v1"

DESCRIPTION = (
    "Control plane for the AI SMM publication queue. This API never "
    "publishes: scheduled publications are executed by the worker."
)


def _fetch_identity(token: Any) -> Any:
    from ai_smm.platform_identity import fetch_identity

    return fetch_identity(token)


def create_app(
    settings: Settings | None = None,
    *,
    session_factory: sessionmaker[Session] | None = None,
    access_log: bool = True,
    identity_fetcher: Any = None,
) -> FastAPI:
    """Build the application.

    settings and session_factory are injectable so that a test can run
    the real app against a disposable database without touching the
    process-wide engine cache.
    """

    settings = settings or get_settings()
    setup_logging(settings)

    # Disabling the schema route takes the UIs down with it, but all three
    # are spelled out so a reader does not have to know that.
    docs_enabled = settings.api_docs_enabled

    app = FastAPI(
        title="AI SMM Department API",
        description=DESCRIPTION,
        version="0.1.0",
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        openapi_url="/openapi.json" if docs_enabled else None,
    )

    app.state.settings = settings
    app.state.session_factory = session_factory
    #: Tests a credential at entry (GET /me). Injectable: tests never
    #: reach the platform.
    app.state.identity_fetcher = identity_fetcher or _fetch_identity

    app.add_middleware(RequestIdMiddleware, access_log=access_log)
    install_error_handlers(app)

    # Unauthenticated and outside /api/v1 on purpose: a probe must not
    # need a session, and its path must not move when the API is versioned.
    app.include_router(health_router.router)

    # CSRF and the read-only gate are dependencies of the whole versioned
    # router, so a route added later is protected without anyone having
    # to remember to ask. Order matters: authentication and CSRF answer
    # first, then the gate refuses whatever is not on its allowlist.
    v1 = APIRouter(
        prefix=API_V1_PREFIX,
        dependencies=[
            Depends(csrf_guard),
            Depends(mutation_gate),
            # SMM-025D: with mutations on, the transaction is bound to the
            # session in SQL before any route touches the database.
            Depends(session_binding),
        ],
    )
    v1.include_router(auth_router.router)
    if settings.api_auth_routes_enabled:
        v1.include_router(auth_router.session_router)
    v1.include_router(projects_router.router)
    v1.include_router(knowledge_router.router)
    v1.include_router(publications_router.router)
    v1.include_router(series_router.router)
    v1.include_router(operations_router.router)
    v1.include_router(content_router.router)
    v1.include_router(social_accounts_router.router)
    v1.include_router(generation_router.router)
    v1.include_router(topic_ideation_router.router)

    app.include_router(v1)

    return app
