"""The migration must produce exactly the schema the models describe."""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text

from ai_smm.db.models import Base
from alembic import command


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _alembic_config(url: str) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url)

    return config


@pytest.fixture
def migration_db(database_url: str):
    """A separate database so migrating does not disturb the model schema."""

    admin_url = database_url
    target_db = "ai_smm_migration_test"

    admin_engine = create_engine(
        admin_url, isolation_level="AUTOCOMMIT", pool_pre_ping=True
    )

    with admin_engine.connect() as connection:
        connection.execute(
            text(f'DROP DATABASE IF EXISTS "{target_db}" WITH (FORCE)')
        )
        connection.execute(text(f'CREATE DATABASE "{target_db}"'))

    admin_engine.dispose()

    target_url = admin_url.rsplit("/", 1)[0] + f"/{target_db}"

    yield target_url

    admin_engine = create_engine(
        admin_url, isolation_level="AUTOCOMMIT", pool_pre_ping=True
    )

    with admin_engine.connect() as connection:
        connection.execute(
            text(f'DROP DATABASE IF EXISTS "{target_db}" WITH (FORCE)')
        )

    admin_engine.dispose()


def test_upgrade_creates_expected_tables(migration_db: str) -> None:
    os.environ["AI_SMM_DATABASE_URL"] = migration_db

    command.upgrade(_alembic_config(migration_db), "head")

    engine = create_engine(migration_db)

    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    assert {
        "projects",
        "publications",
        "publication_attempts",
        "audit_log",
        "alembic_version",
    } <= tables


def test_migration_matches_models(migration_db: str) -> None:
    """No drift between alembic/versions and db/models.py."""

    os.environ["AI_SMM_DATABASE_URL"] = migration_db

    command.upgrade(_alembic_config(migration_db), "head")

    engine = create_engine(migration_db)

    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(
                connection, opts={"compare_type": True}
            )
            diff = compare_metadata(context, Base.metadata)
    finally:
        engine.dispose()

    assert diff == [], f"schema drift detected: {diff}"


def test_downgrade_then_upgrade_is_repeatable(migration_db: str) -> None:
    """Enum types must be dropped too, or the second upgrade fails."""

    os.environ["AI_SMM_DATABASE_URL"] = migration_db
    config = _alembic_config(migration_db)

    command.upgrade(config, "head")
    command.downgrade(config, "base")

    engine = create_engine(migration_db)

    try:
        with engine.connect() as connection:
            leftover_enums = connection.execute(
                text("SELECT count(*) FROM pg_type WHERE typtype = 'e'")
            ).scalar_one()

            tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    assert leftover_enums == 0, "downgrade left enum types behind"
    assert "publications" not in tables

    # The real assertion: upgrading again works.
    command.upgrade(config, "head")


def test_published_row_requires_post_id(migration_db: str) -> None:
    """The check constraint, as applied by the migration."""

    from sqlalchemy.exc import IntegrityError

    os.environ["AI_SMM_DATABASE_URL"] = migration_db
    command.upgrade(_alembic_config(migration_db), "head")

    engine = create_engine(migration_db)

    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO projects (id, display_name, knowledge_path)"
                    " VALUES ('p', 'P', 'k')"
                )
            )

        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO publications "
                    "(project_id, ordinal, title, format, body, "
                    " status, idempotency_key) "
                    "VALUES ('p', 1, 't', 'text', 'b', "
                    "'published', 'k1')"
                )
            )
    finally:
        engine.dispose()
