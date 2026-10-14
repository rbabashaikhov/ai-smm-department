"""business-mutation guards: session-bound transactions, step-up approval
grants, publication lineage and audit provenance (SMM-025D)

Additive. One nullable column on audit_log (written_by, set by a trigger
from session_user), two tables (mutation_contexts, approval_grants), the
guard functions of ai_smm.db.guards version 1 and their triggers. No
existing row is read or rewritten: historical audit rows keep
written_by NULL ("before provenance existed").

The triggers only constrain roles that are not the table owner. The
worker, migrations and the operator's CLI run as the owner and are not
affected; nor is the read-only API's sign-in audit (auth.* only). The
API's business-mutation role can then write nothing without a
transaction bound to a live owner session, and cannot approve without a
step-up grant from smm-auth. Design: docs/adr/ADR-025D-database-enforced-
mutation-safety.md.

Downgrade removes those protections. It refuses unless the operator
states AI_SMM_DOWNGRADE_REMOVES_MUTATION_GUARDS=yes, and it also drops the
provenance of every audit row written since the upgrade. With business
mutations on, never downgrade: switch mutations off first
(cc-compose.sh mutations disable).

Revision ID: c4d81e2a9f63
Revises: a7c2e5f19b40
Create Date: 2026-10-14
"""
from __future__ import annotations

import os

import sqlalchemy as sa

from alembic import op
from ai_smm.db.guards import (
    FUNCTIONS_V1,
    TRIGGERS_V1,
    drop_function_statements,
    revoke_public_statements,
)


revision: str = "c4d81e2a9f63"
down_revision: str | None = "a7c2e5f19b40"
branch_labels = None
depends_on = None


DOWNGRADE_SWITCH = "AI_SMM_DOWNGRADE_REMOVES_MUTATION_GUARDS"


def upgrade() -> None:
    # The bound the `migrate` service passes through PGOPTIONS applies, as
    # for every revision. The column is added without a default, so every
    # existing row is NULL ("written before provenance existed", not a
    # guess) and audit_log is not rewritten; the default comes after.
    op.add_column(
        "audit_log",
        sa.Column("written_by", sa.String(length=63), nullable=True),
    )
    op.alter_column(
        "audit_log", "written_by", server_default=sa.text("SESSION_USER")
    )

    op.create_table(
        "mutation_contexts",
        sa.Column("txid", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column(
            "bound_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_mutation_contexts_user",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("txid"),
    )
    op.create_index(
        "ix_mutation_contexts_bound_at", "mutation_contexts", ["bound_at"]
    )

    op.create_table(
        "approval_grants",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("content_item_id", sa.Uuid(), nullable=False),
        sa.Column("revision_id", sa.Uuid(), nullable=False),
        sa.Column("social_account_id", sa.Uuid(), nullable=True),
        sa.Column("external_account_id", sa.String(length=120), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approval_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint(
            "expires_at > created_at", name="ck_approval_grants_expiry"
        ),
        sa.CheckConstraint(
            "(consumed_at IS NULL) = (approval_id IS NULL)",
            name="ck_approval_grants_consumed",
        ),
        sa.CheckConstraint(
            "length(content_hash) = 64", name="ck_approval_grants_hash"
        ),
        sa.ForeignKeyConstraint(
            ["content_item_id", "revision_id"],
            ["content_revisions.content_item_id", "content_revisions.id"],
            name="fk_approval_grants_revision",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["social_account_id"],
            ["social_accounts.id"],
            name="fk_approval_grants_social_account",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_approval_grants_user",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("approval_id", name="uq_approval_grants_approval"),
    )
    op.create_index("ix_approval_grants_revision", "approval_grants", ["revision_id"])
    op.create_index("ix_approval_grants_user", "approval_grants", ["user_id"])

    # Every function, then PUBLIC's default EXECUTE taken away in the same
    # transaction: no committed state lets PUBLIC call one. The grants
    # files give the business role what it needs (EXECUTE only).
    for function in FUNCTIONS_V1:
        op.execute(function.ddl)

    for statement in revoke_public_statements(FUNCTIONS_V1):
        op.execute(statement)

    for trigger in TRIGGERS_V1:
        op.execute(trigger.ddl)


def downgrade() -> None:
    if os.environ.get(DOWNGRADE_SWITCH, "").strip() != "yes":
        raise RuntimeError(
            "Refusing to downgrade c4d81e2a9f63: it removes the database "
            "guards of business mutations and the audit provenance written "
            "since the upgrade. Switch mutations off first; then, if that "
            f"is really intended, set {DOWNGRADE_SWITCH}=yes."
        )

    for trigger in reversed(TRIGGERS_V1):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger.name} ON public.{trigger.table}")

    for statement in drop_function_statements(FUNCTIONS_V1):
        op.execute(statement)

    op.drop_index("ix_approval_grants_user", table_name="approval_grants")
    op.drop_index("ix_approval_grants_revision", table_name="approval_grants")
    op.drop_table("approval_grants")
    op.drop_index("ix_mutation_contexts_bound_at", table_name="mutation_contexts")
    op.drop_table("mutation_contexts")
    op.drop_column("audit_log", "written_by")
