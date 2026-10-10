"""Opaque tokens and their storage form.

A session token and a CSRF token are generated with secrets.token_urlsafe
and handed to the client once. Only the SHA-256 digest is stored, so the
database never holds a credential that could be replayed. Comparison is
constant time.
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


def tokens_match(stored_hash: str | None, presented_token: str | None) -> bool:
    if not stored_hash or not presented_token:
        return False

    return hmac.compare_digest(stored_hash, hash_token(presented_token))
