"""Publishing a series end to end, with the Threads API mocked.

The questions these answer: does a part wait its turn, does a reply get
the real id of its parent, and does any of this change how a publication
without a series behaves.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.orm import Session

from ai_smm.config import Settings
from ai_smm.db.models import (
    AttemptOutcome,
    Publication,
    PublicationStatus,
    PublishingStrategy,
    SeriesStatus,
)
from ai_smm.publishing.service import publish_claimed_publication
from ai_smm.queue import claim_due_publication
from tests.test_publish_service import FakePublisher


class SeriesPublisher(FakePublisher):
    """FakePublisher that also records replies."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.replies: list[tuple[str, str]] = []

    def publish_reply(
        self, text: str, reply_to_id: str
    ) -> dict[str, Any]:
        self.publish_calls.append("reply")
        self.replies.append((text, reply_to_id))

        if self.publish_error is not None:
            raise self.publish_error

        return self.publish_result


def _claim(session: Session, settings: Settings) -> Publication | None:
    publication = claim_due_publication(
        session, worker_id=settings.worker_id, lease_seconds=900
    )

    if publication is not None:
        session.commit()

    return publication


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


# --- standalone series ----------------------------------------------------


def test_the_first_part_publishes(
    session: Session,
    settings: Settings,
    make_publication,
    make_series,
) -> None:
    first = make_publication()
    second = make_publication()
    make_series(parts=[first, second])

    claimed = _claim(session, settings)
    assert claimed is not None and claimed.id == first.id

    publisher = SeriesPublisher(publish_result={"id": "17900000000000100"})
    outcome = _run(session, claimed, settings, publisher)

    session.refresh(first)

    assert outcome.status == "published"
    assert first.status is PublicationStatus.PUBLISHED
    assert publisher.publish_calls == ["text"]


def test_the_second_part_waits_and_sends_nothing(
    session: Session,
    settings: Settings,
    make_publication,
    make_series,
) -> None:
    """The decisive property: order is kept without burning an attempt."""

    first = make_publication()
    second = make_publication()
    make_series(parts=[first, second])

    # Make only the second part due.
    first.scheduled_at = None
    first.status = PublicationStatus.APPROVED
    session.commit()

    claimed = _claim(session, settings)
    assert claimed is not None and claimed.id == second.id

    publisher = SeriesPublisher()
    outcome = _run(session, claimed, settings, publisher)

    session.refresh(second)

    assert outcome.status == "waiting_for_series"
    assert publisher.publish_calls == []
    assert second.status is PublicationStatus.SCHEDULED
    # Not an attempt: nothing was attempted.
    assert second.attempt_count == 0
    assert second.threads_post_id is None
    # And it backs off rather than spinning every poll.
    assert second.next_attempt_at is not None


def test_the_second_part_publishes_once_the_first_is_live(
    session: Session,
    settings: Settings,
    make_publication,
    make_series,
) -> None:
    first = make_publication()
    second = make_publication()
    series = make_series(parts=[first, second])

    publisher = SeriesPublisher(publish_result={"id": "17900000000000101"})

    claimed = _claim(session, settings)
    _run(session, claimed, settings, publisher)

    publisher.publish_result = {"id": "17900000000000102"}

    second.next_attempt_at = None
    session.commit()

    claimed = _claim(session, settings)
    assert claimed is not None and claimed.id == second.id

    outcome = _run(session, claimed, settings, publisher)

    session.refresh(second)
    session.refresh(series)

    assert outcome.status == "published"
    assert second.threads_post_id == "17900000000000102"
    assert series.status is SeriesStatus.COMPLETED


def test_out_of_order_is_allowed_when_the_series_says_so(
    session: Session,
    settings: Settings,
    make_publication,
    make_series,
) -> None:
    first = make_publication()
    second = make_publication()
    make_series(parts=[first, second], enforce_order=False)

    first.scheduled_at = None
    first.status = PublicationStatus.APPROVED
    session.commit()

    claimed = _claim(session, settings)
    publisher = SeriesPublisher(publish_result={"id": "17900000000000103"})
    outcome = _run(session, claimed, settings, publisher)

    assert outcome.status == "published"
    assert publisher.publish_calls == ["text"]


# --- reply threads --------------------------------------------------------


def test_a_reply_is_sent_with_the_real_parent_id(
    session: Session,
    settings: Settings,
    make_publication,
    make_series,
) -> None:
    first = make_publication()
    second = make_publication(body="Второй пост треда, продолжение.")
    make_series(
        strategy=PublishingStrategy.REPLY_THREAD, parts=[first, second]
    )

    publisher = SeriesPublisher(publish_result={"id": "17900000000000200"})

    claimed = _claim(session, settings)
    _run(session, claimed, settings, publisher)

    publisher.publish_result = {"id": "17900000000000201"}
    second.next_attempt_at = None
    session.commit()

    claimed = _claim(session, settings)
    outcome = _run(session, claimed, settings, publisher)

    session.refresh(second)

    assert outcome.status == "published"
    assert publisher.publish_calls == ["text", "reply"]
    # The id handed to the API is the one the parent actually got.
    assert publisher.replies == [
        ("Второй пост треда, продолжение.", "17900000000000200")
    ]
    assert second.threads_post_id == "17900000000000201"


def test_a_reply_without_a_published_parent_sends_nothing(
    session: Session,
    settings: Settings,
    make_publication,
    make_series,
) -> None:
    first = make_publication()
    second = make_publication()
    make_series(
        strategy=PublishingStrategy.REPLY_THREAD, parts=[first, second]
    )

    first.scheduled_at = None
    first.status = PublicationStatus.APPROVED
    session.commit()

    claimed = _claim(session, settings)
    assert claimed is not None and claimed.id == second.id

    publisher = SeriesPublisher()
    outcome = _run(session, claimed, settings, publisher)

    assert outcome.status == "waiting_for_series"
    assert publisher.publish_calls == []
    assert publisher.replies == []


def test_a_reply_carrying_media_is_refused(
    session: Session,
    settings: Settings,
    make_publication,
    make_series,
    tmp_path: Path,
) -> None:
    """Threads replies are text only; sending one with media would fail
    in a way that leaves the outcome unclear."""

    image = tmp_path / "pic.jpg"
    image.write_bytes(b"\xff\xd8fake")

    first = make_publication()
    second = make_publication(
        publication_format="image",
        images=[{"path": "pic.jpg", "alt_text": "pic"}],
    )
    make_series(
        strategy=PublishingStrategy.REPLY_THREAD, parts=[first, second]
    )

    publisher = SeriesPublisher(publish_result={"id": "17900000000000210"})

    claimed = _claim(session, settings)
    _run(session, claimed, settings, publisher, media_root=tmp_path)

    second.next_attempt_at = None
    session.commit()

    claimed = _claim(session, settings)
    outcome = _run(
        session, claimed, settings, publisher, media_root=tmp_path
    )

    session.refresh(second)

    # The failure happens after the point of no return, so it is treated
    # as ambiguous rather than retried.
    assert outcome.status == "needs_review"
    assert second.status is PublicationStatus.NEEDS_REVIEW
    assert publisher.replies == []


def test_a_three_part_thread_chains_each_reply_to_the_one_before(
    session: Session,
    settings: Settings,
    make_publication,
    make_series,
) -> None:
    parts = [
        make_publication(body=f"Сообщение треда номер {i}.")
        for i in range(1, 4)
    ]
    series = make_series(
        strategy=PublishingStrategy.REPLY_THREAD, parts=parts
    )

    publisher = SeriesPublisher()
    post_ids = [
        "17900000000000301",
        "17900000000000302",
        "17900000000000303",
    ]

    for index, post_id in enumerate(post_ids):
        publisher.publish_result = {"id": post_id}

        for part in parts:
            part.next_attempt_at = None

        session.commit()

        claimed = _claim(session, settings)

        assert claimed is not None
        assert claimed.series_position == index + 1

        outcome = _run(session, claimed, settings, publisher)

        assert outcome.status == "published"

    session.refresh(series)

    assert publisher.publish_calls == ["text", "reply", "reply"]
    assert [reply_to for _, reply_to in publisher.replies] == [
        post_ids[0],
        post_ids[1],
    ]
    assert series.status is SeriesStatus.COMPLETED


# --- idempotency and retries ----------------------------------------------


def test_a_published_part_is_never_claimed_again(
    session: Session,
    settings: Settings,
    make_publication,
    make_series,
) -> None:
    first = make_publication()
    second = make_publication()
    make_series(parts=[first, second])

    publisher = SeriesPublisher(publish_result={"id": "17900000000000400"})

    claimed = _claim(session, settings)
    _run(session, claimed, settings, publisher)

    # The first part is live; claiming again must not return it.
    for part in (first, second):
        part.next_attempt_at = None

    session.commit()

    claimed = _claim(session, settings)

    assert claimed is not None
    assert claimed.id == second.id


def test_waiting_does_not_consume_a_retry(
    session: Session,
    settings: Settings,
    make_publication,
    make_series,
) -> None:
    """Waiting for an earlier part is not a failure, so it must not count
    towards the attempt limit."""

    first = make_publication()
    second = make_publication()
    make_series(parts=[first, second])

    first.scheduled_at = None
    first.status = PublicationStatus.APPROVED
    session.commit()

    publisher = SeriesPublisher()

    for _ in range(5):
        second.next_attempt_at = None
        session.commit()

        claimed = _claim(session, settings)
        assert claimed is not None

        _run(session, claimed, settings, publisher)

    session.refresh(second)

    assert second.attempt_count == 0
    assert second.status is PublicationStatus.SCHEDULED
    assert publisher.publish_calls == []


def test_an_ambiguous_reply_is_not_retried(
    session: Session,
    settings: Settings,
    make_publication,
    make_series,
) -> None:
    first = make_publication()
    second = make_publication()
    make_series(
        strategy=PublishingStrategy.REPLY_THREAD, parts=[first, second]
    )

    publisher = SeriesPublisher(publish_result={"id": "17900000000000500"})

    claimed = _claim(session, settings)
    _run(session, claimed, settings, publisher)

    publisher.publish_error = httpx.ReadTimeout("timed out")
    second.next_attempt_at = None
    session.commit()

    claimed = _claim(session, settings)
    outcome = _run(session, claimed, settings, publisher)

    session.refresh(second)

    assert outcome.status == "needs_review"
    assert second.status is PublicationStatus.NEEDS_REVIEW
    assert second.next_attempt_at is None
    assert second.attempts[-1].outcome is AttemptOutcome.TIMEOUT


def test_a_stuck_part_blocks_the_rest_of_the_series(
    session: Session,
    settings: Settings,
    make_publication,
    make_series,
) -> None:
    """A part under manual review must not be stepped over."""

    first = make_publication()
    second = make_publication()
    make_series(parts=[first, second])

    first.status = PublicationStatus.NEEDS_REVIEW
    first.scheduled_at = None
    session.commit()

    second.next_attempt_at = None
    session.commit()

    claimed = _claim(session, settings)
    publisher = SeriesPublisher()
    outcome = _run(session, claimed, settings, publisher)

    assert outcome.status == "waiting_for_series"
    assert "needs_review" in outcome.detail
    assert publisher.publish_calls == []


# --- dry run and backwards compatibility ----------------------------------


def test_dry_run_reports_a_series_part_without_sending(
    session: Session,
    settings: Settings,
    make_publication,
    make_series,
) -> None:
    first = make_publication()
    make_series(parts=[first])

    dry = replace(settings, dry_run=True)

    claimed = _claim(session, dry)
    publisher = SeriesPublisher()
    outcome = _run(session, claimed, dry, publisher)

    session.refresh(first)

    assert outcome.status == "dry_run"
    assert publisher.publish_calls == []
    assert first.threads_post_id is None


def test_a_publication_without_a_series_publishes_as_before(
    session: Session, settings: Settings, make_publication
) -> None:
    """The regression that matters most: nothing changed for old rows."""

    publication = make_publication()

    claimed = _claim(session, settings)
    publisher = SeriesPublisher(publish_result={"id": "17900000000000600"})
    outcome = _run(session, claimed, settings, publisher)

    session.refresh(publication)

    assert outcome.status == "published"
    assert publisher.publish_calls == ["text"]
    assert publisher.replies == []
    assert publication.threads_post_id == "17900000000000600"
    assert publication.series_id is None


def test_a_mix_of_series_and_single_publications_coexists(
    session: Session,
    settings: Settings,
    make_publication,
    make_series,
) -> None:
    standalone = make_publication(body="Одиночная публикация.")
    first = make_publication(body="Первая часть серии.")
    second = make_publication(body="Вторая часть серии.")
    make_series(parts=[first, second])

    publisher = SeriesPublisher()
    published: list[int] = []

    for index in range(3):
        publisher.publish_result = {"id": f"1790000000000070{index}"}

        for row in (standalone, first, second):
            row.next_attempt_at = None

        session.commit()

        claimed = _claim(session, settings)

        if claimed is None:
            break

        outcome = _run(session, claimed, settings, publisher)

        if outcome.status == "published":
            published.append(claimed.id)

    assert standalone.id in published
    assert first.id in published
    assert second.id in published
