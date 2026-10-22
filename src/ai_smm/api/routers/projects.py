"""Projects: create, read, edit the record and the editorial settings.

A project is created with its owner -- the caller -- in one
transaction (ai_smm.application.projects.create_project). There is no
publishing endpoint here, and there will not be one: the API
is the control plane. A publication reaches Threads only when the worker
claims it from the queue, which is what keeps the number of processes that
can create a post at exactly one.
"""
from __future__ import annotations

from fastapi import APIRouter, Response

from ai_smm.api.dependencies import (
    AppSettings,
    CurrentSession,
    CurrentUser,
    DbSession,
    OwnerProject,
    ViewerProject,
)
from ai_smm.api.errors import ApiError, ErrorCode
from ai_smm.api.mutation_gate import mutations_disabled
from ai_smm.api.schemas import (
    ProjectCreate,
    ProjectOut,
    ProjectPatch,
    ProjectSettingsOut,
    ProjectSettingsPatch,
)
from ai_smm.application.control_plane import (
    MutationsDisabled,
    require_mutations_enabled,
)
from ai_smm.application.projects import (
    ProjectAlreadyExists,
    ProjectCreationForbidden,
    ProjectInput,
    ProjectLimitReached,
    ProjectNameTaken,
    VersionConflictError,
    create_project,
    get_settings_view,
    list_user_projects,
    update_project_record,
    update_settings,
)
from ai_smm.config import Settings
from ai_smm.db.models import MembershipRole, Project


router = APIRouter(prefix="/projects", tags=["projects"])


@router.get(
    "",
    response_model=list[ProjectOut],
    summary="Projects the caller is a member of",
)
def list_projects(
    db: DbSession, user: CurrentUser, settings: AppSettings
) -> list[ProjectOut]:
    return [
        _project_out(item.project, item.role, settings)
        for item in list_user_projects(db, user=user)
    ]


@router.post(
    "",
    response_model=ProjectOut,
    status_code=201,
    summary="Create a project; the caller becomes its owner",
)
def create(
    payload: ProjectCreate,
    response: Response,
    db: DbSession,
    settings: AppSettings,
    authenticated: CurrentSession,
) -> ProjectOut:
    try:
        require_mutations_enabled(
            settings.api_mutations_enabled, command="create_project"
        )
        result = create_project(
            db,
            actor_user=authenticated.user,
            data=ProjectInput(
                id=payload.id,
                display_name=payload.display_name,
                description=payload.description,
                default_timezone=payload.default_timezone,
                default_language=payload.default_language,
                topics=payload.topics,
                tone_of_voice=payload.tone_of_voice,
            ),
        )
    except MutationsDisabled as exc:
        raise mutations_disabled(exc.command) from exc
    except ProjectCreationForbidden as exc:
        raise ApiError(
            status_code=403,
            code=ErrorCode.FORBIDDEN,
            message=str(exc),
        ) from exc
    except ProjectAlreadyExists as exc:
        raise ApiError(
            status_code=409,
            code=ErrorCode.PROJECT_ALREADY_EXISTS,
            message=str(exc),
            details={"field": "id"},
        ) from exc
    except ProjectNameTaken as exc:
        raise ApiError(
            status_code=409,
            code=ErrorCode.PROJECT_NAME_TAKEN,
            message=str(exc),
            details={"field": "display_name"},
        ) from exc
    except ProjectLimitReached as exc:
        raise ApiError(
            status_code=409,
            code=ErrorCode.PROJECT_LIMIT_REACHED,
            message=str(exc),
        ) from exc
    except ValueError as exc:
        raise ApiError(
            status_code=422,
            code=ErrorCode.VALIDATION_ERROR,
            message=str(exc),
        ) from exc

    if result.created:
        response.headers["Location"] = (
            f"/api/v1/projects/{result.project.id}"
        )
    else:
        # A repeat of the same create: nothing was written.
        response.status_code = 200

    return _project_out(result.project, MembershipRole.OWNER, settings)


@router.get(
    "/{project_id}",
    response_model=ProjectOut,
    summary="One project (viewer and above)",
)
def get_project(context: ViewerProject, settings: AppSettings) -> ProjectOut:
    return _project_out(context.project, context.role, settings)


@router.patch(
    "/{project_id}",
    response_model=ProjectOut,
    summary="Edit name, description, timezone, language (admin and above)",
)
def patch_project(
    payload: ProjectPatch,
    db: DbSession,
    settings: AppSettings,
    context: OwnerProject,
) -> ProjectOut:
    try:
        require_mutations_enabled(
            settings.api_mutations_enabled, command="update_project"
        )
        project = update_project_record(
            db,
            project=context.project,
            expected_version=payload.expected_version,
            actor_user=context.authenticated.user,
            actor=context.actor,
            changes=payload.changes(),
        )
    except MutationsDisabled as exc:
        raise mutations_disabled(exc.command) from exc
    except VersionConflictError as exc:
        raise ApiError(
            status_code=409,
            code=ErrorCode.VERSION_CONFLICT,
            message=(
                "This project was modified by someone else. Re-read it and "
                "apply the change again."
            ),
            details={
                "expected_version": payload.expected_version,
                "current_version": exc.current_version,
            },
        ) from exc
    except ProjectNameTaken as exc:
        raise ApiError(
            status_code=409,
            code=ErrorCode.PROJECT_NAME_TAKEN,
            message=str(exc),
            details={"field": "display_name"},
        ) from exc
    except ValueError as exc:
        raise ApiError(
            status_code=422,
            code=ErrorCode.VALIDATION_ERROR,
            message=str(exc),
        ) from exc

    return _project_out(project, context.role, settings)


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
    summary="Replace settings sections (owner only)",
)
def patch_project_settings(
    payload: ProjectSettingsPatch,
    db: DbSession,
    context: OwnerProject,
) -> ProjectSettingsOut:
    try:
        view = update_settings(
            db,
            project=context.project,
            expected_version=payload.expected_version,
            actor=context.actor,
            **payload.sections(),
        )
    except ValueError as exc:
        raise ApiError(
            status_code=422,
            code=ErrorCode.VALIDATION_ERROR,
            message=str(exc),
        ) from exc
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


def _project_out(
    project: Project, role: MembershipRole, settings: Settings
) -> ProjectOut:
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
        legacy_publishing=project.id in settings.legacy_threads_projects,
    )
