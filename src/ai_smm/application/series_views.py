"""Read projections of content series. No series is created or changed here.

Creating a series, attaching a part and the order gate all stay in
ai_smm.series, which the CLI and the worker use. This module only reads,
and the structural report it returns is that module's own
validate_series_structure, not a second opinion about what a valid series
is.

Named series_views rather than series so that `from ai_smm.series import
...` inside it unambiguously means the domain module.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ai_smm.application.publications import Page
from ai_smm.db.models import ContentSeries, Publication, PublicationStatus
from ai_smm.series import (
    count_published_parts,
    series_parts,
    validate_series_structure,
)


@dataclass(frozen=True)
class SeriesSummary:
    """A series as it appears in a list: counts, no part bodies."""

    series: ContentSeries
    parts_total: int
    parts_published: int


@dataclass(frozen=True)
class SeriesDetail:
    series: ContentSeries
    parts: list[Publication]
    parts_published: int
    issues: list[str]


def list_series_page(
    db: Session, *, project_id: str, limit: int, offset: int
) -> Page:
    """Series of one project, newest id last, with per-series counts."""

    stmt = select(ContentSeries).where(
        ContentSeries.project_id == project_id
    )

    total = int(
        db.execute(
            select(func.count()).select_from(stmt.subquery())
        ).scalar_one()
    )

    rows = list(
        db.execute(
            stmt.order_by(ContentSeries.id).limit(limit).offset(offset)
        ).scalars()
    )

    # One grouped query for the counts rather than two per row.
    counts: dict[int, tuple[int, int]] = {}

    if rows:
        counts = {
            series_id: (int(total_parts), int(published_parts))
            for series_id, total_parts, published_parts in db.execute(
                select(
                    Publication.series_id,
                    func.count(),
                    func.count().filter(
                        Publication.status == PublicationStatus.PUBLISHED
                    ),
                )
                .where(
                    Publication.series_id.in_(
                        [series.id for series in rows]
                    )
                )
                .group_by(Publication.series_id)
            ).all()
        }

    summaries = [
        SeriesSummary(
            series=series,
            parts_total=counts.get(series.id, (0, 0))[0],
            parts_published=counts.get(series.id, (0, 0))[1],
        )
        for series in rows
    ]

    return Page(items=summaries, total=total, limit=limit, offset=offset)


def get_series(db: Session, *, series_id: int) -> ContentSeries | None:
    """The row, whatever project owns it.

    The caller reads project_id off the result and checks membership
    against that, so an id in the URL grants nothing by itself.
    """

    return db.get(ContentSeries, series_id)


def series_detail(db: Session, *, series: ContentSeries) -> SeriesDetail:
    return SeriesDetail(
        series=series,
        parts=series_parts(db, series),
        parts_published=count_published_parts(db, series),
        issues=validate_series_structure(db, series),
    )
