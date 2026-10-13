"""Who a Threads token belongs to: GET /me, read-only.

Not part of the publishing layer: it creates nothing, and it is what the
control plane uses to test a token a person enters (and the worker to
prove, before every live publication, that a token is still the
account's). It never logs or raises with the token or a response body.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import httpx

from ai_smm.security.credentials import SecretToken


def threads_api_base_url() -> str:
    return os.getenv(
        "THREADS_API_BASE_URL",
        "https://graph.threads.net",
    ).rstrip("/")


@dataclass(frozen=True)
class ThreadsIdentity:
    """Who a token publishes as. Neither value is secret."""

    account_id: str
    username: str | None


class CredentialRejected(Exception):
    """Threads refused the token itself (expired, revoked, wrong scope)."""


class IdentityUnavailable(Exception):
    """Threads could not be asked: network error or a 5xx."""


def fetch_identity(
    token: SecretToken, *, timeout: float = 15.0
) -> ThreadsIdentity:
    """GET /me?fields=id,username -- read-only, creates nothing.

    Used to test a token when a person enters it and, by the worker, to
    prove before every live publication that the token still belongs to
    the account the row is bound to. Error messages never include the
    token or the response body.
    """

    try:
        response = httpx.get(
            f"{threads_api_base_url()}/me",
            params={"fields": "id,username"},
            headers={"Authorization": f"Bearer {token.reveal()}"},
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        raise IdentityUnavailable(
            f"Threads could not be reached ({type(exc).__name__})."
        ) from None

    if response.status_code >= 500:
        raise IdentityUnavailable(
            f"Threads answered HTTP {response.status_code}."
        )

    if response.status_code >= 400:
        raise CredentialRejected(
            f"Threads rejected the token (HTTP {response.status_code})."
        )

    try:
        payload = response.json()
    except ValueError:
        raise IdentityUnavailable(
            "Threads returned a response that is not JSON."
        ) from None

    account_id = str(payload.get("id") or "").strip()

    if not account_id:
        raise IdentityUnavailable("Threads returned no account id.")

    username = payload.get("username")

    return ThreadsIdentity(
        account_id=account_id,
        username=str(username) if username else None,
    )
