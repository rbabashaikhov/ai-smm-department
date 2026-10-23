"""topic ideation job params (SMM-035B3B1)

Additive. A job now carries what it runs on, copied by the API when it is
queued and never changed after: the project's content profile snapshot
and its most recent idea titles. The ideation worker reads no table, so
this is the only way it learns either, exactly as content_generation_jobs
.params does for a generation job (SMM-035B2).

* topic_ideation_jobs.params -- jsonb NOT NULL DEFAULT '{}', an object of
  at most 64 KiB (ck_topic_ideation_jobs_params). Existing jobs get {}.
* guard_topic_ideation_jobs_insert -- replaced: a business role's params
  must be exactly {content_profile: object, recent_idea_titles: <= 20
  short strings}. Everything else it checked is unchanged.
* topic_ideation_claim -- now also returns params. A changed RETURNS TABLE
  cannot be CREATE OR REPLACEd, so it is dropped and created again: any
  EXECUTE grant on it goes with it. Apply
  deploy/control-center/db/ai_smm_ideation.grants.sql only AFTER this
  migration (and again after a downgrade/upgrade cycle).

topic_ideation_complete/fail, content_generation_jobs and every
generation/publication contract are not touched.

Downgrade refuses while a job carries params: it would destroy the
snapshot that job was queued with.

Revision ID: 1c9e4b7d2a05
Revises: b3a7d2e91c4f
Create Date: 2026-10-23
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from ai_smm.db.guards import (
    TOPIC_IDEATION_FUNCTIONS,
    TOPIC_IDEATION_FUNCTIONS_V1,
    revoke_public_statements,
)
from ai_smm.db.ideation_contracts import (
    TOPIC_IDEATION_CONTRACTS,
    TOPIC_IDEATION_CONTRACTS_V1,
)
from alembic import op


revision: str = "1c9e4b7d2a05"
down_revision: str | None = "b3a7d2e91c4f"
branch_labels = None
depends_on = None

_CLAIM_SIGNATURE = "public.topic_ideation_claim(text, integer)"


def _install(functions) -> None:
    for function in functions:
        op.execute(function.ddl)
    for statement in revoke_public_statements(functions):
        op.execute(statement)


def _claim(contracts):
    return tuple(f for f in contracts if f.name == "topic_ideation_claim")


def upgrade() -> None:
    op.add_column(
        "topic_ideation_jobs",
        sa.Column(
            "params",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_topic_ideation_jobs_params",
        "topic_ideation_jobs",
        "jsonb_typeof(params) = 'object' AND octet_length(params::text) "
        "<= 65536",
    )

    _install(TOPIC_IDEATION_FUNCTIONS)

    op.execute(f"DROP FUNCTION {_CLAIM_SIGNATURE}")
    _install(_claim(TOPIC_IDEATION_CONTRACTS))


def downgrade() -> None:
    jobs = op.get_bind().execute(
        sa.text(
            "SELECT count(*) FROM public.topic_ideation_jobs"
            " WHERE params <> '{}'::jsonb"
        )
    ).scalar_one()

    if jobs:
        raise RuntimeError(
            f"Refusing to downgrade: {jobs} topic ideation job(s) carry "
            "params, and dropping the column would destroy the snapshot "
            "each was queued with. Export and remove them first, "
            "deliberately."
        )

    op.execute(f"DROP FUNCTION {_CLAIM_SIGNATURE}")
    _install(_claim(TOPIC_IDEATION_CONTRACTS_V1))
    _install(TOPIC_IDEATION_FUNCTIONS_V1)

    op.drop_constraint(
        "ck_topic_ideation_jobs_params", "topic_ideation_jobs", type_="check"
    )
    op.drop_column("topic_ideation_jobs", "params")
