"""The step that turns one claimed queue row into one Threads post.

The whole design exists to answer one question safely: if the process does
not learn the outcome of the publish call, did the post get created? Because
that cannot be answered from inside the client, the row goes to needs_review
and no automatic retry ever happens. Only phases that provably did not reach
Threads are retried.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timezone
from pathlib import Path
from typing import Any, Protocol

import httpx
from sqlalchemy.orm import Session

from ai_smm.config import Settings
from ai_smm.db.models import (
    AttemptOutcome,
    AttemptPhase,
    Publication,
    PublicationStatus,
)
from ai_smm.logging_setup import get_logger
from ai_smm.publishing.threads import ThreadsMediaFetchError
from ai_smm.queue import (
    mark_dry_run,
    mark_needs_review,
    mark_permanent_failure,
    mark_published,
    mark_publishing,
    mark_retryable_failure,
    release_claim,
    start_attempt,
    utcnow,
)


logger = get_logger(__name__)


class PublisherProtocol(Protocol):
    """The part of ThreadsPublisher this service depends on."""

    def upload_image(self, local_path: str | Path) -> str: ...

    def publish_text(self, text: str) -> dict[str, Any]: ...

    def create_image_post(
        self, image_url: str, text: str = ..., alt_text: str = ...
    ) -> dict[str, Any]: ...

    def publish_carousel(
        self, text: str, images: list[dict[str, str]]
    ) -> dict[str, Any]: ...

    def publish_thread(
        self, items: list[dict[str, Any]]
    ) -> list[dict[str, Any]]: ...


#: Errors proving the request never reached a state where a post could exist.
PREFLIGHT_SAFE_ERRORS = (
    FileNotFoundError,
    ValueError,
    OSError,
)

#: Network failures where no response was received at all.
AMBIGUOUS_NETWORK_ERRORS = (
    httpx.TimeoutException,
    httpx.RemoteProtocolError,
    httpx.ReadError,
    httpx.WriteError,
)


class PublishBlocked(Exception):
    """The row must not be published: approval, review or policy gate."""


@dataclass
class PublishOutcome:
    publication_id: int
    status: str
    threads_post_id: str | None = None
    detail: str | None = None


def _resolve_image_paths(
    publication: Publication, *, media_root: Path
) -> list[dict[str, str]]:
    """Resolve stored relative image paths, refusing anything outside root.

    media_root is the base the stored paths are relative to. In a source
    checkout that is the repository root; in the container it is the parent
    of the read-only knowledge mount.
    """

    resolved: list[dict[str, str]] = []

    for image in publication.images:
        raw_path = image.get("path")

        if not raw_path:
            raise ValueError(
                f"Publication {publication.id} has an image without a path."
            )

        candidate = Path(raw_path)

        if candidate.is_absolute():
            raise ValueError(
                "Image paths must be relative to the project root: "
                f"{raw_path}"
            )

        real_path = (media_root / candidate).resolve()

        if not real_path.is_relative_to(media_root.resolve()):
            raise ValueError(f"Image path escapes the media root: {raw_path}")

        if not real_path.is_file():
            raise FileNotFoundError(f"Image file not found: {real_path}")

        resolved.append(
            {
                "path": str(real_path),
                "alt_text": str(image.get("alt_text") or ""),
            }
        )

    return resolved


def validate_ready_to_publish(
    publication: Publication,
    *,
    require_human_review: bool = True,
    require_claimed: bool = True,
) -> None:
    """Gates that must hold before any network call.

    require_claimed is relaxed only by the preview command: an operator
    inspecting a record before publishing it holds no claim, and refusing
    to show them the content would defeat the point of a preview.

    Raises PublishBlocked, which the caller turns into a released claim --
    not into a failed attempt, because nothing was attempted.
    """

    if publication.threads_post_id or publication.published_at:
        raise PublishBlocked(
            f"Publication {publication.id} already carries publish metadata; "
            "refusing to publish it again."
        )

    if require_claimed and publication.status not in {
        PublicationStatus.CLAIMED,
        PublicationStatus.SCHEDULED,
    }:
        raise PublishBlocked(
            f"Publication {publication.id} has status "
            f"{publication.status.value}; only a claimed row is publishable."
        )

    if not require_claimed and publication.status in {
        PublicationStatus.PUBLISHED,
        PublicationStatus.CANCELLED,
    }:
        raise PublishBlocked(
            f"Publication {publication.id} is {publication.status.value}; "
            "it will not be published again."
        )

    if require_human_review and not publication.human_reviewed:
        raise PublishBlocked(
            f"Publication {publication.id} is not marked human_reviewed; "
            "approve the exact text and images first."
        )

    if not publication.body.strip():
        raise PublishBlocked(
            f"Publication {publication.id} has empty text."
        )

    image_count = len(publication.images)

    if publication.format == "image" and image_count != 1:
        raise PublishBlocked(
            f"Format 'image' needs exactly 1 image, got {image_count}."
        )

    if publication.format == "carousel" and not 2 <= image_count <= 20:
        raise PublishBlocked(
            f"Format 'carousel' needs 2..20 images, got {image_count}."
        )

    if publication.format == "text" and image_count:
        raise PublishBlocked(
            "Format 'text' cannot carry images."
        )

    if publication.format == "thread" and not publication.items:
        raise PublishBlocked(
            "Format 'thread' needs items."
        )


def publish_claimed_publication(
    session: Session,
    publication: Publication,
    *,
    settings: Settings,
    publisher_factory: Any,
    media_root: Path,
    require_human_review: bool = True,
) -> PublishOutcome:
    """Publish one claimed row, or record exactly why it was not published."""

    actor = f"worker:{settings.worker_id}"

    context = {
        "publication_id": publication.id,
        "project_id": publication.project_id,
        "format": publication.format,
        "dry_run": settings.dry_run,
    }

    try:
        validate_ready_to_publish(
            publication, require_human_review=require_human_review
        )
    except PublishBlocked as exc:
        logger.warning(
            "Publication blocked before any external call",
            extra={"context": {**context, "reason": str(exc)}},
        )

        release_claim(
            session,
            publication,
            actor=actor,
            reason=f"blocked: {exc}",
        )
        publication.last_error = str(exc)[:4000]
        session.commit()

        return PublishOutcome(
            publication_id=publication.id,
            status="blocked",
            detail=str(exc),
        )

    attempt = start_attempt(
        session,
        publication,
        worker_id=settings.worker_id,
        phase=AttemptPhase.PREFLIGHT,
    )

    # --- Media preflight -------------------------------------------------
    # Everything here is safely retryable: no post can exist yet.
    try:
        images = _resolve_image_paths(publication, media_root=media_root)
    except PREFLIGHT_SAFE_ERRORS as exc:
        reason = f"{type(exc).__name__}: {exc}"

        mark_permanent_failure(
            session,
            publication,
            attempt=attempt,
            reason=reason,
            actor=actor,
            error_type=type(exc).__name__,
        )

        session.commit()

        logger.error(
            "Media resolution failed",
            extra={"context": {**context, "error": reason}},
        )

        return PublishOutcome(
            publication_id=publication.id,
            status="failed",
            detail=reason,
        )

    if settings.dry_run:
        mark_dry_run(
            session,
            publication,
            attempt=attempt,
            actor=actor,
            details={
                "format": publication.format,
                "characters": len(publication.body),
                "images": len(images),
                "would_publish_at": utcnow().isoformat(),
            },
        )

        session.commit()

        logger.info(
            "Dry run: publication is due and valid, nothing was sent",
            extra={
                "context": {
                    **context,
                    "characters": len(publication.body),
                    "images": len(images),
                }
            },
        )

        return PublishOutcome(
            publication_id=publication.id,
            status="dry_run",
            detail="No external call was made.",
        )

    publisher = publisher_factory()

    uploaded: list[dict[str, str]] = []

    try:
        for image in images:
            uploaded.append(
                {
                    "url": publisher.upload_image(image["path"]),
                    "alt_text": image["alt_text"],
                }
            )
    except ThreadsMediaFetchError as exc:
        # Meta reports this with is_transient=false: the same URL will keep
        # failing, so a retry only hides the cause.
        reason = f"ThreadsMediaFetchError: {exc}"

        mark_needs_review(
            session,
            publication,
            attempt=attempt,
            reason=reason,
            actor=actor,
            outcome=AttemptOutcome.ERROR,
            error_type="ThreadsMediaFetchError",
        )
        session.commit()

        return PublishOutcome(
            publication_id=publication.id,
            status="needs_review",
            detail=reason,
        )
    except Exception as exc:
        reason = f"Media preflight failed: {type(exc).__name__}: {exc}"

        mark_retryable_failure(
            session,
            publication,
            attempt=attempt,
            reason=reason,
            actor=actor,
            max_attempts=settings.max_attempts,
            error_type=type(exc).__name__,
        )

        session.commit()

        logger.warning(
            "Media preflight failed; nothing reached Threads",
            extra={"context": {**context, "error": reason}},
        )

        return PublishOutcome(
            publication_id=publication.id,
            status="retry_scheduled",
            detail=reason,
        )

    # --- Point of no return ----------------------------------------------
    # From here a post may come into existence, so the row is marked and
    # committed before the call: a crash now is reconstructable.
    attempt.phase = AttemptPhase.PUBLISH
    attempt.details = {
        "uploaded_image_count": len(uploaded),
        "format": publication.format,
    }
    mark_publishing(session, publication)
    session.commit()

    try:
        result = _dispatch_publish(
            publisher, publication, uploaded
        )
    except AMBIGUOUS_NETWORK_ERRORS as exc:
        # No response was received. Threads may have created the post.
        reason = (
            "No response from Threads during publish "
            f"({type(exc).__name__}: {exc}). The post may or may not exist. "
            "Verify the account, then use 'reconcile' -- automatic retry is "
            "disabled for this case."
        )

        mark_needs_review(
            session,
            publication,
            attempt=attempt,
            reason=reason,
            actor=actor,
            outcome=AttemptOutcome.TIMEOUT,
            error_type=type(exc).__name__,
        )
        session.commit()

        return PublishOutcome(
            publication_id=publication.id,
            status="needs_review",
            detail=reason,
        )
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        attempt.http_status = status_code

        if 500 <= status_code < 600:
            # A 5xx from the publish endpoint is still ambiguous: the write
            # may have landed before the error was produced.
            reason = (
                f"Threads returned HTTP {status_code} during publish. "
                "The result is ambiguous; verify before any retry."
            )
            mark_needs_review(
                session,
                publication,
                attempt=attempt,
                reason=reason,
                actor=actor,
                outcome=AttemptOutcome.UNKNOWN,
                error_type="HTTPStatusError",
            )
            session.commit()

            return PublishOutcome(
                publication_id=publication.id,
                status="needs_review",
                detail=reason,
            )

        reason = (
            f"Threads rejected the request with HTTP {status_code}; "
            "no post was created."
        )

        mark_permanent_failure(
            session,
            publication,
            attempt=attempt,
            reason=reason,
            actor=actor,
            error_type="HTTPStatusError",
        )
        session.commit()

        return PublishOutcome(
            publication_id=publication.id,
            status="failed",
            detail=reason,
        )
    except Exception as exc:
        # Unknown failure after the point of no return: assume ambiguity.
        reason = (
            f"Unexpected error during publish ({type(exc).__name__}: {exc}). "
            "Treated as ambiguous; verify the account before any retry."
        )

        mark_needs_review(
            session,
            publication,
            attempt=attempt,
            reason=reason,
            actor=actor,
            outcome=AttemptOutcome.UNKNOWN,
            error_type=type(exc).__name__,
        )
        session.commit()

        return PublishOutcome(
            publication_id=publication.id,
            status="needs_review",
            detail=reason,
        )

    post_id = _extract_post_id(result)

    if not post_id:
        reason = (
            "Threads accepted the request but returned no post id. "
            "The publication may exist; verify before any retry."
        )

        mark_needs_review(
            session,
            publication,
            attempt=attempt,
            reason=reason,
            actor=actor,
            outcome=AttemptOutcome.UNKNOWN,
            error_type="MissingPostId",
        )
        session.commit()

        return PublishOutcome(
            publication_id=publication.id,
            status="needs_review",
            detail=reason,
        )

    mark_published(
        session,
        publication,
        attempt=attempt,
        threads_post_id=post_id,
        actor=actor,
    )
    session.commit()

    logger.info(
        "Published",
        extra={"context": {**context, "threads_post_id": post_id}},
    )

    return PublishOutcome(
        publication_id=publication.id,
        status="published",
        threads_post_id=post_id,
    )


def _dispatch_publish(
    publisher: PublisherProtocol,
    publication: Publication,
    uploaded: list[dict[str, str]],
) -> Any:
    if publication.format == "carousel":
        return publisher.publish_carousel(
            text=publication.body, images=uploaded
        )

    if publication.format == "image":
        image = uploaded[0]

        return publisher.create_image_post(
            image_url=image["url"],
            text=publication.body,
            alt_text=image["alt_text"],
        )

    if publication.format == "thread":
        return publisher.publish_thread(publication.items)

    if publication.format == "text":
        return publisher.publish_text(publication.body)

    raise ValueError(f"Unsupported format: {publication.format}")


def _extract_post_id(result: Any) -> str | None:
    if isinstance(result, dict):
        value = result.get("id")

        return str(value) if value else None

    if isinstance(result, list) and result:
        # publish_thread returns the list of created posts; the root post is
        # the identity of the publication.
        first = result[0]

        if isinstance(first, dict) and first.get("id"):
            return str(first["id"])

    return None


def format_local(dt: Any, settings: Settings) -> str:
    if dt is None:
        return "-"

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(settings.display_tz).strftime("%Y-%m-%d %H:%M %Z")
