"""Generation jobs over HTTP (SMM-026A/2.1): RBAC, the mutation gate,
project isolation and the create/get/list/cancel flow.

Mirrors tests/test_api_content.py's conventions. The guard-level proof
(who may write content_generation_jobs, as which role) lives in
tests/test_db_guards_generation.py; this module is routes and RBAC only,
run with mutations enabled (the conftest.py default).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from ai_smm.application.generation import (
    InvalidGenerationParams,
    create_generation_job,
)
from ai_smm.db.models import ContentGenerationJob, GenerationJobStatus, MembershipRole


def _create(client, project: str, csrf: str, **overrides):
    return client.post(
        f"/api/v1/projects/{project}/generation-jobs",
        json=overrides,
        headers={"X-CSRF-Token": csrf},
    )


def _cancel(client, job_id, csrf: str):
    return client.post(
        f"/api/v1/generation-jobs/{job_id}/cancel",
        headers={"X-CSRF-Token": csrf},
    )


def test_an_owner_can_queue_a_job(client, login, member, project: str) -> None:
    csrf = login(member(MembershipRole.OWNER))

    response = _create(
        client,
        project,
        csrf,
        series_size=2,
        topic_hint="рецепты",
        publishing_strategy="standalone_series",
    )

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["project_id"] == project
    assert body["status"] == "queued"
    assert body["params"]["series_size"] == 2
    assert body["params"]["topic_hint"] == "рецепты"
    assert body["params"]["publishing_strategy"] == "standalone_series"
    assert body["attempt_count"] == 0
    assert body["max_attempts"] == 2
    assert body["finished_at"] is None


@pytest.mark.parametrize("params", [{}, {"publishing_strategy": None}])
def test_defaults_use_standalone_series(
    client, login, member, project: str, params: dict
) -> None:
    csrf = login(member(MembershipRole.OWNER))

    response = _create(client, project, csrf, **params)

    assert response.status_code == 202, response.text
    assert response.json()["params"]["series_size"] == 3
    assert response.json()["params"]["publishing_strategy"] == "standalone_series"


@pytest.mark.parametrize("series_size", [0, 6])
def test_series_size_out_of_range_is_refused(
    client, login, member, project: str, series_size: int
) -> None:
    csrf = login(member(MembershipRole.OWNER))

    response = _create(client, project, csrf, series_size=series_size)

    assert response.status_code == 422


@pytest.mark.parametrize("strategy", ["reply_thread", "nonsense"])
def test_an_unsupported_publishing_strategy_is_refused(
    client, login, member, project: str, strategy: str
) -> None:
    csrf = login(member(MembershipRole.OWNER))

    response = _create(client, project, csrf, publishing_strategy=strategy)

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["message"] == (
        "Only standalone_series is supported for new generation jobs."
    )
    listing = client.get(f"/api/v1/projects/{project}/generation-jobs")
    assert listing.json()["total"] == 0


def test_application_rejects_reply_thread_before_database_access() -> None:
    # Invalid creation must be refused even when bypassing the HTTP schema.
    with pytest.raises(InvalidGenerationParams, match="Only standalone_series"):
        create_generation_job(
            None,
            project_id="test-project",
            requested_by_user=None,
            mutations_enabled=True,
            publishing_strategy="reply_thread",
        )


def test_existing_reply_thread_job_remains_readable(
    client, login, member, project: str, session
) -> None:
    import uuid

    owner = member(MembershipRole.OWNER)
    legacy_params = {"series_size": 2, "publishing_strategy": "reply_thread"}
    legacy_result = {"metadata": {"publishing_strategy": "reply_thread"}}
    job = ContentGenerationJob(
        id=uuid.uuid4(),
        project_id=project,
        requested_by_user_id=owner.id,
        status=GenerationJobStatus.SUCCEEDED,
        finished_at=datetime.now(timezone.utc),
        params=legacy_params,
        result=legacy_result,
    )
    session.add(job)
    session.commit()
    login(owner)

    response = client.get(f"/api/v1/generation-jobs/{job.id}")
    assert response.status_code == 200
    assert response.json()["params"] == legacy_params
    assert response.json()["result"] == legacy_result
    listing = client.get(f"/api/v1/projects/{project}/generation-jobs")
    assert listing.status_code == 200
    assert listing.json()["items"][0]["params"] == legacy_params
    assert listing.json()["items"][0]["result"] == legacy_result


# -- RBAC ---------------------------------------------------------------


@pytest.mark.parametrize(
    "role", [MembershipRole.VIEWER, MembershipRole.EDITOR, MembershipRole.ADMIN]
)
def test_members_below_owner_cannot_queue_a_job(
    client, login, member, project: str, role: MembershipRole
) -> None:
    csrf = login(member(role))

    response = _create(client, project, csrf)

    assert response.status_code == 403


def test_every_member_can_read(client, login, member, project: str) -> None:
    owner_csrf = login(member(MembershipRole.OWNER))
    job_id = _create(client, project, owner_csrf).json()["id"]

    login(member(MembershipRole.VIEWER))  # read routes need no CSRF

    response = client.get(f"/api/v1/generation-jobs/{job_id}")

    assert response.status_code == 200
    assert response.json()["id"] == job_id

    listing = client.get(f"/api/v1/projects/{project}/generation-jobs")

    assert listing.status_code == 200
    assert listing.json()["total"] == 1


@pytest.mark.parametrize(
    "role", [MembershipRole.VIEWER, MembershipRole.EDITOR, MembershipRole.ADMIN]
)
def test_members_below_owner_cannot_cancel(
    client, login, member, project: str, role: MembershipRole
) -> None:
    owner_csrf = login(member(MembershipRole.OWNER))
    job_id = _create(client, project, owner_csrf).json()["id"]

    csrf = login(member(role))

    response = _cancel(client, job_id, csrf)

    assert response.status_code == 403


# -- project isolation ----------------------------------------------------


def test_create_cannot_name_another_project_in_the_body(
    client, login, member, project: str, make_other_project
) -> None:
    csrf = login(member(MembershipRole.OWNER))

    response = client.post(
        f"/api/v1/projects/{make_other_project}/generation-jobs",
        json={},
        headers={"X-CSRF-Token": csrf},
    )

    # Not a member of make_other_project at all.
    assert response.status_code in (403, 404)


def test_a_non_member_cannot_read_or_cancel_another_projects_job(
    client, login, member, project: str, make_other_project, make_user, make_membership
) -> None:
    owner_csrf = login(member(MembershipRole.OWNER))
    job_id = _create(client, project, owner_csrf).json()["id"]

    stranger = make_user()
    make_membership(
        user=stranger,
        project_id=make_other_project,
        role=MembershipRole.OWNER,
    )
    stranger_csrf = login(stranger)

    assert client.get(f"/api/v1/generation-jobs/{job_id}").status_code == 404
    assert _cancel(client, job_id, stranger_csrf).status_code == 404


def test_an_unknown_job_id_is_404(client, login, member, project: str) -> None:
    import uuid

    csrf = login(member(MembershipRole.OWNER))

    response = _cancel(client, uuid.uuid4(), csrf)

    assert response.status_code == 404


# The gate itself (AI_SMM_API_MUTATIONS_ENABLED=false refuses both routes,
# the database unchanged) is tests/test_api_mutation_gate.py's job --
# create_generation_job/cancel_generation_job are entries in its GATED
# dict, run with every role there.


# -- cancel -----------------------------------------------------------------


def test_cancelling_a_queued_job(client, login, member, project: str) -> None:
    csrf = login(member(MembershipRole.OWNER))
    job_id = _create(client, project, csrf).json()["id"]

    response = _cancel(client, job_id, csrf)

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert response.json()["finished_at"] is not None


def test_cancelling_twice_is_refused(client, login, member, project: str) -> None:
    csrf = login(member(MembershipRole.OWNER))
    job_id = _create(client, project, csrf).json()["id"]

    assert _cancel(client, job_id, csrf).status_code == 200

    response = _cancel(client, job_id, csrf)

    assert response.status_code == 409


def test_listing_filters_by_status(client, login, member, project: str) -> None:
    csrf = login(member(MembershipRole.OWNER))
    keep = _create(client, project, csrf).json()["id"]
    cancelled = _create(client, project, csrf).json()["id"]
    _cancel(client, cancelled, csrf)

    response = client.get(
        f"/api/v1/projects/{project}/generation-jobs",
        params={"status": GenerationJobStatus.QUEUED.value},
    )

    assert response.status_code == 200
    ids = {row["id"] for row in response.json()["items"]}
    assert ids == {keep}
