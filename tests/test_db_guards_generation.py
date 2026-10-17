"""Database guards of content_generation_jobs (SMM-026A/2.1).

Same fixtures and style as tests/test_db_guards.py: a session-bound
ai_smm_cc_rw transaction (`rw`/`bound`) for the business-mutation path the
API uses, and the plain owner-role `session` fixture for what the
generation worker does -- claiming, writing results, as the table owner,
exactly like ai_smm.worker already does for publications.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from ai_smm.application.content import RevisionInput, create_content_item
from ai_smm.application.generation import cancel_generation_job, create_generation_job
from ai_smm.db.models import (
    ContentGenerationJob,
    GenerationJobStatus,
    MembershipRole,
    User,
)
from ai_smm.generation_queue import claim_due_generation_job, mark_generation_succeeded
from tests.test_db_guards import bound, owner, rw, token_for  # noqa: F401 -- fixtures
from tests.test_db_least_privilege import INSUFFICIENT_PRIVILEGE, _sqlstate


def _refused(raised: pytest.ExceptionInfo[DBAPIError], fragment: str = "") -> None:
    assert _sqlstate(raised.value) == INSUFFICIENT_PRIVILEGE, raised.value
    assert "guard: " in str(raised.value) or "permission denied" in str(raised.value)

    if fragment:
        assert fragment in str(raised.value), raised.value


# -- who may create a job ---------------------------------------------------


def test_api_role_cannot_forge_generation_provenance(
    rw, owner, project: str, token_for  # noqa: F811
) -> None:
    with bound(rw, token_for(owner)) as db, pytest.raises(DBAPIError) as raised:
        db.execute(
            text(
                "INSERT INTO content_items"
                " (id, project_id, title, content_type, status,"
                " current_revision_id, version, created_by_user_id,"
                " generation_job_id) VALUES"
                " (gen_random_uuid(), :p, 'forged', 'post', 'draft',"
                " gen_random_uuid(), 1, :u, gen_random_uuid())"
            ),
            {"p": project, "u": str(owner.id)},
        )

    _refused(raised, "generation provenance belongs to the generation worker")


def test_api_role_cannot_link_content_to_another_projects_job(
    rw, session, owner, make_other_project, make_user, token_for  # noqa: F811
) -> None:
    other_owner = make_user()
    other_project = make_other_project
    job = ContentGenerationJob(
        id=uuid.uuid4(),
        project_id=other_project,
        requested_by_user_id=other_owner.id,
        status=GenerationJobStatus.QUEUED,
    )
    session.add(job)
    session.commit()

    with bound(rw, token_for(owner)) as db, pytest.raises(DBAPIError) as raised:
        db.execute(
            text(
                "INSERT INTO content_items"
                " (id, project_id, title, content_type, status,"
                " current_revision_id, version, created_by_user_id,"
                " generation_job_id) VALUES"
                " (gen_random_uuid(), 'test-project', 'cross-project',"
                " 'post', 'draft', gen_random_uuid(), 1, :u, :job)"
            ),
            {"u": str(owner.id), "job": str(job.id)},
        )

    _refused(raised, "generation provenance belongs to the generation worker")


def test_generation_provenance_stays_immutable_even_if_update_is_granted(
    rw, session, owner, project: str, token_for  # noqa: F811
) -> None:
    item, _ = create_content_item(
        session,
        project_id=project,
        title="manual",
        content_type="post",
        revision=RevisionInput(body="manual body", format="text"),
        created_by_user=owner,
    )
    session.commit()

    # Prove the trigger is the invariant, independent of today's narrower
    # column matrix. GRANT/REVOKE are confined to this disposable test DB.
    session.execute(
        text(
            "GRANT UPDATE (generation_job_id) ON content_items"
            " TO ai_smm_cc_rw"
        )
    )
    session.commit()
    try:
        with bound(rw, token_for(owner)) as db, pytest.raises(DBAPIError) as raised:
            db.execute(
                text(
                    "UPDATE content_items SET generation_job_id = gen_random_uuid()"
                    " WHERE id = :id"
                ),
                {"id": str(item.id)},
            )
        _refused(raised, "generation provenance is immutable")
    finally:
        session.execute(
            text("REVOKE UPDATE (generation_job_id) ON content_items FROM ai_smm_cc_rw")
        )
        session.commit()


def test_without_a_bound_session_creating_a_job_is_refused(
    rw, owner, project: str  # noqa: F811
) -> None:
    with rw() as db, pytest.raises(DBAPIError) as raised:
        create_generation_job(
            db,
            project_id=project,
            requested_by_user=owner,
            mutations_enabled=True,
        )

    _refused(raised, "session-bound transaction")


@pytest.mark.parametrize(
    "role", [MembershipRole.VIEWER, MembershipRole.EDITOR, MembershipRole.ADMIN]
)
def test_only_the_projects_owner_may_start_a_job(
    rw, make_user, make_membership, project: str, token_for, role  # noqa: F811
) -> None:
    user = make_user()
    make_membership(user=user, project_id=project, role=role)

    with bound(rw, token_for(user)) as db, pytest.raises(DBAPIError) as raised:
        create_generation_job(
            db, project_id=project, requested_by_user=user, mutations_enabled=True
        )

    _refused(raised, "reserved to its owner")


def test_the_requester_must_be_the_bound_user(
    rw, owner, make_user, make_membership, project: str, token_for  # noqa: F811
) -> None:
    other = make_user()
    make_membership(user=other, project_id=project, role=MembershipRole.OWNER)

    with bound(rw, token_for(owner)) as db, pytest.raises(DBAPIError) as raised:
        create_generation_job(
            db, project_id=project, requested_by_user=other, mutations_enabled=True
        )

    _refused(raised, "the requester must be the bound user")


def test_an_owner_bound_session_may_start_a_job(
    rw, owner, project: str, token_for  # noqa: F811
) -> None:
    with bound(rw, token_for(owner)) as db:
        job = create_generation_job(
            db, project_id=project, requested_by_user=owner, mutations_enabled=True
        )
        db.commit()

    assert job.status is GenerationJobStatus.QUEUED
    assert job.claimed_by is None


def test_a_fresh_job_cannot_arrive_pre_claimed_or_finished(
    rw, owner, project: str, token_for  # noqa: F811
) -> None:
    """The guard's own shape check: a caller cannot hand it a job that
    already looks claimed, finished or carries a non-empty result -- the
    same discipline guard_publications_insert enforces for publications."""

    with bound(rw, token_for(owner)) as db, pytest.raises(DBAPIError) as raised:
        db.execute(
            text(
                "INSERT INTO content_generation_jobs"
                " (id, project_id, requested_by_user_id, status, claimed_by)"
                " VALUES (gen_random_uuid(), :p, :u, 'queued', 'sneaky')"
            ),
            {"p": project, "u": str(owner.id)},
        )

    _refused(raised, "freshly queued")


# -- who may cancel a job ----------------------------------------------------


def test_cancelling_someone_elses_project_job_is_refused(
    rw, owner, make_user, make_membership, make_other_project, project: str, token_for  # noqa: F811
) -> None:
    with bound(rw, token_for(owner)) as db:
        job = create_generation_job(
            db, project_id=project, requested_by_user=owner, mutations_enabled=True
        )
        db.commit()

    stranger = make_user()
    make_membership(
        user=stranger, project_id=make_other_project, role=MembershipRole.OWNER
    )

    with bound(rw, token_for(stranger)) as db, pytest.raises(DBAPIError) as raised:
        db.execute(
            text(
                "UPDATE content_generation_jobs SET status = 'cancelled'"
                " WHERE id = :id"
            ),
            {"id": str(job.id)},
        )

    _refused(raised, "reserved to its owner")


def test_only_queued_or_claimed_may_be_cancelled(
    rw, session, owner, project: str, token_for  # noqa: F811
) -> None:
    with bound(rw, token_for(owner)) as db:
        job = create_generation_job(
            db, project_id=project, requested_by_user=owner, mutations_enabled=True
        )
        db.commit()
        job_id = job.id

    # The worker finishes it, as the owner role (unguarded).
    claimed = claim_due_generation_job(session, worker_id="gw-1", lease_seconds=600)
    assert claimed.id == job_id
    session.commit()
    mark_generation_succeeded(
        session,
        claimed,
        content_item_ids=[],
        series_title="",
        revision_count=0,
        tokens={},
        cost_usd=0.0,
        seconds=0.0,
        actor="worker:gw-1",
    )
    session.commit()

    with bound(rw, token_for(owner)) as db, pytest.raises(DBAPIError) as raised:
        db.execute(
            text(
                "UPDATE content_generation_jobs SET status = 'cancelled'"
                " WHERE id = :id"
            ),
            {"id": str(job_id)},
        )

    _refused(raised, "is not a Control Center command")


def test_the_worker_owned_columns_cannot_be_tampered_with_through_cancel(
    rw, owner, project: str, token_for  # noqa: F811
) -> None:
    with bound(rw, token_for(owner)) as db:
        job = create_generation_job(
            db, project_id=project, requested_by_user=owner, mutations_enabled=True
        )
        db.commit()
        job_id = job.id

    with bound(rw, token_for(owner)) as db, pytest.raises(DBAPIError) as raised:
        db.execute(
            text(
                "UPDATE content_generation_jobs"
                " SET status = 'cancelled', claimed_by = 'hacked'"
                " WHERE id = :id"
            ),
            {"id": str(job_id)},
        )

    _refused(raised, "belong to the worker")


def test_an_owner_bound_session_may_cancel_their_own_job(
    rw, owner, project: str, token_for  # noqa: F811
) -> None:
    with bound(rw, token_for(owner)) as db:
        job = create_generation_job(
            db, project_id=project, requested_by_user=owner, mutations_enabled=True
        )
        db.commit()
        job_id = job.id

    with bound(rw, token_for(owner)) as db:
        cancelled = cancel_generation_job(
            db,
            job_id=job_id,
            expected_project_id=project,
            actor_user=owner,
            mutations_enabled=True,
        )
        db.commit()

    assert cancelled.status is GenerationJobStatus.CANCELLED
    assert cancelled.finished_at is not None


# -- the worker is not guarded -----------------------------------------------


def test_the_generation_worker_is_not_guarded(  # noqa: F811
    session: Session, owner: User, project: str  # noqa: F811
) -> None:
    """Claiming and completing a job, as the owner role, is untouched by
    the guards above -- exactly how ai_smm.worker claims publications."""

    job = ContentGenerationJob(
        id=uuid.uuid4(),
        project_id=project,
        requested_by_user_id=owner.id,
        status=GenerationJobStatus.QUEUED,
    )
    session.add(job)
    session.commit()

    claimed = claim_due_generation_job(session, worker_id="gw-1", lease_seconds=600)
    assert claimed is not None and claimed.id == job.id
    assert claimed.status is GenerationJobStatus.CLAIMED
    session.commit()

    mark_generation_succeeded(
        session,
        claimed,
        content_item_ids=["11111111-1111-1111-1111-111111111111"],
        series_title="x",
        revision_count=1,
        tokens={"input": 1},
        cost_usd=0.01,
        seconds=1.0,
        actor="worker:gw-1",
    )
    session.commit()

    session.expire_all()
    stored = session.get(ContentGenerationJob, job.id)
    assert stored.status is GenerationJobStatus.SUCCEEDED
