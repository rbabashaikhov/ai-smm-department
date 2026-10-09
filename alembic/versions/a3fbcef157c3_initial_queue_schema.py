"""initial queue schema

Creates the AI SMM publication queue: projects, publications,
publication_attempts and audit_log, plus the three enum types.

Applied only to the ai_smm database; it touches no other schema.

Revision ID: a3fbcef157c3
Revises: 
Create Date: 2026-10-09 10:16:59.962699+00:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'a3fbcef157c3'
down_revision: str | None = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('audit_log',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('actor', sa.String(length=160), nullable=False),
    sa.Column('action', sa.String(length=120), nullable=False),
    sa.Column('subject', sa.String(length=160), nullable=True),
    sa.Column('details', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_audit_log_at', 'audit_log', ['at'], unique=False)
    op.create_index('ix_audit_log_subject', 'audit_log', ['subject'], unique=False)
    op.create_table('projects',
    sa.Column('id', sa.String(length=120), nullable=False),
    sa.Column('display_name', sa.String(length=255), nullable=False),
    sa.Column('knowledge_path', sa.Text(), nullable=False),
    sa.Column('default_platform', sa.String(length=40), server_default='threads', nullable=False),
    sa.Column('is_active', sa.Boolean(), server_default='true', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('publications',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('project_id', sa.String(length=120), nullable=False),
    sa.Column('platform', sa.String(length=40), server_default='threads', nullable=False),
    sa.Column('ordinal', sa.Integer(), nullable=False),
    sa.Column('title', sa.Text(), nullable=False),
    sa.Column('format', sa.String(length=40), nullable=False),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('images', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False),
    sa.Column('items', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False),
    sa.Column('status', sa.Enum('draft', 'approved', 'scheduled', 'claimed', 'publishing', 'published', 'failed', 'needs_review', 'cancelled', name='publication_status'), server_default='draft', nullable=False),
    sa.Column('scheduled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('idempotency_key', sa.String(length=200), nullable=False),
    sa.Column('threads_post_id', sa.String(length=120), nullable=True),
    sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('claimed_by', sa.String(length=120), nullable=True),
    sa.Column('claimed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('attempt_count', sa.Integer(), server_default='0', nullable=False),
    sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('editor_score', sa.Numeric(precision=3, scale=1), nullable=True),
    sa.Column('human_reviewed', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('source_ref', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("format in ('text', 'image', 'carousel', 'thread')", name='ck_publications_format'),
    sa.CheckConstraint("status <> 'published' OR (threads_post_id IS NOT NULL AND published_at IS NOT NULL)", name='ck_publications_published_has_id'),
    sa.CheckConstraint('attempt_count >= 0', name='ck_publications_attempt_count'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('idempotency_key', name='uq_publications_idempotency_key'),
    sa.UniqueConstraint('project_id', 'platform', 'ordinal', name='uq_publications_project_platform_ordinal'),
    sa.UniqueConstraint('threads_post_id', name='uq_publications_threads_post_id')
    )
    op.create_index('ix_publications_due', 'publications', ['scheduled_at'], unique=False, postgresql_where="status = 'scheduled'")
    op.create_index('ix_publications_project', 'publications', ['project_id'], unique=False)
    op.create_index('ix_publications_status', 'publications', ['status'], unique=False)
    op.create_table('publication_attempts',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('publication_id', sa.BigInteger(), nullable=False),
    sa.Column('attempt_number', sa.Integer(), nullable=False),
    sa.Column('worker_id', sa.String(length=120), nullable=False),
    sa.Column('phase', sa.Enum('preflight', 'container', 'publish', 'verify', name='attempt_phase'), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('outcome', sa.Enum('success', 'error', 'timeout', 'unknown', 'dry_run', name='attempt_outcome'), nullable=True),
    sa.Column('threads_creation_id', sa.String(length=120), nullable=True),
    sa.Column('threads_post_id', sa.String(length=120), nullable=True),
    sa.Column('http_status', sa.Integer(), nullable=True),
    sa.Column('error_type', sa.String(length=120), nullable=True),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.Column('details', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.ForeignKeyConstraint(['publication_id'], ['publications.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('publication_id', 'attempt_number', name='uq_attempts_publication_attempt_number')
    )
    op.create_index('ix_attempts_outcome', 'publication_attempts', ['outcome'], unique=False)
    op.create_index('ix_attempts_publication', 'publication_attempts', ['publication_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_attempts_publication', table_name='publication_attempts')
    op.drop_index('ix_attempts_outcome', table_name='publication_attempts')
    op.drop_table('publication_attempts')
    op.drop_index('ix_publications_status', table_name='publications')
    op.drop_index('ix_publications_project', table_name='publications')
    op.drop_index('ix_publications_due', table_name='publications', postgresql_where="status = 'scheduled'")
    op.drop_table('publications')
    op.drop_table('projects')
    op.drop_index('ix_audit_log_subject', table_name='audit_log')
    op.drop_index('ix_audit_log_at', table_name='audit_log')
    op.drop_table('audit_log')

    # Enum types are not dropped by table removal.
    sa.Enum(name='attempt_outcome').drop(op.get_bind(), checkfirst=True)
    sa.Enum(name='attempt_phase').drop(op.get_bind(), checkfirst=True)
    sa.Enum(name='publication_status').drop(op.get_bind(), checkfirst=True)
