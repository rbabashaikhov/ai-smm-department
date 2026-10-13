"""Social accounts of a project and their write-only credentials.

Reading account metadata needs viewer: an editor has to see where content
can go in order to choose a destination. Everything that changes which
external identity a project speaks as -- creating, renaming, disabling an
account, setting or revoking a credential -- needs owner.

A credential goes in and never comes out. CredentialIn carries the token
as a SecretStr; it is turned into a SecretToken, tested against the
platform when asked to, sealed with the public key and dropped. No
response carries it, and the API process holds no key that could open
what it stored.
"""
from __future__ import annotations

from fastapi import APIRouter, Request

from ai_smm.api.dependencies import (
    AppSettings,
    DbSession,
    OwnerCredential,
    OwnerProject,
    OwnerSocialAccount,
    ViewerProject,
    ViewerSocialAccount,
)
from ai_smm.api.errors import ApiError, ErrorCode, not_found
from ai_smm.api.mutation_gate import mutations_disabled
from ai_smm.api.schemas import (
    CredentialIn,
    CredentialOut,
    SocialAccountCreate,
    SocialAccountDetailOut,
    SocialAccountOut,
    SocialAccountPatch,
    SocialAccountStatusIn,
)
from ai_smm.application.control_plane import MutationsDisabled
from ai_smm.application.social_accounts import (
    AccountAlreadyConnected,
    AccountLimitReached,
    AccountUnavailable,
    AccountVersionConflict,
    AccountView,
    CredentialAccountMismatch,
    CredentialNotActive,
    CredentialUnavailable,
    account_view,
    create_account,
    credential_history,
    list_accounts,
    rename_account,
    revoke_credential,
    set_account_status,
    set_credential,
)
from ai_smm.config import Settings
from ai_smm.db.models import SocialAccountCredential
from ai_smm.platform_identity import CredentialRejected, IdentityUnavailable
from ai_smm.security.credentials import (
    CredentialCryptoError,
    CredentialSealer,
    decode_public_key,
    normalize_token,
)


router = APIRouter(tags=["social-accounts"])


# -- reads ---------------------------------------------------------------


@router.get(
    "/projects/{project_id}/social-accounts",
    response_model=list[SocialAccountOut],
    summary="Accounts of a project with publishing readiness (viewer+)",
)
def list_project_accounts(
    db: DbSession, context: ViewerProject
) -> list[SocialAccountOut]:
    return [
        _account_out(view)
        for view in list_accounts(db, project_id=context.project.id)
    ]


@router.get(
    "/social-accounts/{account_id}",
    response_model=SocialAccountDetailOut,
    summary="One account and its credential history, metadata only (viewer+)",
)
def get_account(db: DbSession, context: ViewerSocialAccount) -> SocialAccountDetailOut:
    view = account_view(db, context.account)

    return SocialAccountDetailOut(
        **_account_out(view).model_dump(),
        credentials=[
            _credential_out(row)
            for row in credential_history(db, account_id=context.account.id)
        ],
    )


# -- accounts (owner) --------------------------------------------------------


@router.post(
    "/projects/{project_id}/social-accounts",
    response_model=SocialAccountOut,
    status_code=201,
    summary="Add an account to the project (owner)",
)
def create(
    payload: SocialAccountCreate,
    db: DbSession,
    settings: AppSettings,
    context: OwnerProject,
) -> SocialAccountOut:
    account = _run(
        lambda: create_account(
            db,
            project=context.project,
            actor_user=context.authenticated.user,
            platform=payload.platform,
            display_name=payload.display_name,
            mutations_enabled=settings.api_mutations_enabled,
        )
    )

    return _account_out(account_view(db, account))


@router.patch(
    "/social-accounts/{account_id}",
    response_model=SocialAccountOut,
    summary="Rename an account (owner)",
)
def rename(
    payload: SocialAccountPatch,
    db: DbSession,
    settings: AppSettings,
    context: OwnerSocialAccount,
) -> SocialAccountOut:
    account = _run(
        lambda: rename_account(
            db,
            account_id=context.account.id,
            expected_project_id=context.project.id,
            expected_version=payload.expected_version,
            actor_user=context.user,
            display_name=payload.display_name,
            mutations_enabled=settings.api_mutations_enabled,
        )
    )

    return _account_out(account_view(db, account))


@router.post(
    "/social-accounts/{account_id}/disable",
    response_model=SocialAccountOut,
    summary="Disable an account; its scheduled rows will not be published (owner)",
)
def disable(
    payload: SocialAccountStatusIn,
    db: DbSession,
    settings: AppSettings,
    context: OwnerSocialAccount,
) -> SocialAccountOut:
    return _set_status(payload, db, settings, context, active=False)


@router.post(
    "/social-accounts/{account_id}/enable",
    response_model=SocialAccountOut,
    summary="Enable an account again (owner)",
)
def enable(
    payload: SocialAccountStatusIn,
    db: DbSession,
    settings: AppSettings,
    context: OwnerSocialAccount,
) -> SocialAccountOut:
    return _set_status(payload, db, settings, context, active=True)


def _set_status(payload, db, settings, context, *, active: bool):
    account = _run(
        lambda: set_account_status(
            db,
            account_id=context.account.id,
            expected_project_id=context.project.id,
            expected_version=payload.expected_version,
            actor_user=context.user,
            active=active,
            mutations_enabled=settings.api_mutations_enabled,
        )
    )

    return _account_out(account_view(db, account))


# -- credentials (owner, write-only) ----------------------------------------


@router.post(
    "/social-accounts/{account_id}/credentials",
    response_model=CredentialOut,
    status_code=201,
    summary=(
        "Store a new token for the account, replacing the active one. "
        "Write-only: the token is never returned (owner)"
    ),
)
def put_credential(
    payload: CredentialIn,
    request: Request,
    db: DbSession,
    settings: AppSettings,
    context: OwnerSocialAccount,
) -> CredentialOut:
    if not settings.api_mutations_enabled:
        # Before the token is even parsed.
        raise mutations_disabled("set_credential")

    sealer = _sealer(settings)

    try:
        token = normalize_token(payload.access_token.get_secret_value())
    except ValueError as exc:
        raise ApiError(
            status_code=422,
            code=ErrorCode.VALIDATION_ERROR,
            message=str(exc),
            details={"fields": [{"field": "access_token", "rule": "format"}]},
        ) from None

    credential = _run(
        lambda: set_credential(
            db,
            account_id=context.account.id,
            expected_project_id=context.project.id,
            actor_user=context.user,
            token=token,
            sealer=sealer,
            mutations_enabled=settings.api_mutations_enabled,
            verify=payload.verify,
            identity_fetcher=request.app.state.identity_fetcher,
            expires_at=payload.expires_at,
        )
    )

    return _credential_out(credential)


@router.post(
    "/social-account-credentials/{credential_id}/revoke",
    response_model=CredentialOut,
    summary="Revoke a credential and destroy its stored ciphertext (owner)",
)
def revoke(
    db: DbSession,
    settings: AppSettings,
    context: OwnerCredential,
) -> CredentialOut:
    credential = _run(
        lambda: revoke_credential(
            db,
            credential_id=context.credential.id,
            expected_project_id=context.project.id,
            actor_user=context.user,
            mutations_enabled=settings.api_mutations_enabled,
        )
    )

    return _credential_out(credential)


# -- translation ---------------------------------------------------------


def _sealer(settings: Settings) -> CredentialSealer:
    if not settings.credentials_public_key:
        raise ApiError(
            status_code=503,
            code=ErrorCode.CREDENTIALS_NOT_CONFIGURED,
            message=(
                "This deployment has no credential encryption key "
                "(AI_SMM_CREDENTIALS_PUBLIC_KEY). Nothing was stored."
            ),
        )

    try:
        return CredentialSealer(decode_public_key(settings.credentials_public_key))
    except CredentialCryptoError as exc:
        raise ApiError(
            status_code=503,
            code=ErrorCode.CREDENTIALS_NOT_CONFIGURED,
            message="The credential encryption key is not valid.",
        ) from exc


def _run(command):
    try:
        return command()
    except MutationsDisabled as exc:
        raise mutations_disabled(exc.command) from exc
    except (AccountUnavailable, CredentialUnavailable) as exc:
        raise not_found(str(exc)) from exc
    except CredentialNotActive as exc:
        raise ApiError(
            status_code=409,
            code=ErrorCode.INVALID_STATE_TRANSITION,
            message=str(exc),
        ) from exc
    except AccountVersionConflict as exc:
        raise ApiError(
            status_code=409,
            code=ErrorCode.VERSION_CONFLICT,
            message=str(exc),
            details={
                "expected_version": exc.expected,
                "current_version": exc.current,
            },
        ) from exc
    except AccountLimitReached as exc:
        raise ApiError(
            status_code=409,
            code=ErrorCode.ACCOUNT_LIMIT_REACHED,
            message=str(exc),
        ) from exc
    except CredentialAccountMismatch as exc:
        raise ApiError(
            status_code=409,
            code=ErrorCode.CREDENTIAL_ACCOUNT_MISMATCH,
            message=str(exc),
        ) from exc
    except AccountAlreadyConnected as exc:
        raise ApiError(
            status_code=409,
            code=ErrorCode.ACCOUNT_ALREADY_CONNECTED,
            message=str(exc),
        ) from exc
    except CredentialRejected as exc:
        raise ApiError(
            status_code=422,
            code=ErrorCode.CREDENTIAL_REJECTED,
            message=f"{exc} Nothing was saved.",
        ) from None
    except IdentityUnavailable as exc:
        raise ApiError(
            status_code=502,
            code=ErrorCode.DEPENDENCY_UNAVAILABLE,
            message=(
                f"The connection test could not reach the platform ({exc}). "
                "Nothing was saved."
            ),
        ) from None
    except ValueError as exc:
        raise ApiError(
            status_code=422,
            code=ErrorCode.VALIDATION_ERROR,
            message=str(exc),
        ) from exc


def _credential_out(row: SocialAccountCredential) -> CredentialOut:
    return CredentialOut(
        id=row.id,
        social_account_id=row.social_account_id,
        status=row.status,
        fingerprint=row.fingerprint,
        key_id=row.key_id,
        verification_status=row.verification_status,
        verified_at=row.verified_at,
        expires_at=row.expires_at,
        created_by_user_id=row.created_by_user_id,
        created_at=row.created_at,
        superseded_at=row.superseded_at,
        revoked_at=row.revoked_at,
    )


def _account_out(view: AccountView) -> SocialAccountOut:
    account = view.account

    return SocialAccountOut(
        id=account.id,
        project_id=account.project_id,
        platform=account.platform,
        display_name=account.display_name,
        status=account.status,
        external_account_id=account.external_account_id,
        external_username=account.external_username,
        version=account.version,
        created_at=account.created_at,
        updated_at=account.updated_at,
        disabled_at=account.disabled_at,
        credential=(
            _credential_out(view.credential) if view.credential else None
        ),
        publishing_ready=view.publishing_ready,
        not_ready_reason=view.not_ready_reason,
        scheduled_publications=view.scheduled_publications,
    )
