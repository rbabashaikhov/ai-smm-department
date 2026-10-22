"""One set of bounds for topic ideation (SMM-035B3A, review finding R3).

The bounds live in ai_smm.db.ideation_contracts. The models build their
CHECK constraints from those constants; the migration spells the same
bounds as literals, because a migration must replay identically whatever
the code later says. Alembic's autogenerate comparison does not look at
CHECK constraints, so nothing else would notice the two drifting apart.

This module does: the constraints and indexes of both tables as the
migration creates them must be exactly those create_all builds from the
constants, and the HTTP schema must accept exactly the constants' range.
"""
from __future__ import annotations

import pytest
from annotated_types import Ge, Le, MaxLen
from sqlalchemy import Engine, create_engine, text

from ai_smm.api.schemas import TopicIdeationJobCreate
from ai_smm.db.ideation_contracts import (
    MAX_IDEAS_PER_JOB,
    MAX_TOPIC_HINT_LENGTH,
    MIN_IDEAS_PER_JOB,
)
from alembic import command
from tests.test_migrations import _alembic_config
from tests.test_migrations import migration_db as migration_db  # noqa: F401  (fixture)


TABLES = ("topic_ideation_jobs", "content_ideas")

_DEFINITIONS = text(
    """
    SELECT 'constraint:' || c.conrelid::regclass::text || ':' || c.conname,
           pg_get_constraintdef(c.oid)
      FROM pg_constraint c
     WHERE c.conrelid IN (SELECT to_regclass(t) FROM unnest(CAST(:tables AS text[])) t)
    UNION ALL
    SELECT 'index:' || i.indrelid::regclass::text || ':' || ic.relname,
           pg_get_indexdef(i.indexrelid)
      FROM pg_index i JOIN pg_class ic ON ic.oid = i.indexrelid
     WHERE i.indrelid IN (SELECT to_regclass(t) FROM unnest(CAST(:tables AS text[])) t)
     ORDER BY 1
    """
)


def _definitions(engine: Engine) -> list[tuple[str, str]]:
    with engine.connect() as connection:
        return [
            tuple(row)
            for row in connection.execute(_DEFINITIONS, {"tables": list(TABLES)})
        ]


def test_the_migration_and_the_models_enforce_the_same_bounds(
    migration_db: str,  # noqa: F811
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AI_SMM_DATABASE_URL", migration_db)
    command.upgrade(_alembic_config(migration_db), "head")

    migrated = create_engine(migration_db)
    try:
        from_migration = _definitions(migrated)
    finally:
        migrated.dispose()

    from_models = _definitions(engine)

    assert from_migration == from_models
    names = {name for name, _ in from_models}
    # Every named bound of both tables is part of the comparison.
    assert {
        "constraint:topic_ideation_jobs:ck_topic_ideation_jobs_idea_count",
        "constraint:topic_ideation_jobs:ck_topic_ideation_jobs_topic_hint",
        "constraint:topic_ideation_jobs:ck_topic_ideation_jobs_max_attempts",
        "constraint:topic_ideation_jobs:ck_topic_ideation_jobs_last_error",
        "constraint:content_ideas:ck_content_ideas_title",
        "constraint:content_ideas:ck_content_ideas_description",
        "constraint:content_ideas:ck_content_ideas_angle",
        "constraint:content_ideas:fk_content_ideas_ideation_job",
    } <= names


@pytest.mark.parametrize(
    ("idea_count", "accepted"),
    [
        (MIN_IDEAS_PER_JOB - 1, False),
        (MIN_IDEAS_PER_JOB, True),
        (MAX_IDEAS_PER_JOB, True),
        (MAX_IDEAS_PER_JOB + 1, False),
    ],
)
def test_the_table_bounds_idea_count_exactly_at_the_constants(
    session, project: str, make_user, idea_count: int, accepted: bool
) -> None:
    from sqlalchemy.exc import IntegrityError

    user = make_user()
    statement = text(
        "INSERT INTO topic_ideation_jobs"
        " (id, project_id, requested_by_user_id, idea_count, topic_hint)"
        " VALUES (gen_random_uuid(), :p, :u, :n, :hint)"
    )
    params = {"p": project, "u": user.id, "n": idea_count, "hint": "x"}

    if accepted:
        session.execute(statement, params)
        session.execute(
            statement, {**params, "n": MIN_IDEAS_PER_JOB,
                        "hint": "x" * MAX_TOPIC_HINT_LENGTH},
        )
        session.rollback()
        return

    with pytest.raises(IntegrityError):
        session.execute(statement, params)
    session.rollback()


def test_the_table_refuses_a_topic_hint_one_past_the_constant(
    session, project: str, make_user
) -> None:
    from sqlalchemy.exc import IntegrityError

    user = make_user()

    with pytest.raises(IntegrityError):
        session.execute(
            text(
                "INSERT INTO topic_ideation_jobs"
                " (id, project_id, requested_by_user_id, idea_count, topic_hint)"
                " VALUES (gen_random_uuid(), :p, :u, 1, :hint)"
            ),
            {"p": project, "u": user.id, "hint": "x" * (MAX_TOPIC_HINT_LENGTH + 1)},
        )
    session.rollback()


def test_the_http_schema_accepts_exactly_the_constants_range() -> None:
    fields = TopicIdeationJobCreate.model_fields
    count = fields["idea_count"].metadata
    hint = fields["topic_hint"].metadata

    assert Ge(MIN_IDEAS_PER_JOB) in count
    assert Le(MAX_IDEAS_PER_JOB) in count
    assert MaxLen(MAX_TOPIC_HINT_LENGTH) in hint
