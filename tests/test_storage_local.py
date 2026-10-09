"""Local storage mode: the worker writes into one bind-mounted directory.

The point of these tests is containment and atomicity. The worker shares a
host with other projects whose media sits next to ours under the same public
base URL, so "cannot write outside its own prefix" has to be a property of
the code, not of the mount alone.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from ai_smm.storage import MODE_LOCAL, MODE_SFTP, StorageUploader


PUBLIC_BASE = "https://media.example.test"
PREFIX = "ai-smm/threads"


@pytest.fixture
def storage_root(tmp_path: Path) -> Path:
    root = tmp_path / "mounted-threads-dir"
    root.mkdir()

    return root


@pytest.fixture
def local_env(
    monkeypatch: pytest.MonkeyPatch, storage_root: Path
) -> None:
    for name in (
        "STORAGE_SSH_HOST",
        "STORAGE_SSH_USER",
        "STORAGE_SSH_KEY_PATH",
        "STORAGE_SSH_PASSWORD",
        "STORAGE_REMOTE_ROOT",
    ):
        monkeypatch.delenv(name, raising=False)

    monkeypatch.setenv("STORAGE_MODE", MODE_LOCAL)
    monkeypatch.setenv("STORAGE_LOCAL_ROOT", str(storage_root))
    monkeypatch.setenv("STORAGE_LOCAL_PREFIX", PREFIX)
    monkeypatch.setenv("STORAGE_PUBLIC_BASE_URL", PUBLIC_BASE)


# --- configuration -------------------------------------------------------


def test_sftp_remains_the_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Existing .env files and scripts must keep working untouched."""

    monkeypatch.delenv("STORAGE_MODE", raising=False)
    monkeypatch.setenv("STORAGE_SSH_HOST", "storage.example.test")
    monkeypatch.setenv("STORAGE_SSH_USER", "deploy")
    monkeypatch.setenv("STORAGE_SSH_PASSWORD", "irrelevant")
    monkeypatch.delenv("STORAGE_SSH_KEY_PATH", raising=False)
    monkeypatch.setenv("STORAGE_REMOTE_ROOT", "/srv/file-storage")
    monkeypatch.setenv("STORAGE_PUBLIC_BASE_URL", PUBLIC_BASE)

    uploader = StorageUploader()

    assert uploader.mode == MODE_SFTP


def test_local_mode_does_not_require_ssh_settings(local_env: None) -> None:
    """No SSH host, user, key or password: that is the whole point."""

    uploader = StorageUploader()

    assert uploader.mode == MODE_LOCAL
    assert uploader.ssh_host == ""
    assert uploader.ssh_key_path is None


def test_local_mode_requires_a_root(
    local_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("STORAGE_LOCAL_ROOT", raising=False)

    with pytest.raises(ValueError, match="STORAGE_LOCAL_ROOT"):
        StorageUploader()


def test_local_mode_requires_a_prefix(
    local_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STORAGE_LOCAL_PREFIX", "")

    with pytest.raises(ValueError, match="STORAGE_LOCAL_PREFIX"):
        StorageUploader()


def test_missing_root_directory_fails_loudly(
    local_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A mount that did not happen must not look like a successful upload."""

    monkeypatch.setenv("STORAGE_LOCAL_ROOT", str(tmp_path / "absent"))

    with pytest.raises(FileNotFoundError, match="not a directory"):
        StorageUploader()


def test_unwritable_root_fails_loudly(
    local_env: None, storage_root: Path
) -> None:
    if os.geteuid() == 0:
        pytest.skip("root bypasses directory permissions")

    storage_root.chmod(0o555)

    try:
        with pytest.raises(PermissionError, match="not writable"):
            StorageUploader()
    finally:
        storage_root.chmod(0o755)


def test_unknown_mode_is_rejected(
    local_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STORAGE_MODE", "ftp")

    with pytest.raises(ValueError, match="STORAGE_MODE"):
        StorageUploader()


def test_plain_http_public_url_is_rejected_in_local_mode(
    local_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STORAGE_PUBLIC_BASE_URL", "http://insecure.test")

    with pytest.raises(ValueError, match="https"):
        StorageUploader()


# --- writing -------------------------------------------------------------


def test_upload_bytes_writes_a_world_readable_file(
    local_env: None, storage_root: Path
) -> None:
    uploader = StorageUploader()

    url = uploader.upload_bytes(
        b"\xff\xd8jpeg-bytes",
        f"{PREFIX}/pic.jpg",
        verify_public_url=False,
    )

    written = storage_root / "pic.jpg"

    assert url == f"{PUBLIC_BASE}/{PREFIX}/pic.jpg"
    assert written.read_bytes() == b"\xff\xd8jpeg-bytes"
    # The web server runs as a different user and must be able to read it.
    assert written.stat().st_mode & 0o777 == 0o644


def test_upload_file_writes_the_same_content(
    local_env: None, storage_root: Path, tmp_path: Path
) -> None:
    source = tmp_path / "source.jpg"
    source.write_bytes(b"\xff\xd8source")

    uploader = StorageUploader()

    url = uploader.upload(
        local_path=source,
        remote_path=f"{PREFIX}/copied.jpg",
        verify_public_url=False,
    )

    assert url == f"{PUBLIC_BASE}/{PREFIX}/copied.jpg"
    assert (storage_root / "copied.jpg").read_bytes() == b"\xff\xd8source"


def test_nested_path_under_the_prefix_is_allowed(
    local_env: None, storage_root: Path
) -> None:
    uploader = StorageUploader()

    uploader.upload_bytes(
        b"x", f"{PREFIX}/2026/11/pic.jpg", verify_public_url=False
    )

    assert (storage_root / "2026" / "11" / "pic.jpg").is_file()


def test_no_partial_file_is_left_behind(
    local_env: None, storage_root: Path
) -> None:
    """Meta fetches the URL within seconds; a half-written file is a bug."""

    uploader = StorageUploader()

    uploader.upload_bytes(
        b"\xff\xd8complete", f"{PREFIX}/pic.jpg", verify_public_url=False
    )

    leftovers = [
        p.name
        for p in storage_root.iterdir()
        if p.name.startswith(".") or p.name.endswith(".part")
    ]

    assert leftovers == []


def test_overwrite_replaces_content_atomically(
    local_env: None, storage_root: Path
) -> None:
    uploader = StorageUploader()

    uploader.upload_bytes(
        b"first", f"{PREFIX}/pic.jpg", verify_public_url=False
    )
    uploader.upload_bytes(
        b"second", f"{PREFIX}/pic.jpg", verify_public_url=False
    )

    assert (storage_root / "pic.jpg").read_bytes() == b"second"
    assert len(list(storage_root.iterdir())) == 1


def test_empty_content_is_refused(
    local_env: None, storage_root: Path
) -> None:
    uploader = StorageUploader()

    with pytest.raises(ValueError, match="empty"):
        uploader.upload_bytes(b"", f"{PREFIX}/pic.jpg")

    assert list(storage_root.iterdir()) == []


# --- containment ---------------------------------------------------------


@pytest.mark.parametrize(
    "remote_path",
    [
        # Another project's media, served from the same public base URL.
        "other-project-a/pic.jpg",
        "other-project-b/pic.jpg",
        # A sibling prefix of our own project.
        "ai-smm/ai-catalog-consultant/pic.jpg",
        "ai-smm/pic.jpg",
        # The prefix itself, naming no file.
        "ai-smm/threads",
        # A prefix that merely starts with the same characters.
        "ai-smm/threads-other/pic.jpg",
    ],
)
def test_paths_outside_the_prefix_are_refused(
    local_env: None, storage_root: Path, remote_path: str
) -> None:
    uploader = StorageUploader()

    with pytest.raises(ValueError):
        uploader.upload_bytes(b"x", remote_path, verify_public_url=False)

    assert list(storage_root.iterdir()) == []


@pytest.mark.parametrize(
    "remote_path",
    [
        "ai-smm/threads/../../escape.jpg",
        "../escape.jpg",
        "ai-smm/threads/../other-project-a/pic.jpg",
    ],
)
def test_traversal_is_refused(
    local_env: None, storage_root: Path, remote_path: str
) -> None:
    uploader = StorageUploader()

    with pytest.raises(ValueError):
        uploader.upload_bytes(b"x", remote_path, verify_public_url=False)


def test_symlink_inside_the_directory_cannot_escape(
    local_env: None, storage_root: Path, tmp_path: Path
) -> None:
    """resolve() follows links, so the containment check runs after it."""

    outside = tmp_path / "outside"
    outside.mkdir()

    (storage_root / "escape").symlink_to(outside)

    uploader = StorageUploader()

    with pytest.raises(ValueError, match="resolves outside"):
        uploader.upload_bytes(
            b"x", f"{PREFIX}/escape/pic.jpg", verify_public_url=False
        )

    assert list(outside.iterdir()) == []


def test_public_url_is_independent_of_mode(
    local_env: None, storage_root: Path
) -> None:
    """Callers must not need to know which mode is active."""

    uploader = StorageUploader()

    assert (
        uploader.build_public_url(f"{PREFIX}/pic.jpg")
        == f"{PUBLIC_BASE}/{PREFIX}/pic.jpg"
    )


# --- integration with the Threads client ---------------------------------


def test_threads_publisher_uploads_through_local_mode(
    local_env: None, storage_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """upload_image() must reach the mounted directory, unchanged API."""

    from PIL import Image

    from ai_smm.publishing.threads import ThreadsPublisher

    monkeypatch.setenv("THREADS_ACCESS_TOKEN", "test-token-value")
    monkeypatch.setenv("THREADS_STORAGE_PREFIX", PREFIX)

    source = storage_root.parent / "input.jpg"
    Image.new("RGB", (1080, 1080), "navy").save(source, "JPEG")

    publisher = ThreadsPublisher()

    # The HTTPS verification is the one thing a unit test cannot do.
    monkeypatch.setattr(
        "ai_smm.storage.StorageUploader.verify_url",
        lambda self, url: None,
    )

    url = publisher.upload_image(source)

    assert url.startswith(f"{PUBLIC_BASE}/{PREFIX}/")

    name = url.rsplit("/", 1)[-1]
    published = storage_root / name

    assert published.is_file()
    assert published.stat().st_mode & 0o777 == 0o644
    # The unique name is what keeps Meta's cache from serving a stale image.
    assert name.endswith("-input.jpg")
    assert len(name) > len("input.jpg")
