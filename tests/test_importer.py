"""Importing the legacy JSON queue must be safe to repeat."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from ai_smm.db.models import Publication, PublicationStatus
from ai_smm.importer import build_idempotency_key, import_json_queue


def _write_queue(tmp_path: Path, document: dict) -> Path:
    path = tmp_path / "queue.json"
    path.write_text(
        json.dumps(document, ensure_ascii=False), encoding="utf-8"
    )

    return path


@pytest.fixture
def queue_document() -> dict:
    return {
        "schema_version": 1,
        "project_id": "pilot",
        "platform": "threads",
        "approval": {
            "approved": True,
            "score": 9.0,
            "human_review_required": True,
        },
        "publications": [
            {
                "order": 1,
                "title": "Already live",
                "format": "carousel",
                "text": "Первый пост, уже опубликован.",
                "images": [
                    {"path": "a/one.jpg", "alt_text": "one"},
                    {"path": "a/two.jpg", "alt_text": "two"},
                ],
                "status": "published",
                "threads_post_id": "17956888254278916",
                "published_at": "2026-10-08T13:12:18+00:00",
            },
            {
                "order": 2,
                "title": "Waiting",
                "format": "image",
                "text": "Второй пост, ждёт публикации.",
                "images": [{"path": "a/three.jpg", "alt_text": "three"}],
                "status": "approved",
                "threads_post_id": None,
                "published_at": None,
            },
        ],
    }


def test_import_creates_rows(
    session: Session, tmp_path: Path, queue_document: dict
) -> None:
    path = _write_queue(tmp_path, queue_document)

    report = import_json_queue(session, queue_path=path)
    session.commit()

    assert len(report.created) == 2
    assert report.updated == []

    rows = {p.ordinal: p for p in session.query(Publication).all()}

    assert rows[1].status is PublicationStatus.PUBLISHED
    assert rows[1].threads_post_id == "17956888254278916"
    assert rows[1].published_at is not None

    assert rows[2].status is PublicationStatus.APPROVED
    assert rows[2].threads_post_id is None


def test_published_row_is_never_schedulable(
    session: Session, tmp_path: Path, queue_document: dict
) -> None:
    from ai_smm.queue import claim_due_publication

    path = _write_queue(tmp_path, queue_document)
    import_json_queue(session, queue_path=path)
    session.commit()

    assert claim_due_publication(
        session, worker_id="w1", lease_seconds=900
    ) is None


def test_import_is_idempotent(
    session: Session, tmp_path: Path, queue_document: dict
) -> None:
    path = _write_queue(tmp_path, queue_document)

    first = import_json_queue(session, queue_path=path)
    session.commit()

    second = import_json_queue(session, queue_path=path)
    session.commit()

    assert len(first.created) == 2
    assert second.created == []
    assert session.query(Publication).count() == 2


def test_rerun_does_not_revert_a_published_row(
    session: Session, tmp_path: Path, queue_document: dict
) -> None:
    """The database, not the file, is the source of truth after import."""

    path = _write_queue(tmp_path, queue_document)
    import_json_queue(session, queue_path=path)
    session.commit()

    # The worker published #2 after the file was written.
    row = (
        session.query(Publication).filter(Publication.ordinal == 2).one()
    )
    row.status = PublicationStatus.PUBLISHED
    row.threads_post_id = "17900000000000777"
    from datetime import datetime, timezone

    row.published_at = datetime.now(timezone.utc)
    session.commit()

    import_json_queue(session, queue_path=path)
    session.commit()
    session.refresh(row)

    assert row.status is PublicationStatus.PUBLISHED
    assert row.threads_post_id == "17900000000000777"


def test_source_file_is_not_modified(
    session: Session, tmp_path: Path, queue_document: dict
) -> None:
    path = _write_queue(tmp_path, queue_document)
    before = path.read_bytes()

    import_json_queue(session, queue_path=path)
    session.commit()

    assert path.read_bytes() == before


def test_mid_flight_status_becomes_needs_review(
    session: Session, tmp_path: Path, queue_document: dict
) -> None:
    """A file cannot tell us what Threads did with a 'publishing' record."""

    queue_document["publications"][1]["status"] = "publishing"
    path = _write_queue(tmp_path, queue_document)

    import_json_queue(session, queue_path=path)
    session.commit()

    row = (
        session.query(Publication).filter(Publication.ordinal == 2).one()
    )

    assert row.status is PublicationStatus.NEEDS_REVIEW


def test_published_without_timestamp_is_ambiguous(
    session: Session, tmp_path: Path, queue_document: dict
) -> None:
    queue_document["publications"][0]["published_at"] = None
    queue_document["publications"][0]["threads_post_id"] = None
    queue_document["publications"][0]["status"] = "published"
    path = _write_queue(tmp_path, queue_document)

    import_json_queue(session, queue_path=path)
    session.commit()

    row = (
        session.query(Publication).filter(Publication.ordinal == 1).one()
    )

    assert row.status is PublicationStatus.NEEDS_REVIEW


def test_naive_timestamp_is_skipped_not_guessed(
    session: Session, tmp_path: Path, queue_document: dict
) -> None:
    queue_document["publications"][1]["scheduled_at"] = "2026-11-01T10:00:00"
    path = _write_queue(tmp_path, queue_document)

    report = import_json_queue(session, queue_path=path)
    session.commit()

    assert len(report.created) == 1
    assert any("timezone" in s for s in report.skipped)


def test_human_review_required_keeps_review_flag_false(
    session: Session, tmp_path: Path, queue_document: dict
) -> None:
    path = _write_queue(tmp_path, queue_document)
    import_json_queue(session, queue_path=path)
    session.commit()

    rows = session.query(Publication).all()

    assert all(row.human_reviewed is False for row in rows)


def test_idempotency_key_changes_when_text_changes() -> None:
    first = build_idempotency_key(
        project_id="p", platform="threads", ordinal=1, body="original"
    )
    second = build_idempotency_key(
        project_id="p", platform="threads", ordinal=1, body="edited"
    )

    assert first != second
    assert first == build_idempotency_key(
        project_id="p", platform="threads", ordinal=1, body="original  "
    )


def test_real_pilot_queue_imports_if_present(
    session: Session,
) -> None:
    """Smoke test against the actual file shipped in the repo, if there."""

    path = (
        Path(__file__).resolve().parents[1]
        / "data/publications/ai-catalog-consultant.json"
    )

    if not path.is_file():
        pytest.skip("pilot queue file is not present in this checkout")

    report = import_json_queue(session, queue_path=path)
    session.commit()

    assert report.total >= 1
    assert session.query(Publication).count() == len(report.created)
