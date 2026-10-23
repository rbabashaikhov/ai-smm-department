"""topic ideation foundation (SMM-035B3A)

Additive. Two new tables and nothing else changes shape:

* topic_ideation_jobs -- its own queue, claimed only by the new
  public.topic_ideation_claim. content_generation_jobs, generation_claim,
  generation_complete and generation_fail are not touched, so a content
  generation worker cannot claim an ideation job and keeps exactly the
  privileges it has;
* content_ideas -- suggested topics, written only by
  public.topic_ideation_complete; a composite key to
  (topic_ideation_jobs.id, project_id) keeps every idea in its job's
  project.

Guards (ai_smm.db.guards.TOPIC_IDEATION_FUNCTIONS/TRIGGERS) let a Control
Center role insert a freshly queued job for an active project its bound
user owns, and nothing more. The contracts (ai_smm.db.ideation_contracts)
are SECURITY DEFINER with a fixed search_path; EXECUTE is revoked from
PUBLIC and granted to nobody here -- the worker role is a later stage.
Audit actions stay inside generation.%, which guard_audit_log_insert
already accepts, so that function is not replaced.

The operator's grants files under deploy/control-center/db/ are updated in
the same change; applying them, like this migration, is a separately
approved production step.

Downgrade refuses while any ideation job exists: it would destroy them.

Revision ID: b3a7d2e91c4f
Revises: f7a91c2d5e04
Create Date: 2026-10-22
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from ai_smm.db.guards import (
    TOPIC_IDEATION_FUNCTIONS_V1,
    TOPIC_IDEATION_TRIGGERS,
    drop_function_statements,
    revoke_public_statements,
)
from ai_smm.db.ideation_contracts import TOPIC_IDEATION_CONTRACTS_V1
from alembic import op


# The frozen V1 definitions: a migration replays identically whatever the
# code later says (1c9e4b7d2a05 replaces the insert guard and the claim).
TOPIC_IDEATION_FUNCTIONS = TOPIC_IDEATION_FUNCTIONS_V1
TOPIC_IDEATION_CONTRACTS = TOPIC_IDEATION_CONTRACTS_V1


revision: str = "b3a7d2e91c4f"
down_revision: str | None = "f7a91c2d5e04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "topic_ideation_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.String(length=120), nullable=False),
        sa.Column("requested_by_user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "queued",
                "running",
                "succeeded",
                "failed",
                "cancelled",
                name="topic_ideation_job_status",
            ),
            server_default="queued",
            nullable=False,
        ),
        sa.Column("idea_count", sa.Integer(), nullable=False),
        sa.Column("topic_hint", sa.Text(), nullable=True),
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
        sa.Column(
            "max_attempts", sa.Integer(), server_default="2", nullable=False
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
            "idea_count BETWEEN 1 AND 5",
            name="ck_topic_ideation_jobs_idea_count",
        ),
        sa.CheckConstraint(
            "topic_hint IS NULL OR char_length(topic_hint) BETWEEN 1 AND 500",
            name="ck_topic_ideation_jobs_topic_hint",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0", name="ck_topic_ideation_jobs_attempt_count"
        ),
        sa.CheckConstraint(
            "max_attempts BETWEEN 1 AND 10",
            name="ck_topic_ideation_jobs_max_attempts",
        ),
        sa.CheckConstraint(
            "status <> 'running' OR (claimed_by IS NOT NULL "
            "AND claimed_at IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="ck_topic_ideation_jobs_claim_shape",
        ),
        sa.CheckConstraint(
            "status NOT IN ('succeeded', 'failed', 'cancelled') "
            "OR finished_at IS NOT NULL",
            name="ck_topic_ideation_jobs_finished",
        ),
        sa.CheckConstraint(
            "last_error IS NULL OR char_length(last_error) <= 4000",
            name="ck_topic_ideation_jobs_last_error",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(result) = 'object'",
            name="ck_topic_ideation_jobs_result",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_topic_ideation_jobs_project_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"],
            ["users.id"],
            name="fk_topic_ideation_jobs_requested_by",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id", "project_id", name="uq_topic_ideation_jobs_id_project"
        ),
    )
    op.create_index(
        "ix_topic_ideation_jobs_due",
        "topic_ideation_jobs",
        ["next_attempt_at"],
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )
    op.create_index(
        "ix_topic_ideation_jobs_project",
        "topic_ideation_jobs",
        ["project_id", "created_at"],
    )

    op.create_table(
        "content_ideas",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.String(length=120), nullable=False),
        sa.Column("ideation_job_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("angle", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "proposed", "rejected", "used", name="content_idea_status"
            ),
            server_default="proposed",
            nullable=False,
        ),
        sa.Column("generation_job_id", sa.Uuid(), nullable=True),
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
        sa.CheckConstraint("position >= 1", name="ck_content_ideas_position"),
        sa.CheckConstraint(
            "title ~ '[^[:space:]]' AND char_length(title) <= 200",
            name="ck_content_ideas_title",
        ),
        sa.CheckConstraint(
            "description ~ '[^[:space:]]' AND char_length(description) <= 2000",
            name="ck_content_ideas_description",
        ),
        sa.CheckConstraint(
            "angle IS NULL OR (angle ~ '[^[:space:]]' "
            "AND char_length(angle) <= 1000)",
            name="ck_content_ideas_angle",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_content_ideas_project_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["ideation_job_id", "project_id"],
            ["topic_ideation_jobs.id", "topic_ideation_jobs.project_id"],
            name="fk_content_ideas_ideation_job",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["generation_job_id"],
            ["content_generation_jobs.id"],
            name="fk_content_ideas_generation_job",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "ideation_job_id", "position", name="uq_content_ideas_job_position"
        ),
    )
    op.create_index(
        "ix_content_ideas_project",
        "content_ideas",
        ["project_id", "status", "created_at"],
    )

    # Guard functions, PUBLIC's default EXECUTE taken away, then the
    # triggers -- the order every earlier guard migration uses.
    for function in TOPIC_IDEATION_FUNCTIONS:
        op.execute(function.ddl)
    for statement in revoke_public_statements(TOPIC_IDEATION_FUNCTIONS):
        op.execute(statement)
    for trigger in TOPIC_IDEATION_TRIGGERS:
        op.execute(trigger.ddl)

    for function in TOPIC_IDEATION_CONTRACTS:
        op.execute(function.ddl)
    for statement in revoke_public_statements(TOPIC_IDEATION_CONTRACTS):
        op.execute(statement)


def downgrade() -> None:
    jobs = op.get_bind().execute(
        sa.text("SELECT count(*) FROM public.topic_ideation_jobs")
    ).scalar_one()

    if jobs:
        raise RuntimeError(
            f"Refusing to downgrade: {jobs} topic ideation job(s) exist, and "
            "dropping topic_ideation_jobs/content_ideas would destroy them "
            "and their ideas. Export and remove them first, deliberately."
        )

    for statement in drop_function_statements(TOPIC_IDEATION_CONTRACTS):
        op.execute(statement)

    for trigger in reversed(TOPIC_IDEATION_TRIGGERS):
        op.execute(
            f"DROP TRIGGER IF EXISTS {trigger.name} ON public.{trigger.table}"
        )
    for statement in drop_function_statements(TOPIC_IDEATION_FUNCTIONS):
        op.execute(statement)

    op.drop_index("ix_content_ideas_project", table_name="content_ideas")
    op.drop_table("content_ideas")
    op.drop_index(
        "ix_topic_ideation_jobs_project", table_name="topic_ideation_jobs"
    )
    op.drop_index(
        "ix_topic_ideation_jobs_due", table_name="topic_ideation_jobs"
    )
    op.drop_table("topic_ideation_jobs")
    op.execute("DROP TYPE IF EXISTS content_idea_status")
    op.execute("DROP TYPE IF EXISTS topic_ideation_job_status")
