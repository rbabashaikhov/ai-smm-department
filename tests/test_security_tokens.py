"""Session tokens, and the CSRF token derived from one.

The CSRF token is not stored anywhere: it is an HMAC of the session
token. These tests pin the three properties that choice is made for --
stability across tabs, one-wayness, and being bound to one session.
"""
from __future__ import annotations

from ai_smm.security.tokens import (
    CSRF_CONTEXT,
    derive_csrf_token,
    generate_token,
    hash_token,
    tokens_equal,
)


def test_generated_tokens_are_unique_and_long() -> None:
    tokens = {generate_token() for _ in range(100)}

    assert len(tokens) == 100
    assert all(len(token) >= 40 for token in tokens)


def test_hashing_is_stable_and_hides_the_token() -> None:
    token = generate_token()

    assert hash_token(token) == hash_token(token)
    assert token not in hash_token(token)
    assert len(hash_token(token)) == 64


def test_the_csrf_token_is_the_same_every_time() -> None:
    """What makes two browser tabs of one session both work."""

    token = generate_token()

    assert derive_csrf_token(token) == derive_csrf_token(token)


def test_each_session_gets_a_different_csrf_token() -> None:
    assert derive_csrf_token(generate_token()) != derive_csrf_token(
        generate_token()
    )


def test_the_csrf_token_does_not_reveal_the_session_token() -> None:
    token = generate_token()
    csrf = derive_csrf_token(token)

    assert csrf != token
    assert token not in csrf
    assert csrf != hash_token(token)
    # An HMAC, not a bare digest of the token: a reader who knows the
    # scheme still cannot recompute it from anything the client leaks.
    assert csrf != hash_token(token + CSRF_CONTEXT.decode())


def test_comparison_rejects_a_mismatch_and_an_empty_value() -> None:
    token = generate_token()
    csrf = derive_csrf_token(token)

    assert tokens_equal(csrf, csrf) is True
    assert tokens_equal(csrf, csrf[:-1] + "0") is False
    assert tokens_equal(csrf, "") is False
    assert tokens_equal(None, csrf) is False
    assert tokens_equal("", "") is False
