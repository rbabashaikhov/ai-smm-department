"""content generation jobs (SMM-026A/2.1)

Additive. One new table, content_generation_jobs, claimed and processed
the same way publications are (FOR UPDATE SKIP LOCKED, a lease, bounded
retries -- ai_smm.generation_queue); one nullable column on content_items,
generation_job_id, linking a draft back to the job that produced it. No
existing row is read or rewritten: every existing content_items row gets
generation_job_id = NULL ("not agent-generated"), the same way a
publication predating content_series keeps series_id = NULL.

The job guards (guard_generation_jobs_insert/update) are a separate pair
from ai_smm.db.guards.FUNCTIONS_V1/TRIGGERS_V1 (shipped by c4d81e2a9f63),
not an extension of them, so that migration still replays unchanged
against a database that does not yet have this table. They depend on
guard_require_owner, already created there.

guard_audit_log_insert (FUNCTIONS_V1) is also updated here, in place, by
CREATE OR REPLACE: its business-audit whitelist gains generation.% so
create_generation_job/cancel_generation_job's audit rows are accepted.
Same function name and trigger as c4d81e2a9f63 shipped; downgrade()
restores that revision's exact body (guards.AUDIT_FUNCTIONS_V2/
_AUDIT_INSERT).

Nothing here is applied to production as part of this change; the
operator's Control Center grants files under deploy/control-center/db/
are updated in the same commit so a future, separately-approved rollout
has them ready, but granting them to the live business-mutation and
read-only API roles is its own step.

Revision ID: e04cb3a748f5
Revises: c4d81e2a9f63
Create Date: 2026-10-15
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from ai_smm.db.generation_contracts import GENERATION_CONTRACTS
from ai_smm.db.guards import (
    _AUDIT_INSERT,
    _ITEMS_INSERT_V1,
    _ITEMS_UPDATE_V1,
    AUDIT_FUNCTIONS_V2,
    CONTENT_ITEM_FUNCTIONS_V2,
    GENERATION_FUNCTIONS,
    GENERATION_TRIGGERS,
    drop_function_statements,
    revoke_public_statements,
)
from alembic import op


revision: str = "e04cb3a748f5"
down_revision: str | None = "c4d81e2a9f63"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "content_generation_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.String(length=120), nullable=False),
        sa.Column("requested_by_user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "queued",
                "claimed",
                "succeeded",
                "failed",
                "cancelled",
                name="generation_job_status",
            ),
            server_default="queued",
            nullable=False,
        ),
        sa.Column(
            "params",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "result",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "attempt_count", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_by", sa.String(length=120), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "lease_expires_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "attempt_count >= 0", name="ck_generation_jobs_attempt_count"
        ),
        sa.CheckConstraint(
            "status <> 'claimed' OR (claimed_by IS NOT NULL "
            "AND claimed_at IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="ck_generation_jobs_claim_shape",
        ),
        sa.CheckConstraint(
            "status <> 'succeeded' OR finished_at IS NOT NULL",
            name="ck_generation_jobs_succeeded_finished",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_generation_jobs_project_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"],
            ["users.id"],
            name="fk_generation_jobs_requested_by",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_generation_jobs_due",
        "content_generation_jobs",
        ["next_attempt_at"],
        postgresql_where=sa.text("status IN ('queued', 'claimed')"),
    )
    op.create_index(
        "ix_generation_jobs_project", "content_generation_jobs", ["project_id"]
    )
    op.create_index(
        "ix_generation_jobs_status", "content_generation_jobs", ["status"]
    )

    op.add_column(
        "content_items",
        sa.Column("generation_job_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_content_items_generation_job",
        "content_items",
        "content_generation_jobs",
        ["generation_job_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_content_items_generation_job",
        "content_items",
        ["generation_job_id"],
    )

    # Guard functions, PUBLIC's default EXECUTE taken away, then the
    # triggers -- the same order c4d81e2a9f63 used for FUNCTIONS_V1.
    for function in GENERATION_FUNCTIONS:
        op.execute(function.ddl)

    for statement in revoke_public_statements(GENERATION_FUNCTIONS):
        op.execute(statement)

    for trigger in GENERATION_TRIGGERS:
        op.execute(trigger.ddl)

    # guard_audit_log_insert v2: CREATE OR REPLACE of the function
    # FUNCTIONS_V1 created in c4d81e2a9f63, so generation.queued/
    # generation.cancelled (ai_smm.application.generation) pass its
    # business-audit whitelist. Same trigger, no new one.
    for function in AUDIT_FUNCTIONS_V2:
        op.execute(function.ddl)

    # The API's ORM names generation_job_id even when it is NULL. These
    # guards make the matching column grant safe and immutable.
    for function in CONTENT_ITEM_FUNCTIONS_V2:
        op.execute(function.ddl)

    for function in GENERATION_CONTRACTS:
        op.execute(function.ddl)
    for statement in revoke_public_statements(GENERATION_CONTRACTS):
        op.execute(statement)


def downgrade() -> None:
    for statement in drop_function_statements(GENERATION_CONTRACTS):
        op.execute(statement)

    # Restore the exact previous-revision bodies before dropping the column
    # referenced by v2.
    op.execute(_ITEMS_INSERT_V1)
    op.execute(_ITEMS_UPDATE_V1)

    # Restore guard_audit_log_insert to exactly what c4d81e2a9f63 shipped.
    op.execute(_AUDIT_INSERT)

    for trigger in reversed(GENERATION_TRIGGERS):
        op.execute(
            f"DROP TRIGGER IF EXISTS {trigger.name} ON public.{trigger.table}"
        )

    for statement in drop_function_statements(GENERATION_FUNCTIONS):
        op.execute(statement)

    op.drop_index(
        "ix_content_items_generation_job", table_name="content_items"
    )
    op.drop_constraint(
        "fk_content_items_generation_job",
        "content_items",
        type_="foreignkey",
    )
    op.drop_column("content_items", "generation_job_id")

    op.drop_index(
        "ix_generation_jobs_status", table_name="content_generation_jobs"
    )
    op.drop_index(
        "ix_generation_jobs_project", table_name="content_generation_jobs"
    )
    op.drop_index(
        "ix_generation_jobs_due", table_name="content_generation_jobs"
    )
    op.drop_table("content_generation_jobs")
    op.execute("DROP TYPE IF EXISTS generation_job_status")
