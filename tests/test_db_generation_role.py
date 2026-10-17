"""Independent privilege matrix for the Agent -> Draft PostgreSQL role."""
from __future__ import annotations

import json
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from ai_smm.application.content import content_hash
from ai_smm.db.models import (
    ContentGenerationJob,
    ContentItem,
    ContentRevision,
    GenerationJobStatus,
)
from ai_smm.generation_queue import claim_due_generation_job, complete_generation_job
from ai_smm.generation_worker import GenerationWorker
from tests import pg_roles
from tests.test_generation_worker import FakeGraph, _good_result


_COMPLETE_SQL = text(
    "SELECT public.generation_complete("
    ":job_id, :worker_id, :run_id, CAST(:drafts AS jsonb), '{}'::jsonb)"
)

_FAIL_SQL = text(
    "SELECT public.generation_fail("
    ":job_id, :worker_id, 'forced failure', false, 3, "
    "'DirectContractTest', 1, 1)"
)


def _contract_draft(*, body: str, title: str = "safe") -> dict[str, object]:
    return {
        "title": title,
        "body": body,
        "format": "text",
        "images": [],
        "items": [],
        "metadata": {},
        "content_hash": content_hash(
            title=None, body=body, format="text", images=[], items=[]
        ),
    }


def _complete_direct(
    db: Session,
    *,
    job_id: uuid.UUID,
    worker_id: str,
    drafts: list[dict[str, object]],
) -> dict[str, object]:
    value = db.execute(
        _COMPLETE_SQL,
        {
            "job_id": job_id,
            "worker_id": worker_id,
            "run_id": uuid.uuid4(),
            "drafts": json.dumps(drafts, ensure_ascii=False),
        },
    ).scalar_one()
    return dict(value)


def test_generation_role_definition_and_operator_verify(
    generation_role, admin_database_url: str
) -> None:
    rows = pg_roles.query_admin(
        admin_database_url,
        "SELECT rolcanlogin, rolsuper, rolinherit, rolcreaterole, rolcreatedb,"
        " rolreplication, rolbypassrls FROM pg_roles"
        " WHERE rolname = 'ai_smm_generation'",
    )
    assert rows == [(True, False, False, False, False, False, False)]
    assert pg_roles.failures(pg_roles.verify_generation(admin_database_url)) == []


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT password_hash FROM users",
        "SELECT ciphertext FROM social_account_credentials",
        "INSERT INTO content_approvals (id) VALUES (gen_random_uuid())",
        "INSERT INTO publications (project_id) VALUES ('test-project')",
        "UPDATE publications SET status = 'cancelled'",
        "SELECT * FROM user_sessions",
    ],
)
def test_generation_role_has_no_unrelated_table_capability(
    generation_session_factory: sessionmaker[Session], statement: str
) -> None:
    with generation_session_factory() as db, pytest.raises(DBAPIError):
        db.execute(text(statement))


def test_generation_contract_derives_project_and_provenance_from_job(
    session: Session,
    generation_session_factory: sessionmaker[Session],
    project: str,
    make_other_project: str,
    make_user,
) -> None:
    requester = make_user()
    job = ContentGenerationJob(
        id=uuid.uuid4(), project_id=project,
        requested_by_user_id=requester.id, status=GenerationJobStatus.QUEUED,
    )
    session.add(job)
    session.commit()

    with generation_session_factory() as worker_db:
        claimed = claim_due_generation_job(
            worker_db, worker_id="role-worker", lease_seconds=600, max_attempts=3
        )
        assert claimed is not None
        worker_db.commit()
        body = "contract draft"
        drafts = [{
            "project_id": make_other_project,  # ignored, not a contract field
            **_contract_draft(body=body),
            "metadata": {"generation_job_id": str(uuid.uuid4())},
        }]
        outcome = complete_generation_job(
            worker_db,
            job_id=claimed.id,
            worker_id="role-worker",
            run_id=uuid.uuid4(),
            drafts_json=json.dumps(drafts),
            result_json="{}",
        )
        assert outcome["status"] == "succeeded"
        worker_db.commit()

    session.expire_all()
    item = session.scalars(
        select(ContentItem).where(ContentItem.generation_job_id == job.id)
    ).one()
    assert item.project_id == project
    assert item.project_id != make_other_project
    assert item.created_by_user_id is None


@pytest.mark.parametrize("body", ["", "   ", "\n\t"])
def test_generation_contract_rejects_empty_text_directly_for_generation_role(
    session: Session,
    generation_session_factory: sessionmaker[Session],
    project: str,
    make_user,
    body: str,
) -> None:
    job = ContentGenerationJob(
        id=uuid.uuid4(), project_id=project,
        requested_by_user_id=make_user().id, status=GenerationJobStatus.QUEUED,
    )
    session.add(job)
    session.commit()

    with generation_session_factory() as worker_db:
        claimed = claim_due_generation_job(
            worker_db, worker_id="hash-worker", lease_seconds=600, max_attempts=3
        )
        assert claimed is not None
        worker_db.commit()

        with pytest.raises(DBAPIError, match="invalid draft payload"):
            _complete_direct(
                worker_db,
                job_id=job.id,
                worker_id="hash-worker",
                drafts=[_contract_draft(body=body)],
            )
        worker_db.rollback()


def test_generation_contract_rejects_hash_for_another_text(
    session: Session,
    generation_session_factory: sessionmaker[Session],
    project: str,
    make_user,
) -> None:
    job = ContentGenerationJob(
        id=uuid.uuid4(), project_id=project,
        requested_by_user_id=make_user().id, status=GenerationJobStatus.QUEUED,
    )
    session.add(job)
    session.commit()

    draft = _contract_draft(body="actual body")
    draft["content_hash"] = content_hash(
        title=None, body="another body", format="text", images=[], items=[]
    )

    with generation_session_factory() as worker_db:
        claimed = claim_due_generation_job(
            worker_db, worker_id="hash-worker", lease_seconds=600, max_attempts=3
        )
        assert claimed is not None
        worker_db.commit()

        with pytest.raises(DBAPIError, match="content hash mismatch"):
            _complete_direct(
                worker_db,
                job_id=job.id,
                worker_id="hash-worker",
                drafts=[draft],
            )
        worker_db.rollback()

    assert session.scalars(
        select(ContentItem).where(ContentItem.generation_job_id == job.id)
    ).all() == []


@pytest.mark.parametrize(
    ("field", "value"),
    [("images", [{"url": "https://example.invalid/a.jpg"}]),
     ("items", [{"order": 1, "text": "nested"}])],
)
def test_generation_contract_rejects_nonempty_rich_content_directly(
    session: Session,
    generation_session_factory: sessionmaker[Session],
    project: str,
    make_user,
    field: str,
    value: list[dict[str, object]],
) -> None:
    job = ContentGenerationJob(
        id=uuid.uuid4(), project_id=project,
        requested_by_user_id=make_user().id, status=GenerationJobStatus.QUEUED,
    )
    session.add(job)
    session.commit()

    draft = _contract_draft(body="actual body")
    draft[field] = value

    with generation_session_factory() as worker_db:
        claimed = claim_due_generation_job(
            worker_db, worker_id="hash-worker", lease_seconds=600, max_attempts=3
        )
        assert claimed is not None
        worker_db.commit()

        with pytest.raises(DBAPIError, match="invalid draft payload"):
            _complete_direct(
                worker_db,
                job_id=job.id,
                worker_id="hash-worker",
                drafts=[draft],
            )
        worker_db.rollback()

    assert session.scalars(
        select(ContentItem).where(ContentItem.generation_job_id == job.id)
    ).all() == []


def test_generation_contract_hashes_unicode_and_multiple_drafts_in_database(
    session: Session,
    generation_session_factory: sessionmaker[Session],
    project: str,
    make_user,
) -> None:
    job = ContentGenerationJob(
        id=uuid.uuid4(), project_id=project,
        requested_by_user_id=make_user().id, status=GenerationJobStatus.QUEUED,
    )
    session.add(job)
    session.commit()
    bodies = ["Привет, мир 🌍\n漢字", "e\u0301 and é — разные Unicode формы"]
    drafts = [
        _contract_draft(body=body, title=f"Часть {position}")
        for position, body in enumerate(bodies, start=1)
    ]

    with generation_session_factory() as worker_db:
        claimed = claim_due_generation_job(
            worker_db, worker_id="hash-worker", lease_seconds=600, max_attempts=3
        )
        assert claimed is not None
        worker_db.commit()
        outcome = _complete_direct(
            worker_db,
            job_id=job.id,
            worker_id="hash-worker",
            drafts=drafts,
        )
        assert outcome["status"] == "succeeded"
        worker_db.commit()

    revisions = session.scalars(
        select(ContentRevision)
        .join(ContentItem, ContentItem.id == ContentRevision.content_item_id)
        .where(ContentItem.generation_job_id == job.id)
        .order_by(ContentRevision.body)
    ).all()
    assert len(revisions) == 2
    assert {revision.body for revision in revisions} == set(bodies)
    assert {
        revision.content_hash for revision in revisions
    } == {
        content_hash(
            title=None, body=body, format="text", images=[], items=[]
        )
        for body in bodies
    }


@pytest.mark.parametrize("expired", [False, True])
def test_generation_fail_rejects_wrong_worker_or_expired_lease_directly(
    session: Session,
    generation_session_factory: sessionmaker[Session],
    project: str,
    make_user,
    expired: bool,
) -> None:
    job = ContentGenerationJob(
        id=uuid.uuid4(), project_id=project,
        requested_by_user_id=make_user().id, status=GenerationJobStatus.QUEUED,
    )
    session.add(job)
    session.commit()

    with generation_session_factory() as worker_db:
        claimed = claim_due_generation_job(
            worker_db, worker_id="lease-owner", lease_seconds=600, max_attempts=3
        )
        assert claimed is not None
        worker_db.commit()

    session.expire_all()
    stored = session.get(ContentGenerationJob, job.id)
    assert stored is not None
    if expired:
        stored.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.commit()

    with generation_session_factory() as worker_db:
        status = worker_db.execute(
            _FAIL_SQL,
            {
                "job_id": job.id,
                "worker_id": "lease-owner" if expired else "wrong-worker",
            },
        ).scalar_one()
        assert status == "claimed"
        worker_db.commit()

    session.expire_all()
    stored = session.get(ContentGenerationJob, job.id)
    assert stored is not None
    assert stored.status is GenerationJobStatus.CLAIMED
    assert stored.claimed_by == "lease-owner"
    assert stored.last_error is None
    assert stored.attempt_count == 1


def test_real_worker_succeeds_using_only_generation_role(
    session: Session,
    generation_role,
    settings,
    project: str,
    make_user,
) -> None:
    requester = make_user()
    job = ContentGenerationJob(
        id=uuid.uuid4(), project_id=project,
        requested_by_user_id=requester.id, status=GenerationJobStatus.QUEUED,
    )
    session.add(job)
    session.commit()
    worker = GenerationWorker(
        settings=replace(settings, database_url=generation_role.url),
        graph_factory=lambda: FakeGraph(_good_result()),
        knowledge_loader=lambda project_id: {"project_id": project_id},
    )

    assert worker.process_one() == "succeeded"
    session.expire_all()
    assert (
        session.get(ContentGenerationJob, job.id).status
        is GenerationJobStatus.SUCCEEDED
    )
    assert len(session.scalars(
        select(ContentItem).where(ContentItem.generation_job_id == job.id)
    ).all()) == 2
