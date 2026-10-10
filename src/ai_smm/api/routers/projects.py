"""Read-only project access plus the settings editor.

There is no publishing endpoint here, and there will not be one: the API
is the control plane. A publication reaches Threads only when the worker
claims it from the queue, which is what keeps the number of processes that
can create a post at exactly one.
"""
from __future__ import annotations

from fastapi import APIRouter

from ai_smm.api.dependencies import (
    AdminProject,
    CurrentUser,
    DbSession,
    ViewerProject,
)
from ai_smm.api.errors import ApiError, ErrorCode
from ai_smm.api.schemas import (
    ProjectOut,
    ProjectSettingsOut,
    ProjectSettingsPatch,
)
from ai_smm.application.projects import (
    VersionConflictError,
    get_settings_view,
    list_user_projects,
    update_settings,
)
from ai_smm.db.models import MembershipRole


router = APIRouter(prefix="/projects", tags=["projects"])


@router.get(
    "",
    response_model=list[ProjectOut],
    summary="Projects the caller is a member of",
)
def list_projects(db: DbSession, user: CurrentUser) -> list[ProjectOut]:
    return [
        _project_out(item.project, item.role)
        for item in list_user_projects(db, user=user)
    ]


@router.get(
    "/{project_id}",
    response_model=ProjectOut,
    summary="One project (viewer and above)",
)
def get_project(context: ViewerProject) -> ProjectOut:
    return _project_out(context.project, context.role)


@router.get(
    "/{project_id}/settings",
    response_model=ProjectSettingsOut,
    summary="Editorial settings, or the defaults when none are stored",
)
def get_project_settings(
    db: DbSession, context: ViewerProject
) -> ProjectSettingsOut:
    view = get_settings_view(db, project=context.project)

    return ProjectSettingsOut(**vars(view))


@router.patch(
    "/{project_id}/settings",
    response_model=ProjectSettingsOut,
    summary="Replace settings sections (admin and above)",
)
def patch_project_settings(
    payload: ProjectSettingsPatch,
    db: DbSession,
    context: AdminProject,
) -> ProjectSettingsOut:
    try:
        view = update_settings(
            db,
            project=context.project,
            expected_version=payload.expected_version,
            actor=context.actor,
            **payload.sections(),
        )
    except VersionConflictError as exc:
        raise ApiError(
            status_code=409,
            code=ErrorCode.VERSION_CONFLICT,
            message=(
                "These settings were modified by someone else. Re-read "
                "them and apply the change again."
            ),
            details={
                "expected_version": payload.expected_version,
                "current_version": exc.current_version,
            },
        ) from exc

    return ProjectSettingsOut(**vars(view))


def _project_out(project, role: MembershipRole) -> ProjectOut:
    return ProjectOut(
        id=project.id,
        display_name=project.display_name,
        description=project.description,
        default_platform=project.default_platform,
        default_timezone=project.default_timezone,
        default_language=project.default_language,
        is_active=project.is_active,
        version=project.version,
        created_at=project.created_at,
        updated_at=project.updated_at,
        role=role,
    )
