from pathlib import Path
from typing import Any

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[3]
KNOWLEDGE_ROOT = PROJECT_ROOT / "knowledge" / "projects"


def load_project_knowledge(project_id: str) -> dict[str, Any]:
    project_dir = KNOWLEDGE_ROOT / project_id

    if not project_dir.exists():
        raise FileNotFoundError(
            f"Knowledge directory not found: {project_dir}"
        )

    result: dict[str, Any] = {
        "project_id": project_id,
        "project_dir": str(project_dir),
        "files": {},
        "assets": [],
    }

    for path in sorted(project_dir.iterdir()):
        if path.is_file():
            if path.suffix in {".md", ".txt"}:
                result["files"][path.name] = path.read_text(
                    encoding="utf-8"
                )

            elif path.suffix in {".yaml", ".yml"}:
                result["files"][path.name] = yaml.safe_load(
                    path.read_text(encoding="utf-8")
                )

    assets_dir = project_dir / "assets"

    if assets_dir.exists():
        result["assets"] = [
            str(path)
            for path in sorted(assets_dir.iterdir())
            if path.is_file()
            and path.suffix.lower()
            in {".png", ".jpg", ".jpeg", ".webp"}
        ]

    return result