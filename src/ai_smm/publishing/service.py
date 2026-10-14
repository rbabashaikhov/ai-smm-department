"""The step that turns one claimed queue row into one Threads post.

The whole design exists to answer one question safely: if the process does
not learn the outcome of the publish call, did the post get created? Because
that cannot be answered from inside the client, the row goes to needs_review
and no automatic retry ever happens. Only phases that provably did not reach
Threads are retried.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session

from ai_smm.config import Settings
from ai_smm.db.models import (
    AttemptOutcome,
    AttemptPhase,
    Publication,
    PublicationStatus,
)
from ai_smm.logging_setup import get_logger
from ai_smm.publishing.routing import RouteUnavailable, resolve_route
from ai_smm.publishing.threads import CredentialRejected, ThreadsMediaFetchError
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
from ai_smm.security.credentials import CredentialOpener, SecretToken
from ai_smm.series import check_series_readiness


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

    def publish_reply(
        self, text: str, reply_to_id: str
    ) -> dict[str, Any]: ...


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


def _default_account_publisher(token: SecretToken) -> Any:
    from ai_smm.publishing.threads import ThreadsPublisher

    # ThreadsPublisher() without a token reads the deployment token; an
    # account route must never get there, whatever the caller passed.
    if not isinstance(token, SecretToken) or not token.reveal():
        raise RuntimeError("An account route needs that account's own token.")

    return ThreadsPublisher(access_token=token)


def _default_identity_fetcher(token: SecretToken) -> Any:
    from ai_smm.publishing.threads import fetch_identity

    return fetch_identity(token)


def lineage_problem(session: Session, publication_id: int) -> str | None:
    """Why this row may not be delivered, by the database's guard, or None."""

    return session.execute(
        text("SELECT public.guard_publication_problem(:id)"), {"id": publication_id}
    ).scalar_one()


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
    account_publisher_factory: Any = None,
    identity_fetcher: Any = None,
    opener: CredentialOpener | None = None,
) -> PublishOutcome:
    """Publish one claimed row, or record exactly why it was not published.

    publisher_factory() builds the **legacy** publisher (deployment token)
    and is used only for a row without a social account, of a project
    allowed to use it. An account-bound row is published with
    account_publisher_factory(token) and that account's own token -- the
    two factories are separate so that no code path can hand an account
    row to the deployment publisher. identity_fetcher(token) proves the
    token is still the account's before anything is sent.
    """

    account_publisher_factory = (
        account_publisher_factory or _default_account_publisher
    )
    identity_fetcher = identity_fetcher or _default_identity_fetcher

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

    # Series order and reply linkage. Checked before an attempt is opened:
    # "it is not this part's turn yet" is not a failed attempt, it is a
    # reason to put the record back and look again later.
    readiness = check_series_readiness(session, publication)

    if not readiness.ready:
        logger.info(
            "Publication is not due yet within its series",
            extra={"context": {**context, "reason": readiness.reason}},
        )

        release_claim(
            session,
            publication,
            actor=actor,
            reason=f"series: {readiness.reason}",
        )
        publication.last_error = readiness.reason[:4000]
        # Do not spin on the same row every poll while the earlier part
        # is still waiting for a human.
        publication.next_attempt_at = utcnow() + timedelta(minutes=10)
        session.commit()

        return PublishOutcome(
            publication_id=publication.id,
            status="waiting_for_series",
            detail=readiness.reason,
        )

    attempt = start_attempt(
        session,
        publication,
        worker_id=settings.worker_id,
        phase=AttemptPhase.PREFLIGHT,
    )

    # --- Approval lineage ---------------------------------------------------
    # Asked of the database, whoever wrote the row (SMM-025D): a content
    # snapshot must still be exactly an approved revision, for the approved
    # account; a row from before the editorial layer must be reviewed and
    # account-less. The same function the Control Center's writes are
    # checked with. A failure is permanent: nothing was sent, and a person
    # must look at how the row came to be.
    problem = lineage_problem(session, publication.id)

    if problem is not None:
        reason = f"Approval lineage does not hold: {problem}"

        mark_permanent_failure(
            session,
            publication,
            attempt=attempt,
            reason=reason,
            actor=actor,
            error_type="ApprovalLineage",
        )
        session.commit()

        logger.error(
            "Publication refused: approval lineage does not hold",
            extra={"context": {**context, "reason": problem}},
        )

        return PublishOutcome(
            publication_id=publication.id, status="failed", detail=reason
        )

    # --- Route -----------------------------------------------------------
    # Which account, which token. A route that is not usable is a
    # permanent failure: a person must fix the account or the credential
    # and reschedule. Nothing else is tried.
    try:
        route = resolve_route(session, publication, settings, opener=opener)
    except RouteUnavailable as exc:
        reason = f"Publishing route unavailable ({exc.code}): {exc}"

        mark_permanent_failure(
            session,
            publication,
            attempt=attempt,
            reason=reason,
            actor=actor,
            error_type="RouteUnavailable",
        )
        session.commit()

        logger.error(
            "Publication has no usable publishing route",
            extra={"context": {**context, "reason": exc.code}},
        )

        return PublishOutcome(
            publication_id=publication.id,
            status="failed",
            detail=reason,
        )

    context = {**context, **route.describe()}
    attempt.details = {**(attempt.details or {}), **route.describe()}

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
                **route.describe(),
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

    if route.is_account and route.token is None:
        # Not reachable through resolve_route; refused explicitly rather
        # than asserted, so it holds under python -O as well.
        reason = "Account route without a decrypted token. Nothing was sent."
        mark_permanent_failure(
            session,
            publication,
            attempt=attempt,
            reason=reason,
            actor=actor,
            error_type="RouteUnavailable",
        )
        session.commit()

        return PublishOutcome(
            publication_id=publication.id, status="failed", detail=reason
        )

    if route.is_account:
        # The account's identity, proven before any upload or publish.
        try:
            identity = identity_fetcher(route.token)
        except CredentialRejected as exc:
            reason = (
                f"The credential of social account {route.social_account_id} "
                f"was rejected by Threads ({exc}). Replace it, then "
                "reschedule. Nothing was sent."
            )
            mark_permanent_failure(
                session,
                publication,
                attempt=attempt,
                reason=reason,
                actor=actor,
                error_type="CredentialRejected",
            )
            session.commit()

            return PublishOutcome(
                publication_id=publication.id, status="failed", detail=reason
            )
        except Exception as exc:
            reason = (
                "The account identity check could not complete "
                f"({type(exc).__name__}). Nothing was sent."
            )
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

            return PublishOutcome(
                publication_id=publication.id,
                status="retry_scheduled",
                detail=reason,
            )

        if identity.account_id != route.expected_external_id:
            reason = (
                f"The credential of social account {route.social_account_id} "
                "belongs to a different Threads account than the one this "
                "account is bound to. Nothing was sent; replace the "
                "credential with the right account's token."
            )
            mark_permanent_failure(
                session,
                publication,
                attempt=attempt,
                reason=reason,
                actor=actor,
                error_type="CredentialAccountMismatch",
            )
            session.commit()

            logger.error(
                "Credential identity does not match its account",
                extra={"context": context},
            )

            return PublishOutcome(
                publication_id=publication.id, status="failed", detail=reason
            )

        publisher = account_publisher_factory(route.token)
    else:
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
        **route.describe(),
    }
    mark_publishing(session, publication)
    session.commit()

    try:
        result = _dispatch_publish(
            publisher,
            publication,
            uploaded,
            reply_to_id=readiness.reply_to_id,
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

    if publication.series_id is not None:
        from ai_smm.db.models import ContentSeries
        from ai_smm.series import refresh_series_status

        series = session.get(ContentSeries, publication.series_id)

        if series is not None:
            refresh_series_status(session, series, actor=actor)

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
    *,
    reply_to_id: str | None = None,
) -> Any:
    if reply_to_id:
        # A reply inside a thread. Threads replies carry text only, so a
        # part with media must be a standalone post rather than a reply.
        if uploaded:
            raise ValueError(
                f"Publication {publication.id} is a reply but carries "
                f"{len(uploaded)} image(s); Threads replies are text only."
            )

        return publisher.publish_reply(
            text=publication.body,
            reply_to_id=reply_to_id,
        )

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
