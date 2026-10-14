"""smm-auth: the isolated authentication service (ADR-024C-3A, SMM-025D).

The same image as smm-api, a different application: this factory mounts
the sign-in, sign-out and step-up-approval routes and the health probes,
nothing else. It connects as ai_smm_auth, which reads users' columns
(password_hash included) and executes the smm_auth.* functions -- and
nothing more: every session, every sign-in audit row and every approval
grant is written by those SECURITY DEFINER functions, with server
timestamps and fixed lifetimes.

Why a separate process. Whoever verifies the password decides who a user
is. Keeping that here means:

* smm-api's database role can no longer create, revive or extend a
  session, nor read a password hash (grants G2);
* passwords never reach smm-api -- nginx routes these paths here;
* an approval by the business-mutation role consumes a grant that only
  this service issues, after re-checking the owner's password for that
  approval (POST /api/v1/auth/step-up/approvals). The preview the owner
  confirms is read here, from the database: a compromised smm-api can
  neither approve nor show the owner a different text at that moment.

Routes, all under /api/v1/auth:

  POST login                         email + password -> session cookie
  POST logout                        session + CSRF -> revoked
  GET  step-up/approvals/{revision}  what approving it would publish
  POST step-up/approvals             revision + password -> 5-minute grant

Peers: with AI_SMM_AUTH_ALLOWED_PEERS set (the packaged service: its
cc-auth subnet), a connection from any other address is answered 403
before routing -- a second layer under the bind address.

Rate limit: failures (sign-in and step-up alike) are counted per address
in the audit log over 15 minutes (smm_auth.recent_login_failures); from
LOGIN_FAILURE_LIMIT on, the password is not even checked until the window
moves on. The answer is the same generic 401 throughout.
"""

from __future__ import annotations

import ipaddress
import threading
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, FastAPI, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from ai_smm.api.cookies import clear_session_cookie, set_session_cookie
from ai_smm.api.dependencies import AppSettings, DbSession
from ai_smm.api.errors import (
    ApiError,
    ErrorCode,
    auth_required,
    error_response,
    install_error_handlers,
    invalid_credentials,
    not_found,
)
from ai_smm.api.request_id import RequestIdMiddleware
from ai_smm.api.routers import health as health_router
from ai_smm.api.schemas import LoginRequest, LoginResponse, UserOut
from ai_smm.application.auth import (
    CSRF_HEADER_NAME,
    SESSION_COOKIE_NAME,
    _decoy_hash,
)
from ai_smm.application.users import InvalidEmailError, normalize_email
from ai_smm.config import Settings, get_settings
from ai_smm.logging_setup import get_logger, setup_logging
from ai_smm.security.passwords import password_needs_rehash, verify_password
from ai_smm.security.tokens import derive_csrf_token, generate_token, tokens_equal


logger = get_logger(__name__)

API_PREFIX = "/api/v1/auth"

#: Failures per address in the rolling 15-minute window after which the
#: password is no longer checked (smm_auth.recent_login_failures).
LOGIN_FAILURE_LIMIT = 10

#: Argon2 costs 64 MiB per verification: at most two at a time (ADR T12).
_ARGON2_SLOTS = threading.BoundedSemaphore(2)

_USER_BY_EMAIL = text(
    "SELECT id, email, password_hash, display_name, is_active, last_login_at,"
    " created_at FROM public.users WHERE email = :email"
)
_USER_BY_ID = text(
    "SELECT id, email, password_hash, display_name, is_active, last_login_at,"
    " created_at FROM public.users WHERE id = :id"
)


class StepUpRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision_id: uuid.UUID
    password: SecretStr = Field(min_length=1, max_length=1024)


class StepUpGrantOut(BaseModel):
    grant_id: uuid.UUID
    revision_id: uuid.UUID
    expires_at: datetime


class ApprovalPreviewOut(BaseModel):
    """Exactly what approving the revision would publish, and where."""

    content_item_id: uuid.UUID
    project_id: str
    revision_id: uuid.UUID
    revision_number: int
    is_current: bool
    item_status: str
    #: The revision's title, or the item's: never NULL (the function coalesces).
    title: str
    body: str
    format: str
    images: list[Any]
    items: list[Any]
    content_hash: str
    social_account_id: uuid.UUID | None
    account_display_name: str | None
    account_external_username: str | None
    account_ready: bool | None


def _verify(password_hash: str, password: str) -> bool:
    with _ARGON2_SLOTS:
        return verify_password(password_hash, password)


def _recent_failures(db: Session, email: str) -> int:
    return db.execute(
        text("SELECT smm_auth.recent_login_failures(:email)"), {"email": email}
    ).scalar_one()


def _record_failure(db: Session, email: str) -> None:
    # Committed on its own: the request transaction is rolled back by the
    # error that follows, and a failure that leaves no trace is exactly
    # what an operator needs after the fact.
    db.execute(text("SELECT smm_auth.record_login_failure(:email)"), {"email": email})
    db.commit()


def _check_password(db: Session, *, email: str, user, password: str) -> bool:
    """The password check of sign-in and step-up, rate-limited per address."""

    if _recent_failures(db, email) >= LOGIN_FAILURE_LIMIT:
        logger.warning(
            "Sign-in throttled for an address", extra={"context": {"limited": True}}
        )
        return False

    if user is None:
        # A missing account costs the same as a wrong password.
        _verify(_decoy_hash(), password)
        _record_failure(db, email)
        return False

    if not _verify(user.password_hash, password) or not user.is_active:
        _record_failure(db, email)
        return False

    if password_needs_rehash(user.password_hash):
        # ADR §11, V1: no opportunistic rehash in a network-facing service.
        logger.warning(
            "Outdated password hash; an operator reset upgrades it",
            extra={"context": {"user_id": str(user.id)}},
        )

    return True


def _user_out(user) -> UserOut:
    return UserOut(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        is_active=user.is_active,
        last_login_at=user.last_login_at,
        created_at=user.created_at,
    )


def _session_token(request: Request) -> str:
    token = request.cookies.get(SESSION_COOKIE_NAME)

    if not token:
        raise auth_required()

    return token


def _require_csrf(request: Request, token: str) -> None:
    presented = request.headers.get(CSRF_HEADER_NAME)

    if not presented:
        raise ApiError(
            status_code=403,
            code=ErrorCode.CSRF_REQUIRED,
            message=f"{CSRF_HEADER_NAME} is required for this method.",
        )

    if not tokens_equal(derive_csrf_token(token), presented):
        raise ApiError(
            status_code=403,
            code=ErrorCode.CSRF_INVALID,
            message=f"{CSRF_HEADER_NAME} does not match this session.",
        )


def _sql_token(db: Session, statement: str, params: dict[str, Any]):
    """Call an smm_auth function with a raw token; a malformed one is no session."""

    try:
        return db.execute(text(statement), params).first()
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) == "22023":
            db.rollback()
            raise auth_required() from None

        raise


router = APIRouter(prefix=API_PREFIX, tags=["auth"])


@router.post("/login", response_model=LoginResponse, summary="Sign in")
def login(
    payload: LoginRequest, response: Response, db: DbSession, settings: AppSettings
) -> LoginResponse:
    try:
        email = normalize_email(payload.email)
    except InvalidEmailError as exc:
        raise invalid_credentials() from exc

    user = db.execute(_USER_BY_EMAIL, {"email": email}).first()

    if not _check_password(
        db, email=email, user=user, password=payload.password.get_secret_value()
    ):
        raise invalid_credentials()

    token = generate_token()
    created = db.execute(
        text(
            "SELECT session_id, expires_at FROM smm_auth.session_create(:user, :token)"
        ),
        {"user": user.id, "token": token},
    ).first()

    if created is None:
        # Deactivated between the check and the insert.
        raise invalid_credentials()

    db.commit()
    set_session_cookie(response, token=token, settings=settings)
    # Read again: session_create set last_login_at.
    fresh = db.execute(_USER_BY_ID, {"id": user.id}).first()

    return LoginResponse(user=_user_out(fresh), csrf_token=derive_csrf_token(token))


@router.post("/logout", status_code=204, summary="Sign out")
def logout(request: Request, db: DbSession, settings: AppSettings) -> Response:
    token = _session_token(request)
    _require_csrf(request, token)

    revoked = _sql_token(
        db,
        "SELECT session_id FROM smm_auth.session_revoke(:token)",
        {"token": token},
    )

    if revoked is None:
        raise auth_required()

    response = Response(status_code=204)
    clear_session_cookie(response, settings=settings)

    return response


@router.get(
    "/step-up/approvals/{revision_id}",
    response_model=ApprovalPreviewOut,
    summary="What approving this revision would publish, and where",
)
def approval_preview(revision_id: uuid.UUID, request: Request, db: DbSession):
    token = _session_token(request)
    row = _sql_token(
        db,
        "SELECT * FROM smm_auth.approval_preview(:token, :revision)",
        {"token": token, "revision": revision_id},
    )

    if row is None:
        # Unknown revision, or not the owner of its project: the same 404.
        raise not_found("Revision not found.")

    data = dict(row._mapping)

    return ApprovalPreviewOut(revision_id=revision_id, **data)


@router.post(
    "/step-up/approvals",
    status_code=201,
    response_model=StepUpGrantOut,
    summary="Confirm, with the password, the approval of one revision",
)
def approval_step_up(
    payload: StepUpRequest, request: Request, db: DbSession
) -> StepUpGrantOut:
    token = _session_token(request)
    _require_csrf(request, token)

    session = _sql_token(
        db,
        "SELECT user_id FROM smm_auth.session_resolve(:token)",
        {"token": token},
    )

    if session is None:
        raise auth_required()

    user = db.execute(_USER_BY_ID, {"id": session.user_id}).first()

    if not _check_password(
        db,
        email=user.email,
        user=user,
        password=payload.password.get_secret_value(),
    ):
        raise ApiError(
            status_code=401,
            code=ErrorCode.STEP_UP_FAILED,
            message="The password did not confirm the approval. Nothing was approved.",
        )

    try:
        grant = db.execute(
            text(
                "SELECT grant_id, expires_at"
                " FROM smm_auth.approval_grant_issue(:token, :revision)"
            ),
            {"token": token, "revision": payload.revision_id},
        ).first()
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) == "55000":
            raise ApiError(
                status_code=409,
                code=ErrorCode.INVALID_STATE_TRANSITION,
                message=(
                    "This revision cannot be confirmed now: it must be the "
                    "current revision, under review, with a ready "
                    "destination account."
                ),
            ) from None

        raise

    if grant is None:
        raise not_found("Revision not found.")

    db.commit()

    return StepUpGrantOut(
        grant_id=grant.grant_id,
        revision_id=payload.revision_id,
        expires_at=grant.expires_at,
    )


class PeerAllowlistMiddleware:
    """Refuses every connection whose peer is outside `networks`."""

    def __init__(self, app, networks: tuple[str, ...]) -> None:
        self.app = app
        self.networks = tuple(ipaddress.ip_network(n) for n in networks)

    def _allowed(self, client) -> bool:
        if not client:
            return False

        try:
            address = ipaddress.ip_address(client[0])
        except ValueError:
            return False

        return any(address in network for network in self.networks)

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] in ("http", "websocket") and not self._allowed(
            scope.get("client")
        ):
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return

            response = error_response(
                status_code=403,
                code=ErrorCode.FORBIDDEN,
                message="Not available from this network.",
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


def create_auth_app(
    settings: Settings | None = None,
    *,
    session_factory: sessionmaker[Session] | None = None,
    access_log: bool = True,
) -> FastAPI:
    """The smm-auth application. No docs, no business routes, ever."""

    settings = settings or get_settings()
    setup_logging(settings)

    app = FastAPI(
        title="AI SMM Department authentication",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.session_factory = session_factory

    app.add_middleware(RequestIdMiddleware, access_log=access_log)

    if settings.auth_allowed_peers:
        # Added last, so it runs first: nothing else sees the request.
        app.add_middleware(
            PeerAllowlistMiddleware, networks=settings.auth_allowed_peers
        )

    install_error_handlers(app)
    app.include_router(health_router.router)
    app.include_router(router)

    return app
