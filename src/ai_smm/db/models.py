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
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    event,
    func,
    text,
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

from ai_smm.db.ideation_contracts import (
    MAX_IDEA_ANGLE_LENGTH,
    MAX_IDEA_DESCRIPTION_LENGTH,
    MAX_IDEA_TITLE_LENGTH,
    MAX_IDEAS_PER_JOB,
    MAX_IDEATION_ERROR_LENGTH,
    MAX_IDEATION_PARAMS_BYTES,
    MAX_TOPIC_HINT_LENGTH,
    MIN_IDEAS_PER_JOB,
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


class GenerationJobStatus(str, enum.Enum):
    """Lifecycle of one agent-generation job.

        queued    -> claimed | cancelled
        claimed   -> succeeded | failed | queued (lease expired) | cancelled
        failed    -> queued (retried) | terminal (attempts exhausted)
        succeeded -> terminal
        cancelled -> terminal

    Unlike a Publication, a job has no irreversible external side effect:
    nothing is ever written to content_items/content_revisions until the
    very end of processing, in the same transaction that marks the job
    succeeded (ai_smm.generation_queue). A process that dies mid-job
    therefore never needs reconciliation -- there is deliberately no
    needs_review-equivalent status here; the lease simply expires and the
    job is retried from scratch.
    """

    QUEUED = "queued"
    CLAIMED = "claimed"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


#: Statuses a generation worker may never resume on its own.
GENERATION_TERMINAL_STATUSES = frozenset(
    {
        GenerationJobStatus.SUCCEEDED,
        GenerationJobStatus.CANCELLED,
    }
)


class TopicIdeationJobStatus(str, enum.Enum):
    """Lifecycle of one topic-ideation job (SMM-035B3A).

        queued    -> running | cancelled
        running   -> succeeded | failed | queued (lease expired or retry)
        succeeded, failed, cancelled -> terminal

    Its own queue, claimed only by public.topic_ideation_claim
    (ai_smm.db.ideation_contracts): a content generation worker never sees
    it. Like a generation job it has no external side effect, so an
    expired lease is simply retried.
    """

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ContentIdeaStatus(str, enum.Enum):
    """A suggested topic. Only `proposed` is ever written today; rejecting
    an idea or using it for a generation job is a later stage."""

    PROPOSED = "proposed"
    REJECTED = "rejected"
    USED = "used"


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

generation_job_status_enum = SAEnum(
    GenerationJobStatus,
    name="generation_job_status",
    values_callable=lambda enum_cls: [m.value for m in enum_cls],
)

topic_ideation_job_status_enum = SAEnum(
    TopicIdeationJobStatus,
    name="topic_ideation_job_status",
    values_callable=lambda enum_cls: [m.value for m in enum_cls],
)

content_idea_status_enum = SAEnum(
    ContentIdeaStatus,
    name="content_idea_status",
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
        # MATCH SIMPLE: a legacy row (social_account_id NULL) is not
        # checked; an account-bound row must name an account of its own
        # project.
        ForeignKeyConstraint(
            ["project_id", "social_account_id"],
            ["social_accounts.project_id", "social_accounts.id"],
            name="fk_publications_social_account",
            ondelete="RESTRICT",
        ),
        Index("ix_publications_social_account", "social_account_id"),
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

    #: The account this row is published as. NULL is a legacy row: the
    #: deployment token, and only for an allow-listed project
    #: (ai_smm.publishing.routing). Copied from the approved revision at
    #: materialisation and never written afterwards. The composite foreign
    #: key below keeps it inside the row's own project.
    social_account_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True)
    )

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
    #: The database role that wrote the row (session_user), set by a trigger
    #: whatever the writer sends (SMM-025D, FU-024C2-2). Inside a SECURITY
    #: DEFINER function it is the role that called it, e.g. ai_smm_auth.
    #: NULL only for rows written before the column existed.
    written_by: Mapped[str | None] = mapped_column(
        String(63), server_default=text("SESSION_USER")
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
    #: Deferred (SMM-025D, grants G2): smm-api's roles may not SELECT it, so
    #: nothing loads it unless asked (application.auth.authenticate does).
    password_hash: Mapped[str] = mapped_column(
        Text, nullable=False, deferred=True
    )
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


# --- social accounts ----------------------------------------------------
# Which external identity a project publishes as, and the sealed token
# that proves it. The worker reads both, through
# ai_smm.publishing.routing only; the HTTP API reads metadata and writes
# ciphertext it cannot open (ai_smm.security.credentials).

#: Platforms an account may be created for: the ones the worker can
#: deliver to. Widening this is a worker change, not a data change.
SOCIAL_PLATFORMS = ("threads",)

ACCOUNT_STATUS_ACTIVE = "active"
ACCOUNT_STATUS_DISABLED = "disabled"

CREDENTIAL_STATUS_ACTIVE = "active"
CREDENTIAL_STATUS_SUPERSEDED = "superseded"
CREDENTIAL_STATUS_REVOKED = "revoked"

VERIFICATION_UNVERIFIED = "unverified"
VERIFICATION_VERIFIED = "verified"

#: The columns of social_account_credentials that are not key material.
#: Everything the control plane reads; the API's database role can read
#: these and nothing else (deploy/control-center/db/ai_smm_cc.grants.sql).
CREDENTIAL_METADATA_COLUMNS = (
    "id",
    "social_account_id",
    "status",
    "scheme",
    "key_id",
    "fingerprint",
    "verification_status",
    "verified_at",
    "expires_at",
    "created_by_user_id",
    "created_at",
    "superseded_at",
    "revoked_at",
)


class SocialAccount(Base):
    """One account on one platform, belonging to exactly one project.

    external_account_id is the platform's own id of the account. It is
    set by the first successful connection test and never changed after:
    from then on every credential of this account must prove that same
    identity, at entry and again before each live publication.
    """

    __tablename__ = "social_accounts"
    __table_args__ = (
        CheckConstraint(
            "platform in ('threads')", name="ck_social_accounts_platform"
        ),
        CheckConstraint(
            "status in ('active', 'disabled')",
            name="ck_social_accounts_status",
        ),
        CheckConstraint(
            "status <> 'disabled' OR disabled_at IS NOT NULL",
            name="ck_social_accounts_disabled_at",
        ),
        CheckConstraint(
            "length(btrim(display_name)) > 0",
            name="ck_social_accounts_display_name",
        ),
        CheckConstraint("version >= 1", name="ck_social_accounts_version"),
        # Target of the composite foreign key from publications.
        UniqueConstraint(
            "project_id", "id", name="uq_social_accounts_project_id"
        ),
        # One Threads identity at most once per project.
        Index(
            "uq_social_accounts_project_external",
            "project_id",
            "platform",
            "external_account_id",
            unique=True,
            postgresql_where=("external_account_id IS NOT NULL"),
        ),
        Index("ix_social_accounts_project", "project_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[str] = mapped_column(
        String(120),
        ForeignKey(
            "projects.id",
            ondelete="RESTRICT",
            name="fk_social_accounts_project_id",
        ),
        nullable=False,
    )
    platform: Mapped[str] = mapped_column(String(40), nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=ACCOUNT_STATUS_ACTIVE
    )
    external_account_id: Mapped[str | None] = mapped_column(String(120))
    external_username: Mapped[str | None] = mapped_column(String(120))
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="1"
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "users.id",
            ondelete="RESTRICT",
            name="fk_social_accounts_created_by",
        ),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    disabled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    @property
    def is_active(self) -> bool:
        return self.status == ACCOUNT_STATUS_ACTIVE


class SocialAccountCredential(Base):
    """A sealed token of one account. At most one is active.

    Only an active credential holds key material: replacing or revoking
    one sets its ciphertext, nonce and ephemeral key to NULL in the same
    statement that changes its status, and a check constraint makes any
    other combination impossible. The rest of the row stays as history.
    """

    __tablename__ = "social_account_credentials"
    __table_args__ = (
        CheckConstraint(
            "status in ('active', 'superseded', 'revoked')",
            name="ck_social_account_credentials_status",
        ),
        CheckConstraint(
            "(status = 'active') = ("
            "ciphertext IS NOT NULL AND nonce IS NOT NULL"
            " AND ephemeral_public_key IS NOT NULL)",
            name="ck_social_account_credentials_material",
        ),
        CheckConstraint(
            "status <> 'revoked' OR revoked_at IS NOT NULL",
            name="ck_social_account_credentials_revoked_at",
        ),
        CheckConstraint(
            "status <> 'superseded' OR superseded_at IS NOT NULL",
            name="ck_social_account_credentials_superseded_at",
        ),
        CheckConstraint(
            "verification_status in ('unverified', 'verified')",
            name="ck_social_account_credentials_verification",
        ),
        CheckConstraint(
            "length(fingerprint) = 12",
            name="ck_social_account_credentials_fingerprint",
        ),
        Index(
            "uq_social_account_credentials_one_active",
            "social_account_id",
            unique=True,
            postgresql_where=("status = 'active'"),
        ),
        Index(
            "ix_social_account_credentials_account", "social_account_id"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    social_account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "social_accounts.id",
            ondelete="RESTRICT",
            name="fk_social_account_credentials_account",
        ),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    scheme: Mapped[str] = mapped_column(String(64), nullable=False)
    key_id: Mapped[str] = mapped_column(String(32), nullable=False)
    ephemeral_public_key: Mapped[bytes | None] = mapped_column(LargeBinary)
    nonce: Mapped[bytes | None] = mapped_column(LargeBinary)
    ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    fingerprint: Mapped[str] = mapped_column(String(12), nullable=False)
    verification_status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=VERIFICATION_UNVERIFIED
    )
    verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "users.id",
            ondelete="RESTRICT",
            name="fk_social_account_credentials_created_by",
        ),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    superseded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    def __repr__(self) -> str:
        # The default repr would include the ciphertext bytes.
        return (
            f"SocialAccountCredential(id={self.id}, "
            f"account={self.social_account_id}, status={self.status}, "
            f"fingerprint={self.fingerprint})"
        )



# --- editorial layer ----------------------------------------------------
# Content, its immutable revisions and the human decisions about them sit
# in front of the delivery layer. Nothing here is read by the worker: a
# revision reaches the queue only when an editor explicitly materialises
# an approved one into a Publication, which is then scheduled and
# delivered exactly as before.

#: Formats a revision may take: the same set publications.format allows,
#: so a materialised revision can never fail the delivery table's check.
CONTENT_FORMATS = ("text", "image", "carousel", "thread")


class ContentGenerationJob(Base):
    """One request to run the agent pipeline (Strategist/Copywriter/Editor)
    for a project and turn its result into draft content items.

    Claimed and processed the same way publications are (FOR UPDATE SKIP
    LOCKED, a lease, bounded retries -- ai_smm.generation_queue): any
    number of generation workers can run without double-claiming, and a
    crashed worker's claim simply expires and is retried.

    requested_by_user_id is who asked for it, not who wrote the resulting
    text: the content_items it produces are agent-authored
    (created_by_user_id NULL, source=copywriter), exactly like one the
    operator CLI imports. A generation job never approves or schedules
    anything; it only ever calls create_content_item.
    """

    __tablename__ = "content_generation_jobs"
    __table_args__ = (
        CheckConstraint(
            "attempt_count >= 0", name="ck_generation_jobs_attempt_count"
        ),
        CheckConstraint(
            "max_attempts BETWEEN 1 AND 10",
            name="ck_generation_jobs_max_attempts",
        ),
        # A claimed row always carries claim bookkeeping. One-directional
        # on purpose: cancelling a job that is already claimed (the owner,
        # through Control Center) moves status away from 'claimed' but
        # must not touch claimed_by/claimed_at/lease_expires_at -- the
        # guard trigger forbids exactly that write -- so those columns are
        # left as a harmless record of who last held the lease.
        CheckConstraint(
            "status <> 'claimed' OR (claimed_by IS NOT NULL "
            "AND claimed_at IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="ck_generation_jobs_claim_shape",
        ),
        CheckConstraint(
            "status <> 'succeeded' OR finished_at IS NOT NULL",
            name="ck_generation_jobs_succeeded_finished",
        ),
        Index(
            "ix_generation_jobs_due",
            "next_attempt_at",
            postgresql_where=(
                "status IN ('queued', 'claimed')"
            ),
        ),
        Index("ix_generation_jobs_project", "project_id"),
        Index("ix_generation_jobs_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[str] = mapped_column(
        String(120),
        ForeignKey(
            "projects.id",
            ondelete="RESTRICT",
            name="fk_generation_jobs_project_id",
        ),
        nullable=False,
    )
    requested_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "users.id",
            ondelete="RESTRICT",
            name="fk_generation_jobs_requested_by",
        ),
        nullable=False,
    )
    status: Mapped[GenerationJobStatus] = mapped_column(
        generation_job_status_enum,
        nullable=False,
        server_default=GenerationJobStatus.QUEUED.value,
    )
    #: {"series_size": int, "publishing_strategy": str | None,
    #:  "topic_hint": str | None} -- validated by the API, not here.
    params: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    #: {"content_item_ids": [...], "series_title": str, "revision_count":
    #:  int, "tokens": {...}, "cost_usd": float, "seconds": float}.
    result: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    last_error: Mapped[str | None] = mapped_column(Text)

    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="2"
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    claimed_by: Mapped[str | None] = mapped_column(String(120))
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    content_items: Mapped[list[ContentItem]] = relationship(
        back_populates="generation_job"
    )


class TopicIdeationJob(Base):
    """One request to suggest topics for a project (SMM-035B3A).

    Its own queue, separate from content_generation_jobs: claimed, leased,
    retried and completed only through public.topic_ideation_* (SECURITY
    DEFINER, ai_smm.db.ideation_contracts). The API may only insert a
    freshly queued row for an active project the bound user owns
    (guard_topic_ideation_jobs_insert); every other write by a guarded role
    is refused. What a run produces is content_ideas rows, never content.
    """

    __tablename__ = "topic_ideation_jobs"
    __table_args__ = (
        CheckConstraint(
            f"idea_count BETWEEN {MIN_IDEAS_PER_JOB} AND {MAX_IDEAS_PER_JOB}",
            name="ck_topic_ideation_jobs_idea_count",
        ),
        CheckConstraint(
            "topic_hint IS NULL OR char_length(topic_hint) BETWEEN 1 AND "
            f"{MAX_TOPIC_HINT_LENGTH}",
            name="ck_topic_ideation_jobs_topic_hint",
        ),
        CheckConstraint(
            "attempt_count >= 0", name="ck_topic_ideation_jobs_attempt_count"
        ),
        CheckConstraint(
            "max_attempts BETWEEN 1 AND 10",
            name="ck_topic_ideation_jobs_max_attempts",
        ),
        CheckConstraint(
            "status <> 'running' OR (claimed_by IS NOT NULL "
            "AND claimed_at IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="ck_topic_ideation_jobs_claim_shape",
        ),
        CheckConstraint(
            "status NOT IN ('succeeded', 'failed', 'cancelled') "
            "OR finished_at IS NOT NULL",
            name="ck_topic_ideation_jobs_finished",
        ),
        CheckConstraint(
            "last_error IS NULL OR char_length(last_error) <= "
            f"{MAX_IDEATION_ERROR_LENGTH}",
            name="ck_topic_ideation_jobs_last_error",
        ),
        CheckConstraint(
            "jsonb_typeof(result) = 'object'",
            name="ck_topic_ideation_jobs_result",
        ),
        CheckConstraint(
            "jsonb_typeof(params) = 'object' AND octet_length(params::text) "
            f"<= {MAX_IDEATION_PARAMS_BYTES}",
            name="ck_topic_ideation_jobs_params",
        ),
        # The target of content_ideas' (ideation_job_id, project_id) key:
        # an idea can only ever reference a job of its own project.
        UniqueConstraint(
            "id", "project_id", name="uq_topic_ideation_jobs_id_project"
        ),
        Index(
            "ix_topic_ideation_jobs_due",
            "next_attempt_at",
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
        Index("ix_topic_ideation_jobs_project", "project_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[str] = mapped_column(
        String(120),
        ForeignKey(
            "projects.id",
            ondelete="RESTRICT",
            name="fk_topic_ideation_jobs_project_id",
        ),
        nullable=False,
    )
    requested_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "users.id",
            ondelete="RESTRICT",
            name="fk_topic_ideation_jobs_requested_by",
        ),
        nullable=False,
    )
    status: Mapped[TopicIdeationJobStatus] = mapped_column(
        topic_ideation_job_status_enum,
        nullable=False,
        server_default=TopicIdeationJobStatus.QUEUED.value,
    )
    idea_count: Mapped[int] = mapped_column(Integer, nullable=False)
    topic_hint: Mapped[str | None] = mapped_column(Text)
    #: What the job runs on, copied by the API when it is queued and never
    #: changed after (SMM-035B3B1): the project's profile snapshot and its
    #: recent idea titles. {} on a job queued before that stage.
    params: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    #: The worker's run summary (usage, model) plus "idea_ids"; written
    #: only by topic_ideation_complete.
    result: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    last_error: Mapped[str | None] = mapped_column(Text)

    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="2"
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    claimed_by: Mapped[str | None] = mapped_column(String(120))
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )


class ContentIdea(Base):
    """A topic suggested by a topic-ideation job (SMM-035B3A).

    Written only by public.topic_ideation_complete, which takes the
    project and the job from the locked job row; the composite key to
    (topic_ideation_jobs.id, project_id) makes an idea of one project
    pointing at another project's job impossible in the schema itself.
    generation_job_id is reserved for the stage that turns an idea into a
    generation job; nothing sets it yet.
    """

    __tablename__ = "content_ideas"
    __table_args__ = (
        ForeignKeyConstraint(
            ["ideation_job_id", "project_id"],
            ["topic_ideation_jobs.id", "topic_ideation_jobs.project_id"],
            ondelete="RESTRICT",
            name="fk_content_ideas_ideation_job",
        ),
        UniqueConstraint(
            "ideation_job_id", "position", name="uq_content_ideas_job_position"
        ),
        CheckConstraint("position >= 1", name="ck_content_ideas_position"),
        CheckConstraint(
            "title ~ '[^[:space:]]' AND char_length(title) <= "
            f"{MAX_IDEA_TITLE_LENGTH}",
            name="ck_content_ideas_title",
        ),
        CheckConstraint(
            "description ~ '[^[:space:]]' AND char_length(description) <= "
            f"{MAX_IDEA_DESCRIPTION_LENGTH}",
            name="ck_content_ideas_description",
        ),
        CheckConstraint(
            "angle IS NULL OR (angle ~ '[^[:space:]]' AND char_length(angle) <= "
            f"{MAX_IDEA_ANGLE_LENGTH})",
            name="ck_content_ideas_angle",
        ),
        Index("ix_content_ideas_project", "project_id", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[str] = mapped_column(
        String(120),
        ForeignKey(
            "projects.id",
            ondelete="RESTRICT",
            name="fk_content_ideas_project_id",
        ),
        nullable=False,
    )
    ideation_job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    #: 1-based order within its job; unique per job.
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    angle: Mapped[str | None] = mapped_column(Text)
    status: Mapped[ContentIdeaStatus] = mapped_column(
        content_idea_status_enum,
        nullable=False,
        server_default=ContentIdeaStatus.PROPOSED.value,
    )
    generation_job_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "content_generation_jobs.id",
            ondelete="RESTRICT",
            name="fk_content_ideas_generation_job",
        ),
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


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
        Index("ix_content_items_generation_job", "generation_job_id"),
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
    #: Which generation job produced this item, if any. NULL for every
    #: item a human created directly or the importer brought in -- the
    #: same "optional, additive" shape content_series has on Publication.
    generation_job_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "content_generation_jobs.id",
            ondelete="SET NULL",
            name="fk_content_items_generation_job",
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
    generation_job: Mapped[ContentGenerationJob | None] = relationship(
        back_populates="content_items"
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
    #: Where this revision is to be published. Part of what an approval
    #: approves, and of content_hash when set: choosing another account is
    #: a new revision, which clears the approval. NULL is the legacy
    #: destination (allow-listed projects only).
    social_account_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "social_accounts.id",
            ondelete="RESTRICT",
            name="fk_content_revisions_social_account",
        ),
    )
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


# --- business-mutation guards (SMM-025D) -----------------------------------
# ai_smm.db.guards: who a guarded transaction acts for, and the step-up
# grants that an approval by a guarded role consumes. No guarded role may
# read or write either table directly.


class MutationContext(Base):
    """Transaction -> authenticated user, written only by guard_bind_session."""

    __tablename__ = "mutation_contexts"
    __table_args__ = (Index("ix_mutation_contexts_bound_at", "bound_at"),)

    #: pg_current_xact_id() of the bound transaction.
    txid: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "users.id", ondelete="CASCADE", name="fk_mutation_contexts_user"
        ),
        nullable=False,
    )
    session_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    bound_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ApprovalGrant(Base):
    """A step-up confirmation: this owner re-entered their password for this
    revision, in this session, shortly ago. Issued only by
    smm_auth.approval_grant_issue (smm-auth); consumed by the approval it
    authorises (guard_consume_approval_grant)."""

    __tablename__ = "approval_grants"
    __table_args__ = (
        ForeignKeyConstraint(
            ["content_item_id", "revision_id"],
            ["content_revisions.content_item_id", "content_revisions.id"],
            name="fk_approval_grants_revision",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "expires_at > created_at", name="ck_approval_grants_expiry"
        ),
        CheckConstraint(
            "(consumed_at IS NULL) = (approval_id IS NULL)",
            name="ck_approval_grants_consumed",
        ),
        CheckConstraint(
            "length(content_hash) = 64", name="ck_approval_grants_hash"
        ),
        UniqueConstraint("approval_id", name="uq_approval_grants_approval"),
        Index("ix_approval_grants_revision", "revision_id"),
        Index("ix_approval_grants_user", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "users.id", ondelete="RESTRICT", name="fk_approval_grants_user"
        ),
        nullable=False,
    )
    session_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    content_item_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    revision_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    social_account_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey(
            "social_accounts.id",
            ondelete="RESTRICT",
            name="fk_approval_grants_social_account",
        ),
    )
    #: The destination's bound Threads identity when the owner confirmed.
    external_account_id: Mapped[str | None] = mapped_column(String(120))
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: The approval that consumed it (no foreign key: the grant is consumed
    #: in the approval's own BEFORE INSERT trigger, before the row exists).
    approval_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)


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


# Account routing guard. A worker that predates social accounts publishes
# whatever it claims with the deployment token; if it ever claimed an
# account-bound row it would post as the wrong account. Claiming such a
# row therefore requires the claiming transaction to declare that it
# routes by account -- set_config('ai_smm.account_routing', 'on', true),
# which only ai_smm.queue.claim_due_publication does. An old worker's
# claim fails here, and nothing is published.
ACCOUNT_CLAIM_GUARD_FUNCTION = "publication_account_claim_guard"
ACCOUNT_ROUTING_SETTING = "ai_smm.account_routing"

ACCOUNT_CLAIM_GUARD_FUNCTION_DDL = f"""
CREATE OR REPLACE FUNCTION {ACCOUNT_CLAIM_GUARD_FUNCTION}() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.social_account_id IS NOT NULL
       AND current_setting('{ACCOUNT_ROUTING_SETTING}', true)
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

ACCOUNT_CLAIM_GUARD_TRIGGER_DDL = (
    "CREATE TRIGGER trg_publications_account_claim_guard "
    "BEFORE UPDATE OF status ON publications "
    "FOR EACH ROW WHEN (NEW.status = 'claimed' "
    "AND OLD.status IS DISTINCT FROM 'claimed') "
    f"EXECUTE FUNCTION {ACCOUNT_CLAIM_GUARD_FUNCTION}()"
)

event.listen(
    Base.metadata,
    "before_create",
    DDL(ACCOUNT_CLAIM_GUARD_FUNCTION_DDL.replace("%", "%%")).execute_if(
        dialect="postgresql"
    ),
)
event.listen(
    Publication.__table__,
    "after_create",
    DDL(ACCOUNT_CLAIM_GUARD_TRIGGER_DDL).execute_if(dialect="postgresql"),
)
event.listen(
    Base.metadata,
    "after_drop",
    DDL(
        f"DROP FUNCTION IF EXISTS {ACCOUNT_CLAIM_GUARD_FUNCTION}()"
    ).execute_if(dialect="postgresql"),
)


# Business-mutation guards (SMM-025D): the current version of
# ai_smm.db.guards, so that a schema built with create_all (the test suite)
# is guarded exactly like a migrated one. Everything after the last table
# exists -- %ROWTYPE declarations are resolved when a function is created
# -- in the migration's order: functions, PUBLIC's default EXECUTE taken
# away, then the triggers.
from ai_smm.db import guards as _guards  # noqa: E402  (after the models)
from ai_smm.db.generation_contracts import (  # noqa: E402
    GENERATION_CONTRACTS as _GENERATION_CONTRACTS,
)


for _statement in (
    [_function.ddl for _function in _guards.FUNCTIONS_V1]
    + _guards.revoke_public_statements(_guards.FUNCTIONS_V1)
    + [_trigger.ddl for _trigger in _guards.TRIGGERS_V1]
):
    event.listen(
        Base.metadata,
        "after_create",
        DDL(_statement.replace("%", "%%")).execute_if(dialect="postgresql"),
    )

for _statement in _guards.drop_function_statements(_guards.FUNCTIONS_V1):
    event.listen(
        Base.metadata,
        "before_drop",
        DDL(_statement + " CASCADE").execute_if(dialect="postgresql"),
    )


# Generation-job guards (SMM-026A/2.1): a separate, later tuple -- see
# ai_smm.db.guards.GENERATION_FUNCTIONS. guard_require_owner (FUNCTIONS_V1)
# already exists in the same after_create batch above, and SQLAlchemy
# fires before_create/after_create listeners for a given event in the
# order they were registered, so these run after it.
for _statement in (
    [_function.ddl for _function in _guards.GENERATION_FUNCTIONS]
    + _guards.revoke_public_statements(_guards.GENERATION_FUNCTIONS)
    + [_trigger.ddl for _trigger in _guards.GENERATION_TRIGGERS]
):
    event.listen(
        Base.metadata,
        "after_create",
        DDL(_statement.replace("%", "%%")).execute_if(dialect="postgresql"),
    )

for _statement in _guards.drop_function_statements(_guards.GENERATION_FUNCTIONS):
    event.listen(
        Base.metadata,
        "before_drop",
        DDL(_statement + " CASCADE").execute_if(dialect="postgresql"),
    )


# guard_audit_log_insert v2 (SMM-026A/2.1): CREATE OR REPLACE of the same
# function FUNCTIONS_V1 created above, so generation.* audit actions are
# accepted. Must run after the FUNCTIONS_V1 batch; no new trigger, no new
# drop statement (FUNCTIONS_V1's DROP FUNCTION ... CASCADE above already
# removes it by name).
for _function in _guards.AUDIT_FUNCTIONS_V2:
    event.listen(
        Base.metadata,
        "after_create",
        DDL(_function.ddl.replace("%", "%%")).execute_if(dialect="postgresql"),
    )

# Provenance guard v2 is installed only after content_generation_jobs and
# content_items.generation_job_id exist. It replaces the V1 bodies while
# retaining their already-created triggers.
for _function in _guards.CONTENT_ITEM_FUNCTIONS_V2:
    event.listen(
        Base.metadata,
        "after_create",
        DDL(_function.ddl.replace("%", "%%")).execute_if(dialect="postgresql"),
    )

# The generation worker receives EXECUTE on these narrow SECURITY DEFINER
# contracts, never privileges on their underlying tables.
for _statement in (
    [_function.ddl for _function in _GENERATION_CONTRACTS]
    + _guards.revoke_public_statements(_GENERATION_CONTRACTS)
):
    event.listen(
        Base.metadata,
        "after_create",
        DDL(_statement.replace("%", "%%")).execute_if(dialect="postgresql"),
    )

for _statement in _guards.drop_function_statements(_GENERATION_CONTRACTS):
    event.listen(
        Base.metadata,
        "before_drop",
        DDL(_statement).execute_if(dialect="postgresql"),
    )


# Topic ideation (SMM-035B3A): guards, then the worker contracts, after
# guard_require_owner (FUNCTIONS_V1) and every table exist.
from ai_smm.db.ideation_contracts import (  # noqa: E402
    TOPIC_IDEATION_CONTRACTS as _TOPIC_IDEATION_CONTRACTS,
)


for _statement in (
    [_function.ddl for _function in _guards.TOPIC_IDEATION_FUNCTIONS]
    + _guards.revoke_public_statements(_guards.TOPIC_IDEATION_FUNCTIONS)
    + [_trigger.ddl for _trigger in _guards.TOPIC_IDEATION_TRIGGERS]
    + [_function.ddl for _function in _TOPIC_IDEATION_CONTRACTS]
    + _guards.revoke_public_statements(_TOPIC_IDEATION_CONTRACTS)
):
    event.listen(
        Base.metadata,
        "after_create",
        DDL(_statement.replace("%", "%%")).execute_if(dialect="postgresql"),
    )

for _statement in _guards.drop_function_statements(_TOPIC_IDEATION_CONTRACTS):
    event.listen(
        Base.metadata,
        "before_drop",
        DDL(_statement).execute_if(dialect="postgresql"),
    )

for _statement in _guards.drop_function_statements(
    _guards.TOPIC_IDEATION_FUNCTIONS
):
    event.listen(
        Base.metadata,
        "before_drop",
        DDL(_statement + " CASCADE").execute_if(dialect="postgresql"),
    )


def credential_metadata_only():
    """Loader option: never SELECT ciphertext, nonce or ephemeral key."""

    from sqlalchemy.orm import load_only

    return load_only(
        *(
            getattr(SocialAccountCredential, name)
            for name in CREDENTIAL_METADATA_COLUMNS
        )
    )
