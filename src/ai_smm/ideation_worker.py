"""Topic ideation worker (SMM-035B3B1): poll PostgreSQL, ask the model for
topics, save them as content_ideas.

A process of its own, beside the publish worker (ai_smm.worker) and the
generation worker (ai_smm.generation_worker), with a database role of its
own: ai_smm_ideation may EXECUTE the three topic_ideation_* contracts and
nothing else (deploy/control-center/db/ai_smm_ideation.*.sql), and
check_readiness refuses any connection that could do more. It imports no
publishing, credential, approval or content-writing module; what it can
write is decided by topic_ideation_complete, which only ever inserts
content_ideas rows of the claimed job's project.

What a job runs on is what the API copied into its params when it was
queued: the project's profile snapshot and its recent idea titles. The
worker reads no table, and a job without a snapshot (queued before this
stage) fails at once, without a model call.

Idempotency: the model is called before anything is written, and the
ideas and the job's outcome are saved by one contract call in one
transaction. A crash before it leaves nothing behind; the lease expires
and the job is retried while attempts remain -- a claim consumes an
attempt, so a job is billed at most max_attempts times. A completion
that arrives after the claim was lost changes nothing.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import threading
import time
import uuid
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from langchain_core.callbacks import get_usage_metadata_callback
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from ai_smm.agents.ideation import (
    IDEATION_MODEL,
    IdeationOutputInvalid,
    build_ideation_llm,
    suggest_ideas,
)
from ai_smm.config import Settings, get_settings
from ai_smm.content_profile import validate_profile_snapshot
from ai_smm.db.ideation_contracts import (
    IDEATION_PROFILE_PARAM,
    IDEATION_RECENT_TITLES_PARAM,
    MAX_IDEA_TITLE_LENGTH,
    MAX_RECENT_IDEA_TITLES,
)
from ai_smm.db.session import is_transient_db_error, session_scope, with_db_retry
from ai_smm.generation_worker import (
    _READINESS_SQL,
    _estimate_cost_usd,
    _flatten_tokens,
)
from ai_smm.ideation_queue import (
    ClaimedIdeationJob,
    claim_ideation_job,
    complete_ideation_job,
    fail_ideation_job,
)
from ai_smm.local_env import load_local_dotenv
from ai_smm.logging_setup import get_logger, setup_logging


logger = get_logger(__name__)

HEARTBEAT_PATH = Path("/tmp/ai-smm-ideation-worker-heartbeat")

#: The only database objects this worker may use: the three SECURITY
#: DEFINER contracts ai_smm_ideation.grants.sql grants, and verifies.
IDEATION_CONTRACTS = (
    "public.topic_ideation_claim(text,integer)",
    "public.topic_ideation_complete(uuid,text,jsonb,jsonb)",
    "public.topic_ideation_fail(uuid,text,text,boolean,text,integer,integer)",
)

# Every other function in public this role could call: the generation and
# publication contracts above all. Trigger functions are left out -- they
# cannot be called directly, whoever holds EXECUTE. Readable by the
# least-privileged role, like _READINESS_SQL.
_FOREIGN_FUNCTIONS_SQL = text(
    """
SELECT ARRAY(
           SELECT p.oid::regprocedure::text FROM pg_catalog.pg_proc AS p
            WHERE p.pronamespace = 'public'::regnamespace
              AND p.prorettype <> 'pg_catalog.trigger'::pg_catalog.regtype
              AND pg_catalog.has_function_privilege(p.oid, 'EXECUTE')
              AND p.oid NOT IN (
                  SELECT pg_catalog.to_regprocedure(f)::oid
                    FROM pg_catalog.unnest(CAST(:contracts AS text[])) AS f
                   WHERE pg_catalog.to_regprocedure(f) IS NOT NULL)
            ORDER BY 1
       ) AS executable
"""
)


# Every role this connection's role is a member of, or that is a member of it,
# through any chain, in either direction. Any row is a problem: the role has
# no membership at all (ai_smm_ideation.grants.sql refuses to provision with
# one). pg_auth_members is readable by every role; nothing is revoked here.
_MEMBERSHIPS_SQL = text(
    """
WITH RECURSIVE up(oid) AS (
    SELECT m.roleid FROM pg_catalog.pg_auth_members AS m
     WHERE m.member = (SELECT r.oid FROM pg_catalog.pg_roles AS r
                        WHERE r.rolname = current_user)
    UNION
    SELECT m.roleid FROM pg_catalog.pg_auth_members AS m
      JOIN up ON m.member = up.oid
), down(oid) AS (
    SELECT m.member FROM pg_catalog.pg_auth_members AS m
     WHERE m.roleid = (SELECT r.oid FROM pg_catalog.pg_roles AS r
                        WHERE r.rolname = current_user)
    UNION
    SELECT m.member FROM pg_catalog.pg_auth_members AS m
      JOIN down ON m.roleid = down.oid
)
SELECT ARRAY(SELECT pg_catalog.pg_get_userbyid(oid)::text FROM up
              ORDER BY 1) AS member_of,
       ARRAY(SELECT pg_catalog.pg_get_userbyid(oid)::text FROM down
              ORDER BY 1) AS members
"""
)


class IdeationFailed(Exception):
    """Something about the job itself is wrong; retrying will not help."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _job_inputs(
    job: ClaimedIdeationJob,
) -> tuple[dict[str, Any], list[str]]:
    """The job's profile snapshot and recent titles, validated again: the
    insert guard fixed their shape, not their content."""

    params = job.params

    if IDEATION_PROFILE_PARAM not in params:
        raise IdeationFailed(
            "the job has no content profile snapshot (queued before "
            "SMM-035B3B1); queue a new one"
        )

    try:
        profile = validate_profile_snapshot(params[IDEATION_PROFILE_PARAM])
    except ValueError as exc:
        raise IdeationFailed(str(exc)) from exc

    titles = params.get(IDEATION_RECENT_TITLES_PARAM, [])

    if (
        not isinstance(titles, list)
        or len(titles) > MAX_RECENT_IDEA_TITLES
        or not all(
            isinstance(title, str)
            and title.strip()
            and len(title) <= MAX_IDEA_TITLE_LENGTH
            for title in titles
        )
    ):
        raise IdeationFailed("recent_idea_titles is invalid")

    return profile, [title.strip() for title in titles]


class IdeationWorker:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        llm_factory: Any = build_ideation_llm,
        heartbeat_path: Path | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.llm_factory = llm_factory
        self.heartbeat_path = heartbeat_path or HEARTBEAT_PATH

        self._stop_event = threading.Event()
        self._cycle_lock = threading.Lock()
        self._scheduler: BackgroundScheduler | None = None

    @property
    def worker_id(self) -> str:
        return self.settings.ideation_worker_id

    # -- lifecycle --------------------------------------------------------

    def request_stop(self, signum: int, _frame: Any) -> None:
        logger.info(
            "Shutdown signal received; finishing the current job",
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
            "Ideation worker starting",
            extra={
                "context": {
                    "worker_id": self.worker_id,
                    "poll_interval_seconds": (
                        self.settings.ideation_poll_interval_seconds
                    ),
                    "lease_seconds": self.settings.ideation_lease_seconds,
                    "model": IDEATION_MODEL,
                }
            },
        )

        self._touch_heartbeat()

        scheduler = BackgroundScheduler(timezone="UTC")
        scheduler.add_job(
            self.run_cycle_safely,
            trigger="interval",
            seconds=self.settings.ideation_poll_interval_seconds,
            id="poll-ideation-jobs",
            coalesce=True,
            max_instances=1,
            next_run_time=utcnow() + timedelta(seconds=2),
        )
        scheduler.start()
        self._scheduler = scheduler

        try:
            while not self._stop_event.wait(timeout=1.0):
                pass
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None

        acquired = self._cycle_lock.acquire(
            timeout=max(60, self.settings.ideation_lease_seconds)
        )

        try:
            logger.info(
                "Ideation worker stopped", extra={"context": {"clean": acquired}}
            )
        finally:
            if acquired:
                self._cycle_lock.release()

    # -- the cycle ----------------------------------------------------------

    def run_cycle_safely(self) -> None:
        if self._stop_event.is_set():
            return

        if not self._cycle_lock.acquire(blocking=False):
            return

        try:
            with_db_retry(self.process_one)
        except Exception as exc:
            if is_transient_db_error(exc):
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
                logger.exception("Ideation cycle failed")
        finally:
            self._touch_heartbeat()
            self._cycle_lock.release()

    def process_one(self) -> str | None:
        with session_scope(self.settings) as session:
            job = claim_ideation_job(
                session,
                worker_id=self.worker_id,
                lease_seconds=self.settings.ideation_lease_seconds,
            )

            if job is None:
                return None

            # The claim is visible to every other worker before the slow
            # part, as in the publish and generation workers.
            session.commit()

            return self._run_job(session, job)

    def _run_job(self, session: Session, job: ClaimedIdeationJob) -> str:
        try:
            ideas, usage = self._suggest(job)
        except (IdeationFailed, IdeationOutputInvalid) as exc:
            return self._fail(
                session,
                job.id,
                reason=str(exc),
                retryable=False,
                error_type=type(exc).__name__,
            )
        except Exception as exc:  # noqa: BLE001 -- runtime failures may be transient
            logger.exception(
                "Ideation job raised", extra={"context": {"job_id": str(job.id)}}
            )
            return self._fail(
                session,
                job.id,
                reason=f"{type(exc).__name__}: {exc}",
                retryable=True,
                error_type=type(exc).__name__,
            )

        try:
            result = complete_ideation_job(
                session,
                job_id=job.id,
                worker_id=self.worker_id,
                ideas_json=json.dumps(ideas, ensure_ascii=False, allow_nan=False),
                result_json=json.dumps(usage, ensure_ascii=False, allow_nan=False),
            )
        except DBAPIError as exc:
            # The contract raised, which aborts the transaction and saves
            # no idea at all; record the outcome in a fresh one. The claim
            # was committed before the model call.
            session.rollback()
            if is_transient_db_error(exc):
                raise
            logger.warning(
                "Ideas rejected by database contract",
                extra={"context": {"job_id": str(job.id)}},
            )
            return self._fail(
                session,
                job.id,
                reason="database contract rejected the suggested ideas",
                retryable=False,
                error_type="IdeationContractRejected",
            )

        status = str(result["status"])

        if status != "succeeded":
            logger.info(
                "Ideation job no longer claimed; its result was discarded",
                extra={"context": {"job_id": str(job.id), "status": status}},
            )

        return status

    def _fail(
        self,
        session: Session,
        job_id: uuid.UUID,
        *,
        reason: str,
        retryable: bool,
        error_type: str,
    ) -> str:
        return fail_ideation_job(
            session,
            job_id=job_id,
            worker_id=self.worker_id,
            reason=reason,
            retryable=retryable,
            error_type=error_type,
        )

    # -- the model call (no database access) ---------------------------------

    def _suggest(
        self, job: ClaimedIdeationJob
    ) -> tuple[list[dict[str, str | None]], dict[str, Any]]:
        profile, recent_titles = _job_inputs(job)

        llm = self.llm_factory()
        started = time.monotonic()

        with get_usage_metadata_callback() as callback:
            ideas = suggest_ideas(
                llm,
                profile=profile,
                recent_titles=recent_titles,
                topic_hint=job.topic_hint,
                idea_count=job.idea_count,
            )
            usage_by_model = deepcopy(callback.usage_metadata)

        return ideas, {
            "model": IDEATION_MODEL,
            "suggested": len(ideas),
            "profile_settings_version": profile["settings_version"],
            "tokens": _flatten_tokens(usage_by_model),
            "cost_usd": _estimate_cost_usd(usage_by_model),
            "seconds": round(time.monotonic() - started, 2),
        }

    def _touch_heartbeat(self) -> None:
        try:
            self.heartbeat_path.write_text(utcnow().isoformat(), encoding="utf-8")
        except OSError as exc:
            logger.warning(
                "Could not write ideation worker heartbeat",
                extra={"context": {"error": type(exc).__name__}},
            )


def check_readiness(session: Session) -> list[str]:
    """Why this connection must not serve an ideation worker; [] if it may.

    Ready means: the three ideation contracts exist and are executable,
    and nothing else is -- no superuser, no role membership in either
    direction (through any chain), no table or column access, and no
    other function in public (in particular no generation or publication
    contract). A worker started as the owner, the API's roles or the
    generation role refuses to start.
    """

    row = session.execute(
        _READINESS_SQL, {"contracts": list(IDEATION_CONTRACTS)}
    ).one()
    foreign = session.execute(
        _FOREIGN_FUNCTIONS_SQL, {"contracts": list(IDEATION_CONTRACTS)}
    ).scalar_one()

    memberships = session.execute(_MEMBERSHIPS_SQL).one()

    problems: list[str] = []

    if row.is_superuser:
        problems.append("database role is a superuser")

    if memberships.member_of:
        problems.append(
            "database role is a member of: " + ", ".join(memberships.member_of)
        )

    if memberships.members:
        problems.append(
            "other roles are members of the database role: "
            + ", ".join(memberships.members)
        )

    if row.unusable_contracts:
        problems.append(
            "contracts missing or not executable: "
            + ", ".join(row.unusable_contracts)
        )

    if row.accessible_tables:
        problems.append(
            "database role has table access: " + ", ".join(row.accessible_tables)
        )

    if row.column_access_tables:
        problems.append(
            "database role has column access: "
            + ", ".join(row.column_access_tables)
        )

    if foreign:
        problems.append(
            "database role may execute other functions: " + ", ".join(foreign)
        )

    return problems


def check_heartbeat(
    settings: Settings,
    *,
    heartbeat_path: Path | None = None,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """Liveness, as ai_smm.generation_worker.check_heartbeat, with this
    worker's lease and poll interval."""

    path = heartbeat_path or HEARTBEAT_PATH

    if not path.is_file():
        return False, "unhealthy: no heartbeat"

    try:
        beat = datetime.fromisoformat(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError) as exc:
        return False, f"unhealthy: unreadable heartbeat: {type(exc).__name__}"

    if beat.tzinfo is None:
        beat = beat.replace(tzinfo=timezone.utc)

    age = ((now or utcnow()) - beat).total_seconds()
    allowed = (
        settings.ideation_lease_seconds
        + settings.ideation_poll_interval_seconds * settings.health_stale_factor
    )

    if age > allowed:
        return False, f"unhealthy: last cycle {int(age)}s ago, allowed {allowed}s"

    return True, f"healthy: last cycle {int(age)}s ago"


def _readiness(settings: Settings) -> list[str]:
    try:
        with session_scope(settings) as session:
            return check_readiness(session)
    except Exception as exc:
        # The type only: a driver message may carry the connection target.
        return [f"database check failed: {type(exc).__name__}"]


def run_check(settings: Settings) -> int:
    """Container healthcheck (--check): readiness, then liveness."""

    problems = _readiness(settings)

    if problems:
        print("unhealthy: " + "; ".join(problems))

        return 1

    healthy, reason = check_heartbeat(settings)
    print(reason)

    return 0 if healthy else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ai-smm-ideation-worker",
        description="Turn queued topic ideation jobs into suggested ideas.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="readiness and liveness probe for a running worker; exit 0 if healthy",
    )
    args = parser.parse_args(argv)

    load_local_dotenv()
    settings = get_settings()

    if args.check:
        return run_check(settings)

    setup_logging(settings)

    problems = _readiness(settings)

    # Without a key every job would burn its attempts on an error; refuse
    # to start instead of claiming anything.
    if not (os.environ.get("OPENAI_API_KEY") or "").strip():
        problems.append("OPENAI_API_KEY is not set")

    if problems:
        logger.error(
            "Ideation worker refuses to start",
            extra={"context": {"problems": problems}},
        )

        return 2

    IdeationWorker(settings=settings).run()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
