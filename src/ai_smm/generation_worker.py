"""Generation worker: poll PostgreSQL, run the agent pipeline, write drafts.

A separate process from the publish worker (ai_smm.worker). It never
imports ai_smm.publishing, ai_smm.security.credentials or
ai_smm.publishing.threads, and never reads a social_account or a
credential row -- it has no use for any of them, and this is enforced by
what the module imports, not only by the database guards (see
ai_smm.db.guards, ai_smm.generation_queue).

What it writes: draft content items only, through
ai_smm.application.content.create_content_item, exactly as the operator
CLI or the importer would -- status=draft, created_by_user=None,
source=copywriter. It never calls approve_revision or
materialize_approved_revision, and does not import them. A generation job
can therefore never become a publication by itself; the existing human
approval path (step-up password, ai_smm_auth) is the only way from here
to there, and it is completely unchanged.

Idempotency: every LLM call happens before any row is written. All of a
job's content items, and the job's own succeeded/failed/cancelled status,
are written in one transaction, committed once at the very end of
process_one. A crash at any point before that commit leaves nothing in
content_items: the claim's lease simply expires and the job is retried
from scratch by whichever generation worker claims it next. There is
deliberately no reconciliation state (contrast the publish worker's
needs_review) because nothing external or irreversible has happened.
"""
from __future__ import annotations

import json
import signal
import threading
import uuid
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from langchain_core.callbacks import get_usage_metadata_callback
from sqlalchemy.orm import Session

from ai_smm.application.content import content_hash
from ai_smm.config import Settings, get_settings
from ai_smm.db.models import ContentGenerationJob, GenerationJobStatus
from ai_smm.db.session import is_transient_db_error, session_scope, with_db_retry
from ai_smm.generation_queue import (
    claim_due_generation_job,
    complete_generation_job,
    mark_generation_failure,
    utcnow,
)
from ai_smm.local_env import load_local_dotenv
from ai_smm.logging_setup import get_logger, setup_logging


logger = get_logger(__name__)

HEARTBEAT_PATH = Path("/tmp/ai-smm-generation-worker-heartbeat")

#: One versioned, documented shape for every content_revisions.metadata
#: this worker writes (SMM-026A/2.1 #5). "editor_verdict.approved" is the
#: agent's own judgement; it is never the same thing as a human approval,
#: which only ever exists as a content_approvals row with a real
#: actor_user_id -- this worker writes neither that table nor
#: content_items.status='approved', and does not import anything that
#: could.
REVISION_METADATA_VERSION = 1

#: Indicative only, for the job's own result.cost_usd -- not a billing
#: record. gpt-4.1-mini, USD per 1M tokens (input, cached input, output).
#: Keep in step with the prices ai_smm.agents.* actually configures the
#: model with; update both together if the model ever changes.
_PRICE_PER_MILLION_TOKENS = {"gpt-4.1-mini": (0.40, 0.10, 1.60)}


def _estimate_cost_usd(usage_by_model: dict[str, dict[str, Any]]) -> float:
    total = 0.0

    for model, usage in usage_by_model.items():
        prices = next(
            (
                p
                for key, p in _PRICE_PER_MILLION_TOKENS.items()
                if model.startswith(key)
            ),
            None,
        )

        if prices is None:
            continue

        price_in, price_cached, price_out = prices
        cached = (usage.get("input_token_details") or {}).get("cache_read", 0) or 0
        billable_in = usage.get("input_tokens", 0) - cached
        total += billable_in * price_in / 1_000_000
        total += cached * price_cached / 1_000_000
        total += usage.get("output_tokens", 0) * price_out / 1_000_000

    return round(total, 6)


def _flatten_tokens(usage_by_model: dict[str, dict[str, Any]]) -> dict[str, int]:
    totals = {"input": 0, "output": 0, "cached": 0}

    for usage in usage_by_model.values():
        totals["input"] += usage.get("input_tokens", 0)
        totals["output"] += usage.get("output_tokens", 0)
        totals["cached"] += (
            usage.get("input_token_details") or {}
        ).get("cache_read", 0) or 0

    return totals


class GenerationFailed(Exception):
    """Something about the request itself is wrong; retrying will not help."""


def _build_graph():
    # Imported lazily: agents build their ChatOpenAI clients at import, so
    # a generation worker that is merely being health-checked (or a test
    # that monkeypatches the agent modules before this import) never pays
    # for that, and a missing OPENAI_API_KEY never breaks the health route.
    from ai_smm.graph import build_graph

    return build_graph()


def _load_knowledge(project_id: str) -> dict[str, Any]:
    from ai_smm.knowledge.loader import load_project_knowledge

    try:
        return load_project_knowledge(project_id)
    except (FileNotFoundError, ValueError) as exc:
        raise GenerationFailed(f"knowledge unavailable: {exc}") from exc


class GenerationWorker:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        graph_factory: Any = _build_graph,
        knowledge_loader: Any = _load_knowledge,
        heartbeat_path: Path | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.graph_factory = graph_factory
        self.knowledge_loader = knowledge_loader
        self.heartbeat_path = heartbeat_path or HEARTBEAT_PATH

        self._stop_event = threading.Event()
        self._cycle_lock = threading.Lock()
        self._scheduler: BackgroundScheduler | None = None

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
            "Generation worker starting",
            extra={
                "context": {
                    "worker_id": self.settings.generation_worker_id,
                    "poll_interval_seconds": (
                        self.settings.generation_poll_interval_seconds
                    ),
                    "lease_seconds": self.settings.generation_lease_seconds,
                    "max_attempts": self.settings.generation_max_attempts,
                    "max_revisions": self.settings.generation_max_revisions,
                }
            },
        )

        scheduler = BackgroundScheduler(timezone="UTC")
        scheduler.add_job(
            self.run_cycle_safely,
            trigger="interval",
            seconds=self.settings.generation_poll_interval_seconds,
            id="poll-generation-jobs",
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
            timeout=max(60, self.settings.generation_lease_seconds)
        )

        try:
            logger.info(
                "Generation worker stopped", extra={"context": {"clean": acquired}}
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
                logger.exception("Generation cycle failed")
        finally:
            self._touch_heartbeat()
            self._cycle_lock.release()

    def process_one(self) -> str | None:
        with session_scope(self.settings) as session:
            job = claim_due_generation_job(
                session,
                worker_id=self.settings.generation_worker_id,
                lease_seconds=self.settings.generation_lease_seconds,
                max_attempts=self.settings.generation_max_attempts,
            )

            if job is None:
                return None

            # Make the claim visible to every other generation worker at
            # once -- the same reason ai_smm.worker.Worker.process_one
            # commits here before doing the slow part.
            session.commit()

            return self._run_job(session, job)

    def _run_job(self, session: Session, job: ContentGenerationJob) -> str:
        job_id = job.id

        try:
            draft, content_plan, usage_report = self._generate(job)
            run_id, drafts_payload = self._build_drafts_payload(
                job=job,
                draft=draft,
                content_plan=content_plan,
                usage_report=usage_report,
            )
        except GenerationFailed as exc:
            self._fail(session, job_id, reason=str(exc), retryable=False)
            return GenerationJobStatus.FAILED.value
        except Exception as exc:  # noqa: BLE001 -- never let one job take the process down
            logger.exception(
                "Generation job raised", extra={"context": {"job_id": str(job_id)}}
            )
            self._fail(
                session,
                job_id,
                reason=f"{type(exc).__name__}: {exc}",
                retryable=True,
                error_type=type(exc).__name__,
            )
            return GenerationJobStatus.FAILED.value

        result = complete_generation_job(
            session,
            job_id=job_id,
            worker_id=self.settings.generation_worker_id,
            run_id=run_id,
            drafts_json=json.dumps(drafts_payload, ensure_ascii=False),
            result_json=json.dumps(
                {
                    "series_title": draft.get("series_title", ""),
                    "revision_count": usage_report["revision_count"],
                    "tokens": usage_report["tokens"],
                    "cost_usd": usage_report["cost_usd"],
                    "seconds": usage_report["seconds"],
                },
                ensure_ascii=False,
            ),
        )
        status = str(result["status"])

        if status != GenerationJobStatus.SUCCEEDED.value:
            logger.info(
                "Generation job no longer claimed; discarding its result",
                extra={
                    "context": {
                        "job_id": str(job_id),
                        "status": status,
                    }
                },
            )

        return status

    def _fail(
        self,
        session: Session,
        job_id: uuid.UUID,
        *,
        reason: str,
        retryable: bool,
        error_type: str | None = None,
    ) -> None:
        job = ContentGenerationJob(
            id=job_id,
            project_id="contract-only",
            requested_by_user_id=uuid.UUID(int=0),
            claimed_by=self.settings.generation_worker_id,
            status=GenerationJobStatus.CLAIMED,
        )

        mark_generation_failure(
            session,
            job,
            reason=reason,
            actor=f"worker:{self.settings.generation_worker_id}",
            max_attempts=self.settings.generation_max_attempts,
            error_type=error_type,
            retryable=retryable,
        )

    # -- generation (no database access; everything here is in-memory or
    # talks to OpenAI, never to Postgres) -----------------------------------

    def _generate(
        self, job: ContentGenerationJob
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        import time

        params = job.params or {}
        knowledge = self.knowledge_loader(job.project_id)

        state: dict[str, Any] = {
            "project_id": job.project_id,
            "knowledge": knowledge,
            "revision_count": 0,
            "max_revisions": self.settings.generation_max_revisions,
            "errors": [],
            "publish_live": False,
        }

        series_size = params.get("series_size")

        if isinstance(series_size, int) and series_size > 0:
            state["series_size"] = series_size

        publishing_strategy = params.get("publishing_strategy")

        if publishing_strategy:
            state["publishing_strategy"] = publishing_strategy

        graph = self.graph_factory()
        started = time.monotonic()

        with get_usage_metadata_callback() as callback:
            result = graph.invoke(state)
            usage_by_model = deepcopy(callback.usage_metadata)

        seconds = round(time.monotonic() - started, 2)

        draft = result.get("draft")

        if not isinstance(draft, dict) or not draft.get("publications"):
            raise GenerationFailed(
                "the agent pipeline produced no usable draft publications"
            )

        usage_report = {
            "tokens": _flatten_tokens(usage_by_model),
            "cost_usd": _estimate_cost_usd(usage_by_model),
            "seconds": seconds,
            "revision_count": result.get("revision_count", 0),
            "editor_approved": bool(result.get("editor_approved", False)),
            "editor_score": result.get("editor_score"),
            "editor_narrative_score": result.get("editor_narrative_score"),
            "editor_issues": list(result.get("editor_issues") or []),
            "editor_feedback": result.get("editor_feedback") or "",
            "model": "gpt-4.1-mini",
        }

        return draft, result.get("content_plan") or {}, usage_report

    def _build_drafts_payload(
        self,
        *,
        job: ContentGenerationJob,
        draft: dict[str, Any],
        content_plan: dict[str, Any],
        usage_report: dict[str, Any],
    ) -> tuple[uuid.UUID, list[dict[str, Any]]]:
        publications = draft.get("publications", [])
        series_title = draft.get("series_title") or content_plan.get(
            "series_title", ""
        )
        series_total = len(publications)
        run_id = uuid.uuid4()

        payload: list[dict[str, Any]] = []

        for publication in publications:
            items = publication.get("items", [])
            body = "\n\n".join(
                str(item.get("text", "")).strip() for item in items
            ).strip()

            if not body:
                # The Editor already reports an empty item as an issue;
                # a job must still finish with whatever is usable rather
                # than fail the whole series over one empty part.
                continue

            metadata = {
                "version": REVISION_METADATA_VERSION,
                "generation_job_id": str(job.id),
                "run_id": str(run_id),
                "model": usage_report["model"],
                "series_title": series_title,
                "position": publication.get("position", 0),
                "series_total": series_total,
                "publishing_strategy": draft.get("publishing_strategy", ""),
                # The agent's own judgement -- never a human approval.
                # A human approval exists only as a content_approvals row
                # with a real actor_user_id (ai_smm.application.content);
                # this worker never writes one.
                "editor_verdict": {
                    "approved": usage_report["editor_approved"],
                    "score": usage_report["editor_score"],
                    "narrative_score": usage_report["editor_narrative_score"],
                    "issues": usage_report["editor_issues"],
                    "revision_count": usage_report["revision_count"],
                },
                "usage": {
                    "tokens": usage_report["tokens"],
                    "cost_usd": usage_report["cost_usd"],
                    "seconds": usage_report["seconds"],
                },
            }

            title = str(publication.get("title") or series_title or "Untitled")
            format_ = str(publication.get("format") or "text")
            payload.append(
                {
                    "title": title,
                    "body": body,
                    "format": format_,
                    "images": [],
                    "items": [],
                    "metadata": metadata,
                    "editor_score": usage_report["editor_score"],
                    "editor_notes": (usage_report.get("editor_feedback") or "")[:5000],
                    "content_hash": content_hash(
                        title=None,
                        body=body,
                        format=format_,
                        images=[],
                        items=[],
                    ),
                }
            )

        if not payload:
            raise GenerationFailed(
                "every generated publication had empty text"
            )

        return run_id, payload

    def _touch_heartbeat(self) -> None:
        try:
            self.heartbeat_path.write_text(utcnow().isoformat(), encoding="utf-8")
        except OSError as exc:
            logger.warning(
                "Could not write generation worker heartbeat",
                extra={"context": {"error": type(exc).__name__}},
            )


def main() -> int:
    load_local_dotenv()
    settings = get_settings()
    setup_logging(settings)

    GenerationWorker(settings=settings).run()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
