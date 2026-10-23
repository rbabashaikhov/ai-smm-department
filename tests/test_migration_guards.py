"""Migration c4d81e2a9f63: the business-mutation guards (SMM-025D).

Additive and refusing to go away silently: the downgrade removes database
protections, so it needs AI_SMM_DOWNGRADE_REMOVES_MUTATION_GUARDS=yes.
The migrated guards must be exactly the ones create_all builds for the
rest of the suite.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import Engine, create_engine, text

from alembic import command
from tests.test_migrations import _alembic_config
from tests.test_migrations import migration_db as migration_db  # noqa: F401  (fixture)


SOCIAL = "a7c2e5f19b40"
GUARDS = "c4d81e2a9f63"
#: Whatever is above GUARDS today (through 1c9e4b7d2a05, additive):
#: a downgrade refused at GUARDS rolls back the whole multi-step command,
#: landing back at head, not at GUARDS itself.
HEAD = "1c9e4b7d2a05"
SWITCH = "AI_SMM_DOWNGRADE_REMOVES_MUTATION_GUARDS"

_BODIES = text(
    """
    SELECT 'function:' || p.proname,
           md5(p.prosrc) || '|' || p.prosecdef::text || '|' || p.provolatile::text
           || '|' || coalesce(array_to_string(p.proconfig, ','), '')
      FROM pg_proc p
     WHERE p.pronamespace = 'public'::regnamespace
       AND (p.proname LIKE 'guard\\_%' OR p.proname LIKE 'generation\\_%'
            OR p.proname LIKE 'topic\\_ideation\\_%')
    UNION ALL
    SELECT 'trigger:' || t.tgname,
           pg_get_triggerdef(t.oid) || '|deferrable=' || t.tgdeferrable::text
           || '|deferred=' || t.tginitdeferred::text
      FROM pg_trigger t
     WHERE NOT t.tgisinternal AND t.tgname LIKE 'trg\\_%\\_guard\\_%'
     ORDER BY 1
    """
)


def guard_bodies(connection) -> list[tuple[str, str]]:
    return [tuple(row) for row in connection.execute(_BODIES)]


def _engine(url: str) -> Engine:
    return create_engine(url)


def _at(url: str) -> str | None:
    engine = _engine(url)
    try:
        with engine.connect() as connection:
            return connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar()
    finally:
        engine.dispose()


def test_the_migrated_guards_are_those_create_all_builds(
    migration_db: str,
    engine: Engine,  # noqa: F811
) -> None:
    os.environ["AI_SMM_DATABASE_URL"] = migration_db
    command.upgrade(_alembic_config(migration_db), "head")

    migrated = _engine(migration_db)
    try:
        with migrated.connect() as connection:
            from_migration = guard_bodies(connection)
    finally:
        migrated.dispose()

    with engine.connect() as connection:
        from_models = guard_bodies(connection)

    assert from_migration == from_models
    # 18 from c4d81e2a9f63 + trg_generation_jobs_guard_insert/update
    # (e04cb3a748f5, SMM-026A/2.1) + the four topic ideation guards
    # (b3a7d2e91c4f, SMM-035B3A).
    assert len([n for n, _ in from_migration if n.startswith("trigger:")]) == 24
    assert {
        "function:topic_ideation_claim",
        "function:topic_ideation_complete",
        "function:topic_ideation_fail",
    } <= {n for n, _ in from_migration}


def test_public_can_execute_no_security_contract(migration_db: str) -> None:  # noqa: F811
    os.environ["AI_SMM_DATABASE_URL"] = migration_db
    command.upgrade(_alembic_config(migration_db), "head")

    engine = _engine(migration_db)
    try:
        with engine.connect() as connection:
            executable = (
                connection.execute(
                    text(
                        "SELECT p.proname FROM pg_proc p"
                        " WHERE p.pronamespace = 'public'::regnamespace"
                        " AND (p.proname LIKE 'guard\\_%'"
                        "      OR p.proname LIKE 'generation\\_%'"
                        "      OR p.proname LIKE 'topic\\_ideation\\_%')"
                        " AND has_function_privilege('public', p.oid, 'EXECUTE')"
                    )
                )
                .scalars()
                .all()
            )
    finally:
        engine.dispose()

    assert executable == []


def test_the_downgrade_refuses_without_its_switch(
    migration_db: str,
    monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    monkeypatch.setenv("AI_SMM_DATABASE_URL", migration_db)
    monkeypatch.delenv(SWITCH, raising=False)
    config = _alembic_config(migration_db)
    command.upgrade(config, "head")

    for value in (None, "", "no", "true", "YES "):
        if value is None:
            monkeypatch.delenv(SWITCH, raising=False)
        else:
            monkeypatch.setenv(SWITCH, value)

        with pytest.raises(RuntimeError, match="removes the database guards"):
            command.downgrade(config, SOCIAL)

        assert _at(migration_db) == HEAD


def test_down_and_up_again_with_the_switch(
    migration_db: str,
    monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    monkeypatch.setenv("AI_SMM_DATABASE_URL", migration_db)
    monkeypatch.setenv(SWITCH, "yes")
    config = _alembic_config(migration_db)

    command.upgrade(config, "head")
    command.downgrade(config, SOCIAL)

    engine = _engine(migration_db)
    try:
        with engine.connect() as connection:
            assert guard_bodies(connection) == []
            assert (
                connection.execute(
                    text(
                        "SELECT count(*) FROM information_schema.columns"
                        " WHERE table_name = 'audit_log' AND column_name = 'written_by'"
                    )
                ).scalar_one()
                == 0
            )
            assert (
                connection.execute(
                    text("SELECT to_regclass('public.approval_grants')")
                ).scalar()
                is None
            )
    finally:
        engine.dispose()

    command.upgrade(config, "head")

    assert _at(migration_db) == HEAD


def test_existing_audit_rows_get_no_invented_provenance(
    migration_db: str,
    monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    monkeypatch.setenv("AI_SMM_DATABASE_URL", migration_db)
    config = _alembic_config(migration_db)
    command.upgrade(config, SOCIAL)

    engine = _engine(migration_db)
    try:
        with engine.begin() as connection:
            connection.execute(
                text("INSERT INTO audit_log (actor, action) VALUES ('cli:x', 'old')")
            )

        command.upgrade(config, "head")

        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO audit_log (actor, action, written_by)"
                    " VALUES ('cli:x', 'new', 'forged')"
                )
            )
            rows = dict(
                connection.execute(
                    text("SELECT action, written_by FROM audit_log ORDER BY id")
                ).all()
            )
            me = connection.execute(text("SELECT session_user")).scalar_one()
    finally:
        engine.dispose()

    assert rows["old"] is None
    # Even the owner cannot choose it: the guard sets session_user.
    assert rows["new"] == me
