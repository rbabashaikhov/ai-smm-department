"""Topic ideation in the database (SMM-035B3A): the topic_ideation_*
contracts, the guards of topic_ideation_jobs/content_ideas, and the
privileges around both.

The contracts are called here as the table owner -- no role but the
ideation worker's own (ai_smm_ideation, SMM-035B3B1,
tests/test_db_ideation_role.py) has EXECUTE on them; the privilege tests
prove exactly that. The business-role path the API uses
is the session-bound ai_smm_cc_rw transaction of tests/test_db_guards.py.
"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from ai_smm.db.models import (
    AuditLog,
    ContentGenerationJob,
    ContentIdea,
    ContentIdeaStatus,
    GenerationJobStatus,
    MembershipRole,
    TopicIdeationJob,
    TopicIdeationJobStatus,
    User,
)
from ai_smm.generation_queue import claim_due_generation_job
from tests import pg_roles
from tests.test_db_guards import bound, owner, rw, token_for  # noqa: F401 -- fixtures
from tests.test_db_least_privilege import INSUFFICIENT_PRIVILEGE, _sqlstate


CONTRACTS = (
    "public.topic_ideation_claim(text,integer)",
    "public.topic_ideation_complete(uuid,text,jsonb,jsonb)",
    "public.topic_ideation_fail(uuid,text,text,boolean,text,integer,integer)",
)

_CLAIM = text("SELECT * FROM public.topic_ideation_claim(:worker, :lease)")
_COMPLETE = text(
    "SELECT public.topic_ideation_complete("
    ":job_id, :worker, CAST(:ideas AS jsonb), CAST(:result AS jsonb))"
)
_FAIL = text(
    "SELECT public.topic_ideation_fail("
    ":job_id, :worker, :reason, :retryable, 'TestError', :base, :cap)"
)


def _idea(n: int = 1, **overrides: object) -> dict[str, object]:
    idea: dict[str, object] = {
        "title": f"Идея {n}",
        "description": f"Описание идеи {n}",
        "angle": f"Угол {n}",
    }
    idea.update(overrides)
    return idea


@pytest.fixture
def requester(make_user) -> User:
    return make_user(display_name="Requester")


@pytest.fixture
def make_job(session: Session, project: str, requester: User):
    """An ideation job as the owner role writes it (no guard applies)."""

    def factory(
        *,
        project_id: str | None = None,
        idea_count: int = 3,
        max_attempts: int = 2,
        created_at: datetime | None = None,
        **kwargs: object,
    ) -> TopicIdeationJob:
        job = TopicIdeationJob(
            id=uuid.uuid4(),
            project_id=project_id or project,
            requested_by_user_id=requester.id,
            idea_count=idea_count,
            max_attempts=max_attempts,
            **kwargs,
        )
        if created_at is not None:
            job.created_at = created_at
        session.add(job)
        session.commit()
        return job

    return factory


def _claim(db: Session, worker: str = "ideation-1", lease: int = 60):
    params = {"worker": worker, "lease": lease}
    return db.execute(_CLAIM, params).mappings().one_or_none()


def _complete(db: Session, job_id, ideas, *, worker: str = "ideation-1", result=None):
    return db.execute(
        _COMPLETE,
        {
            "job_id": job_id,
            "worker": worker,
            "ideas": json.dumps(ideas, ensure_ascii=False),
            "result": json.dumps({} if result is None else result),
        },
    ).scalar_one()


def _fail(
    db: Session, job_id, *, worker="ideation-1", retryable=True, base=30, cap=600
):
    return db.execute(
        _FAIL,
        {
            "job_id": job_id,
            "worker": worker,
            "reason": "boom",
            "retryable": retryable,
            "base": base,
            "cap": cap,
        },
    ).scalar_one()


def _reload(session: Session, job: TopicIdeationJob) -> TopicIdeationJob:
    session.expire_all()
    return session.get(TopicIdeationJob, job.id)


def _ideas(session: Session) -> list[ContentIdea]:
    session.expire_all()
    return list(session.scalars(select(ContentIdea).order_by(ContentIdea.position)))


def _expire_lease(session: Session, job: TopicIdeationJob) -> None:
    session.execute(
        text(
            "UPDATE topic_ideation_jobs"
            " SET lease_expires_at = now() - interval '1 second'"
            " WHERE id = :id"
        ),
        {"id": job.id},
    )
    session.commit()


# -- claim -------------------------------------------------------------------


def test_claim_takes_the_oldest_due_job_and_consumes_an_attempt(
    session: Session, make_job
) -> None:
    now = datetime.now(timezone.utc)
    older = make_job(created_at=now - timedelta(minutes=5))
    make_job(created_at=now)

    row = _claim(session)
    session.commit()

    assert row["id"] == older.id
    assert row["status"] == "running"
    assert row["idea_count"] == 3
    assert row["attempt_count"] == 1
    assert row["max_attempts"] == 2
    assert row["claimed_by"] == "ideation-1"
    job = _reload(session, older)
    assert job.status == TopicIdeationJobStatus.RUNNING
    assert job.lease_expires_at > datetime.now(timezone.utc)
    assert session.scalar(
        select(func.count()).select_from(AuditLog).where(
            AuditLog.action == "generation.ideation_claimed",
            AuditLog.subject == f"topic_ideation_job:{older.id}",
        )
    ) == 1


def test_claim_returns_the_jobs_params_as_queued(
    session: Session, make_job
) -> None:
    """SMM-035B3B1: the worker reads no table; the claim hands it params."""

    params = {**VALID_PARAMS, "recent_idea_titles": ["Старая тема"]}
    job = make_job(params=params)
    legacy = make_job(created_at=datetime.now(timezone.utc) + timedelta(minutes=1))

    first = _claim(session)
    second = _claim(session)
    session.commit()

    assert (first["id"], first["params"]) == (job.id, params)
    assert (second["id"], second["params"]) == (legacy.id, {})


def test_claim_ignores_future_retries_and_non_queued_jobs(
    session: Session, make_job
) -> None:
    make_job(next_attempt_at=datetime.now(timezone.utc) + timedelta(minutes=5))
    make_job(
        status=TopicIdeationJobStatus.CANCELLED,
        finished_at=datetime.now(timezone.utc),
    )

    assert _claim(session) is None


def test_a_held_claim_is_skipped_not_waited_for(
    session_factory: sessionmaker[Session], make_job
) -> None:
    make_job()
    first, second = session_factory(), session_factory()
    try:
        assert _claim(first) is not None  # uncommitted: the row stays locked
        # Without SKIP LOCKED this would wait forever on `first`; fail fast.
        second.execute(text("SET LOCAL lock_timeout = '2s'"))
        assert _claim(second, worker="ideation-2") is None
    finally:
        first.rollback()
        second.rollback()
        first.close()
        second.close()


def test_concurrent_claims_never_take_the_same_job(
    session_factory: sessionmaker[Session], make_job
) -> None:
    jobs = {make_job().id for _ in range(4)}
    workers = 8
    barrier = threading.Barrier(workers)
    claimed: list[uuid.UUID] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker(n: int) -> None:
        db = session_factory()
        try:
            barrier.wait()
            row = _claim(db, worker=f"ideation-{n}")
            db.commit()
            if row is not None:
                with lock:
                    claimed.append(row["id"])
        except BaseException as exc:  # noqa: BLE001 -- reported below
            errors.append(exc)
        finally:
            db.close()

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors, errors
    assert sorted(claimed) == sorted(jobs)


@pytest.mark.parametrize(
    ("worker", "lease"),
    [("", 60), ("   ", 60), ("w" * 121, 60), ("ideation-1", 0), ("ideation-1", 3601)],
)
def test_claim_refuses_unbounded_parameters(
    session: Session, make_job, worker: str, lease: int
) -> None:
    make_job()

    with pytest.raises(DBAPIError) as raised:
        _claim(session, worker=worker, lease=lease)
    session.rollback()

    assert _sqlstate(raised.value) == "22023"
    assert _claim(session) is not None  # the job was left alone


# -- leases and retries ---------------------------------------------------------


def test_an_expired_lease_is_requeued_and_claimed_again(
    session: Session, make_job
) -> None:
    job = make_job(max_attempts=2)
    _claim(session)
    session.commit()
    _expire_lease(session, job)

    row = _claim(session, worker="ideation-2")
    session.commit()

    assert row["id"] == job.id
    assert row["attempt_count"] == 2
    assert row["claimed_by"] == "ideation-2"


def test_an_expired_final_claim_is_terminal(session: Session, make_job) -> None:
    job = make_job(max_attempts=1)
    _claim(session)
    session.commit()
    _expire_lease(session, job)

    assert _claim(session) is None
    session.commit()

    job = _reload(session, job)
    assert job.status == TopicIdeationJobStatus.FAILED
    assert job.finished_at is not None
    assert "lease expired" in job.last_error


def test_a_retryable_failure_is_requeued_with_backoff(
    session: Session, make_job
) -> None:
    job = make_job(max_attempts=2)
    _claim(session)
    assert _fail(session, job.id) == "queued"
    session.commit()

    job = _reload(session, job)
    assert job.status == TopicIdeationJobStatus.QUEUED
    assert job.claimed_by is None
    assert job.next_attempt_at > datetime.now(timezone.utc)
    assert job.last_error == "boom"
    assert _claim(session) is None  # not due yet


def test_failure_is_terminal_when_not_retryable_or_exhausted(
    session: Session, make_job
) -> None:
    fatal = make_job(max_attempts=3)
    _claim(session)
    assert _fail(session, fatal.id, retryable=False) == "failed"
    session.commit()

    exhausted = make_job(max_attempts=1)
    _claim(session)
    assert _fail(session, exhausted.id) == "failed"
    session.commit()

    for job in (fatal, exhausted):
        job = _reload(session, job)
        assert job.status == TopicIdeationJobStatus.FAILED
        assert job.finished_at is not None
        assert job.next_attempt_at is None


def test_fail_from_a_stale_claimant_changes_nothing(
    session: Session, make_job
) -> None:
    job = make_job()
    _claim(session)
    session.commit()

    assert _fail(session, job.id, worker="someone-else") == "running"
    session.commit()
    assert _reload(session, job).attempt_count == 1

    _expire_lease(session, job)
    assert _fail(session, job.id) == "running"


@pytest.mark.parametrize(
    ("base", "cap"), [(0, 600), (30, 3601), (601, 600), (None, 600)]
)
def test_fail_refuses_unbounded_backoff(
    session: Session, make_job, base, cap
) -> None:
    job = make_job()
    _claim(session)
    session.commit()

    with pytest.raises(DBAPIError) as raised:
        _fail(session, job.id, base=base, cap=cap)
    session.rollback()

    assert _sqlstate(raised.value) == "22023"


# -- completion --------------------------------------------------------------


def test_completion_saves_ideas_in_the_jobs_project_atomically(
    session: Session, make_job, project: str
) -> None:
    job = make_job(idea_count=3)
    _claim(session)
    session.commit()

    value = _complete(
        session,
        job.id,
        [
            _idea(1),
            _idea(2, angle=None),
            {"title": "  Третья  ", "description": "Текст"},
        ],
        result={"model": "test", "tokens": {"input": 1}},
    )
    session.commit()

    ideas = _ideas(session)
    assert value["status"] == "succeeded"
    assert value["idea_ids"] == [str(i.id) for i in ideas]
    assert [i.position for i in ideas] == [1, 2, 3]
    assert {i.project_id for i in ideas} == {project}
    assert {i.ideation_job_id for i in ideas} == {job.id}
    assert {i.status for i in ideas} == {ContentIdeaStatus.PROPOSED}
    assert ideas[1].angle is None
    assert ideas[2].title == "Третья"
    assert ideas[2].angle is None
    assert all(i.generation_job_id is None for i in ideas)

    job = _reload(session, job)
    assert job.status == TopicIdeationJobStatus.SUCCEEDED
    assert job.claimed_by is None and job.lease_expires_at is None
    assert job.finished_at is not None
    assert job.result["idea_ids"] == value["idea_ids"]
    assert job.result["model"] == "test"


def test_completion_accepts_fewer_ideas_than_requested(
    session: Session, make_job
) -> None:
    job = make_job(idea_count=5)
    _claim(session)

    assert _complete(session, job.id, [_idea(1)])["status"] == "succeeded"


def test_a_repeated_completion_is_a_no_op(session: Session, make_job) -> None:
    job = make_job(idea_count=2)
    _claim(session)
    first = _complete(session, job.id, [_idea(1), _idea(2)])
    session.commit()

    again = _complete(session, job.id, [_idea(3), _idea(4)])
    session.commit()

    assert again == first
    assert len(_ideas(session)) == 2


def test_a_lost_claim_cannot_complete(session: Session, make_job) -> None:
    job = make_job()
    _claim(session)
    session.commit()

    stale = _complete(session, job.id, [_idea()], worker="someone-else")
    assert stale["status"] == "running"
    _expire_lease(session, job)
    expired = _complete(session, job.id, [_idea()])
    assert expired == {"status": "running", "idea_ids": []}
    session.commit()

    assert _ideas(session) == []


def test_a_cancelled_job_discards_the_result(session: Session, make_job) -> None:
    job = make_job()
    _claim(session)
    session.execute(
        text(
            "UPDATE topic_ideation_jobs SET status = 'cancelled', finished_at = now()"
            " WHERE id = :id"
        ),
        {"id": job.id},
    )
    session.commit()

    value = _complete(session, job.id, [_idea()])
    assert value == {"status": "cancelled", "idea_ids": []}
    assert _ideas(session) == []


@pytest.mark.parametrize(
    "ideas",
    [
        pytest.param({"title": "x"}, id="not-an-array"),
        pytest.param([], id="empty"),
        pytest.param([_idea(n) for n in range(1, 4)], id="more-than-requested"),
        pytest.param(["just a string"], id="idea-not-an-object"),
        pytest.param(
            [_idea(1, project_id="other-project")], id="foreign-project-key"
        ),
        pytest.param(
            [_idea(1, ideation_job_id=str(uuid.uuid4()))], id="foreign-job-key"
        ),
        pytest.param([_idea(1, status="used")], id="status-key"),
        pytest.param([_idea(1, title=42)], id="title-not-text"),
        pytest.param([_idea(1, description=None)], id="description-null"),
        pytest.param([{"title": "only a title"}], id="description-missing"),
        pytest.param([_idea(1, angle=["list"])], id="angle-not-text"),
        pytest.param([_idea(1, title=" \n\t ")], id="title-blank"),
        pytest.param([_idea(1, title="x" * 201)], id="title-too-long"),
        pytest.param([_idea(1, description="x" * 2001)], id="description-too-long"),
        pytest.param([_idea(1, angle="x" * 1001)], id="angle-too-long"),
        pytest.param([_idea(1), _idea(1, title="ИДЕЯ 1")], id="duplicate-title"),
        pytest.param([_idea(1), _idea(2, title="")], id="valid-then-invalid"),
    ],
)
def test_malformed_output_saves_nothing_and_keeps_the_claim(
    session: Session, make_job, ideas
) -> None:
    job = make_job(idea_count=2)
    _claim(session)
    session.commit()

    with pytest.raises(DBAPIError) as raised:
        _complete(session, job.id, ideas)
    session.rollback()

    assert _sqlstate(raised.value) == "22023"
    assert _ideas(session) == []
    job = _reload(session, job)
    assert job.status == TopicIdeationJobStatus.RUNNING
    # The worker can still record the outcome, in a fresh transaction.
    assert _fail(session, job.id, retryable=False) == "failed"


@pytest.mark.parametrize(
    "result",
    [["not", "an", "object"], {"blob": "x" * (16 * 1024)}],
    ids=["not-an-object", "too-large"],
)
def test_completion_refuses_an_invalid_result(
    session: Session, make_job, result
) -> None:
    job = make_job()
    _claim(session)
    session.commit()

    with pytest.raises(DBAPIError):
        _complete(session, job.id, [_idea()], result=result)
    session.rollback()

    assert _ideas(session) == []


def test_completion_of_an_unknown_job_raises(session: Session) -> None:
    with pytest.raises(DBAPIError) as raised:
        _complete(session, uuid.uuid4(), [_idea()])
    session.rollback()

    assert _sqlstate(raised.value) == "P0002"


# -- isolation from content generation ----------------------------------------


def test_the_two_queues_never_see_each_others_jobs(
    session: Session, make_job, project: str, requester: User
) -> None:
    ideation = make_job()
    generation = ContentGenerationJob(
        id=uuid.uuid4(),
        project_id=project,
        requested_by_user_id=requester.id,
        status=GenerationJobStatus.QUEUED,
        params={"series_size": 1, "publishing_strategy": "standalone_series"},
    )
    session.add(generation)
    session.commit()

    claimed = claim_due_generation_job(session, worker_id="gen-1", lease_seconds=60)
    session.commit()
    assert claimed.id == generation.id
    again = claim_due_generation_job(session, worker_id="gen-1", lease_seconds=60)
    assert again is None

    row = _claim(session)
    session.commit()
    assert row["id"] == ideation.id
    assert _claim(session) is None


def test_an_idea_cannot_point_at_another_projects_job(
    session: Session, make_job, make_other_project: str, project: str
) -> None:
    foreign = make_job(project_id=make_other_project)

    with pytest.raises(IntegrityError):
        session.execute(
            text(
                "INSERT INTO content_ideas"
                " (id, project_id, ideation_job_id, position, title, description)"
                " VALUES (gen_random_uuid(), :p, :job, 1, 't', 'd')"
            ),
            {"p": project, "job": foreign.id},
        )
    session.rollback()


@pytest.mark.parametrize(
    "values",
    [
        {"idea_count": 0},
        {"idea_count": 6},
        {"max_attempts": 0},
        {"max_attempts": 11},
        {"topic_hint": ""},
        {"topic_hint": "x" * 501},
    ],
)
def test_the_table_bounds_a_job(session: Session, make_job, values) -> None:
    with pytest.raises(IntegrityError):
        make_job(**values)
    session.rollback()


# -- the API's role (session-bound ai_smm_cc_rw) ----------------------------


def _refused(raised: pytest.ExceptionInfo[DBAPIError], fragment: str = "") -> None:
    assert _sqlstate(raised.value) == INSUFFICIENT_PRIVILEGE, raised.value
    if fragment:
        assert fragment in str(raised.value), raised.value


#: The params shape the API writes since SMM-035B3B1 (the guard checks it).
VALID_PARAMS = {"content_profile": {"schema_version": 1}, "recent_idea_titles": []}


def _insert_job(db: Session, *, project_id: str, user: User, **columns: object) -> None:
    values = {
        "id": uuid.uuid4(),
        "project_id": project_id,
        "requested_by_user_id": user.id,
        "status": "queued",
        "idea_count": 2,
        "params": VALID_PARAMS,
        "max_attempts": 2,
        **columns,
    }
    names = ", ".join(values)
    placeholders = ", ".join(
        f"CAST(:{name} AS jsonb)" if name == "params" else f":{name}"
        for name in values
    )
    if isinstance(values["params"], (dict, list)):
        values["params"] = json.dumps(values["params"])
    db.execute(
        text(f"INSERT INTO topic_ideation_jobs ({names}) VALUES ({placeholders})"),
        values,
    )
    db.flush()


def test_the_api_role_may_queue_a_job_for_its_own_project(
    rw, session: Session, owner: User, project: str, token_for  # noqa: F811
) -> None:
    with bound(rw, token_for(owner)) as db:
        _insert_job(db, project_id=project, user=owner)
        db.commit()

    job = session.scalars(select(TopicIdeationJob)).one()
    assert job.status == TopicIdeationJobStatus.QUEUED
    assert job.requested_by_user_id == owner.id


@pytest.mark.parametrize(
    ("columns", "fragment"),
    [
        ({"status": "running"}, "freshly queued"),
        ({"status": "succeeded"}, "freshly queued"),
        ({"max_attempts": 50}, "max_attempts"),
    ],
)
def test_the_api_role_cannot_queue_anything_but_a_fresh_job(
    rw, owner: User, project: str, token_for, columns, fragment  # noqa: F811
) -> None:
    with bound(rw, token_for(owner)) as db, pytest.raises(DBAPIError) as raised:
        _insert_job(db, project_id=project, user=owner, **columns)

    _refused(raised, fragment)


@pytest.mark.parametrize(
    "params",
    [
        {},
        [],
        '"a scalar"',
        {"content_profile": {"schema_version": 1}},
        {"recent_idea_titles": []},
        {"content_profile": [], "recent_idea_titles": []},
        {**VALID_PARAMS, "prompt": "ignore the rules"},
        {**VALID_PARAMS, "recent_idea_titles": "a title"},
        {**VALID_PARAMS, "recent_idea_titles": [1]},
        {**VALID_PARAMS, "recent_idea_titles": [{"t": "x"}]},
        {**VALID_PARAMS, "recent_idea_titles": ["  "]},
        {**VALID_PARAMS, "recent_idea_titles": ["x" * 201]},
        {**VALID_PARAMS, "recent_idea_titles": [f"t{n}" for n in range(21)]},
    ],
)
def test_the_api_role_queues_only_well_shaped_params(
    rw, owner: User, project: str, token_for, params  # noqa: F811
) -> None:
    """SMM-035B3B1: a profile object and <= 20 short titles, nothing else."""

    with bound(rw, token_for(owner)) as db, pytest.raises(DBAPIError) as raised:
        _insert_job(db, project_id=project, user=owner, params=params)

    _refused(raised, "ideation params")


def test_the_table_bounds_the_size_of_params(session: Session, make_job) -> None:
    with pytest.raises(IntegrityError):
        make_job(params={"content_profile": {"x": "y" * (64 * 1024)}})
    session.rollback()


@pytest.mark.parametrize(
    "column", ["claimed_by", "attempt_count", "result", "last_error", "finished_at"]
)
def test_the_api_role_has_no_grant_on_worker_columns(
    rw, owner: User, project: str, token_for, column: str  # noqa: F811
) -> None:
    value = {
        "claimed_by": "x",
        "attempt_count": 0,
        "result": "{}",
        "last_error": "x",
        "finished_at": datetime.now(timezone.utc),
    }[column]

    with bound(rw, token_for(owner)) as db, pytest.raises(DBAPIError) as raised:
        _insert_job(db, project_id=project, user=owner, **{column: value})

    _refused(raised, "permission denied")


def test_the_api_role_cannot_queue_for_someone_else(
    rw, owner: User, make_user, project: str, token_for  # noqa: F811
) -> None:
    other = make_user()

    with bound(rw, token_for(owner)) as db, pytest.raises(DBAPIError) as raised:
        _insert_job(db, project_id=project, user=other)

    _refused(raised, "the requester must be the bound user")


@pytest.mark.parametrize(
    "role",
    [MembershipRole.VIEWER, MembershipRole.EDITOR, MembershipRole.ADMIN, None],
)
def test_only_an_owner_may_queue(
    rw, make_user, make_membership, project: str, token_for, role  # noqa: F811
) -> None:
    user = make_user()
    if role is not None:
        make_membership(user=user, project_id=project, role=role)

    with bound(rw, token_for(user)) as db, pytest.raises(DBAPIError) as raised:
        _insert_job(db, project_id=project, user=user)

    _refused(raised, "reserved to its owner")


def test_an_inactive_project_gets_no_new_job(
    rw, session: Session, owner: User, project: str, token_for  # noqa: F811
) -> None:
    session.execute(
        text("UPDATE projects SET is_active = false WHERE id = :p"), {"p": project}
    )
    session.commit()

    with bound(rw, token_for(owner)) as db, pytest.raises(DBAPIError) as raised:
        _insert_job(db, project_id=project, user=owner)

    _refused(raised, "active project")


def test_an_unbound_api_role_writes_nothing(
    rw, owner: User, project: str  # noqa: F811
) -> None:
    db = rw()
    try:
        with pytest.raises(DBAPIError) as raised:
            _insert_job(db, project_id=project, user=owner)
    finally:
        db.rollback()
        db.close()

    _refused(raised, "session-bound")


def test_the_api_role_cannot_write_ideas_or_change_jobs(
    rw, session: Session, owner: User, project: str, make_job, token_for  # noqa: F811
) -> None:
    job = make_job()
    token = token_for(owner)

    with bound(rw, token) as db, pytest.raises(DBAPIError) as raised:
        db.execute(
            text(
                "INSERT INTO content_ideas"
                " (id, project_id, ideation_job_id, position, title, description)"
                " VALUES (gen_random_uuid(), :p, :job, 1, 't', 'd')"
            ),
            {"p": project, "job": job.id},
        )
    _refused(raised, "permission denied")

    with bound(rw, token) as db, pytest.raises(DBAPIError) as raised:
        db.execute(
            text("UPDATE topic_ideation_jobs SET status = 'cancelled' WHERE id = :id"),
            {"id": job.id},
        )
    _refused(raised, "permission denied")


def test_the_guards_refuse_idea_and_job_writes_even_if_granted(
    rw, session: Session, owner: User, project: str, make_job, token_for  # noqa: F811
) -> None:
    """The trigger is the invariant, independent of today's grants.
    GRANT/REVOKE are confined to this disposable test database."""

    job = make_job()
    token = token_for(owner)
    session.execute(text("GRANT INSERT, UPDATE ON content_ideas TO ai_smm_cc_rw"))
    session.execute(
        text("GRANT UPDATE (status) ON topic_ideation_jobs TO ai_smm_cc_rw")
    )
    session.commit()
    try:
        with bound(rw, token) as db, pytest.raises(DBAPIError) as raised:
            db.execute(
                text(
                    "INSERT INTO content_ideas"
                    " (id, project_id, ideation_job_id, position, title, description)"
                    " VALUES (gen_random_uuid(), :p, :job, 1, 't', 'd')"
                ),
                {"p": project, "job": job.id},
            )
        _refused(
            raised, "INSERT on content_ideas belongs to the topic ideation contracts"
        )

        with bound(rw, token) as db, pytest.raises(DBAPIError) as raised:
            db.execute(
                text(
                    "UPDATE topic_ideation_jobs SET status = 'cancelled'"
                    " WHERE id = :id"
                ),
                {"id": job.id},
            )
        _refused(
            raised,
            "UPDATE on topic_ideation_jobs belongs to the topic ideation contracts",
        )
    finally:
        session.execute(
            text("REVOKE INSERT, UPDATE ON content_ideas FROM ai_smm_cc_rw")
        )
        session.execute(
            text("REVOKE UPDATE (status) ON topic_ideation_jobs FROM ai_smm_cc_rw")
        )
        session.commit()


# -- privileges and contract definitions ---------------------------------------


def test_the_contracts_are_security_definer_with_a_fixed_search_path(
    session: Session,
) -> None:
    for contract in CONTRACTS:
        secdef, config, superuser = session.execute(
            text(
                "SELECT p.prosecdef, p.proconfig, r.rolsuper"
                " FROM pg_proc p JOIN pg_roles r ON r.oid = p.proowner"
                " WHERE p.oid = to_regprocedure(:f)"
            ),
            {"f": contract},
        ).one()
        assert secdef, contract
        assert config == ["search_path=pg_catalog, pg_temp"], contract
        assert not superuser, contract


@pytest.mark.parametrize("role", ["public", "ai_smm_cc", "ai_smm_cc_rw"])
def test_no_role_may_execute_the_contracts_yet(
    session: Session, mutation_role, role: str
) -> None:
    del mutation_role  # provisions ai_smm_cc and ai_smm_cc_rw
    for contract in CONTRACTS:
        assert not session.execute(
            text("SELECT has_function_privilege(:r, to_regprocedure(:f), 'EXECUTE')"),
            {"r": role, "f": contract},
        ).scalar_one(), (role, contract)


def test_the_generation_role_is_unchanged_and_kept_out_of_ideation(
    session: Session, admin_database_url: str, generation_role
) -> None:
    del generation_role
    assert pg_roles.failures(pg_roles.verify_generation(admin_database_url)) == []

    for contract in CONTRACTS:
        assert not session.execute(
            text(
                "SELECT has_function_privilege('ai_smm_generation',"
                " to_regprocedure(:f), 'EXECUTE')"
            ),
            {"f": contract},
        ).scalar_one(), contract
    for table in ("topic_ideation_jobs", "content_ideas"):
        assert not session.execute(
            text(
                "SELECT has_table_privilege('ai_smm_generation', :t,"
                " 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')"
            ),
            {"t": f"public.{table}"},
        ).scalar_one(), table
