"""Content series: ordering, linkage, and backwards compatibility.

The two properties that matter most here are that a part never goes out
before the part it depends on, and that a publication with no series keeps
behaving exactly as it did before series existed.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ai_smm.db.models import (
    Publication,
    PublicationStatus,
    PublishingStrategy,
    SeriesStatus,
)
from ai_smm.queue import claim_due_publication
from ai_smm.series import (
    attach_publication,
    check_series_readiness,
    count_published_parts,
    create_series,
    previous_part,
    refresh_series_status,
    series_context_for_copywriter,
    unpublished_earlier_parts,
    validate_series_structure,
)


def _publish(session: Session, publication: Publication, post_id: str) -> None:
    publication.status = PublicationStatus.PUBLISHED
    publication.threads_post_id = post_id
    publication.published_at = datetime.now(timezone.utc)
    session.commit()


# --- creation ------------------------------------------------------------


def test_create_series(session: Session, project: str) -> None:
    series = create_series(
        session,
        project_id=project,
        title="How a catalog consultant was built",
        narrative_goal="why deterministic selection beats vector search",
        planned_total=3,
    )
    session.commit()

    assert series.id is not None
    assert series.status is SeriesStatus.DRAFT
    assert (
        series.publishing_strategy is PublishingStrategy.STANDALONE_SERIES
    )
    assert series.enforce_order is True


def test_reply_thread_always_enforces_order(
    session: Session, project: str
) -> None:
    """A reply is impossible without its parent, so order is not optional."""

    series = create_series(
        session,
        project_id=project,
        title="A thread",
        publishing_strategy=PublishingStrategy.REPLY_THREAD,
        enforce_order=False,
    )

    assert series.enforce_order is True


def test_series_requires_a_known_project(session: Session) -> None:
    with pytest.raises(ValueError, match="Unknown project"):
        create_series(session, project_id="nope", title="x")


def test_series_requires_a_title(session: Session, project: str) -> None:
    with pytest.raises(ValueError, match="title"):
        create_series(session, project_id=project, title="   ")


# --- attaching and positions ---------------------------------------------


def test_attach_links_neighbours(
    session: Session, make_publication, make_series
) -> None:
    first = make_publication()
    second = make_publication()
    series = make_series(parts=[first, second])

    session.refresh(second)

    assert second.series_id == series.id
    assert second.series_position == 2
    assert second.series_total == 2
    assert second.previous_publication_id == first.id
    # standalone_series: no reply relationship.
    assert second.parent_publication_id is None


def test_reply_thread_sets_the_parent(
    session: Session, make_publication, make_series
) -> None:
    first = make_publication()
    second = make_publication()
    make_series(
        strategy=PublishingStrategy.REPLY_THREAD, parts=[first, second]
    )

    session.refresh(second)

    assert second.parent_publication_id == first.id
    assert second.previous_publication_id == first.id


def test_attaching_later_part_first_links_backwards(
    session: Session, make_publication, make_series
) -> None:
    """Parts may be registered out of order; the links still come out right."""

    first = make_publication()
    second = make_publication()
    series = make_series(parts=[])

    attach_publication(
        session, second, series=series, position=2, total=2, actor="t"
    )
    attach_publication(
        session, first, series=series, position=1, total=2, actor="t"
    )
    session.commit()
    session.refresh(second)

    assert second.previous_publication_id == first.id


def test_position_must_be_unique_within_a_series(
    session: Session, make_publication, make_series
) -> None:
    first = make_publication()
    second = make_publication()
    series = make_series(parts=[first])

    with pytest.raises(ValueError, match="already held"):
        attach_publication(
            session, second, series=series, position=1, actor="t"
        )


def test_database_also_rejects_a_duplicate_position(
    session: Session, make_publication, make_series
) -> None:
    """The partial unique index, independent of the application check."""

    first = make_publication()
    second = make_publication()
    series = make_series(parts=[first])

    second.series_id = series.id
    second.series_position = 1

    with pytest.raises(IntegrityError):
        session.commit()

    session.rollback()


def test_same_position_in_different_series_is_fine(
    session: Session, make_publication, make_series
) -> None:
    first = make_publication()
    second = make_publication()

    make_series(title="Series A", parts=[first])
    make_series(title="Series B", parts=[second])

    session.refresh(first)
    session.refresh(second)

    assert first.series_position == second.series_position == 1
    assert first.series_id != second.series_id


def test_position_must_be_positive(
    session: Session, make_publication, make_series
) -> None:
    publication = make_publication()
    series = make_series(parts=[])

    with pytest.raises(ValueError, match="1-based"):
        attach_publication(
            session, publication, series=series, position=0, actor="t"
        )


def test_series_and_publication_must_share_a_project(
    session: Session, make_publication, make_series, make_other_project
) -> None:
    publication = make_publication()
    series = make_series(parts=[])

    publication.project_id = make_other_project
    session.commit()

    with pytest.raises(ValueError, match="belongs to project"):
        attach_publication(
            session, publication, series=series, position=1, actor="t"
        )


# --- backwards compatibility ---------------------------------------------


def test_a_publication_without_a_series_is_unaffected(
    session: Session, make_publication
) -> None:
    """The behaviour every pre-series publication relies on."""

    publication = make_publication()

    assert publication.series_id is None
    assert publication.series_position is None
    assert publication.parent_publication_id is None

    readiness = check_series_readiness(session, publication)

    assert readiness.ready is True
    assert readiness.reply_to_id is None

    claimed = claim_due_publication(
        session, worker_id="w1", lease_seconds=900
    )

    assert claimed is not None
    assert claimed.id == publication.id


def test_series_helpers_are_no_ops_without_a_series(
    session: Session, make_publication
) -> None:
    publication = make_publication()

    assert previous_part(session, publication) is None
    assert unpublished_earlier_parts(session, publication) == []
    assert series_context_for_copywriter(session, publication) == {}


def test_membership_is_all_or_nothing(
    session: Session, make_publication, make_series
) -> None:
    """A position without a series makes ordering undecidable."""

    publication = make_publication()

    publication.series_position = 3

    with pytest.raises(IntegrityError):
        session.commit()

    session.rollback()


# --- ordering ------------------------------------------------------------


def test_second_part_waits_for_the_first(
    session: Session, make_publication, make_series
) -> None:
    first = make_publication()
    second = make_publication()
    make_series(parts=[first, second])

    readiness = check_series_readiness(session, second)

    assert readiness.ready is False
    assert "cannot go out before" in readiness.reason


def test_first_part_is_always_ready(
    session: Session, make_publication, make_series
) -> None:
    first = make_publication()
    second = make_publication()
    make_series(parts=[first, second])

    assert check_series_readiness(session, first).ready is True


def test_second_part_becomes_ready_once_the_first_is_published(
    session: Session, make_publication, make_series
) -> None:
    first = make_publication()
    second = make_publication()
    make_series(parts=[first, second])

    _publish(session, first, "17900000000000001")

    assert check_series_readiness(session, second).ready is True


def test_order_can_be_waived_for_a_standalone_series(
    session: Session, make_publication, make_series
) -> None:
    first = make_publication()
    second = make_publication()
    make_series(parts=[first, second], enforce_order=False)

    assert check_series_readiness(session, second).ready is True


def test_a_cancelled_series_blocks_its_parts(
    session: Session, make_publication, make_series
) -> None:
    part = make_publication()
    series = make_series(parts=[part])

    series.status = SeriesStatus.CANCELLED
    session.commit()

    readiness = check_series_readiness(session, part)

    assert readiness.ready is False
    assert "cancelled" in readiness.reason


def test_unpublished_earlier_parts_lists_them_in_order(
    session: Session, make_publication, make_series
) -> None:
    parts = [make_publication() for _ in range(4)]
    make_series(parts=parts)

    _publish(session, parts[0], "17900000000000002")

    pending = unpublished_earlier_parts(session, parts[3])

    assert [p.series_position for p in pending] == [2, 3]


# --- reply threads -------------------------------------------------------


def test_reply_gets_the_parent_post_id(
    session: Session, make_publication, make_series
) -> None:
    first = make_publication()
    second = make_publication()
    make_series(
        strategy=PublishingStrategy.REPLY_THREAD, parts=[first, second]
    )

    _publish(session, first, "17900000000000010")

    readiness = check_series_readiness(session, second)

    assert readiness.ready is True
    assert readiness.reply_to_id == "17900000000000010"


def test_reply_is_blocked_while_the_parent_is_unpublished(
    session: Session, make_publication, make_series
) -> None:
    first = make_publication()
    second = make_publication()
    make_series(
        strategy=PublishingStrategy.REPLY_THREAD, parts=[first, second]
    )

    readiness = check_series_readiness(session, second)

    assert readiness.ready is False
    assert readiness.reply_to_id is None


def test_reply_is_blocked_when_the_parent_is_missing(
    session: Session, make_publication, make_series
) -> None:
    """A reply with nothing to reply to must not become a top-level post."""

    first = make_publication()
    second = make_publication()
    series = make_series(
        strategy=PublishingStrategy.REPLY_THREAD, parts=[first, second]
    )

    _publish(session, first, "17900000000000011")

    # The parent row disappears; the link is cleared by ON DELETE SET NULL.
    second.parent_publication_id = None
    second.previous_publication_id = None
    session.delete(first)
    session.commit()

    readiness = check_series_readiness(session, second)

    assert readiness.ready is False
    assert "no parent" in readiness.reason
    assert series.id is not None


def test_a_parent_cannot_be_published_without_a_post_id(
    session: Session, make_publication, make_series
) -> None:
    """The reply target can never be unknown: the database forbids it.

    check_series_readiness also guards against a parent without a post id,
    but that branch is defence in depth: this constraint means the state
    cannot be reached in the first place, which is the stronger guarantee.
    """

    first = make_publication()
    second = make_publication()
    make_series(
        strategy=PublishingStrategy.REPLY_THREAD, parts=[first, second]
    )

    with pytest.raises(IntegrityError):
        session.execute(
            Publication.__table__.update()
            .where(Publication.id == first.id)
            .values(
                status=PublicationStatus.PUBLISHED,
                published_at=datetime.now(timezone.utc),
                threads_post_id=None,
            )
        )
        session.commit()

    session.rollback()


def test_thread_opening_post_needs_no_parent(
    session: Session, make_publication, make_series
) -> None:
    first = make_publication()
    make_series(strategy=PublishingStrategy.REPLY_THREAD, parts=[first])

    readiness = check_series_readiness(session, first)

    assert readiness.ready is True
    assert readiness.reply_to_id is None


# --- status ---------------------------------------------------------------


def test_series_status_follows_its_parts(
    session: Session, make_publication, make_series
) -> None:
    parts = [make_publication() for _ in range(3)]
    series = make_series(parts=parts)

    assert refresh_series_status(session, series) is SeriesStatus.DRAFT

    _publish(session, parts[0], "17900000000000020")
    assert refresh_series_status(session, series) is SeriesStatus.ACTIVE

    _publish(session, parts[1], "17900000000000021")
    _publish(session, parts[2], "17900000000000022")
    assert refresh_series_status(session, series) is SeriesStatus.COMPLETED

    assert count_published_parts(session, series) == 3


def test_cancelled_series_is_not_revived_by_a_published_part(
    session: Session, make_publication, make_series
) -> None:
    part = make_publication()
    series = make_series(parts=[part])

    series.status = SeriesStatus.CANCELLED
    session.commit()

    _publish(session, part, "17900000000000023")

    assert refresh_series_status(session, series) is SeriesStatus.CANCELLED


# --- structure ------------------------------------------------------------


def test_structure_of_a_well_formed_series(
    session: Session, make_publication, make_series
) -> None:
    series = make_series(
        parts=[
            make_publication(body=f"Distinct body for part {i}.")
            for i in range(1, 4)
        ]
    )

    assert validate_series_structure(session, series) == []


def test_structure_flags_a_gap_in_positions(
    session: Session, make_publication, make_series
) -> None:
    first = make_publication()
    third = make_publication()
    series = make_series(parts=[])

    attach_publication(
        session, first, series=series, position=1, actor="t"
    )
    attach_publication(
        session, third, series=series, position=3, actor="t"
    )
    session.commit()

    issues = validate_series_structure(session, series)

    assert any("complete 1..2 sequence" in i for i in issues)


def test_structure_flags_identical_texts(
    session: Session, make_publication, make_series
) -> None:
    first = make_publication(body="Exactly the same text.")
    second = make_publication(body="Exactly the same text.")
    series = make_series(parts=[first, second])

    issues = validate_series_structure(session, series)

    assert any("identical text" in i for i in issues)


def test_structure_flags_a_missing_parent_in_a_thread(
    session: Session, make_publication, make_series
) -> None:
    first = make_publication()
    second = make_publication()
    series = make_series(
        strategy=PublishingStrategy.REPLY_THREAD, parts=[first, second]
    )

    second.parent_publication_id = None
    session.commit()

    issues = validate_series_structure(session, series)

    assert any("no parent" in i for i in issues)


def test_structure_flags_a_disagreement_about_the_total(
    session: Session, make_publication, make_series
) -> None:
    parts = [make_publication() for _ in range(2)]
    series = make_series(parts=parts)

    parts[1].series_total = 5
    session.commit()

    issues = validate_series_structure(session, series)

    assert any("disagree on the series total" in i for i in issues)


# --- copywriter context ---------------------------------------------------


def test_context_for_a_middle_part(
    session: Session, make_publication, make_series
) -> None:
    parts = [
        make_publication(body=f"Part {i} body") for i in range(1, 4)
    ]
    make_series(parts=parts, title="Catalog consultant")

    context = series_context_for_copywriter(session, parts[1])

    assert context["series_title"] == "Catalog consultant"
    assert context["position"] == 2
    assert context["total"] == 3
    assert context["previous_text"] == "Part 1 body"
    assert context["next_title"] == parts[2].title
    assert context["is_first"] is False
    assert context["is_last"] is False


def test_context_marks_the_last_part(
    session: Session, make_publication, make_series
) -> None:
    parts = [make_publication() for _ in range(2)]
    make_series(parts=parts)

    context = series_context_for_copywriter(session, parts[1])

    assert context["is_last"] is True
    assert context["next_title"] is None


# --- self-reference -------------------------------------------------------


def test_a_publication_cannot_reply_to_itself(
    session: Session, make_publication
) -> None:
    publication = make_publication()

    publication.parent_publication_id = publication.id

    with pytest.raises(IntegrityError):
        session.commit()

    session.rollback()
