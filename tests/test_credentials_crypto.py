"""Sealed-box credential encryption (ai_smm.security.credentials).

No database. The tokens here are random test values, never a real
credential.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

import pytest

from ai_smm.security.credentials import (
    SCHEME,
    CredentialCryptoError,
    CredentialKeyMissing,
    CredentialOpener,
    CredentialSealer,
    SealedCredential,
    SecretToken,
    associated_data,
    decode_public_key,
    encode_public_key,
    generate_key_pair,
    key_id_for,
    load_private_key_file,
    normalize_token,
    token_fingerprint,
    write_private_key_file,
)


def _token() -> SecretToken:
    return SecretToken("THQtest" + secrets.token_urlsafe(48))


AAD_A = associated_data(account_id="acc-a", credential_id="cred-1")
AAD_B = associated_data(account_id="acc-b", credential_id="cred-2")


def test_seal_and_open_round_trip() -> None:
    private, public = generate_key_pair()
    token = _token()

    sealed = CredentialSealer(public).seal(token, aad=AAD_A)
    opened = CredentialOpener([private]).open(sealed, aad=AAD_A)

    assert opened == token
    assert sealed.scheme == SCHEME
    assert sealed.key_id == key_id_for(public)
    # Nothing of the token is visible in what is stored.
    assert token.reveal().encode() not in sealed.ciphertext


def test_each_seal_is_different() -> None:
    _, public = generate_key_pair()
    token = _token()
    sealer = CredentialSealer(public)

    first = sealer.seal(token, aad=AAD_A)
    second = sealer.seal(token, aad=AAD_A)

    assert first.ciphertext != second.ciphertext
    assert first.nonce != second.nonce
    assert first.ephemeral_public_key != second.ephemeral_public_key


def test_a_ciphertext_moved_to_another_row_does_not_open() -> None:
    """The associated data binds a ciphertext to its account and row."""

    private, public = generate_key_pair()
    sealed = CredentialSealer(public).seal(_token(), aad=AAD_A)

    with pytest.raises(CredentialCryptoError, match="failed authentication"):
        CredentialOpener([private]).open(sealed, aad=AAD_B)


@pytest.mark.parametrize("field", ["ciphertext", "nonce", "ephemeral_public_key"])
def test_tampering_is_detected(field: str) -> None:
    private, public = generate_key_pair()
    sealed = CredentialSealer(public).seal(_token(), aad=AAD_A)
    value = bytearray(getattr(sealed, field))
    value[0] ^= 0x01
    tampered = SealedCredential(**{**sealed.__dict__, field: bytes(value)})

    with pytest.raises(CredentialCryptoError):
        CredentialOpener([private]).open(tampered, aad=AAD_A)


def test_another_key_pair_cannot_open() -> None:
    _, public = generate_key_pair()
    other_private, _ = generate_key_pair()
    sealed = CredentialSealer(public).seal(_token(), aad=AAD_A)

    with pytest.raises(CredentialKeyMissing):
        CredentialOpener([other_private]).open(sealed, aad=AAD_A)


def test_rotation_opens_with_the_key_named_by_key_id() -> None:
    old_private, old_public = generate_key_pair()
    new_private, new_public = generate_key_pair()
    token_old, token_new = _token(), _token()

    sealed_old = CredentialSealer(old_public).seal(token_old, aad=AAD_A)
    sealed_new = CredentialSealer(new_public).seal(token_new, aad=AAD_B)
    opener = CredentialOpener([new_private, old_private])

    assert opener.open(sealed_old, aad=AAD_A) == token_old
    assert opener.open(sealed_new, aad=AAD_B) == token_new
    assert opener.key_ids == {key_id_for(old_public), key_id_for(new_public)}


def test_an_unknown_scheme_is_refused() -> None:
    private, public = generate_key_pair()
    sealed = CredentialSealer(public).seal(_token(), aad=AAD_A)
    future = SealedCredential(**{**sealed.__dict__, "scheme": "v99"})

    with pytest.raises(CredentialCryptoError, match="Unsupported"):
        CredentialOpener([private]).open(future, aad=AAD_A)


def test_no_private_key_means_no_opener() -> None:
    with pytest.raises(CredentialKeyMissing):
        CredentialOpener([])


def test_secret_token_never_prints_its_value() -> None:
    token = _token()
    raw = token.reveal()

    for rendered in (repr(token), str(token), f"{token}", f"{token!r}", f"{token}"):
        assert raw not in rendered

    try:
        raise ValueError(f"bad token {token}")
    except ValueError as exc:
        assert raw not in str(exc)


def test_fingerprint_is_short_and_contains_no_part_of_the_token() -> None:
    token = _token()
    fingerprint = token_fingerprint(token)

    assert len(fingerprint) == 12
    assert int(fingerprint, 16) >= 0
    assert fingerprint not in token.reveal()
    assert token_fingerprint(SecretToken(token.reveal())) == fingerprint


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "abc12",
        "has space inside the token value",
        "x" * 5000,
        "tab\tinside-0123456789",
    ],
)
def test_malformed_tokens_are_refused_without_echoing_them(raw: str) -> None:
    with pytest.raises(ValueError) as caught:
        normalize_token(raw)

    if raw.strip():
        assert raw.strip() not in str(caught.value)


def test_a_token_is_trimmed() -> None:
    value = "THQ" + "a" * 40

    assert normalize_token(f"  {value}\n").reveal() == value


def test_public_key_round_trips_through_base64() -> None:
    _, public = generate_key_pair()

    decoded = decode_public_key(encode_public_key(public))

    assert key_id_for(decoded) == key_id_for(public)


@pytest.mark.parametrize("encoded", ["not base64!!", "AAAA"])
def test_an_invalid_public_key_is_refused(encoded: str) -> None:
    with pytest.raises(CredentialCryptoError):
        decode_public_key(encoded)


def test_private_key_file_is_created_600_and_never_overwritten(tmp_path: Path) -> None:
    private, public = generate_key_pair()
    path = tmp_path / "credentials.pem"

    write_private_key_file(path, private)

    assert (path.stat().st_mode & 0o777) == 0o600
    assert key_id_for(load_private_key_file(path).public_key()) == key_id_for(public)

    with pytest.raises(FileExistsError):
        write_private_key_file(path, generate_key_pair()[0])


def test_a_group_readable_private_key_file_is_refused(tmp_path: Path) -> None:
    private, _ = generate_key_pair()
    path = tmp_path / "credentials.pem"
    write_private_key_file(path, private)
    os.chmod(path, 0o640)

    with pytest.raises(CredentialCryptoError, match="chmod 600"):
        load_private_key_file(path)


def test_a_missing_private_key_file_is_reported(tmp_path: Path) -> None:
    with pytest.raises(CredentialKeyMissing):
        load_private_key_file(tmp_path / "absent.pem")


def test_a_non_key_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "junk.pem"
    path.write_text("not a key")
    os.chmod(path, 0o600)

    with pytest.raises(CredentialCryptoError, match="not a valid PEM"):
        load_private_key_file(path)
