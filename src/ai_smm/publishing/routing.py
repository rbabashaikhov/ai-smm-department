"""Opening an account's sealed token for the worker. Publishing side only.

The policy -- which account, whether a route is usable at all -- is
ai_smm.account_routing, shared with the control plane. This module adds
the one step the control plane must never be able to take: decrypting a
stored credential with the worker's private key. It is imported by the
publish pipeline, the worker and the operator CLI, never by
ai_smm.api or ai_smm.application.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ai_smm.account_routing import (  # noqa: F401  (re-exported)
    LEGACY_PLATFORM,
    REASON_ACCOUNT_DISABLED,
    REASON_ACCOUNT_MISSING,
    REASON_ACCOUNT_OTHER_PROJECT,
    REASON_ACCOUNT_PLATFORM,
    REASON_ACCOUNT_UNVERIFIED,
    REASON_DECRYPT,
    REASON_KEY_MISSING,
    REASON_LEGACY_NOT_ALLOWED,
    REASON_NO_CREDENTIAL,
    ROUTE_ACCOUNT,
    ROUTE_LEGACY,
    PublishRoute,
    RouteUnavailable,
    account_readiness,
    active_credential,
    check_route_ready,
    legacy_route_allowed,
)
from ai_smm.config import Settings
from ai_smm.db.models import (
    CREDENTIAL_STATUS_ACTIVE,
    Publication,
    SocialAccountCredential,
)
from ai_smm.logging_setup import register_runtime_secret
from ai_smm.security.credentials import (
    CredentialCryptoError,
    CredentialKeyMissing,
    CredentialOpener,
    SealedCredential,
    associated_data,
    load_private_key_file,
)


def build_opener(settings: Settings) -> CredentialOpener | None:
    """The worker's decryption keys, or None when none are configured."""

    if not settings.credentials_private_key_files:
        return None

    return CredentialOpener(
        [
            load_private_key_file(path)
            for path in settings.credentials_private_key_files
        ]
    )


def resolve_route(
    session: Session,
    publication: Publication,
    settings: Settings,
    *,
    opener: CredentialOpener | None,
) -> PublishRoute:
    """The route with its token, ready to build a publisher."""

    route = check_route_ready(session, publication, settings)

    if not route.is_account:
        return route

    if opener is None:
        raise RouteUnavailable(
            REASON_KEY_MISSING,
            f"Publication {publication.id} is bound to a social account, "
            "but no credential private key is configured for this worker "
            "(AI_SMM_CREDENTIALS_PRIVATE_KEY_FILES).",
        )

    credential = session.get(SocialAccountCredential, route.credential_id)

    # Fail closed, explicitly (an assert would vanish under python -O): the
    # credential may have been revoked or replaced since check_route_ready
    # read it, and a retired row carries no key material. Nothing else is
    # tried -- in particular never the deployment token.
    if (
        credential is None
        or credential.status != CREDENTIAL_STATUS_ACTIVE
        or credential.social_account_id != route.social_account_id
        or credential.ciphertext is None
        or credential.nonce is None
        or credential.ephemeral_public_key is None
    ):
        raise RouteUnavailable(
            REASON_NO_CREDENTIAL,
            f"Publication {publication.id}: the active credential of its "
            "social account is gone (revoked or replaced); nothing was sent.",
        )

    try:
        token = opener.open(
            SealedCredential(
                scheme=credential.scheme,
                key_id=credential.key_id,
                ephemeral_public_key=bytes(credential.ephemeral_public_key),
                nonce=bytes(credential.nonce),
                ciphertext=bytes(credential.ciphertext),
            ),
            aad=associated_data(
                account_id=str(credential.social_account_id),
                credential_id=str(credential.id),
            ),
        )
    except CredentialKeyMissing as exc:
        raise RouteUnavailable(
            REASON_KEY_MISSING,
            f"Publication {publication.id}: the credential of its social "
            f"account was sealed with a key this worker does not hold "
            f"({exc}).",
        ) from exc
    except CredentialCryptoError as exc:
        raise RouteUnavailable(
            REASON_DECRYPT,
            f"Publication {publication.id}: the credential of its social "
            f"account could not be decrypted ({exc}).",
        ) from exc

    # From now on this value exists in the process; make sure no log
    # record can carry it, whatever code ends up printing what.
    register_runtime_secret(token.reveal())

    return PublishRoute(
        kind=ROUTE_ACCOUNT,
        project_id=route.project_id,
        platform=route.platform,
        social_account_id=route.social_account_id,
        credential_id=route.credential_id,
        expected_external_id=route.expected_external_id,
        token=token,
    )
