"""Request and response bodies.

A password arrives as a SecretStr so that an accidental repr, a log line
or a validation error cannot print it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from ai_smm.db.models import (
    ApprovalDecision,
    AttemptOutcome,
    AttemptPhase,
    ContentStatus,
    GenerationJobStatus,
    MembershipRole,
    PublicationStatus,
    PublishingStrategy,
    RevisionSource,
    SeriesStatus,
)


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=320)
    password: SecretStr = Field(min_length=1, max_length=1024)


class UserOut(BaseModel):
    """Safe projection of a user: no password hash, no session data."""

    id: uuid.UUID
    email: str
    display_name: str
    is_active: bool
    last_login_at: datetime | None
    created_at: datetime


class ProjectAccessOut(BaseModel):
    project_id: str
    display_name: str
    role: MembershipRole


class ControlPlaneOut(BaseModel):
    """Deployment-wide switches a client needs before it offers a control."""

    #: false: every business-state change is refused with 403
    #: MUTATIONS_DISABLED, whatever the caller's role.
    mutations_enabled: bool


class MeResponse(BaseModel):
    user: UserOut
    projects: list[ProjectAccessOut]
    control_plane: ControlPlaneOut


class LoginResponse(BaseModel):
    user: UserOut
    #: Returned so a client can make its first unsafe request without a
    #: second round trip. Only the digest is stored server side.
    csrf_token: str


class CsrfResponse(BaseModel):
    csrf_token: str
    header_name: str


class ProjectOut(BaseModel):
    id: str
    display_name: str
    description: str
    default_platform: str
    default_timezone: str
    default_language: str
    is_active: bool
    version: int
    created_at: datetime
    updated_at: datetime
    #: The caller's own role, so a client can hide what it cannot do.
    role: MembershipRole
    #: Account-less publications of this project may use the deployment
    #: account (operator allow-list). False for every project created
    #: through the Control Center.
    legacy_publishing: bool = False


class ProjectCreate(BaseModel):
    """A new project. The caller becomes its owner; nothing else can be
    named: no user, no role, no knowledge path, no platform."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=3, max_length=64)
    display_name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    default_timezone: str | None = Field(default=None, max_length=64)
    default_language: str | None = Field(default=None, max_length=16)
    topics: list[str] | None = Field(default=None, max_length=10)
    tone_of_voice: str | None = Field(default=None, max_length=4000)


class ProjectPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    default_timezone: str | None = Field(default=None, max_length=64)
    default_language: str | None = Field(default=None, max_length=16)

    def changes(self) -> dict[str, str]:
        return {
            name: getattr(self, name)
            for name in (
                "display_name",
                "description",
                "default_timezone",
                "default_language",
            )
            if getattr(self, name) is not None
        }

    @model_validator(mode="after")
    def _something(self) -> ProjectPatch:
        if not self.changes():
            raise ValueError("Nothing to change.")

        return self


# -- social accounts and credentials ------------------------------------
# Credentials are write-only. CredentialOut is metadata; no response model
# in this API has a field that can carry a token, a ciphertext, a nonce or
# key material (tests/test_api_social_accounts.py walks the schema).


class SocialAccountCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: Literal["threads"] = "threads"
    display_name: str = Field(min_length=1, max_length=120)


class SocialAccountPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    display_name: str = Field(min_length=1, max_length=120)


class SocialAccountStatusIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)


class CredentialIn(BaseModel):
    """A token as a person pastes it. SecretStr: never printed or echoed."""

    model_config = ConfigDict(extra="forbid")

    access_token: SecretStr = Field(min_length=1, max_length=4096)
    expires_at: datetime | None = None
    #: Test the token against the platform before storing it. Off only
    #: for an offline setup; an account is not publishable until a test
    #: has bound it to its platform identity.
    verify: bool = True


class CredentialOut(BaseModel):
    id: uuid.UUID
    social_account_id: uuid.UUID
    status: Literal["active", "superseded", "revoked"]
    #: 12 hex characters of SHA-256 of the token. Not a part of it.
    fingerprint: str
    key_id: str
    verification_status: Literal["unverified", "verified"]
    verified_at: datetime | None
    expires_at: datetime | None
    created_by_user_id: uuid.UUID | None
    created_at: datetime
    superseded_at: datetime | None
    revoked_at: datetime | None


class SocialAccountOut(BaseModel):
    id: uuid.UUID
    project_id: str
    platform: str
    display_name: str
    status: Literal["active", "disabled"]
    external_account_id: str | None
    external_username: str | None
    version: int
    created_at: datetime
    updated_at: datetime
    disabled_at: datetime | None
    #: The active credential, metadata only.
    credential: CredentialOut | None
    publishing_ready: bool
    not_ready_reason: str | None
    #: Rows of this account that the worker will try to publish.
    scheduled_publications: int


class SocialAccountDetailOut(SocialAccountOut):
    credentials: list[CredentialOut]


class ProjectSettingsOut(BaseModel):
    project_id: str
    content_config: dict[str, Any]
    publishing_config: dict[str, Any]
    brand_config: dict[str, Any]
    version: int
    updated_at: datetime | None


class ProjectSettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: The version the caller read. A stale number is refused with 409
    #: rather than merged. Use 0 when GET reported no settings yet.
    expected_version: int = Field(ge=0)
    content_config: dict[str, Any] | None = None
    publishing_config: dict[str, Any] | None = None
    brand_config: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _at_least_one_section(self) -> ProjectSettingsPatch:
        if (
            self.content_config is None
            and self.publishing_config is None
            and self.brand_config is None
        ):
            raise ValueError(
                "At least one of content_config, publishing_config or "
                "brand_config must be supplied."
            )

        return self

    def sections(self) -> dict[str, dict[str, Any]]:
        return {
            name: value
            for name, value in (
                ("content_config", self.content_config),
                ("publishing_config", self.publishing_config),
                ("brand_config", self.brand_config),
            )
            if value is not None
        }


class HealthResponse(BaseModel):
    status: str


# -- publishing core (read) ----------------------------------------------
# Projections of rows the worker owns. They are read-only by design:
# there is no request body anywhere in this API that can create or edit a
# publication, and none that can publish one.


class PageMeta(BaseModel):
    """Pagination envelope shared by every list endpoint."""

    total: int
    limit: int
    offset: int


class PublicationSummary(BaseModel):
    """A publication as it appears in a list: no body, no attempt history."""

    id: int
    project_id: str
    platform: str
    ordinal: int
    title: str
    format: str
    status: PublicationStatus
    scheduled_at: datetime | None
    published_at: datetime | None
    threads_post_id: str | None
    attempt_count: int
    human_reviewed: bool
    series_id: int | None
    series_position: int | None
    series_total: int | None
    image_count: int
    body_chars: int
    created_at: datetime
    updated_at: datetime
    #: The account this row is published as; null for a legacy row.
    social_account_id: uuid.UUID | None = None


class PublicationList(PageMeta):
    items: list[PublicationSummary]


class PublicationDetail(PublicationSummary):
    """One publication, with its text and the operational fields."""

    body: str
    images: list[dict[str, Any]]
    items: list[dict[str, Any]]
    editor_score: float | None
    last_error: str | None
    next_attempt_at: datetime | None
    claimed_by: str | None
    claimed_at: datetime | None
    lease_expires_at: datetime | None
    parent_publication_id: int | None
    previous_publication_id: int | None
    source_ref: str | None
    idempotency_key: str


class PublicationPreviewOut(BaseModel):
    """What the worker would send, and whether it could send it now.

    Reporting only. Nothing in this response has contacted Threads, and
    reading it publishes nothing: the worker is still the only process
    that creates a post.
    """

    publication: PublicationDetail
    content_valid: bool
    content_error: str | None
    human_reviewed: bool
    series_ready: bool
    series_reason: str | None
    blocking_parts: list[int]
    publishable_now: bool


class AttemptOut(BaseModel):
    id: int
    publication_id: int
    attempt_number: int
    worker_id: str
    phase: AttemptPhase
    outcome: AttemptOutcome | None
    started_at: datetime
    finished_at: datetime | None
    threads_creation_id: str | None
    threads_post_id: str | None
    http_status: int | None
    error_type: str | None
    error_message: str | None
    details: dict[str, Any]


class AttemptList(PageMeta):
    items: list[AttemptOut]


class SeriesSummaryOut(BaseModel):
    id: int
    project_id: str
    title: str
    description: str
    narrative_goal: str
    target_audience: str
    publishing_strategy: PublishingStrategy
    status: SeriesStatus
    enforce_order: bool
    planned_total: int | None
    parts_total: int
    parts_published: int
    created_at: datetime
    updated_at: datetime


class SeriesList(PageMeta):
    items: list[SeriesSummaryOut]


class SeriesDetailOut(SeriesSummaryOut):
    parts: list[PublicationSummary]
    #: Structural problems reported by ai_smm.series.validate_series_structure.
    issues: list[str]


class OperationsSummaryOut(BaseModel):
    project_id: str
    total: int
    by_status: dict[str, int]
    due_now: int
    needs_attention: int
    published_last_24h: int
    last_published_at: datetime | None
    next_scheduled_at: datetime | None
    series_total: int
    series_active: int
    #: AI_SMM_DRY_RUN of the API process. It does not gate any API
    #: command and is NOT the worker's mode: the worker is a separate
    #: process with its own configuration.
    api_dry_run: bool
    #: AI_SMM_API_MUTATIONS_ENABLED: false means this API is read-only.
    api_mutations_enabled: bool
    #: The worker's execution mode from a verifiable source, or "unknown".
    #: Never inferred from api_dry_run.
    worker_mode: Literal["live", "dry_run", "unknown"]
    #: Where worker_mode came from; null while it is "unknown".
    worker_mode_source: str | None


class AuditEntryOut(BaseModel):
    id: int
    at: datetime
    actor: str
    action: str
    subject: str | None
    details: dict[str, Any]


class AuditList(PageMeta):
    items: list[AuditEntryOut]


# -- publishing core (safe commands) -------------------------------------
# Three commands, none of which publishes: they move a row inside the
# queue, and the worker decides when anything is sent.


class ScheduleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Must carry a timezone offset. The CLI reads a naive time in the
    #: operator timezone as a convenience; an API that guessed one could
    #: move a publish by hours.
    scheduled_at: datetime
    note: str | None = Field(default=None, max_length=500)
    #: Clear the attempt counter, for a row that failed for a reason that
    #: has since been fixed.
    reset_attempts: bool = False

    @model_validator(mode="after")
    def _require_offset(self) -> ScheduleRequest:
        if self.scheduled_at.tzinfo is None:
            raise ValueError(
                "scheduled_at must include a timezone offset, "
                "e.g. 2026-10-12T09:00:00+03:00."
            )

        return self


class CancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str | None = Field(default=None, max_length=500)


# -- editorial layer -----------------------------------------------------
# A revision has no update body anywhere in this API: the only way to
# change content is to POST a new revision.

ContentFormat = Literal["text", "image", "carousel", "thread"]


class RevisionIn(BaseModel):
    """The content of a new revision."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=500)
    body: str = Field(min_length=1, max_length=20000)
    format: ContentFormat = "text"
    images: list[dict[str, Any]] = Field(default_factory=list, max_length=20)
    items: list[dict[str, Any]] = Field(default_factory=list, max_length=50)
    metadata: dict[str, Any] = Field(default_factory=dict)
    #: Provenance. A person posting through the panel is "human"; a value
    #: such as "copywriter" records that the text came from an agent. It
    #: grants nothing -- approval is a separate, human-only act.
    source: RevisionSource = RevisionSource.HUMAN
    source_ref: str | None = Field(default=None, max_length=500)
    editor_score: float | None = Field(default=None, ge=0, le=10)
    editor_notes: str = Field(default="", max_length=5000)
    #: Where this revision is to be published: an active account of the
    #: item's project. Approved together with the text; choosing another
    #: account is a new revision.
    social_account_id: uuid.UUID | None = None


class ContentItemCreate(BaseModel):
    """A new item and its first revision, in one request.

    The project comes from the URL. There is no project_id field, and an
    unknown field is refused.
    """

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=500)
    content_type: str = Field(default="post", min_length=1, max_length=40)
    revision: RevisionIn


class RevisionCreate(RevisionIn):
    #: The item version the author read. A stale number is a 409, so two
    #: editors cannot silently stack revisions on each other's work.
    expected_item_version: int = Field(ge=1)


class RevisionDecisionIn(BaseModel):
    """submit-review, approve and reject all name the exact revision."""

    model_config = ConfigDict(extra="forbid")

    revision_id: uuid.UUID
    expected_item_version: int | None = Field(default=None, ge=1)
    note: str = Field(default="", max_length=2000)


class MaterializeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Optional guard: the approved revision the caller expects to send.
    #: If the approval has moved on, the request is refused as stale.
    revision_id: uuid.UUID | None = None


class RevisionOut(BaseModel):
    id: uuid.UUID
    content_item_id: uuid.UUID
    revision_number: int
    title: str | None
    body: str
    format: str
    images: list[dict[str, Any]]
    items: list[dict[str, Any]]
    metadata: dict[str, Any]
    source: RevisionSource
    source_ref: str | None
    editor_score: float | None
    editor_notes: str
    content_hash: str
    created_by_user_id: uuid.UUID | None
    created_at: datetime
    social_account_id: uuid.UUID | None = None


class RevisionList(PageMeta):
    items: list[RevisionOut]


class ContentItemSummary(BaseModel):
    id: uuid.UUID
    project_id: str
    title: str
    content_type: str
    status: ContentStatus
    current_revision_id: uuid.UUID
    approved_revision_id: uuid.UUID | None
    version: int
    created_by_user_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None


class ContentItemList(PageMeta):
    items: list[ContentItemSummary]


class ContentItemDetail(ContentItemSummary):
    current_revision: RevisionOut
    #: True only when the current revision is the approved one. An older
    #: revision's approval never counts.
    approval_in_effect: bool


class ApprovalOut(BaseModel):
    id: uuid.UUID
    content_item_id: uuid.UUID
    revision_id: uuid.UUID
    decision: ApprovalDecision
    actor_user_id: uuid.UUID
    note: str
    created_at: datetime


class ApprovalList(PageMeta):
    items: list[ApprovalOut]


class RevisionCreated(BaseModel):
    item: ContentItemSummary
    revision: RevisionOut


class DecisionOut(BaseModel):
    item: ContentItemSummary
    approval: ApprovalOut


class MaterializeOut(BaseModel):
    """What the bridge did. It never rewrites, schedules or publishes."""

    #: created | unchanged. A Publication's content never changes after it
    #: is created, so there is no "updated".
    result: Literal["created", "unchanged"]
    platform: str
    revision_id: uuid.UUID
    publication: PublicationDetail


# -- generation jobs (SMM-026A/2.1) ---------------------------------------
# Agent → Draft. A job only ever produces draft content items (see
# ai_smm.generation_worker); approving, materialising, scheduling and
# publishing stay exactly the existing content/publications routes.


class GenerationJobCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    series_size: int = Field(default=3, ge=1, le=5)
    # The application validates strategies so HTTP callers get an actionable
    # error, rather than the generic (redacted) Pydantic validation message.
    publishing_strategy: str | None = Field(
        default="standalone_series",
        description="New jobs support only standalone_series; null uses it too.",
        json_schema_extra={"enum": ["standalone_series", None]},
    )
    topic_hint: str | None = Field(default=None, max_length=500)


class GenerationJobOut(BaseModel):
    id: uuid.UUID
    project_id: str
    requested_by_user_id: uuid.UUID
    status: GenerationJobStatus
    params: dict[str, Any]
    result: dict[str, Any]
    last_error: str | None
    attempt_count: int
    max_attempts: int
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None


class GenerationJobList(PageMeta):
    items: list[GenerationJobOut]
