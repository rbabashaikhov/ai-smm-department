"""Import the legacy JSON queue into PostgreSQL.

Safe to run repeatedly: every record is matched on a deterministic
idempotency key, so a second run updates nothing it should not and never
creates a duplicate. The source file is only read, never modified.

A record that is already published in the JSON keeps its threads_post_id and
lands as published, which means the worker can never pick it up again.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_smm.db.models import Publication, PublicationStatus
from ai_smm.logging_setup import get_logger
from ai_smm.queue import ensure_project, record_audit, utcnow


logger = get_logger(__name__)

#: JSON format names mapped onto the schema's allowed formats.
FORMAT_ALIASES = {
    "text": "text",
    "single_post": "text",
    "image": "image",
    "carousel": "carousel",
    "thread": "thread",
}


@dataclass
class ImportReport:
    created: list[int] = field(default_factory=list)
    updated: list[int] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.created) + len(self.updated) + len(self.skipped)

    def as_dict(self) -> dict[str, Any]:
        return {
            "created": self.created,
            "updated": self.updated,
            "skipped": self.skipped,
        }


def build_idempotency_key(
    *,
    project_id: str,
    platform: str,
    ordinal: int,
    body: str,
) -> str:
    """Stable across reruns and sensitive to the text actually published.

    Including a digest of the body means an edited draft becomes a new
    logical publication instead of silently reusing the identity of the one
    that was reviewed.
    """

    digest = hashlib.sha256(body.strip().encode("utf-8")).hexdigest()[:16]

    return f"{project_id}:{platform}:{ordinal}:{digest}"


def _parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None

    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))

    if parsed.tzinfo is None:
        raise ValueError(
            f"Timestamp {value!r} has no timezone offset; refusing to guess."
        )

    return parsed.astimezone(timezone.utc)


def _normalize_format(raw: str | None, image_count: int) -> str:
    if raw:
        normalized = FORMAT_ALIASES.get(raw.strip().lower())

        if normalized:
            return normalized

        raise ValueError(f"Unsupported format: {raw!r}")

    if image_count > 1:
        return "carousel"

    if image_count == 1:
        return "image"

    return "text"


def _map_status(entry: dict[str, Any]) -> PublicationStatus:
    """Map a JSON status onto the schema lifecycle, conservatively.

    Anything that was mid-flight or unclear in the file becomes
    needs_review: the file cannot tell us what Threads actually did.
    """

    raw = str(entry.get("status") or "").strip().lower()
    has_post_id = bool(entry.get("threads_post_id"))

    if has_post_id:
        return PublicationStatus.PUBLISHED

    mapping = {
        "approved": PublicationStatus.APPROVED,
        "draft": PublicationStatus.DRAFT,
        "ready": PublicationStatus.DRAFT,
        "cancelled": PublicationStatus.CANCELLED,
        "canceled": PublicationStatus.CANCELLED,
        "failed": PublicationStatus.FAILED,
        "needs_review": PublicationStatus.NEEDS_REVIEW,
        "publishing": PublicationStatus.NEEDS_REVIEW,
        "published": PublicationStatus.NEEDS_REVIEW,
    }

    return mapping.get(raw, PublicationStatus.DRAFT)


def import_json_queue(
    session: Session,
    *,
    queue_path: Path,
    actor: str = "importer",
    project_display_name: str | None = None,
    knowledge_path: str | None = None,
) -> ImportReport:
    document = json.loads(queue_path.read_text(encoding="utf-8"))

    project_id = document.get("project_id")

    if not project_id:
        raise ValueError(f"{queue_path} has no project_id.")

    platform = document.get("platform") or "threads"
    entries = document.get("publications")

    if not isinstance(entries, list):
        raise ValueError(f"{queue_path} has no publications list.")

    approval = document.get("approval") or {}
    approved_document = approval.get("approved") is True
    editor_score = approval.get("score")
    human_review_required = bool(approval.get("human_review_required"))

    ensure_project(
        session,
        project_id=project_id,
        display_name=project_display_name or project_id,
        knowledge_path=knowledge_path or f"knowledge/projects/{project_id}",
        default_platform=platform,
    )

    report = ImportReport()

    for entry in entries:
        ordinal = entry.get("order")

        if not isinstance(ordinal, int):
            report.skipped.append(
                f"entry without integer 'order': {entry.get('title')!r}"
            )
            continue

        body = str(entry.get("text") or "").strip()

        if not body:
            report.skipped.append(f"order {ordinal}: empty text")
            continue

        images = entry.get("images") or []

        if not isinstance(images, list):
            report.skipped.append(f"order {ordinal}: images is not a list")
            continue

        try:
            publication_format = _normalize_format(
                entry.get("format"), len(images)
            )
            published_at = _parse_timestamp(entry.get("published_at"))
            scheduled_at = _parse_timestamp(entry.get("scheduled_at"))
        except ValueError as exc:
            report.skipped.append(f"order {ordinal}: {exc}")
            continue

        key = build_idempotency_key(
            project_id=project_id,
            platform=platform,
            ordinal=ordinal,
            body=body,
        )

        existing = session.execute(
            select(Publication).where(
                Publication.idempotency_key == key
            )
        ).scalar_one_or_none()

        status = _map_status(entry)
        threads_post_id = entry.get("threads_post_id") or None

        if status is PublicationStatus.PUBLISHED and published_at is None:
            # The check constraint requires both; a published row without a
            # timestamp would be rejected, so treat it as ambiguous instead.
            status = PublicationStatus.NEEDS_REVIEW

        if existing is not None:
            # Never move a row backwards out of a terminal or manual state:
            # the database, not the file, is the source of truth now.
            if existing.status in {
                PublicationStatus.PUBLISHED,
                PublicationStatus.CANCELLED,
                PublicationStatus.NEEDS_REVIEW,
                PublicationStatus.CLAIMED,
                PublicationStatus.PUBLISHING,
                PublicationStatus.SCHEDULED,
            }:
                report.skipped.append(
                    f"order {ordinal}: already in database with status "
                    f"{existing.status.value}"
                )
                continue

            existing.title = str(entry.get("title") or existing.title)
            existing.format = publication_format
            existing.images = images
            existing.items = entry.get("items") or []
            existing.source_ref = str(queue_path)
            existing.updated_at = utcnow()

            report.updated.append(existing.id)
            continue

        publication = Publication(
            project_id=project_id,
            platform=platform,
            ordinal=ordinal,
            title=str(entry.get("title") or f"Publication {ordinal}"),
            format=publication_format,
            body=body,
            images=images,
            items=entry.get("items") or [],
            status=status,
            scheduled_at=scheduled_at,
            idempotency_key=key,
            threads_post_id=threads_post_id,
            published_at=published_at,
            editor_score=editor_score if approved_document else None,
            human_reviewed=(
                approved_document and not human_review_required
            ),
            source_ref=str(queue_path),
            last_error=(
                entry.get("last_error")
                if status is PublicationStatus.NEEDS_REVIEW
                else None
            ),
        )

        session.add(publication)
        session.flush()

        report.created.append(publication.id)

    record_audit(
        session,
        actor=actor,
        action="json_import",
        subject=f"project:{project_id}",
        details={
            "source": str(queue_path),
            **report.as_dict(),
        },
    )

    logger.info(
        "Imported JSON queue",
        extra={
            "context": {
                "source": str(queue_path),
                "project_id": project_id,
                "created": len(report.created),
                "updated": len(report.updated),
                "skipped": len(report.skipped),
            }
        },
    )

    return report
