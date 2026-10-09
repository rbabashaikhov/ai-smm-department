"""Worker behaviour: restart recovery, database outages, shutdown."""
from __future__ import annotations

import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from ai_smm.config import Settings
from ai_smm.db.models import AuditLog, Publication, PublicationStatus
from ai_smm.db.session import with_db_retry
from ai_smm.worker import Worker
from tests.test_publish_service import FakePublisher


@pytest.fixture
def worker_factory(
    settings: Settings,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A Worker wired to the test session factory and a fake publisher."""

    def factory(
        *,
        publisher: FakePublisher | None = None,
        worker_settings: Settings | None = None,
    ) -> tuple[Worker, FakePublisher]:
        publisher = publisher or FakePublisher()
        effective = worker_settings or settings

        import ai_smm.db.session as session_module

        monkeypatch.setattr(
            session_module,
            "get_session_factory",
            lambda _settings=None: session_factory,
        )

        worker = Worker(
            settings=effective,
            publisher_factory=lambda: publisher,
            media_root=tmp_path,
            heartbeat_path=tmp_path / "heartbeat",
        )

        return worker, publisher

    return factory


def test_cycle_publishes_a_due_row(
    session: Session, make_publication, worker_factory
) -> None:
    publication = make_publication()

    worker, publisher = worker_factory()
    results = worker.run_cycle()

    session.expire_all()
    stored = session.get(Publication, publication.id)

    assert results == ["published"]
    assert publisher.publish_calls == ["text"]
    assert stored is not None
    assert stored.status is PublicationStatus.PUBLISHED


def test_cycle_is_a_no_op_when_nothing_is_due(
    session: Session, make_publication, worker_factory
) -> None:
    make_publication(
        scheduled_at=datetime.now(timezone.utc) + timedelta(hours=2)
    )

    worker, publisher = worker_factory()

    assert worker.run_cycle() == []
    assert publisher.publish_calls == []


def test_dry_run_cycle_sends_nothing(
    session: Session, settings: Settings, make_publication, worker_factory
) -> None:
    publication = make_publication()

    worker, publisher = worker_factory(
        worker_settings=replace(settings, dry_run=True)
    )
    results = worker.run_cycle()

    session.expire_all()
    stored = session.get(Publication, publication.id)

    assert results == ["dry_run"]
    assert publisher.publish_calls == []
    assert stored is not None
    assert stored.status is PublicationStatus.SCHEDULED
    assert stored.threads_post_id is None


def test_restart_quarantines_a_crashed_publish(
    session: Session, make_publication, worker_factory
) -> None:
    """Simulates SIGKILL during publishing, then a restart."""

    publication = make_publication(
        status=PublicationStatus.PUBLISHING,
        claimed_by="old-worker",
        lease_expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )

    worker, publisher = worker_factory()
    worker.recover_on_start()

    session.expire_all()
    stored = session.get(Publication, publication.id)

    assert stored is not None
    assert stored.status is PublicationStatus.NEEDS_REVIEW

    # And the next cycle must not touch it.
    assert worker.run_cycle() == []
    assert publisher.publish_calls == []


def test_restart_releases_an_expired_claim(
    session: Session, make_publication, worker_factory
) -> None:
    """A claim with no external call made is safely retried."""

    publication = make_publication(
        status=PublicationStatus.CLAIMED,
        claimed_by="old-worker",
        lease_expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )

    worker, publisher = worker_factory()
    worker.recover_on_start()
    results = worker.run_cycle()

    session.expire_all()
    stored = session.get(Publication, publication.id)

    assert results == ["published"]
    assert stored is not None
    assert stored.status is PublicationStatus.PUBLISHED


def test_restart_is_recorded_in_the_audit_log(
    session: Session, worker_factory
) -> None:
    worker, _ = worker_factory()
    worker.recover_on_start()

    session.expire_all()
    actions = [row.action for row in session.query(AuditLog).all()]

    assert "worker_started" in actions


def test_published_state_survives_a_restart(
    session: Session, make_publication, worker_factory
) -> None:
    """Persistence check: a restart must not change a completed row."""

    publication = make_publication()

    worker, _ = worker_factory()
    worker.run_cycle()

    session.expire_all()
    first = session.get(Publication, publication.id)
    assert first is not None
    post_id = first.threads_post_id
    published_at = first.published_at

    # A new Worker object stands in for a restarted process.
    worker2, publisher2 = worker_factory()
    worker2.recover_on_start()
    worker2.run_cycle()

    session.expire_all()
    second = session.get(Publication, publication.id)

    assert second is not None
    assert second.status is PublicationStatus.PUBLISHED
    assert second.threads_post_id == post_id
    assert second.published_at == published_at
    assert publisher2.publish_calls == []


def test_cycle_failure_does_not_propagate(
    session: Session, make_publication, worker_factory, monkeypatch
) -> None:
    """A scheduler thread must survive an unexpected error."""

    worker, _ = worker_factory()

    monkeypatch.setattr(
        worker, "run_cycle", lambda: (_ for _ in ()).throw(RuntimeError("x"))
    )

    worker.run_cycle_safely()

    assert (worker.heartbeat_path).is_file()


def test_rate_limit_blocks_a_too_soon_publish(
    session: Session, settings: Settings, make_publication, worker_factory
) -> None:
    make_publication(
        status=PublicationStatus.PUBLISHED,
        threads_post_id="17900000000000500",
        published_at=datetime.now(timezone.utc) - timedelta(seconds=30),
    )
    make_publication()

    worker, publisher = worker_factory(
        worker_settings=replace(
            settings, min_publish_interval_seconds=600
        )
    )

    assert worker.run_cycle() == []
    assert publisher.publish_calls == []


def test_daily_limit_blocks_further_publishing(
    session: Session, settings: Settings, make_publication, worker_factory
) -> None:
    make_publication(
        status=PublicationStatus.PUBLISHED,
        threads_post_id="17900000000000501",
        published_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    make_publication()

    worker, publisher = worker_factory(
        worker_settings=replace(settings, daily_publish_limit=1)
    )

    assert worker.run_cycle() == []
    assert publisher.publish_calls == []


def test_stop_event_prevents_new_cycles(
    session: Session, make_publication, worker_factory
) -> None:
    make_publication()

    worker, publisher = worker_factory()
    worker._stop_event.set()
    worker.run_cycle_safely()

    assert publisher.publish_calls == []


def test_shutdown_waits_for_an_in_flight_cycle(
    worker_factory,
) -> None:
    """SIGTERM must not cut a publish short."""

    worker, _ = worker_factory()

    released = threading.Event()
    worker._cycle_lock.acquire()

    def release_later() -> None:
        released.wait(timeout=5)
        worker._cycle_lock.release()

    helper = threading.Thread(target=release_later)
    helper.start()

    def finish() -> None:
        released.set()

    timer = threading.Timer(0.3, finish)
    timer.start()

    worker.shutdown()

    helper.join(timeout=5)
    timer.cancel()

    assert released.is_set()


def test_sigterm_handler_sets_stop_event(worker_factory) -> None:
    import signal

    worker, _ = worker_factory()

    assert not worker._stop_event.is_set()

    worker.request_stop(signal.SIGTERM, None)

    assert worker._stop_event.is_set()


# --- database outage -----------------------------------------------------


def test_db_retry_recovers_after_a_transient_outage() -> None:
    calls = {"n": 0}
    slept: list[float] = []

    def flaky() -> str:
        calls["n"] += 1

        if calls["n"] < 3:
            raise OperationalError(
                "SELECT 1",
                {},
                Exception("could not connect to server: Connection refused"),
            )

        return "ok"

    result = with_db_retry(
        flaky, attempts=4, base_delay=0.01, sleep=slept.append
    )

    assert result == "ok"
    assert calls["n"] == 3
    assert len(slept) == 2
    # Backoff grows.
    assert slept[1] > slept[0]


def test_db_retry_gives_up_and_raises() -> None:
    def always_down() -> str:
        raise OperationalError(
            "SELECT 1",
            {},
            Exception("could not connect to server: Connection refused"),
        )

    with pytest.raises(OperationalError):
        with_db_retry(
            always_down, attempts=2, base_delay=0.01, sleep=lambda _: None
        )


def test_db_retry_does_not_mask_a_programming_error() -> None:
    """A constraint violation must surface immediately, not be retried."""

    calls = {"n": 0}

    def broken_query() -> str:
        calls["n"] += 1

        raise OperationalError(
            "SELECT bogus",
            {},
            Exception('column "bogus" does not exist'),
        )

    with pytest.raises(OperationalError):
        with_db_retry(
            broken_query, attempts=3, base_delay=0.01, sleep=lambda _: None
        )

    assert calls["n"] == 1


def test_worker_cycle_survives_database_errors(
    session: Session, make_publication, worker_factory, monkeypatch
) -> None:
    """An outage must stall publishing, not kill the worker."""

    make_publication()

    worker, publisher = worker_factory()

    attempts = {"n": 0}
    original = worker.process_one

    def flaky_process() -> str | None:
        attempts["n"] += 1

        if attempts["n"] == 1:
            raise OperationalError(
                "SELECT 1",
                {},
                Exception("server closed the connection unexpectedly"),
            )

        return original()

    monkeypatch.setattr(worker, "process_one", flaky_process)

    # run_cycle_safely swallows the error; the next call proceeds normally.
    worker.run_cycle_safely()
    worker.run_cycle_safely()

    assert publisher.publish_calls == ["text"]


def test_dns_failure_is_treated_as_transient() -> None:
    """What a stopped PostgreSQL container actually looks like in Docker."""

    from ai_smm.db.session import is_transient_db_error

    exc = OperationalError(
        "SELECT 1",
        {},
        Exception(
            "failed to resolve host 'postgres-host': "
            "[Errno -3] Temporary failure in name resolution"
        ),
    )

    assert is_transient_db_error(exc) is True


def test_dns_failure_is_retried_by_the_helper() -> None:
    calls = {"n": 0}

    def flaky() -> str:
        calls["n"] += 1

        if calls["n"] == 1:
            raise OperationalError(
                "SELECT 1",
                {},
                Exception("failed to resolve host 'db': name resolution"),
            )

        return "ok"

    assert (
        with_db_retry(flaky, attempts=3, base_delay=0.01, sleep=lambda _: None)
        == "ok"
    )
    assert calls["n"] == 2


def test_query_error_is_not_transient() -> None:
    from ai_smm.db.session import is_transient_db_error

    exc = OperationalError(
        "SELECT bogus", {}, Exception('column "bogus" does not exist')
    )

    assert is_transient_db_error(exc) is False


def test_outage_logs_one_line_not_a_traceback(
    worker_factory, monkeypatch, caplog
) -> None:
    """A 6 KB traceback per poll would churn the container log rotation."""

    import logging

    worker, _ = worker_factory()

    def down() -> None:
        raise OperationalError(
            "SELECT 1", {}, Exception("connection refused")
        )

    monkeypatch.setattr(worker, "run_cycle", down)

    with caplog.at_level(logging.WARNING, logger="ai_smm.worker"):
        worker.run_cycle_safely()

    records = [
        r for r in caplog.records if r.name == "ai_smm.worker"
    ]

    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert records[0].exc_info is None
    assert "Database unreachable" in records[0].getMessage()
