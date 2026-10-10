"""Server-side sessions.

A login produces one random token. The client keeps it in the smm_session
cookie; the database keeps only its SHA-256 digest, so the session table
cannot be replayed as a login even if it is dumped.

Two deadlines bound a session, and both are checked on every request:

* absolute lifetime -- expires_at, fixed at login (7 days by default);
* idle timeout -- derived from last_seen_at (24 hours by default), so a
  session that is simply abandoned stops working long before it expires.

Nothing here contacts an external system, and no function in this module
returns a reason for a failed login: the API answers with one generic
message so that an attacker cannot tell a wrong password from an unknown
address.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_smm.config import Settings
from ai_smm.db.models import User, UserSession
from ai_smm.security.passwords import (
    hash_password,
    password_needs_rehash,
    verify_password,
)
from ai_smm.security.tokens import generate_token, hash_token, tokens_match


#: The only cookie the API sets. HttpOnly, SameSite=Lax, Path=/, and
#: Secure unless AI_SMM_API_COOKIE_SECURE says otherwise.
SESSION_COOKIE_NAME = "smm_session"

#: Header an unsafe request must carry.
CSRF_HEADER_NAME = "X-CSRF-Token"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class AuthenticatedSession:
    """A session that passed every check, with its user already loaded."""

    user: User
    session: UserSession


def authenticate(
    db: Session, *, email: str, password: str
) -> User | None:
    """Return the user when the credentials are valid, else None.

    An inactive user never authenticates. The password is verified even
    when no user matches, so the response time does not reveal whether
    the address exists.
    """

    user = db.scalars(
        select(User).where(User.email == email)
    ).one_or_none()

    if user is None:
        # Compare against a throwaway hash so a missing account costs the
        # same as a wrong password.
        verify_password(_decoy_hash(), password)

        return None

    if not verify_password(user.password_hash, password):
        return None

    if not user.is_active:
        return None

    if password_needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)

    return user


_DECOY_HASH: str | None = None


def _decoy_hash() -> str:
    """An Argon2 hash of a value no one knows, computed once per process."""

    global _DECOY_HASH

    if _DECOY_HASH is None:
        _DECOY_HASH = hash_password(generate_token())

    return _DECOY_HASH


def create_session(
    db: Session,
    *,
    user: User,
    settings: Settings,
    now: datetime | None = None,
) -> tuple[UserSession, str]:
    """Open a session. Returns the row and the raw token, in that order.

    The raw token is the only copy; it is returned so the caller can set
    the cookie and is never stored or logged.
    """

    now = now or utcnow()
    token = generate_token()

    user_session = UserSession(
        user_id=user.id,
        token_hash=hash_token(token),
        expires_at=now
        + timedelta(seconds=settings.api_session_lifetime_seconds),
        last_seen_at=now,
        created_at=now,
    )

    db.add(user_session)
    db.flush()

    return user_session, token


def resolve_session(
    db: Session,
    *,
    token: str | None,
    settings: Settings,
    now: datetime | None = None,
) -> AuthenticatedSession | None:
    """Validate a cookie token. None means "not authenticated", no detail.

    A session that is revoked, past its absolute expiry, idle for longer
    than the configured window, or owned by a deactivated user, is
    rejected the same way as a token that was never issued.
    """

    if not token:
        return None

    now = now or utcnow()

    user_session = db.scalars(
        select(UserSession).where(UserSession.token_hash == hash_token(token))
    ).one_or_none()

    if user_session is None:
        return None

    if user_session.revoked_at is not None:
        return None

    if _as_utc(user_session.expires_at) <= now:
        return None

    idle_deadline = _as_utc(user_session.last_seen_at) + timedelta(
        seconds=settings.api_session_idle_seconds
    )

    if idle_deadline <= now:
        return None

    user = db.get(User, user_session.user_id)

    if user is None or not user.is_active:
        return None

    user_session.last_seen_at = now

    return AuthenticatedSession(user=user, session=user_session)


def revoke_session(
    user_session: UserSession, *, now: datetime | None = None
) -> None:
    """Idempotent: revoking twice keeps the first timestamp."""

    if user_session.revoked_at is None:
        user_session.revoked_at = now or utcnow()


def issue_csrf_token(
    user_session: UserSession, *, now: datetime | None = None
) -> str:
    """Mint a CSRF token for this session and store only its digest.

    Issuing replaces any previous token, so a client fetches one and
    reuses it for the lifetime of the session rather than per request.
    """

    token = generate_token()
    user_session.csrf_token_hash = hash_token(token)
    user_session.last_seen_at = now or utcnow()

    return token


def csrf_token_is_valid(
    user_session: UserSession, presented: str | None
) -> bool:
    return tokens_match(user_session.csrf_token_hash, presented)


def _as_utc(value: datetime) -> datetime:
    """Timestamps are stored as timestamptz; be safe if one comes back naive."""

    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)

    return value
