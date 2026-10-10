"""Password hashing.

Argon2id via argon2-cffi. Nothing here implements cryptography: the
parameters are the library defaults, which follow the current OWASP
recommendation, and the only local decisions are the minimum length a new
password must have and the refusal to hash an empty string.
"""
from __future__ import annotations

from argon2 import PasswordHasher, Type
from argon2.exceptions import (
    InvalidHashError,
    VerificationError,
    VerifyMismatchError,
)


#: Shortest password the operator command will accept. Length is the only
#: rule enforced: a composition rule pushes operators towards patterns a
#: cracker already knows.
MIN_PASSWORD_LENGTH = 12

#: Explicitly Argon2id (also the library default) so a future default
#: change cannot silently move us to another variant.
_hasher = PasswordHasher(type=Type.ID)


class WeakPasswordError(ValueError):
    """The supplied password is shorter than MIN_PASSWORD_LENGTH."""


def validate_password_strength(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise WeakPasswordError(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
        )


def hash_password(password: str) -> str:
    """Return an Argon2id encoded hash. The password is never logged."""

    if not password:
        raise WeakPasswordError("Password must not be empty.")

    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """True when the password matches. Never raises on a wrong password.

    A malformed or truncated stored hash counts as "does not match", so a
    damaged row cannot be turned into a login.
    """

    if not password_hash or not password:
        return False

    try:
        return _hasher.verify(password_hash, password)
    except (
        VerifyMismatchError,
        VerificationError,
        InvalidHashError,
    ):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    """True when the stored hash was made with weaker parameters."""

    try:
        return _hasher.check_needs_rehash(password_hash)
    except (InvalidHashError, VerificationError):
        return False
