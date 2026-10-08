from typing import Any, TypedDict


class SMMState(TypedDict, total=False):
    # Input
    project_id: str

    # Knowledge loaded from knowledge/projects/<project_id>
    knowledge: dict[str, Any]

    # Strategist output
    content_plan: dict[str, Any]

    # Copywriter output
    draft: dict[str, Any]

    # Editor output
    editor_feedback: str
    editor_score: float
    editor_approved: bool

    # Revision control
    revision_count: int
    max_revisions: int

    # Publisher output
    publication: dict[str, Any]

    # Debug / execution metadata
    current_agent: str
    errors: list[str]

    # Test / debug
    force_bad_draft: bool
    bad_draft_injected: bool

    # Publishing
    publish_live: bool