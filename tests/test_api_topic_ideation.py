"""Topic ideation over HTTP (SMM-035B3A): the disabled-by-default gate,
RBAC, the mutation gate, validation, serialisation, pagination and
project isolation.

Most tests run with the feature gate ON (this module's api_settings), as
the stage that ships the ideation worker will; the default -- OFF on every
deployment -- has its own tests below. The database-level proof of who may
write which row lives in tests/test_db_topic_ideation.py.
"""
from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select, text

from ai_smm.application.control_plane import MutationsDisabled
from ai_smm.application.topic_ideation import (
    TopicIdeationDisabled,
    create_topic_ideation_job,
)
from ai_smm.config import load_settings
from ai_smm.db.models import (
    AuditLog,
    ContentIdea,
    ContentIdeaStatus,
    MembershipRole,
    TopicIdeationJob,
    TopicIdeationJobStatus,
)


@pytest.fixture
def api_settings(settings):
    return replace(
        settings,
        api_cookie_secure=False,
        api_mutations_enabled=True,
        topic_ideation_jobs_enabled=True,
    )


def _create(client, project: str, csrf: str, payload: dict | None = None):
    return client.post(
        f"/api/v1/projects/{project}/topic-ideation-jobs",
        json={} if payload is None else payload,
        headers={"X-CSRF-Token": csrf},
    )


def _job_count(session) -> int:
    session.expire_all()
    return session.scalar(select(func.count()).select_from(TopicIdeationJob))


@pytest.fixture
def seed_job(session, project: str):
    """A finished job and its ideas, as the ideation contracts leave them."""

    def factory(
        *, project_id: str | None = None, user, ideas: int = 2, created_at=None
    ) -> TopicIdeationJob:
        now = created_at or datetime.now(timezone.utc)
        job = TopicIdeationJob(
            id=uuid.uuid4(),
            project_id=project_id or project,
            requested_by_user_id=user.id,
            status=TopicIdeationJobStatus.SUCCEEDED,
            idea_count=max(ideas, 1),
            finished_at=now,
            created_at=now,
        )
        session.add(job)
        session.flush()
        for position in range(1, ideas + 1):
            session.add(
                ContentIdea(
                    project_id=job.project_id,
                    ideation_job_id=job.id,
                    position=position,
                    title=f"Идея {position}",
                    description=f"Описание {position}",
                    angle=None if position % 2 else "Угол",
                    created_at=now,
                )
            )
        session.commit()
        return job

    return factory


# -- the feature gate (off by default) ---------------------------------------


def test_the_gate_is_off_by_default_and_not_switchable_by_environment(
    monkeypatch,
) -> None:
    monkeypatch.setenv("AI_SMM_TOPIC_IDEATION_JOBS_ENABLED", "true")
    monkeypatch.setenv("AI_SMM_TOPIC_IDEATION_ENABLED", "true")

    assert load_settings().topic_ideation_jobs_enabled is False


@pytest.fixture
def default_client(api_settings, api_session_factory):
    from fastapi.testclient import TestClient

    from ai_smm.api.app import create_app

    app = create_app(
        replace(api_settings, topic_ideation_jobs_enabled=False),
        session_factory=api_session_factory,
        access_log=False,
    )
    with TestClient(app) as test_client:
        yield test_client


def test_queuing_fails_closed_while_the_gate_is_off(
    default_client, open_session, session, member, project: str
) -> None:
    csrf = open_session(default_client, member(MembershipRole.OWNER))

    response = _create(default_client, project, csrf, {"idea_count": 2})

    assert response.status_code == 403, response.text
    error = response.json()["error"]
    assert error["code"] == "FEATURE_DISABLED"
    assert error["details"] == {"feature": "topic_ideation"}
    assert _job_count(session) == 0
    assert session.scalar(
        select(func.count()).select_from(AuditLog).where(
            AuditLog.action.like("generation.ideation%")
        )
    ) == 0


def test_reads_work_while_the_gate_is_off(
    default_client, open_session, member, seed_job, project: str
) -> None:
    owner = member(MembershipRole.OWNER)
    seed_job(user=owner)
    open_session(default_client, owner)

    jobs = default_client.get(f"/api/v1/projects/{project}/topic-ideation-jobs")
    ideas = default_client.get(f"/api/v1/projects/{project}/content-ideas")

    assert jobs.status_code == 200 and jobs.json()["total"] == 1
    assert ideas.status_code == 200 and ideas.json()["total"] == 2


def test_both_switches_refuse_before_the_database_is_touched() -> None:
    # db=None: any database access would raise AttributeError instead.
    with pytest.raises(MutationsDisabled):
        create_topic_ideation_job(
            None,
            project=None,
            requested_by_user=None,
            mutations_enabled=False,
            ideation_enabled=True,
            idea_count=1,
        )
    with pytest.raises(TopicIdeationDisabled):
        create_topic_ideation_job(
            None,
            project=None,
            requested_by_user=None,
            mutations_enabled=True,
            ideation_enabled=False,
            idea_count=1,
        )


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


def test_the_mutation_gate_refuses_first(
    read_only_client, open_session, session, member, project: str
) -> None:
    csrf = open_session(read_only_client, member(MembershipRole.OWNER))

    response = _create(read_only_client, project, csrf)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "MUTATIONS_DISABLED"
    assert _job_count(session) == 0


# -- queuing (gate on) ------------------------------------------------------


def test_an_owner_can_queue_a_job(
    client, login, member, session, project: str
) -> None:
    owner = member(MembershipRole.OWNER)
    csrf = login(owner)

    response = _create(
        client, project, csrf, {"idea_count": 4, "topic_hint": "  весенние рецепты  "}
    )

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["project_id"] == project
    assert body["requested_by_user_id"] == str(owner.id)
    assert body["status"] == "queued"
    assert body["idea_count"] == 4
    assert body["topic_hint"] == "весенние рецепты"
    assert body["attempt_count"] == 0
    assert body["max_attempts"] == 2
    assert body["result"] == {}
    assert body["last_error"] is None
    assert body["finished_at"] is None
    assert set(body) == {
        "id", "project_id", "requested_by_user_id", "status", "idea_count",
        "topic_hint", "result", "last_error", "attempt_count", "max_attempts",
        "created_at", "updated_at", "finished_at",
    }

    audit = session.scalars(
        select(AuditLog).where(AuditLog.action == "generation.ideation_queued")
    ).one()
    assert audit.actor == f"user:{owner.id}"
    assert audit.subject == f"topic_ideation_job:{body['id']}"
    assert audit.details["idea_count"] == 4


def test_defaults_and_a_blank_hint(client, login, member, project: str) -> None:
    csrf = login(member(MembershipRole.OWNER))

    response = _create(client, project, csrf, {"topic_hint": "   "})

    assert response.status_code == 202, response.text
    assert response.json()["idea_count"] == 3
    assert response.json()["topic_hint"] is None


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"idea_count": 0}, id="zero"),
        pytest.param({"idea_count": 6}, id="six"),
        pytest.param({"idea_count": "3"}, id="string-count"),
        pytest.param({"idea_count": True}, id="bool-count"),
        pytest.param({"idea_count": 2.5}, id="float-count"),
        pytest.param({"idea_count": None}, id="null-count"),
        pytest.param({"topic_hint": "x" * 501}, id="hint-too-long"),
        pytest.param({"topic_hint": 42}, id="hint-not-text"),
        pytest.param({"status": "succeeded"}, id="status"),
        pytest.param({"ideas": [{"title": "t", "description": "d"}]}, id="ideas"),
        pytest.param({"project_id": "other-project"}, id="project_id"),
        pytest.param({"max_attempts": 10}, id="max_attempts"),
        pytest.param({"result": {"idea_ids": []}}, id="result"),
    ],
)
def test_invalid_or_privileged_input_is_refused(
    client, login, member, session, project: str, payload: dict
) -> None:
    csrf = login(member(MembershipRole.OWNER))

    response = _create(client, project, csrf, payload)

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert _job_count(session) == 0


def test_a_non_object_body_is_refused(client, login, member, project: str) -> None:
    csrf = login(member(MembershipRole.OWNER))

    response = client.post(
        f"/api/v1/projects/{project}/topic-ideation-jobs",
        json=[1, 2, 3],
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    "role", [MembershipRole.VIEWER, MembershipRole.EDITOR, MembershipRole.ADMIN]
)
def test_below_owner_may_not_queue(
    client, login, member, session, project: str, role
) -> None:
    csrf = login(member(role))

    response = _create(client, project, csrf, {"idea_count": 1})

    assert response.status_code == 403
    error = response.json()["error"]
    assert error["code"] == "FORBIDDEN"
    assert error["details"] == {"required_role": "owner", "your_role": role.value}
    assert _job_count(session) == 0


@pytest.mark.parametrize(
    "role",
    [
        MembershipRole.VIEWER,
        MembershipRole.EDITOR,
        MembershipRole.ADMIN,
        MembershipRole.OWNER,
    ],
)
def test_every_member_role_may_read(
    client, login, member, seed_job, project: str, role
) -> None:
    job = seed_job(user=member(MembershipRole.OWNER), ideas=1)
    login(member(role))

    jobs = client.get(f"/api/v1/projects/{project}/topic-ideation-jobs")
    detail = client.get(f"/api/v1/projects/{project}/topic-ideation-jobs/{job.id}")
    ideas = client.get(f"/api/v1/projects/{project}/content-ideas")

    assert jobs.status_code == 200 and jobs.json()["total"] == 1
    assert detail.status_code == 200
    assert ideas.status_code == 200 and ideas.json()["total"] == 1


def test_a_non_member_cannot_tell_the_project_exists(
    client, login, make_user, make_other_project, session
) -> None:
    csrf = login(make_user())

    response = _create(client, make_other_project, csrf)

    assert response.status_code == 404
    assert _job_count(session) == 0


def test_an_inactive_project_gets_no_job(
    client, login, member, session, project: str
) -> None:
    csrf = login(member(MembershipRole.OWNER))
    session.execute(
        text("UPDATE projects SET is_active = false WHERE id = :p"), {"p": project}
    )
    session.commit()

    response = _create(client, project, csrf)

    assert response.status_code == 409
    assert response.json()["error"]["details"] == {"reason": "project_inactive"}
    assert _job_count(session) == 0


def test_an_unauthenticated_request_is_refused(client, project: str) -> None:
    response = client.post(
        f"/api/v1/projects/{project}/topic-ideation-jobs", json={}
    )

    assert response.status_code in (401, 403)


# -- reads -------------------------------------------------------------------


def test_a_viewer_reads_jobs_and_ideas(
    client, login, member, seed_job, project: str
) -> None:
    owner = member(MembershipRole.OWNER)
    job = seed_job(user=owner, ideas=3)
    login(member(MembershipRole.VIEWER))

    listing = client.get(f"/api/v1/projects/{project}/topic-ideation-jobs")
    detail = client.get(f"/api/v1/projects/{project}/topic-ideation-jobs/{job.id}")
    ideas = client.get(f"/api/v1/projects/{project}/content-ideas")

    assert listing.status_code == 200
    assert [item["id"] for item in listing.json()["items"]] == [str(job.id)]
    assert detail.status_code == 200
    assert detail.json()["status"] == "succeeded"
    body = ideas.json()
    assert body["total"] == 3
    first = sorted(body["items"], key=lambda i: i["position"])[0]
    assert set(first) == {
        "id", "project_id", "ideation_job_id", "position", "title",
        "description", "angle", "status", "generation_job_id",
        "created_at", "updated_at",
    }
    assert first["ideation_job_id"] == str(job.id)
    assert first["status"] == "proposed"
    assert first["generation_job_id"] is None


def test_another_projects_rows_are_invisible(
    client, login, member, make_user, make_membership, make_other_project,
    seed_job, project: str,
) -> None:
    viewer = member(MembershipRole.VIEWER)
    other_owner = make_user()
    make_membership(
        user=other_owner, project_id=make_other_project, role=MembershipRole.OWNER
    )
    foreign = seed_job(project_id=make_other_project, user=other_owner)
    login(viewer)

    own_jobs = client.get(f"/api/v1/projects/{project}/topic-ideation-jobs")
    own_ideas = client.get(f"/api/v1/projects/{project}/content-ideas")
    by_id = client.get(
        f"/api/v1/projects/{project}/topic-ideation-jobs/{foreign.id}"
    )
    by_job = client.get(
        f"/api/v1/projects/{project}/content-ideas",
        params={"ideation_job_id": str(foreign.id)},
    )
    their_project = client.get(
        f"/api/v1/projects/{make_other_project}/topic-ideation-jobs/{foreign.id}"
    )

    assert own_jobs.json()["total"] == 0
    assert own_ideas.json()["total"] == 0
    assert by_id.status_code == 404
    assert by_job.json()["total"] == 0
    assert their_project.status_code == 404


def test_an_unknown_job_is_404(client, login, member, project: str) -> None:
    login(member(MembershipRole.VIEWER))

    response = client.get(
        f"/api/v1/projects/{project}/topic-ideation-jobs/{uuid.uuid4()}"
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_jobs_are_paginated_newest_first_and_filterable(
    client, login, member, seed_job, session, project: str
) -> None:
    owner = member(MembershipRole.OWNER)
    now = datetime.now(timezone.utc)
    jobs = [
        seed_job(user=owner, ideas=0, created_at=now - timedelta(minutes=n))
        for n in range(3)
    ]
    session.execute(
        text(
            "UPDATE topic_ideation_jobs SET status = 'failed' WHERE id = :id"
        ),
        {"id": jobs[2].id},
    )
    session.commit()
    login(owner)

    page = client.get(
        f"/api/v1/projects/{project}/topic-ideation-jobs",
        params={"limit": 2, "offset": 0},
    ).json()
    rest = client.get(
        f"/api/v1/projects/{project}/topic-ideation-jobs",
        params={"limit": 2, "offset": 2},
    ).json()
    failed = client.get(
        f"/api/v1/projects/{project}/topic-ideation-jobs",
        params={"status": "failed"},
    ).json()

    assert page["total"] == 3 and page["limit"] == 2 and page["offset"] == 0
    assert [i["id"] for i in page["items"]] == [str(jobs[0].id), str(jobs[1].id)]
    assert [i["id"] for i in rest["items"]] == [str(jobs[2].id)]
    assert [i["id"] for i in failed["items"]] == [str(jobs[2].id)]


def test_ideas_filter_by_job_and_status(
    client, login, member, seed_job, session, project: str
) -> None:
    owner = member(MembershipRole.OWNER)
    first = seed_job(user=owner, ideas=2)
    seed_job(user=owner, ideas=3)
    session.execute(
        text(
            "UPDATE content_ideas SET status = 'rejected'"
            " WHERE ideation_job_id = :id AND position = 1"
        ),
        {"id": first.id},
    )
    session.commit()
    login(owner)

    by_job = client.get(
        f"/api/v1/projects/{project}/content-ideas",
        params={"ideation_job_id": str(first.id)},
    ).json()
    rejected = client.get(
        f"/api/v1/projects/{project}/content-ideas",
        params={"status": ContentIdeaStatus.REJECTED.value},
    ).json()
    bad = client.get(
        f"/api/v1/projects/{project}/content-ideas", params={"status": "nonsense"}
    )

    assert by_job["total"] == 2
    assert [i["position"] for i in by_job["items"]] == [1, 2]
    assert rejected["total"] == 1
    assert bad.status_code == 422


def test_there_is_no_route_that_changes_an_idea(
    client, login, member, seed_job, project: str
) -> None:
    owner = member(MembershipRole.OWNER)
    seed_job(user=owner, ideas=1)
    csrf = login(owner)
    idea_id = client.get(
        f"/api/v1/projects/{project}/content-ideas"
    ).json()["items"][0]["id"]

    for method, path in [
        ("POST", f"/api/v1/projects/{project}/content-ideas"),
        ("PATCH", f"/api/v1/projects/{project}/content-ideas/{idea_id}"),
        ("POST", f"/api/v1/content-ideas/{idea_id}/reject"),
        ("POST", f"/api/v1/content-ideas/{idea_id}/use"),
        ("DELETE", f"/api/v1/projects/{project}/topic-ideation-jobs"),
    ]:
        response = client.request(method, path, headers={"X-CSRF-Token": csrf})
        assert response.status_code in (404, 405), (method, path)
