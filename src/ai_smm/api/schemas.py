"""Request and response bodies.

A password arrives as a SecretStr so that an accidental repr, a log line
or a validation error cannot print it.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from ai_smm.db.models import MembershipRole


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
