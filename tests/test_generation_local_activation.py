"""Local activation of the generation worker (SMM-033B), FakeGraph only.

The packaged worker as compose.runtime.yml runs it: connected as the
EXECUTE-only ai_smm_generation role, the real project knowledge loader,
its readiness/liveness probe and its entry point -- with a fake standing
in for the agent graph and every outbound socket refused, so no external
API can be reached whatever the code under test tries.
"""
from __future__ import annotations

import signal
import socket
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

import ai_smm.generation_worker as generation_worker_module
from ai_smm.config import get_settings
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
from ai_smm.db.session import create_db_engine, reset_engine, session_scope
from ai_smm.generation_queue import claim_due_generation_job
from ai_smm.generation_worker import (
    GenerationWorker,
    check_heartbeat,
    check_readiness,
    main,
)
from tests import pg_roles
from tests.test_generation_worker import FakeGraph, _good_result


LOCAL_OWNER_SQL = (
    Path(__file__).resolve().parents[1]
    / "deploy"
    / "local-runtime"
    / "generation-owner.sql"
)


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Refuse every Python-level outbound connection and record it.

    psycopg connects through libpq, not the socket module, so the
    database keeps working; an HTTP client (OpenAI, Langfuse, Threads)
    would not.
    """

    attempts: list[Any] = []

    def refuse(_self, address, *args, **kwargs):
        attempts.append(address)
        raise OSError("outbound network disabled in this test")

    def refuse_create(address, *args, **kwargs):
        attempts.append(address)
        raise OSError("outbound network disabled in this test")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse_create)

    return attempts


@pytest.fixture
def fresh_engine():
    """GenerationWorker uses the process-wide engine: start from none."""

    reset_engine()
    get_settings.cache_clear()
    yield
    reset_engine()
    get_settings.cache_clear()


@pytest.fixture
def worker_settings(settings, generation_role, fresh_engine):
    return replace(
        settings,
        database_url=generation_role.url,
        generation_worker_id="local-generation-1",
    )


@pytest.fixture
def knowledge_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "knowledge-base"
    (root / "projects" / "test-project").mkdir(parents=True)
    (root / "editorial_policy.md").write_text(
        "# Политика\nПиши ясно.", encoding="utf-8"
    )
    (root / "projects" / "test-project" / "brief.md").write_text(
        "# Бриф\nПроект для проверки.", encoding="utf-8"
    )
    monkeypatch.setenv("AI_SMM_KNOWLEDGE_ROOT", str(root))

    return root


class ObservingGraph(FakeGraph):
    """FakeGraph that also looks at the job while "the agents run"."""

    def __init__(self, observer: Session, job_id: uuid.UUID, **kwargs) -> None:
        super().__init__(**kwargs)
        self.observer = observer
        self.job_id = job_id
        self.seen: list[tuple[GenerationJobStatus, str | None, int]] = []

    def invoke(self, state: dict[str, Any]) -> dict[str, Any]:
        self.observer.expire_all()
        job = self.observer.get(ContentGenerationJob, self.job_id)
        self.seen.append((job.status, job.claimed_by, job.attempt_count))
        self.observer.rollback()

        return super().invoke(state)


def _enqueue(session: Session, project: str, requester, **kwargs):
    job = ContentGenerationJob(
        id=uuid.uuid4(),
        project_id=project,
        requested_by_user_id=requester.id,
        status=GenerationJobStatus.QUEUED,
        params={"series_size": 2},
        **kwargs,
    )
    session.add(job)
    session.commit()

    return job


def _real_knowledge_worker(settings, graph: FakeGraph) -> GenerationWorker:
    # knowledge_loader left at its default: the real project loader.
    return GenerationWorker(settings=settings, graph_factory=lambda: graph)


# -- readiness and the entry point ------------------------------------------


def test_readiness_accepts_exactly_the_generation_role(
    session: Session, generation_session_factory, admin_database_url: str
) -> None:
    with generation_session_factory() as db:
        assert check_readiness(db) == []

    owner_problems = check_readiness(session)
    assert len(owner_problems) == 1
    assert owner_problems[0].startswith("database role has table access: ")
    assert "social_account_credentials" in owner_problems[0]
    assert "publications" in owner_problems[0]
    assert "content_approvals" in owner_problems[0]

    admin = create_db_engine(admin_database_url, pool_size=1, max_overflow=0)
    try:
        with Session(admin) as db:
            assert "database role is a superuser" in check_readiness(db)
    finally:
        admin.dispose()


def test_readiness_reports_a_contract_the_role_cannot_execute(
    generation_session_factory, admin_database_url: str
) -> None:
    pg_roles.run_admin(
        admin_database_url,
        "REVOKE EXECUTE ON FUNCTION public.generation_fail("
        "uuid, text, text, boolean, text, integer, integer) FROM ai_smm_generation",
    )

    with generation_session_factory() as db:
        assert check_readiness(db) == [
            "contracts missing or not executable: "
            "public.generation_fail(uuid,text,text,boolean,text,integer,integer)"
        ]


def test_readiness_refuses_a_single_column_grant_on_credentials(
    generation_session_factory, admin_database_url: str
) -> None:
    # The three contracts stay executable; one extra column grant is enough.
    pg_roles.run_admin(
        admin_database_url,
        "GRANT SELECT (id) ON public.social_account_credentials "
        "TO ai_smm_generation",
    )

    try:
        with generation_session_factory() as db:
            assert check_readiness(db) == [
                "database role has column access: social_account_credentials"
            ]
    finally:
        pg_roles.run_admin(
            admin_database_url,
            "REVOKE SELECT (id) ON public.social_account_credentials "
            "FROM ai_smm_generation",
        )


@pytest.mark.parametrize("privilege", ["INSERT", "UPDATE", "REFERENCES"])
def test_readiness_refuses_other_column_privileges(
    generation_session_factory, admin_database_url: str, privilege: str
) -> None:
    pg_roles.run_admin(
        admin_database_url,
        f"GRANT {privilege} (status) ON public.publications TO ai_smm_generation",
    )

    try:
        with generation_session_factory() as db:
            assert check_readiness(db) == [
                "database role has column access: publications"
            ]
    finally:
        pg_roles.run_admin(
            admin_database_url,
            f"REVOKE {privilege} (status) ON public.publications "
            "FROM ai_smm_generation",
        )


def test_entry_point_refuses_to_start_with_the_owner_login(
    monkeypatch: pytest.MonkeyPatch, database_url: str, fresh_engine
) -> None:
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.setenv("AI_SMM_DATABASE_URL", database_url)
    monkeypatch.setattr(
        GenerationWorker,
        "run",
        lambda self: pytest.fail("an over-privileged worker must not run"),
    )

    assert main([]) == 2


def test_entry_point_starts_the_worker_as_the_generation_role(
    monkeypatch: pytest.MonkeyPatch, generation_role, fresh_engine
) -> None:
    started: list[str] = []
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.setenv("AI_SMM_DATABASE_URL", generation_role.url)
    monkeypatch.setattr(
        GenerationWorker,
        "run",
        lambda self: started.append(self.settings.generation_worker_id),
    )

    assert main([]) == 0
    assert started == ["generation-worker-1"]


def test_check_probe_reports_readiness_and_liveness(
    monkeypatch: pytest.MonkeyPatch,
    generation_role,
    database_url: str,
    tmp_path: Path,
    fresh_engine,
    capsys: pytest.CaptureFixture[str],
) -> None:
    heartbeat = tmp_path / "heartbeat"
    monkeypatch.setattr(generation_worker_module, "HEARTBEAT_PATH", heartbeat)
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.setenv("AI_SMM_DATABASE_URL", generation_role.url)

    # Ready, but no heartbeat: fail closed.
    assert main(["--check"]) == 1
    assert capsys.readouterr().out.startswith("unhealthy: no heartbeat")

    heartbeat.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
    assert main(["--check"]) == 0
    assert capsys.readouterr().out.startswith("healthy")

    stale = datetime.now(timezone.utc) - timedelta(hours=2)
    heartbeat.write_text(stale.isoformat(), encoding="utf-8")
    assert main(["--check"]) == 1
    assert capsys.readouterr().out.startswith("unhealthy: last cycle")

    # Not ready as the owner, whatever the heartbeat says.
    heartbeat.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
    reset_engine()
    get_settings.cache_clear()
    monkeypatch.setenv("AI_SMM_DATABASE_URL", database_url)
    assert main(["--check"]) == 1
    out = capsys.readouterr().out
    assert out.startswith("unhealthy: database role has table access")
    assert "smm033b" not in out and "@" not in out


def test_check_probe_hides_the_connection_target_when_unreachable(
    monkeypatch: pytest.MonkeyPatch,
    fresh_engine,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.setenv(
        "AI_SMM_DATABASE_URL",
        "postgresql+psycopg://ai_smm_generation:hidden-pw@127.0.0.1:1/nowhere",
    )

    assert main(["--check"]) == 1
    out = capsys.readouterr().out
    assert out.startswith("unhealthy: database check failed: ")
    assert "hidden-pw" not in out and "127.0.0.1" not in out


def test_liveness_allows_one_full_lease_for_a_running_job(settings) -> None:
    settings = replace(
        settings,
        generation_lease_seconds=600,
        generation_poll_interval_seconds=15,
        health_stale_factor=3,
    )
    now = datetime.now(timezone.utc)
    path = Path(settings.media_root) / "beat"

    path.write_text((now - timedelta(seconds=640)).isoformat(), encoding="utf-8")
    assert check_heartbeat(settings, heartbeat_path=path, now=now)[0] is True

    path.write_text((now - timedelta(seconds=650)).isoformat(), encoding="utf-8")
    assert check_heartbeat(settings, heartbeat_path=path, now=now)[0] is False

    path.write_text("not a timestamp", encoding="utf-8")
    assert check_heartbeat(settings, heartbeat_path=path, now=now)[0] is False


def test_liveness_missing_fresh_and_stale_heartbeat(settings) -> None:
    now = datetime.now(timezone.utc)
    path = Path(settings.media_root) / "beat-missing-fresh-stale"
    allowed = (
        settings.generation_lease_seconds
        + settings.generation_poll_interval_seconds * settings.health_stale_factor
    )

    assert not path.exists()
    assert check_heartbeat(settings, heartbeat_path=path, now=now) == (
        False,
        "unhealthy: no heartbeat",
    )

    path.write_text(now.isoformat(), encoding="utf-8")
    healthy, reason = check_heartbeat(settings, heartbeat_path=path, now=now)
    assert healthy is True and reason.startswith("healthy")

    path.write_text(
        (now - timedelta(seconds=allowed + 1)).isoformat(), encoding="utf-8"
    )
    healthy, reason = check_heartbeat(settings, heartbeat_path=path, now=now)
    assert healthy is False and reason.startswith("unhealthy: last cycle")


def test_run_writes_a_heartbeat_before_the_first_cycle(
    settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(generation_worker_module, "setup_logging", lambda _s: None)
    monkeypatch.setattr(GenerationWorker, "install_signal_handlers", lambda self: None)
    graph = FakeGraph(_good_result())
    worker = GenerationWorker(
        settings=settings,
        graph_factory=lambda: graph,
        heartbeat_path=tmp_path / "beat",
    )
    # Stop at once: run() returns before its first scheduled cycle.
    worker.request_stop(signal.SIGTERM, None)

    worker.run()

    assert graph.invocations == []
    assert check_heartbeat(settings, heartbeat_path=tmp_path / "beat")[0] is True


def test_local_owner_script_refuses_any_other_database(
    admin_database_url: str,
) -> None:
    with pytest.raises(Exception, match="ai_smm_runtime_local only"):
        pg_roles.run_script(admin_database_url, LOCAL_OWNER_SQL)

    assert (
        pg_roles.query_admin(
            admin_database_url,
            "SELECT count(*) FROM pg_roles WHERE rolname = 'ai_smm_local_owner'",
        )
        == [(0,)]
    )


# -- FakeGraph smoke: queued -> claimed -> succeeded -> Draft + Revision ----


def test_fake_graph_smoke_turns_a_queued_job_into_drafts(
    session: Session,
    worker_settings,
    project: str,
    make_user,
    knowledge_root: Path,
    no_network: list[Any],
) -> None:
    job = _enqueue(session, project, make_user())
    graph = ObservingGraph(session, job.id, result=_good_result())
    worker = _real_knowledge_worker(worker_settings, graph)

    # The worker's own connection is the least-privileged role.
    with session_scope(worker_settings) as db:
        assert check_readiness(db) == []

    assert worker.process_one() == "succeeded"

    # claimed while the agents ran, by this worker, first attempt
    assert graph.seen == [
        (GenerationJobStatus.CLAIMED, "local-generation-1", 1)
    ]

    # project knowledge came from the mounted knowledge base
    (state,) = graph.invocations
    assert state["project_id"] == project
    assert state["publish_live"] is False
    assert state["max_revisions"] == worker_settings.generation_max_revisions
    assert state["knowledge"]["editorial_policy"].startswith("# Политика")
    assert set(state["knowledge"]["files"]) == {"brief.md"}

    session.expire_all()
    stored = session.get(ContentGenerationJob, job.id)
    assert stored.status is GenerationJobStatus.SUCCEEDED
    assert stored.claimed_by is None and stored.finished_at is not None
    items = session.scalars(
        select(ContentItem).where(ContentItem.generation_job_id == job.id)
    ).all()
    assert len(items) == 2
    assert sorted(stored.result["content_item_ids"]) == sorted(
        str(item.id) for item in items
    )

    for item in items:
        assert item.status is ContentStatus.DRAFT
        assert item.created_by_user_id is None
        revision = session.get(ContentRevision, item.current_revision_id)
        assert revision.source is RevisionSource.COPYWRITER
        verdict = revision.meta["editor_verdict"]
        assert verdict == {
            "approved": True,
            "score": 9.0,
            "narrative_score": 9.0,
            "issues": [],
            "revision_count": 0,
        }

    assert session.scalars(select(ContentApproval)).all() == []
    assert session.scalars(select(Publication)).all() == []
    actions = session.execute(
        text(
            "SELECT action, written_by FROM audit_log "
            "WHERE subject = :subject ORDER BY id"
        ),
        {"subject": f"generation_job:{job.id}"},
    ).all()
    assert [a for a, _ in actions][0] == "generation.claimed"
    assert {w for _, w in actions} == {"ai_smm_generation"}
    assert no_network == []


def test_fake_graph_smoke_with_a_project_without_knowledge_fails_terminally(
    session: Session,
    worker_settings,
    project: str,
    make_user,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_network: list[Any],
) -> None:
    empty = tmp_path / "empty-knowledge"
    (empty / "projects").mkdir(parents=True)
    monkeypatch.setenv("AI_SMM_KNOWLEDGE_ROOT", str(empty))
    job = _enqueue(session, project, make_user())
    graph = FakeGraph(_good_result())

    assert _real_knowledge_worker(worker_settings, graph).process_one() == "failed"

    session.expire_all()
    stored = session.get(ContentGenerationJob, job.id)
    assert stored.status is GenerationJobStatus.FAILED
    assert "knowledge unavailable" in stored.last_error
    assert graph.invocations == []
    assert no_network == []


def test_retry_exhaustion_ends_the_job_after_its_own_attempt_limit(
    session: Session,
    worker_settings,
    project: str,
    make_user,
    knowledge_root: Path,
    no_network: list[Any],
) -> None:
    job = _enqueue(session, project, make_user(), max_attempts=2)
    graph = FakeGraph(raises=TimeoutError("model timeout"))
    worker = _real_knowledge_worker(worker_settings, graph)

    worker.process_one()
    session.expire_all()
    first = session.get(ContentGenerationJob, job.id)
    assert first.status is GenerationJobStatus.QUEUED
    assert first.attempt_count == 1
    assert first.next_attempt_at > datetime.now(timezone.utc)

    # backoff is respected: nothing is due yet
    assert worker.process_one() is None

    session.execute(
        text(
            "UPDATE content_generation_jobs "
            "SET next_attempt_at = now() - interval '1 second' WHERE id = :id"
        ),
        {"id": job.id},
    )
    session.commit()

    worker.process_one()
    session.expire_all()
    final = session.get(ContentGenerationJob, job.id)
    assert final.status is GenerationJobStatus.FAILED
    assert final.attempt_count == final.max_attempts == 2
    assert final.finished_at is not None
    assert "TimeoutError" in final.last_error

    assert worker.process_one() is None
    assert len(graph.invocations) == 2
    assert session.scalars(select(ContentItem)).all() == []
    assert no_network == []


def test_stop_request_claims_nothing_more(
    session: Session,
    worker_settings,
    project: str,
    make_user,
    knowledge_root: Path,
) -> None:
    job = _enqueue(session, project, make_user())
    graph = FakeGraph(_good_result())
    worker = _real_knowledge_worker(worker_settings, graph)
    worker.heartbeat_path = knowledge_root / "beat"

    worker.request_stop(signal.SIGTERM, None)
    worker.run_cycle_safely()

    session.expire_all()
    assert session.get(ContentGenerationJob, job.id).status is (
        GenerationJobStatus.QUEUED
    )
    assert graph.invocations == []


def test_a_restart_after_a_kill_mid_job_produces_one_set_of_drafts(
    session: Session,
    worker_settings,
    generation_session_factory,
    project: str,
    make_user,
    knowledge_root: Path,
    no_network: list[Any],
) -> None:
    job = _enqueue(session, project, make_user())

    # The first process claims, commits the claim, and is killed while the
    # agents run: nothing else of it ever reaches the database.
    with generation_session_factory() as killed:
        claimed = claim_due_generation_job(
            killed, worker_id="local-generation-1", lease_seconds=600
        )
        assert claimed is not None and claimed.id == job.id
        killed.commit()

    graph = FakeGraph(_good_result())
    restarted = _real_knowledge_worker(worker_settings, graph)

    # Restarted at once: the lease still holds, the job is not run twice.
    assert restarted.process_one() is None
    assert graph.invocations == []

    session.execute(
        text(
            "UPDATE content_generation_jobs "
            "SET lease_expires_at = now() - interval '1 second' WHERE id = :id"
        ),
        {"id": job.id},
    )
    session.commit()

    assert restarted.process_one() == "succeeded"
    session.expire_all()
    stored = session.get(ContentGenerationJob, job.id)
    assert stored.status is GenerationJobStatus.SUCCEEDED
    assert stored.attempt_count == 2
    assert (
        len(
            session.scalars(
                select(ContentItem).where(ContentItem.generation_job_id == job.id)
            ).all()
        )
        == 2
    )
    assert no_network == []
