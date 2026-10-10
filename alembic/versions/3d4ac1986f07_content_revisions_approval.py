"""content items, revisions and approvals

The editorial layer in front of the delivery queue. Additive only: four
new tables, three enum types, two trigger functions and three triggers.
No existing table is altered, no existing row is read or rewritten, and
the worker reads none of the new tables, so it behaves identically on the
upgraded schema.

content_publication_links is the authoritative editorial -> delivery
mapping. It references publications.id from the new side; publications
itself gains no column.

content_items and content_revisions refer to each other (an item points
at its current and approved revision; a revision belongs to an item), so
the two composite foreign keys from items to revisions are added after
both tables exist, and dropped before either is removed.

Revision ID: 3d4ac1986f07
Revises: 2964ac9ceea7
Create Date: 2026-10-10
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op


revision: str = "3d4ac1986f07"
down_revision: str | None = "2964ac9ceea7"
branch_labels = None
depends_on = None


APPEND_ONLY_FUNCTION = "content_reject_mutation"
LINK_CHECK_FUNCTION = "content_publication_link_check"


def upgrade() -> None:
    op.create_table(
        "content_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.String(length=120), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column(
            "content_type",
            sa.String(length=40),
            server_default="post",
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "draft",
                "in_review",
                "approved",
                "rejected",
                "archived",
                name="content_status",
            ),
            server_default="draft",
            nullable=False,
        ),
        sa.Column("current_revision_id", sa.Uuid(), nullable=False),
        sa.Column("approved_revision_id", sa.Uuid(), nullable=True),
        sa.Column(
            "version", sa.Integer(), server_default="1", nullable=False
        ),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
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
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        # An item is approved exactly when an approved revision is set,
        # and that revision is always the current one: an approval of an
        # older revision cannot be in effect.
        sa.CheckConstraint(
            "(status = 'approved') = (approved_revision_id IS NOT NULL)",
            name="ck_content_items_approved_has_revision",
        ),
        sa.CheckConstraint(
            "approved_revision_id IS NULL"
            " OR approved_revision_id = current_revision_id",
            name="ck_content_items_approval_is_current",
        ),
        sa.CheckConstraint(
            "status <> 'archived' OR archived_at IS NOT NULL",
            name="ck_content_items_archived_at",
        ),
        sa.CheckConstraint(
            "length(content_type) > 0", name="ck_content_items_content_type"
        ),
        sa.CheckConstraint("version >= 1", name="ck_content_items_version"),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_content_items_project_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name="fk_content_items_created_by",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_content_items_project", "content_items", ["project_id"]
    )
    op.create_index("ix_content_items_status", "content_items", ["status"])

    op.create_table(
        "content_revisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("content_item_id", sa.Uuid(), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("format", sa.String(length=40), nullable=False),
        sa.Column(
            "images",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
        sa.Column(
            "items",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "source",
            sa.Enum(
                "human",
                "strategist",
                "copywriter",
                "editor",
                "import",
                name="content_revision_source",
            ),
            nullable=False,
        ),
        sa.Column("source_ref", sa.Text(), nullable=True),
        sa.Column(
            "editor_score", sa.Numeric(precision=3, scale=1), nullable=True
        ),
        sa.Column(
            "editor_notes", sa.Text(), server_default="", nullable=False
        ),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "revision_number >= 1", name="ck_content_revisions_number"
        ),
        # The same set publications.format allows, so a materialised
        # revision can never fail the delivery table's own check.
        sa.CheckConstraint(
            "format in ('text', 'image', 'carousel', 'thread')",
            name="ck_content_revisions_format",
        ),
        sa.CheckConstraint(
            "length(content_hash) = 64", name="ck_content_revisions_hash"
        ),
        sa.ForeignKeyConstraint(
            ["content_item_id"],
            ["content_items.id"],
            name="fk_content_revisions_item",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name="fk_content_revisions_created_by",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "content_item_id",
            "revision_number",
            name="uq_content_revisions_item_number",
        ),
        # Target of the composite foreign keys below.
        sa.UniqueConstraint(
            "content_item_id", "id", name="uq_content_revisions_item_id"
        ),
    )

    # Now that both tables exist, close the cycle. Composite keys, so a
    # current or approved revision can only ever be a revision of the
    # same item. The current-revision key is deferred to commit because
    # an item and its first revision are inserted in one transaction.
    op.create_foreign_key(
        "fk_content_items_current_revision",
        "content_items",
        "content_revisions",
        ["id", "current_revision_id"],
        ["content_item_id", "id"],
        ondelete="RESTRICT",
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_foreign_key(
        "fk_content_items_approved_revision",
        "content_items",
        "content_revisions",
        ["id", "approved_revision_id"],
        ["content_item_id", "id"],
        ondelete="RESTRICT",
    )

    op.create_table(
        "content_approvals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("content_item_id", sa.Uuid(), nullable=False),
        sa.Column("revision_id", sa.Uuid(), nullable=False),
        sa.Column(
            "decision",
            sa.Enum(
                "approved", "rejected", name="content_approval_decision"
            ),
            nullable=False,
        ),
        sa.Column("actor_user_id", sa.Uuid(), nullable=False),
        sa.Column("note", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["content_item_id", "revision_id"],
            ["content_revisions.content_item_id", "content_revisions.id"],
            name="fk_content_approvals_revision",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name="fk_content_approvals_actor",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_content_approvals_item_created",
        "content_approvals",
        ["content_item_id", "created_at"],
    )
    op.create_index(
        "ix_content_approvals_revision", "content_approvals", ["revision_id"]
    )

    # Which revision each delivery Publication's current snapshot came
    # from. One row per Publication (UNIQUE publication_id); one
    # Publication per revision and platform (the primary key).
    op.create_table(
        "content_publication_links",
        sa.Column("content_item_id", sa.Uuid(), nullable=False),
        sa.Column("revision_id", sa.Uuid(), nullable=False),
        sa.Column("platform", sa.String(length=40), nullable=False),
        sa.Column("publication_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(platform) > 0",
            name="ck_content_publication_links_platform",
        ),
        sa.ForeignKeyConstraint(
            ["content_item_id"],
            ["content_items.id"],
            name="fk_content_publication_links_item",
            ondelete="RESTRICT",
        ),
        # The revision must belong to the item named on the same row.
        sa.ForeignKeyConstraint(
            ["content_item_id", "revision_id"],
            ["content_revisions.content_item_id", "content_revisions.id"],
            name="fk_content_publication_links_revision",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["publication_id"],
            ["publications.id"],
            name="fk_content_publication_links_publication",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("content_item_id", "revision_id", "platform"),
        sa.UniqueConstraint(
            "publication_id", name="uq_content_publication_links_publication"
        ),
    )

    # The Publication must be in the item's project and on the link's
    # platform. No foreign key can say that without a composite unique
    # key on publications, which this migration does not alter.
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {LINK_CHECK_FUNCTION}() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            publication_project text;
            publication_platform text;
            item_project text;
        BEGIN
            SELECT project_id, platform
              INTO publication_project, publication_platform
              FROM publications WHERE id = NEW.publication_id;

            SELECT project_id INTO item_project
              FROM content_items WHERE id = NEW.content_item_id;

            IF publication_project IS DISTINCT FROM item_project
               OR publication_platform IS DISTINCT FROM NEW.platform THEN
                RAISE EXCEPTION
                    'publication % is not in the project and on the platform of '
                    'content item %', NEW.publication_id, NEW.content_item_id
                    USING ERRCODE = 'foreign_key_violation';
            END IF;

            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_content_publication_links_consistent "
        "BEFORE INSERT OR UPDATE ON content_publication_links "
        f"FOR EACH ROW EXECUTE FUNCTION {LINK_CHECK_FUNCTION}()"
    )

    # Revisions are immutable and approvals are append-only, for every
    # client of the database and not only for this application.
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {APPEND_ONLY_FUNCTION}() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION '% is append-only: % is not allowed',
                TG_TABLE_NAME, TG_OP
                USING ERRCODE = 'restrict_violation';
        END
        $$
        """
    )

    for table in ("content_revisions", "content_approvals"):
        op.execute(
            f"CREATE TRIGGER trg_{table}_append_only "
            f"BEFORE UPDATE OR DELETE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION {APPEND_ONLY_FUNCTION}()"
        )


def downgrade() -> None:
    # Triggers go with their tables; the functions are dropped explicitly
    # once nothing uses them.
    op.drop_table("content_publication_links")
    op.execute(f"DROP FUNCTION IF EXISTS {LINK_CHECK_FUNCTION}()")

    op.drop_index(
        "ix_content_approvals_revision", table_name="content_approvals"
    )
    op.drop_index(
        "ix_content_approvals_item_created", table_name="content_approvals"
    )
    op.drop_table("content_approvals")

    # Open the cycle before removing either side of it.
    op.drop_constraint(
        "fk_content_items_approved_revision",
        "content_items",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_content_items_current_revision",
        "content_items",
        type_="foreignkey",
    )

    op.drop_table("content_revisions")

    op.drop_index("ix_content_items_status", table_name="content_items")
    op.drop_index("ix_content_items_project", table_name="content_items")
    op.drop_table("content_items")

    op.execute(f"DROP FUNCTION IF EXISTS {APPEND_ONLY_FUNCTION}()")

    # Dropping a table does not drop the enum types it used.
    for enum_name in (
        "content_approval_decision",
        "content_revision_source",
        "content_status",
    ):
        sa.Enum(name=enum_name).drop(op.get_bind(), checkfirst=True)
