"""The knowledge document view: containment and loader compatibility.

No database: these are the filesystem rules under the read-only
Knowledge Base API (SMM-034A).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ai_smm.knowledge.documents import (
    MAX_DOCUMENT_BYTES,
    DocumentNotFound,
    DocumentTooLarge,
    KnowledgeUnavailable,
    list_documents,
    read_document,
)
from ai_smm.knowledge.loader import DEFAULT_KNOWLEDGE_ROOT, load_project_knowledge


@pytest.fixture
def root(tmp_path: Path) -> Path:
    project = tmp_path / "projects" / "alpha"
    project.mkdir(parents=True)
    (project / "overview.md").write_text("# Alpha\n", encoding="utf-8")
    (project / "project.yaml").write_text("id: alpha\n", encoding="utf-8")
    (project / "notes.txt").write_text("plain", encoding="utf-8")
    (project / "photo.jpg").write_bytes(b"\xff\xd8")
    (project / "assets").mkdir()
    (project / "assets" / "README.md").write_text("nested", encoding="utf-8")

    other = tmp_path / "projects" / "beta"
    other.mkdir()
    (other / "secret.md").write_text("beta only", encoding="utf-8")

    (tmp_path / "editorial_policy.md").write_text("policy", encoding="utf-8")

    return tmp_path


def test_lists_the_top_level_text_documents_only(root: Path) -> None:
    names = [(d.name, d.kind) for d in list_documents(root, "alpha")]

    assert names == [
        ("notes.txt", "text"),
        ("overview.md", "markdown"),
        ("project.yaml", "yaml"),
    ]


def test_reads_a_document_with_its_digest(root: Path) -> None:
    document = read_document(root, "alpha", "overview.md")

    assert document.content == "# Alpha\n"
    assert document.info.size_bytes == len("# Alpha\n")
    assert len(document.sha256) == 64


@pytest.mark.parametrize(
    "name",
    [
        "../beta/secret.md",
        "..",
        ".",
        "../../editorial_policy.md",
        "..%2Fbeta%2Fsecret.md",
        "assets/README.md",
        "/etc/passwd",
        ".hidden.md",
        "photo.jpg",
        "missing.md",
        "overview.md\x00.txt",
        "",
    ],
)
def test_refuses_names_outside_the_project_documents(
    root: Path, name: str
) -> None:
    with pytest.raises(DocumentNotFound):
        read_document(root, "alpha", name)


@pytest.mark.parametrize("project_id", ["../projects/beta", "..", "Alpha!", ""])
def test_refuses_project_ids_that_are_not_one_segment(
    root: Path, project_id: str
) -> None:
    with pytest.raises(KnowledgeUnavailable):
        list_documents(root, project_id)

    with pytest.raises(DocumentNotFound):
        read_document(root, project_id, "overview.md")


def test_a_symlink_to_another_project_is_neither_listed_nor_read(
    root: Path,
) -> None:
    link = root / "projects" / "alpha" / "stolen.md"
    link.symlink_to(root / "projects" / "beta" / "secret.md")

    assert "stolen.md" not in [d.name for d in list_documents(root, "alpha")]

    with pytest.raises(DocumentNotFound):
        read_document(root, "alpha", "stolen.md")


def test_a_symlinked_project_directory_is_unavailable(root: Path) -> None:
    (root / "projects" / "gamma").symlink_to(root / "projects" / "beta")

    # Resolves to beta, whose parent is still projects/ -- but it is
    # beta's directory, so the name must match too.
    with pytest.raises(KnowledgeUnavailable):
        list_documents(root, "gamma")


def test_a_project_without_a_directory_is_unavailable(root: Path) -> None:
    with pytest.raises(KnowledgeUnavailable):
        list_documents(root, "delta")


def test_a_missing_root_is_unavailable(tmp_path: Path) -> None:
    with pytest.raises(KnowledgeUnavailable):
        list_documents(tmp_path / "nowhere", "alpha")


def test_an_oversized_document_is_not_served(root: Path) -> None:
    (root / "projects" / "alpha" / "big.md").write_bytes(
        b"x" * (MAX_DOCUMENT_BYTES + 1)
    )

    with pytest.raises(DocumentTooLarge):
        read_document(root, "alpha", "big.md")


def test_a_non_utf8_document_is_not_served(root: Path) -> None:
    (root / "projects" / "alpha" / "latin1.md").write_bytes(b"caf\xe9")

    with pytest.raises(DocumentNotFound):
        read_document(root, "alpha", "latin1.md")


def test_the_view_shows_what_the_agents_loader_reads(root: Path, monkeypatch) -> None:
    monkeypatch.setenv("AI_SMM_KNOWLEDGE_ROOT", str(root))

    loaded = load_project_knowledge("alpha")

    assert sorted(d.name for d in list_documents(root, "alpha")) == sorted(
        loaded["files"]
    )

    for name, value in loaded["files"].items():
        if isinstance(value, str):
            assert read_document(root, "alpha", name).content == value


def test_the_repository_knowledge_matches_the_loader(monkeypatch) -> None:
    """The real project in knowledge/: the same files, the same text."""

    monkeypatch.delenv("AI_SMM_KNOWLEDGE_ROOT", raising=False)
    project_id = "ai-catalog-consultant"
    loaded = load_project_knowledge(project_id)

    listed = list_documents(DEFAULT_KNOWLEDGE_ROOT, project_id)

    assert sorted(d.name for d in listed) == sorted(loaded["files"])


@pytest.fixture
def unreadable(root: Path):
    """chmod 000 is a no-op for root; skip there."""

    import os

    if os.geteuid() == 0:
        pytest.skip("permission bits do not bind root")

    restore: list[tuple[Path, int]] = []

    def lock(path: Path) -> None:
        restore.append((path, path.stat().st_mode))
        path.chmod(0)

    yield lock

    for path, mode in reversed(restore):
        path.chmod(mode)


def test_an_inaccessible_project_directory_is_unavailable(
    root: Path, unreadable
) -> None:
    unreadable(root / "projects" / "alpha")

    with pytest.raises(KnowledgeUnavailable) as caught:
        list_documents(root, "alpha")

    assert str(root) not in str(caught.value)

    with pytest.raises(DocumentNotFound):
        read_document(root, "alpha", "overview.md")


def test_an_inaccessible_projects_root_is_unavailable(
    root: Path, unreadable
) -> None:
    unreadable(root / "projects")

    with pytest.raises(KnowledgeUnavailable):
        list_documents(root, "alpha")


def test_a_mode_000_document_is_not_found(root: Path, unreadable) -> None:
    unreadable(root / "projects" / "alpha" / "overview.md")

    with pytest.raises(DocumentNotFound) as caught:
        read_document(root, "alpha", "overview.md")

    assert str(root) not in str(caught.value)
    assert read_document(root, "alpha", "notes.txt").content == "plain"
