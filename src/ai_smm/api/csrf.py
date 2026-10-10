"""CSRF protection for the cookie-authenticated API.

The session cookie is SameSite=Lax, which already stops a cross-site POST
from carrying it in current browsers. That is a mitigation, not a
guarantee -- it says nothing about older clients, about a reverse proxy
that rewrites the attribute, or about a same-site subdomain -- so every
unsafe request must additionally present the session's CSRF token in the
X-CSRF-Token header.

The check is installed as a dependency of the whole /api/v1 router rather
than per route, so a route added later is protected by default and has to
be listed in CSRF_EXEMPT_PATHS to opt out.

The token itself is derived from the session token rather than stored and
rotated, so every tab of one session holds the same working token and
fetching it again never invalidates a tab that already has it. It stops
working when the session is revoked or expires, because the check starts
from the cookie.

The one exemption is login: there is no session yet, so there is no token
to present, and a forged login grants an attacker a session in their own
browser rather than access to the victim's account.
"""
from __future__ import annotations

from fastapi import Request

from ai_smm.api.dependencies import AppSettings, DbSession
from ai_smm.api.errors import ApiError, ErrorCode, auth_required
from ai_smm.application.auth import (
    CSRF_HEADER_NAME,
    SESSION_COOKIE_NAME,
    csrf_token_is_valid,
    resolve_session,
)


#: Methods that must not change state, and so need no token.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

#: Unsafe endpoints that cannot present a token because no session exists.
CSRF_EXEMPT_PATHS = frozenset({"/api/v1/auth/login"})


def csrf_guard(
    request: Request, db: DbSession, settings: AppSettings
) -> None:
    if request.method in SAFE_METHODS:
        return

    if request.url.path in CSRF_EXEMPT_PATHS:
        return

    # The session is resolved here rather than through the identity
    # dependency: this guard also covers the exempt paths, where
    # demanding authentication first would be wrong.
    authenticated = resolve_session(
        db,
        token=request.cookies.get(SESSION_COOKIE_NAME),
        settings=settings,
    )

    if authenticated is None:
        raise auth_required()

    presented = request.headers.get(CSRF_HEADER_NAME)

    if not presented:
        raise ApiError(
            status_code=403,
            code=ErrorCode.CSRF_REQUIRED,
            message=(
                f"{CSRF_HEADER_NAME} is required for this method. Fetch a "
                "token from GET /api/v1/auth/csrf."
            ),
        )

    if not csrf_token_is_valid(authenticated, presented):
        raise ApiError(
            status_code=403,
            code=ErrorCode.CSRF_INVALID,
            message=(
                f"{CSRF_HEADER_NAME} does not match this session. Fetch a "
                "new token from GET /api/v1/auth/csrf."
            ),
        )
