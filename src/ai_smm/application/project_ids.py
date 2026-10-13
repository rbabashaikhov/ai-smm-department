"""The project identifier rule, in one place and with no dependencies.

A project id is the URL segment, the knowledge directory name, the prefix
of publication idempotency keys and the audit subject. It is chosen once
and never changed, and since it can now come from an HTTP request it must
be safe as a path segment: lower-case letters, digits and single hyphens,
3 to 64 characters, not starting or ending with a hyphen.
"""
from __future__ import annotations

import re


PROJECT_ID_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,62}[a-z0-9])$")

#: Ids that would collide with a UI route or read as something else.
RESERVED_PROJECT_IDS = frozenset(
    {"new", "settings", "api", "admin", "projects"}
)


def is_valid_project_id(value: str, *, allow_reserved: bool = False) -> bool:
    if not isinstance(value, str) or not PROJECT_ID_PATTERN.match(value):
        return False

    if "--" in value:
        return False

    return allow_reserved or value not in RESERVED_PROJECT_IDS


def require_valid_project_id(value: str) -> str:
    """Raise ValueError for an id that is not safe to create."""

    if not is_valid_project_id(value):
        raise ValueError(
            "Project id must be 3-64 characters of a-z, 0-9 and single "
            "hyphens, must not start or end with a hyphen, and must not be "
            f"one of: {', '.join(sorted(RESERVED_PROJECT_IDS))}."
        )

    return value
