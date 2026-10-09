"""CLI gates: nothing publishes implicitly, ambiguity needs a human."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.orm import Session, sessionmaker

from ai_smm import cli
from ai_smm.config import Settings
from ai_smm.db.models import Publication, PublicationStatus


@pytest.fixture(autouse=True)
def bind_cli_session(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    import ai_smm.db.session as session_module

    monkeypatch.setattr(
        session_module,
        "get_session_factory",
        lambda _settings=None: session_factory,
    )


def _run(argv: list[str], settings: Settings) -> int:
    parser = cli.build_parser()
    args = parser.parse_args(argv)

    return int(args.func(args, settings))


def test_queue_lists_rows(
    session: Session, settings: Settings, make_publication, capsys
) -> None:
    make_publication()

    assert _run(["queue"], settings) == 0

    out = capsys.readouterr().out

    assert "Test publication 1" in out
    assert settings.display_tz_name in out


def test_queue_filters_by_status(
    session: Session, settings: Settings, make_publication, capsys
) -> None:
    make_publication(status=PublicationStatus.DRAFT)

    assert _run(["queue", "--status", "needs_review"], settings) == 0

    assert "empty" in capsys.readouterr().out


def test_errors_lists_needs_review(
    session: Session, settings: Settings, make_publication, capsys
) -> None:
    make_publication(
        status=PublicationStatus.NEEDS_REVIEW,
        last_error="No response from Threads during publish",
    )

    assert _run(["errors"], settings) == 0

    out = capsys.readouterr().out

    assert "needs_review" in out
    assert "reconcile" in out


def test_schedule_interprets_naive_time_in_display_timezone(
    session: Session, settings: Settings, make_publication
) -> None:
    """A naive '2026-11-01 12:00' is Moscow time, not UTC."""

    publication = make_publication(status=PublicationStatus.FAILED)

    assert (
        _run(
            ["schedule", str(publication.id), "--at", "2026-11-01T12:00"],
            settings,
        )
        == 0
    )

    session.expire_all()
    stored = session.get(Publication, publication.id)

    assert stored is not None
    assert stored.scheduled_at == datetime(
        2026, 11, 1, 12, 0, tzinfo=ZoneInfo("Europe/Moscow")
    )
    assert stored.status is PublicationStatus.SCHEDULED


def test_schedule_accepts_explicit_offset(
    session: Session, settings: Settings, make_publication
) -> None:
    publication = make_publication(status=PublicationStatus.FAILED)

    _run(
        [
            "schedule",
            str(publication.id),
            "--at",
            "2026-11-01T12:00:00+00:00",
        ],
        settings,
    )

    session.expire_all()
    stored = session.get(Publication, publication.id)

    assert stored is not None
    assert stored.scheduled_at.astimezone(timezone.utc).hour == 12


def test_schedule_refuses_a_needs_review_row(
    session: Session, settings: Settings, make_publication
) -> None:
    publication = make_publication(status=PublicationStatus.NEEDS_REVIEW)

    assert (
        _run(["schedule", str(publication.id), "--at", "now"], settings) == 1
    )

    session.expire_all()
    stored = session.get(Publication, publication.id)

    assert stored is not None
    assert stored.status is PublicationStatus.NEEDS_REVIEW


def test_reconcile_retry_requires_explicit_confirmation(
    session: Session, settings: Settings, make_publication
) -> None:
    """The guard against re-sending a possibly live post."""

    publication = make_publication(status=PublicationStatus.NEEDS_REVIEW)

    assert (
        _run(["reconcile", str(publication.id), "--retry"], settings) == 2
    )

    session.expire_all()
    stored = session.get(Publication, publication.id)

    assert stored is not None
    assert stored.status is PublicationStatus.NEEDS_REVIEW


def test_reconcile_retry_with_confirmation_reschedules(
    session: Session, settings: Settings, make_publication
) -> None:
    publication = make_publication(status=PublicationStatus.NEEDS_REVIEW)

    assert (
        _run(
            [
                "reconcile",
                str(publication.id),
                "--retry",
                "--confirm-not-published",
            ],
            settings,
        )
        == 0
    )

    session.expire_all()
    stored = session.get(Publication, publication.id)

    assert stored is not None
    assert stored.status is PublicationStatus.SCHEDULED


def test_reconcile_published_closes_the_record(
    session: Session, settings: Settings, make_publication
) -> None:
    publication = make_publication(status=PublicationStatus.NEEDS_REVIEW)

    assert (
        _run(
            [
                "reconcile",
                str(publication.id),
                "--published",
                "17900000000000900",
            ],
            settings,
        )
        == 0
    )

    session.expire_all()
    stored = session.get(Publication, publication.id)

    assert stored is not None
    assert stored.status is PublicationStatus.PUBLISHED
    assert stored.threads_post_id == "17900000000000900"


def test_reconcile_refuses_a_row_that_is_not_under_review(
    session: Session, settings: Settings, make_publication
) -> None:
    publication = make_publication(status=PublicationStatus.SCHEDULED)

    assert (
        _run(
            ["reconcile", str(publication.id), "--published", "1"], settings
        )
        == 1
    )


def test_reconcile_requires_a_mode(
    session: Session, settings: Settings, make_publication
) -> None:
    publication = make_publication(status=PublicationStatus.NEEDS_REVIEW)

    assert _run(["reconcile", str(publication.id)], settings) == 2


def test_publish_now_without_live_is_a_preview(
    session: Session, settings: Settings, make_publication, capsys
) -> None:
    publication = make_publication()

    assert _run(["publish-now", str(publication.id)], settings) == 0

    out = capsys.readouterr().out

    assert "Preview only" in out
    assert "nothing was sent" in out.lower()

    session.expire_all()
    stored = session.get(Publication, publication.id)

    assert stored is not None
    assert stored.status is PublicationStatus.SCHEDULED
    assert stored.attempt_count == 0


def test_publish_now_live_requires_confirm_reviewed(
    session: Session, settings: Settings, make_publication
) -> None:
    publication = make_publication()

    assert (
        _run(["publish-now", str(publication.id), "--live"], settings) == 2
    )

    session.expire_all()
    stored = session.get(Publication, publication.id)

    assert stored is not None
    assert stored.status is PublicationStatus.SCHEDULED


def test_approve_sets_the_review_flag(
    session: Session, settings: Settings, make_publication
) -> None:
    publication = make_publication(
        status=PublicationStatus.DRAFT, human_reviewed=False
    )

    assert _run(["approve", str(publication.id)], settings) == 0

    session.expire_all()
    stored = session.get(Publication, publication.id)

    assert stored is not None
    assert stored.human_reviewed is True
    assert stored.status is PublicationStatus.APPROVED


def test_show_prints_attempts(
    session: Session, settings: Settings, make_publication, capsys
) -> None:
    publication = make_publication()

    assert _run(["show", str(publication.id)], settings) == 0

    out = capsys.readouterr().out

    assert "idempotency_key" in out
    assert publication.body in out


def test_unknown_publication_returns_error(
    session: Session, settings: Settings
) -> None:
    assert _run(["show", "999999"], settings) == 1


def test_recover_reports_actions(
    session: Session, settings: Settings, make_publication, capsys
) -> None:
    make_publication(
        status=PublicationStatus.PUBLISHING,
        lease_expires_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )

    assert _run(["recover"], settings) == 0

    out = capsys.readouterr().out

    assert "quarantined to needs_review" in out


def test_stats_reports_dry_run_mode(
    session: Session, settings: Settings, capsys
) -> None:
    assert _run(["stats"], settings) == 0

    out = capsys.readouterr().out

    assert "dry_run mode" in out
    assert "Europe/Moscow" in out


def test_import_json_does_not_touch_the_file(
    session: Session, settings: Settings, tmp_path: Path, capsys
) -> None:
    import json

    path = tmp_path / "q.json"
    document = {
        "project_id": "cli-project",
        "platform": "threads",
        "approval": {"approved": True, "score": 8.0},
        "publications": [
            {
                "order": 1,
                "title": "t",
                "format": "text",
                "text": "Текст публикации.",
                "images": [],
                "status": "approved",
            }
        ],
    }
    path.write_text(json.dumps(document), encoding="utf-8")
    before = path.read_bytes()

    assert _run(["import-json", str(path)], settings) == 0
    assert path.read_bytes() == before

    out = capsys.readouterr().out

    assert "left unchanged" in out
