"""Read-only view of one project's knowledge documents (SMM-034A).

The documents are exactly the files the agents' loader reads
(ai_smm.knowledge.loader.load_project_knowledge): the Markdown, text and
YAML files directly inside knowledge/projects/<project_id>/. Nothing in a
sub-directory, nothing of another project, not the shared editorial
policy.

There is no write path. In production the knowledge base is a host
directory mounted read-only into the worker; the Control Center has no
writable storage for it (docs/architecture/smm-034a-project-knowledge-base.md).
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ai_smm.application.project_ids import is_valid_project_id


#: The suffixes the loader reads, and what each one is.
DOCUMENT_KINDS: dict[str, str] = {
    ".md": "markdown",
    ".txt": "text",
    ".yaml": "yaml",
    ".yml": "yaml",
}

#: One path segment: no separator, no leading dot, so never "." or "..".
_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")

#: A knowledge document is a short text; anything larger is not served.
MAX_DOCUMENT_BYTES = 256 * 1024


class KnowledgeUnavailable(Exception):
    """This deployment has no knowledge directory for the project."""


class DocumentNotFound(Exception):
    """No readable document of that name in the project's knowledge."""


class DocumentTooLarge(Exception):
    pass


@dataclass(frozen=True)
class DocumentInfo:
    name: str
    kind: str
    size_bytes: int
    updated_at: datetime


@dataclass(frozen=True)
class Document:
    info: DocumentInfo
    content: str
    sha256: str


def _project_dir(root: Path, project_id: str) -> Path:
    try:
        return _resolve_project_dir(root, project_id)
    except OSError:
        # PermissionError and friends: the directory exists but this process
        # cannot use it. Same answer as a missing one; the host path stays
        # out of the exception.
        raise KnowledgeUnavailable(project_id) from None


def _resolve_project_dir(root: Path, project_id: str) -> Path:
    # The same containment rule as the loader: the id is one segment and
    # the resolved directory sits directly in <root>/projects.
    if not is_valid_project_id(project_id, allow_reserved=True):
        raise KnowledgeUnavailable(project_id)

    # A symlinked directory could stand for another project.
    projects_root = (root / "projects").resolve()
    candidate = root / "projects" / project_id
    project_dir = candidate.resolve()

    if (
        candidate.is_symlink()
        or project_dir.parent != projects_root
        or project_dir.name != project_id
        or not project_dir.is_dir()
    ):
        raise KnowledgeUnavailable(project_id)

    return project_dir


def _document_path(project_dir: Path, name: str) -> Path | None:
    """The file of a valid document name, or None.

    A symlink is never followed: it could point at another project, the
    editorial policy or anything else the process can read.
    """

    if not _NAME_PATTERN.fullmatch(name):
        return None

    if Path(name).suffix.lower() not in DOCUMENT_KINDS:
        return None

    path = project_dir / name

    try:
        if path.is_symlink() or not path.is_file():
            return None

        if path.resolve().parent != project_dir:
            return None
    except OSError:
        return None

    return path


def _info(path: Path) -> DocumentInfo:
    stat = path.stat()

    return DocumentInfo(
        name=path.name,
        kind=DOCUMENT_KINDS[path.suffix.lower()],
        size_bytes=stat.st_size,
        updated_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
    )


def list_documents(root: Path, project_id: str) -> list[DocumentInfo]:
    project_dir = _project_dir(root, project_id)

    try:
        entries = sorted(project_dir.iterdir())
        found = [
            path
            for entry in entries
            if (path := _document_path(project_dir, entry.name)) is not None
        ]
        return [_info(path) for path in found]
    except OSError:
        raise KnowledgeUnavailable(project_id) from None


def read_document(root: Path, project_id: str, name: str) -> Document:
    try:
        project_dir = _project_dir(root, project_id)
    except KnowledgeUnavailable as exc:
        raise DocumentNotFound(name) from exc

    path = _document_path(project_dir, name)

    if path is None:
        raise DocumentNotFound(name)

    try:
        info = _info(path)

        if info.size_bytes > MAX_DOCUMENT_BYTES:
            raise DocumentTooLarge(name)

        raw = path.read_bytes()
    except OSError:
        # Unreadable (mode 000, vanished): as if it were not there.
        raise DocumentNotFound(name) from None

    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        # The loader would fail on it too; it is not a text document.
        raise DocumentNotFound(name) from exc

    return Document(
        info=info, content=content, sha256=hashlib.sha256(raw).hexdigest()
    )
