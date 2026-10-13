"""Sealed-box encryption of social account credentials.

The split of keys is the security boundary:

* the **public key** is all the HTTP API holds. It can encrypt a token a
  person pastes in, and nothing else: a compromised API, a leaked API
  database role or a dump of the database yields ciphertext it cannot
  open;
* the **private key** lives in a file outside the database, readable by
  the worker (and the operator CLI that runs next to it) only. It is the
  one place a stored token is ever decrypted.

The construction is the usual sealed box: a fresh X25519 key pair per
message, ECDH with the recipient's public key, HKDF-SHA256 to a 256-bit
key, AES-256-GCM. The associated data binds a ciphertext to the account
and credential row it was written for, so moving a ciphertext to another
row -- the one way to make a token publish as a different account without
knowing it -- fails authentication instead of decrypting.

A plaintext token exists only inside :class:`SecretToken`, whose repr and
str never show it.
"""
from __future__ import annotations

import base64
import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


#: Bumped if the construction ever changes; stored with every ciphertext.
SCHEME = "x25519-hkdf-sha256-aes256gcm-v1"

_HKDF_INFO = b"ai-smm social credential v1"
_NONCE_BYTES = 12
_KEY_BYTES = 32

#: Shortest and longest token accepted. Meta tokens are well inside this.
MIN_TOKEN_LENGTH = 16
MAX_TOKEN_LENGTH = 4096


class CredentialCryptoError(Exception):
    """A credential could not be encrypted or decrypted.

    The message never contains key material or plaintext.
    """


class CredentialKeyMissing(CredentialCryptoError):
    """The key needed for this operation is not configured."""


class SecretToken:
    """A plaintext token that refuses to be printed.

    Only reveal() returns the value. repr, str and format show a mask, so
    the token cannot reach a log line, an exception message or a test
    report by accident.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "SecretToken(***)"

    __str__ = __repr__

    def __format__(self, spec: str) -> str:
        return repr(self)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, SecretToken) and other._value == self._value

    def __hash__(self) -> int:
        return hash(("SecretToken", self._value))

    def __reduce__(self):  # pragma: no cover - defensive
        raise TypeError("SecretToken cannot be pickled.")


def normalize_token(raw: str | None) -> SecretToken:
    """Trim and check the shape. Raises ValueError without echoing it."""

    value = (raw or "").strip()

    if len(value) < MIN_TOKEN_LENGTH:
        raise ValueError("The token is too short.")

    if len(value) > MAX_TOKEN_LENGTH:
        raise ValueError("The token is too long.")

    if any(ch.isspace() for ch in value) or not value.isprintable():
        raise ValueError("The token contains whitespace or control characters.")

    return SecretToken(value)


def token_fingerprint(token: SecretToken) -> str:
    """A short, non-reversible label for a token: 12 hex chars of SHA-256.

    Lets a person tell two tokens apart in the UI without any character
    of the token itself being shown.
    """

    return hashlib.sha256(token.reveal().encode("utf-8")).hexdigest()[:12]


def associated_data(*, account_id: str, credential_id: str) -> bytes:
    return f"ai-smm:social-credential:{account_id}:{credential_id}".encode()


# -- keys -------------------------------------------------------------------


def _raw_public(key: X25519PublicKey) -> bytes:
    return key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def key_id_for(public_key: X25519PublicKey) -> str:
    """Identifies which key pair sealed a ciphertext; derived, not secret."""

    return hashlib.sha256(_raw_public(public_key)).hexdigest()[:16]


def encode_public_key(public_key: X25519PublicKey) -> str:
    return base64.b64encode(_raw_public(public_key)).decode("ascii")


def decode_public_key(encoded: str) -> X25519PublicKey:
    try:
        raw = base64.b64decode(encoded.strip(), validate=True)
    except (ValueError, TypeError) as exc:
        raise CredentialCryptoError("The public key is not valid base64.") from exc

    if len(raw) != _KEY_BYTES:
        raise CredentialCryptoError("The public key must be 32 bytes.")

    return X25519PublicKey.from_public_bytes(raw)


def generate_key_pair() -> tuple[X25519PrivateKey, X25519PublicKey]:
    private = X25519PrivateKey.generate()

    return private, private.public_key()


def private_key_to_pem(private_key: X25519PrivateKey) -> bytes:
    return private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def load_private_key_file(path: Path) -> X25519PrivateKey:
    """Read a PKCS#8 PEM X25519 key, refusing a group/world-readable file."""

    try:
        mode = path.stat().st_mode
    except OSError as exc:
        raise CredentialKeyMissing(
            f"Credential private key file is not readable: {path}"
        ) from exc

    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise CredentialCryptoError(
            f"Credential private key file {path} must not be accessible to "
            "group or others (chmod 600)."
        )

    try:
        key = serialization.load_pem_private_key(
            path.read_bytes(), password=None
        )
    except (OSError, ValueError, TypeError) as exc:
        raise CredentialCryptoError(
            f"Credential private key file {path} is not a valid PEM key."
        ) from exc

    if not isinstance(key, X25519PrivateKey):
        raise CredentialCryptoError(
            f"Credential private key file {path} is not an X25519 key."
        )

    return key


def write_private_key_file(path: Path, private_key: X25519PrivateKey) -> None:
    """Create the file with mode 600 before any byte of the key is written."""

    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)

    with os.fdopen(fd, "wb") as handle:
        handle.write(private_key_to_pem(private_key))


# -- sealing ------------------------------------------------------------------


@dataclass(frozen=True)
class SealedCredential:
    """What is stored. None of it is secret without the private key."""

    scheme: str
    key_id: str
    ephemeral_public_key: bytes
    nonce: bytes
    ciphertext: bytes


def _derive(shared: bytes, ephemeral: bytes, recipient: bytes) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=_KEY_BYTES,
        salt=ephemeral + recipient,
        info=_HKDF_INFO,
    ).derive(shared)


class CredentialSealer:
    """Encrypts with the public key. All the API is ever given."""

    def __init__(self, public_key: X25519PublicKey) -> None:
        self._public_key = public_key
        self.key_id = key_id_for(public_key)

    def seal(self, token: SecretToken, *, aad: bytes) -> SealedCredential:
        ephemeral_private = X25519PrivateKey.generate()
        ephemeral_public = _raw_public(ephemeral_private.public_key())
        recipient = _raw_public(self._public_key)

        key = _derive(
            ephemeral_private.exchange(self._public_key),
            ephemeral_public,
            recipient,
        )
        nonce = os.urandom(_NONCE_BYTES)
        ciphertext = AESGCM(key).encrypt(
            nonce, token.reveal().encode("utf-8"), aad
        )

        return SealedCredential(
            scheme=SCHEME,
            key_id=self.key_id,
            ephemeral_public_key=ephemeral_public,
            nonce=nonce,
            ciphertext=ciphertext,
        )


class CredentialOpener:
    """Decrypts with the private key(s). Worker and operator CLI only.

    Several keys may be configured during a rotation; the ciphertext's
    key_id picks the right one, and there is no trial decryption.
    """

    def __init__(self, private_keys: list[X25519PrivateKey]) -> None:
        if not private_keys:
            raise CredentialKeyMissing("No credential private key configured.")

        self._keys = {
            key_id_for(key.public_key()): key for key in private_keys
        }

    @property
    def key_ids(self) -> frozenset[str]:
        return frozenset(self._keys)

    def open(self, sealed: SealedCredential, *, aad: bytes) -> SecretToken:
        if sealed.scheme != SCHEME:
            raise CredentialCryptoError(
                f"Unsupported credential scheme {sealed.scheme!r}."
            )

        private = self._keys.get(sealed.key_id)

        if private is None:
            raise CredentialKeyMissing(
                f"No private key for credential key id {sealed.key_id}."
            )

        try:
            ephemeral = X25519PublicKey.from_public_bytes(
                sealed.ephemeral_public_key
            )
            key = _derive(
                private.exchange(ephemeral),
                sealed.ephemeral_public_key,
                _raw_public(private.public_key()),
            )
            plaintext = AESGCM(key).decrypt(
                sealed.nonce, sealed.ciphertext, aad
            )
        except (InvalidTag, ValueError) as exc:
            # Wrong key, tampered ciphertext, or a ciphertext moved to a
            # row it was not written for. Indistinguishable on purpose.
            raise CredentialCryptoError(
                "The stored credential failed authentication."
            ) from exc

        return SecretToken(plaintext.decode("utf-8"))
