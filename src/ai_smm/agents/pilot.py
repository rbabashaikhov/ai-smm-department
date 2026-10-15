"""Rules of the pilot series that every agent has to agree on.

The pilot (AI Catalog Consultant) publishes three posts, each carrying real
screenshots of the project. A Threads reply carries text only, so the pilot
cannot be a reply thread: with reply_thread the Copywriter is required to
format every part as carousel while the continuity check forbids media in
replies, and no draft can satisfy both. The strategy is therefore resolved
here, once, and Strategist, Copywriter and Editor all ask this function
rather than each deciding for itself.
"""
from __future__ import annotations


PILOT_PROJECT_ID = "ai-catalog-consultant"

#: Every pilot post carries media, which only a standalone post can.
PILOT_PUBLISHING_STRATEGY = "standalone_series"

PUBLISHING_STRATEGIES = frozenset({"standalone_series", "reply_thread"})

PILOT_STRATEGY_REASON = (
    "Пилотная серия: каждая часть публикуется со скриншотами проекта, "
    "а ответы в Threads не могут нести изображения, поэтому только "
    "standalone_series."
)


def resolve_publishing_strategy(project_id: str, strategy: str | None) -> str:
    """The strategy the series is actually published with."""

    if project_id == PILOT_PROJECT_ID:
        return PILOT_PUBLISHING_STRATEGY

    if strategy in PUBLISHING_STRATEGIES:
        return strategy

    return "standalone_series"
