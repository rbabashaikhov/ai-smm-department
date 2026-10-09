"""Database schema: PostgreSQL is the single source of truth for the queue.

All timestamps are timezone-aware and stored in UTC. Scheduling input is
accepted in the operator timezone (AI_SMM_DISPLAY_TZ, Europe/Moscow) and
converted on the way in.
"""
from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
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
    knowledge_path: Mapped[str] = mapped_column(Text, nullable=False)
    default_platform: Mapped[str] = mapped_column(
        String(40), nullable=False, server_default="threads"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true"
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
