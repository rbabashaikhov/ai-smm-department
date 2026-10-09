"""content series and narrative links

Adds content_series and the optional links that place a publication inside
one. Every added column is nullable and every link is optional, so the
publications that existed before series were introduced keep working
exactly as they did: the migration reads no existing row and changes none.

Revision ID: 79731eadacd7
Revises: a3fbcef157c3
Create Date: 2026-10-09
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision: str = "79731eadacd7"
down_revision: str | None = "a3fbcef157c3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "content_series",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.String(length=120), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column(
            "description", sa.Text(), server_default="", nullable=False
        ),
        sa.Column(
            "narrative_goal", sa.Text(), server_default="", nullable=False
        ),
        sa.Column(
            "target_audience", sa.Text(), server_default="", nullable=False
        ),
        sa.Column(
            "publishing_strategy",
            sa.Enum(
                "standalone_series",
                "reply_thread",
                name="publishing_strategy",
            ),
            server_default="standalone_series",
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "draft",
                "active",
                "completed",
                "cancelled",
                name="series_status",
            ),
            server_default="draft",
            nullable=False,
        ),
        sa.Column(
            "enforce_order",
            sa.Boolean(),
            server_default="true",
            nullable=False,
        ),
        sa.Column("planned_total", sa.Integer(), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_content_series_project", "content_series", ["project_id"]
    )
    op.create_index(
        "ix_content_series_status", "content_series", ["status"]
    )

    # All nullable: existing rows get NULL and keep behaving as before.
    op.add_column(
        "publications", sa.Column("series_id", sa.BigInteger(), nullable=True)
    )
    op.add_column(
        "publications",
        sa.Column("series_position", sa.Integer(), nullable=True),
    )
    op.add_column(
        "publications", sa.Column("series_total", sa.Integer(), nullable=True)
    )
    op.add_column(
        "publications",
        sa.Column("parent_publication_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "publications",
        sa.Column("previous_publication_id", sa.BigInteger(), nullable=True),
    )

    op.create_index("ix_publications_series", "publications", ["series_id"])
    # Partial: only rows that are in a series compete for a position, so
    # the many rows with series_id NULL are unaffected.
    op.create_index(
        "uq_publications_series_position",
        "publications",
        ["series_id", "series_position"],
        unique=True,
        postgresql_where=sa.text("series_id IS NOT NULL"),
    )

    op.create_foreign_key(
        "fk_publications_series_id",
        "publications",
        "content_series",
        ["series_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_publications_parent_publication_id",
        "publications",
        "publications",
        ["parent_publication_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_publications_previous_publication_id",
        "publications",
        "publications",
        ["previous_publication_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # Membership is all-or-nothing: a position without a series, or a
    # series without a position, would make ordering undecidable.
    op.create_check_constraint(
        "ck_publications_series_membership",
        "publications",
        "(series_id IS NULL AND series_position IS NULL)"
        " OR (series_id IS NOT NULL AND series_position IS NOT NULL"
        "     AND series_position >= 1)",
    )
    op.create_check_constraint(
        "ck_publications_series_total",
        "publications",
        "series_total IS NULL OR series_total >= 1",
    )
    op.create_check_constraint(
        "ck_publications_parent_not_self",
        "publications",
        "parent_publication_id IS NULL OR parent_publication_id <> id",
    )
    op.create_check_constraint(
        "ck_publications_previous_not_self",
        "publications",
        "previous_publication_id IS NULL OR previous_publication_id <> id",
    )


def downgrade() -> None:
    for name in (
        "ck_publications_previous_not_self",
        "ck_publications_parent_not_self",
        "ck_publications_series_total",
        "ck_publications_series_membership",
    ):
        op.drop_constraint(name, "publications", type_="check")

    for name in (
        "fk_publications_previous_publication_id",
        "fk_publications_parent_publication_id",
        "fk_publications_series_id",
    ):
        op.drop_constraint(name, "publications", type_="foreignkey")

    op.drop_index(
        "uq_publications_series_position",
        table_name="publications",
        postgresql_where=sa.text("series_id IS NOT NULL"),
    )
    op.drop_index("ix_publications_series", table_name="publications")

    for column in (
        "previous_publication_id",
        "parent_publication_id",
        "series_total",
        "series_position",
        "series_id",
    ):
        op.drop_column("publications", column)

    op.drop_index("ix_content_series_status", table_name="content_series")
    op.drop_index("ix_content_series_project", table_name="content_series")
    op.drop_table("content_series")

    # Table removal does not drop the enum types.
    sa.Enum(name="series_status").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="publishing_strategy").drop(op.get_bind(), checkfirst=True)
