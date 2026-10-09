"""Engine and session factory.

pool_pre_ping is mandatory here: the queue lives in a PostgreSQL instance
shared with other projects, so a connection can go away between polls
without this process noticing. Combined with the retry helper, a restart of
the database stalls publishing instead of crashing the worker.
"""
from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TypeVar

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from ai_smm.config import Settings, get_settings
from ai_smm.logging_setup import get_logger


logger = get_logger(__name__)

T = TypeVar("T")

#: Errors that mean "the database was not reachable", not "the query is wrong".
TRANSIENT_DB_ERRORS = (OperationalError, DBAPIError)

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def create_db_engine(
    database_url: str | None = None,
    *,
    settings: Settings | None = None,
    **kwargs: object,
) -> Engine:
    settings = settings or get_settings()
    url = database_url or settings.require_database_url()

    options: dict[str, object] = {
        "pool_pre_ping": True,
        "pool_size": 5,
        "max_overflow": 2,
        "pool_recycle": 1800,
        "pool_timeout": 10,
        "future": True,
        "connect_args": {
            # Keep a stalled socket from holding the poll loop forever.
            "connect_timeout": 10,
            "application_name": f"ai-smm/{settings.worker_id}",
        },
    }
    options.update(kwargs)

    if url.startswith("sqlite"):
        for unsupported in (
            "pool_size",
            "max_overflow",
            "pool_recycle",
            "pool_timeout",
            "connect_args",
        ):
            options.pop(unsupported, None)

    return create_engine(url, **options)  # type: ignore[arg-type]


def get_engine(settings: Settings | None = None) -> Engine:
    global _engine

    if _engine is None:
        _engine = create_db_engine(settings=settings)

    return _engine


def get_session_factory(
    settings: Settings | None = None,
) -> sessionmaker[Session]:
    global _session_factory

    if _session_factory is None:
        _session_factory = sessionmaker(
            bind=get_engine(settings),
            expire_on_commit=False,
            future=True,
        )

    return _session_factory


def reset_engine() -> None:
    """Drop cached engine and factory (used by tests and after a failover)."""

    global _engine, _session_factory

    if _engine is not None:
        _engine.dispose()

    _engine = None
    _session_factory = None


@contextmanager
def session_scope(
    settings: Settings | None = None,
) -> Iterator[Session]:
    session = get_session_factory(settings)()

    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


def with_db_retry(
    operation: Callable[[], T],
    *,
    attempts: int = 4,
    base_delay: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Run a database operation, retrying only transient connection errors.

    A programming error (bad SQL, constraint violation) is re-raised at once:
    retrying it would hide a bug rather than survive an outage.
    """

    last_error: BaseException | None = None

    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except TRANSIENT_DB_ERRORS as exc:
            if not is_transient_db_error(exc):
                raise

            last_error = exc

            if attempt == attempts:
                break

            delay = base_delay * (2 ** (attempt - 1))

            logger.warning(
                "Database unavailable, retrying",
                extra={
                    "context": {
                        "attempt": attempt,
                        "attempts": attempts,
                        "delay_seconds": delay,
                        "error_type": type(exc).__name__,
                    }
                },
            )

            sleep(delay)

    assert last_error is not None

    raise last_error


#: Substrings that identify "the database was unreachable" as opposed to
#: "the query was wrong". The DNS entries matter in Docker: while the
#: PostgreSQL container is down its name does not resolve at all, which is
#: what a restart of the shared instance looks like from inside our network.
_CONNECTION_ERROR_MARKERS = (
    "could not connect",
    "connection refused",
    "server closed the connection",
    "connection already closed",
    "terminating connection",
    "no connection to the server",
    "connection timed out",
    "connection attempt failed",
    "is the server running",
    "ssl connection has been closed",
    "unable to open database file",
    "failed to resolve host",
    "name resolution",
    "name or service not known",
    "temporary failure",
    "no route to host",
    "network is unreachable",
    "the database system is starting up",
    "the database system is shutting down",
    "too many clients",
    "invalidatepoolerror",
)


def is_transient_db_error(exc: BaseException) -> bool:
    """True when the error means the server was unreachable."""

    if not isinstance(exc, TRANSIENT_DB_ERRORS):
        return False

    if getattr(exc, "connection_invalidated", False):
        return True

    return _looks_like_connection_error(exc)


def _looks_like_connection_error(exc: BaseException) -> bool:
    # Walk the cause chain: SQLAlchemy wraps the driver error, and the
    # useful text is usually on the psycopg exception underneath.
    seen: list[str] = []
    current: BaseException | None = exc

    for _ in range(5):
        if current is None:
            break

        seen.append(str(current).lower())
        current = current.__cause__ or current.__context__

    original = getattr(exc, "orig", None)

    if original is not None:
        seen.append(str(original).lower())

    return any(
        marker in message
        for message in seen
        for marker in _CONNECTION_ERROR_MARKERS
    )


def check_database(session: Session) -> None:
    session.execute(text("SELECT 1"))
