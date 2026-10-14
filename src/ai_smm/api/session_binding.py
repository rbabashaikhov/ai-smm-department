"""Bind every business-mutation transaction to its session, in SQL (SMM-025D).

With mutations on, an unsafe request first calls guard_bind_session with
the raw cookie token, in the request's own transaction. PostgreSQL checks
the session itself -- not revoked, not expired, not idle, an active user,
plausible timestamps -- and records which user this transaction acts for.
Every database guard (ai_smm.db.guards) reads the user from there; nothing
the API passes names it.

This is what turns the API's own role into a role that cannot write on
its own: a business write without a bound transaction is refused by the
database, so a code path that skipped this dependency would fail closed,
not open. Login and logout are not bound: they write no business row.

Runs after csrf_guard (which already answered 401 for a missing session)
and the read-only gate (which already refused everything when mutations
are off).
"""
from __future__ import annotations

from fastapi import Request

from ai_smm.api.csrf import SAFE_METHODS
from ai_smm.api.dependencies import AppSettings, DbSession
from ai_smm.api.errors import auth_required
from ai_smm.api.mutation_gate import MUTATION_GATE_EXEMPT_PATHS
from ai_smm.application.auth import (
    SESSION_COOKIE_NAME,
    bind_transaction_to_session,
)


def session_binding(request: Request, db: DbSession, settings: AppSettings) -> None:
    if not settings.api_mutations_enabled:
        return

    if request.method in SAFE_METHODS:
        return

    if request.url.path in MUTATION_GATE_EXEMPT_PATHS:
        return

    bound = bind_transaction_to_session(
        db, token=request.cookies.get(SESSION_COOKIE_NAME)
    )

    if bound is None:
        # The database does not consider this session live: the same answer
        # as a missing session, and nothing has been written.
        raise auth_required()
