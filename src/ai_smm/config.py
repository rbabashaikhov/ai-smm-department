"""Single place where runtime configuration is read from the environment.

Importing this module never touches the network and never raises on missing
secrets: validation happens when a component actually needs a value, so the
CLI stays usable for read-only commands (queue listing, error review) on a
machine that has no Threads or OpenAI credentials.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Scheduling is entered and displayed in this zone; storage is always UTC.
DEFAULT_DISPLAY_TZ = "Europe/Moscow"


def _env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)

    if value is None:
        return default

    value = value.strip()

    return value or default


def _env_int(name: str, default: int) -> int:
    raw = _env(name)

    if raw is None:
        return default

    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(
            f"{name} must be an integer, got {raw!r}."
        ) from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)

    if raw is None:
        return default

    normalized = raw.lower()

    if normalized in {"1", "true", "yes", "on"}:
        return True

    if normalized in {"0", "false", "no", "off"}:
        return False

    raise ValueError(
        f"{name} must be a boolean, got {raw!r}."
    )


@dataclass(frozen=True)
class Settings:
    # Database
    database_url: str | None

    # Runtime identity and behaviour
    worker_id: str
    dry_run: bool

    # Scheduling
    poll_interval_seconds: int
    display_tz_name: str
    lease_seconds: int
    max_attempts: int
    min_publish_interval_seconds: int
    daily_publish_limit: int
    batch_size: int
    misfire_grace_seconds: int

    # Knowledge base
    knowledge_root: Path
    #: Base for the relative image paths stored in publications.images.
    #: Those paths are repo-relative ("knowledge/projects/<id>/assets/x.jpg"),
    #: so in the container this is the parent of the knowledge mount, not
    #: /app, where the source tree happens to live.
    media_root: Path

    # Logging
    log_level: str
    log_format: str

    # Health
    health_stale_factor: int

    # Secret names that must never reach a log record.
    secret_env_names: tuple[str, ...] = field(
        default=(
            "OPENAI_API_KEY",
            "THREADS_ACCESS_TOKEN",
            "LANGFUSE_SECRET_KEY",
            "LANGFUSE_PUBLIC_KEY",
            "STORAGE_SSH_PASSWORD",
            "AI_SMM_DATABASE_URL",
        ),
    )

    @property
    def display_tz(self) -> ZoneInfo:
        return ZoneInfo(self.display_tz_name)

    def require_database_url(self) -> str:
        if not self.database_url:
            raise RuntimeError(
                "AI_SMM_DATABASE_URL is not configured."
            )

        return self.database_url


def load_settings() -> Settings:
    knowledge_root = Path(
        _env("AI_SMM_KNOWLEDGE_ROOT")
        or str(PROJECT_ROOT / "knowledge")
    ).expanduser()

    return Settings(
        database_url=_env("AI_SMM_DATABASE_URL"),
        worker_id=_env("AI_SMM_WORKER_ID", "worker-1") or "worker-1",
        dry_run=_env_bool("AI_SMM_DRY_RUN", True),
        poll_interval_seconds=_env_int(
            "AI_SMM_POLL_INTERVAL_SECONDS", 60
        ),
        display_tz_name=(
            _env("AI_SMM_DISPLAY_TZ", DEFAULT_DISPLAY_TZ)
            or DEFAULT_DISPLAY_TZ
        ),
        lease_seconds=_env_int("AI_SMM_LEASE_SECONDS", 900),
        max_attempts=_env_int("AI_SMM_MAX_ATTEMPTS", 5),
        min_publish_interval_seconds=_env_int(
            "AI_SMM_MIN_PUBLISH_INTERVAL_SECONDS", 300
        ),
        daily_publish_limit=_env_int(
            "AI_SMM_DAILY_PUBLISH_LIMIT", 20
        ),
        batch_size=_env_int("AI_SMM_BATCH_SIZE", 1),
        misfire_grace_seconds=_env_int(
            "AI_SMM_MISFIRE_GRACE_SECONDS", 3600
        ),
        knowledge_root=knowledge_root,
        media_root=Path(
            _env("AI_SMM_MEDIA_ROOT") or str(PROJECT_ROOT)
        ).expanduser(),
        log_level=(_env("LOG_LEVEL", "INFO") or "INFO").upper(),
        log_format=(_env("LOG_FORMAT", "json") or "json").lower(),
        health_stale_factor=_env_int("AI_SMM_HEALTH_STALE_FACTOR", 3),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
