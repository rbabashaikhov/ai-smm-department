"""Publishing outcomes, with every external call mocked.

No test here may touch the network: the Threads client is replaced by a fake
whose behaviour each test chooses.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ai_smm.config import Settings
from ai_smm.db.models import (
    AttemptOutcome,
    AttemptPhase,
    Publication,
    PublicationStatus,
)
from ai_smm.publishing.service import (
    PublishBlocked,
    publish_claimed_publication,
    validate_ready_to_publish,
)
from ai_smm.queue import claim_due_publication


class FakePublisher:
    """Stands in for ThreadsPublisher; records what it was asked to do."""

    def __init__(
        self,
        *,
        publish_result: Any = None,
        publish_error: BaseException | None = None,
        upload_error: BaseException | None = None,
    ) -> None:
        self.publish_result = (
            publish_result
            if publish_result is not None
            else {"id": "17900000000000123"}
        )
        self.publish_error = publish_error
        self.upload_error = upload_error

        self.uploads: list[str] = []
        self.publish_calls: list[str] = []

    def upload_image(self, local_path: str | Path) -> str:
        if self.upload_error is not None:
            raise self.upload_error

        self.uploads.append(str(local_path))

        return f"https://media.example.test/{Path(local_path).name}"

    def _maybe_fail(self, kind: str) -> Any:
        self.publish_calls.append(kind)

        if self.publish_error is not None:
            raise self.publish_error

        return self.publish_result

    def publish_text(self, text: str) -> dict[str, Any]:
        return self._maybe_fail("text")

    def create_image_post(
        self, image_url: str, text: str = "", alt_text: str = ""
    ) -> dict[str, Any]:
        return self._maybe_fail("image")

    def publish_carousel(
        self, text: str, images: list[dict[str, str]]
    ) -> dict[str, Any]:
        return self._maybe_fail("carousel")

    def publish_thread(
        self, items: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        return self._maybe_fail("thread")


def _run(
    session: Session,
    publication: Publication,
    settings: Settings,
    publisher: FakePublisher,
    *,
    media_root: Path | None = None,
):
    return publish_claimed_publication(
        session,
        publication,
        settings=settings,
        publisher_factory=lambda: publisher,
        media_root=media_root or Path.cwd(),
    )


def _claim(session: Session, settings: Settings) -> Publication:
    publication = claim_due_publication(
        session, worker_id=settings.worker_id, lease_seconds=900
    )

    assert publication is not None
    session.commit()

    return publication


# --- happy path ----------------------------------------------------------


def test_text_publication_is_published(
    session: Session, settings: Settings, make_publication
) -> None:
    make_publication()
    publication = _claim(session, settings)

    publisher = FakePublisher(publish_result={"id": "17999"})
    outcome = _run(session, publication, settings, publisher)

    session.refresh(publication)

    assert outcome.status == "published"
    assert publisher.publish_calls == ["text"]
    assert publication.status is PublicationStatus.PUBLISHED
    assert publication.threads_post_id == "17999"
    assert publication.published_at is not None
    assert publication.last_error is None

    attempt = publication.attempts[-1]
    assert attempt.outcome is AttemptOutcome.SUCCESS
    assert attempt.threads_post_id == "17999"


# --- dry run -------------------------------------------------------------


def test_dry_run_makes_no_external_call(
    session: Session, settings: Settings, make_publication
) -> None:
    dry = replace(settings, dry_run=True)

    make_publication()
    publication = _claim(session, dry)

    publisher = FakePublisher()
    outcome = _run(session, publication, dry, publisher)

    session.refresh(publication)

    assert outcome.status == "dry_run"
    assert publisher.publish_calls == []
    assert publisher.uploads == []
    assert publication.status is PublicationStatus.SCHEDULED
    assert publication.threads_post_id is None
    assert publication.attempts[-1].outcome is AttemptOutcome.DRY_RUN
    # And it does not spin on every poll.
    assert publication.next_attempt_at is not None


def test_dry_run_does_not_mark_published(
    session: Session, settings: Settings, make_publication
) -> None:
    dry = replace(settings, dry_run=True)

    make_publication()
    publication = _claim(session, dry)
    _run(session, publication, dry, FakePublisher())

    session.refresh(publication)

    assert publication.published_at is None


# --- the ambiguity protocol ---------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        httpx.ReadTimeout("timed out"),
        httpx.ConnectTimeout("timed out"),
        httpx.WriteTimeout("timed out"),
        httpx.RemoteProtocolError("peer closed connection"),
    ],
)
def test_timeout_during_publish_goes_to_needs_review(
    session: Session,
    settings: Settings,
    make_publication,
    error: BaseException,
) -> None:
    """Threads may have created the post, so no automatic retry is allowed."""

    make_publication()
    publication = _claim(session, settings)

    publisher = FakePublisher(publish_error=error)
    outcome = _run(session, publication, settings, publisher)

    session.refresh(publication)

    assert outcome.status == "needs_review"
    assert publication.status is PublicationStatus.NEEDS_REVIEW
    assert publication.next_attempt_at is None
    assert publication.threads_post_id is None
    assert publication.attempts[-1].outcome is AttemptOutcome.TIMEOUT
    assert publication.attempts[-1].phase is AttemptPhase.PUBLISH

    # The decisive property: the scheduler will not pick it up again.
    assert claim_due_publication(
        session, worker_id="other", lease_seconds=900
    ) is None


def test_server_error_during_publish_is_ambiguous(
    session: Session, settings: Settings, make_publication
) -> None:
    request = httpx.Request("POST", "https://graph.threads.net/me/threads")
    response = httpx.Response(503, request=request)

    make_publication()
    publication = _claim(session, settings)

    outcome = _run(
        session,
        publication,
        settings,
        FakePublisher(
            publish_error=httpx.HTTPStatusError(
                "unavailable", request=request, response=response
            )
        ),
    )

    session.refresh(publication)

    assert outcome.status == "needs_review"
    assert publication.status is PublicationStatus.NEEDS_REVIEW
    assert publication.attempts[-1].outcome is AttemptOutcome.UNKNOWN
    assert publication.attempts[-1].http_status == 503


def test_client_error_is_a_permanent_failure(
    session: Session, settings: Settings, make_publication
) -> None:
    """A 4xx means nothing was created; retrying it would change nothing."""

    request = httpx.Request("POST", "https://graph.threads.net/me/threads")
    response = httpx.Response(400, request=request)

    make_publication()
    publication = _claim(session, settings)

    outcome = _run(
        session,
        publication,
        settings,
        FakePublisher(
            publish_error=httpx.HTTPStatusError(
                "bad request", request=request, response=response
            )
        ),
    )

    session.refresh(publication)

    assert outcome.status == "failed"
    assert publication.status is PublicationStatus.FAILED
    assert publication.next_attempt_at is None


def test_missing_post_id_is_ambiguous(
    session: Session, settings: Settings, make_publication
) -> None:
    make_publication()
    publication = _claim(session, settings)

    outcome = _run(
        session,
        publication,
        settings,
        FakePublisher(publish_result={"no_id": True}),
    )

    session.refresh(publication)

    assert outcome.status == "needs_review"
    assert publication.attempts[-1].error_type == "MissingPostId"


def test_unexpected_exception_is_ambiguous(
    session: Session, settings: Settings, make_publication
) -> None:
    make_publication()
    publication = _claim(session, settings)

    outcome = _run(
        session,
        publication,
        settings,
        FakePublisher(publish_error=RuntimeError("something odd")),
    )

    session.refresh(publication)

    assert outcome.status == "needs_review"
    assert publication.attempts[-1].outcome is AttemptOutcome.UNKNOWN


def test_media_fetch_error_is_not_retried(
    session: Session, settings: Settings, make_publication, tmp_path: Path
) -> None:
    """Meta reports subcode 2207052 as is_transient=false."""

    from ai_smm.publishing.threads import ThreadsMediaFetchError

    image = tmp_path / "pic.jpg"
    image.write_bytes(b"\xff\xd8fake")

    make_publication(
        publication_format="image",
        images=[{"path": "pic.jpg", "alt_text": "pic"}],
    )
    publication = _claim(session, settings)

    outcome = _run(
        session,
        publication,
        settings,
        FakePublisher(
            upload_error=ThreadsMediaFetchError(
                image_url="https://media.example.test/pic.jpg",
                response_body="{}",
                diagnosis="unreachable from Meta",
            )
        ),
        media_root=tmp_path,
    )

    session.refresh(publication)

    assert outcome.status == "needs_review"
    assert publication.status is PublicationStatus.NEEDS_REVIEW
    assert publication.next_attempt_at is None


# --- retryable preflight -------------------------------------------------


def test_storage_failure_is_retried_with_backoff(
    session: Session, settings: Settings, make_publication, tmp_path: Path
) -> None:
    """Nothing reached Threads, so another attempt is safe."""

    image = tmp_path / "pic.jpg"
    image.write_bytes(b"\xff\xd8fake")

    make_publication(
        publication_format="image",
        images=[{"path": "pic.jpg", "alt_text": "pic"}],
    )
    publication = _claim(session, settings)

    outcome = _run(
        session,
        publication,
        settings,
        FakePublisher(upload_error=ConnectionError("sftp down")),
        media_root=tmp_path,
    )

    session.refresh(publication)

    assert outcome.status == "retry_scheduled"
    assert publication.status is PublicationStatus.SCHEDULED
    assert publication.next_attempt_at is not None
    assert publication.next_attempt_at > datetime.now(timezone.utc)
    assert publication.attempts[-1].outcome is AttemptOutcome.ERROR


def test_retries_stop_at_max_attempts(
    session: Session, settings: Settings, make_publication, tmp_path: Path
) -> None:
    image = tmp_path / "pic.jpg"
    image.write_bytes(b"\xff\xd8fake")

    make_publication(
        publication_format="image",
        images=[{"path": "pic.jpg", "alt_text": "pic"}],
    )

    publisher = FakePublisher(upload_error=ConnectionError("sftp down"))

    for _ in range(settings.max_attempts):
        publication = claim_due_publication(
            session, worker_id=settings.worker_id, lease_seconds=900
        )

        if publication is None:
            # Backoff pushed it into the future; simulate waiting it out.
            stored = session.query(Publication).one()
            stored.next_attempt_at = None
            session.commit()
            continue

        session.commit()
        _run(
            session,
            publication,
            settings,
            publisher,
            media_root=tmp_path,
        )
        stored = session.query(Publication).one()
        stored.next_attempt_at = None
        session.commit()

    stored = session.query(Publication).one()

    assert stored.attempt_count >= settings.max_attempts
    assert stored.status is PublicationStatus.FAILED


def test_missing_image_file_is_a_permanent_failure(
    session: Session, settings: Settings, make_publication, tmp_path: Path
) -> None:
    make_publication(
        publication_format="image",
        images=[{"path": "absent.jpg", "alt_text": "x"}],
    )
    publication = _claim(session, settings)

    outcome = _run(
        session,
        publication,
        settings,
        FakePublisher(),
        media_root=tmp_path,
    )

    session.refresh(publication)

    assert outcome.status == "failed"
    assert publication.status is PublicationStatus.FAILED


def test_image_path_escaping_media_root_is_rejected(
    session: Session, settings: Settings, make_publication, tmp_path: Path
) -> None:
    make_publication(
        publication_format="image",
        images=[{"path": "../../etc/passwd", "alt_text": "x"}],
    )
    publication = _claim(session, settings)

    outcome = _run(
        session,
        publication,
        settings,
        FakePublisher(),
        media_root=tmp_path,
    )

    assert outcome.status == "failed"


# --- gates ---------------------------------------------------------------


def test_unreviewed_publication_is_blocked(
    session: Session, settings: Settings, make_publication
) -> None:
    make_publication(human_reviewed=False)
    publication = _claim(session, settings)

    publisher = FakePublisher()
    outcome = _run(session, publication, settings, publisher)

    session.refresh(publication)

    assert outcome.status == "blocked"
    assert publisher.publish_calls == []
    # Released, not failed: nothing was attempted.
    assert publication.status is PublicationStatus.SCHEDULED
    assert publication.attempt_count == 0


def test_already_published_row_is_blocked() -> None:
    publication = Publication(
        project_id="p",
        ordinal=1,
        title="t",
        format="text",
        body="b",
        images=[],
        items=[],
        status=PublicationStatus.CLAIMED,
        idempotency_key="k",
        threads_post_id="17900000000000003",
        human_reviewed=True,
    )

    with pytest.raises(PublishBlocked, match="already carries publish"):
        validate_ready_to_publish(publication)


@pytest.mark.parametrize(
    ("publication_format", "images", "items"),
    [
        ("image", [], []),
        ("image", [{"path": "a"}, {"path": "b"}], []),
        ("carousel", [{"path": "a"}], []),
        ("text", [{"path": "a"}], []),
        ("thread", [], []),
    ],
)
def test_format_and_media_mismatch_is_blocked(
    publication_format: str,
    images: list[dict[str, str]],
    items: list[Any],
) -> None:
    publication = Publication(
        project_id="p",
        ordinal=1,
        title="t",
        format=publication_format,
        body="b",
        images=images,
        items=items,
        status=PublicationStatus.CLAIMED,
        idempotency_key="k",
        human_reviewed=True,
    )

    with pytest.raises(PublishBlocked):
        validate_ready_to_publish(publication)


# --- deduplication at the storage layer ----------------------------------


def test_duplicate_threads_post_id_is_rejected_by_the_database(
    session: Session, make_publication
) -> None:
    make_publication(
        status=PublicationStatus.PUBLISHED,
        threads_post_id="17900000000000009",
        published_at=datetime.now(timezone.utc),
    )

    with pytest.raises(IntegrityError):
        make_publication(
            status=PublicationStatus.PUBLISHED,
            threads_post_id="17900000000000009",
            published_at=datetime.now(timezone.utc),
        )

    session.rollback()


def test_duplicate_idempotency_key_is_rejected(
    session: Session, project: str
) -> None:
    for _ in range(2):
        session.add(
            Publication(
                project_id=project,
                ordinal=100 + _,
                title="t",
                format="text",
                body="b",
                images=[],
                items=[],
                status=PublicationStatus.DRAFT,
                idempotency_key="same-key",
            )
        )

    with pytest.raises(IntegrityError):
        session.commit()

    session.rollback()


def test_publication_cannot_be_published_twice_end_to_end(
    session: Session, settings: Settings, make_publication
) -> None:
    """After a successful publish, a second cycle finds nothing to do."""

    make_publication()
    publication = _claim(session, settings)

    publisher = FakePublisher(publish_result={"id": "17900000000000011"})
    _run(session, publication, settings, publisher)

    assert claim_due_publication(
        session, worker_id="w2", lease_seconds=900
    ) is None
    assert publisher.publish_calls == ["text"]
