"""api control plane: users, sessions, memberships, project settings

Everything here is additive. Four new tables are created and four columns
are added to projects, each with a server default, so every existing row
is valid the moment the migration commits and no row is read or rewritten.

Nothing the worker uses is touched: publications, publication_attempts,
content_series and audit_log are left exactly as they were, and the
columns projects.id, knowledge_path, default_platform and is_active keep
their definitions. A worker running against the upgraded schema behaves
identically.

Revision ID: 2964ac9ceea7
Revises: 79731eadacd7
Create Date: 2026-10-10
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op


revision: str = "2964ac9ceea7"
down_revision: str | None = "79731eadacd7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The email is stored already normalised (trimmed, lowercased) and the
    # check constraint keeps it that way, which is what lets a plain unique
    # index behave like a case-insensitive one without CITEXT.
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column(
            "is_active", sa.Boolean(), server_default="true", nullable=False
        ),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint(
            "email = lower(email)", name="ck_users_email_lower"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )

    # Only the SHA-256 digest of a session token is stored, so a dump of
    # this table cannot be replayed as a login. Same for the CSRF token.
    op.create_table(
        "user_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("csrf_token_hash", sa.String(length=64), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "token_hash", name="uq_user_sessions_token_hash"
        ),
    )
    op.create_index(
        "ix_user_sessions_user", "user_sessions", ["user_id"], unique=False
    )
    op.create_index(
        "ix_user_sessions_expires_at",
        "user_sessions",
        ["expires_at"],
        unique=False,
    )

    op.create_table(
        "project_memberships",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.String(length=120), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "role",
            sa.Enum(
                "viewer",
                "editor",
                "admin",
                "owner",
                name="membership_role",
            ),
            nullable=False,
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
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "project_id", "user_id", name="uq_project_memberships_pair"
        ),
    )
    op.create_index(
        "ix_project_memberships_project",
        "project_memberships",
        ["project_id"],
        unique=False,
    )
    op.create_index(
        "ix_project_memberships_user",
        "project_memberships",
        ["user_id"],
        unique=False,
    )

    # One row per project at most, and the row itself is optional: a
    # project that predates this table has none and the API answers with
    # documented defaults until the first PATCH creates it.
    op.create_table(
        "project_settings",
        sa.Column("project_id", sa.String(length=120), nullable=False),
        sa.Column(
            "content_config",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "publishing_config",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "brand_config",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "version", sa.Integer(), server_default="1", nullable=False
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("project_id"),
    )

    # Every one has a server default, so existing projects stay valid and
    # the importer, the CLI and the worker keep inserting as before.
    op.add_column(
        "projects",
        sa.Column("description", sa.Text(), server_default="", nullable=False),
    )
    op.add_column(
        "projects",
        sa.Column(
            "default_timezone",
            sa.String(length=64),
            server_default="Europe/Moscow",
            nullable=False,
        ),
    )
    op.add_column(
        "projects",
        sa.Column(
            "default_language",
            sa.String(length=16),
            server_default="ru",
            nullable=False,
        ),
    )
    op.add_column(
        "projects",
        sa.Column(
            "version", sa.Integer(), server_default="1", nullable=False
        ),
    )


def downgrade() -> None:
    for column in (
        "version",
        "default_language",
        "default_timezone",
        "description",
    ):
        op.drop_column("projects", column)

    op.drop_table("project_settings")

    op.drop_index(
        "ix_project_memberships_user", table_name="project_memberships"
    )
    op.drop_index(
        "ix_project_memberships_project", table_name="project_memberships"
    )
    op.drop_table("project_memberships")

    op.drop_index("ix_user_sessions_expires_at", table_name="user_sessions")
    op.drop_index("ix_user_sessions_user", table_name="user_sessions")
    op.drop_table("user_sessions")

    op.drop_table("users")

    # Dropping the table does not drop the enum type it used.
    sa.Enum(name="membership_role").drop(op.get_bind(), checkfirst=True)
