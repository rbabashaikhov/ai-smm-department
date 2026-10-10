"""Opaque tokens, their storage form, and the CSRF token derived from one.

A session token is generated with secrets.token_urlsafe and handed to the
client once. Only its SHA-256 digest is stored, so the database never
holds a credential that could be replayed. Comparison is constant time.

The CSRF token is not a second stored secret: it is an HMAC of the
session token, so it is the same value for the life of the session and is
recomputed from the cookie on every request. See derive_csrf_token.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets


#: 32 bytes of entropy; token_urlsafe returns ~43 characters for this.
TOKEN_BYTES = 32


def generate_token() -> str:
    """A cryptographically secure, URL-safe opaque token."""

    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    """The form that is safe to store: SHA-256 hex of the raw token.

    A plain digest is correct here (unlike for a password): the token has
    full machine entropy, so there is nothing to brute force and no salt
    or work factor would add anything.
    """

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


#: Domain separator. It keeps the CSRF token distinct from any other value
#: that might one day be derived from the same session token, and the
#: version suffix leaves room to change the derivation without silently
#: accepting both forms.
CSRF_CONTEXT = b"ai-smm-csrf-v1"


def derive_csrf_token(session_token: str) -> str:
    """The CSRF token belonging to this session token.

    Derived rather than generated and stored, which buys three things:

    * it is stable, so two browser tabs of one session get the same
      working token and neither can invalidate the other's;
    * it is one-way, so handing the token to page scripts or putting it
      in a header does not expose the session cookie it came from;
    * it needs no storage and dies with the session: validating it
      requires the cookie, so a revoked or expired session has no valid
      CSRF token any more.

    The session token has full machine entropy, so it is a usable HMAC
    key as it stands.
    """

    return hmac.new(
        session_token.encode("utf-8"), CSRF_CONTEXT, hashlib.sha256
    ).hexdigest()


def tokens_equal(expected: str | None, presented: str | None) -> bool:
    """Constant-time comparison of two tokens of the same kind."""

    if not expected or not presented:
        return False

    return hmac.compare_digest(expected, presented)
