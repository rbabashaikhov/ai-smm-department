"""Creating and configuring projects through the API (SMM-025B B1)."""

from __future__ import annotations

import threading

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from ai_smm.db.models import (
    AuditLog,
    MembershipRole,
    Project,
    ProjectMembership,
    ProjectSettings,
    Publication,
)


def _csrf(token: str) -> dict[str, str]:
    return {"X-CSRF-Token": token}


def _create(client, csrf: str, **body):
    payload = {"id": "travel-notes", "display_name": "Путевые заметки", **body}

    return client.post("/api/v1/projects", json=payload, headers=_csrf(csrf))


def _count(session: Session, model, *where) -> int:
    session.expire_all()

    return session.scalar(select(func.count()).select_from(model).where(*where))


@pytest.fixture
def owner(member):
    return member(MembershipRole.OWNER)


# -- create ---------------------------------------------------------------


def test_an_owner_creates_a_project_and_owns_it(
    client, login, owner, session: Session
) -> None:
    csrf = login(owner)

    response = _create(
        client,
        csrf,
        description="Заметки о поездках",
        default_timezone="Asia/Tbilisi",
        topics=["путешествия", "фотография"],
        tone_of_voice="Дружелюбно, от первого лица.",
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["id"] == "travel-notes"
    assert body["role"] == "owner"
    assert body["legacy_publishing"] is False
    assert response.headers["Location"] == "/api/v1/projects/travel-notes"

    session.expire_all()
    project = session.get(Project, "travel-notes")
    memberships = session.scalars(
        select(ProjectMembership).where(ProjectMembership.project_id == "travel-notes")
    ).all()
    settings = session.get(ProjectSettings, "travel-notes")

    assert project.knowledge_path == "knowledge/projects/travel-notes"
    assert project.default_timezone == "Asia/Tbilisi"
    assert [(m.user_id, m.role) for m in memberships] == [
        (owner.id, MembershipRole.OWNER)
    ]
    assert settings.version == 1
    assert settings.content_config == {"topics": ["путешествия", "фотография"]}
    assert settings.brand_config == {"tone_of_voice": "Дружелюбно, от первого лица."}
    assert settings.publishing_config == {}
    assert _count(session, AuditLog, AuditLog.action == "project.create") == 1

    projects = client.get("/api/v1/projects").json()
    me = client.get("/api/v1/auth/me").json()

    assert "travel-notes" in [p["id"] for p in projects]
    assert "travel-notes" in [p["project_id"] for p in me["projects"]]


def test_creating_a_project_creates_no_publication_and_nothing_to_claim(
    client, login, owner, session: Session
) -> None:
    from ai_smm.queue import claim_due_publication

    before = _count(session, Publication)

    assert _create(client, login(owner)).status_code == 201

    assert _count(session, Publication) == before
    assert claim_due_publication(session, worker_id="w", lease_seconds=60) is None


def test_a_repeat_of_the_same_create_returns_the_project_and_writes_nothing(
    client, login, owner, session: Session
) -> None:
    csrf = login(owner)

    assert _create(client, csrf).status_code == 201
    audit_before = _count(session, AuditLog)

    again = _create(client, csrf, display_name="путевые ЗАМЕТКИ")

    assert again.status_code == 200
    assert again.json()["id"] == "travel-notes"
    assert _count(session, AuditLog) == audit_before
    assert (
        _count(
            session, ProjectMembership, ProjectMembership.project_id == "travel-notes"
        )
        == 1
    )


def test_a_taken_id_with_another_name_is_a_conflict(client, login, owner) -> None:
    csrf = login(owner)
    _create(client, csrf)

    response = _create(client, csrf, display_name="Совсем другое")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PROJECT_ALREADY_EXISTS"


def test_someone_elses_id_is_a_conflict_not_a_takeover(
    client, login, owner, make_user, make_membership, session: Session
) -> None:
    other = make_user()
    make_membership(user=other, project_id="test-project", role=MembershipRole.OWNER)
    _create(client, login(owner))

    response = _create(client, login(other))

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PROJECT_ALREADY_EXISTS"
    assert (
        _count(
            session,
            ProjectMembership,
            ProjectMembership.project_id == "travel-notes",
            ProjectMembership.user_id == other.id,
        )
        == 0
    )


def test_the_same_name_as_another_own_project_is_a_conflict(
    client, login, owner
) -> None:
    response = _create(client, login(owner), id="another", display_name="test project")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PROJECT_NAME_TAKEN"


@pytest.mark.parametrize(
    "role", [MembershipRole.VIEWER, MembershipRole.EDITOR, MembershipRole.ADMIN]
)
def test_only_an_owner_of_some_project_may_create(
    client, login, member, role, session: Session
) -> None:
    response = _create(client, login(member(role)))

    assert response.status_code == 403
    assert session.get(Project, "travel-notes") is None


def test_an_anonymous_caller_cannot_create(client) -> None:
    response = client.post("/api/v1/projects", json={"id": "abc", "display_name": "x"})

    assert response.status_code in (401, 403)


@pytest.mark.parametrize(
    "body",
    [
        {"id": "A-B"},
        {"id": "ab"},
        {"id": "-ab"},
        {"id": "ab-"},
        {"id": "a--b"},
        {"id": "../etc"},
        {"id": "new"},
        {"id": "x" * 65},
        {"display_name": "   "},
        {"default_timezone": "Mars/Olympus"},
        {"default_language": "russian"},
        {"topics": ["a"] * 11},
        {"topics": ["dup", "DUP"]},
        {"topics": ["x" * 81]},
        {"tone_of_voice": "x" * 4001},
        {"role": "viewer"},
        {"owner_user_id": "00000000-0000-0000-0000-000000000000"},
        {"knowledge_path": "/etc"},
        {"is_active": False},
        {"default_platform": "x"},
    ],
)
def test_invalid_input_is_refused_and_writes_nothing(
    client, login, owner, session: Session, body
) -> None:
    before = _count(session, Project)

    response = _create(client, login(owner), **body)

    assert response.status_code == 422, response.text
    assert _count(session, Project) == before


def test_a_failure_after_the_project_insert_leaves_nothing(
    client, login, owner, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ai_smm.application.projects as projects

    def boom(*args, **kwargs):
        raise RuntimeError("injected after the project and membership rows")

    monkeypatch.setattr(projects, "record_audit", boom)
    csrf = login(owner)

    with pytest.raises(RuntimeError):
        _create(client, csrf)

    assert session.get(Project, "travel-notes") is None
    assert (
        _count(
            session, ProjectMembership, ProjectMembership.project_id == "travel-notes"
        )
        == 0
    )
    assert session.get(ProjectSettings, "travel-notes") is None


def test_concurrent_creates_of_one_id_make_one_project(
    session: Session,
    session_factory: sessionmaker[Session],
    owner,
) -> None:
    from ai_smm.application.projects import ProjectInput, create_project
    from ai_smm.db.models import User

    barrier = threading.Barrier(2)
    results: list[str] = []

    def worker() -> None:
        with session_factory() as db:
            user = db.get(User, owner.id)
            barrier.wait()
            result = create_project(
                db,
                actor_user=user,
                data=ProjectInput(id="race-project", display_name="Race"),
            )
            db.commit()
            results.append("created" if result.created else "replayed")

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(results) == ["created", "replayed"]
    assert (
        _count(
            session, ProjectMembership, ProjectMembership.project_id == "race-project"
        )
        == 1
    )
    assert _count(session, AuditLog, AuditLog.subject == "project:race-project") == 1


def test_creation_is_refused_on_a_read_only_deployment(
    api_settings, session_factory, owner, session: Session
) -> None:
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from ai_smm.api.app import create_app
    from tests.conftest import TEST_PASSWORD

    app = create_app(
        replace(api_settings, api_mutations_enabled=False),
        session_factory=session_factory,
        access_log=False,
    )

    with TestClient(app) as read_only:
        csrf = read_only.post(
            "/api/v1/auth/login",
            json={"email": owner.email, "password": TEST_PASSWORD},
        ).json()["csrf_token"]

        response = read_only.post(
            "/api/v1/projects",
            json={"id": "travel-notes", "display_name": "x"},
            headers=_csrf(csrf),
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "MUTATIONS_DISABLED"
    session.expire_all()
    assert session.get(Project, "travel-notes") is None


# -- edit -------------------------------------------------------------------


def test_an_admin_edits_the_project_record_with_a_version(
    client, login, member, session: Session
) -> None:
    csrf = login(member(MembershipRole.OWNER))
    current = client.get("/api/v1/projects/test-project").json()

    response = client.patch(
        "/api/v1/projects/test-project",
        json={
            "expected_version": current["version"],
            "display_name": "Renamed",
            "description": "Новое описание",
            "default_timezone": "UTC",
        },
        headers=_csrf(csrf),
    )

    assert response.status_code == 200, response.text
    assert response.json()["version"] == current["version"] + 1
    assert response.json()["display_name"] == "Renamed"

    stale = client.patch(
        "/api/v1/projects/test-project",
        json={"expected_version": current["version"], "display_name": "Again"},
        headers=_csrf(csrf),
    )

    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "VERSION_CONFLICT"
    assert stale.json()["error"]["details"]["current_version"] == current["version"] + 1

    entry = session.scalars(
        select(AuditLog).where(AuditLog.action == "project.update")
    ).one()
    assert entry.details["changed"]["display_name"] == ["Test Project", "Renamed"]


@pytest.mark.parametrize(
    "role", [MembershipRole.VIEWER, MembershipRole.EDITOR, MembershipRole.ADMIN]
)
def test_below_owner_cannot_edit(client, login, member, role) -> None:
    csrf = login(member(role))

    response = client.patch(
        "/api/v1/projects/test-project",
        json={"expected_version": 1, "display_name": "x"},
        headers=_csrf(csrf),
    )

    assert response.status_code == 403


def test_a_non_member_gets_404_for_reads_and_edits(
    client, login, make_user, make_other_project
) -> None:
    csrf = login(make_user())

    assert client.get(f"/api/v1/projects/{make_other_project}").status_code == 404
    assert (
        client.patch(
            f"/api/v1/projects/{make_other_project}",
            json={"expected_version": 1, "display_name": "x"},
            headers=_csrf(csrf),
        ).status_code
        == 404
    )


@pytest.mark.parametrize(
    "body",
    [
        {"expected_version": 1, "id": "renamed"},
        {"expected_version": 1, "knowledge_path": "/x"},
        {"expected_version": 1, "is_active": False},
        {"expected_version": 1},
        {"expected_version": 1, "default_timezone": "Nowhere/City"},
    ],
)
def test_immutable_or_invalid_fields_are_refused(client, login, member, body) -> None:
    csrf = login(member(MembershipRole.OWNER))

    response = client.patch(
        "/api/v1/projects/test-project", json=body, headers=_csrf(csrf)
    )

    assert response.status_code == 422


# -- settings ---------------------------------------------------------------


def test_topics_and_tone_are_validated_and_other_keys_kept(
    client, login, member
) -> None:
    csrf = login(member(MembershipRole.OWNER))

    ok = client.patch(
        "/api/v1/projects/test-project/settings",
        json={
            "expected_version": 0,
            "content_config": {"topics": ["  ai  ", "ml"], "custom": 1},
            "brand_config": {"tone_of_voice": " Спокойно ", "legacy_key": "x"},
        },
        headers=_csrf(csrf),
    )

    assert ok.status_code == 200, ok.text
    assert ok.json()["content_config"] == {"topics": ["ai", "ml"], "custom": 1}
    assert ok.json()["brand_config"] == {"tone_of_voice": "Спокойно", "legacy_key": "x"}

    for bad in (
        {"content_config": {"topics": "not a list"}},
        {"content_config": {"topics": ["a", "A"]}},
        {"brand_config": {"tone_of_voice": 5}},
        {"brand_config": {"blob": "x" * 20000}},
    ):
        response = client.patch(
            "/api/v1/projects/test-project/settings",
            json={"expected_version": 1, **bad},
            headers=_csrf(csrf),
        )

        assert response.status_code == 422, bad


# -- the operator CLI --------------------------------------------------------


def test_the_cli_creates_a_project_for_an_existing_user(
    owner, settings, session: Session, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    from ai_smm import cli

    monkeypatch.setattr(cli, "get_settings", lambda: settings)

    assert (
        cli.main(
            [
                "project",
                "create",
                "--id",
                "cli-made",
                "--display-name",
                "CLI made",
                "--owner-email",
                owner.email,
            ]
        )
        == 0
    )

    session.expire_all()
    membership = session.scalars(
        select(ProjectMembership).where(ProjectMembership.project_id == "cli-made")
    ).one()

    assert membership.user_id == owner.id
    assert membership.role is MembershipRole.OWNER

    assert (
        cli.main(
            [
                "project",
                "create",
                "--id",
                "cli-made2",
                "--display-name",
                "x",
                "--owner-email",
                "nobody@example.com",
            ]
        )
        == 1
    )
    assert session.get(Project, "cli-made2") is None
