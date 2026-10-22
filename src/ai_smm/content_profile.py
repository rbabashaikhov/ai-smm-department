"""The project content profile a Generation Job carries (SMM-035B2).

A generation job is created by the API, which can read project_settings;
it is run by the generation worker, which has no table access at all
(ai_smm.generation_worker.check_readiness). So the API copies the
project's editorial profile into job.params["content_profile"] when the
job is created, and the worker and agents use only that copy: a later
settings change never reaches a job that already exists, and the worker
role needs no new privilege.

This module is pure (no database, no LLM): the API builds the snapshot
with it, and the worker validates what it finds in params with it before
anything reaches a prompt. A job without the key is a legacy job (created
before SMM-035B2) and keeps the behaviour it had.
"""
from __future__ import annotations

import json
import re
from typing import Any


#: Key in content_generation_jobs.params.
PROFILE_PARAM = "content_profile"

PROFILE_SCHEMA_VERSION = 1

#: Free-text fields, from content_config / brand_config.
PROFILE_TEXT_FIELDS = ("main_theme", "audience", "tone_of_voice", "guidelines")

#: List-of-strings fields, from content_config / brand_config.
PROFILE_LIST_FIELDS = ("topics", "excluded_topics", "goals", "forbidden_terms")

#: Every profile field, in the order the agents show them.
PROFILE_FIELDS = (
    "main_theme",
    "topics",
    "excluded_topics",
    "audience",
    "goals",
    "tone_of_voice",
    "guidelines",
    "forbidden_terms",
    "default_language",
)

_CONTENT_CONFIG_FIELDS = (
    "main_theme", "topics", "excluded_topics", "audience", "goals",
)
_BRAND_CONFIG_FIELDS = ("tone_of_voice", "guidelines", "forbidden_terms")

#: Mirrors ai_smm.application.projects.normalize_language.
_LANGUAGE_RE = re.compile(r"^[a-z]{2}(-[A-Z]{2})?$")

#: Bounds for what the worker accepts; the API's own limits are tighter
#: (docs/api.md), these only stop a malformed row from reaching a prompt.
_MAX_TEXT_LENGTH = 4000
_MAX_LIST_ITEMS = 20
_MAX_LIST_ITEM_LENGTH = 200
_MAX_SNAPSHOT_BYTES = 32 * 1024


def build_profile_snapshot(
    *,
    content_config: dict[str, Any],
    brand_config: dict[str, Any],
    default_language: str,
    settings_version: int | None,
) -> dict[str, Any]:
    """The snapshot of already validated settings; absent fields are empty."""

    snapshot: dict[str, Any] = {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "settings_version": settings_version,
    }

    for section, fields in (
        (content_config, _CONTENT_CONFIG_FIELDS),
        (brand_config, _BRAND_CONFIG_FIELDS),
    ):
        for field in fields:
            empty: Any = [] if field in PROFILE_LIST_FIELDS else ""
            snapshot[field] = section.get(field, empty)

    snapshot["default_language"] = default_language

    return validate_profile_snapshot(snapshot)


def validate_profile_snapshot(raw: Any) -> dict[str, Any]:
    """A clean copy of a stored snapshot, or ValueError naming the problem.

    Unknown keys are dropped rather than passed on: only the fields named
    here ever reach an agent.
    """

    if not isinstance(raw, dict):
        raise ValueError("content_profile must be an object")

    if raw.get("schema_version") != PROFILE_SCHEMA_VERSION:
        raise ValueError(
            f"content_profile.schema_version must be {PROFILE_SCHEMA_VERSION}"
        )

    settings_version = raw.get("settings_version")
    if settings_version is not None and (
        isinstance(settings_version, bool)
        or not isinstance(settings_version, int)
        or settings_version < 1
    ):
        raise ValueError(
            "content_profile.settings_version must be a positive integer or null"
        )

    clean: dict[str, Any] = {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "settings_version": settings_version,
    }

    for field in PROFILE_TEXT_FIELDS:
        value = raw.get(field, "")
        if not isinstance(value, str) or len(value) > _MAX_TEXT_LENGTH:
            raise ValueError(
                f"content_profile.{field} must be a string of at most "
                f"{_MAX_TEXT_LENGTH} characters"
            )
        clean[field] = value.strip()

    for field in PROFILE_LIST_FIELDS:
        value = raw.get(field, [])
        if (
            not isinstance(value, list)
            or len(value) > _MAX_LIST_ITEMS
            or not all(
                isinstance(item, str)
                and item.strip()
                and len(item) <= _MAX_LIST_ITEM_LENGTH
                for item in value
            )
        ):
            raise ValueError(
                f"content_profile.{field} must be a list of at most "
                f"{_MAX_LIST_ITEMS} non-empty strings"
            )
        clean[field] = [item.strip() for item in value]

    language = raw.get("default_language")
    if not isinstance(language, str) or not _LANGUAGE_RE.match(language):
        raise ValueError(
            "content_profile.default_language must look like 'ru' or 'en-US'"
        )
    clean["default_language"] = language

    size = len(json.dumps(clean, ensure_ascii=False).encode("utf-8"))
    if size > _MAX_SNAPSHOT_BYTES:
        raise ValueError("content_profile is too large")

    return clean
