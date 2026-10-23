"""Single place where runtime configuration is read from the environment.

Importing this module never touches the network and never raises on missing
secrets: validation happens when a component actually needs a value, so the
CLI stays usable for read-only commands (queue listing, error review) on a
machine that has no Threads or OpenAI credentials.
"""
from __future__ import annotations

import ipaddress
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

    # HTTP API (control plane). The API never publishes: it reads and
    # writes the queue's metadata, and the worker remains the only
    # process that talks to Threads.
    api_session_lifetime_seconds: int
    api_session_idle_seconds: int
    api_cookie_secure: bool
    #: /docs, /redoc and /openapi.json. On for local development; a
    #: packaged private runtime turns them off so the schema is not served
    #: to anyone who can reach the origin.
    api_docs_enabled: bool = True
    #: Whether the API may change business state at all. Off by default:
    #: a Control Center deployed next to a live worker is read-only until
    #: an operator turns this on explicitly. Login and logout keep working
    #: either way (see ai_smm.api.mutation_gate). This switch belongs to
    #: the API process only -- the worker never reads it, so it can
    #: neither stop nor start publishing.
    api_mutations_enabled: bool = False
    #: Whether smm-api itself serves POST /auth/login and /auth/logout.
    #: On for local development and the test suite; the packaged Control
    #: Center turns it off: smm-auth signs users in (ADR-024C-3A, grants G2
    #: take session-creating rights from the API's roles).
    api_auth_routes_enabled: bool = True
    #: smm-auth only: the networks a request may come from (CIDR). Empty
    #: -- local development and tests -- accepts any peer; the packaged
    #: service names its own cc-auth subnet, so that even a wider bind
    #: would not serve the shared network (ADR-024C-3A §9.2).
    auth_allowed_peers: tuple[str, ...] = ()

    # Social accounts (SMM-025B). See ai_smm.publishing.routing.
    #: Projects whose account-less (legacy) publications may still be
    #: published with the deployment THREADS_ACCESS_TOKEN. Empty -- the
    #: default -- means none: a legacy row of any other project fails at
    #: preflight instead of posting as the deployment account. A project
    #: is never added here implicitly.
    legacy_threads_projects: tuple[str, ...] = ()
    #: Base64 X25519 public key. The API seals credentials with it and
    #: can open none of them.
    credentials_public_key: str | None = None
    #: Private key files (PKCS#8 PEM, mode 600) of the worker and the
    #: operator CLI. Several during a key rotation.
    credentials_private_key_files: tuple[Path, ...] = ()

    # Generation worker (SMM-026A/2.1). A separate process from the
    # publish worker above; see ai_smm.generation_worker. It never reads
    # dry_run/worker_id/lease_seconds -- those belong to publishing.
    generation_worker_id: str = "generation-worker-1"
    generation_poll_interval_seconds: int = 15
    #: Generous margin over the slowest real run observed
    #: (docs/reports/smm-026a/part1b-e2e-20261015, ~90s worst case): a
    #: job that is merely slow is never mistaken for an abandoned one.
    generation_lease_seconds: int = 600
    generation_max_attempts: int = 2
    #: Caps the agent graph's own revision loop (SMMState.max_revisions),
    #: regardless of what a caller might ask for -- bounds the number of
    #: LLM calls, and so the cost, of one job.
    generation_max_revisions: int = 3

    #: Topic ideation (SMM-035B3A): whether the API may queue an ideation
    #: job. Deliberately not read from the environment, so no deployment
    #: can switch it on: the worker exists since SMM-035B3B1, but turning
    #: real ideation on is a separately approved stage.
    topic_ideation_jobs_enabled: bool = False

    # Topic ideation worker (SMM-035B3B1). A process of its own; see
    # ai_smm.ideation_worker. One structured LLM call per attempt, so the
    # lease only has to cover that call and the completion.
    ideation_worker_id: str = "ideation-worker-1"
    ideation_poll_interval_seconds: int = 30
    ideation_lease_seconds: int = 300

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
        api_session_lifetime_seconds=_env_int(
            "AI_SMM_API_SESSION_LIFETIME_SECONDS", 7 * 24 * 3600
        ),
        api_session_idle_seconds=_env_int(
            "AI_SMM_API_SESSION_IDLE_SECONDS", 24 * 3600
        ),
        # Secure by default: an http deployment has to say so explicitly
        # rather than lose the flag by forgetting to set it.
        api_cookie_secure=_env_bool("AI_SMM_API_COOKIE_SECURE", True),
        api_docs_enabled=_env_bool("AI_SMM_API_DOCS_ENABLED", True),
        # Read-only by default, like the cookie flag above: a deployment
        # that wants to edit has to say so.
        api_mutations_enabled=_env_bool(
            "AI_SMM_API_MUTATIONS_ENABLED", False
        ),
        api_auth_routes_enabled=_env_bool("AI_SMM_API_AUTH_ROUTES", True),
        auth_allowed_peers=_networks("AI_SMM_AUTH_ALLOWED_PEERS"),
        legacy_threads_projects=_legacy_projects(),
        credentials_public_key=_env("AI_SMM_CREDENTIALS_PUBLIC_KEY"),
        credentials_private_key_files=tuple(
            Path(item).expanduser()
            for item in _env_list("AI_SMM_CREDENTIALS_PRIVATE_KEY_FILES")
        ),
        generation_worker_id=(
            _env("AI_SMM_GENERATION_WORKER_ID", "generation-worker-1")
            or "generation-worker-1"
        ),
        generation_poll_interval_seconds=_env_int(
            "AI_SMM_GENERATION_POLL_INTERVAL_SECONDS", 15
        ),
        generation_lease_seconds=_env_int(
            "AI_SMM_GENERATION_LEASE_SECONDS", 600
        ),
        generation_max_attempts=_env_int(
            "AI_SMM_GENERATION_MAX_ATTEMPTS", 2
        ),
        generation_max_revisions=_env_int(
            "AI_SMM_GENERATION_MAX_REVISIONS", 3
        ),
        ideation_worker_id=(
            _env("AI_SMM_IDEATION_WORKER_ID", "ideation-worker-1")
            or "ideation-worker-1"
        ),
        ideation_poll_interval_seconds=_env_int(
            "AI_SMM_IDEATION_POLL_INTERVAL_SECONDS", 30
        ),
        ideation_lease_seconds=_env_int("AI_SMM_IDEATION_LEASE_SECONDS", 300),
    )


def _env_list(name: str) -> tuple[str, ...]:
    raw = _env(name)

    if raw is None:
        return ()

    return tuple(
        item.strip() for item in raw.split(",") if item.strip()
    )


def _networks(name: str) -> tuple[str, ...]:
    networks = []

    for item in _env_list(name):
        try:
            networks.append(str(ipaddress.ip_network(item, strict=True)))
        except ValueError as exc:
            raise ValueError(f"{name}: {item!r} is not a network (CIDR).") from exc

    return tuple(networks)


def _legacy_projects() -> tuple[str, ...]:
    # Imported here: the id rule lives with the project service, which
    # imports this module.
    from ai_smm.application.project_ids import is_valid_project_id

    values = _env_list("AI_SMM_LEGACY_THREADS_PROJECTS")

    for value in values:
        if not is_valid_project_id(value, allow_reserved=True):
            raise ValueError(
                "AI_SMM_LEGACY_THREADS_PROJECTS contains an invalid project "
                f"id: {value!r}."
            )

    return values


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
