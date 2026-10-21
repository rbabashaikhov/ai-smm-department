"""The generation worker end to end, with a fake graph (no real LLM calls).

GenerationWorker takes graph_factory/knowledge_loader as injectable
dependencies specifically so these tests never import ai_smm.graph or
ai_smm.agents.*: a fake stands in for "Strategist/Copywriter/Editor ran
and returned this state", and everything downstream -- idempotency, the
editor-verdict-vs-human-approval distinction, what the worker is and is
not capable of writing -- is tested against the real database.
"""
from __future__ import annotations

import inspect
import json
import uuid
from datetime import datetime, timezone
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

import ai_smm.generation_worker as generation_worker_module
from ai_smm.db.models import (
    ContentApproval,
    ContentGenerationJob,
    ContentItem,
    ContentRevision,
    ContentStatus,
    GenerationJobStatus,
    Publication,
    RevisionSource,
)
from ai_smm.generation_worker import GenerationFailed, GenerationWorker


class FakeGraph:
    """Stands in for ai_smm.graph.build_graph()'s compiled graph."""

    def __init__(
        self,
        result: dict[str, Any] | None = None,
        raises: Exception | None = None,
    ) -> None:
        self.result = result or {}
        self.raises = raises
        self.invocations: list[dict[str, Any]] = []

    def invoke(self, state: dict[str, Any]) -> dict[str, Any]:
        self.invocations.append(state)

        if self.raises is not None:
            raise self.raises

        return self.result


def _good_result(
    *, approved: bool = True, issues: list[str] | None = None
) -> dict[str, Any]:
    return {
        "content_plan": {"series_title": "Серия", "planned_total": 2},
        "draft": {
            "series_title": "Серия",
            "publishing_strategy": "standalone_series",
            "publications": [
                {
                    "position": 1,
                    "title": "Часть 1",
                    "format": "text",
                    "items": [{"order": 1, "text": "Текст первой части."}],
                },
                {
                    "position": 2,
                    "title": "Часть 2",
                    "format": "text",
                    "items": [{"order": 1, "text": "Текст второй части."}],
                },
            ],
        },
        "editor_approved": approved,
        "editor_score": 9.0 if approved else 5.0,
        "editor_narrative_score": 9.0 if approved else 5.0,
        "editor_issues": issues or [],
        "editor_feedback": "ok" if approved else "not good enough",
        "revision_count": 0,
    }


def _fake_worker(settings, graph: FakeGraph) -> GenerationWorker:
    return GenerationWorker(
        settings=settings,
        graph_factory=lambda: graph,
        knowledge_loader=lambda project_id: {"project_id": project_id},
    )


def _enqueue(
    session: Session, *, project_id: str, requester, **params
) -> ContentGenerationJob:
    job = ContentGenerationJob(
        id=uuid.uuid4(),
        project_id=project_id,
        requested_by_user_id=requester.id,
        status=GenerationJobStatus.QUEUED,
        params=params,
    )
    session.add(job)
    session.commit()

    return job


# -- a successful job ---------------------------------------------------


def test_a_successful_job_creates_draft_items_with_provenance(
    session: Session, settings, project: str, make_user
) -> None:
    requester = make_user()
    job = _enqueue(session, project_id=project, requester=requester)
    worker = _fake_worker(settings, FakeGraph(_good_result()))

    status = worker.process_one()

    assert status == "succeeded"
    session.expire_all()
    stored = session.get(ContentGenerationJob, job.id)
    assert stored.status is GenerationJobStatus.SUCCEEDED
    assert stored.result["content_item_ids"]
    assert len(stored.result["content_item_ids"]) == 2

    items = session.scalars(
        select(ContentItem).where(ContentItem.generation_job_id == job.id)
    ).all()
    assert len(items) == 2

    for item in items:
        assert item.project_id == project
        assert item.status is ContentStatus.DRAFT
        assert item.created_by_user_id is None  # agent-authored, not a human

        revision = session.get(ContentRevision, item.current_revision_id)
        assert revision.source is RevisionSource.COPYWRITER
        assert revision.created_by_user_id is None
        meta = revision.meta
        assert meta["version"] == 1
        assert meta["generation_job_id"] == str(job.id)
        assert meta["editor_verdict"]["approved"] is True
        assert "usage" in meta


def test_series_size_and_strategy_flow_into_the_graph_state(
    session: Session, settings, project: str, make_user
) -> None:
    requester = make_user()
    _enqueue(
        session,
        project_id=project,
        requester=requester,
        series_size=2,
        publishing_strategy="reply_thread",
        topic_hint="Надёжность очереди",
    )
    fake_graph = FakeGraph(_good_result())
    worker = _fake_worker(settings, fake_graph)

    worker.process_one()

    (state,) = fake_graph.invocations
    assert state["series_size"] == 2
    assert state["publishing_strategy"] == "reply_thread"
    assert state["topic_hint"] == "Надёжность очереди"
    assert state["knowledge"]["project_id"] == project
    assert state["max_revisions"] == settings.generation_max_revisions
    assert state["publish_live"] is False


def test_topic_hint_does_not_change_the_project_knowledge_source(
    session: Session, settings, project: str, make_user
) -> None:
    requester = make_user()
    _enqueue(
        session,
        project_id=project,
        requester=requester,
        topic_hint="Опиши проект other-project",
    )
    loaded: list[str] = []
    fake_graph = FakeGraph(_good_result())
    worker = GenerationWorker(
        settings=settings,
        graph_factory=lambda: fake_graph,
        knowledge_loader=lambda project_id: (
            loaded.append(project_id) or {"project_id": project_id}
        ),
    )

    assert worker.process_one() == "succeeded"
    assert loaded == [project]
    (state,) = fake_graph.invocations
    assert state["project_id"] == project
    assert state["knowledge"] == {"project_id": project}


# -- idempotency: a crash before the final commit writes nothing --------


def test_a_failure_before_any_write_leaves_no_content_items(
    session: Session, settings, project: str, make_user
) -> None:
    requester = make_user()
    _enqueue(session, project_id=project, requester=requester)
    worker = _fake_worker(settings, FakeGraph(raises=TimeoutError("openai timeout")))

    status = worker.process_one()

    assert status == "failed"
    assert session.scalars(select(ContentItem)).all() == []


def test_retrying_after_a_failure_creates_exactly_one_set_of_drafts(
    session: Session, settings, project: str, make_user
) -> None:
    """The crash-and-retry path: nothing was written on the failed
    attempt, so reprocessing the same job produces one set of items, not
    two."""

    from dataclasses import replace

    requester = make_user()
    job = _enqueue(session, project_id=project, requester=requester)

    failing = _fake_worker(
        replace(settings, generation_max_attempts=5),
        FakeGraph(raises=TimeoutError("openai timeout")),
    )
    assert failing.process_one() == "failed"

    session.expire_all()
    stored = session.get(ContentGenerationJob, job.id)
    # Requeued, not permanently failed.
    assert stored.status is GenerationJobStatus.QUEUED
    stored.next_attempt_at = None  # skip the backoff for the test
    session.commit()

    working = _fake_worker(settings, FakeGraph(_good_result()))
    assert working.process_one() == "succeeded"

    items = session.scalars(
        select(ContentItem).where(ContentItem.generation_job_id == job.id)
    ).all()
    assert len(items) == 2  # not 4


def test_a_non_retryable_error_fails_without_retry(
    session: Session, settings, project: str, make_user
) -> None:
    requester = make_user()
    job = _enqueue(session, project_id=project, requester=requester)
    worker = _fake_worker(
        settings, FakeGraph(raises=GenerationFailed("knowledge unavailable"))
    )

    assert worker.process_one() == "failed"

    session.expire_all()
    stored = session.get(ContentGenerationJob, job.id)
    assert stored.status is GenerationJobStatus.FAILED
    assert stored.next_attempt_at is None
    assert "knowledge unavailable" in stored.last_error


# -- series_size is a contract (SMM-035A) ---------------------------------
#
# Job eb41298e asked for one publication and got three drafts marked
# succeeded. The count that comes back must be the count that was asked
# for, for every project; anything else fails the job for good (no retry:
# another paid run would not change what the contract means) and writes
# no draft at all. editor_approved=false is a verdict, not a failure.


def _result_with(publications: int, *, approved: bool = True) -> dict[str, Any]:
    result = _good_result(approved=approved)
    result["draft"]["publications"] = [
        {
            "position": n,
            "title": f"Часть {n}",
            "format": "text",
            "items": [{"order": 1, "text": f"Текст части {n}."}],
        }
        for n in range(1, publications + 1)
    ]

    return result


def _drafts_of(session: Session, job: ContentGenerationJob) -> list[ContentItem]:
    return list(
        session.scalars(
            select(ContentItem).where(ContentItem.generation_job_id == job.id)
        ).all()
    )


@pytest.mark.parametrize("size", [1, 3])
def test_exactly_the_requested_number_of_drafts_is_stored(
    session: Session, settings, project: str, make_user, size: int
) -> None:
    job = _enqueue(
        session,
        project_id=project,
        requester=make_user(),
        series_size=size,
        publishing_strategy="standalone_series",
    )
    worker = _fake_worker(settings, FakeGraph(_result_with(size)))

    assert worker.process_one() == "succeeded"

    session.expire_all()
    assert len(_drafts_of(session, job)) == size


@pytest.mark.parametrize(("asked", "got"), [(1, 3), (3, 1), (2, 3), (3, 2)])
def test_a_different_count_fails_the_job_and_stores_nothing(
    session: Session, settings, project: str, make_user, asked: int, got: int
) -> None:
    job = _enqueue(
        session,
        project_id=project,
        requester=make_user(),
        series_size=asked,
        publishing_strategy="standalone_series",
    )
    worker = _fake_worker(settings, FakeGraph(_result_with(got)))

    assert worker.process_one() == "failed"

    session.expire_all()
    stored = session.get(ContentGenerationJob, job.id)
    assert stored.status is GenerationJobStatus.FAILED
    assert stored.next_attempt_at is None  # not retryable
    assert stored.attempt_count == 1
    assert f"series_size {asked}" in stored.last_error
    assert f"{got} publications" in stored.last_error
    assert _drafts_of(session, job) == []


def test_the_pilot_project_gets_no_special_count(
    session: Session, settings, make_user
) -> None:
    from ai_smm.queue import ensure_project

    ensure_project(
        session,
        project_id="ai-catalog-consultant",
        display_name="AI Catalog Consultant",
        knowledge_path="knowledge/projects/ai-catalog-consultant",
    )
    session.commit()
    job = _enqueue(
        session,
        project_id="ai-catalog-consultant",
        requester=make_user(),
        series_size=1,
        publishing_strategy="standalone_series",
    )
    worker = _fake_worker(settings, FakeGraph(_result_with(3)))

    assert worker.process_one() == "failed"

    session.expire_all()
    assert _drafts_of(session, job) == []


def test_a_job_without_series_size_keeps_the_old_behaviour(
    session: Session, settings, project: str, make_user
) -> None:
    job = _enqueue(session, project_id=project, requester=make_user())
    worker = _fake_worker(settings, FakeGraph(_result_with(2)))

    assert worker.process_one() == "succeeded"

    session.expire_all()
    assert len(_drafts_of(session, job)) == 2


def test_an_unapproved_verdict_with_the_right_count_still_succeeds(
    session: Session, settings, project: str, make_user
) -> None:
    job = _enqueue(
        session,
        project_id=project,
        requester=make_user(),
        series_size=1,
        publishing_strategy="standalone_series",
    )
    worker = _fake_worker(
        settings, FakeGraph(_result_with(1, approved=False))
    )

    assert worker.process_one() == "succeeded"

    session.expire_all()
    (draft,) = _drafts_of(session, job)
    revision = session.get(ContentRevision, draft.current_revision_id)
    assert revision.meta["editor_verdict"]["approved"] is False
    assert draft.status is ContentStatus.DRAFT


def test_the_graph_state_carries_the_requested_size_for_every_project(
    session: Session, settings, project: str, make_user
) -> None:
    _enqueue(session, project_id=project, requester=make_user(), series_size=1)
    fake_graph = FakeGraph(_result_with(1))

    _fake_worker(settings, fake_graph).process_one()

    (state,) = fake_graph.invocations
    assert state["series_size"] == 1


# -- cancelled while generating ------------------------------------------


def test_a_job_cancelled_mid_flight_discards_the_result(
    session: Session, settings, project: str, make_user
) -> None:
    requester = make_user()
    job = _enqueue(session, project_id=project, requester=requester)
    worker = _fake_worker(settings, FakeGraph(_good_result()))

    from ai_smm.generation_queue import claim_due_generation_job

    claimed = claim_due_generation_job(
        session, worker_id=settings.generation_worker_id, lease_seconds=600
    )
    session.commit()

    # An owner's cancel lands between the claim and the worker finishing.
    claimed.status = GenerationJobStatus.CANCELLED
    claimed.finished_at = datetime.now(timezone.utc)
    session.commit()

    status = worker._run_job(session, claimed)

    assert status == "cancelled"
    assert session.scalars(select(ContentItem)).all() == []
    session.expire_all()
    stored = session.get(ContentGenerationJob, job.id)
    assert stored.status is GenerationJobStatus.CANCELLED  # not overwritten


# -- an Editor rejection is recorded, never auto-approved ----------------


def test_a_rejected_draft_is_still_saved_but_never_approved(
    session: Session, settings, project: str, make_user
) -> None:
    requester = make_user()
    job = _enqueue(session, project_id=project, requester=requester)
    worker = _fake_worker(
        settings, FakeGraph(_good_result(approved=False, issues=["too short"]))
    )

    status = worker.process_one()

    assert status == "succeeded"  # the JOB succeeded: it produced drafts
    items = session.scalars(
        select(ContentItem).where(ContentItem.generation_job_id == job.id)
    ).all()
    assert len(items) == 2

    for item in items:
        assert item.status is ContentStatus.DRAFT
        assert item.approved_revision_id is None
        revision = session.get(ContentRevision, item.current_revision_id)
        assert revision.meta["editor_verdict"]["approved"] is False
        assert revision.meta["editor_verdict"]["issues"] == ["too short"]

    # No human ever approved these -- the only table a real approval is
    # recorded in is empty.
    assert session.scalars(select(ContentApproval)).all() == []


# -- what the worker cannot do, structurally -----------------------------


def test_the_module_never_imports_approval_materialize_or_publishing() -> None:
    """Checks actual import statements, not the docstring that explains
    this guarantee in prose (which necessarily names these symbols)."""

    import ast

    tree = ast.parse(inspect.getsource(generation_worker_module))
    imported: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            imported.add(module)
            imported.update(f"{module}.{alias.name}" for alias in node.names)
            imported.update(alias.name for alias in node.names)

    forbidden = {
        "approve_revision",
        "materialize_approved_revision",
        "ThreadsPublisher",
        "CredentialOpener",
        "social_account",
    }
    hit = forbidden & imported
    assert not hit, hit
    assert not any(name.startswith("ai_smm.publishing") for name in imported)


def test_a_successful_run_creates_no_publication_rows(
    session: Session, settings, project: str, make_user
) -> None:
    requester = make_user()
    _enqueue(session, project_id=project, requester=requester)
    worker = _fake_worker(settings, FakeGraph(_good_result()))

    worker.process_one()

    assert session.scalars(select(Publication)).all() == []


def test_single_post_output_is_normalized_before_hash_and_storage(
    session: Session, settings, project: str, make_user
) -> None:
    requester = make_user()
    job = _enqueue(session, project_id=project, requester=requester)
    result = _good_result()
    for publication in result["draft"]["publications"]:
        publication["format"] = "single_post"
    worker = _fake_worker(settings, FakeGraph(result))

    assert worker.process_one() == "succeeded"
    revisions = session.scalars(
        select(ContentRevision)
        .join(ContentItem, ContentItem.id == ContentRevision.content_item_id)
        .where(ContentItem.generation_job_id == job.id)
    ).all()
    assert len(revisions) == 2
    assert {revision.format for revision in revisions} == {"text"}


def test_unsupported_output_format_is_terminal_without_another_graph_call(
    session: Session, settings, project: str, make_user
) -> None:
    requester = make_user()
    job = _enqueue(session, project_id=project, requester=requester)
    result = _good_result()
    result["draft"]["publications"][0]["format"] = "video"
    graph = FakeGraph(result)
    worker = _fake_worker(settings, graph)

    assert worker.process_one() == "failed"
    assert worker.process_one() is None
    assert len(graph.invocations) == 1
    session.expire_all()
    stored = session.get(ContentGenerationJob, job.id)
    assert stored.status is GenerationJobStatus.FAILED
    assert stored.claimed_by is None
    assert stored.lease_expires_at is None
    assert stored.attempt_count == 1
    assert session.scalars(select(ContentItem)).all() == []


@pytest.mark.parametrize("malformation", ["editor_issues", "json_metadata"])
def test_malformed_output_is_terminal_without_another_graph_call(
    session: Session, settings, project: str, make_user, malformation: str
) -> None:
    requester = make_user()
    job = _enqueue(session, project_id=project, requester=requester)
    result = _good_result()
    if malformation == "editor_issues":
        result["editor_issues"] = 42
    else:
        result["draft"]["publications"][0]["position"] = object()
    graph = FakeGraph(result)
    worker = _fake_worker(settings, graph)

    assert worker.process_one() == "failed"
    assert worker.process_one() is None
    assert len(graph.invocations) == 1
    session.expire_all()
    stored = session.get(ContentGenerationJob, job.id)
    assert stored.status is GenerationJobStatus.FAILED
    assert stored.claimed_by is None
    assert stored.lease_expires_at is None
    assert stored.attempt_count == 1
    assert session.scalars(select(ContentItem)).all() == []


def test_database_contract_rejection_rolls_back_and_fails_terminally(
    session: Session, settings, project: str, make_user, monkeypatch
) -> None:
    requester = make_user()
    job = _enqueue(session, project_id=project, requester=requester)
    original_complete = generation_worker_module.complete_generation_job

    def reject_hash(db, **kwargs):
        drafts = json.loads(kwargs["drafts_json"])
        drafts[0]["content_hash"] = "0" * 64
        return original_complete(
            db,
            **{**kwargs, "drafts_json": json.dumps(drafts)},
        )

    monkeypatch.setattr(
        generation_worker_module, "complete_generation_job", reject_hash
    )
    worker = _fake_worker(settings, FakeGraph(_good_result()))

    assert worker.process_one() == "failed"
    session.expire_all()
    stored = session.get(ContentGenerationJob, job.id)
    assert stored.status is GenerationJobStatus.FAILED
    assert stored.claimed_by is None
    assert stored.lease_expires_at is None
    assert stored.last_error == "database contract rejected the generated draft"
    assert stored.attempt_count == 1
    assert session.scalars(select(ContentItem)).all() == []


def test_an_empty_draft_fails_the_job_without_writing_anything(
    session: Session, settings, project: str, make_user
) -> None:
    requester = make_user()
    _enqueue(session, project_id=project, requester=requester)
    empty = _good_result()
    empty["draft"]["publications"] = []
    worker = _fake_worker(settings, FakeGraph(empty))

    status = worker.process_one()

    assert status == "failed"
    assert session.scalars(select(ContentItem)).all() == []


def test_all_empty_publications_fail_immediately_not_after_lease_timeout(
    session: Session, settings, project: str, make_user
) -> None:
    """GenerationFailed raised while preparing DB writes is terminal too."""

    requester = make_user()
    job = _enqueue(session, project_id=project, requester=requester)
    result = _good_result()
    for publication in result["draft"]["publications"]:
        publication["items"] = [{"text": "   "}]
    worker = _fake_worker(settings, FakeGraph(result))

    assert worker.process_one() == "failed"
    session.expire_all()
    stored = session.get(ContentGenerationJob, job.id)
    assert stored.status is GenerationJobStatus.FAILED
    assert stored.claimed_by is None
    assert stored.lease_expires_at is None
    assert session.scalars(select(ContentItem)).all() == []
