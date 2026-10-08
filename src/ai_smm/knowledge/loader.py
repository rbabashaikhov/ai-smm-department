
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[3]

KNOWLEDGE_ROOT = PROJECT_ROOT / "knowledge"

PROJECTS_ROOT = KNOWLEDGE_ROOT / "projects"

EDITORIAL_POLICY_PATH = (
    KNOWLEDGE_ROOT / "editorial_policy.md"
)


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

    project_dir = PROJECTS_ROOT / project_id

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
    if not EDITORIAL_POLICY_PATH.is_file():
        raise FileNotFoundError(
            "Global editorial policy not found: "
            f"{EDITORIAL_POLICY_PATH}"
        )

    result["editorial_policy"] = (
        EDITORIAL_POLICY_PATH.read_text(
            encoding="utf-8"
        )
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
