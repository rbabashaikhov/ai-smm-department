"""Login, logout, identity and the CSRF token.

What this module deliberately does not have: a registration endpoint. The
first user of a project is created at the console with
`ai-smm user create-owner`, so no one can create an account over HTTP.

Every failed login gets the same answer whatever went wrong -- unknown
address, wrong password, deactivated account -- so the API cannot be used
to find out which addresses exist.
"""
from __future__ import annotations

from fastapi import APIRouter, Response

from ai_smm.api.cookies import clear_session_cookie, set_session_cookie
from ai_smm.api.dependencies import (
    AppSettings,
    CurrentSession,
    DbSession,
    actor_for,
)
from ai_smm.api.errors import invalid_credentials
from ai_smm.api.schemas import (
    CsrfResponse,
    LoginRequest,
    LoginResponse,
    MeResponse,
    ProjectAccessOut,
    UserOut,
)
from ai_smm.application.auth import (
    CSRF_HEADER_NAME,
    authenticate,
    create_session,
    issue_csrf_token,
    revoke_session,
    utcnow,
)
from ai_smm.application.projects import list_user_projects
from ai_smm.application.users import InvalidEmailError, normalize_email
from ai_smm.queue import record_audit


router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/login",
    response_model=LoginResponse,
    summary="Open a server-side session and set the smm_session cookie",
)
def login(
    payload: LoginRequest,
    response: Response,
    db: DbSession,
    settings: AppSettings,
) -> LoginResponse:
    try:
        email = normalize_email(payload.email)
    except InvalidEmailError as exc:
        # Same response as a wrong password: a malformed address must not
        # be distinguishable from an unknown one.
        raise invalid_credentials() from exc

    user = authenticate(
        db, email=email, password=payload.password.get_secret_value()
    )

    if user is None:
        record_audit(
            db,
            actor="anonymous",
            action="auth.login_failed",
            subject=f"email:{email}",
            details={},
        )

        # Committed here on purpose: raising rolls the request session
        # back, and a failed attempt that leaves no trace is exactly the
        # record an operator needs after the fact.
        db.commit()

        raise invalid_credentials()

    now = utcnow()
    user_session, token = create_session(
        db, user=user, settings=settings, now=now
    )
    csrf_token = issue_csrf_token(user_session, now=now)

    user.last_login_at = now
    user.updated_at = now

    record_audit(
        db,
        actor=f"user:{user.id}",
        action="auth.login",
        subject=f"user:{user.id}",
        details={"session_id": str(user_session.id)},
    )

    set_session_cookie(response, token=token, settings=settings)

    return LoginResponse(
        user=_user_out(user), csrf_token=csrf_token
    )


@router.post(
    "/logout",
    status_code=204,
    summary="Revoke the current session and clear the cookie",
)
def logout(
    db: DbSession,
    settings: AppSettings,
    authenticated: CurrentSession,
) -> Response:
    revoke_session(authenticated.session)

    record_audit(
        db,
        actor=actor_for(authenticated),
        action="auth.logout",
        subject=f"user:{authenticated.user.id}",
        details={"session_id": str(authenticated.session.id)},
    )

    response = Response(status_code=204)
    clear_session_cookie(response, settings=settings)

    return response


@router.get(
    "/me",
    response_model=MeResponse,
    summary="The signed-in user and the projects they may work on",
)
def me(db: DbSession, authenticated: CurrentSession) -> MeResponse:
    access = list_user_projects(db, user=authenticated.user)

    return MeResponse(
        user=_user_out(authenticated.user),
        projects=[
            ProjectAccessOut(
                project_id=item.project.id,
                display_name=item.project.display_name,
                role=item.role,
            )
            for item in access
        ],
    )


@router.get(
    "/csrf",
    response_model=CsrfResponse,
    summary="Mint the CSRF token this session must send on unsafe requests",
)
def csrf(authenticated: CurrentSession) -> CsrfResponse:
    # Issuing replaces the previous token for this session, so a client
    # fetches one after login and keeps it, rather than per request.
    token = issue_csrf_token(authenticated.session)

    return CsrfResponse(csrf_token=token, header_name=CSRF_HEADER_NAME)


def _user_out(user) -> UserOut:
    return UserOut(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        is_active=user.is_active,
        last_login_at=user.last_login_at,
        created_at=user.created_at,
    )
