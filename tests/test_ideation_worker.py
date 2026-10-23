"""The topic ideation worker (SMM-035B3B1) against PostgreSQL, with a fake
model: claim, the one model call, all-or-nothing completion, bounded
retries, lost claims, project isolation, and what the worker refuses to
run. Run as the table owner here; tests/test_db_ideation_role.py runs it
as ai_smm_ideation.
"""
from __future__ import annotations

import ast
import inspect
import uuid
from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

import ai_smm.ideation_worker as ideation_worker_module
from ai_smm.application.topic_ideation import create_topic_ideation_job
from ai_smm.content_profile import build_profile_snapshot
from ai_smm.db.models import (
    ContentIdea,
    Project,
    ProjectSettings,
    TopicIdeationJob,
    TopicIdeationJobStatus,
)
from ai_smm.db.session import reset_engine
from ai_smm.ideation_worker import IdeationWorker


PROFILE = build_profile_snapshot(
    content_config={"main_theme": "Кухня"},
    brand_config={},
    default_language="ru",
    settings_version=None,
)


def _idea(n: int, **overrides: Any) -> dict[str, Any]:
    idea = {"title": f"Тема {n}", "description": f"Описание {n}", "angle": None}
    return {**idea, **overrides}


class FakeLLM:
    def __init__(self, *answers: Any, on_invoke=None) -> None:
        self.answers = list(answers)
        self.calls: list[Any] = []
        self.on_invoke = on_invoke

    def invoke(self, messages: Any) -> Any:
        self.calls.append(messages)
        if self.on_invoke is not None:
            self.on_invoke()
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture(autouse=True)
def _fresh_engine() -> Iterator[None]:
    # session_scope caches one engine per process; every test here picks
    # its own URL (owner or ai_smm_ideation).
    reset_engine()
    yield
    reset_engine()


@pytest.fixture
def worker_settings(settings):
    return replace(
        settings, ideation_worker_id="ideation-test", ideation_lease_seconds=60
    )


def _worker(worker_settings, llm: FakeLLM) -> IdeationWorker:
    built: list[FakeLLM] = []

    def factory() -> FakeLLM:
        built.append(llm)
        return llm

    worker = IdeationWorker(settings=worker_settings, llm_factory=factory)
    worker.built = built  # type: ignore[attr-defined]
    return worker


@pytest.fixture
def queue(session: Session, project: str, make_user):
    requester = make_user()

    def factory(
        *, project_id: str | None = None, idea_count: int = 3, params=None, **kw
    ) -> TopicIdeationJob:
        job = TopicIdeationJob(
            id=uuid.uuid4(),
            project_id=project_id or project,
            requested_by_user_id=requester.id,
            idea_count=idea_count,
            params=(
                {"content_profile": PROFILE, "recent_idea_titles": []}
                if params is None
                else params
            ),
            **kw,
        )
        session.add(job)
        session.commit()
        return job

    return factory


def _job(session: Session, job: TopicIdeationJob) -> TopicIdeationJob:
    session.expire_all()
    return session.get(TopicIdeationJob, job.id)


def _ideas(session: Session) -> list[ContentIdea]:
    session.expire_all()
    return list(
        session.scalars(
            select(ContentIdea).order_by(
                ContentIdea.ideation_job_id, ContentIdea.position
            )
        )
    )


def _make_due(session: Session, job: TopicIdeationJob) -> None:
    session.execute(
        text("UPDATE topic_ideation_jobs SET next_attempt_at = now() WHERE id = :id"),
        {"id": job.id},
    )
    session.commit()


# -- the happy path ------------------------------------------------------------


def test_a_job_becomes_ideas_with_one_model_call(
    session: Session, worker_settings, queue
) -> None:
    job = queue(topic_hint="весна")
    llm = FakeLLM({"ideas": [_idea(1), _idea(2, angle="Угол"), _idea(3)]})

    assert _worker(worker_settings, llm).process_one() == "succeeded"

    assert len(llm.calls) == 1
    stored = _job(session, job)
    assert stored.status == TopicIdeationJobStatus.SUCCEEDED
    assert stored.attempt_count == 1
    ideas = _ideas(session)
    assert [(i.position, i.title, i.angle) for i in ideas] == [
        (1, "Тема 1", None), (2, "Тема 2", "Угол"), (3, "Тема 3", None),
    ]
    assert {i.project_id for i in ideas} == {job.project_id}
    assert stored.result["idea_ids"] == [str(i.id) for i in ideas]
    assert stored.result["model"] == "gpt-4.1-mini"
    assert stored.result["suggested"] == 3
    assert set(stored.result["tokens"]) == {"input", "output", "cached"}
    assert stored.result["cost_usd"] == 0
    assert "весна" in llm.calls[0][1][1]


def test_nothing_due_means_no_model_call(worker_settings, queue, session) -> None:
    job = queue()
    llm = FakeLLM({"ideas": [_idea(1)]})
    worker = _worker(worker_settings, llm)

    assert worker.process_one() == "succeeded"
    # The finished job is never claimed again: no second call, no new ideas.
    assert worker.process_one() is None
    assert len(llm.calls) == 1
    assert len(_ideas(session)) == 1
    assert _job(session, job).status == TopicIdeationJobStatus.SUCCEEDED


def test_the_prompt_is_the_snapshot_not_the_current_settings(
    session: Session, worker_settings, project: str, make_user
) -> None:
    def set_theme(theme: str) -> None:
        row = session.get(ProjectSettings, project) or ProjectSettings(
            project_id=project, version=1
        )
        row.content_config = {"main_theme": theme}
        session.add(row)
        session.commit()

    set_theme("Тема при постановке")
    job = create_topic_ideation_job(
        session,
        project=session.get(Project, project),
        requested_by_user=make_user(),
        mutations_enabled=True,
        ideation_enabled=True,
        idea_count=1,
    )
    session.commit()
    set_theme("Тема после постановки")
    llm = FakeLLM({"ideas": [_idea(1)]})

    assert _worker(worker_settings, llm).process_one() == "succeeded"

    prompt = llm.calls[0][1][1]
    assert "Тема при постановке" in prompt
    assert "Тема после постановки" not in prompt
    assert _job(session, job).status == TopicIdeationJobStatus.SUCCEEDED


def test_each_job_writes_only_into_its_own_project(
    session: Session, worker_settings, queue, make_other_project: str
) -> None:
    mine = queue()
    theirs = queue(project_id=make_other_project)
    llm = FakeLLM({"ideas": [_idea(1)]}, {"ideas": [_idea(2)]})
    worker = _worker(worker_settings, llm)

    assert worker.process_one() == "succeeded"
    assert worker.process_one() == "succeeded"

    by_job = {i.ideation_job_id: i.project_id for i in _ideas(session)}
    assert by_job == {mine.id: mine.project_id, theirs.id: theirs.project_id}


# -- failures ------------------------------------------------------------------


@pytest.mark.parametrize(
    "answer",
    [
        {"ideas": []},
        {"ideas": [_idea(1), _idea(1)]},
        {"ideas": [_idea(1), _idea(2), _idea(3), _idea(4)]},
        {"ideas": [_idea(1, title="x" * 201)]},
        {"something": "else"},
    ],
)
def test_a_malformed_answer_fails_the_job_and_saves_nothing(
    session: Session, worker_settings, queue, answer
) -> None:
    job = queue()
    llm = FakeLLM(answer)

    assert _worker(worker_settings, llm).process_one() == "failed"

    stored = _job(session, job)
    assert stored.status == TopicIdeationJobStatus.FAILED
    assert stored.attempt_count == 1  # not retried: the answer is the problem
    assert _ideas(session) == []


def test_an_answer_the_contract_refuses_saves_no_part_of_it(
    session: Session, worker_settings, queue, monkeypatch
) -> None:
    """Defence in depth: if the agent's own check were wrong, the contract
    still refuses the whole payload -- no idea of it is kept."""

    job = queue()
    monkeypatch.setattr(
        ideation_worker_module,
        "suggest_ideas",
        lambda llm, **_: [_idea(1), _idea(2, title="тема 1".upper())],
    )

    assert _worker(worker_settings, FakeLLM()).process_one() == "failed"

    stored = _job(session, job)
    assert stored.status == TopicIdeationJobStatus.FAILED
    assert stored.last_error == "database contract rejected the suggested ideas"
    assert _ideas(session) == []


def test_a_runtime_error_is_retried_within_the_jobs_attempts(
    session: Session, worker_settings, queue
) -> None:
    job = queue(max_attempts=2)
    llm = FakeLLM(TimeoutError("model timed out"), TimeoutError("again"))
    worker = _worker(worker_settings, llm)

    assert worker.process_one() == "queued"
    stored = _job(session, job)
    assert stored.status == TopicIdeationJobStatus.QUEUED
    assert stored.next_attempt_at is not None
    assert "TimeoutError" in stored.last_error

    assert worker.process_one() is None  # backoff: not due yet
    _make_due(session, job)
    assert worker.process_one() == "failed"

    stored = _job(session, job)
    assert stored.status == TopicIdeationJobStatus.FAILED
    assert stored.attempt_count == 2
    assert len(llm.calls) == 2  # billed at most max_attempts times
    assert _ideas(session) == []


@pytest.mark.parametrize(
    ("params", "reason"),
    [
        ({}, "no content profile snapshot"),
        ({"content_profile": None, "recent_idea_titles": []}, "content_profile"),
        (
            {
                "content_profile": {**PROFILE, "schema_version": 9},
                "recent_idea_titles": [],
            },
            "schema_version",
        ),
        ({"content_profile": PROFILE, "recent_idea_titles": "x"}, "recent_idea_titles"),
    ],
)
def test_a_job_without_a_usable_snapshot_fails_without_a_model_call(
    session: Session, worker_settings, queue, params, reason: str
) -> None:
    """{} is what a job queued before SMM-035B3B1 carries."""

    job = queue(params=params)
    llm = FakeLLM()
    worker = _worker(worker_settings, llm)

    assert worker.process_one() == "failed"

    stored = _job(session, job)
    assert stored.status == TopicIdeationJobStatus.FAILED
    assert reason in stored.last_error
    assert llm.calls == [] and worker.built == []  # type: ignore[attr-defined]


def test_a_result_for_a_lost_claim_is_discarded(
    session: Session, worker_settings, queue, session_factory
) -> None:
    job = queue()

    def cancel() -> None:
        with session_factory() as other:
            other.execute(
                text(
                    "UPDATE topic_ideation_jobs SET status = 'cancelled',"
                    " claimed_by = NULL, claimed_at = NULL,"
                    " lease_expires_at = NULL, finished_at = now()"
                    " WHERE id = :id"
                ),
                {"id": job.id},
            )
            other.commit()

    llm = FakeLLM({"ideas": [_idea(1)]}, on_invoke=cancel)

    assert _worker(worker_settings, llm).process_one() == "cancelled"
    assert _job(session, job).status == TopicIdeationJobStatus.CANCELLED
    assert _ideas(session) == []


# -- what the worker is, and refuses -------------------------------------------


def test_the_module_imports_no_publishing_approval_or_content_writer() -> None:
    tree = ast.parse(inspect.getsource(ideation_worker_module))
    imported: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported.update(alias.name for alias in node.names)

    for name in imported:
        assert not name.startswith("ai_smm.publishing"), name
        assert not name.startswith("ai_smm.security"), name
        assert name not in {
            "ai_smm.worker",
            "ai_smm.queue",
            "ai_smm.application.content",
            "ai_smm.generation_queue",
            "approve_revision",
            "materialize_approved_revision",
            "create_content_item",
            "complete_generation_job",
        }, name


def test_main_refuses_to_start_without_a_model_key(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setattr(ideation_worker_module, "load_local_dotenv", lambda: None)
    monkeypatch.setattr(ideation_worker_module, "_readiness", lambda settings: [])
    monkeypatch.setattr(
        ideation_worker_module.IdeationWorker,
        "run",
        lambda self: pytest.fail("the worker must not run"),
    )

    assert ideation_worker_module.main([]) == 2


def test_main_refuses_to_start_when_not_ready(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-used")
    monkeypatch.setattr(ideation_worker_module, "load_local_dotenv", lambda: None)
    monkeypatch.setattr(
        ideation_worker_module,
        "_readiness",
        lambda settings: ["database role is a superuser"],
    )
    monkeypatch.setattr(
        ideation_worker_module.IdeationWorker,
        "run",
        lambda self: pytest.fail("the worker must not run"),
    )

    assert ideation_worker_module.main([]) == 2


def test_the_heartbeat_allows_one_lease_plus_some_polls(
    worker_settings, tmp_path
) -> None:
    from datetime import timedelta

    path = tmp_path / "beat"
    healthy, reason = ideation_worker_module.check_heartbeat(
        worker_settings, heartbeat_path=path
    )
    assert (healthy, reason) == (False, "unhealthy: no heartbeat")

    now = ideation_worker_module.utcnow()
    path.write_text(now.isoformat(), encoding="utf-8")
    allowed = (
        worker_settings.ideation_lease_seconds
        + worker_settings.ideation_poll_interval_seconds
        * worker_settings.health_stale_factor
    )

    assert ideation_worker_module.check_heartbeat(
        worker_settings, heartbeat_path=path, now=now + timedelta(seconds=allowed)
    )[0]
    assert not ideation_worker_module.check_heartbeat(
        worker_settings, heartbeat_path=path, now=now + timedelta(seconds=allowed + 1)
    )[0]
