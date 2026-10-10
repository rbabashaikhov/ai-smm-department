"""Request-scoped dependencies: settings, database session, identity, RBAC.

The authorisation flow is fixed and goes in one direction only:

    resource from the database -> membership -> role check -> action

A project id is read from the URL path and loaded from the database; the
request body is never consulted for it. A caller with no membership on a
project is answered with 404, not 403, so that the API cannot be used to
discover which project ids exist.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from ai_smm.api.errors import ApiError, ErrorCode, auth_required, not_found
from ai_smm.application.auth import (
    SESSION_COOKIE_NAME,
    AuthenticatedSession,
    resolve_session,
)
from ai_smm.application.projects import get_membership
from ai_smm.config import Settings, get_settings
from ai_smm.db.models import MembershipRole, Project, User
from ai_smm.db.session import get_session_factory
from ai_smm.security.rbac import role_satisfies


def get_app_settings(request: Request) -> Settings:
    """Settings fixed when the app was created, never re-read per request."""

    settings: Settings | None = getattr(request.app.state, "settings", None)

    return settings or get_settings()


def get_db(request: Request) -> Iterator[Session]:
    """One session per request, committed on success, rolled back on error.

    The factory is resolved here rather than at import time: creating an
    engine while the module is imported would make `uvicorn --factory`
    touch the database before the first request.
    """

    factory = getattr(request.app.state, "session_factory", None)

    if factory is None:
        factory = get_session_factory(get_app_settings(request))

    session = factory()

    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


#: Declared as Annotated aliases rather than as parameter defaults, which
#: keeps the signatures readable and avoids a callable default.
AppSettings = Annotated[Settings, Depends(get_app_settings)]
DbSession = Annotated[Session, Depends(get_db)]


def get_current_session(
    request: Request,
    db: DbSession,
    settings: AppSettings,
) -> AuthenticatedSession:
    """The caller's session, or 401. Also advances the idle deadline."""

    token = request.cookies.get(SESSION_COOKIE_NAME)
    authenticated = resolve_session(db, token=token, settings=settings)

    if authenticated is None:
        raise auth_required()

    return authenticated


CurrentSession = Annotated[
    AuthenticatedSession, Depends(get_current_session)
]


def get_current_user(authenticated: CurrentSession) -> User:
    return authenticated.user


CurrentUser = Annotated[User, Depends(get_current_user)]


def actor_for(authenticated: AuthenticatedSession) -> str:
    """Audit actor for a request made by a signed-in user."""

    return f"user:{authenticated.user.id}"


class ProjectContext:
    """A project loaded from the database plus the caller's role in it."""

    def __init__(
        self,
        *,
        project: Project,
        role: MembershipRole,
        authenticated: AuthenticatedSession,
    ) -> None:
        self.project = project
        self.role = role
        self.authenticated = authenticated

    @property
    def actor(self) -> str:
        return actor_for(self.authenticated)


def require_project_role(
    minimum: MembershipRole,
) -> Callable[..., ProjectContext]:
    """Dependency factory: the caller must hold `minimum` or higher.

    Missing project and missing membership are both 404. Having a
    membership that is too low is 403: at that point the caller already
    knows the project exists.
    """

    def dependency(
        project_id: str,
        db: DbSession,
        authenticated: CurrentSession,
    ) -> ProjectContext:
        project = db.get(Project, project_id)

        if project is None:
            raise not_found("Project not found.")

        membership = get_membership(
            db, project_id=project.id, user=authenticated.user
        )

        if membership is None:
            raise not_found("Project not found.")

        if not role_satisfies(membership.role, minimum):
            raise ApiError(
                status_code=403,
                code=ErrorCode.FORBIDDEN,
                message="Insufficient role for this action.",
                details={
                    "required_role": minimum.value,
                    "your_role": membership.role.value,
                },
            )

        return ProjectContext(
            project=project,
            role=membership.role,
            authenticated=authenticated,
        )

    return dependency


#: The two access levels the routes need today. A new level is one more
#: alias, so no route has to spell out a role comparison.
ViewerProject = Annotated[
    ProjectContext, Depends(require_project_role(MembershipRole.VIEWER))
]
AdminProject = Annotated[
    ProjectContext, Depends(require_project_role(MembershipRole.ADMIN))
]
