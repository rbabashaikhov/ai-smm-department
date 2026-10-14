"""Migration a7c2e5f19b40: social accounts, credentials, routing columns.

Run against a scratch database, like tests/test_migrations.py. The
questions: does every existing row keep its meaning, can an old worker
misroute anything on the new schema, and does a downgrade refuse while it
would turn account-bound rows into deployment-account rows?
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, inspect, text

from alembic import command
from tests.test_migrations import _alembic_config, migration_db  # noqa: F401


EDITORIAL = "3d4ac1986f07"


@pytest.fixture(autouse=True)
def _downgrades_past_the_guards(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests exercise older revisions, so they downgrade through
    c4d81e2a9f63, which refuses without its switch. Its own refusal is
    tested in tests/test_migration_guards.py."""

    monkeypatch.setenv("AI_SMM_DOWNGRADE_REMOVES_MUTATION_GUARDS", "yes")

SOCIAL = "a7c2e5f19b40"


def _seed_legacy(connection) -> None:
    connection.execute(
        text(
            "INSERT INTO projects (id, display_name, knowledge_path)"
            " VALUES ('pilot', 'Pilot', 'knowledge/projects/pilot')"
        )
    )
    connection.execute(
        text(
            "INSERT INTO publications (project_id, ordinal, title, format,"
            " body, status, scheduled_at, idempotency_key, human_reviewed)"
            " VALUES ('pilot', 1, 'Old', 'text', 'old body', 'scheduled',"
            " now() - interval '1 minute', 'pilot:threads:1:abc', true)"
        )
    )


def _seed_account_row(connection, *, status: str = "scheduled") -> uuid.UUID:
    account_id = uuid.uuid4()
    published = status == "published"
    connection.execute(
        text(
            "INSERT INTO social_accounts (id, project_id, platform, display_name,"
            " external_account_id) VALUES (:id, 'pilot', 'threads', 'Blog', '1789')"
        ),
        {"id": account_id},
    )
    connection.execute(
        text(
            "INSERT INTO publications (project_id, ordinal, title, format, body,"
            " status, scheduled_at, idempotency_key, human_reviewed,"
            " social_account_id, threads_post_id, published_at) VALUES"
            " ('pilot', 2, 'New', 'text', 'new body',"
            " CAST(:status AS publication_status),"
            " now() - interval '1 minute', 'pilot:threads:2:def', true, :account,"
            " :post_id, CASE WHEN :published THEN now() END)"
        ),
        {
            "account": account_id,
            "status": status,
            "post_id": "17900000000000777" if published else None,
            "published": published,
        },
    )

    return account_id


def _publications_snapshot(engine) -> dict[str, object]:
    inspector = inspect(engine)

    return {
        "columns": sorted(c["name"] for c in inspector.get_columns("publications")),
        "fks": sorted(k["name"] for k in inspector.get_foreign_keys("publications")),
        "indexes": sorted(i["name"] for i in inspector.get_indexes("publications")),
    }


def test_existing_rows_keep_their_meaning(migration_db: str) -> None:  # noqa: F811
    os.environ["AI_SMM_DATABASE_URL"] = migration_db
    config = _alembic_config(migration_db)
    command.upgrade(config, EDITORIAL)
    engine = create_engine(migration_db)

    try:
        with engine.begin() as connection:
            _seed_legacy(connection)

        # Closed explicitly: an open read transaction would hold a lock
        # on publications and make the upgrade's ALTER TABLE wait forever.
        with engine.connect() as connection:
            before = connection.execute(
                text("SELECT id, status, project_id, body FROM publications")
            ).all()
        schema_before = _publications_snapshot(engine)

        command.upgrade(config, SOCIAL)

        with engine.connect() as connection:
            after = connection.execute(
                text("SELECT id, status, project_id, body FROM publications")
            ).all()
            accounts = (
                connection.execute(text("SELECT social_account_id FROM publications"))
                .scalars()
                .all()
            )
        schema_after = _publications_snapshot(engine)
    finally:
        engine.dispose()

    assert after == before
    # Every existing row is a legacy row: no account was invented for it.
    assert accounts == [None]
    # The only additions to publications.
    assert set(schema_after["columns"]) - set(schema_before["columns"]) == {
        "social_account_id"
    }
    assert set(schema_after["fks"]) - set(schema_before["fks"]) == {
        "fk_publications_social_account"
    }
    assert set(schema_after["indexes"]) - set(schema_before["indexes"]) == {
        "ix_publications_social_account"
    }


def test_an_old_worker_claim_cannot_take_an_account_row(migration_db: str) -> None:  # noqa: F811
    """The exact UPDATE of the pre-SMM-025B claim, on the new schema."""

    os.environ["AI_SMM_DATABASE_URL"] = migration_db
    command.upgrade(_alembic_config(migration_db), SOCIAL)
    engine = create_engine(migration_db)
    old_claim = text(
        "UPDATE publications SET status = 'claimed', claimed_by = 'worker-1',"
        " claimed_at = now(), lease_expires_at = now() + interval '15 minutes'"
        " WHERE id = :id AND status = 'scheduled' RETURNING id"
    )

    try:
        with engine.begin() as connection:
            _seed_legacy(connection)
            _seed_account_row(connection)

        with engine.begin() as connection:
            legacy_id, account_row_id = (
                connection.execute(text("SELECT id FROM publications ORDER BY ordinal"))
                .scalars()
                .all()
            )

        with engine.begin() as connection:
            assert (
                connection.execute(old_claim, {"id": legacy_id}).scalar() == legacy_id
            )

        with (
            pytest.raises(Exception, match="only an account-routing worker"),
            engine.begin() as connection,
        ):
            connection.execute(old_claim, {"id": account_row_id})

        with engine.begin() as connection:
            connection.execute(
                text("SELECT set_config('ai_smm.account_routing', 'on', true)")
            )
            assert (
                connection.execute(old_claim, {"id": account_row_id}).scalar()
                == account_row_id
            )
    finally:
        engine.dispose()


def test_a_downgrade_refuses_while_an_account_row_is_live(migration_db: str) -> None:  # noqa: F811
    os.environ["AI_SMM_DATABASE_URL"] = migration_db
    config = _alembic_config(migration_db)
    command.upgrade(config, SOCIAL)
    engine = create_engine(migration_db)

    try:
        with engine.begin() as connection:
            _seed_legacy(connection)
            _seed_account_row(connection, status="scheduled")

        with pytest.raises(RuntimeError, match="Refusing to downgrade"):
            command.downgrade(config, EDITORIAL)

        # Nothing was dropped: the refusal comes before any DDL.
        assert "social_accounts" in inspect(engine).get_table_names()
        assert "social_account_id" in {
            c["name"] for c in inspect(engine).get_columns("publications")
        }
    finally:
        engine.dispose()


def test_a_downgrade_with_only_terminal_account_rows_restores_the_schema(
    migration_db: str,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Destroying account data is explicit (see the next test).
    monkeypatch.setenv("AI_SMM_DOWNGRADE_DESTROYS_ACCOUNT_DATA", "yes")
    os.environ["AI_SMM_DATABASE_URL"] = migration_db
    config = _alembic_config(migration_db)
    command.upgrade(config, EDITORIAL)
    engine = create_engine(migration_db)

    try:
        schema_before = _publications_snapshot(engine)
        tables_before = set(inspect(engine).get_table_names())

        command.upgrade(config, SOCIAL)

        with engine.begin() as connection:
            _seed_legacy(connection)
            _seed_account_row(connection, status="published")

        command.downgrade(config, EDITORIAL)

        engine.dispose()
        engine = create_engine(migration_db)
        schema_after = _publications_snapshot(engine)
        tables_after = set(inspect(engine).get_table_names())

        with engine.connect() as connection:
            leftovers = connection.execute(
                text(
                    "SELECT proname FROM pg_proc WHERE proname = "
                    "'publication_account_claim_guard'"
                )
            ).all()
            rows = connection.execute(
                text("SELECT count(*) FROM publications")
            ).scalar()
    finally:
        engine.dispose()

    assert schema_after == schema_before
    assert tables_after == tables_before
    assert leftovers == []
    assert rows == 2


def test_the_migration_is_repeatable(migration_db: str) -> None:  # noqa: F811
    os.environ["AI_SMM_DATABASE_URL"] = migration_db
    config = _alembic_config(migration_db)

    command.upgrade(config, "head")
    command.downgrade(config, EDITORIAL)
    command.upgrade(config, "head")
    command.downgrade(config, EDITORIAL)
    command.upgrade(config, "head")

    engine = create_engine(migration_db)

    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    assert {"social_accounts", "social_account_credentials"} <= tables


@pytest.mark.parametrize("switch", [None, "", "no", "true"])
def test_a_downgrade_that_would_destroy_account_data_needs_the_explicit_switch(
    migration_db: str,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    switch: str | None,
) -> None:
    """Even with every account-bound row terminal, the data is not dropped
    by accident: accounts, credentials and attributions would be lost."""

    if switch is None:
        monkeypatch.delenv("AI_SMM_DOWNGRADE_DESTROYS_ACCOUNT_DATA", raising=False)
    else:
        monkeypatch.setenv("AI_SMM_DOWNGRADE_DESTROYS_ACCOUNT_DATA", switch)

    os.environ["AI_SMM_DATABASE_URL"] = migration_db
    config = _alembic_config(migration_db)
    command.upgrade(config, SOCIAL)
    engine = create_engine(migration_db)

    try:
        with engine.begin() as connection:
            _seed_legacy(connection)
            _seed_account_row(connection, status="published")

        with pytest.raises(RuntimeError, match="permanently delete 1 social account"):
            command.downgrade(config, EDITORIAL)

        with engine.connect() as connection:
            accounts = connection.execute(
                text("SELECT count(*) FROM social_accounts")
            ).scalar()
    finally:
        engine.dispose()

    assert accounts == 1


def test_the_destroy_switch_never_overrides_the_live_row_refusal(
    migration_db: str,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AI_SMM_DOWNGRADE_DESTROYS_ACCOUNT_DATA", "yes")
    os.environ["AI_SMM_DATABASE_URL"] = migration_db
    config = _alembic_config(migration_db)
    command.upgrade(config, SOCIAL)
    engine = create_engine(migration_db)

    try:
        with engine.begin() as connection:
            _seed_legacy(connection)
            _seed_account_row(connection, status="scheduled")

        with pytest.raises(
            RuntimeError, match="would be published with the deployment"
        ):
            command.downgrade(config, EDITORIAL)
    finally:
        engine.dispose()
