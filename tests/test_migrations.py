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
        # SMM-022A control plane.
        "users",
        "user_sessions",
        "project_memberships",
        "project_settings",
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


# -- SMM-022A: the control-plane migration is additive --------------------


def _columns(engine, table: str) -> dict[str, dict]:
    return {
        column["name"]: column
        for column in inspect(engine).get_columns(table)
    }


def test_the_control_plane_migration_only_adds_to_projects(
    migration_db: str,
) -> None:
    """The columns the worker and the importer rely on are untouched."""

    os.environ["AI_SMM_DATABASE_URL"] = migration_db
    config = _alembic_config(migration_db)

    command.upgrade(config, "79731eadacd7")

    engine = create_engine(migration_db)

    try:
        before = _columns(engine, "projects")

        command.upgrade(config, "head")

        after = _columns(engine, "projects")
    finally:
        engine.dispose()

    for name in (
        "id",
        "display_name",
        "knowledge_path",
        "default_platform",
        "is_active",
        "created_at",
        "updated_at",
    ):
        assert str(before[name]["type"]) == str(after[name]["type"])
        assert before[name]["nullable"] == after[name]["nullable"]
        assert before[name].get("default") == after[name].get("default")

    assert set(after) - set(before) == {
        "description",
        "default_timezone",
        "default_language",
        "version",
    }

    for name in after:
        if name not in before:
            # A new NOT NULL column needs a default, or the migration
            # would fail on any project that already exists.
            assert after[name]["nullable"] is False
            assert after[name]["default"] is not None


def test_the_publications_table_is_not_changed_by_the_new_migration(
    migration_db: str,
) -> None:
    """Existing Publication schema semantics must be identical."""

    os.environ["AI_SMM_DATABASE_URL"] = migration_db
    config = _alembic_config(migration_db)

    command.upgrade(config, "79731eadacd7")

    engine = create_engine(migration_db)

    def snapshot() -> dict[str, object]:
        inspector = inspect(engine)

        return {
            "columns": [
                (
                    column["name"],
                    str(column["type"]),
                    column["nullable"],
                    str(column.get("default")),
                )
                for column in inspector.get_columns("publications")
            ],
            "indexes": sorted(
                (index["name"], tuple(index["column_names"]))
                for index in inspector.get_indexes("publications")
            ),
            "checks": sorted(
                (check["name"], check["sqltext"])
                for check in inspector.get_check_constraints("publications")
            ),
            "unique": sorted(
                (
                    constraint["name"],
                    tuple(constraint["column_names"]),
                )
                for constraint in inspector.get_unique_constraints(
                    "publications"
                )
            ),
            "foreign_keys": sorted(
                (
                    key["name"],
                    tuple(key["constrained_columns"]),
                    key["referred_table"],
                )
                for key in inspector.get_foreign_keys("publications")
            ),
        }

    try:
        before = snapshot()

        command.upgrade(config, "head")

        after = snapshot()
    finally:
        engine.dispose()

    assert before == after


def test_existing_rows_survive_the_upgrade(migration_db: str) -> None:
    """A project and a publication written before the upgrade are intact."""

    os.environ["AI_SMM_DATABASE_URL"] = migration_db
    config = _alembic_config(migration_db)

    command.upgrade(config, "79731eadacd7")

    engine = create_engine(migration_db)

    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO projects (id, display_name, knowledge_path)"
                    " VALUES ('legacy', 'Legacy', 'knowledge/legacy')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO publications "
                    "(project_id, ordinal, title, format, body, status, "
                    " idempotency_key) "
                    "VALUES ('legacy', 1, 'Old post', 'text', 'body', "
                    "'scheduled', 'legacy:threads:1')"
                )
            )

        command.upgrade(config, "head")

        with engine.connect() as connection:
            project = connection.execute(
                text(
                    "SELECT description, default_timezone, default_language,"
                    " version FROM projects WHERE id = 'legacy'"
                )
            ).one()
            publication_count = connection.execute(
                text(
                    "SELECT count(*) FROM publications "
                    "WHERE project_id = 'legacy'"
                )
            ).scalar_one()
            settings_count = connection.execute(
                text("SELECT count(*) FROM project_settings")
            ).scalar_one()
    finally:
        engine.dispose()

    # The defaults were applied to the row that already existed.
    assert project == ("", "Europe/Moscow", "ru", 1)
    assert publication_count == 1
    # A project with no settings row is the normal state after upgrading.
    assert settings_count == 0


def test_a_session_token_hash_column_is_not_the_token(
    migration_db: str,
) -> None:
    """The schema has no column that could hold a raw session token."""

    os.environ["AI_SMM_DATABASE_URL"] = migration_db
    command.upgrade(_alembic_config(migration_db), "head")

    engine = create_engine(migration_db)

    try:
        columns = _columns(engine, "user_sessions")
    finally:
        engine.dispose()

    assert "token" not in columns
    assert "session_token" not in columns
    assert "token_hash" in columns

    # The CSRF token is derived from the session token on each request,
    # so there is no column for it either.
    assert [name for name in columns if "csrf" in name] == []
