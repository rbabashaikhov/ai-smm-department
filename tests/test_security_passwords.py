"""Argon2id hashing: the one place a password is turned into a hash."""
from __future__ import annotations

import pytest

from ai_smm.security.passwords import (
    MIN_PASSWORD_LENGTH,
    WeakPasswordError,
    hash_password,
    validate_password_strength,
    verify_password,
)


PASSWORD = "correct-horse-battery-staple"


def test_hash_is_argon2id() -> None:
    encoded = hash_password(PASSWORD)

    assert encoded.startswith("$argon2id$")


def test_hash_does_not_contain_the_password() -> None:
    encoded = hash_password(PASSWORD)

    assert PASSWORD not in encoded


def test_verify_accepts_the_right_password() -> None:
    assert verify_password(hash_password(PASSWORD), PASSWORD) is True


def test_verify_rejects_the_wrong_password() -> None:
    assert verify_password(hash_password(PASSWORD), PASSWORD + "x") is False


def test_hashes_are_salted() -> None:
    assert hash_password(PASSWORD) != hash_password(PASSWORD)


@pytest.mark.parametrize(
    "stored", ["", "not-a-hash", "$argon2id$v=19$truncated"]
)
def test_verify_rejects_a_damaged_hash(stored: str) -> None:
    """A corrupt row must not become a way in, and must not raise."""

    assert verify_password(stored, PASSWORD) is False


def test_verify_rejects_an_empty_password() -> None:
    assert verify_password(hash_password(PASSWORD), "") is False


def test_empty_password_cannot_be_hashed() -> None:
    with pytest.raises(WeakPasswordError):
        hash_password("")


def test_strength_check_enforces_a_minimum_length() -> None:
    with pytest.raises(WeakPasswordError):
        validate_password_strength("x" * (MIN_PASSWORD_LENGTH - 1))

    validate_password_strength("x" * MIN_PASSWORD_LENGTH)
