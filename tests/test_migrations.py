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


# -- SMM-022C: the editorial layer is additive ----------------------------

CONTROL_PLANE_HEAD = "2964ac9ceea7"
EDITORIAL_REVISION = "3d4ac1986f07"
EDITORIAL_TABLES = {
    "content_items",
    "content_revisions",
    "content_approvals",
    "content_publication_links",
}
EDITORIAL_FUNCTIONS = {
    "content_reject_mutation",
    "content_publication_link_check",
}
EDITORIAL_ENUMS = {
    "content_status",
    "content_revision_source",
    "content_approval_decision",
}


def _schema_snapshot(engine) -> dict[str, object]:
    """Columns, indexes and constraints of every table, for comparison."""

    inspector = inspect(engine)
    snapshot: dict[str, object] = {}

    for table in sorted(inspector.get_table_names()):
        snapshot[table] = {
            "columns": [
                (c["name"], str(c["type"]), c["nullable"], str(c.get("default")))
                for c in inspector.get_columns(table)
            ],
            "indexes": sorted(
                (i["name"], tuple(i["column_names"]), bool(i["unique"]))
                for i in inspector.get_indexes(table)
            ),
            "checks": sorted(
                (c["name"], c["sqltext"])
                for c in inspector.get_check_constraints(table)
            ),
            "unique": sorted(
                (u["name"], tuple(u["column_names"]))
                for u in inspector.get_unique_constraints(table)
            ),
            "foreign_keys": sorted(
                (
                    k["name"],
                    tuple(k["constrained_columns"]),
                    k["referred_table"],
                )
                for k in inspector.get_foreign_keys(table)
            ),
        }

    return snapshot


def _enums(engine) -> set[str]:
    with engine.connect() as connection:
        return set(
            connection.execute(
                text("SELECT typname FROM pg_type WHERE typtype = 'e'")
            ).scalars()
        )


def test_the_editorial_migration_upgrades_from_the_current_head(
    migration_db: str,
) -> None:
    os.environ["AI_SMM_DATABASE_URL"] = migration_db
    config = _alembic_config(migration_db)

    command.upgrade(config, CONTROL_PLANE_HEAD)

    engine = create_engine(migration_db)

    try:
        assert EDITORIAL_TABLES.isdisjoint(inspect(engine).get_table_names())

        command.upgrade(config, EDITORIAL_REVISION)

        inspector = inspect(engine)

        assert set(inspector.get_table_names()) >= EDITORIAL_TABLES
        assert _enums(engine) >= EDITORIAL_ENUMS

        with engine.connect() as connection:
            triggers = set(
                connection.execute(
                    text(
                        "SELECT tgname FROM pg_trigger "
                        "WHERE tgname LIKE 'trg_content%'"
                    )
                ).scalars()
            )

        assert triggers == {
            "trg_content_revisions_append_only",
            "trg_content_approvals_append_only",
            "trg_content_publication_links_consistent",
        }
    finally:
        engine.dispose()


def test_existing_rows_survive_the_editorial_migration(
    migration_db: str,
) -> None:
    os.environ["AI_SMM_DATABASE_URL"] = migration_db
    config = _alembic_config(migration_db)

    command.upgrade(config, CONTROL_PLANE_HEAD)

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
                    "INSERT INTO publications (project_id, ordinal, title,"
                    " format, body, status, idempotency_key, human_reviewed)"
                    " VALUES ('legacy', 1, 'Old', 'text', 'old body',"
                    " 'scheduled', 'legacy:threads:1:abc', true)"
                )
            )

        before = engine.connect().execute(
            text("SELECT * FROM publications ORDER BY id")
        ).all()

        command.upgrade(config, EDITORIAL_REVISION)

        with engine.connect() as connection:
            after = connection.execute(
                text("SELECT * FROM publications ORDER BY id")
            ).all()
            project = connection.execute(
                text("SELECT id, display_name FROM projects")
            ).all()
    finally:
        engine.dispose()

    assert after == before
    assert project == [("legacy", "Legacy")]


def test_downgrading_the_editorial_migration_removes_only_its_additions(
    migration_db: str,
) -> None:
    """Every pre-existing table is identical before and after the round trip."""

    os.environ["AI_SMM_DATABASE_URL"] = migration_db
    config = _alembic_config(migration_db)

    command.upgrade(config, CONTROL_PLANE_HEAD)

    engine = create_engine(migration_db)

    try:
        schema_before = _schema_snapshot(engine)
        enums_before = _enums(engine)

        command.upgrade(config, EDITORIAL_REVISION)
        command.downgrade(config, CONTROL_PLANE_HEAD)

        schema_after = _schema_snapshot(engine)
        enums_after = _enums(engine)

        with engine.connect() as connection:
            functions_left = set(
                connection.execute(
                    text(
                        "SELECT proname FROM pg_proc "
                        "WHERE proname = ANY(:names)"
                    ),
                    {"names": sorted(EDITORIAL_FUNCTIONS)},
                ).scalars()
            )
    finally:
        engine.dispose()

    assert schema_after == schema_before
    assert enums_after == enums_before
    assert EDITORIAL_ENUMS.isdisjoint(enums_after)
    assert functions_left == set()


def test_the_editorial_migration_is_repeatable(migration_db: str) -> None:
    os.environ["AI_SMM_DATABASE_URL"] = migration_db
    config = _alembic_config(migration_db)

    command.upgrade(config, "head")
    command.downgrade(config, CONTROL_PLANE_HEAD)
    command.upgrade(config, "head")
    command.downgrade(config, CONTROL_PLANE_HEAD)
    command.upgrade(config, "head")

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


def test_the_migrated_schema_enforces_the_editorial_invariants(
    migration_db: str,
) -> None:
    """The guarantees hold on the migrated schema, not only on create_all."""

    from sqlalchemy.exc import IntegrityError

    os.environ["AI_SMM_DATABASE_URL"] = migration_db
    command.upgrade(_alembic_config(migration_db), "head")

    engine = create_engine(migration_db)
    item = "11111111-1111-1111-1111-111111111111"
    first = "22222222-2222-2222-2222-222222222222"

    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO projects (id, display_name, knowledge_path)"
                    " VALUES ('p', 'P', 'k')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO content_items"
                    " (id, project_id, title, current_revision_id)"
                    " VALUES (:item, 'p', 't', :rev)"
                ),
                {"item": item, "rev": first},
            )
            connection.execute(
                text(
                    "INSERT INTO content_revisions (id, content_item_id,"
                    " revision_number, body, format, source, content_hash)"
                    " VALUES (:rev, :item, 1, 'b', 'text', 'human',"
                    " repeat('a', 64))"
                ),
                {"item": item, "rev": first},
            )

        for statement in (
            "UPDATE content_revisions SET body = 'x'",
            "DELETE FROM content_revisions",
            "UPDATE content_items SET status = 'approved'",
            "INSERT INTO content_revisions (id, content_item_id,"
            " revision_number, body, format, source, content_hash) VALUES"
            " (gen_random_uuid(), '11111111-1111-1111-1111-111111111111',"
            " 1, 'dup', 'text', 'human', repeat('b', 64))",
        ):
            with pytest.raises(IntegrityError), engine.begin() as connection:
                connection.execute(text(statement))

        # An item pointing at a revision that does not exist is refused at
        # commit, when the deferred key is checked.
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO content_items"
                    " (id, project_id, title, current_revision_id) VALUES"
                    " (gen_random_uuid(), 'p', 't', gen_random_uuid())"
                )
            )
    finally:
        engine.dispose()



def test_the_migrated_link_table_enforces_lineage(migration_db: str) -> None:
    """Each link guarantee, on the schema Alembic builds -- not create_all."""

    from sqlalchemy.exc import IntegrityError

    os.environ["AI_SMM_DATABASE_URL"] = migration_db
    command.upgrade(_alembic_config(migration_db), "head")

    engine = create_engine(migration_db)
    ids = {
        "item_a": "aaaaaaaa-0000-0000-0000-000000000001",
        "rev_a": "aaaaaaaa-0000-0000-0000-0000000000a1",
        "item_b": "bbbbbbbb-0000-0000-0000-000000000001",
        "rev_b": "bbbbbbbb-0000-0000-0000-0000000000b1",
    }

    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO projects (id, display_name, knowledge_path)"
                    " VALUES ('p', 'P', 'k'), ('q', 'Q', 'k')"
                )
            )

            for item, rev in (("item_a", "rev_a"), ("item_b", "rev_b")):
                connection.execute(
                    text(
                        "INSERT INTO content_items"
                        " (id, project_id, title, current_revision_id)"
                        " VALUES (:item, 'p', 't', :rev)"
                    ),
                    {"item": ids[item], "rev": ids[rev]},
                )
                connection.execute(
                    text(
                        "INSERT INTO content_revisions (id, content_item_id,"
                        " revision_number, body, format, source, content_hash)"
                        " VALUES (:rev, :item, 1, 'b', 'text', 'human',"
                        " repeat('a', 64))"
                    ),
                    {"item": ids[item], "rev": ids[rev]},
                )

            connection.execute(
                text(
                    "INSERT INTO publications (id, project_id, ordinal, title,"
                    " format, body, idempotency_key) VALUES"
                    " (1, 'p', 1, 't', 'text', 'b', 'k1'),"
                    " (2, 'p', 2, 't', 'text', 'b', 'k2'),"
                    " (3, 'q', 1, 't', 'text', 'b', 'k3')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO content_publication_links"
                    " (content_item_id, revision_id, platform, publication_id)"
                    " VALUES (:item, :rev, 'threads', 1)"
                ),
                {"item": ids["item_a"], "rev": ids["rev_a"]},
            )

        # statement -> the constraint or trigger that must be the reason.
        refused = {
            "a revision of another item": (
                "INSERT INTO content_publication_links VALUES"
                " (:item_a, :rev_b, 'threads', 2, now())",
                "fk_content_publication_links_revision",
            ),
            "the same revision and platform twice": (
                "INSERT INTO content_publication_links VALUES"
                " (:item_a, :rev_a, 'threads', 2, now())",
                "content_publication_links_pkey",
            ),
            "one publication for two revisions": (
                "INSERT INTO content_publication_links VALUES"
                " (:item_b, :rev_b, 'threads', 1, now())",
                "uq_content_publication_links_publication",
            ),
            "a publication in another project": (
                "INSERT INTO content_publication_links VALUES"
                " (:item_b, :rev_b, 'threads', 3, now())",
                "not in the project",
            ),
            # The consistency trigger runs before the foreign key, so a
            # missing publication is refused by it first; the key remains
            # as the second line.
            "a publication that does not exist": (
                "INSERT INTO content_publication_links VALUES"
                " (:item_b, :rev_b, 'threads', 999, now())",
                "not in the project",
            ),
            "deleting a linked publication": (
                "DELETE FROM publications WHERE id = 1",
                "fk_content_publication_links_publication",
            ),
        }

        for statement, reason in refused.values():
            with pytest.raises(IntegrityError, match=reason), engine.begin() as c:
                c.execute(text(statement), ids)
    finally:
        engine.dispose()
