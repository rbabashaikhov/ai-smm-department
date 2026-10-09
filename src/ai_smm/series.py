"""Content series: ordering, linkage and the gates a part must pass.

A series is a set of publications meant to be read as one narrative. The
rules that matter are here rather than in the worker, because they decide
whether a part may be published at all:

* a part is never published before the part before it, when the series
  says order matters;
* a reply needs the real Threads id of its parent, which exists only once
  the parent has actually been published;
* positions within a series are unique, enforced by a partial unique index
  as well as by the checks below.

Nothing here applies to a publication with no series: those keep the exact
behaviour they had before series existed.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ai_smm.db.models import (
    ContentSeries,
    Project,
    Publication,
    PublicationStatus,
    PublishingStrategy,
    SeriesStatus,
)
from ai_smm.logging_setup import get_logger
from ai_smm.queue import record_audit, utcnow


logger = get_logger(__name__)


class SeriesOrderViolation(Exception):
    """A part cannot be published yet because of its place in the series."""


@dataclass(frozen=True)
class SeriesReadiness:
    """Why a part may or may not go out now."""

    ready: bool
    reason: str = ""
    #: reply_thread only: the Threads id this part must reply to.
    reply_to_id: str | None = None

    def __bool__(self) -> bool:
        return self.ready


# -- creation -------------------------------------------------------------


def create_series(
    session: Session,
    *,
    project_id: str,
    title: str,
    description: str = "",
    narrative_goal: str = "",
    target_audience: str = "",
    publishing_strategy: PublishingStrategy = (
        PublishingStrategy.STANDALONE_SERIES
    ),
    planned_total: int | None = None,
    enforce_order: bool = True,
    status: SeriesStatus = SeriesStatus.DRAFT,
    actor: str = "system",
) -> ContentSeries:
    if session.get(Project, project_id) is None:
        raise ValueError(f"Unknown project: {project_id}")

    if not title.strip():
        raise ValueError("A series needs a title.")

    series = ContentSeries(
        project_id=project_id,
        title=title.strip(),
        description=description,
        narrative_goal=narrative_goal,
        target_audience=target_audience,
        publishing_strategy=publishing_strategy,
        planned_total=planned_total,
        # A reply is impossible without its parent, so order is not
        # optional for a thread whatever the caller asked for.
        enforce_order=(
            True
            if publishing_strategy is PublishingStrategy.REPLY_THREAD
            else enforce_order
        ),
        status=status,
    )

    session.add(series)
    session.flush()

    record_audit(
        session,
        actor=actor,
        action="series_created",
        subject=f"series:{series.id}",
        details={
            "project_id": project_id,
            "title": series.title,
            "strategy": publishing_strategy.value,
            "planned_total": planned_total,
        },
    )

    return series


def attach_publication(
    session: Session,
    publication: Publication,
    *,
    series: ContentSeries,
    position: int,
    total: int | None = None,
    actor: str = "system",
    link_previous: bool = True,
) -> Publication:
    """Place an existing publication at a position in a series.

    Used both for newly drafted parts and for registering publications
    that already exist -- including ones already live, whose
    threads_post_id this never touches.
    """

    if position < 1:
        raise ValueError("series_position is 1-based.")

    if publication.project_id != series.project_id:
        raise ValueError(
            f"Publication {publication.id} belongs to project "
            f"{publication.project_id!r}, series to "
            f"{series.project_id!r}."
        )

    occupant = session.execute(
        select(Publication).where(
            Publication.series_id == series.id,
            Publication.series_position == position,
            Publication.id != publication.id,
        )
    ).scalar_one_or_none()

    if occupant is not None:
        raise ValueError(
            f"Position {position} of series {series.id} is already held by "
            f"publication {occupant.id}."
        )

    publication.series_id = series.id
    publication.series_position = position
    publication.series_total = total or series.planned_total
    publication.updated_at = utcnow()

    if link_previous:
        previous = session.execute(
            select(Publication).where(
                Publication.series_id == series.id,
                Publication.series_position == position - 1,
            )
        ).scalar_one_or_none()

        if previous is not None:
            publication.previous_publication_id = previous.id

            if series.is_reply_thread:
                publication.parent_publication_id = previous.id

        # A later part may already be waiting for this one.
        following = session.execute(
            select(Publication).where(
                Publication.series_id == series.id,
                Publication.series_position == position + 1,
            )
        ).scalar_one_or_none()

        if following is not None:
            following.previous_publication_id = publication.id

            if series.is_reply_thread:
                following.parent_publication_id = publication.id

    record_audit(
        session,
        actor=actor,
        action="series_attached",
        subject=f"publication:{publication.id}",
        details={
            "series_id": series.id,
            "position": position,
            "total": publication.series_total,
        },
    )

    return publication


# -- reading --------------------------------------------------------------


def series_parts(
    session: Session, series: ContentSeries
) -> list[Publication]:
    return list(
        session.execute(
            select(Publication)
            .where(Publication.series_id == series.id)
            .order_by(Publication.series_position)
        ).scalars()
    )


def previous_part(
    session: Session, publication: Publication
) -> Publication | None:
    if publication.series_id is None or not publication.series_position:
        return None

    if publication.previous_publication_id is not None:
        return session.get(
            Publication, publication.previous_publication_id
        )

    return session.execute(
        select(Publication).where(
            Publication.series_id == publication.series_id,
            Publication.series_position == publication.series_position - 1,
        )
    ).scalar_one_or_none()


def unpublished_earlier_parts(
    session: Session, publication: Publication
) -> list[Publication]:
    """Every earlier part that is not live yet."""

    if publication.series_id is None or not publication.series_position:
        return []

    return list(
        session.execute(
            select(Publication)
            .where(
                Publication.series_id == publication.series_id,
                Publication.series_position < publication.series_position,
                Publication.status != PublicationStatus.PUBLISHED,
            )
            .order_by(Publication.series_position)
        ).scalars()
    )


# -- the gate -------------------------------------------------------------


def check_series_readiness(
    session: Session, publication: Publication
) -> SeriesReadiness:
    """Decide whether this part may be published right now.

    Returns rather than raises, so a caller can release a claim instead of
    burning a retry on something that is simply not its turn yet.
    """

    if publication.series_id is None:
        # Not part of a series: nothing extra applies.
        return SeriesReadiness(ready=True)

    series = session.get(ContentSeries, publication.series_id)

    if series is None:
        return SeriesReadiness(
            ready=False,
            reason=(
                f"Publication {publication.id} references series "
                f"{publication.series_id}, which no longer exists."
            ),
        )

    if series.status is SeriesStatus.CANCELLED:
        return SeriesReadiness(
            ready=False,
            reason=f"Series {series.id} is cancelled.",
        )

    position = publication.series_position or 0

    if series.enforce_order:
        pending = unpublished_earlier_parts(session, publication)

        if pending:
            waiting = ", ".join(
                f"#{p.series_position} (id {p.id}, {p.status.value})"
                for p in pending
            )

            return SeriesReadiness(
                ready=False,
                reason=(
                    f"Part {position} of series {series.id} cannot go out "
                    f"before: {waiting}."
                ),
            )

    if not series.is_reply_thread:
        return SeriesReadiness(ready=True)

    # --- reply_thread ---------------------------------------------------
    if position <= 1:
        # The opening post of a thread is an ordinary top-level post.
        return SeriesReadiness(ready=True)

    parent = (
        session.get(Publication, publication.parent_publication_id)
        if publication.parent_publication_id is not None
        else previous_part(session, publication)
    )

    if parent is None:
        return SeriesReadiness(
            ready=False,
            reason=(
                f"Part {position} of series {series.id} is a reply but has "
                "no parent publication recorded."
            ),
        )

    if parent.status is not PublicationStatus.PUBLISHED:
        return SeriesReadiness(
            ready=False,
            reason=(
                f"Parent publication {parent.id} is "
                f"{parent.status.value}, not published; a reply cannot be "
                "created until it is."
            ),
        )

    if not parent.threads_post_id:
        # Defence in depth. ck_publications_published_has_id makes this
        # state unreachable through the database, but a reply built on a
        # guessed parent would attach to the wrong post, so the id is
        # checked rather than assumed.
        return SeriesReadiness(
            ready=False,
            reason=(
                f"Parent publication {parent.id} has no threads_post_id; "
                "the reply target is unknown."
            ),
        )

    return SeriesReadiness(
        ready=True, reply_to_id=parent.threads_post_id
    )


# -- narrative checks -----------------------------------------------------


def validate_series_structure(
    session: Session, series: ContentSeries
) -> list[str]:
    """Structural problems a human should see before publishing starts."""

    issues: list[str] = []
    parts = series_parts(session, series)

    if not parts:
        return [f"Series {series.id} has no publications."]

    positions = [p.series_position for p in parts]

    if len(set(positions)) != len(positions):
        issues.append("Series has duplicate positions.")

    expected = list(range(1, len(parts) + 1))

    if sorted(positions) != expected:
        issues.append(
            f"Positions are not a complete 1..{len(parts)} sequence: "
            f"{sorted(positions)}."
        )

    if series.planned_total and len(parts) != series.planned_total:
        issues.append(
            f"Series declares {series.planned_total} parts but holds "
            f"{len(parts)}."
        )

    totals = {p.series_total for p in parts if p.series_total}

    if len(totals) > 1:
        issues.append(
            f"Parts disagree on the series total: {sorted(totals)}."
        )

    if series.is_reply_thread:
        for part in parts:
            if (part.series_position or 0) <= 1:
                continue

            if part.parent_publication_id is None:
                issues.append(
                    f"Part {part.series_position} has no parent, which a "
                    "reply thread requires."
                )

    bodies = [(p.series_position, p.body.strip()) for p in parts]

    for index, (position, body) in enumerate(bodies):
        for other_position, other_body in bodies[index + 1 :]:
            if body and body == other_body:
                issues.append(
                    f"Parts {position} and {other_position} have identical "
                    "text."
                )

    return issues


def count_published_parts(
    session: Session, series: ContentSeries
) -> int:
    return int(
        session.execute(
            select(func.count())
            .select_from(Publication)
            .where(
                Publication.series_id == series.id,
                Publication.status == PublicationStatus.PUBLISHED,
            )
        ).scalar_one()
    )


def refresh_series_status(
    session: Session, series: ContentSeries, *, actor: str = "system"
) -> SeriesStatus:
    """Move a series to active or completed as its parts go out."""

    parts = series_parts(session, series)

    if not parts or series.status is SeriesStatus.CANCELLED:
        return series.status

    published = count_published_parts(session, series)
    previous_status = series.status

    if published == 0:
        new_status = (
            SeriesStatus.DRAFT
            if series.status is SeriesStatus.DRAFT
            else SeriesStatus.ACTIVE
        )
    elif published == len(parts):
        new_status = SeriesStatus.COMPLETED
    else:
        new_status = SeriesStatus.ACTIVE

    if new_status is not previous_status:
        series.status = new_status
        series.updated_at = utcnow()

        record_audit(
            session,
            actor=actor,
            action="series_status_changed",
            subject=f"series:{series.id}",
            details={
                "from": previous_status.value,
                "to": new_status.value,
                "published_parts": published,
                "total_parts": len(parts),
            },
        )

    return series.status


def series_context_for_copywriter(
    session: Session, publication: Publication
) -> dict[str, Any]:
    """What the Copywriter needs to write a part that follows on."""

    if publication.series_id is None:
        return {}

    series = session.get(ContentSeries, publication.series_id)

    if series is None:
        return {}

    previous = previous_part(session, publication)
    parts = series_parts(session, series)
    position = publication.series_position or 0

    following = next(
        (p for p in parts if (p.series_position or 0) == position + 1),
        None,
    )

    return {
        "series_title": series.title,
        "series_description": series.description,
        "narrative_goal": series.narrative_goal,
        "target_audience": series.target_audience,
        "publishing_strategy": series.publishing_strategy.value,
        "position": position,
        "total": publication.series_total or series.planned_total,
        "previous_title": previous.title if previous else None,
        "previous_text": previous.body if previous else None,
        "next_title": following.title if following else None,
        "is_first": position <= 1,
        "is_last": bool(
            publication.series_total
            and position >= publication.series_total
        ),
    }
