"""Configuration and knowledge-base resolution."""
from __future__ import annotations

from pathlib import Path

import pytest

from ai_smm.config import load_settings


def test_dry_run_defaults_to_true(monkeypatch: pytest.MonkeyPatch) -> None:
    """The safe default: a fresh deployment cannot publish by accident."""

    monkeypatch.delenv("AI_SMM_DRY_RUN", raising=False)

    assert load_settings().dry_run is True


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("true", True),
        ("1", True),
        ("yes", True),
        ("false", False),
        ("0", False),
        ("off", False),
    ],
)
def test_dry_run_parsing(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: bool
) -> None:
    monkeypatch.setenv("AI_SMM_DRY_RUN", value)

    assert load_settings().dry_run is expected


def test_invalid_boolean_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typo must not silently become "publish for real"."""

    monkeypatch.setenv("AI_SMM_DRY_RUN", "maybe")

    with pytest.raises(ValueError, match="must be a boolean"):
        load_settings()


def test_display_timezone_defaults_to_moscow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AI_SMM_DISPLAY_TZ", raising=False)

    settings = load_settings()

    assert settings.display_tz_name == "Europe/Moscow"
    assert settings.display_tz is not None


def test_missing_database_url_raises_only_when_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AI_SMM_DATABASE_URL", raising=False)

    settings = load_settings()

    assert settings.database_url is None

    with pytest.raises(RuntimeError, match="AI_SMM_DATABASE_URL"):
        settings.require_database_url()


def test_knowledge_root_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AI_SMM_KNOWLEDGE_ROOT", str(tmp_path))

    assert load_settings().knowledge_root == tmp_path


def test_loader_uses_the_env_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """In the container the package lives in site-packages, so parents[3]
    cannot be used to find the knowledge base."""

    from ai_smm.knowledge.loader import knowledge_root, load_project_knowledge

    monkeypatch.setenv("AI_SMM_KNOWLEDGE_ROOT", str(tmp_path))

    assert knowledge_root() == tmp_path

    (tmp_path / "editorial_policy.md").write_text(
        "Политика.", encoding="utf-8"
    )
    project_dir = tmp_path / "projects" / "demo"
    project_dir.mkdir(parents=True)
    (project_dir / "overview.md").write_text("Обзор.", encoding="utf-8")

    knowledge = load_project_knowledge("demo")

    assert knowledge["project_id"] == "demo"
    assert knowledge["editorial_policy"] == "Политика."
    assert "overview.md" in knowledge["files"]


def test_loader_fails_without_editorial_policy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from ai_smm.knowledge.loader import load_project_knowledge

    monkeypatch.setenv("AI_SMM_KNOWLEDGE_ROOT", str(tmp_path))

    (tmp_path / "projects" / "demo").mkdir(parents=True)

    with pytest.raises(FileNotFoundError, match="editorial policy"):
        load_project_knowledge("demo")


# --- storage defaults ----------------------------------------------------


def test_storage_requires_explicit_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_smm.storage import StorageUploader

    for name in (
        "STORAGE_SSH_HOST",
        "STORAGE_SSH_USER",
        "STORAGE_REMOTE_ROOT",
        "STORAGE_PUBLIC_BASE_URL",
        "STORAGE_SSH_KEY_PATH",
        "STORAGE_SSH_PASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ValueError, match="STORAGE_SSH_HOST"):
        StorageUploader()


def test_storage_requires_remote_root_and_public_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression: a stale default used to point at a dead host."""

    from ai_smm.storage import StorageUploader

    monkeypatch.setenv("STORAGE_SSH_HOST", "storage.example.test")
    monkeypatch.setenv("STORAGE_SSH_USER", "deploy")
    monkeypatch.setenv("STORAGE_SSH_PASSWORD", "irrelevant")
    monkeypatch.delenv("STORAGE_SSH_KEY_PATH", raising=False)
    monkeypatch.delenv("STORAGE_REMOTE_ROOT", raising=False)
    monkeypatch.delenv("STORAGE_PUBLIC_BASE_URL", raising=False)

    with pytest.raises(ValueError, match="STORAGE_REMOTE_ROOT"):
        StorageUploader()

    monkeypatch.setenv("STORAGE_REMOTE_ROOT", "/srv/storage")

    with pytest.raises(ValueError, match="STORAGE_PUBLIC_BASE_URL"):
        StorageUploader()


def test_storage_rejects_plain_http_public_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_smm.storage import StorageUploader

    monkeypatch.setenv("STORAGE_SSH_HOST", "storage.example.test")
    monkeypatch.setenv("STORAGE_SSH_USER", "deploy")
    monkeypatch.setenv("STORAGE_SSH_PASSWORD", "irrelevant")
    monkeypatch.delenv("STORAGE_SSH_KEY_PATH", raising=False)
    monkeypatch.setenv("STORAGE_REMOTE_ROOT", "/srv/storage")
    monkeypatch.setenv("STORAGE_PUBLIC_BASE_URL", "http://insecure.test")

    with pytest.raises(ValueError, match="https"):
        StorageUploader()


def test_no_stale_infrastructure_defaults_remain() -> None:
    """Guards against the decommissioned host creeping back in."""

    source = (
        Path(__file__).resolve().parents[1] / "src/ai_smm/storage.py"
    ).read_text(encoding="utf-8")

    assert "files.apps.leadmeter.ru" not in source
    assert "/srv/miniapps/file-storage" not in source


def test_media_root_defaults_to_the_repository_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AI_SMM_MEDIA_ROOT", raising=False)

    from ai_smm.config import PROJECT_ROOT

    assert load_settings().media_root == PROJECT_ROOT


def test_media_root_override_resolves_container_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The container layout: knowledge is mounted outside the source tree.

    Stored image paths are repo-relative ("knowledge/projects/..."), so the
    media root must be the parent of the knowledge mount, not /app.
    """

    from ai_smm.db.models import Publication, PublicationStatus
    from ai_smm.publishing.service import _resolve_image_paths

    media_root = tmp_path / "srv" / "ai-smm"
    assets = media_root / "knowledge" / "projects" / "demo" / "assets"
    assets.mkdir(parents=True)
    (assets / "pic.jpg").write_bytes(b"\xff\xd8data")

    monkeypatch.setenv("AI_SMM_MEDIA_ROOT", str(media_root))

    assert load_settings().media_root == media_root

    publication = Publication(
        project_id="demo",
        ordinal=1,
        title="t",
        format="image",
        body="b",
        images=[
            {
                "path": "knowledge/projects/demo/assets/pic.jpg",
                "alt_text": "pic",
            }
        ],
        items=[],
        status=PublicationStatus.CLAIMED,
        idempotency_key="k",
        human_reviewed=True,
    )

    resolved = _resolve_image_paths(publication, media_root=media_root)

    assert len(resolved) == 1
    assert Path(resolved[0]["path"]).is_file()
