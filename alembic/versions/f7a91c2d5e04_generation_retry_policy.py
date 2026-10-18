"""Store the generation retry ceiling on each job.

Revision ID: f7a91c2d5e04
Revises: e04cb3a748f5
Create Date: 2026-10-17
"""
from __future__ import annotations

import sqlalchemy as sa

from ai_smm.db.generation_contracts import (
    GENERATION_CONTRACTS,
    GENERATION_CONTRACTS_V1,
)
from ai_smm.db.guards import (
    GENERATION_FUNCTIONS,
    GENERATION_FUNCTIONS_V1,
    revoke_public_statements,
)
from alembic import op


revision: str = "f7a91c2d5e04"
down_revision: str | None = "e04cb3a748f5"
branch_labels = None
depends_on = None


def _install(functions) -> None:
    for function in functions:
        op.execute(function.ddl)
    for statement in revoke_public_statements(functions):
        op.execute(statement)


def upgrade() -> None:
    # Existing jobs inherit the backend's historical default. Jobs already at
    # or above this ceiling become terminal on the next claim/recovery pass.
    op.add_column(
        "content_generation_jobs",
        sa.Column(
            "max_attempts", sa.Integer(), server_default="2", nullable=False
        ),
    )
    op.create_check_constraint(
        "ck_generation_jobs_max_attempts",
        "content_generation_jobs",
        "max_attempts BETWEEN 1 AND 10",
    )

    _install(GENERATION_FUNCTIONS)

    # Remove the worker-controlled retry-policy signatures before granting
    # access to the new contracts through the separately managed role script.
    op.execute(
        "DROP FUNCTION public.generation_claim(text, integer, integer)"
    )
    op.execute(
        "DROP FUNCTION public.generation_fail("
        "uuid, text, text, boolean, integer, text, integer, integer)"
    )
    _install(GENERATION_CONTRACTS)


def downgrade() -> None:
    op.execute("DROP FUNCTION public.generation_claim(text, integer)")
    op.execute(
        "DROP FUNCTION public.generation_fail("
        "uuid, text, text, boolean, text, integer, integer)"
    )
    _install(GENERATION_CONTRACTS_V1)
    _install(GENERATION_FUNCTIONS_V1)

    op.drop_constraint(
        "ck_generation_jobs_max_attempts",
        "content_generation_jobs",
        type_="check",
    )
    op.drop_column("content_generation_jobs", "max_attempts")
