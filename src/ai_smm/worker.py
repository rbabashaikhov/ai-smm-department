"""Long-running worker: poll PostgreSQL, publish what is due.

APScheduler is used purely as a periodic trigger. The schedule itself lives
in publications.scheduled_at, so there is no separate jobstore to keep in
sync and a restart loses nothing.
"""
from __future__ import annotations

import signal
import threading
from datetime import timedelta
from pathlib import Path
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy.orm import Session

from ai_smm.config import Settings, get_settings
from ai_smm.db.session import (
    is_transient_db_error,
    session_scope,
    with_db_retry,
)
from ai_smm.logging_setup import get_logger, setup_logging
from ai_smm.publishing.service import publish_claimed_publication
from ai_smm.queue import (
    claim_due_publication,
    count_published_since,
    last_published_at,
    quarantine_abandoned_publishing,
    record_audit,
    release_expired_leases,
    utcnow,
)


logger = get_logger(__name__)

#: Written after each completed cycle so the healthcheck can tell a live
#: worker from a hung one without opening a network port.
HEARTBEAT_PATH = Path("/tmp/ai-smm-worker-heartbeat")


def _default_publisher_factory() -> Any:
    # Imported lazily so a dry-run worker never needs Threads credentials.
    from ai_smm.publishing.threads import ThreadsPublisher

    return ThreadsPublisher()


class Worker:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        publisher_factory: Any = _default_publisher_factory,
        media_root: Path | None = None,
        heartbeat_path: Path | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.publisher_factory = publisher_factory
        self.media_root = media_root or self.settings.media_root
        self.heartbeat_path = heartbeat_path or HEARTBEAT_PATH

        self._stop_event = threading.Event()
        #: Held while an external call may be in flight, so SIGTERM waits.
        self._cycle_lock = threading.Lock()
        self._scheduler: BackgroundScheduler | None = None

    # -- lifecycle --------------------------------------------------------

    def request_stop(self, signum: int, _frame: Any) -> None:
        logger.info(
            "Shutdown signal received; finishing the current cycle",
            extra={"context": {"signal": signum}},
        )
        self._stop_event.set()

    def install_signal_handlers(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, self.request_stop)

    def run(self) -> None:
        setup_logging(self.settings)
        self.install_signal_handlers()

        logger.info(
            "Worker starting",
            extra={
                "context": {
                    "worker_id": self.settings.worker_id,
                    "dry_run": self.settings.dry_run,
                    "poll_interval_seconds": (
                        self.settings.poll_interval_seconds
                    ),
                    "display_tz": self.settings.display_tz_name,
                }
            },
        )

        self.recover_on_start()

        scheduler = BackgroundScheduler(timezone="UTC")
        scheduler.add_job(
            self.run_cycle_safely,
            trigger="interval",
            seconds=self.settings.poll_interval_seconds,
            id="poll-queue",
            # After downtime, one catch-up run instead of a burst.
            coalesce=True,
            max_instances=1,
            misfire_grace_time=self.settings.misfire_grace_seconds,
            next_run_time=utcnow() + timedelta(seconds=2),
        )
        scheduler.start()
        self._scheduler = scheduler

        try:
            # Woken by the signal handler; no busy loop.
            while not self._stop_event.wait(timeout=1.0):
                pass
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        if self._scheduler is not None:
            # Do not start new cycles, do not wait for the interval.
            self._scheduler.shutdown(wait=False)
            self._scheduler = None

        # Block until an in-flight cycle has recorded its outcome.
        acquired = self._cycle_lock.acquire(timeout=60)

        try:
            logger.info(
                "Worker stopped",
                extra={"context": {"clean": acquired}},
            )
        finally:
            if acquired:
                self._cycle_lock.release()

    # -- recovery ---------------------------------------------------------

    def recover_on_start(self) -> None:
        """Reconcile whatever the previous process left behind."""

        def operation() -> tuple[list[int], list[int]]:
            with session_scope(self.settings) as session:
                quarantined = quarantine_abandoned_publishing(
                    session, actor=f"worker:{self.settings.worker_id}"
                )
                released = release_expired_leases(
                    session, actor=f"worker:{self.settings.worker_id}"
                )

                record_audit(
                    session,
                    actor=f"worker:{self.settings.worker_id}",
                    action="worker_started",
                    details={
                        "dry_run": self.settings.dry_run,
                        "quarantined": quarantined,
                        "released": released,
                    },
                )

                return quarantined, released

        quarantined, released = with_db_retry(operation)

        if quarantined or released:
            logger.warning(
                "Recovered state from a previous worker run",
                extra={
                    "context": {
                        "quarantined_needs_review": len(quarantined),
                        "leases_released": len(released),
                    }
                },
            )

    # -- the cycle --------------------------------------------------------

    def run_cycle_safely(self) -> None:
        """Never let an exception kill the scheduler thread."""

        if self._stop_event.is_set():
            return

        if not self._cycle_lock.acquire(blocking=False):
            return

        try:
            with_db_retry(self.run_cycle)
        except Exception as exc:
            if is_transient_db_error(exc):
                # An outage of the shared PostgreSQL is expected to happen.
                # A full SQLAlchemy traceback every poll would bury the real
                # signal and churn through the log rotation, so record one
                # line and let the next cycle try again.
                logger.warning(
                    "Database unreachable; skipping this cycle",
                    extra={
                        "context": {
                            "error_type": type(exc).__name__,
                            "detail": str(getattr(exc, "orig", exc))[:200],
                        }
                    },
                )
            else:
                logger.exception("Publishing cycle failed")
        finally:
            self._touch_heartbeat()
            self._cycle_lock.release()

    def run_cycle(self) -> list[str]:
        results: list[str] = []

        with session_scope(self.settings) as session:
            release_expired_leases(
                session, actor=f"worker:{self.settings.worker_id}"
            )

        for _ in range(max(1, self.settings.batch_size)):
            if self._stop_event.is_set():
                break

            outcome = self.process_one()

            if outcome is None:
                break

            results.append(outcome)

        return results

    def process_one(self) -> str | None:
        with session_scope(self.settings) as session:
            if not self._rate_limit_allows(session):
                return None

            publication = claim_due_publication(
                session,
                worker_id=self.settings.worker_id,
                lease_seconds=self.settings.lease_seconds,
            )

            if publication is None:
                return None

            # Make the claim visible to every other worker at once.
            session.commit()

            outcome = publish_claimed_publication(
                session,
                publication,
                settings=self.settings,
                publisher_factory=self.publisher_factory,
                media_root=self.media_root,
            )

            return outcome.status

    def _rate_limit_allows(self, session: Session) -> bool:
        """Spacing and daily cap, enforced before a row is ever claimed."""

        if self.settings.dry_run:
            return True

        now = utcnow()

        published_today = count_published_since(
            session,
            platform="threads",
            since=now - timedelta(hours=24),
        )

        if published_today >= self.settings.daily_publish_limit:
            logger.warning(
                "Daily publish limit reached; skipping cycle",
                extra={
                    "context": {
                        "published_last_24h": published_today,
                        "limit": self.settings.daily_publish_limit,
                    }
                },
            )

            return False

        last = last_published_at(session, platform="threads")

        if last is not None:
            elapsed = (now - last).total_seconds()

            if elapsed < self.settings.min_publish_interval_seconds:
                logger.info(
                    "Minimum interval between publications not reached",
                    extra={
                        "context": {
                            "elapsed_seconds": int(elapsed),
                            "required_seconds": (
                                self.settings.min_publish_interval_seconds
                            ),
                        }
                    },
                )

                return False

        return True

    def _touch_heartbeat(self) -> None:
        try:
            self.heartbeat_path.write_text(
                utcnow().isoformat(), encoding="utf-8"
            )
        except OSError as exc:
            logger.warning(
                "Could not write heartbeat",
                extra={"context": {"error": type(exc).__name__}},
            )


def main() -> int:
    settings = get_settings()
    setup_logging(settings)

    Worker(settings=settings).run()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
