"""Account-aware publishing (SMM-025B B2).

The property under test: a publication is published as its own account,
with that account's own token, or not at all. No failure of an account
route ever falls back to the deployment token, and a legacy row uses the
deployment token only for an allow-listed project.

Every platform call is a fake. No token here is real.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from ai_smm.config import Settings
from ai_smm.db.models import (
    AuditLog,
    Publication,
    PublicationAttempt,
    PublicationStatus,
    PublishingStrategy,
    SocialAccount,
    SocialAccountCredential,
)
from ai_smm.publishing.routing import (
    REASON_ACCOUNT_DISABLED,
    REASON_DECRYPT,
    REASON_KEY_MISSING,
    REASON_LEGACY_NOT_ALLOWED,
    REASON_NO_CREDENTIAL,
    RouteUnavailable,
    build_opener,
    check_route_ready,
)
from ai_smm.publishing.service import publish_claimed_publication
from ai_smm.queue import claim_due_publication
from ai_smm.worker import Worker
from tests.test_publish_service import FakePublisher


class AccountPublishers:
    """Account publisher factory: one FakePublisher per token it is given."""

    def __init__(self) -> None:
        self.tokens: list[str] = []
        self.publishers: list[FakePublisher] = []

    def __call__(self, token) -> FakePublisher:
        self.tokens.append(token.reveal())
        publisher = FakePublisher(
            publish_result={"id": f"1790000000000{len(self.tokens):04d}"}
        )
        self.publishers.append(publisher)

        return publisher

    @property
    def publish_calls(self) -> list[str]:
        return [call for p in self.publishers for call in p.publish_calls]


class LegacyFactory:
    """The deployment-token factory. Any call to it is recorded."""

    def __init__(self) -> None:
        self.calls = 0
        self.publisher = FakePublisher(publish_result={"id": "17900000000009999"})

    def __call__(self) -> FakePublisher:
        self.calls += 1

        return self.publisher


@pytest.fixture
def route(session: Session, account_settings: Settings, identity_service):
    """Claim the due row and publish it through the real service."""

    def run(
        *,
        settings: Settings | None = None,
        legacy: LegacyFactory | None = None,
        accounts: AccountPublishers | None = None,
        opener: Any = "build",
    ):
        effective = settings or account_settings
        legacy = legacy or LegacyFactory()
        accounts = accounts or AccountPublishers()
        claimed = claim_due_publication(
            session, worker_id=effective.worker_id, lease_seconds=900
        )
        assert claimed is not None
        session.commit()

        outcome = publish_claimed_publication(
            session,
            claimed,
            settings=effective,
            publisher_factory=legacy,
            media_root=Path.cwd(),
            account_publisher_factory=accounts,
            identity_fetcher=identity_service,
            opener=build_opener(effective) if opener == "build" else opener,
        )
        session.expire_all()

        return outcome, legacy, accounts, session.get(Publication, claimed.id)

    return run


def _no_legacy(settings: Settings) -> Settings:
    return replace(settings, legacy_threads_projects=())


# -- the account route --------------------------------------------------


def test_an_account_row_is_published_with_its_own_token_only(
    make_account, make_publication, route
) -> None:
    account, token = make_account()
    make_account(display_name="Second account")  # another token exists
    publication = make_publication(social_account_id=account.id)

    outcome, legacy, accounts, stored = route()

    assert outcome.status == "published"
    assert accounts.tokens == [token]
    assert accounts.publish_calls == ["text"]
    assert legacy.calls == 0
    assert stored.status is PublicationStatus.PUBLISHED
    assert stored.id == publication.id


@pytest.mark.parametrize("spoil", ["no_lineage", "text_changed_after_approval"])
def test_the_worker_refuses_a_row_whose_approval_lineage_does_not_hold(
    session: Session, make_account, make_publication, route, identity_service, spoil
) -> None:
    """SMM-025D: asked of the database right before any external call, so a
    row the guards did not see being written (an owner-role path) is caught
    too. A permanent failure; nothing reaches Threads."""

    account, _ = make_account()

    if spoil == "no_lineage":
        publication = make_publication(social_account_id=account.id, lineage=False)
    else:
        publication = make_publication(social_account_id=account.id)
        publication.body = "Текст, которого никто не одобрял."
        session.commit()

    calls_before = list(identity_service.calls)
    outcome, legacy, accounts, stored = route()

    assert outcome.status == "failed"
    assert "Approval lineage does not hold" in outcome.detail
    assert stored.status is PublicationStatus.FAILED
    assert accounts.tokens == [] and accounts.publish_calls == []
    assert legacy.calls == 0
    # Not even the identity check (GET /me) was made.
    assert identity_service.calls == calls_before
    assert stored.id == publication.id


def test_two_accounts_each_get_their_own_token(
    session: Session, make_account, make_publication, route
) -> None:
    first, first_token = make_account()
    second, second_token = make_account()
    make_publication(social_account_id=first.id)
    make_publication(
        social_account_id=second.id,
        scheduled_at=datetime.now(timezone.utc) - timedelta(seconds=10),
    )
    accounts = AccountPublishers()

    route(accounts=accounts)
    route(accounts=accounts)

    assert accounts.tokens == [first_token, second_token]


def test_the_attempt_records_the_route_but_never_the_token(
    session: Session, make_account, make_publication, route
) -> None:
    account, token = make_account()
    publication = make_publication(social_account_id=account.id)

    route()

    attempts = (
        session.query(PublicationAttempt).filter_by(publication_id=publication.id).all()
    )
    audit = session.query(AuditLog).all()

    assert attempts[-1].details["route"] == "account"
    assert attempts[-1].details["social_account_id"] == str(account.id)

    for row in [*attempts, *audit]:
        assert token not in str(getattr(row, "details", ""))
        assert token not in str(getattr(row, "error_message", "") or "")


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("disable", REASON_ACCOUNT_DISABLED),
        ("revoke", REASON_NO_CREDENTIAL),
    ],
)
def test_an_unusable_account_fails_without_any_fallback(
    session: Session,
    make_account,
    make_publication,
    route,
    change: str,
    reason: str,
) -> None:
    account, _ = make_account()
    make_publication(social_account_id=account.id)

    if change == "disable":
        session.execute(
            update(SocialAccount)
            .where(SocialAccount.id == account.id)
            .values(status="disabled", disabled_at=datetime.now(timezone.utc))
        )
    else:
        session.execute(
            update(SocialAccountCredential)
            .where(SocialAccountCredential.social_account_id == account.id)
            .values(
                status="revoked",
                revoked_at=datetime.now(timezone.utc),
                ciphertext=None,
                nonce=None,
                ephemeral_public_key=None,
            )
        )
    session.commit()

    outcome, legacy, accounts, stored = route()

    assert outcome.status == "failed"
    assert reason in outcome.detail
    assert legacy.calls == 0
    assert accounts.tokens == []
    assert stored.status is PublicationStatus.FAILED
    assert stored.threads_post_id is None


def test_a_worker_without_the_private_key_fails_the_row(
    make_account, make_publication, route
) -> None:
    account, _ = make_account()
    make_publication(social_account_id=account.id)

    outcome, legacy, accounts, stored = route(opener=None)

    assert outcome.status == "failed"
    assert REASON_KEY_MISSING in outcome.detail
    assert legacy.calls == 0 and accounts.tokens == []


def test_a_worker_with_another_key_fails_the_row(
    make_account, make_publication, route, tmp_path: Path
) -> None:
    from ai_smm.security.credentials import (
        CredentialOpener,
        generate_key_pair,
    )

    account, _ = make_account()
    make_publication(social_account_id=account.id)

    outcome, legacy, accounts, _ = route(
        opener=CredentialOpener([generate_key_pair()[0]])
    )

    assert outcome.status == "failed"
    assert REASON_KEY_MISSING in outcome.detail
    assert legacy.calls == 0 and accounts.tokens == []


def test_ciphertexts_swapped_between_accounts_open_for_neither(
    session: Session, make_account, make_publication, route
) -> None:
    """A database writer who swaps two sealed tokens gains nothing."""

    first, _ = make_account()
    second, _ = make_account()
    make_publication(social_account_id=first.id)

    rows = {
        row.social_account_id: row
        for row in session.query(SocialAccountCredential).all()
    }
    a, b = rows[first.id], rows[second.id]
    material_a = (a.ephemeral_public_key, a.nonce, a.ciphertext)
    material_b = (b.ephemeral_public_key, b.nonce, b.ciphertext)
    a.ephemeral_public_key, a.nonce, a.ciphertext = material_b
    b.ephemeral_public_key, b.nonce, b.ciphertext = material_a
    session.commit()

    outcome, legacy, accounts, stored = route()

    assert outcome.status == "failed"
    assert REASON_DECRYPT in outcome.detail
    assert accounts.tokens == [] and legacy.calls == 0


# -- identity check -------------------------------------------------------


def test_a_token_of_another_identity_is_never_used_to_publish(
    make_account, make_publication, route, identity_service
) -> None:
    account, token = make_account(external_id="expected-id")
    identity_service.register(token, "somebody-else")
    make_publication(social_account_id=account.id)

    outcome, legacy, accounts, stored = route()

    assert outcome.status == "failed"
    assert "different Threads account" in outcome.detail
    assert accounts.publish_calls == []
    assert legacy.calls == 0
    assert stored.threads_post_id is None


def test_a_rejected_token_fails_the_row(
    make_account, make_publication, route, identity_service
) -> None:
    account, token = make_account()
    identity_service.rejected.add(token)
    make_publication(social_account_id=account.id)

    outcome, legacy, accounts, stored = route()

    assert outcome.status == "failed"
    assert accounts.publish_calls == [] and legacy.calls == 0


def test_an_unreachable_identity_check_is_retried_and_sends_nothing(
    make_account, make_publication, route, identity_service
) -> None:
    account, _ = make_account()
    identity_service.unavailable = True
    make_publication(social_account_id=account.id)

    outcome, legacy, accounts, stored = route()

    assert outcome.status == "retry_scheduled"
    assert stored.status is PublicationStatus.SCHEDULED
    assert stored.next_attempt_at is not None
    assert accounts.publish_calls == [] and legacy.calls == 0


def test_a_retry_resolves_the_same_account_again(
    session: Session, make_account, make_publication, route, identity_service
) -> None:
    account, token = make_account()
    make_publication(social_account_id=account.id)
    identity_service.unavailable = True
    route()

    identity_service.unavailable = False
    session.execute(update(Publication).values(next_attempt_at=None))
    session.commit()
    outcome, legacy, accounts, stored = route()

    assert outcome.status == "published"
    assert accounts.tokens == [token]
    assert legacy.calls == 0


def test_dry_run_decrypts_but_makes_no_call(
    make_account, make_publication, route, account_settings, identity_service
) -> None:
    account, _ = make_account()
    make_publication(social_account_id=account.id)

    outcome, legacy, accounts, stored = route(
        settings=replace(account_settings, dry_run=True)
    )

    assert outcome.status == "dry_run"
    assert identity_service.calls == []
    assert accounts.tokens == [] and legacy.calls == 0


# -- the legacy route -------------------------------------------------------


def test_a_legacy_row_of_an_allow_listed_project_uses_the_deployment_token(
    make_publication, route
) -> None:
    make_publication()

    outcome, legacy, accounts, _ = route()

    assert outcome.status == "published"
    assert legacy.calls == 1
    assert accounts.tokens == []


def test_a_legacy_row_of_any_other_project_is_never_published(
    make_publication, route, account_settings
) -> None:
    make_publication()

    outcome, legacy, accounts, stored = route(settings=_no_legacy(account_settings))

    assert outcome.status == "failed"
    assert REASON_LEGACY_NOT_ALLOWED in outcome.detail
    assert legacy.calls == 0 and accounts.tokens == []
    assert stored.status is PublicationStatus.FAILED


def test_the_default_configuration_allows_no_legacy_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_smm.config import load_settings

    monkeypatch.delenv("AI_SMM_LEGACY_THREADS_PROJECTS", raising=False)

    assert load_settings().legacy_threads_projects == ()


def test_an_invalid_legacy_project_id_is_a_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_smm.config import load_settings

    monkeypatch.setenv("AI_SMM_LEGACY_THREADS_PROJECTS", "ok-project,../x")

    with pytest.raises(ValueError, match="invalid project id"):
        load_settings()


def test_the_account_factory_never_reads_the_deployment_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_smm.publishing.threads import ThreadsPublisher
    from ai_smm.security.credentials import SecretToken

    monkeypatch.setenv("THREADS_ACCESS_TOKEN", "THQdeployment-token-0123456789")

    publisher = ThreadsPublisher(
        access_token=SecretToken("THQaccount-token-0123456789")
    )

    assert publisher.access_token == "THQaccount-token-0123456789"

    with pytest.raises(RuntimeError, match="empty"):
        ThreadsPublisher(access_token=SecretToken(""))


# -- the transition guard (old worker on the new schema) ----------------------


def test_a_claim_without_account_routing_is_refused_by_the_database(
    session: Session, make_account, make_publication
) -> None:
    """What an old worker's claim UPDATE runs into."""

    account, _ = make_account()
    publication = make_publication(social_account_id=account.id)

    with pytest.raises(DBAPIError, match="only an account-routing worker"):
        session.execute(
            update(Publication)
            .where(Publication.id == publication.id)
            .values(status=PublicationStatus.CLAIMED, claimed_by="old-worker")
        )

    session.rollback()
    session.expire_all()

    assert session.get(Publication, publication.id).status is (
        PublicationStatus.SCHEDULED
    )


def test_an_old_style_claim_of_a_legacy_row_still_works(
    session: Session, make_publication
) -> None:
    publication = make_publication()

    session.execute(
        update(Publication)
        .where(Publication.id == publication.id)
        .values(status=PublicationStatus.CLAIMED, claimed_by="old-worker")
    )
    session.commit()


def test_the_routing_declaration_is_local_to_the_claim_transaction(
    session: Session, make_account, make_publication
) -> None:
    account, _ = make_account()
    make_publication(social_account_id=account.id)

    assert claim_due_publication(session, worker_id="w", lease_seconds=60)
    session.commit()

    assert session.execute(
        text("SELECT current_setting('ai_smm.account_routing', true)")
    ).scalar() in (None, "")


# -- scheduling ---------------------------------------------------------------


def test_scheduling_checks_the_route(
    session: Session, make_account, make_publication, account_settings
) -> None:
    from ai_smm.application.publications import (
        PublishingRouteNotReady,
        schedule_publication,
    )

    ready, _ = make_account()
    disabled, _ = make_account(status="disabled")
    unverified, _ = make_account(verified=False)
    good = make_publication(
        status=PublicationStatus.APPROVED, social_account_id=ready.id
    )
    bad = make_publication(
        status=PublicationStatus.APPROVED, social_account_id=disabled.id
    )
    unknown = make_publication(
        status=PublicationStatus.APPROVED, social_account_id=unverified.id
    )
    legacy = make_publication(status=PublicationStatus.APPROVED)
    when = datetime.now(timezone.utc) + timedelta(hours=1)

    def schedule(publication, settings=account_settings):
        return schedule_publication(
            session,
            publication_id=publication.id,
            expected_project_id=publication.project_id,
            scheduled_at=when,
            actor="test",
            mutations_enabled=True,
            settings=settings,
        )

    assert schedule(good).status is PublicationStatus.SCHEDULED
    session.commit()

    for publication in (bad, unknown):
        with pytest.raises(PublishingRouteNotReady):
            schedule(publication)
        session.rollback()

    with pytest.raises(PublishingRouteNotReady) as caught:
        schedule(legacy, settings=_no_legacy(account_settings))

    assert caught.value.code == REASON_LEGACY_NOT_ALLOWED
    session.rollback()

    assert schedule(legacy).status is PublicationStatus.SCHEDULED


def test_check_route_ready_never_decrypts(
    session: Session, make_account, make_publication, settings: Settings
) -> None:
    """The API holds no private key; the check must not need one."""

    account, _ = make_account()
    publication = make_publication(social_account_id=account.id)

    route = check_route_ready(session, publication, settings)

    assert route.is_account
    assert route.token is None


def test_the_database_refuses_an_account_of_another_project(
    session: Session, make_account, make_publication, make_other_project
) -> None:
    from sqlalchemy.exc import IntegrityError

    foreign, _ = make_account(project_id=make_other_project)

    with pytest.raises(IntegrityError, match="fk_publications_social_account"):
        make_publication(social_account_id=foreign.id)

    session.rollback()


# -- series ---------------------------------------------------------------------


def test_a_series_holds_one_account(
    session: Session, make_account, make_publication, make_series
) -> None:
    from ai_smm.series import attach_publication

    first, _ = make_account()
    second, _ = make_account()
    a = make_publication(social_account_id=first.id)
    b = make_publication(social_account_id=second.id)
    series = make_series(parts=[a])

    with pytest.raises(ValueError, match="different social account"):
        attach_publication(session, b, series=series, position=2, actor="test")

    session.rollback()


def test_a_reply_to_a_parent_of_another_account_is_not_ready(
    session: Session, make_account, make_publication, make_series
) -> None:
    from ai_smm.series import check_series_readiness

    account, _ = make_account()
    parent = make_publication(
        status=PublicationStatus.PUBLISHED,
        threads_post_id="17900000000000555",
        published_at=datetime.now(timezone.utc),
    )
    reply = make_publication()
    make_series(strategy=PublishingStrategy.REPLY_THREAD, parts=[parent, reply])

    # Bypass attach's own check to reach the readiness guard.
    session.execute(
        update(Publication)
        .where(Publication.id == reply.id)
        .values(social_account_id=account.id)
    )
    session.commit()
    session.expire_all()

    readiness = check_series_readiness(session, session.get(Publication, reply.id))

    assert not readiness.ready
    assert "another social account" in readiness.reason


# -- the worker end to end ------------------------------------------------


def test_the_worker_routes_by_account(
    session: Session,
    session_factory: sessionmaker[Session],
    account_settings: Settings,
    make_account,
    make_publication,
    identity_service,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import ai_smm.db.session as session_module

    monkeypatch.setattr(
        session_module, "get_session_factory", lambda _settings=None: session_factory
    )
    account, token = make_account()
    legacy_row = make_publication()
    account_row = make_publication(
        social_account_id=account.id,
        scheduled_at=datetime.now(timezone.utc) - timedelta(seconds=5),
    )
    legacy = LegacyFactory()
    accounts = AccountPublishers()

    worker = Worker(
        settings=account_settings,
        publisher_factory=legacy,
        account_publisher_factory=accounts,
        identity_fetcher=identity_service,
        media_root=tmp_path,
        heartbeat_path=tmp_path / "heartbeat",
    )
    worker.run_cycle()
    worker.run_cycle()

    session.expire_all()

    assert session.get(Publication, legacy_row.id).status is PublicationStatus.PUBLISHED
    assert (
        session.get(Publication, account_row.id).status is PublicationStatus.PUBLISHED
    )
    assert legacy.calls == 1
    assert accounts.tokens == [token]


def test_a_worker_with_a_group_readable_key_refuses_to_start(
    account_settings: Settings, credential_keys
) -> None:
    import os

    from ai_smm.security.credentials import CredentialCryptoError

    os.chmod(credential_keys.path, 0o644)

    with pytest.raises(CredentialCryptoError, match="chmod 600"):
        Worker(settings=account_settings)


def test_route_unavailable_messages_carry_no_secret(
    session: Session, make_account, make_publication, settings: Settings
) -> None:
    account, token = make_account(status="disabled")
    publication = make_publication(social_account_id=account.id)

    with pytest.raises(RouteUnavailable) as caught:
        check_route_ready(session, publication, settings)

    assert token not in str(caught.value)


def test_a_credential_is_tested_before_the_account_row_is_locked(
    session: Session,
    make_account,
    owner_user,
    credential_keys,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No writer of the account waits on the platform's answer."""

    import ai_smm.application.social_accounts as accounts
    from ai_smm.platform_identity import ThreadsIdentity
    from ai_smm.security.credentials import CredentialSealer, SecretToken

    account, _ = make_account(external_id="1789")
    order: list[str] = []
    real_lock = accounts.lock_account

    def lock(*args, **kwargs):
        order.append("lock")

        return real_lock(*args, **kwargs)

    def identity(token):
        order.append("identity")

        return ThreadsIdentity(account_id="1789", username="u")

    monkeypatch.setattr(accounts, "lock_account", lock)

    accounts.set_credential(
        session,
        account_id=account.id,
        expected_project_id=account.project_id,
        actor_user=owner_user,
        token=SecretToken("THQtest-order-0123456789"),
        sealer=CredentialSealer(credential_keys.public),
        mutations_enabled=True,
        identity_fetcher=identity,
    )
    session.rollback()

    assert order == ["identity", "lock"]


def test_the_default_account_publisher_refuses_to_run_without_a_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a token ThreadsPublisher would read the deployment one."""

    from ai_smm.publishing.service import _default_account_publisher
    from ai_smm.security.credentials import SecretToken

    monkeypatch.setenv("THREADS_ACCESS_TOKEN", "THQdeployment-token-0123456789")

    for bad in (None, SecretToken(""), "THQplain-string-not-a-secret-token"):
        with pytest.raises(RuntimeError, match="own token"):
            _default_account_publisher(bad)


def test_an_account_route_without_a_token_is_refused_not_asserted(
    session: Session,
    make_account,
    make_publication,
    account_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ai_smm.publishing.service as service
    from ai_smm.account_routing import ROUTE_ACCOUNT, PublishRoute

    account, _ = make_account()
    make_publication(social_account_id=account.id)
    monkeypatch.setattr(
        service,
        "resolve_route",
        lambda *a, **k: PublishRoute(
            kind=ROUTE_ACCOUNT,
            project_id="test-project",
            platform="threads",
            social_account_id=account.id,
        ),
    )
    legacy = LegacyFactory()
    claimed = claim_due_publication(session, worker_id="w", lease_seconds=60)
    session.commit()

    outcome = publish_claimed_publication(
        session,
        claimed,
        settings=account_settings,
        publisher_factory=legacy,
        media_root=Path.cwd(),
    )

    assert outcome.status == "failed"
    assert legacy.calls == 0


# -- the credential disappears between the check and the decryption ----------


@pytest.mark.parametrize("change", ["missing", "revoked", "other_account"])
def test_a_credential_gone_before_decryption_is_refused(
    session: Session,
    make_account,
    make_publication,
    account_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    """Revoked or replaced after check_route_ready read it: refuse, never
    decrypt nothing, never fall back."""

    import uuid as uuid_module

    import ai_smm.publishing.routing as routing
    from ai_smm.account_routing import ROUTE_ACCOUNT, PublishRoute
    from ai_smm.publishing.routing import resolve_route

    account, _ = make_account()
    other, _ = make_account()
    publication = make_publication(social_account_id=account.id)
    own = session.scalars(
        select(SocialAccountCredential.id).where(
            SocialAccountCredential.social_account_id == account.id
        )
    ).one()
    foreign = session.scalars(
        select(SocialAccountCredential.id).where(
            SocialAccountCredential.social_account_id == other.id
        )
    ).one()

    credential_id = {
        "missing": uuid_module.uuid4(),
        "revoked": own,
        "other_account": foreign,
    }[change]

    if change == "revoked":
        session.execute(
            update(SocialAccountCredential)
            .where(SocialAccountCredential.id == own)
            .values(
                status="revoked",
                revoked_at=datetime.now(timezone.utc),
                ciphertext=None,
                nonce=None,
                ephemeral_public_key=None,
            )
        )
        session.commit()

    monkeypatch.setattr(
        routing,
        "check_route_ready",
        lambda *a, **k: PublishRoute(
            kind=ROUTE_ACCOUNT,
            project_id=publication.project_id,
            platform="threads",
            social_account_id=account.id,
            credential_id=credential_id,
            expected_external_id=account.external_account_id,
        ),
    )

    with pytest.raises(RouteUnavailable) as caught:
        resolve_route(
            session,
            publication,
            account_settings,
            opener=build_opener(account_settings),
        )

    assert caught.value.code == REASON_NO_CREDENTIAL


def test_the_pipeline_fails_a_row_whose_credential_vanished(
    session: Session,
    make_account,
    make_publication,
    route,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import uuid as uuid_module

    import ai_smm.publishing.routing as routing
    from ai_smm.account_routing import ROUTE_ACCOUNT, PublishRoute

    account, _ = make_account()
    make_publication(social_account_id=account.id)
    monkeypatch.setattr(
        routing,
        "check_route_ready",
        lambda s, p, st: PublishRoute(
            kind=ROUTE_ACCOUNT,
            project_id=p.project_id,
            platform="threads",
            social_account_id=account.id,
            credential_id=uuid_module.uuid4(),
            expected_external_id=account.external_account_id,
        ),
    )

    outcome, legacy, accounts, stored = route()

    assert outcome.status == "failed"
    assert REASON_NO_CREDENTIAL in outcome.detail
    assert legacy.calls == 0 and accounts.tokens == []
    assert stored.status is PublicationStatus.FAILED


_OPTIMIZED_PROBE = """
import sys, uuid
import ai_smm.account_routing as policy
import ai_smm.publishing.routing as routing
from ai_smm.account_routing import ROUTE_ACCOUNT, PublishRoute, RouteUnavailable
from ai_smm.config import load_settings
from ai_smm.security.credentials import CredentialOpener, generate_key_pair

print("optimize", sys.flags.optimize)
account_id = uuid.uuid4()

class Publication:
    id = 1
    project_id = "test-project"
    platform = "threads"
    social_account_id = account_id

class Account:
    id = account_id
    project_id = "test-project"
    platform = "threads"
    is_active = True
    external_account_id = "1"

class Session:
    def get(self, model, key):
        return Account() if model.__name__ == "SocialAccount" else None

# 1. worker side: the credential named by the route no longer exists
routing.check_route_ready = lambda s, p, st: PublishRoute(
    kind=ROUTE_ACCOUNT, project_id="test-project", platform="threads",
    social_account_id=account_id, credential_id=uuid.uuid4(),
    expected_external_id="1",
)
try:
    routing.resolve_route(
        Session(), Publication(), load_settings(),
        opener=CredentialOpener([generate_key_pair()[0]]),
    )
    print("resolve", "NOT REFUSED")
except RouteUnavailable as exc:
    print("resolve", exc.code)

# 2. policy side: readiness says yes but no credential row came back
policy.active_credential = lambda session, account_id: None
policy.account_readiness = lambda account, credential: (True, None)
try:
    policy._account_and_credential(Session(), Publication())
    print("policy", "NOT REFUSED")
except RouteUnavailable as exc:
    print("policy", exc.code)
"""


def test_the_refusals_hold_under_python_dash_o() -> None:
    """Asserts are stripped by -O; these guards must not be asserts."""

    import os
    import subprocess
    import sys

    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("THREADS_", "OPENAI_", "LANGFUSE_", "STORAGE_"))
    }
    env["PYTHON_DOTENV_DISABLED"] = "1"

    result = subprocess.run(
        [sys.executable, "-O", "-c", _OPTIMIZED_PROBE],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    lines = result.stdout.strip().splitlines()
    assert lines == [
        "optimize 1",
        f"resolve {REASON_NO_CREDENTIAL}",
        f"policy {REASON_NO_CREDENTIAL}",
    ], result.stdout + result.stderr
