"""social accounts, sealed credentials and account-bound publications

Additive. Two tables (social_accounts, social_account_credentials), one
nullable column on content_revisions and one on publications, each with
its foreign key and index, and one trigger on publications. No existing
row is read or rewritten: every existing publication keeps
social_account_id NULL, i.e. it stays a legacy row published with the
deployment token (for allow-listed projects, ai_smm.publishing.routing).

The trigger trg_publications_account_claim_guard is the barrier between
an old and a new worker: an old worker never declares account routing,
so it cannot claim an account-bound row and post it as the deployment
account. Rows existing at upgrade time are all account-less, so the
trigger changes nothing for them.

Downgrade refuses while any account-bound publication is still live:
dropping the column would turn it into a legacy row, and a legacy row is
published with the deployment token -- the wrong account. That refusal
has no override.

Downgrade also DESTROYS DATA once accounts exist: every social account,
every stored credential, the destination of every revision and the
account attribution of every publication, published ones included. It
therefore refuses while any account or credential row exists, unless the
operator states AI_SMM_DOWNGRADE_DESTROYS_ACCOUNT_DATA=yes. With real
account data, roll back the application and keep the schema instead
(docs/reports/smm-025b/migration-rollback.md).

Revision ID: a7c2e5f19b40
Revises: 3d4ac1986f07
Create Date: 2026-10-13
"""
from __future__ import annotations

import os

import sqlalchemy as sa

from alembic import op


revision: str = "a7c2e5f19b40"
down_revision: str | None = "3d4ac1986f07"
branch_labels = None
depends_on = None


GUARD_FUNCTION = "publication_account_claim_guard"
DESTROY_SWITCH = "AI_SMM_DOWNGRADE_DESTROYS_ACCOUNT_DATA"
GUARD_TRIGGER = "trg_publications_account_claim_guard"

GUARD_FUNCTION_DDL = f"""
CREATE OR REPLACE FUNCTION {GUARD_FUNCTION}() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.social_account_id IS NOT NULL
       AND current_setting('ai_smm.account_routing', true)
           IS DISTINCT FROM 'on' THEN
        RAISE EXCEPTION
            'publication % is bound to social account %; only an '
            'account-routing worker may claim it', NEW.id,
            NEW.social_account_id
            USING ERRCODE = 'insufficient_privilege';
    END IF;

    RETURN NEW;
END
$$
"""

GUARD_TRIGGER_DDL = (
    f"CREATE TRIGGER {GUARD_TRIGGER} "
    "BEFORE UPDATE OF status ON publications "
    "FOR EACH ROW WHEN (NEW.status = 'claimed' "
    "AND OLD.status IS DISTINCT FROM 'claimed') "
    f"EXECUTE FUNCTION {GUARD_FUNCTION}()"
)


def upgrade() -> None:
    # No lock_timeout of its own: like every revision, this one runs with
    # the bound the `migrate` service passes through PGOPTIONS
    # (compose.control-center.yml), so a live worker's lock makes it give
    # up instead of queueing every reader of publications behind it.
    op.create_table(
        "social_accounts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.String(length=120), nullable=False),
        sa.Column("platform", sa.String(length=40), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default="active",
            nullable=False,
        ),
        sa.Column("external_account_id", sa.String(length=120), nullable=True),
        sa.Column("external_username", sa.String(length=120), nullable=True),
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
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "platform in ('threads')", name="ck_social_accounts_platform"
        ),
        sa.CheckConstraint(
            "status in ('active', 'disabled')",
            name="ck_social_accounts_status",
        ),
        sa.CheckConstraint(
            "status <> 'disabled' OR disabled_at IS NOT NULL",
            name="ck_social_accounts_disabled_at",
        ),
        sa.CheckConstraint(
            "length(btrim(display_name)) > 0",
            name="ck_social_accounts_display_name",
        ),
        sa.CheckConstraint("version >= 1", name="ck_social_accounts_version"),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_social_accounts_project_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name="fk_social_accounts_created_by",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "project_id", "id", name="uq_social_accounts_project_id"
        ),
    )
    op.create_index(
        "ix_social_accounts_project", "social_accounts", ["project_id"]
    )
    op.create_index(
        "uq_social_accounts_project_external",
        "social_accounts",
        ["project_id", "platform", "external_account_id"],
        unique=True,
        postgresql_where=sa.text("external_account_id IS NOT NULL"),
    )

    op.create_table(
        "social_account_credentials",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("social_account_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("scheme", sa.String(length=64), nullable=False),
        sa.Column("key_id", sa.String(length=32), nullable=False),
        sa.Column("ephemeral_public_key", sa.LargeBinary(), nullable=True),
        sa.Column("nonce", sa.LargeBinary(), nullable=True),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("fingerprint", sa.String(length=12), nullable=False),
        sa.Column(
            "verification_status",
            sa.String(length=16),
            server_default="unverified",
            nullable=False,
        ),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "superseded_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status in ('active', 'superseded', 'revoked')",
            name="ck_social_account_credentials_status",
        ),
        sa.CheckConstraint(
            "(status = 'active') = ("
            "ciphertext IS NOT NULL AND nonce IS NOT NULL"
            " AND ephemeral_public_key IS NOT NULL)",
            name="ck_social_account_credentials_material",
        ),
        sa.CheckConstraint(
            "status <> 'revoked' OR revoked_at IS NOT NULL",
            name="ck_social_account_credentials_revoked_at",
        ),
        sa.CheckConstraint(
            "status <> 'superseded' OR superseded_at IS NOT NULL",
            name="ck_social_account_credentials_superseded_at",
        ),
        sa.CheckConstraint(
            "verification_status in ('unverified', 'verified')",
            name="ck_social_account_credentials_verification",
        ),
        sa.CheckConstraint(
            "length(fingerprint) = 12",
            name="ck_social_account_credentials_fingerprint",
        ),
        sa.ForeignKeyConstraint(
            ["social_account_id"],
            ["social_accounts.id"],
            name="fk_social_account_credentials_account",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name="fk_social_account_credentials_created_by",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_social_account_credentials_account",
        "social_account_credentials",
        ["social_account_id"],
    )
    op.create_index(
        "uq_social_account_credentials_one_active",
        "social_account_credentials",
        ["social_account_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )

    # Destination of a revision: approved together with its text.
    op.add_column(
        "content_revisions",
        sa.Column("social_account_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_content_revisions_social_account",
        "content_revisions",
        "social_accounts",
        ["social_account_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    # Destination of a delivery snapshot. Nullable, no default: adding it
    # rewrites no row and takes only a brief lock.
    op.add_column(
        "publications",
        sa.Column("social_account_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_publications_social_account",
        "publications",
        "social_accounts",
        ["project_id", "social_account_id"],
        ["project_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_publications_social_account",
        "publications",
        ["social_account_id"],
    )

    op.execute(GUARD_FUNCTION_DDL)
    op.execute(GUARD_TRIGGER_DDL)


def downgrade() -> None:
    bind = op.get_bind()
    live = bind.execute(
        sa.text(
            "SELECT count(*) FROM publications "
            "WHERE social_account_id IS NOT NULL "
            "AND status NOT IN ('published', 'cancelled')"
        )
    ).scalar_one()

    if live:
        raise RuntimeError(
            f"Refusing to downgrade: {live} account-bound publication(s) "
            "are not published or cancelled. Without social_account_id "
            "they would be published with the deployment token, i.e. as "
            "the wrong account. Cancel them first."
        )

    accounts, credentials = bind.execute(
        sa.text(
            "SELECT (SELECT count(*) FROM social_accounts), "
            "(SELECT count(*) FROM social_account_credentials)"
        )
    ).one()

    if (accounts or credentials) and os.environ.get(
        DESTROY_SWITCH, ""
    ).strip().lower() != "yes":
        raise RuntimeError(
            f"Refusing to downgrade: it would permanently delete {accounts} "
            f"social account(s), {credentials} credential row(s), and the "
            "account of every revision and publication. Roll back the "
            "application and keep the schema instead; or, to destroy that "
            f"data deliberately, set {DESTROY_SWITCH}=yes."
        )

    op.execute(f"DROP TRIGGER IF EXISTS {GUARD_TRIGGER} ON publications")
    op.execute(f"DROP FUNCTION IF EXISTS {GUARD_FUNCTION}()")

    op.drop_index("ix_publications_social_account", table_name="publications")
    op.drop_constraint(
        "fk_publications_social_account", "publications", type_="foreignkey"
    )
    op.drop_column("publications", "social_account_id")

    op.drop_constraint(
        "fk_content_revisions_social_account",
        "content_revisions",
        type_="foreignkey",
    )
    op.drop_column("content_revisions", "social_account_id")

    op.drop_index(
        "uq_social_account_credentials_one_active",
        table_name="social_account_credentials",
    )
    op.drop_index(
        "ix_social_account_credentials_account",
        table_name="social_account_credentials",
    )
    op.drop_table("social_account_credentials")

    op.drop_index(
        "uq_social_accounts_project_external", table_name="social_accounts"
    )
    op.drop_index("ix_social_accounts_project", table_name="social_accounts")
    op.drop_table("social_accounts")
