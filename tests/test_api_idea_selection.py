"""Rejecting and using a suggested topic over HTTP (SMM-035B3B2).

The business rules: owner only, proposed only, exactly one generation job
per used idea, atomic, replay-safe. The database-level proof (composite
key, direct-SQL bypass attempts, privileges) is in
tests/test_db_idea_selection.py. tests/mutation_role/ re-runs this module
with the API connected as ai_smm_cc_rw.
"""
from __future__ import annotations

import threading
import uuid
from dataclasses import replace
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from ai_smm.db.models import (
    AuditLog,
    ContentGenerationJob,
    ContentIdea,
    ContentIdeaStatus,
    MembershipRole,
    TopicIdeationJob,
    TopicIdeationJobStatus,
)


@pytest.fixture
def api_settings(settings):
    return replace(settings, api_cookie_secure=False, api_mutations_enabled=True)


@pytest.fixture
def seed_idea(session, project: str):
    def factory(
        *,
        user,
        project_id: str | None = None,
        title: str = "Тема",
        description: str = "Описание темы",
        angle: str | None = "Угол",
    ) -> ContentIdea:
        now = datetime.now(timezone.utc)
        job = TopicIdeationJob(
            id=uuid.uuid4(),
            project_id=project_id or project,
            requested_by_user_id=user.id,
            status=TopicIdeationJobStatus.SUCCEEDED,
            idea_count=1,
            finished_at=now,
            created_at=now,
        )
        session.add(job)
        session.flush()
        idea = ContentIdea(
            id=uuid.uuid4(),
            project_id=job.project_id,
            ideation_job_id=job.id,
            position=1,
            title=title,
            description=description,
            angle=angle,
            created_at=now,
        )
        session.add(idea)
        session.commit()

        return idea

    return factory


def _url(project: str, idea_id, command: str) -> str:
    return f"/api/v1/projects/{project}/content-ideas/{idea_id}/{command}"


def _post(client, project: str, idea_id, command: str, csrf: str):
    return client.post(
        _url(project, idea_id, command), headers={"X-CSRF-Token": csrf}
    )


def _reload(session, idea_id) -> ContentIdea:
    session.expire_all()

    return session.get(ContentIdea, idea_id)


def _jobs(session) -> int:
    session.expire_all()

    return session.scalar(select(func.count()).select_from(ContentGenerationJob))


def _audit(session, action: str) -> int:
    session.expire_all()

    return session.scalar(
        select(func.count()).select_from(AuditLog).where(AuditLog.action == action)
    )


# -- reject ------------------------------------------------------------------


def test_an_owner_rejects_a_proposed_idea(
    client, login, member, seed_idea, session, project: str
) -> None:
    owner = member(MembershipRole.OWNER)
    idea = seed_idea(user=owner)
    csrf = login(owner)

    response = _post(client, project, idea.id, "reject", csrf)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "rejected" and body["generation_job_id"] is None
    assert _reload(session, idea.id).status is ContentIdeaStatus.REJECTED
    assert _jobs(session) == 0
    assert _audit(session, "generation.idea_rejected") == 1


@pytest.mark.parametrize("first", ["reject", "use"])
def test_a_terminal_idea_cannot_be_rejected(
    first, client, login, member, seed_idea, session, project: str
) -> None:
    owner = member(MembershipRole.OWNER)
    idea = seed_idea(user=owner)
    csrf = login(owner)
    assert _post(client, project, idea.id, first, csrf).status_code in (200, 202)
    jobs_before = _jobs(session)

    response = _post(client, project, idea.id, "reject", csrf)

    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert error["code"] == "INVALID_STATE_TRANSITION"
    assert error["details"] == {
        "command": "reject",
        "status": "rejected" if first == "reject" else "used",
    }
    assert _jobs(session) == jobs_before


# -- use ---------------------------------------------------------------------


def test_using_an_idea_queues_exactly_one_matching_job(
    client, login, member, seed_idea, session, project: str, settings
) -> None:
    owner = member(MembershipRole.OWNER)
    idea = seed_idea(user=owner, title="Заголовок", description="Суть", angle="Угол")
    csrf = login(owner)

    response = _post(client, project, idea.id, "use", csrf)

    assert response.status_code == 202, response.text
    body = response.json()
    job = session.get(ContentGenerationJob, uuid.UUID(body["generation_job_id"]))
    assert _jobs(session) == 1
    assert body["replayed"] is False
    assert body["idea"]["status"] == "used"
    assert body["idea"]["generation_job_id"] == body["generation_job_id"]
    assert body["generation_job"]["id"] == body["generation_job_id"]
    assert body["generation_job"]["status"] == "queued"
    assert body["topic_includes"] == ["title", "description", "angle"]
    assert job.project_id == project
    assert job.requested_by_user_id == owner.id
    assert job.max_attempts == settings.generation_max_attempts
    assert job.params["series_size"] == 1
    assert job.params["publishing_strategy"] == "standalone_series"
    assert job.params["topic_hint"] == "Заголовок\n\nСуть\n\nУгол"
    # The same profile snapshot a directly created job carries.
    assert job.params["content_profile"]["settings_version"] is None
    stored = _reload(session, idea.id)
    assert stored.status is ContentIdeaStatus.USED
    assert stored.generation_job_id == job.id
    assert _audit(session, "generation.idea_used") == 1
    assert _audit(session, "generation.queued") == 1


def test_a_long_description_is_left_out_whole_not_truncated(
    client, login, member, seed_idea, session, project: str
) -> None:
    owner = member(MembershipRole.OWNER)
    idea = seed_idea(
        user=owner,
        title="Короткий заголовок",
        description="д" * 1500,
        angle="Короткий угол",
    )
    csrf = login(owner)

    response = _post(client, project, idea.id, "use", csrf)

    assert response.status_code == 202, response.text
    job = session.get(
        ContentGenerationJob, uuid.UUID(response.json()["generation_job_id"])
    )
    # The title always goes in whole; the part that does not fit is omitted
    # entirely and reported, and the one that fits still goes in.
    assert job.params["topic_hint"] == "Короткий заголовок\n\nКороткий угол"
    assert response.json()["topic_includes"] == ["title", "angle"]


def test_a_maximal_title_alone_still_fits(
    client, login, member, seed_idea, session, project: str
) -> None:
    owner = member(MembershipRole.OWNER)
    idea = seed_idea(user=owner, title="т" * 200, description="д" * 2000, angle=None)
    csrf = login(owner)

    response = _post(client, project, idea.id, "use", csrf)

    assert response.status_code == 202, response.text
    job = session.get(
        ContentGenerationJob, uuid.UUID(response.json()["generation_job_id"])
    )
    assert job.params["topic_hint"] == "т" * 200
    assert response.json()["topic_includes"] == ["title"]


def test_repeating_use_returns_the_same_job_and_creates_nothing(
    client, login, member, seed_idea, session, project: str
) -> None:
    owner = member(MembershipRole.OWNER)
    idea = seed_idea(user=owner)
    csrf = login(owner)
    first = _post(client, project, idea.id, "use", csrf)

    second = _post(client, project, idea.id, "use", csrf)

    assert first.status_code == 202 and second.status_code == 200, second.text
    assert second.json()["generation_job_id"] == first.json()["generation_job_id"]
    assert second.json()["replayed"] is True
    assert second.json()["topic_includes"] == []
    assert _jobs(session) == 1
    assert _audit(session, "generation.idea_used") == 1
    assert _audit(session, "generation.queued") == 1


def test_a_rejected_idea_cannot_be_used(
    client, login, member, seed_idea, session, project: str
) -> None:
    owner = member(MembershipRole.OWNER)
    idea = seed_idea(user=owner)
    csrf = login(owner)
    assert _post(client, project, idea.id, "reject", csrf).status_code == 200

    response = _post(client, project, idea.id, "use", csrf)

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "INVALID_STATE_TRANSITION"
    assert response.json()["error"]["details"] == {
        "command": "use",
        "status": "rejected",
    }
    assert _jobs(session) == 0
    assert _reload(session, idea.id).status is ContentIdeaStatus.REJECTED


def test_a_failing_job_creation_leaves_nothing_behind(
    client, login, member, seed_idea, session, project: str, monkeypatch
) -> None:
    """Atomicity: the idea is locked and the job inserted before the failure."""

    import ai_smm.application.topic_ideation as module

    real_create = module.create_generation_job

    def create_then_fail(*args, **kwargs):
        real_create(*args, **kwargs)  # the row is really inserted ...
        raise RuntimeError("boom")  # ... and then the command dies

    monkeypatch.setattr(module, "create_generation_job", create_then_fail)
    owner = member(MembershipRole.OWNER)
    idea = seed_idea(user=owner)
    csrf = login(owner)

    with pytest.raises(RuntimeError):
        _post(client, project, idea.id, "use", csrf)

    assert _jobs(session) == 0
    assert _reload(session, idea.id).status is ContentIdeaStatus.PROPOSED
    assert _reload(session, idea.id).generation_job_id is None
    assert _audit(session, "generation.idea_used") == 0
    assert _audit(session, "generation.queued") == 0


def test_an_invalid_profile_refuses_use_and_keeps_the_idea_proposed(
    client, login, member, seed_idea, session, project: str
) -> None:
    from ai_smm.db.models import ProjectSettings

    owner = member(MembershipRole.OWNER)
    idea = seed_idea(user=owner)
    session.add(
        ProjectSettings(
            project_id=project,
            content_config=[],
            publishing_config={},
            brand_config={},
            version=1,
        )
    )
    session.commit()
    csrf = login(owner)

    response = _post(client, project, idea.id, "use", csrf)

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert _jobs(session) == 0
    assert _reload(session, idea.id).status is ContentIdeaStatus.PROPOSED


# -- concurrency -------------------------------------------------------------


def _race(client_factory, calls: list[tuple[str, str]], project, idea_id):
    """Fire the commands together, each from its own client (own connection)."""

    results: list[tuple[str, int, dict]] = [None] * len(calls)  # type: ignore
    barrier = threading.Barrier(len(calls))

    def run(position: int, command: str, csrf: str, client) -> None:
        barrier.wait()
        response = _post(client, project, idea_id, command, csrf)
        results[position] = (command, response.status_code, response.json())

    threads = [
        threading.Thread(target=run, args=(i, command, csrf, client_factory(csrf)))
        for i, (command, csrf) in enumerate(calls)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    return results


@pytest.fixture
def racers(api_settings, api_session_factory, open_session, member):
    """Independent signed-in clients of one owner, for real contention."""

    from fastapi.testclient import TestClient

    from ai_smm.api.app import create_app

    owner = member(MembershipRole.OWNER)
    clients: list[TestClient] = []

    def make(count: int):
        pairs = []
        for _ in range(count):
            app = create_app(
                api_settings, session_factory=api_session_factory, access_log=False
            )
            test_client = TestClient(app)
            test_client.__enter__()
            clients.append(test_client)
            pairs.append((test_client, open_session(test_client, owner)))

        return owner, pairs

    yield make

    for test_client in clients:
        test_client.__exit__(None, None, None)


def test_concurrent_use_creates_one_job(
    racers, seed_idea, session, project: str
) -> None:
    owner, pairs = racers(4)
    idea = seed_idea(user=owner)
    by_csrf = {csrf: client for client, csrf in pairs}

    results = _race(
        lambda csrf: by_csrf[csrf],
        [("use", csrf) for _, csrf in pairs],
        project,
        idea.id,
    )

    assert sorted(code for _, code, _ in results) == [200, 200, 200, 202], results
    assert len({body["generation_job_id"] for _, _, body in results}) == 1
    assert _jobs(session) == 1
    assert _audit(session, "generation.idea_used") == 1
    assert _reload(session, idea.id).status is ContentIdeaStatus.USED


def test_a_reject_use_race_ends_in_one_consistent_state(
    racers, seed_idea, session, project: str
) -> None:
    owner, pairs = racers(2)
    idea = seed_idea(user=owner)
    by_csrf = {csrf: client for client, csrf in pairs}
    (_, csrf_a), (_, csrf_b) = pairs

    results = _race(
        lambda csrf: by_csrf[csrf],
        [("reject", csrf_a), ("use", csrf_b)],
        project,
        idea.id,
    )

    final = _reload(session, idea.id)
    by_command = {command: (code, body) for command, code, body in results}

    if final.status is ContentIdeaStatus.REJECTED:
        assert by_command["reject"][0] == 200
        assert by_command["use"][0] == 409
        assert _jobs(session) == 0
        assert final.generation_job_id is None
    else:
        assert final.status is ContentIdeaStatus.USED
        assert by_command["use"][0] == 202
        assert by_command["reject"][0] == 409
        assert _jobs(session) == 1
        assert final.generation_job_id is not None


# -- access ------------------------------------------------------------------


@pytest.mark.parametrize("command", ["reject", "use"])
@pytest.mark.parametrize(
    "role", [MembershipRole.EDITOR, MembershipRole.VIEWER]
)
def test_members_below_owner_cannot_choose(
    command, role, client, login, member, seed_idea, session, project: str
) -> None:
    owner = member(MembershipRole.OWNER)
    idea = seed_idea(user=owner)
    csrf = login(member(role))

    response = _post(client, project, idea.id, command, csrf)

    assert response.status_code == 403, response.text
    assert _jobs(session) == 0
    assert _reload(session, idea.id).status is ContentIdeaStatus.PROPOSED


@pytest.mark.parametrize("command", ["reject", "use"])
def test_a_non_member_learns_nothing(
    command, client, login, make_user, member, seed_idea, session, project: str
) -> None:
    owner = member(MembershipRole.OWNER)
    idea = seed_idea(user=owner)
    csrf = login(make_user())

    response = _post(client, project, idea.id, command, csrf)

    assert response.status_code in (403, 404), response.text
    assert _jobs(session) == 0
    assert _reload(session, idea.id).status is ContentIdeaStatus.PROPOSED


@pytest.mark.parametrize("command", ["reject", "use"])
def test_another_projects_idea_is_404_even_for_an_owner_of_both(
    command,
    client,
    login,
    member,
    make_membership,
    make_other_project: str,
    seed_idea,
    session,
    project: str,
) -> None:
    owner = member(MembershipRole.OWNER)
    make_membership(
        user=owner, project_id=make_other_project, role=MembershipRole.OWNER
    )
    foreign = seed_idea(user=owner, project_id=make_other_project)
    csrf = login(owner)

    response = _post(client, project, foreign.id, command, csrf)

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "NOT_FOUND"
    assert _jobs(session) == 0
    assert _reload(session, foreign.id).status is ContentIdeaStatus.PROPOSED


@pytest.mark.parametrize("command", ["reject", "use"])
def test_an_unknown_idea_is_404(
    command, client, login, member, project: str
) -> None:
    csrf = login(member(MembershipRole.OWNER))

    response = _post(client, project, uuid.uuid4(), command, csrf)

    assert response.status_code == 404, response.text


@pytest.mark.parametrize("command", ["reject", "use"])
def test_a_command_needs_a_csrf_token(
    command, client, login, member, seed_idea, session, project: str
) -> None:
    owner = member(MembershipRole.OWNER)
    idea = seed_idea(user=owner)
    login(owner)

    response = client.post(_url(project, idea.id, command))

    assert response.status_code == 403, response.text
    assert _jobs(session) == 0
    assert _reload(session, idea.id).status is ContentIdeaStatus.PROPOSED


@pytest.fixture
def read_only_client(api_settings, api_session_factory):
    from fastapi.testclient import TestClient

    from ai_smm.api.app import create_app

    app = create_app(
        replace(api_settings, api_mutations_enabled=False),
        session_factory=api_session_factory,
        access_log=False,
    )
    with TestClient(app) as test_client:
        yield test_client


@pytest.mark.parametrize("command", ["reject", "use"])
def test_read_only_mode_refuses_both_commands(
    command, read_only_client, open_session, member, seed_idea, session, project: str
) -> None:
    owner = member(MembershipRole.OWNER)
    idea = seed_idea(user=owner)
    csrf = open_session(read_only_client, owner)

    response = _post(read_only_client, project, idea.id, command, csrf)

    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "MUTATIONS_DISABLED"
    assert _jobs(session) == 0
    assert _reload(session, idea.id).status is ContentIdeaStatus.PROPOSED


# -- unchanged neighbours ----------------------------------------------------


def test_the_generation_endpoint_is_unchanged(
    client, login, member, session, project: str
) -> None:
    csrf = login(member(MembershipRole.OWNER))

    response = client.post(
        f"/api/v1/projects/{project}/generation-jobs",
        json={"series_size": 2, "topic_hint": "  своя тема  "},
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 202, response.text
    params = response.json()["params"]
    assert params["series_size"] == 2
    assert params["topic_hint"] == "своя тема"
    assert params["publishing_strategy"] == "standalone_series"
    assert _jobs(session) == 1
    assert _audit(session, "generation.idea_used") == 0
