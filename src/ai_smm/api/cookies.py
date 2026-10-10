"""The session cookie, in one place.

HttpOnly so no script can read the token, SameSite=Lax so a cross-site
POST does not carry it, Path=/ so it covers the whole API, and Secure
unless the deployment explicitly turns it off for plain-HTTP local use.
"""
from __future__ import annotations

from starlette.responses import Response

from ai_smm.application.auth import SESSION_COOKIE_NAME
from ai_smm.config import Settings


def set_session_cookie(
    response: Response, *, token: str, settings: Settings
) -> None:
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=settings.api_session_lifetime_seconds,
        path="/",
        httponly=True,
        samesite="lax",
        secure=settings.api_cookie_secure,
    )


def clear_session_cookie(response: Response, *, settings: Settings) -> None:
    # The attributes must match the ones used when setting it, or the
    # browser keeps the original cookie alongside the deletion.
    response.delete_cookie(
        SESSION_COOKIE_NAME,
        path="/",
        httponly=True,
        samesite="lax",
        secure=settings.api_cookie_secure,
    )
