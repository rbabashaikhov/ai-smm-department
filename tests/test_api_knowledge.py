"""GET /projects/{id}/knowledge[/{name}]: read-only, isolated (SMM-034A)."""
from __future__ import annotations

from pathlib import Path

import pytest

from ai_smm.config import Settings
from ai_smm.db.models import MembershipRole


@pytest.fixture
def knowledge(api_settings: Settings, project: str, make_other_project) -> Path:
    root = api_settings.knowledge_root
    own = root / "projects" / project
    own.mkdir(parents=True)
    (own / "overview.md").write_text("# Обзор\nТекст.\n", encoding="utf-8")
    (own / "project.yaml").write_text("id: test-project\n", encoding="utf-8")
    (own / "photo.jpg").write_bytes(b"\xff\xd8")

    other = root / "projects" / make_other_project
    other.mkdir(parents=True)
    (other / "secret.md").write_text("other only", encoding="utf-8")

    (root / "editorial_policy.md").write_text("policy", encoding="utf-8")

    return root


@pytest.mark.parametrize(
    "role",
    [
        MembershipRole.VIEWER,
        MembershipRole.EDITOR,
        MembershipRole.ADMIN,
        MembershipRole.OWNER,
    ],
)
def test_every_member_lists_the_documents(
    client, login, member, project: str, knowledge: Path, role
) -> None:
    login(member(role))

    response = client.get(f"/api/v1/projects/{project}/knowledge")

    assert response.status_code == 200

    body = response.json()

    assert body["project_id"] == project
    assert body["available"] is True
    assert body["writable"] is False
    assert [(d["name"], d["kind"]) for d in body["documents"]] == [
        ("overview.md", "markdown"),
        ("project.yaml", "yaml"),
    ]


def test_a_member_reads_one_document(
    client, login, member, project: str, knowledge: Path
) -> None:
    login(member(MembershipRole.VIEWER))

    response = client.get(f"/api/v1/projects/{project}/knowledge/overview.md")

    assert response.status_code == 200

    body = response.json()

    assert body["name"] == "overview.md"
    assert body["content"] == "# Обзор\nТекст.\n"
    assert len(body["sha256"]) == 64


def test_knowledge_requires_authentication(
    client, project: str, knowledge: Path
) -> None:
    assert client.get(f"/api/v1/projects/{project}/knowledge").status_code == 401
    assert (
        client.get(f"/api/v1/projects/{project}/knowledge/overview.md").status_code
        == 401
    )


def test_a_member_of_one_project_cannot_read_another_projects_knowledge(
    client, login, member, make_other_project, knowledge: Path
) -> None:
    login(member(MembershipRole.OWNER))

    listing = client.get(f"/api/v1/projects/{make_other_project}/knowledge")
    document = client.get(
        f"/api/v1/projects/{make_other_project}/knowledge/secret.md"
    )

    assert listing.status_code == 404
    assert document.status_code == 404
    assert "other only" not in document.text


@pytest.mark.parametrize(
    "path",
    [
        "..%2Fother-project%2Fsecret.md",
        "%2E%2E%2Fother-project%2Fsecret.md",
        "..%2F..%2Feditorial_policy.md",
        "..%5Cother-project%5Csecret.md",
        "%2Fetc%2Fpasswd",
        "photo.jpg",
        "missing.md",
        ".hidden.md",
    ],
)
def test_a_document_name_cannot_leave_the_project(
    client, login, member, project: str, knowledge: Path, path: str
) -> None:
    login(member(MembershipRole.OWNER))

    response = client.get(f"/api/v1/projects/{project}/knowledge/{path}")

    assert response.status_code == 404
    assert "other only" not in response.text
    assert "policy" not in response.text


def test_without_a_knowledge_directory_the_list_is_unavailable(
    client, login, member, project: str
) -> None:
    """The production Control Center has no knowledge mount today."""

    login(member(MembershipRole.VIEWER))

    body = client.get(f"/api/v1/projects/{project}/knowledge").json()

    assert body == {
        "project_id": project,
        "available": False,
        "writable": False,
        "documents": [],
    }


@pytest.mark.parametrize("method", ["put", "patch", "post", "delete"])
def test_there_is_no_write_route(
    client, login, member, project: str, knowledge: Path, method: str
) -> None:
    csrf = login(member(MembershipRole.OWNER))

    response = client.request(
        method.upper(),
        f"/api/v1/projects/{project}/knowledge/overview.md",
        json={"content": "overwritten"},
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 405
    assert (
        knowledge / "projects" / project / "overview.md"
    ).read_text(encoding="utf-8") == "# Обзор\nТекст.\n"


def test_an_inaccessible_directory_is_unavailable_not_a_500(
    client, login, member, project: str, knowledge: Path
) -> None:
    import os

    if os.geteuid() == 0:
        pytest.skip("permission bits do not bind root")

    own = knowledge / "projects" / project
    login(member(MembershipRole.VIEWER))
    own.chmod(0)

    try:
        listing = client.get(f"/api/v1/projects/{project}/knowledge")
        document = client.get(f"/api/v1/projects/{project}/knowledge/overview.md")
    finally:
        own.chmod(0o755)

    assert listing.status_code == 200
    assert listing.json()["available"] is False
    assert document.status_code == 404
    assert str(knowledge) not in listing.text + document.text


def test_a_mode_000_document_is_404_not_a_500(
    client, login, member, project: str, knowledge: Path
) -> None:
    import os

    if os.geteuid() == 0:
        pytest.skip("permission bits do not bind root")

    path = knowledge / "projects" / project / "overview.md"
    login(member(MembershipRole.VIEWER))
    path.chmod(0)

    try:
        response = client.get(f"/api/v1/projects/{project}/knowledge/overview.md")
    finally:
        path.chmod(0o644)

    assert response.status_code == 404
    assert str(knowledge) not in response.text
