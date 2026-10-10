"""Request and response bodies.

A password arrives as a SecretStr so that an accidental repr, a log line
or a validation error cannot print it.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from ai_smm.db.models import (
    AttemptOutcome,
    AttemptPhase,
    MembershipRole,
    PublicationStatus,
    PublishingStrategy,
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


class MeResponse(BaseModel):
    user: UserOut
    projects: list[ProjectAccessOut]


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
    #: The deployment-wide publishing switch, reported so a panel can say
    #: why nothing is going out. The API cannot change it.
    dry_run: bool


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
