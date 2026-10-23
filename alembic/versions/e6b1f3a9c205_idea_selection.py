"""topic idea selection (SMM-035B3B2)

Additive. Nothing is rewritten, no row changes, and no existing function is
touched:

* content_generation_jobs gets UNIQUE (id, project_id), the target of a
  composite key. id is already the primary key, so the constraint adds no
  restriction of its own; building it briefly locks that small table;
* content_ideas.generation_job_id is re-pointed from a single-column key
  on content_generation_jobs(id) to (generation_job_id, project_id) ->
  content_generation_jobs(id, project_id): an idea can only be linked to a
  job of its own project, enforced by PostgreSQL. A NULL job id is not
  checked (the default MATCH SIMPLE), as before;
* a job can be linked by at most one idea (UNIQUE generation_job_id; NULLs
  are distinct), so no generation job is consumed twice;
* status = 'used' exactly when generation_job_id is set (CHECK). Every
  existing row is `proposed` with no job, so it satisfies the check;
* three SECURITY DEFINER contracts (ai_smm.db.idea_selection_contracts)
  for rejecting and using an idea. EXECUTE is revoked from PUBLIC here and
  granted to the business role by the operator's grants file under
  deploy/control-center/db/, a separately approved production step like
  this migration.

Downgrade restores the single-column key and drops the rest. It loses no
data: a `used` idea keeps its status and job link, only the checks go.

Revision ID: e6b1f3a9c205
Revises: 1c9e4b7d2a05
Create Date: 2026-10-23
"""
from __future__ import annotations

import sqlalchemy as sa

from ai_smm.db.guards import drop_function_statements, revoke_public_statements
from ai_smm.db.idea_selection_contracts import IDEA_SELECTION_CONTRACTS
from alembic import op


revision: str = "e6b1f3a9c205"
down_revision: str | None = "1c9e4b7d2a05"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_content_generation_jobs_id_project",
        "content_generation_jobs",
        ["id", "project_id"],
    )

    op.drop_constraint(
        "fk_content_ideas_generation_job", "content_ideas", type_="foreignkey"
    )
    op.create_foreign_key(
        "fk_content_ideas_generation_job",
        "content_ideas",
        "content_generation_jobs",
        ["generation_job_id", "project_id"],
        ["id", "project_id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_content_ideas_generation_job", "content_ideas", ["generation_job_id"]
    )
    op.create_check_constraint(
        "ck_content_ideas_used_job",
        "content_ideas",
        "(status = 'used') = (generation_job_id IS NOT NULL)",
    )

    for function in IDEA_SELECTION_CONTRACTS:
        op.execute(function.ddl)
    for statement in revoke_public_statements(IDEA_SELECTION_CONTRACTS):
        op.execute(statement)


def downgrade() -> None:
    for statement in drop_function_statements(IDEA_SELECTION_CONTRACTS):
        op.execute(statement)

    op.drop_constraint("ck_content_ideas_used_job", "content_ideas", type_="check")
    op.drop_constraint(
        "uq_content_ideas_generation_job", "content_ideas", type_="unique"
    )
    op.drop_constraint(
        "fk_content_ideas_generation_job", "content_ideas", type_="foreignkey"
    )
    op.create_foreign_key(
        "fk_content_ideas_generation_job",
        "content_ideas",
        "content_generation_jobs",
        ["generation_job_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.drop_constraint(
        "uq_content_generation_jobs_id_project",
        "content_generation_jobs",
        type_="unique",
    )
