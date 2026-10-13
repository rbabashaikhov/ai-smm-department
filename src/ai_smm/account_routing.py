"""Which account a publication is published as -- the policy, no secrets.

This module and ai_smm.publishing.routing are the only places that decide
it. This half is pure: it reads rows and configuration, and it never
decrypts. The control plane (API, application services) imports only
this; the worker-side half, which opens the sealed token, lives in
ai_smm.publishing.routing and is out of the control plane's reach
(tests/test_api_contract.py). The rules:

* A publication with ``social_account_id`` is published **only** as that
  account, with that account's own active credential, decrypted here
  with the worker's private key. If anything about that route is wrong
  -- account disabled, credential missing or revoked, key absent,
  ciphertext not authentic -- the result is :class:`RouteUnavailable`.
  There is no second choice: the deployment token is never tried.
* A publication without one is a **legacy** row and keeps its old
  meaning -- the deployment ``THREADS_ACCESS_TOKEN`` -- but only for a
  project the operator lists in ``AI_SMM_LEGACY_THREADS_PROJECTS``. A
  project created through the Control Center is never on that list.

check_route_ready() applies the same rules without decrypting; the API
and the CLI run it before a row may be scheduled, so a person gets an
early, precise refusal. The worker runs resolve_route() again at publish
time, because the state can change in between.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_smm.config import Settings
from ai_smm.db.models import (
    CREDENTIAL_STATUS_ACTIVE,
    Publication,
    SocialAccount,
    SocialAccountCredential,
    credential_metadata_only,
)
from ai_smm.security.credentials import SecretToken


ROUTE_LEGACY = "legacy"
ROUTE_ACCOUNT = "account"

#: Stable reason codes, for logs, audit and the API's error details.
REASON_LEGACY_NOT_ALLOWED = "legacy_project_not_allowed"
REASON_PLATFORM = "unsupported_platform"
REASON_ACCOUNT_MISSING = "account_missing"
REASON_ACCOUNT_OTHER_PROJECT = "account_in_other_project"
REASON_ACCOUNT_PLATFORM = "account_platform_mismatch"
REASON_ACCOUNT_DISABLED = "account_disabled"
REASON_ACCOUNT_UNVERIFIED = "account_identity_unknown"
REASON_NO_CREDENTIAL = "no_active_credential"
REASON_KEY_MISSING = "credential_key_missing"
REASON_DECRYPT = "credential_not_authentic"

LEGACY_PLATFORM = "threads"


class RouteUnavailable(Exception):
    """The row must not be published now; a person has to act.

    The message names the reason and never contains secret material.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)

        self.code = code


@dataclass(frozen=True)
class PublishRoute:
    kind: str
    project_id: str
    platform: str
    social_account_id: uuid.UUID | None = None
    credential_id: uuid.UUID | None = None
    #: The identity the token must prove before anything is sent.
    expected_external_id: str | None = None
    #: Only on an account route resolved with decryption.
    token: SecretToken | None = field(default=None, repr=False)

    @property
    def is_account(self) -> bool:
        return self.kind == ROUTE_ACCOUNT

    def describe(self) -> dict[str, Any]:
        """Safe to log and to store in an attempt's details."""

        return {
            "route": self.kind,
            "social_account_id": (
                str(self.social_account_id) if self.social_account_id else None
            ),
            "credential_id": (
                str(self.credential_id) if self.credential_id else None
            ),
        }


def legacy_route_allowed(
    settings: Settings, *, project_id: str, platform: str
) -> bool:
    return (
        platform == LEGACY_PLATFORM
        and project_id in settings.legacy_threads_projects
    )


def active_credential(
    session: Session, account_id: uuid.UUID
) -> SocialAccountCredential | None:
    # Metadata only: the policy never needs key material, and the API's
    # database role may not read it. The worker loads the material itself
    # (ai_smm.publishing.routing.resolve_route).
    return session.scalars(
        select(SocialAccountCredential)
        .options(credential_metadata_only())
        .where(
            SocialAccountCredential.social_account_id == account_id,
            SocialAccountCredential.status == CREDENTIAL_STATUS_ACTIVE,
        )
    ).one_or_none()


def account_readiness(
    account: SocialAccount,
    credential: SocialAccountCredential | None,
) -> tuple[bool, str | None]:
    """Whether an account can publish at all, and if not, why not."""

    if not account.is_active:
        return False, REASON_ACCOUNT_DISABLED

    if not account.external_account_id:
        return False, REASON_ACCOUNT_UNVERIFIED

    if credential is None:
        return False, REASON_NO_CREDENTIAL

    return True, None


_MESSAGES = {
    REASON_ACCOUNT_DISABLED: "The social account is disabled.",
    REASON_ACCOUNT_UNVERIFIED: (
        "The social account has no verified identity yet; save a token "
        "with the connection test first."
    ),
    REASON_NO_CREDENTIAL: "The social account has no active credential.",
}


def _account_and_credential(
    session: Session, publication: Publication
) -> tuple[SocialAccount, SocialAccountCredential]:
    account = session.get(SocialAccount, publication.social_account_id)

    if account is None:
        raise RouteUnavailable(
            REASON_ACCOUNT_MISSING,
            f"Publication {publication.id} names a social account that "
            "does not exist.",
        )

    # The composite foreign key already guarantees this; checked again
    # because the cost of being wrong is a post on another project's
    # account.
    if account.project_id != publication.project_id:
        raise RouteUnavailable(
            REASON_ACCOUNT_OTHER_PROJECT,
            f"Publication {publication.id} names an account of another "
            "project.",
        )

    if account.platform != publication.platform:
        raise RouteUnavailable(
            REASON_ACCOUNT_PLATFORM,
            f"Publication {publication.id} is for {publication.platform}, "
            f"its account is on {account.platform}.",
        )

    credential = active_credential(session, account.id)
    ready, reason = account_readiness(account, credential)

    # Explicit refusals, not asserts: they must hold under python -O too.
    if not ready or credential is None:
        code = reason or REASON_NO_CREDENTIAL
        raise RouteUnavailable(
            code,
            f"Publication {publication.id}: {_MESSAGES[code]}",
        )

    return account, credential


def check_route_ready(
    session: Session, publication: Publication, settings: Settings
) -> PublishRoute:
    """The route this row would take, without decrypting anything."""

    if publication.social_account_id is None:
        if not legacy_route_allowed(
            settings,
            project_id=publication.project_id,
            platform=publication.platform,
        ):
            raise RouteUnavailable(
                REASON_LEGACY_NOT_ALLOWED,
                f"Publication {publication.id} has no social account, and "
                f"project {publication.project_id!r} is not allowed to "
                "publish with the deployment account "
                "(AI_SMM_LEGACY_THREADS_PROJECTS). Choose an account for "
                "the content and approve it again.",
            )

        return PublishRoute(
            kind=ROUTE_LEGACY,
            project_id=publication.project_id,
            platform=publication.platform,
        )

    account, credential = _account_and_credential(session, publication)

    return PublishRoute(
        kind=ROUTE_ACCOUNT,
        project_id=publication.project_id,
        platform=publication.platform,
        social_account_id=account.id,
        credential_id=credential.id,
        expected_external_id=account.external_account_id,
    )
