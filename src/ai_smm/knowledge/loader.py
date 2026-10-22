
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from ai_smm.application.project_ids import is_valid_project_id


PROJECT_ROOT = Path(__file__).resolve().parents[3]

#: Default location when the package is used from a source checkout.
DEFAULT_KNOWLEDGE_ROOT = PROJECT_ROOT / "knowledge"


def knowledge_root() -> Path:
    """Where the knowledge base lives.

    Resolved at call time, not import time: inside the container the package
    is installed under site-packages, so parents[3] points nowhere useful and
    AI_SMM_KNOWLEDGE_ROOT supplies the real (read-only mounted) path.
    """

    override = os.getenv("AI_SMM_KNOWLEDGE_ROOT")

    if override and override.strip():
        return Path(override.strip()).expanduser()

    return DEFAULT_KNOWLEDGE_ROOT


# Kept for backwards compatibility with existing scripts and imports.
KNOWLEDGE_ROOT = DEFAULT_KNOWLEDGE_ROOT

PROJECTS_ROOT = DEFAULT_KNOWLEDGE_ROOT / "projects"

EDITORIAL_POLICY_PATH = DEFAULT_KNOWLEDGE_ROOT / "editorial_policy.md"


def load_project_knowledge(
    project_id: str,
    *,
    allow_missing: bool = False,
) -> dict[str, Any]:
    """
    Load project-specific knowledge and global editorial policy.

    Project knowledge:
        knowledge/projects/<project_id>/

    Global editorial policy:
        knowledge/editorial_policy.md

    Media assets:
        knowledge/projects/<project_id>/assets/

    The editorial policy is shared across all projects.

    allow_missing: a project without its own directory gets no files and
    no assets instead of FileNotFoundError -- never another project's
    directory. The editorial policy is still required.
    """

    # The id is a path segment. Ids can come from an HTTP request since
    # SMM-025B, so it is checked here too, and the resolved directory must
    # stay inside knowledge/projects/: one project never reads another's.
    if not is_valid_project_id(project_id, allow_reserved=True):
        raise ValueError(f"Invalid project id: {project_id!r}")

    root = knowledge_root()

    projects_root = (root / "projects").resolve()
    project_dir = root / "projects" / project_id

    if project_dir.resolve().parent != projects_root:
        raise ValueError(
            f"Knowledge directory of {project_id!r} escapes {projects_root}."
        )

    editorial_policy_path = root / "editorial_policy.md"

    project_dir_exists = project_dir.is_dir()

    if not project_dir_exists and not allow_missing:
        raise FileNotFoundError(
            f"Knowledge directory not found: {project_dir}"
        )

    result: dict[str, Any] = {
        "project_id": project_id,
        "project_dir": str(project_dir),
        "editorial_policy": "",
        "files": {},
        "assets": [],
    }

    # Load global editorial policy.
    # Fail early rather than silently run without it.
    if not editorial_policy_path.is_file():
        raise FileNotFoundError(
            "Global editorial policy not found: "
            f"{editorial_policy_path}"
        )

    result["editorial_policy"] = editorial_policy_path.read_text(
        encoding="utf-8"
    )

    if not result["editorial_policy"].strip():
        raise ValueError(
            "Global editorial policy is empty."
        )

    if not project_dir_exists:
        return result

    # Load project-specific Markdown, text and YAML.
    for path in sorted(project_dir.iterdir()):
        if not path.is_file():
            continue

        suffix = path.suffix.lower()

        if suffix in {".md", ".txt"}:
            result["files"][path.name] = (
                path.read_text(encoding="utf-8")
            )

        elif suffix in {".yaml", ".yml"}:
            result["files"][path.name] = yaml.safe_load(
                path.read_text(encoding="utf-8")
            )

    # Load existing media asset paths.
    assets_dir = project_dir / "assets"

    if assets_dir.is_dir():
        result["assets"] = [
            str(path.resolve())
            for path in sorted(assets_dir.iterdir())
            if path.is_file()
            and path.suffix.lower()
            in {".png", ".jpg", ".jpeg", ".webp"}
        ]

    return result
