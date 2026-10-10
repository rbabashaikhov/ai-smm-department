"""Role checks, project isolation and optimistic locking on settings."""
from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_smm.db.models import AuditLog, MembershipRole, ProjectSettings


def _patch(client, project: str, csrf: str, **body):
    return client.patch(
        f"/api/v1/projects/{project}/settings",
        json=body,
        headers={"X-CSRF-Token": csrf},
    )


# -- listing and reading --------------------------------------------------


def test_project_list_requires_authentication(client) -> None:
    assert client.get("/api/v1/projects").status_code == 401


def test_project_list_shows_only_projects_with_a_membership(
    client, login, member, project: str, make_other_project
) -> None:
    other = make_other_project
    login(member(MembershipRole.VIEWER))

    response = client.get("/api/v1/projects")

    assert response.status_code == 200

    ids = [item["id"] for item in response.json()]

    assert ids == [project]
    assert other not in ids


def test_project_list_is_empty_without_memberships(
    client, login, make_user, project: str
) -> None:
    login(make_user())

    assert client.get("/api/v1/projects").json() == []


def test_get_project_returns_the_new_fields(
    client, login, member, project: str
) -> None:
    login(member(MembershipRole.VIEWER))

    body = client.get(f"/api/v1/projects/{project}").json()

    assert body["id"] == project
    assert body["description"] == ""
    assert body["default_timezone"] == "Europe/Moscow"
    assert body["default_language"] == "ru"
    assert body["version"] == 1
    assert body["role"] == "viewer"


# -- cross-project isolation ---------------------------------------------


def test_a_member_of_one_project_cannot_read_another(
    client, login, member, make_other_project
) -> None:
    """404, not 403: the API must not confirm that the id exists."""

    other = make_other_project
    login(member(MembershipRole.OWNER))

    response = client.get(f"/api/v1/projects/{other}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_a_member_of_one_project_cannot_read_anothers_settings(
    client, login, member, make_other_project
) -> None:
    other = make_other_project
    login(member(MembershipRole.OWNER))

    assert (
        client.get(f"/api/v1/projects/{other}/settings").status_code == 404
    )


def test_a_member_of_one_project_cannot_write_anothers_settings(
    client, session: Session, login, member, make_other_project
) -> None:
    other = make_other_project
    csrf = login(member(MembershipRole.OWNER))

    response = _patch(
        client, other, csrf, expected_version=0, brand_config={"x": 1}
    )

    assert response.status_code == 404

    session.expire_all()

    assert session.get(ProjectSettings, other) is None


def test_an_unknown_project_looks_the_same_as_a_forbidden_one(
    client, login, member, make_other_project
) -> None:
    other = make_other_project
    login(member(MembershipRole.OWNER))

    forbidden = client.get(f"/api/v1/projects/{other}")
    unknown = client.get("/api/v1/projects/no-such-project")

    assert forbidden.status_code == unknown.status_code == 404
    assert forbidden.json()["error"]["code"] == unknown.json()["error"]["code"]
    assert (
        forbidden.json()["error"]["message"]
        == unknown.json()["error"]["message"]
    )


# -- role hierarchy -------------------------------------------------------


@pytest.mark.parametrize(
    "role",
    [
        MembershipRole.VIEWER,
        MembershipRole.EDITOR,
        MembershipRole.ADMIN,
        MembershipRole.OWNER,
    ],
)
def test_every_role_may_read_settings(
    client, login, member, project: str, role: MembershipRole
) -> None:
    login(member(role))

    assert (
        client.get(f"/api/v1/projects/{project}/settings").status_code == 200
    )


@pytest.mark.parametrize(
    "role", [MembershipRole.VIEWER, MembershipRole.EDITOR]
)
def test_viewer_and_editor_may_not_patch_settings(
    client, session: Session, login, member, project: str, role
) -> None:
    csrf = login(member(role))

    response = _patch(
        client, project, csrf, expected_version=0, content_config={"a": 1}
    )

    assert response.status_code == 403

    error = response.json()["error"]

    assert error["code"] == "FORBIDDEN"
    assert error["details"] == {
        "required_role": "admin",
        "your_role": role.value,
    }

    session.expire_all()

    assert session.get(ProjectSettings, project) is None


@pytest.mark.parametrize(
    "role", [MembershipRole.ADMIN, MembershipRole.OWNER]
)
def test_admin_and_owner_may_patch_settings(
    client, login, member, project: str, role
) -> None:
    csrf = login(member(role))

    response = _patch(
        client, project, csrf, expected_version=0, content_config={"a": 1}
    )

    assert response.status_code == 200
    assert response.json()["version"] == 1


def test_a_user_without_a_membership_gets_404_not_403(
    client, login, make_user, project: str
) -> None:
    login(make_user())

    assert client.get(f"/api/v1/projects/{project}").status_code == 404


# -- settings defaults and versioning -----------------------------------


def test_settings_return_defaults_when_no_row_exists(
    client, login, member, project: str
) -> None:
    login(member(MembershipRole.VIEWER))

    body = client.get(f"/api/v1/projects/{project}/settings").json()

    assert body == {
        "project_id": project,
        "content_config": {},
        "publishing_config": {},
        "brand_config": {},
        "version": 0,
        "updated_at": None,
    }


def test_the_first_patch_creates_the_row_at_version_one(
    client, session: Session, login, member, project: str
) -> None:
    csrf = login(member(MembershipRole.ADMIN))

    response = _patch(
        client,
        project,
        csrf,
        expected_version=0,
        content_config={"tone": "dry"},
        brand_config={"voice": "engineer"},
    )

    assert response.status_code == 200

    body = response.json()

    assert body["version"] == 1
    assert body["content_config"] == {"tone": "dry"}
    assert body["brand_config"] == {"voice": "engineer"}
    assert body["publishing_config"] == {}
    assert body["updated_at"] is not None

    session.expire_all()
    stored = session.get(ProjectSettings, project)

    assert stored is not None
    assert stored.version == 1


def test_an_untouched_section_survives_a_patch(
    client, login, member, project: str
) -> None:
    csrf = login(member(MembershipRole.ADMIN))

    _patch(client, project, csrf, expected_version=0, content_config={"a": 1})
    body = _patch(
        client, project, csrf, expected_version=1, brand_config={"b": 2}
    ).json()

    assert body["content_config"] == {"a": 1}
    assert body["brand_config"] == {"b": 2}
    assert body["version"] == 2


def test_a_supplied_section_is_replaced_whole(
    client, login, member, project: str
) -> None:
    csrf = login(member(MembershipRole.ADMIN))

    _patch(
        client,
        project,
        csrf,
        expected_version=0,
        content_config={"a": 1, "b": 2},
    )
    body = _patch(
        client, project, csrf, expected_version=1, content_config={"a": 9}
    ).json()

    assert body["content_config"] == {"a": 9}


def test_a_stale_version_is_refused_with_409(
    client, login, member, project: str
) -> None:
    csrf = login(member(MembershipRole.ADMIN))

    _patch(client, project, csrf, expected_version=0, content_config={"a": 1})

    # Two editors read version 1; the first write wins.
    assert (
        _patch(
            client, project, csrf, expected_version=1, content_config={"a": 2}
        ).status_code
        == 200
    )

    stale = _patch(
        client, project, csrf, expected_version=1, content_config={"a": 3}
    )

    assert stale.status_code == 409

    error = stale.json()["error"]

    assert error["code"] == "VERSION_CONFLICT"
    assert error["details"] == {"expected_version": 1, "current_version": 2}


def test_the_losing_write_changed_nothing(
    client, session: Session, login, member, project: str
) -> None:
    csrf = login(member(MembershipRole.ADMIN))

    _patch(client, project, csrf, expected_version=0, content_config={"a": 1})
    _patch(client, project, csrf, expected_version=1, content_config={"a": 2})
    _patch(client, project, csrf, expected_version=1, content_config={"a": 3})

    session.expire_all()
    stored = session.get(ProjectSettings, project)

    assert stored.content_config == {"a": 2}
    assert stored.version == 2


def test_a_wrong_version_on_a_missing_row_is_also_409(
    client, login, member, project: str
) -> None:
    csrf = login(member(MembershipRole.ADMIN))

    response = _patch(
        client, project, csrf, expected_version=7, content_config={"a": 1}
    )

    assert response.status_code == 409
    assert response.json()["error"]["details"] == {
        "expected_version": 7,
        "current_version": 0,
    }


def test_a_patch_with_no_section_is_a_validation_error(
    client, login, member, project: str
) -> None:
    csrf = login(member(MembershipRole.ADMIN))

    response = _patch(client, project, csrf, expected_version=0)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_a_patch_cannot_smuggle_a_project_id(
    client, login, member, project: str, make_other_project
) -> None:
    """The project comes from the path and nowhere else."""

    csrf = login(member(MembershipRole.ADMIN))

    response = _patch(
        client,
        project,
        csrf,
        expected_version=0,
        content_config={"a": 1},
        project_id=make_other_project,
    )

    assert response.status_code == 422


def test_a_successful_patch_is_audited(
    client, session: Session, login, member, project: str
) -> None:
    user = member(MembershipRole.ADMIN)
    csrf = login(user)

    _patch(client, project, csrf, expected_version=0, content_config={"a": 1})

    session.expire_all()
    entry = session.scalars(
        select(AuditLog).where(AuditLog.action == "project.settings.update")
    ).one()

    assert entry.actor == f"user:{user.id}"
    assert entry.subject == f"project:{project}"
    assert entry.details == {"version": 1, "sections": ["content_config"]}


def test_a_refused_patch_writes_no_audit_entry(
    client, session: Session, login, member, project: str
) -> None:
    csrf = login(member(MembershipRole.VIEWER))

    _patch(client, project, csrf, expected_version=0, content_config={"a": 1})

    session.expire_all()
    entries = session.scalars(
        select(AuditLog).where(AuditLog.action == "project.settings.update")
    ).all()

    assert list(entries) == []
