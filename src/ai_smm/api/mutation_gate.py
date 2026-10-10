"""Read-only mode for the whole versioned API.

When AI_SMM_API_MUTATIONS_ENABLED is off, every request with an unsafe
method is refused with 403 MUTATIONS_DISABLED before its endpoint runs --
whatever the caller's role, owner included. The request's database
session is rolled back, so a refused request leaves no row behind, not
even the session's last_seen_at.

Deny by default: the gate is a dependency of the whole /api/v1 router,
next to csrf_guard, so a route added later is covered without anyone
having to remember it. It runs after csrf_guard, which keeps the answers
for a missing session (401) and a missing or wrong token (403 CSRF_*)
exactly what they are with the gate on.

The allowlist is the session lifecycle and nothing else:

* POST /api/v1/auth/login  -- creates a user_sessions row, sets
  users.last_login_at, writes auth.login / auth.login_failed audit rows;
* POST /api/v1/auth/logout -- sets user_sessions.revoked_at, writes an
  auth.logout audit row.

GET requests are not gated. Their one write is the session maintenance
resolve_session does on every authenticated request (last_seen_at, the
idle deadline); no GET route changes business state.

Not on the allowlist, on purpose: user and membership bootstrap (there is
no HTTP route for it; it is the `ai-smm user` CLI), project settings,
content, approval and every publication command.
"""
from __future__ import annotations

from fastapi import Request

from ai_smm.api.csrf import SAFE_METHODS
from ai_smm.api.dependencies import AppSettings
from ai_smm.api.errors import ApiError, ErrorCode


#: Unsafe endpoints that stay open in read-only mode: the session
#: lifecycle. Matched against the exact path, so anything else -- a
#: trailing slash, another prefix -- falls on the refusing side.
MUTATION_GATE_EXEMPT_PATHS = frozenset(
    {
        "/api/v1/auth/login",
        "/api/v1/auth/logout",
    }
)


def mutations_disabled(command: str | None = None) -> ApiError:
    details: dict[str, str] = {"reason": "read_only_control_plane"}

    if command:
        details["command"] = command

    return ApiError(
        status_code=403,
        code=ErrorCode.MUTATIONS_DISABLED,
        message=(
            "This Control Center is read-only: changes are disabled on the "
            "server (AI_SMM_API_MUTATIONS_ENABLED=false). Nothing was "
            "changed."
        ),
        details=details,
    )


def mutation_gate(request: Request, settings: AppSettings) -> None:
    if settings.api_mutations_enabled:
        return

    if request.method in SAFE_METHODS:
        return

    if request.url.path in MUTATION_GATE_EXEMPT_PATHS:
        return

    raise mutations_disabled()
