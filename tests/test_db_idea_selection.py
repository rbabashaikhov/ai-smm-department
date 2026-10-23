"""Choosing a suggested topic in the database (SMM-035B3B2): the schema's own
integrity (composite key, unique link, status/link check), the
content_idea_* contracts a session-bound owner calls, and the privileges
around both -- including what a business role can NOT do with direct SQL.

The API-level behaviour is tests/test_api_idea_selection.py.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from ai_smm.db.models import (
    ContentGenerationJob,
    ContentIdea,
    GenerationJobStatus,
    MembershipRole,
    TopicIdeationJob,
    TopicIdeationJobStatus,
    User,
)
from tests.test_db_guards import (  # noqa: F401 -- fixtures
    _refused,
    bound,
    owner,
    rw,
    token_for,
)
from tests.test_db_least_privilege import _sqlstate


CONTRACTS = (
    "public.content_idea_begin_use(text,uuid)",
    "public.content_idea_finish_use(text,uuid,uuid)",
    "public.content_idea_reject(text,uuid)",
)

_BEGIN = text(
    "SELECT idea_id, idea_status, title, description, angle, generation_job_id"
    " FROM public.content_idea_begin_use(:p, :idea)"
)
_FINISH = text("SELECT public.content_idea_finish_use(:p, :idea, :job)")
_REJECT = text("SELECT public.content_idea_reject(:p, :idea)")

_INSERT_JOB = text(
    "INSERT INTO content_generation_jobs"
    " (id, project_id, requested_by_user_id, status, params, max_attempts)"
    " VALUES (:id, :p, :user, 'queued', CAST(:params AS jsonb), 2)"
)


@pytest.fixture
def make_idea(session: Session, project: str, owner: User):  # noqa: F811
    def factory(*, project_id: str | None = None, status: str = "proposed"):
        now = datetime.now(timezone.utc)
        job = TopicIdeationJob(
            id=uuid.uuid4(),
            project_id=project_id or project,
            requested_by_user_id=owner.id,
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
            title="Тема",
            description="Описание",
        )
        session.add(idea)
        session.commit()

        return idea

    return factory


@pytest.fixture
def make_gen_job(session: Session, project: str, owner: User):  # noqa: F811
    def factory(
        *,
        project_id: str | None = None,
        user: User | None = None,
        status: GenerationJobStatus = GenerationJobStatus.QUEUED,
    ) -> ContentGenerationJob:
        claimed = status is GenerationJobStatus.CLAIMED
        now = datetime.now(timezone.utc)
        job = ContentGenerationJob(
            id=uuid.uuid4(),
            project_id=project_id or project,
            requested_by_user_id=(user or owner).id,
            status=status,
            params={"series_size": 1, "publishing_strategy": "standalone_series"},
            claimed_by="worker-1" if claimed else None,
            claimed_at=now if claimed else None,
            lease_expires_at=now if claimed else None,
            attempt_count=1 if claimed else 0,
            finished_at=now if status is GenerationJobStatus.CANCELLED else None,
        )
        session.add(job)
        session.commit()

        return job

    return factory


def _status(session: Session, idea_id) -> tuple[str, uuid.UUID | None]:
    session.expire_all()
    row = session.execute(
        text("SELECT status::text, generation_job_id FROM content_ideas WHERE id = :i"),
        {"i": idea_id},
    ).one()

    return row[0], row[1]


# -- schema integrity (owner connection: the constraints themselves) -----------


def test_a_job_of_another_project_cannot_be_linked(
    session: Session, make_idea, make_gen_job, make_other_project: str
) -> None:
    idea = make_idea()
    foreign = make_gen_job(project_id=make_other_project)

    with pytest.raises(IntegrityError) as raised:
        session.execute(
            text(
                "UPDATE content_ideas SET status = 'used', generation_job_id = :j"
                " WHERE id = :i"
            ),
            {"j": foreign.id, "i": idea.id},
        )
    session.rollback()

    assert "fk_content_ideas_generation_job" in str(raised.value)
    assert _status(session, idea.id) == ("proposed", None)


def test_a_job_of_the_same_project_can_be_linked(
    session: Session, make_idea, make_gen_job
) -> None:
    idea = make_idea()
    job = make_gen_job()

    session.execute(
        text(
            "UPDATE content_ideas SET status = 'used', generation_job_id = :j"
            " WHERE id = :i"
        ),
        {"j": job.id, "i": idea.id},
    )
    session.commit()

    assert _status(session, idea.id) == ("used", job.id)


@pytest.mark.parametrize(
    "assignment",
    [
        "status = 'used'",
        "status = 'rejected', generation_job_id = :j",
        "generation_job_id = :j",
    ],
)
def test_used_means_linked_and_only_used(
    assignment: str, session: Session, make_idea, make_gen_job
) -> None:
    idea = make_idea()
    job = make_gen_job()

    with pytest.raises(IntegrityError) as raised:
        session.execute(
            text(f"UPDATE content_ideas SET {assignment} WHERE id = :i"),
            {"j": job.id, "i": idea.id},
        )
    session.rollback()

    assert "ck_content_ideas_used_job" in str(raised.value)
    assert _status(session, idea.id) == ("proposed", None)


def test_a_job_is_consumed_by_at_most_one_idea(
    session: Session, make_idea, make_gen_job
) -> None:
    first, second = make_idea(), make_idea()
    job = make_gen_job()
    link = text(
        "UPDATE content_ideas SET status = 'used', generation_job_id = :j"
        " WHERE id = :i"
    )
    session.execute(link, {"j": job.id, "i": first.id})
    session.commit()

    with pytest.raises(IntegrityError) as raised:
        session.execute(link, {"j": job.id, "i": second.id})
    session.rollback()

    assert "uq_content_ideas_generation_job" in str(raised.value)
    assert _status(session, second.id) == ("proposed", None)


def test_a_linked_job_cannot_be_deleted_or_moved_away(
    session: Session, make_idea, make_gen_job, make_other_project: str
) -> None:
    idea = make_idea()
    job = make_gen_job()
    session.execute(
        text(
            "UPDATE content_ideas SET status = 'used', generation_job_id = :j"
            " WHERE id = :i"
        ),
        {"j": job.id, "i": idea.id},
    )
    session.commit()

    with pytest.raises(IntegrityError):
        session.execute(
            text("DELETE FROM content_generation_jobs WHERE id = :j"), {"j": job.id}
        )
    session.rollback()

    with pytest.raises(IntegrityError):
        session.execute(
            text("UPDATE content_generation_jobs SET project_id = :p WHERE id = :j"),
            {"p": make_other_project, "j": job.id},
        )
    session.rollback()


# -- the contracts, called by a session-bound owner ----------------------------


def test_an_owner_rejects_through_the_contract(
    rw, session: Session, make_idea, owner: User, project: str, token_for  # noqa: F811
) -> None:
    idea = make_idea()

    with bound(rw, token_for(owner)) as db:
        assert db.execute(_REJECT, {"p": project, "idea": idea.id}).scalar_one() == (
            "proposed"
        )
        db.commit()

    assert _status(session, idea.id) == ("rejected", None)

    # A second call changes nothing and reports the state it found.
    with bound(rw, token_for(owner)) as db:
        assert db.execute(_REJECT, {"p": project, "idea": idea.id}).scalar_one() == (
            "rejected"
        )
        db.commit()

    assert _status(session, idea.id) == ("rejected", None)


def test_an_owner_uses_through_the_contracts(
    rw, session: Session, make_idea, owner: User, project: str, token_for  # noqa: F811
) -> None:
    idea = make_idea()
    job_id = uuid.uuid4()

    with bound(rw, token_for(owner)) as db:
        row = db.execute(_BEGIN, {"p": project, "idea": idea.id}).one()
        assert (row.idea_status, row.generation_job_id) == ("proposed", None)
        db.execute(
            _INSERT_JOB,
            {"id": job_id, "p": project, "user": owner.id, "params": "{}"},
        )
        assert db.execute(
            _FINISH, {"p": project, "idea": idea.id, "job": job_id}
        ).scalar_one() == "used"
        db.commit()

    assert _status(session, idea.id) == ("used", job_id)


def test_the_rolled_back_use_leaves_no_trace(
    rw, session: Session, make_idea, owner: User, project: str, token_for  # noqa: F811
) -> None:
    idea = make_idea()

    with bound(rw, token_for(owner)) as db:
        job_id = uuid.uuid4()
        db.execute(_BEGIN, {"p": project, "idea": idea.id}).one()
        db.execute(
            _INSERT_JOB,
            {"id": job_id, "p": project, "user": owner.id, "params": "{}"},
        )
        db.execute(_FINISH, {"p": project, "idea": idea.id, "job": job_id})
        db.rollback()

    assert _status(session, idea.id) == ("proposed", None)
    assert session.execute(
        text("SELECT count(*) FROM content_generation_jobs")
    ).scalar_one() == 0


@pytest.mark.parametrize("contract", ["begin", "finish", "reject"])
def test_the_contracts_need_a_session_bound_transaction(
    contract: str, rw, make_idea, project: str  # noqa: F811
) -> None:
    idea = make_idea()
    call = {
        "begin": _BEGIN,
        "finish": _FINISH,
        "reject": _REJECT,
    }[contract]
    db = rw()
    try:
        with pytest.raises(DBAPIError) as raised:
            db.execute(call, {"p": project, "idea": idea.id, "job": uuid.uuid4()})
    finally:
        db.rollback()
        db.close()

    _refused(raised, "needs a session-bound transaction")


@pytest.mark.parametrize("contract", ["begin", "finish", "reject"])
@pytest.mark.parametrize("role", [MembershipRole.EDITOR, MembershipRole.VIEWER])
def test_the_contracts_refuse_a_member_who_is_not_the_owner(
    contract: str,
    role: MembershipRole,
    rw,  # noqa: F811
    session: Session,
    make_idea,
    make_user,
    make_membership,
    project: str,
    token_for,  # noqa: F811
) -> None:
    idea = make_idea()
    member = make_user()
    make_membership(user=member, project_id=project, role=role)
    call = {"begin": _BEGIN, "finish": _FINISH, "reject": _REJECT}[contract]

    with bound(rw, token_for(member)) as db, pytest.raises(DBAPIError) as raised:
        db.execute(call, {"p": project, "idea": idea.id, "job": uuid.uuid4()})

    _refused(raised, "is reserved to its owner")
    assert _status(session, idea.id) == ("proposed", None)


@pytest.mark.parametrize("contract", ["begin", "reject"])
def test_an_owner_of_another_project_cannot_name_this_one(
    contract: str,
    rw,  # noqa: F811
    session: Session,
    make_idea,
    make_user,
    make_membership,
    make_other_project: str,
    project: str,
    token_for,  # noqa: F811
) -> None:
    idea = make_idea()
    stranger = make_user()
    make_membership(
        user=stranger, project_id=make_other_project, role=MembershipRole.OWNER
    )
    call = {"begin": _BEGIN, "reject": _REJECT}[contract]

    with bound(rw, token_for(stranger)) as db, pytest.raises(DBAPIError) as raised:
        db.execute(call, {"p": project, "idea": idea.id})

    _refused(raised, "is reserved to its owner")
    assert _status(session, idea.id) == ("proposed", None)


@pytest.mark.parametrize("contract", ["begin", "reject"])
def test_an_owner_of_both_projects_cannot_reach_an_idea_through_the_wrong_one(
    contract: str,
    rw,  # noqa: F811
    session: Session,
    make_idea,
    make_membership,
    make_other_project: str,
    owner: User,  # noqa: F811
    project: str,
    token_for,  # noqa: F811
) -> None:
    make_membership(
        user=owner, project_id=make_other_project, role=MembershipRole.OWNER
    )
    foreign = make_idea(project_id=make_other_project)
    call = {"begin": _BEGIN, "reject": _REJECT}[contract]

    with bound(rw, token_for(owner)) as db:
        result = db.execute(call, {"p": project, "idea": foreign.id})
        # No row and no change: indistinguishable from a missing idea.
        assert (
            result.scalar_one() is None
            if contract == "reject"
            else result.all() == []
        )
        db.commit()

    assert _status(session, foreign.id) == ("proposed", None)


def test_finish_refuses_a_job_that_is_not_the_callers_fresh_job(
    rw,  # noqa: F811
    session: Session,
    make_idea,
    make_gen_job,
    make_user,
    make_membership,
    make_other_project: str,
    owner: User,  # noqa: F811
    project: str,
    token_for,  # noqa: F811
) -> None:
    other_owner = make_user()
    make_membership(user=other_owner, project_id=project, role=MembershipRole.OWNER)
    make_membership(
        user=owner, project_id=make_other_project, role=MembershipRole.OWNER
    )
    idea = make_idea()
    candidates = {
        "another project": make_gen_job(project_id=make_other_project),
        "another requester": make_gen_job(user=other_owner),
        "already running": make_gen_job(status=GenerationJobStatus.CLAIMED),
        "cancelled": make_gen_job(status=GenerationJobStatus.CANCELLED),
    }

    for label, job in candidates.items():
        with bound(rw, token_for(owner)) as db, pytest.raises(DBAPIError) as raised:
            db.execute(_FINISH, {"p": project, "idea": idea.id, "job": job.id})

        assert _sqlstate(raised.value) == "22023", label
        assert _status(session, idea.id) == ("proposed", None), label

    with bound(rw, token_for(owner)) as db, pytest.raises(DBAPIError) as raised:
        db.execute(_FINISH, {"p": project, "idea": idea.id, "job": None})
    assert _sqlstate(raised.value) == "22023"


def test_finish_refuses_an_idea_that_is_not_proposed(
    rw, session: Session, make_idea, make_gen_job, owner: User, project: str, token_for  # noqa: F811
) -> None:
    idea = make_idea()
    job = make_gen_job()
    with bound(rw, token_for(owner)) as db:
        db.execute(_REJECT, {"p": project, "idea": idea.id})
        db.commit()

    with bound(rw, token_for(owner)) as db, pytest.raises(DBAPIError) as raised:
        db.execute(_FINISH, {"p": project, "idea": idea.id, "job": job.id})

    assert _sqlstate(raised.value) == "55000"
    assert _status(session, idea.id) == ("rejected", None)


def test_finish_on_a_missing_idea_is_not_found(
    rw, owner: User, project: str, token_for  # noqa: F811
) -> None:
    with bound(rw, token_for(owner)) as db, pytest.raises(DBAPIError) as raised:
        db.execute(_FINISH, {"p": project, "idea": uuid.uuid4(), "job": uuid.uuid4()})

    assert _sqlstate(raised.value) == "P0002"


# -- direct SQL by the business role ---------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE content_ideas SET status = 'rejected' WHERE id = :i",
        "UPDATE content_ideas SET status = 'used', generation_job_id = :j"
        " WHERE id = :i",
        "UPDATE content_ideas SET generation_job_id = :j WHERE id = :i",
        "UPDATE content_ideas SET title = 'x' WHERE id = :i",
        "DELETE FROM content_ideas WHERE id = :i",
        "SELECT id FROM content_ideas WHERE id = :i FOR UPDATE",
        "TRUNCATE content_ideas",
    ],
)
def test_the_business_role_cannot_change_an_idea_with_sql(
    statement: str,
    rw,  # noqa: F811
    session: Session,
    make_idea,
    make_gen_job,
    owner: User,  # noqa: F811
    token_for,  # noqa: F811
) -> None:
    idea = make_idea()
    job = make_gen_job()

    with bound(rw, token_for(owner)) as db, pytest.raises(DBAPIError) as raised:
        db.execute(text(statement), {"i": idea.id, "j": job.id})

    _refused(raised, "permission denied")
    assert _status(session, idea.id) == ("proposed", None)


def test_the_guard_still_refuses_idea_writes_even_if_granted(
    rw, session: Session, make_idea, make_gen_job, owner: User, token_for  # noqa: F811
) -> None:
    """The trigger, not only today's grants, keeps a business role out.
    GRANT/REVOKE are confined to this disposable test database."""

    idea = make_idea()
    job = make_gen_job()
    session.execute(text("GRANT UPDATE ON content_ideas TO ai_smm_cc_rw"))
    session.commit()
    try:
        with bound(rw, token_for(owner)) as db, pytest.raises(DBAPIError) as raised:
            db.execute(
                text(
                    "UPDATE content_ideas SET status = 'used',"
                    " generation_job_id = :j WHERE id = :i"
                ),
                {"i": idea.id, "j": job.id},
            )
        _refused(raised, "belongs to the topic ideation contracts")
    finally:
        session.execute(text("REVOKE UPDATE ON content_ideas FROM ai_smm_cc_rw"))
        session.commit()

    assert _status(session, idea.id) == ("proposed", None)


# -- privileges and definitions --------------------------------------------------


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


def _can_execute(session: Session, role: str, contract: str) -> bool:
    return session.execute(
        text("SELECT has_function_privilege(:r, to_regprocedure(:f), 'EXECUTE')"),
        {"r": role, "f": contract},
    ).scalar_one()


def test_only_the_business_role_may_execute_the_contracts(
    session: Session, mutation_role, generation_role, ideation_role  # noqa: F811
) -> None:
    del mutation_role, generation_role, ideation_role  # provision the roles

    for contract in CONTRACTS:
        assert _can_execute(session, "ai_smm_cc_rw", contract), contract
        for role in ("public", "ai_smm_cc", "ai_smm_generation", "ai_smm_ideation"):
            assert not _can_execute(session, role, contract), (role, contract)


def test_the_business_role_still_has_no_ideation_contract_or_idea_write(
    session: Session, mutation_role  # noqa: F811
) -> None:
    del mutation_role

    for contract in (
        "public.topic_ideation_claim(text,integer)",
        "public.topic_ideation_complete(uuid,text,jsonb,jsonb)",
        "public.topic_ideation_fail(uuid,text,text,boolean,text,integer,integer)",
    ):
        assert not _can_execute(session, "ai_smm_cc_rw", contract), contract
    assert not session.execute(
        text(
            "SELECT has_table_privilege('ai_smm_cc_rw', 'public.content_ideas',"
            " 'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')"
        )
    ).scalar_one()
    assert not session.execute(
        text(
            "SELECT has_column_privilege('ai_smm_cc_rw', 'public.content_ideas',"
            " 'generation_job_id', 'UPDATE')"
        )
    ).scalar_one()


def test_the_operators_verify_file_covers_the_contracts(
    admin_database_url: str, mutation_role  # noqa: F811
) -> None:
    from tests import pg_roles

    del mutation_role
    report = pg_roles.verify_mutation(admin_database_url)

    assert pg_roles.failures(report) == []
    for contract in ("begin_use", "finish_use", "reject"):
        assert f"PASS|function.content_idea_{contract}.execute|granted" in report
