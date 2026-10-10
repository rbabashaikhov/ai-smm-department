"""Database schema: PostgreSQL is the single source of truth for the queue.

All timestamps are timezone-aware and stored in UTC. Scheduling input is
accepted in the operator timezone (AI_SMM_DISPLAY_TZ, Europe/Moscow) and
converted on the way in.
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    DDL,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    event,
    func,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
)


#: Defaults for a project's editorial settings. The worker does not read
#: them; they tell the control plane how to present and plan content.
DEFAULT_PROJECT_TIMEZONE = "Europe/Moscow"
DEFAULT_PROJECT_LANGUAGE = "ru"


class Base(DeclarativeBase):
    type_annotation_map = {
        dict[str, Any]: JSONB,
        list[Any]: JSONB,
    }


class PublicationStatus(str, enum.Enum):
    """Lifecycle of one publication.

    Allowed transitions:

        draft      -> approved | cancelled
        approved   -> scheduled | cancelled
        scheduled  -> claimed | cancelled
        claimed    -> publishing | scheduled (lease released) | failed
        publishing -> published | needs_review
        failed     -> scheduled (manual reschedule) | cancelled
        needs_review -> published | scheduled | cancelled  (manual only)
        published  -> terminal

    A record in publishing or needs_review is never picked up automatically:
    the outcome of the external call is unknown, so only a human decides.
    """

    DRAFT = "draft"
    APPROVED = "approved"
    SCHEDULED = "scheduled"
    CLAIMED = "claimed"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED = "failed"
    NEEDS_REVIEW = "needs_review"
    CANCELLED = "cancelled"


#: Statuses a worker may never resume on its own.
TERMINAL_STATUSES = frozenset(
    {
        PublicationStatus.PUBLISHED,
        PublicationStatus.CANCELLED,
    }
)

#: Statuses that require a human decision before anything else happens.
MANUAL_ONLY_STATUSES = frozenset(
    {
        PublicationStatus.PUBLISHING,
        PublicationStatus.NEEDS_REVIEW,
    }
)


class PublishingStrategy(str, enum.Enum):
    """How the parts of a series reach the platform.

    standalone_series -- each part is its own top-level post. Readers who
        see only one part still get something complete.
    reply_thread -- the first part is a top-level post and every later part
        is a reply to the one before it, so the series reads as one thread.
        A reply needs the real Threads id of its parent, which only exists
        once the parent has actually been published.
    """

    STANDALONE_SERIES = "standalone_series"
    REPLY_THREAD = "reply_thread"


class SeriesStatus(str, enum.Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class MembershipRole(str, enum.Enum):
    """What a user may do inside one project.

    The values are ordered: every role contains the rights of the ones
    before it. The ordering lives in ai_smm.security.rbac so that a
    comparison is never written by hand at a call site.
    """

    VIEWER = "viewer"
    EDITOR = "editor"
    ADMIN = "admin"
    OWNER = "owner"


class ContentStatus(str, enum.Enum):
    """Editorial lifecycle of a content item.

        draft     -> in_review              (submit for review)
        in_review -> approved | rejected    (a human decides)
        any live  -> draft                  (a new revision is created)

    approved always refers to one exact revision: the item's
    approved_revision_id, which a check constraint pins to its current
    revision. Creating a revision therefore returns the item to draft and
    clears the approval -- an approval of revision N says nothing about
    revision N+1. archived is terminal and has no transition in this stage.
    """

    DRAFT = "draft"
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    ARCHIVED = "archived"


class RevisionSource(str, enum.Enum):
    """Who produced a revision. Provenance only: it grants nothing.

    An agent may write a draft revision. Only an authenticated human can
    approve one, and that is enforced by content_approvals.actor_user_id
    being a NOT NULL reference to users, not by this value.
    """

    HUMAN = "human"
    STRATEGIST = "strategist"
    COPYWRITER = "copywriter"
    EDITOR = "editor"
    IMPORT = "import"


class ApprovalDecision(str, enum.Enum):
    APPROVED = "approved"
    REJECTED = "rejected"


class AttemptOutcome(str, enum.Enum):
    SUCCESS = "success"
    ERROR = "error"
    TIMEOUT = "timeout"
    #: Threads may or may not have created the post. Never retry on this.
    UNKNOWN = "unknown"
    DRY_RUN = "dry_run"


class AttemptPhase(str, enum.Enum):
    PREFLIGHT = "preflight"
    CONTAINER = "container"
    PUBLISH = "publish"
    VERIFY = "verify"


publication_status_enum = SAEnum(
    PublicationStatus,
    name="publication_status",
    values_callable=lambda enum_cls: [m.value for m in enum_cls],
)

attempt_outcome_enum = SAEnum(
    AttemptOutcome,
    name="attempt_outcome",
    values_callable=lambda enum_cls: [m.value for m in enum_cls],
)

attempt_phase_enum = SAEnum(
    AttemptPhase,
    name="attempt_phase",
    values_callable=lambda enum_cls: [m.value for m in enum_cls],
)

membership_role_enum = SAEnum(
    MembershipRole,
    name="membership_role",
    values_callable=lambda enum_cls: [m.value for m in enum_cls],
)

content_status_enum = SAEnum(
    ContentStatus,
    name="content_status",
    values_callable=lambda enum_cls: [m.value for m in enum_cls],
)

revision_source_enum = SAEnum(
    RevisionSource,
    name="content_revision_source",
    values_callable=lambda enum_cls: [m.value for m in enum_cls],
)

approval_decision_enum = SAEnum(
    ApprovalDecision,
    name="content_approval_decision",
    values_callable=lambda enum_cls: [m.value for m in enum_cls],
)

publishing_strategy_enum = SAEnum(
    PublishingStrategy,
    name="publishing_strategy",
    values_callable=lambda enum_cls: [m.value for m in enum_cls],
)

series_status_enum = SAEnum(
    SeriesStatus,
    name="series_status",
    values_callable=lambda enum_cls: [m.value for m in enum_cls],
)


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=""
    )
    knowledge_path: Mapped[str] = mapped_column(Text, nullable=False)
    default_platform: Mapped[str] = mapped_column(
        String(40), nullable=False, server_default="threads"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true"
    )
    #: Operator defaults for new content; the worker reads neither.
    default_timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default=DEFAULT_PROJECT_TIMEZONE
    )
    default_language: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=DEFAULT_PROJECT_LANGUAGE
    )
    #: Optimistic concurrency for edits to the project record itself.
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="1"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    publications: Mapped[list[Publication]] = relationship(
        back_populates="project"
    )
    series: Mapped[list[ContentSeries]] = relationship(
        back_populates="project"
    )
    memberships: Mapped[list[ProjectMembership]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
    )
    settings: Mapped[ProjectSettings | None] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        uselist=False,
    )


class ContentSeries(Base):
    """A set of publications meant to be read as one narrative.

    A publication does not need a series: every link added here is
    nullable, so the single posts that existed before series were
    introduced keep working untouched.
    """

    __tablename__ = "content_series"
    __table_args__ = (
        Index("ix_content_series_project", "project_id"),
        Index("ix_content_series_status", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String(120),
        ForeignKey("projects.id", ondelete="RESTRICT"),
        nullable=False,
    )

    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=""
    )
    #: What the series as a whole is meant to achieve for the reader.
    narrative_goal: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=""
    )
    target_audience: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=""
    )

    publishing_strategy: Mapped[PublishingStrategy] = mapped_column(
        publishing_strategy_enum,
        nullable=False,
        server_default=PublishingStrategy.STANDALONE_SERIES.value,
    )
    status: Mapped[SeriesStatus] = mapped_column(
        series_status_enum,
        nullable=False,
        server_default=SeriesStatus.DRAFT.value,
    )

    #: Refuse to publish part N before part N-1 is live. Always enforced
    #: for reply_thread, where a reply is impossible without its parent.
    enforce_order: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true"
    )

    planned_total: Mapped[int | None] = mapped_column(Integer)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    project: Mapped[Project] = relationship(back_populates="series")
    publications: Mapped[list[Publication]] = relationship(
        back_populates="series",
        order_by="Publication.series_position",
    )

    @property
    def is_reply_thread(self) -> bool:
        return self.publishing_strategy is PublishingStrategy.REPLY_THREAD


class Publication(Base):
    __tablename__ = "publications"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "platform",
            "ordinal",
            name="uq_publications_project_platform_ordinal",
        ),
        UniqueConstraint(
            "idempotency_key", name="uq_publications_idempotency_key"
        ),
        UniqueConstraint(
            "threads_post_id", name="uq_publications_threads_post_id"
        ),
        CheckConstraint(
            "attempt_count >= 0", name="ck_publications_attempt_count"
        ),
        CheckConstraint(
            "format in ('text', 'image', 'carousel', 'thread')",
            name="ck_publications_format",
        ),
        # A published record must carry the external id that proves it.
        CheckConstraint(
            "status <> 'published'"
            " OR (threads_post_id IS NOT NULL AND published_at IS NOT NULL)",
            name="ck_publications_published_has_id",
        ),
        Index(
            "ix_publications_due",
            "scheduled_at",
            postgresql_where=(
                "status = 'scheduled'"
            ),
        ),
        Index("ix_publications_status", "status"),
        Index("ix_publications_project", "project_id"),
        # One publication per position within a series. Partial, so the
        # many publications with series_id NULL are unaffected.
        Index(
            "uq_publications_series_position",
            "series_id",
            "series_position",
            unique=True,
            postgresql_where=("series_id IS NOT NULL"),
        ),
        Index("ix_publications_series", "series_id"),
        # Membership is all-or-nothing: a position without a series, or a
        # series without a position, would make ordering undecidable.
        CheckConstraint(
            "(series_id IS NULL AND series_position IS NULL)"
            " OR (series_id IS NOT NULL AND series_position IS NOT NULL"
            "     AND series_position >= 1)",
            name="ck_publications_series_membership",
        ),
        CheckConstraint(
            "series_total IS NULL OR series_total >= 1",
            name="ck_publications_series_total",
        ),
        # A reply to itself would be a cycle of length one.
        CheckConstraint(
            "parent_publication_id IS NULL OR parent_publication_id <> id",
            name="ck_publications_parent_not_self",
        ),
        CheckConstraint(
            "previous_publication_id IS NULL"
            " OR previous_publication_id <> id",
            name="ck_publications_previous_not_self",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    project_id: Mapped[str] = mapped_column(
        String(120),
        ForeignKey("projects.id", ondelete="RESTRICT"),
        nullable=False,
    )
    platform: Mapped[str] = mapped_column(
        String(40), nullable=False, server_default="threads"
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)

    title: Mapped[str] = mapped_column(Text, nullable=False)
    format: Mapped[str] = mapped_column(String(40), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    #: [{"path": "...", "alt_text": "..."}] relative to the project root.
    images: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
    #: Thread items, when format == 'thread'.
    items: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )

    status: Mapped[PublicationStatus] = mapped_column(
        publication_status_enum,
        nullable=False,
        server_default=PublicationStatus.DRAFT.value,
    )

    scheduled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    #: Stable across reruns of the importer; the first dedup barrier.
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    #: The second dedup barrier: unique, and refuses a re-publish when set.
    threads_post_id: Mapped[str | None] = mapped_column(String(120))
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    claimed_by: Mapped[str | None] = mapped_column(String(120))
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    last_error: Mapped[str | None] = mapped_column(Text)

    editor_score: Mapped[float | None] = mapped_column(Numeric(3, 1))
    human_reviewed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )

    source_ref: Mapped[str | None] = mapped_column(Text)

    # --- series membership (all optional) --------------------------------
    # A publication that belongs to no series behaves exactly as before.
    series_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            "content_series.id",
            ondelete="SET NULL",
            name="fk_publications_series_id",
        ),
    )
    #: 1-based position within the series.
    series_position: Mapped[int | None] = mapped_column(Integer)
    #: How many parts the series was planned to have, denormalised so a
    #: part can say "2/3" without loading the series.
    series_total: Mapped[int | None] = mapped_column(Integer)
    #: reply_thread only: the publication this one replies to. Its
    #: threads_post_id is what the Threads API needs as reply_to_id, and
    #: it exists only once that publication is actually published.
    parent_publication_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            "publications.id",
            ondelete="SET NULL",
            name="fk_publications_parent_publication_id",
        ),
    )
    #: The part before this one, in either strategy. For reply_thread it
    #: is normally the same row as the parent; it is kept separate because
    #: narrative order and reply structure need not coincide.
    previous_publication_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            "publications.id",
            ondelete="SET NULL",
            name="fk_publications_previous_publication_id",
        ),
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    project: Mapped[Project] = relationship(back_populates="publications")
    series: Mapped[ContentSeries | None] = relationship(
        back_populates="publications",
        foreign_keys=[series_id],
    )
    parent_publication: Mapped[Publication | None] = relationship(
        remote_side="Publication.id",
        foreign_keys=[parent_publication_id],
    )
    previous_publication: Mapped[Publication | None] = relationship(
        remote_side="Publication.id",
        foreign_keys=[previous_publication_id],
    )
    attempts: Mapped[list[PublicationAttempt]] = relationship(
        back_populates="publication",
        order_by="PublicationAttempt.attempt_number",
    )


class PublicationAttempt(Base):
    """One attempt to publish. Written before the external call, not after.

    phase plus threads_creation_id is what makes an ambiguous outcome
    reconcilable: if the process dies between the container and the publish
    call, the row says exactly how far it got.
    """

    __tablename__ = "publication_attempts"
    __table_args__ = (
        UniqueConstraint(
            "publication_id",
            "attempt_number",
            name="uq_attempts_publication_attempt_number",
        ),
        Index("ix_attempts_publication", "publication_id"),
        Index("ix_attempts_outcome", "outcome"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    publication_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("publications.id", ondelete="CASCADE"),
        nullable=False,
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_id: Mapped[str] = mapped_column(String(120), nullable=False)
    phase: Mapped[AttemptPhase] = mapped_column(
        attempt_phase_enum, nullable=False
    )

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    outcome: Mapped[AttemptOutcome | None] = mapped_column(
        attempt_outcome_enum
    )
    threads_creation_id: Mapped[str | None] = mapped_column(String(120))
    threads_post_id: Mapped[str | None] = mapped_column(String(120))
    http_status: Mapped[int | None] = mapped_column(Integer)
    error_type: Mapped[str | None] = mapped_column(String(120))
    error_message: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )

    publication: Mapped[Publication] = relationship(
        back_populates="attempts"
    )


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_log_at", "at"),
        Index("ix_audit_log_subject", "subject"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    #: worker:<id> | cli:<user> | scheduler | importer
    actor: Mapped[str] = mapped_column(String(160), nullable=False)
    action: Mapped[str] = mapped_column(String(120), nullable=False)
    subject: Mapped[str | None] = mapped_column(String(160))
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )


# --- control plane ------------------------------------------------------
# Identity, sessions and per-project settings exist for the HTTP API only.
# The worker reads none of these tables, so its behaviour is unchanged by
# their presence.


class User(Base):
    """An operator who can sign in to the API.

    There is no public registration: a user is created by an operator
    command (`ai-smm user create-owner`). The email is stored already
    normalised (trimmed, lowercased) and the uniqueness constraint is on
    that normalised form, so no CITEXT extension is required.
    """

    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("email", name="uq_users_email"),
        CheckConstraint("email = lower(email)", name="ck_users_email_lower"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    #: Argon2id, produced by ai_smm.security.passwords. Never a raw password.
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true"
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    sessions: Mapped[list[UserSession]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )
    memberships: Mapped[list[ProjectMembership]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )


class UserSession(Base):
    """One server-side session.

    The raw session token exists only in the client cookie: this table
    stores its SHA-256 hash, so a dump of the database cannot be replayed
    as a login. The CSRF token is not stored at all -- it is an HMAC of
    the session token, recomputed from the cookie on each request, which
    is what keeps it identical across a session's browser tabs.

    Two independent deadlines apply. expires_at is the absolute lifetime,
    fixed at login. Idleness is derived from last_seen_at by the session
    service, so the window can be changed without a migration.
    """

    __tablename__ = "user_sessions"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_user_sessions_token_hash"),
        Index("ix_user_sessions_user", "user_id"),
        Index("ix_user_sessions_expires_at", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: SHA-256 hex digest of the session token.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    user: Mapped[User] = relationship(back_populates="sessions")


class ProjectMembership(Base):
    """What one user may do in one project.

    Authorisation is always derived from a row here, never from anything
    the client sends: the project comes from the URL and is loaded from
    the database, then this table decides.
    """

    __tablename__ = "project_memberships"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "user_id", name="uq_project_memberships_pair"
        ),
        Index("ix_project_memberships_user", "user_id"),
        Index("ix_project_memberships_project", "project_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[str] = mapped_column(
        String(120),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[MembershipRole] = mapped_column(
        membership_role_enum, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    project: Mapped[Project] = relationship(back_populates="memberships")
    user: Mapped[User] = relationship(back_populates="memberships")


class ProjectSettings(Base):
    """Editorial configuration of one project, edited through the API.

    The row is optional: a project that predates this table has no row and
    the API answers with the documented defaults. The first PATCH creates
    it. version is the optimistic concurrency token -- a PATCH states the
    version it read, and a stale number is refused rather than merged.
    """

    __tablename__ = "project_settings"

    project_id: Mapped[str] = mapped_column(
        String(120),
        ForeignKey("projects.id", ondelete="CASCADE"),
        primary_key=True,
    )
    content_config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    publishing_config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    brand_config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="1"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    project: Mapped[Project] = relationship(back_populates="settings")



# --- editorial layer ----------------------------------------------------
# Content, its immutable revisions and the human decisions about them sit
# in front of the delivery layer. Nothing here is read by the worker: a
# revision reaches the queue only when an editor explicitly materialises
# an approved one into a Publication, which is then scheduled and
# delivered exactly as before.

#: Formats a revision may take: the same set publications.format allows,
#: so a materialised revision can never fail the delivery table's check.
CONTENT_FORMATS = ("text", "image", "carousel", "thread")


class ContentItem(Base):
    """One piece of content and where it stands editorially.

    It holds no editable text. The words live in content_revisions, one
    immutable row per version; the item only points at the current one
    and, while an approval is in effect, at the approved one.

    Integrity that the database enforces rather than the code:

    * current_revision_id and approved_revision_id are composite foreign
      keys onto (content_item_id, id) of content_revisions, so either can
      only ever name a revision of *this* item;
    * an item is approved exactly when approved_revision_id is set, and
      that revision must be the current one -- so an approval of an older
      revision cannot be in effect, whatever the application does;
    * every item has a current revision. The foreign key is deferred to
      commit, because an item and its first revision are written in the
      same transaction and each refers to the other.
    """

    __tablename__ = "content_items"
    __table_args__ = (
        ForeignKeyConstraint(
            ["id", "current_revision_id"],
            ["content_revisions.content_item_id", "content_revisions.id"],
            name="fk_content_items_current_revision",
            ondelete="RESTRICT",
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["id", "approved_revision_id"],
            ["content_revisions.content_item_id", "content_revisions.id"],
            name="fk_content_items_approved_revision",
            ondelete="RESTRICT",
            use_alter=True,
        ),
        CheckConstraint(
            "(status = 'approved') = (approved_revision_id IS NOT NULL)",
            name="ck_content_items_approved_has_revision",
        ),
        CheckConstraint(
            "approved_revision_id IS NULL"
            " OR approved_revision_id = current_revision_id",
            name="ck_content_items_approval_is_current",
        ),
        CheckConstraint(
            "status <> 'archived' OR archived_at IS NOT NULL",
            name="ck_content_items_archived_at",
        ),
        CheckConstraint(
            "length(content_type) > 0", name="ck_content_items_content_type"
        ),
        CheckConstraint("version >= 1", name="ck_content_items_version"),
        Index("ix_content_items_project", "project_id"),
        Index("ix_content_items_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[str] = mapped_column(
        String(120),
        ForeignKey(
            "projects.id",
            ondelete="RESTRICT",
            name="fk_content_items_project_id",
        ),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str] = mapped_column(
        String(40), nullable=False, server_default="post"
    )
    status: Mapped[ContentStatus] = mapped_column(
        content_status_enum,
        nullable=False,
        server_default=ContentStatus.DRAFT.value,
    )
    current_revision_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    #: Set only while an approval is in effect, and then always equal to
    #: current_revision_id. History lives in content_approvals.
    approved_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True)
    )
    #: Optimistic concurrency token, bumped by every editorial change.
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="1"
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "users.id",
            ondelete="RESTRICT",
            name="fk_content_items_created_by",
        ),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    revisions: Mapped[list[ContentRevision]] = relationship(
        back_populates="content_item",
        foreign_keys="ContentRevision.content_item_id",
        order_by="ContentRevision.revision_number",
        viewonly=True,
    )


class ContentRevision(Base):
    """An immutable snapshot of one version of a content item.

    Rows are never updated and never deleted: a trigger rejects both, so
    the guarantee holds for every client of the database and not only
    for this application. Any change to the content is a new row.

    content_hash is the SHA-256 of the deliverable snapshot -- title,
    body, format, images, items -- so two revisions with the same hash
    would publish the same post.
    """

    __tablename__ = "content_revisions"
    __table_args__ = (
        UniqueConstraint(
            "content_item_id",
            "revision_number",
            name="uq_content_revisions_item_number",
        ),
        # The target of the composite foreign keys that tie a current,
        # approved or decided revision to the item it belongs to.
        UniqueConstraint(
            "content_item_id", "id", name="uq_content_revisions_item_id"
        ),
        CheckConstraint(
            "revision_number >= 1", name="ck_content_revisions_number"
        ),
        CheckConstraint(
            "format in ('text', 'image', 'carousel', 'thread')",
            name="ck_content_revisions_format",
        ),
        CheckConstraint(
            "length(content_hash) = 64", name="ck_content_revisions_hash"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    content_item_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "content_items.id",
            ondelete="RESTRICT",
            name="fk_content_revisions_item",
        ),
        nullable=False,
    )
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    format: Mapped[str] = mapped_column(String(40), nullable=False)
    images: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
    items: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
    #: Stored in a column named "metadata"; the attribute is renamed
    #: because Declarative reserves that name for the table metadata.
    meta: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default="{}"
    )
    source: Mapped[RevisionSource] = mapped_column(
        revision_source_enum, nullable=False
    )
    source_ref: Mapped[str | None] = mapped_column(Text)
    editor_score: Mapped[float | None] = mapped_column(Numeric(3, 1))
    editor_notes: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=""
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    #: NULL for a revision an agent produced; a human author otherwise.
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "users.id",
            ondelete="RESTRICT",
            name="fk_content_revisions_created_by",
        ),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    content_item: Mapped[ContentItem] = relationship(
        back_populates="revisions",
        foreign_keys=[content_item_id],
    )


class ContentApproval(Base):
    """One human decision about one exact revision. Append-only.

    actor_user_id is a NOT NULL reference to users: an approval without an
    authenticated human behind it cannot be stored. The composite foreign
    key makes it impossible to record a decision about a revision that
    belongs to a different item. A trigger rejects UPDATE and DELETE, so
    the history cannot be rewritten after the fact.
    """

    __tablename__ = "content_approvals"
    __table_args__ = (
        ForeignKeyConstraint(
            ["content_item_id", "revision_id"],
            ["content_revisions.content_item_id", "content_revisions.id"],
            name="fk_content_approvals_revision",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_content_approvals_item_created",
            "content_item_id",
            "created_at",
        ),
        Index("ix_content_approvals_revision", "revision_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    content_item_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    revision_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    decision: Mapped[ApprovalDecision] = mapped_column(
        approval_decision_enum, nullable=False
    )
    actor_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "users.id",
            ondelete="RESTRICT",
            name="fk_content_approvals_actor",
        ),
        nullable=False,
    )
    note: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ContentPublicationLink(Base):
    """Which revision a delivery Publication's current snapshot came from.

    This table, not publications.source_ref or idempotency_key, is the
    authoritative mapping from the editorial layer to the delivery layer.
    Those two columns are kept as a human-readable trace and as a second
    barrier against duplicates; nothing reads them to decide anything.

    Semantics: one row per Publication, describing its *current*
    snapshot.

    * (content_item_id, revision_id, platform) is the primary key, so one
      revision is delivered at most once per platform;
    * publication_id is UNIQUE, so a Publication is never attributed to
      two revisions at once;
    * the composite foreign key onto content_revisions means the revision
      always belongs to the item named on the same row;
    * a trigger checks that the Publication belongs to the item's project
      and is on the row's platform -- a constraint no foreign key can
      express without altering publications, which is left untouched.

    Rows are written once, together with the Publication they describe,
    and never repointed: a Publication is the immutable delivery snapshot
    of one revision, so its link can never come to mean a different one.
    A newer revision gets a new Publication and a new row, and only after
    the earlier Publication has been cancelled; that cancelled Publication
    keeps its row as the historical lineage of the revision it carried.
    """

    __tablename__ = "content_publication_links"
    __table_args__ = (
        ForeignKeyConstraint(
            ["content_item_id", "revision_id"],
            ["content_revisions.content_item_id", "content_revisions.id"],
            name="fk_content_publication_links_revision",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "publication_id", name="uq_content_publication_links_publication"
        ),
        CheckConstraint(
            "length(platform) > 0",
            name="ck_content_publication_links_platform",
        ),
    )

    content_item_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "content_items.id",
            ondelete="RESTRICT",
            name="fk_content_publication_links_item",
        ),
        primary_key=True,
    )
    revision_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True
    )
    platform: Mapped[str] = mapped_column(String(40), primary_key=True)
    publication_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "publications.id",
            ondelete="RESTRICT",
            name="fk_content_publication_links_publication",
        ),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# Append-only enforcement. The same function and triggers are created by
# migration 3d4ac1986f07; these listeners give a schema built with
# Base.metadata.create_all (the test suite) the identical guarantee, so
# no test can pass against a weaker database than production runs.
APPEND_ONLY_FUNCTION = "content_reject_mutation"

_APPEND_ONLY_FUNCTION_DDL = f"""
CREATE OR REPLACE FUNCTION {APPEND_ONLY_FUNCTION}() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only: % is not allowed',
        TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'restrict_violation';
END
$$
"""


def _append_only_trigger_ddl(table: str) -> str:
    return (
        f"CREATE TRIGGER trg_{table}_append_only "
        f"BEFORE UPDATE OR DELETE ON {table} "
        f"FOR EACH ROW EXECUTE FUNCTION {APPEND_ONLY_FUNCTION}()"
    )


event.listen(
    Base.metadata,
    "before_create",
    # DDL() interpolates its statement with %, so the placeholders that
    # belong to plpgsql's RAISE have to be escaped for it.
    DDL(_APPEND_ONLY_FUNCTION_DDL.replace("%", "%%")).execute_if(
        dialect="postgresql"
    ),
)
event.listen(
    ContentRevision.__table__,
    "after_create",
    DDL(_append_only_trigger_ddl("content_revisions")).execute_if(
        dialect="postgresql"
    ),
)
event.listen(
    ContentApproval.__table__,
    "after_create",
    DDL(_append_only_trigger_ddl("content_approvals")).execute_if(
        dialect="postgresql"
    ),
)
event.listen(
    Base.metadata,
    "after_drop",
    DDL(
        f"DROP FUNCTION IF EXISTS {APPEND_ONLY_FUNCTION}()"
    ).execute_if(dialect="postgresql"),
)


# Link consistency: the Publication must be in the content item's project
# and on the link's platform. A foreign key cannot say this without a
# composite unique key on publications, and publications is not altered.
LINK_CHECK_FUNCTION = "content_publication_link_check"

LINK_CHECK_FUNCTION_DDL = f"""
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

LINK_CHECK_TRIGGER_DDL = (
    "CREATE TRIGGER trg_content_publication_links_consistent "
    "BEFORE INSERT OR UPDATE ON content_publication_links "
    f"FOR EACH ROW EXECUTE FUNCTION {LINK_CHECK_FUNCTION}()"
)

event.listen(
    Base.metadata,
    "before_create",
    DDL(LINK_CHECK_FUNCTION_DDL.replace("%", "%%")).execute_if(
        dialect="postgresql"
    ),
)
event.listen(
    ContentPublicationLink.__table__,
    "after_create",
    DDL(LINK_CHECK_TRIGGER_DDL).execute_if(dialect="postgresql"),
)
event.listen(
    Base.metadata,
    "after_drop",
    DDL(f"DROP FUNCTION IF EXISTS {LINK_CHECK_FUNCTION}()").execute_if(
        dialect="postgresql"
    ),
)
