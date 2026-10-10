"""Reading content series. Nothing here creates or changes one.

Creating a series and attaching a part stay in the operator CLI and in
ai_smm.series; this stage only gives a panel the ability to see what
exists. The structural report on the detail endpoint is that module's own
validate_series_structure, so the API and the CLI cannot disagree about
whether a series is well formed.
"""
from __future__ import annotations

from fastapi import APIRouter

from ai_smm.api.dependencies import (
    DbSession,
    PageParams,
    ViewerProject,
    ViewerSeries,
)
from ai_smm.api.routers.publications import summary_of
from ai_smm.api.schemas import (
    SeriesDetailOut,
    SeriesList,
    SeriesSummaryOut,
)
from ai_smm.application.series_views import list_series_page, series_detail
from ai_smm.db.models import ContentSeries


router = APIRouter(tags=["series"])


@router.get(
    "/projects/{project_id}/series",
    response_model=SeriesList,
    summary="Series of one project (viewer and above)",
)
def list_project_series(
    db: DbSession, page: PageParams, context: ViewerProject
) -> SeriesList:
    result = list_series_page(
        db,
        project_id=context.project.id,
        limit=page.limit,
        offset=page.offset,
    )

    return SeriesList(
        items=[
            SeriesSummaryOut(
                **_series_fields(item.series),
                parts_total=item.parts_total,
                parts_published=item.parts_published,
            )
            for item in result.items
        ],
        total=result.total,
        limit=result.limit,
        offset=result.offset,
    )


@router.get(
    "/series/{series_id}",
    response_model=SeriesDetailOut,
    summary="One series with its parts (viewer and above)",
)
def get_series(db: DbSession, context: ViewerSeries) -> SeriesDetailOut:
    detail = series_detail(db, series=context.series)

    return SeriesDetailOut(
        **_series_fields(detail.series),
        parts_total=len(detail.parts),
        parts_published=detail.parts_published,
        parts=[summary_of(part) for part in detail.parts],
        issues=detail.issues,
    )


def _series_fields(series: ContentSeries) -> dict[str, object]:
    return {
        "id": series.id,
        "project_id": series.project_id,
        "title": series.title,
        "description": series.description,
        "narrative_goal": series.narrative_goal,
        "target_audience": series.target_audience,
        "publishing_strategy": series.publishing_strategy,
        "status": series.status,
        "enforce_order": series.enforce_order,
        "planned_total": series.planned_total,
        "created_at": series.created_at,
        "updated_at": series.updated_at,
    }
