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

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from ai_smm.config import Settings, load_settings
from ai_smm.db.models import (
    Base,
    ContentSeries,
    Publication,
    PublicationStatus,
    PublishingStrategy,
)
from ai_smm.db.session import create_db_engine
from ai_smm.queue import ensure_project


TEST_DB_ENV = "AI_SMM_TEST_DATABASE_URL"


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
                "TRUNCATE publication_attempts, publications, "
                "content_series, projects, audit_log "
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
