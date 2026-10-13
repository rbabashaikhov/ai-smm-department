"""Social accounts of a project and their write-only credentials.

Who may do what is decided by the caller (the API's owner dependency, or
the operator CLI); this module enforces what must hold whoever calls it:

* an account belongs to one project, for good;
* at most one credential of an account is active, and only that one has
  key material -- superseding or revoking one wipes its ciphertext in the
  same UPDATE that changes its status;
* the first successful connection test binds the account to one external
  identity; every later credential must prove that same identity, or it
  is not stored;
* a plaintext token is a SecretToken from the moment it is received. It
  is sealed with the public key and dropped; it never reaches the
  database, an audit entry, a log line or an exception message.

Every command locks the account row first, so a replace cannot interleave
with a revoke, a disable or another replace.
"""
from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ai_smm.account_routing import account_readiness, active_credential
from ai_smm.application.control_plane import require_mutations_enabled
from ai_smm.db.models import (
    ACCOUNT_STATUS_ACTIVE,
    ACCOUNT_STATUS_DISABLED,
    CREDENTIAL_METADATA_COLUMNS,
    CREDENTIAL_STATUS_ACTIVE,
    CREDENTIAL_STATUS_REVOKED,
    CREDENTIAL_STATUS_SUPERSEDED,
    SOCIAL_PLATFORMS,
    VERIFICATION_UNVERIFIED,
    VERIFICATION_VERIFIED,
    Project,
    Publication,
    PublicationStatus,
    SocialAccount,
    SocialAccountCredential,
    User,
    credential_metadata_only,
)
from ai_smm.queue import record_audit
from ai_smm.security.credentials import (
    CredentialSealer,
    SecretToken,
    associated_data,
    token_fingerprint,
)


MAX_ACCOUNT_NAME_LENGTH = 120
MAX_ACCOUNTS_PER_PROJECT = 20

#: Rows that will be published by the worker unless someone acts.
PENDING_DELIVERY_STATUSES = (
    PublicationStatus.SCHEDULED,
    PublicationStatus.CLAIMED,
)


class AccountUnavailable(Exception):
    """No such account in this project (answered as 404)."""


class AccountVersionConflict(Exception):
    def __init__(self, *, expected: int, current: int) -> None:
        super().__init__("The account was changed by someone else.")

        self.expected = expected
        self.current = current


class AccountLimitReached(Exception):
    pass


class CredentialAccountMismatch(Exception):
    """The token proves a different identity than the account is bound to."""


class AccountAlreadyConnected(Exception):
    """Another account of this project is bound to that identity."""


class CredentialUnavailable(Exception):
    """No such credential in this project (answered as 404)."""


class CredentialNotActive(Exception):
    """The credential exists but was already superseded or revoked."""


@dataclass(frozen=True)
class IdentityResult:
    account_id: str
    username: str | None


#: Callable that tests a token: returns its identity or raises
#: ai_smm.publishing.threads.CredentialRejected / IdentityUnavailable.
IdentityFetcher = Callable[[SecretToken], Any]


@dataclass(frozen=True)
class AccountView:
    account: SocialAccount
    credential: SocialAccountCredential | None
    publishing_ready: bool
    not_ready_reason: str | None
    scheduled_publications: int


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _actor(user: User) -> str:
    return f"user:{user.id}"


def normalize_account_name(raw: str) -> str:
    name = " ".join((raw or "").split())

    if not name:
        raise ValueError("Account name must not be empty.")

    if len(name) > MAX_ACCOUNT_NAME_LENGTH:
        raise ValueError(
            f"Account name must be at most {MAX_ACCOUNT_NAME_LENGTH} "
            "characters."
        )

    return name


def _pending_count(db: Session, account_id: uuid.UUID) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(Publication)
            .where(
                Publication.social_account_id == account_id,
                Publication.status.in_(PENDING_DELIVERY_STATUSES),
            )
        )
        or 0
    )


def account_view(db: Session, account: SocialAccount) -> AccountView:
    credential = active_credential(db, account.id)
    ready, reason = account_readiness(account, credential)

    return AccountView(
        account=account,
        credential=credential,
        publishing_ready=ready,
        not_ready_reason=reason,
        scheduled_publications=_pending_count(db, account.id),
    )


def list_accounts(db: Session, *, project_id: str) -> list[AccountView]:
    accounts = db.scalars(
        select(SocialAccount)
        .where(SocialAccount.project_id == project_id)
        .order_by(SocialAccount.created_at, SocialAccount.id)
    ).all()

    return [account_view(db, account) for account in accounts]


def credential_history(
    db: Session, *, account_id: uuid.UUID
) -> list[SocialAccountCredential]:
    return list(
        db.scalars(
            select(SocialAccountCredential)
            .options(credential_metadata_only())
            .where(SocialAccountCredential.social_account_id == account_id)
            .order_by(SocialAccountCredential.created_at.desc())
        ).all()
    )


def load_credential_metadata(
    db: Session, credential_id: uuid.UUID
) -> SocialAccountCredential | None:
    """A credential row without its key material."""

    return db.scalars(
        select(SocialAccountCredential)
        .options(credential_metadata_only())
        .where(SocialAccountCredential.id == credential_id)
    ).one_or_none()


def get_account(db: Session, account_id: uuid.UUID) -> SocialAccount | None:
    return db.get(SocialAccount, account_id)


def lock_account(
    db: Session, *, account_id: uuid.UUID, expected_project_id: str
) -> SocialAccount:
    account = db.scalars(
        select(SocialAccount)
        .where(SocialAccount.id == account_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()

    if account is None or account.project_id != expected_project_id:
        raise AccountUnavailable("Social account not found.")

    return account


def _check_version(account: SocialAccount, expected: int | None) -> None:
    if expected is not None and account.version != expected:
        raise AccountVersionConflict(
            expected=expected, current=account.version
        )


def _bump(account: SocialAccount, now: datetime) -> None:
    account.version += 1
    account.updated_at = now


# -- accounts ---------------------------------------------------------------


def create_account(
    db: Session,
    *,
    project: Project,
    actor_user: User,
    platform: str,
    display_name: str,
    mutations_enabled: bool,
    now: datetime | None = None,
) -> SocialAccount:
    require_mutations_enabled(mutations_enabled, command="create_account")

    if platform not in SOCIAL_PLATFORMS:
        raise ValueError(
            f"platform must be one of: {', '.join(SOCIAL_PLATFORMS)}."
        )

    name = normalize_account_name(display_name)
    now = now or utcnow()

    count = db.scalar(
        select(func.count())
        .select_from(SocialAccount)
        .where(SocialAccount.project_id == project.id)
    )

    if (count or 0) >= MAX_ACCOUNTS_PER_PROJECT:
        raise AccountLimitReached(
            f"A project may have at most {MAX_ACCOUNTS_PER_PROJECT} accounts."
        )

    account = SocialAccount(
        id=uuid.uuid4(),
        project_id=project.id,
        platform=platform,
        display_name=name,
        status=ACCOUNT_STATUS_ACTIVE,
        version=1,
        created_by_user_id=actor_user.id,
        created_at=now,
        updated_at=now,
    )
    db.add(account)
    db.flush()

    record_audit(
        db,
        actor=_actor(actor_user),
        action="social_account.create",
        subject=f"social_account:{account.id}",
        details={
            "project_id": project.id,
            "platform": platform,
            "display_name": name,
        },
    )

    return account


def rename_account(
    db: Session,
    *,
    account_id: uuid.UUID,
    expected_project_id: str,
    expected_version: int,
    actor_user: User,
    display_name: str,
    mutations_enabled: bool,
    now: datetime | None = None,
) -> SocialAccount:
    require_mutations_enabled(mutations_enabled, command="rename_account")

    name = normalize_account_name(display_name)
    now = now or utcnow()
    account = lock_account(
        db, account_id=account_id, expected_project_id=expected_project_id
    )
    _check_version(account, expected_version)

    before = account.display_name
    account.display_name = name
    _bump(account, now)
    db.flush()

    record_audit(
        db,
        actor=_actor(actor_user),
        action="social_account.rename",
        subject=f"social_account:{account.id}",
        details={"from": before, "to": name, "version": account.version},
    )

    return account


def set_account_status(
    db: Session,
    *,
    account_id: uuid.UUID,
    expected_project_id: str,
    expected_version: int,
    actor_user: User,
    active: bool,
    mutations_enabled: bool,
    now: datetime | None = None,
) -> SocialAccount:
    """Disable or enable. Disabling never touches a queued publication.

    A scheduled row of a disabled account is refused by the worker at
    preflight (permanent failure, nothing sent); the response tells the
    caller how many there are so that they can cancel or wait.
    """

    command = "enable_account" if active else "disable_account"
    require_mutations_enabled(mutations_enabled, command=command)

    now = now or utcnow()
    account = lock_account(
        db, account_id=account_id, expected_project_id=expected_project_id
    )
    _check_version(account, expected_version)

    target = ACCOUNT_STATUS_ACTIVE if active else ACCOUNT_STATUS_DISABLED

    if account.status == target:
        return account

    account.status = target
    account.disabled_at = None if active else now
    _bump(account, now)
    db.flush()

    record_audit(
        db,
        actor=_actor(actor_user),
        action=f"social_account.{'enable' if active else 'disable'}",
        subject=f"social_account:{account.id}",
        details={
            "version": account.version,
            "scheduled_publications": _pending_count(db, account.id),
        },
    )

    return account


# -- credentials ------------------------------------------------------------


def _retire_active(
    db: Session,
    *,
    account_id: uuid.UUID,
    status: str,
    now: datetime,
) -> uuid.UUID | None:
    """Supersede or revoke the active credential and wipe its material."""

    stamp = {
        CREDENTIAL_STATUS_SUPERSEDED: {"superseded_at": now},
        CREDENTIAL_STATUS_REVOKED: {"revoked_at": now},
    }[status]

    retired = db.execute(
        update(SocialAccountCredential)
        .where(
            SocialAccountCredential.social_account_id == account_id,
            SocialAccountCredential.status == CREDENTIAL_STATUS_ACTIVE,
        )
        .values(
            status=status,
            ciphertext=None,
            nonce=None,
            ephemeral_public_key=None,
            **stamp,
        )
        .returning(SocialAccountCredential.id)
        .execution_options(synchronize_session=False)
    ).scalar_one_or_none()

    return retired


def set_credential(
    db: Session,
    *,
    account_id: uuid.UUID,
    expected_project_id: str,
    actor_user: User,
    token: SecretToken,
    sealer: CredentialSealer,
    mutations_enabled: bool,
    verify: bool = True,
    identity_fetcher: IdentityFetcher | None = None,
    expires_at: datetime | None = None,
    actor: str | None = None,
    now: datetime | None = None,
) -> SocialAccountCredential:
    """Store a new active credential, superseding the previous one.

    With verify, the token is tested (GET /me) **before** anything is
    written: a rejected token, an unreachable platform or the identity of
    another account means nothing is stored and the previous credential
    stays active. The first successful test binds the account to that
    identity.
    """

    require_mutations_enabled(mutations_enabled, command="set_credential")

    now = now or utcnow()

    if expires_at is not None and expires_at.tzinfo is None:
        raise ValueError("expires_at must include a timezone offset.")

    if get_account(db, account_id) is None:
        raise AccountUnavailable("Social account not found.")

    identity = None

    if verify:
        if identity_fetcher is None:
            raise ValueError("A connection test needs an identity fetcher.")

        # The network call happens before the row lock is taken, so no
        # writer of this account waits on the platform. Raises
        # CredentialRejected / IdentityUnavailable: nothing stored.
        identity = identity_fetcher(token)

    # Every decision below is made under the lock, on the row as it is now.
    account = lock_account(
        db, account_id=account_id, expected_project_id=expected_project_id
    )

    if identity is not None:
        if (
            account.external_account_id is not None
            and identity.account_id != account.external_account_id
        ):
            raise CredentialAccountMismatch(
                "This token belongs to a different Threads account than the "
                "one this social account is connected to. Nothing was saved."
            )

        if account.external_account_id is None:
            clash = db.scalar(
                select(SocialAccount.id).where(
                    SocialAccount.project_id == account.project_id,
                    SocialAccount.platform == account.platform,
                    SocialAccount.external_account_id == identity.account_id,
                    SocialAccount.id != account.id,
                )
            )

            if clash is not None:
                raise AccountAlreadyConnected(
                    "That Threads account is already connected to another "
                    "account of this project. Nothing was saved."
                )

    previous = _retire_active(
        db,
        account_id=account.id,
        status=CREDENTIAL_STATUS_SUPERSEDED,
        now=now,
    )

    credential_id = uuid.uuid4()
    sealed = sealer.seal(
        token,
        aad=associated_data(
            account_id=str(account.id), credential_id=str(credential_id)
        ),
    )

    credential = SocialAccountCredential(
        id=credential_id,
        social_account_id=account.id,
        status=CREDENTIAL_STATUS_ACTIVE,
        scheme=sealed.scheme,
        key_id=sealed.key_id,
        ephemeral_public_key=sealed.ephemeral_public_key,
        nonce=sealed.nonce,
        ciphertext=sealed.ciphertext,
        fingerprint=token_fingerprint(token),
        verification_status=(
            VERIFICATION_VERIFIED if identity else VERIFICATION_UNVERIFIED
        ),
        verified_at=now if identity else None,
        expires_at=expires_at,
        created_by_user_id=actor_user.id,
        created_at=now,
    )
    db.add(credential)

    bound_now = False

    if identity is not None and account.external_account_id is None:
        account.external_account_id = identity.account_id
        account.external_username = identity.username
        bound_now = True

    if identity is not None and identity.username:
        account.external_username = identity.username

    _bump(account, now)
    db.flush()

    record_audit(
        db,
        actor=actor or _actor(actor_user),
        action="social_account.credential_set",
        subject=f"social_account:{account.id}",
        details={
            "credential_id": str(credential.id),
            "fingerprint": credential.fingerprint,
            "key_id": credential.key_id,
            "verified": identity is not None,
            "bound_identity": bound_now,
            "replaced_credential_id": str(previous) if previous else None,
        },
    )

    return credential


def revoke_credential(
    db: Session,
    *,
    credential_id: uuid.UUID,
    expected_project_id: str,
    actor_user: User,
    mutations_enabled: bool,
    now: datetime | None = None,
) -> SocialAccountCredential:
    """Revoke the active credential and destroy its ciphertext.

    Revoking here removes the token from this system; it does not revoke
    it at Meta. Scheduled rows of the account then fail at preflight.
    """

    require_mutations_enabled(mutations_enabled, command="revoke_credential")

    now = now or utcnow()
    credential = load_credential_metadata(db, credential_id)

    if credential is None:
        raise CredentialUnavailable("Credential not found.")

    account = lock_account(
        db,
        account_id=credential.social_account_id,
        expected_project_id=expected_project_id,
    )

    db.refresh(credential, attribute_names=list(CREDENTIAL_METADATA_COLUMNS))

    if credential.status != CREDENTIAL_STATUS_ACTIVE:
        raise CredentialNotActive(
            f"The credential is already {credential.status}."
        )

    _retire_active(
        db,
        account_id=account.id,
        status=CREDENTIAL_STATUS_REVOKED,
        now=now,
    )
    _bump(account, now)
    db.flush()
    db.refresh(credential, attribute_names=list(CREDENTIAL_METADATA_COLUMNS))

    record_audit(
        db,
        actor=_actor(actor_user),
        action="social_account.credential_revoke",
        subject=f"social_account:{account.id}",
        details={
            "credential_id": str(credential.id),
            "fingerprint": credential.fingerprint,
            "scheduled_publications": _pending_count(db, account.id),
        },
    )

    return credential
