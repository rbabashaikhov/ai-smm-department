
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml


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
    """

    root = knowledge_root()

    project_dir = root / "projects" / project_id

    editorial_policy_path = root / "editorial_policy.md"

    if not project_dir.is_dir():
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
