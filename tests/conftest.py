"""Test fixtures.

The queue relies on PostgreSQL features that have no SQLite equivalent
(FOR UPDATE SKIP LOCKED, JSONB, native enums, partial indexes), so the
database tests run against a real PostgreSQL. Point
AI_SMM_TEST_DATABASE_URL at one; otherwise those tests are skipped with an
explicit reason rather than silently passing against a weaker engine.
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest


#: The suite's fixture projects publish account-less (legacy) rows, as
#: every test written before social accounts does. Production allows no
#: project unless the operator lists it; the tests that check exactly that
#: build their settings with an empty tuple (tests/test_publish_routing.py).
#: Set before any ai_smm import: get_settings() is cached on first use,
#: and importing a module that logs already uses it.
os.environ.setdefault(
    "AI_SMM_LEGACY_THREADS_PROJECTS", "test-project,other-project"
)

from sqlalchemy import Engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from ai_smm.config import Settings, load_settings
from ai_smm.db.models import (
    Base,
    ContentSeries,
    MembershipRole,
    ProjectMembership,
    Publication,
    PublicationStatus,
    PublishingStrategy,
    User,
)
from ai_smm.db.session import create_db_engine
from ai_smm.queue import ensure_project
from tests import pg_roles


TEST_DB_ENV = "AI_SMM_TEST_DATABASE_URL"
#: A superuser URL to the same test database: creates ai_smm_cc.
ADMIN_DB_ENV = "AI_SMM_TEST_ADMIN_DATABASE_URL"
#: owner (default) | restricted: the role the API under test connects as.
API_ROLE_ENV = "AI_SMM_TEST_API_ROLE"


def _test_database_url() -> str | None:
    return os.getenv(TEST_DB_ENV)


@pytest.fixture(scope="session")
def database_url() -> str:
    url = _test_database_url()

    if not url:
        pytest.skip(
            f"{TEST_DB_ENV} is not set; database tests need a live "
            "PostgreSQL 16 instance."
        )

    # The suite drops and truncates every table, so pointing it at a
    # database that holds real publications would destroy the queue.
    # Requiring "test" in the name makes that mistake impossible rather
    # than merely unlikely.
    database_name = url.rsplit("/", 1)[-1].split("?")[0]

    if "test" not in database_name.lower():
        pytest.fail(
            f"{TEST_DB_ENV} points at database {database_name!r}, whose name "
            "does not contain 'test'. This suite drops and truncates tables; "
            "use a disposable database such as 'ai_smm_test'.",
            pytrace=False,
        )

    return url


@pytest.fixture(scope="session")
def engine(database_url: str) -> Iterator[Engine]:
    engine = create_db_engine(
        database_url,
        settings=_base_settings(),
        pool_size=10,
        max_overflow=5,
    )

    # Schema comes from the models here; a dedicated test asserts that the
    # Alembic migration produces the same thing.
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    yield engine

    engine.dispose()


def _base_settings() -> Settings:
    return load_settings()


def drop_scratch_database(connection, name: str, *, attempts: int = 20) -> None:
    """DROP DATABASE IF EXISTS ... WITH (FORCE), as the owner, race-free.

    FORCE terminates the other sessions in the database, but a
    non-superuser may not terminate an autovacuum worker (it runs as no
    role): PostgreSQL then refuses with insufficient_privilege. A freshly
    created and migrated scratch database attracts autovacuum within
    seconds, which made this fail intermittently. The worker finishes or
    moves on quickly, so only that refusal is retried, briefly; anything
    else, or a refusal that persists, fails the test as before.
    """

    import time

    from sqlalchemy.exc import DBAPIError

    for attempt in range(1, attempts + 1):
        try:
            connection.execute(
                text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            )

            return
        except DBAPIError as exc:
            sqlstate = getattr(exc.orig, "sqlstate", None)

            if sqlstate != "42501" or attempt == attempts:
                raise

            time.sleep(0.25)


# --- the API's least-privilege role (SMM-024C-2) ---------------------------
# Creating a role needs more than the owner role the suite runs as, so the
# tests that need ai_smm_cc take a second URL to the SAME test database, as
# a superuser of that (disposable) cluster. Without it they are skipped.


@pytest.fixture(scope="session")
def admin_database_url(database_url: str) -> str:
    url = os.getenv(ADMIN_DB_ENV)

    if not url:
        pytest.skip(
            f"{ADMIN_DB_ENV} is not set; the least-privilege tests create "
            "a role and need a superuser of the disposable test cluster."
        )

    admin, suite = make_url(url), make_url(database_url)

    if (admin.host, admin.port, admin.database) != (
        suite.host,
        suite.port,
        suite.database,
    ):
        pytest.fail(
            f"{ADMIN_DB_ENV} must point at the same server and database as "
            f"{TEST_DB_ENV} ({suite.database!r}).",
            pytrace=False,
        )

    return url


@pytest.fixture(scope="session")
def api_role(
    engine: Engine, admin_database_url: str, database_url: str
) -> Iterator[pg_roles.ApiRole]:
    """ai_smm_cc, provisioned by the operator's grants file.

    After `engine`: the grants are taken on the tables create_all made.
    Retired by the operator's revoke file at the end of the session, which
    also proves that file works -- it refuses while a session is open.
    """

    role = pg_roles.provision(admin_database_url, database_url)

    yield role

    pg_roles.retire(admin_database_url)

    # Rollback stage 2 leaves every other role exactly as it found it.
    assert pg_roles.snapshot_others(admin_database_url) == role.others_before


@pytest.fixture(scope="session")
def restricted_engine(api_role: pg_roles.ApiRole) -> Iterator[Engine]:
    engine = create_db_engine(api_role.url, settings=_base_settings())

    yield engine

    engine.dispose()


@pytest.fixture
def restricted_session_factory(
    restricted_engine: Engine,
) -> sessionmaker[Session]:
    return sessionmaker(
        bind=restricted_engine, expire_on_commit=False, future=True
    )


@pytest.fixture
def settings(database_url: str, tmp_path: Path) -> Settings:
    return replace(
        load_settings(),
        database_url=database_url,
        worker_id="test-worker",
        dry_run=False,
        poll_interval_seconds=1,
        lease_seconds=900,
        max_attempts=3,
        min_publish_interval_seconds=0,
        daily_publish_limit=100,
        batch_size=1,
        knowledge_root=tmp_path / "knowledge",
        media_root=tmp_path,
        log_format="text",
    )


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@pytest.fixture(autouse=True)
def clean_tables(request: pytest.FixtureRequest) -> Iterator[None]:
    """Truncate between tests so ordering never matters."""

    if "engine" not in request.fixturenames:
        yield

        return

    engine: Engine = request.getfixturevalue("engine")

    with engine.begin() as connection:
        connection.execute(
            text(
                "TRUNCATE content_publication_links, "
                "content_approvals, content_revisions, "
                "content_items, "
                "publication_attempts, publications, "
                "content_series, project_settings, "
                "project_memberships, user_sessions, users, "
                "projects, audit_log "
                "RESTART IDENTITY CASCADE"
            )
        )

    yield


@pytest.fixture
def session(
    session_factory: sessionmaker[Session],
) -> Iterator[Session]:
    db_session = session_factory()

    try:
        yield db_session

        # A test may deliberately leave the session in a failed state (for
        # example when asserting a constraint violation); committing it here
        # would turn that into a teardown error.
        if db_session.is_active:
            db_session.commit()
    except Exception:
        db_session.rollback()
        raise
    finally:
        db_session.close()


@pytest.fixture
def project(session: Session) -> str:
    ensure_project(
        session,
        project_id="test-project",
        display_name="Test Project",
        knowledge_path="knowledge/projects/test-project",
    )
    session.commit()

    return "test-project"


@pytest.fixture
def make_publication(session: Session, project: str):
    """Create a due, reviewed, text-only publication by default."""

    counter = {"n": 0}

    def factory(
        *,
        status: PublicationStatus = PublicationStatus.SCHEDULED,
        scheduled_at: datetime | None = None,
        body: str = "Готовый текст публикации для теста.",
        publication_format: str = "text",
        images: list[dict[str, str]] | None = None,
        human_reviewed: bool = True,
        commit: bool = True,
        **kwargs: object,
    ) -> Publication:
        counter["n"] += 1
        ordinal = counter["n"]

        publication = Publication(
            project_id=project,
            platform="threads",
            ordinal=ordinal,
            title=f"Test publication {ordinal}",
            format=publication_format,
            body=body,
            images=images or [],
            items=[],
            status=status,
            scheduled_at=(
                scheduled_at
                if scheduled_at is not None
                else datetime.now(timezone.utc) - timedelta(minutes=1)
            ),
            idempotency_key=f"test-project:threads:{ordinal}:fixture",
            human_reviewed=human_reviewed,
            **kwargs,
        )

        session.add(publication)

        if commit:
            session.commit()
        else:
            session.flush()

        return publication

    return factory


@pytest.fixture
def make_series(session: Session, project: str):
    """Create a series, optionally with its parts already attached."""

    from ai_smm.series import attach_publication, create_series

    def factory(
        *,
        strategy: PublishingStrategy = (
            PublishingStrategy.STANDALONE_SERIES
        ),
        title: str = "Test series",
        planned_total: int | None = None,
        enforce_order: bool = True,
        parts: list[Publication] | None = None,
    ) -> ContentSeries:
        series = create_series(
            session,
            project_id=project,
            title=title,
            narrative_goal="what the reader should take away",
            target_audience="engineers",
            publishing_strategy=strategy,
            planned_total=planned_total
            or (len(parts) if parts else None),
            enforce_order=enforce_order,
            actor="test",
        )

        for position, publication in enumerate(parts or [], start=1):
            attach_publication(
                session,
                publication,
                series=series,
                position=position,
                total=len(parts or []),
                actor="test",
            )

        session.commit()

        return series

    return factory


@pytest.fixture
def make_other_project(session: Session) -> str:
    ensure_project(
        session,
        project_id="other-project",
        display_name="Other Project",
        knowledge_path="knowledge/projects/other-project",
    )
    session.commit()

    return "other-project"


# --- API fixtures --------------------------------------------------------
# The API tests drive the real application through its factory, against
# the same disposable database. Nothing is mocked except the clock, where
# a test needs an expired session.

#: Used by every test user. Long enough to pass the strength check and
#: obviously not a real credential.
TEST_PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
def api_settings(settings: Settings) -> Settings:
    # TestClient speaks http, so the Secure attribute would stop the
    # cookie from being sent back. Production keeps the default (true).
    #
    # Mutations on: the existing API suites test what an editable control
    # plane allows. The read-only default (false) has its own suite,
    # tests/test_api_mutation_gate.py, built on read_only_client.
    return replace(
        settings, api_cookie_secure=False, api_mutations_enabled=True
    )


@pytest.fixture
def api_session_factory(
    request: pytest.FixtureRequest, session_factory: sessionmaker[Session]
) -> sessionmaker[Session]:
    """The database identity the API under test connects with.

    The owner role by default, like every other fixture. With
    AI_SMM_TEST_API_ROLE=restricted, the API -- and only the API --
    connects as ai_smm_cc, while fixtures keep seeding data as the owner:
    that runs whole suites against the production privilege model.
    tests/least_privilege/ does the same for the suites that apply.
    """

    mode = os.getenv(API_ROLE_ENV, "owner")

    if mode == "restricted":
        return request.getfixturevalue("restricted_session_factory")

    if mode != "owner":
        pytest.fail(f"{API_ROLE_ENV} must be 'owner' or 'restricted'")

    return session_factory


@pytest.fixture
def api_app(
    api_settings: Settings, api_session_factory: sessionmaker[Session]
):
    from ai_smm.api.app import create_app

    return create_app(
        api_settings, session_factory=api_session_factory, access_log=False
    )


@pytest.fixture
def client(api_app) -> Iterator[Any]:
    from fastapi.testclient import TestClient

    with TestClient(api_app) as test_client:
        yield test_client


@pytest.fixture
def make_user(session: Session):
    """Create an active user with a known password."""

    from ai_smm.security.passwords import hash_password

    counter = {"n": 0}

    def factory(
        *,
        email: str | None = None,
        display_name: str = "Test User",
        password: str = TEST_PASSWORD,
        is_active: bool = True,
    ) -> User:
        counter["n"] += 1
        user = User(
            email=email or f"user{counter['n']}@example.com",
            password_hash=hash_password(password),
            display_name=display_name,
            is_active=is_active,
        )
        session.add(user)
        session.commit()

        return user

    return factory


@pytest.fixture
def make_membership(session: Session):
    def factory(
        *, user: User, project_id: str, role: MembershipRole
    ) -> ProjectMembership:
        membership = ProjectMembership(
            project_id=project_id, user_id=user.id, role=role
        )
        session.add(membership)
        session.commit()

        return membership

    return factory


@pytest.fixture
def login(client, session: Session):
    """Sign a user in and return the CSRF token for their session."""

    def do_login(user: User, password: str = TEST_PASSWORD) -> str:
        response = client.post(
            "/api/v1/auth/login",
            json={"email": user.email, "password": password},
        )

        assert response.status_code == 200, response.text

        # The test session must not keep a stale copy of the row the API
        # just updated (last_login_at, and the new session row).
        session.expire_all()

        return response.json()["csrf_token"]

    return do_login


@pytest.fixture
def member(session: Session, project: str, make_user, make_membership):
    """A user who is a member of the standard test project."""

    def factory(role: MembershipRole, **kwargs) -> User:
        user = make_user(**kwargs)
        make_membership(user=user, project_id=project, role=role)

        return user

    return factory


# --- editorial fixtures ----------------------------------------------------


@pytest.fixture
def make_content(session: Session, project: str):
    """Create a content item with its first revision, through the service."""

    from ai_smm.application.content import RevisionInput, create_content_item

    counter = {"n": 0}

    def factory(
        *,
        user: User | None = None,
        project_id: str | None = None,
        title: str | None = None,
        body: str = "Первая версия текста.",
        publication_format: str = "text",
        images: list[dict[str, str]] | None = None,
    ):
        counter["n"] += 1
        item, revision = create_content_item(
            session,
            project_id=project_id or project,
            title=title or f"Content {counter['n']}",
            content_type="post",
            revision=RevisionInput(
                body=body,
                format=publication_format,
                images=images or [],
            ),
            created_by_user=user,
        )
        session.commit()

        return item, revision

    return factory


# --- social accounts (SMM-025B) -------------------------------------------
# Tokens here are random test values. No fixture ever reads a real
# credential, and no test reaches a platform: identity checks go to
# FakeIdentityService.


def fake_token(label: str = "t") -> str:
    import secrets

    return f"THQtest{label}{secrets.token_urlsafe(32)}"


class FakeIdentityService:
    """Stands in for Threads GET /me: token -> identity, or an error."""

    def __init__(self) -> None:
        self.identities: dict[str, tuple[str, str | None]] = {}
        self.rejected: set[str] = set()
        self.unavailable = False
        self.calls: list[str] = []

    def register(
        self, token: str, account_id: str, username: str | None = None
    ) -> None:
        self.identities[token] = (account_id, username)

    def __call__(self, token):
        from ai_smm.publishing.threads import (
            CredentialRejected,
            IdentityUnavailable,
            ThreadsIdentity,
        )

        value = token.reveal()
        self.calls.append(value)

        if self.unavailable:
            raise IdentityUnavailable("Threads could not be reached (test).")

        if value in self.rejected or value not in self.identities:
            raise CredentialRejected("Threads rejected the token (HTTP 401).")

        account_id, username = self.identities[value]

        return ThreadsIdentity(account_id=account_id, username=username)


@pytest.fixture
def identity_service() -> FakeIdentityService:
    return FakeIdentityService()


@pytest.fixture
def credential_keys(tmp_path: Path):
    from types import SimpleNamespace

    from ai_smm.security.credentials import (
        encode_public_key,
        generate_key_pair,
        write_private_key_file,
    )

    private, public = generate_key_pair()
    path = tmp_path / "credentials.pem"
    write_private_key_file(path, private)

    return SimpleNamespace(
        private=private,
        public=public,
        path=path,
        public_b64=encode_public_key(public),
    )


@pytest.fixture
def account_settings(settings: Settings, credential_keys) -> Settings:
    """Worker settings that can decrypt credentials sealed by make_account."""

    return replace(
        settings,
        credentials_public_key=credential_keys.public_b64,
        credentials_private_key_files=(credential_keys.path,),
    )


@pytest.fixture
def make_account(session: Session, project: str, credential_keys, identity_service):
    """A verified account with an active sealed credential, by default."""

    import uuid as uuid_module

    from ai_smm.db.models import (
        CREDENTIAL_STATUS_ACTIVE,
        VERIFICATION_VERIFIED,
        SocialAccount,
        SocialAccountCredential,
    )
    from ai_smm.security.credentials import (
        CredentialSealer,
        SecretToken,
        associated_data,
        token_fingerprint,
    )

    counter = {"n": 0}

    def factory(
        *,
        project_id: str | None = None,
        display_name: str | None = None,
        external_id: str | None = None,
        token: str | None = None,
        with_credential: bool = True,
        verified: bool = True,
        status: str = "active",
    ):
        counter["n"] += 1
        n = counter["n"]
        now = datetime.now(timezone.utc)
        external = external_id or f"ext-{n}"
        token = token or fake_token(str(n))

        account = SocialAccount(
            id=uuid_module.uuid4(),
            project_id=project_id or project,
            platform="threads",
            display_name=display_name or f"Account {n}",
            status=status,
            external_account_id=external if verified else None,
            external_username=f"user{n}" if verified else None,
            disabled_at=now if status == "disabled" else None,
        )
        session.add(account)
        session.flush()

        if with_credential:
            credential_id = uuid_module.uuid4()
            secret = SecretToken(token)
            sealed = CredentialSealer(credential_keys.public).seal(
                secret,
                aad=associated_data(
                    account_id=str(account.id), credential_id=str(credential_id)
                ),
            )
            session.add(
                SocialAccountCredential(
                    id=credential_id,
                    social_account_id=account.id,
                    status=CREDENTIAL_STATUS_ACTIVE,
                    scheme=sealed.scheme,
                    key_id=sealed.key_id,
                    ephemeral_public_key=sealed.ephemeral_public_key,
                    nonce=sealed.nonce,
                    ciphertext=sealed.ciphertext,
                    fingerprint=token_fingerprint(secret),
                    verification_status=VERIFICATION_VERIFIED,
                    verified_at=now,
                )
            )

        identity_service.register(token, external, f"user{n}")
        session.commit()

        return account, token

    return factory


@pytest.fixture
def owner_user(member) -> User:
    return member(MembershipRole.OWNER)
