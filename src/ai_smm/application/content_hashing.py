"""Canonical form and SHA-256 of a deliverable content snapshot.

Pure functions with no project imports, split out of
ai_smm.application.content so that a process which only needs the hash
(the generation worker) does not load that module's publishing and
credential dependencies. ai_smm.application.content re-exports both
names; the algorithm and its output are unchanged.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any


def canonical_content(
    *,
    title: str | None,
    body: str,
    format: str,
    images: list[Any],
    items: list[Any],
    social_account_id: uuid.UUID | None = None,
) -> str:
    """The canonical UTF-8 JSON text hashed for approval lineage.

    Covers exactly what would be published, and where. metadata, source
    and the editor's notes are about the revision, not part of the post,
    and are left out so that two revisions which would publish the same
    thing have the same hash. The destination account is included only
    when one is set, so every hash computed before accounts existed stays
    valid.
    """

    snapshot: dict[str, Any] = {
        "title": title,
        "body": body,
        "format": format,
        "images": images,
        "items": items,
    }

    if social_account_id is not None:
        snapshot["social_account_id"] = str(social_account_id)

    return json.dumps(
        snapshot,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def content_hash(
    *,
    title: str | None,
    body: str,
    format: str,
    images: list[Any],
    items: list[Any],
    social_account_id: uuid.UUID | None = None,
) -> str:
    """SHA-256 of the deliverable snapshot's canonical UTF-8 JSON."""

    canonical = canonical_content(
        title=title,
        body=body,
        format=format,
        images=images,
        items=items,
        social_account_id=social_account_id,
    )

    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
