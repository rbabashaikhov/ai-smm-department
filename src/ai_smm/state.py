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
    #: How well the parts read as one series, separate from per-post score.
    editor_narrative_score: float
    editor_issues: list[str]

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

    # Series planning (inputs to Strategist)
    series_size: int
    publishing_strategy: str