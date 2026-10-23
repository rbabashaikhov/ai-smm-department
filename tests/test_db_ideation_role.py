"""ai_smm_ideation (SMM-035B3B1): the topic ideation worker's database role,
made by the operator's own files (deploy/control-center/db/
ai_smm_ideation.*.sql), and the isolation between it and every other role.

Skipped without AI_SMM_TEST_ADMIN_DATABASE_URL, like every role suite.
"""
from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import replace

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from ai_smm.content_profile import build_profile_snapshot
from ai_smm.db.models import ContentIdea, TopicIdeationJob, TopicIdeationJobStatus
from ai_smm.db.session import reset_engine
from ai_smm.generation_worker import GENERATION_CONTRACTS
from ai_smm.ideation_queue import claim_ideation_job, complete_ideation_job
from ai_smm.ideation_worker import IDEATION_CONTRACTS, IdeationWorker, check_readiness
from tests import pg_roles
from tests.test_ideation_worker import FakeLLM, _idea


PROFILE = build_profile_snapshot(
    content_config={}, brand_config={}, default_language="ru", settings_version=None
)


@pytest.fixture(autouse=True)
def _fresh_engine() -> Iterator[None]:
    reset_engine()
    yield
    reset_engine()


@pytest.fixture
def ideation_db(ideation_engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=ideation_engine, expire_on_commit=False, future=True)
    with factory() as db:
        yield db


def _queue(session: Session, project: str, user, **kw) -> TopicIdeationJob:
    job = TopicIdeationJob(
        id=uuid.uuid4(),
        project_id=project,
        requested_by_user_id=user.id,
        idea_count=2,
        params={"content_profile": PROFILE, "recent_idea_titles": []},
        **kw,
    )
    session.add(job)
    session.commit()
    return job


def _can_execute(session: Session, role: str, function: str) -> bool:
    return session.execute(
        text("SELECT has_function_privilege(:r, to_regprocedure(:f), 'EXECUTE')"),
        {"r": role, "f": function},
    ).scalar_one()


def test_the_operator_verify_passes(ideation_role, admin_database_url: str) -> None:
    del ideation_role
    lines = pg_roles.verify_ideation(admin_database_url)

    assert pg_roles.failures(lines) == [], lines
    assert {line.split("|")[1] for line in lines} == {
        "role_attributes", "no_memberships", "no_transitive_membership",
        "no_table_privileges",
        "contract_execute", "claim_returns_params", "no_other_functions",
        "generation_role_excluded", "public_cannot_execute",
    }


def test_the_role_is_ready_and_nothing_else_is(
    ideation_db: Session, session: Session
) -> None:
    assert check_readiness(ideation_db) == []

    owner_problems = check_readiness(session)
    assert any("table access" in p for p in owner_problems), owner_problems
    assert any("other functions" in p for p in owner_problems), owner_problems


def test_the_generation_role_is_not_ready_for_ideation(
    generation_engine: Engine, ideation_role
) -> None:
    del ideation_role
    with sessionmaker(bind=generation_engine)() as db:
        problems = check_readiness(db)

    assert any("not executable" in p for p in problems), problems
    assert any("generation_claim" in p for p in problems), problems


def test_the_two_worker_roles_cannot_call_each_others_contracts(
    session: Session,
    ideation_role,
    generation_role,
    mutation_role,
    admin_database_url: str,
) -> None:
    del ideation_role, generation_role, mutation_role  # + ai_smm_cc, ai_smm_cc_rw
    for contract in GENERATION_CONTRACTS:
        assert not _can_execute(session, "ai_smm_ideation", contract), contract
    for contract in IDEATION_CONTRACTS:
        assert not _can_execute(session, "ai_smm_generation", contract), contract
        assert _can_execute(session, "ai_smm_ideation", contract), contract
    for role in ("public", "ai_smm_cc", "ai_smm_cc_rw"):
        for contract in IDEATION_CONTRACTS:
            assert not _can_execute(session, role, contract), (role, contract)

    assert pg_roles.failures(pg_roles.verify_generation(admin_database_url)) == []
    assert pg_roles.failures(pg_roles.verify_ideation(admin_database_url)) == []


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT 1 FROM topic_ideation_jobs LIMIT 1",
        "SELECT 1 FROM content_ideas LIMIT 1",
        "SELECT 1 FROM project_settings LIMIT 1",
        "SELECT 1 FROM publications LIMIT 1",
        "SELECT 1 FROM social_account_credentials LIMIT 1",
        "SELECT 1 FROM content_generation_jobs LIMIT 1",
        "UPDATE topic_ideation_jobs SET status = 'succeeded'",
        "DELETE FROM content_ideas",
        "SELECT * FROM public.generation_claim('x', 60)",
    ],
)
def test_the_role_has_no_capability_beyond_its_contracts(
    ideation_db: Session, statement: str
) -> None:
    with pytest.raises(DBAPIError) as raised:
        ideation_db.execute(text(statement))
    assert "permission denied" in str(raised.value)
    ideation_db.rollback()


def test_the_role_writes_only_into_the_claimed_jobs_project(
    ideation_db: Session, session: Session, project: str, make_other_project: str,
    make_user,
) -> None:
    user = make_user()
    mine = _queue(session, project, user)
    theirs = _queue(session, make_other_project, user)

    claimed = claim_ideation_job(ideation_db, worker_id="w-1", lease_seconds=60)
    ideation_db.commit()
    assert claimed.id == mine.id
    assert claimed.params["content_profile"] == PROFILE

    # Completing a job it never claimed is a no-op, not a write.
    outcome = complete_ideation_job(
        ideation_db, job_id=theirs.id, worker_id="w-1",
        ideas_json='[{"title": "x", "description": "y"}]', result_json="{}",
    )
    ideation_db.commit()
    assert outcome == {"status": "queued", "idea_ids": []}

    outcome = complete_ideation_job(
        ideation_db, job_id=mine.id, worker_id="w-1",
        ideas_json='[{"title": "x", "description": "y"}]', result_json="{}",
    )
    ideation_db.commit()
    assert outcome["status"] == "succeeded"

    session.expire_all()
    ideas = session.query(ContentIdea).all()
    assert [(i.project_id, i.ideation_job_id) for i in ideas] == [(project, mine.id)]


def test_the_real_worker_runs_as_the_role(
    session: Session, ideation_role, settings, project: str, make_user
) -> None:
    job = _queue(session, project, make_user())
    worker = IdeationWorker(
        settings=replace(
            settings, database_url=ideation_role.url, ideation_worker_id="role-test"
        ),
        llm_factory=lambda: FakeLLM({"ideas": [_idea(1), _idea(2)]}),
    )

    assert worker.process_one() == "succeeded"

    session.expire_all()
    stored = session.get(TopicIdeationJob, job.id)
    assert stored.status == TopicIdeationJobStatus.SUCCEEDED
    assert session.query(ContentIdea).count() == 2


def test_verify_and_readiness_catch_a_stray_grant(
    ideation_role, ideation_db: Session, admin_database_url: str
) -> None:
    del ideation_role
    claim = "FUNCTION public.generation_claim(text, integer)"
    pg_roles.run_admin(
        admin_database_url, "GRANT SELECT ON content_ideas TO ai_smm_ideation"
    )
    pg_roles.run_admin(
        admin_database_url, f"GRANT EXECUTE ON {claim} TO ai_smm_ideation"
    )
    try:
        failed = {
            line.split("|")[1]
            for line in pg_roles.failures(pg_roles.verify_ideation(admin_database_url))
        }
        assert failed == {"no_table_privileges", "no_other_functions"}

        problems = check_readiness(ideation_db)
        assert any("content_ideas" in p for p in problems), problems
        assert any("generation_claim" in p for p in problems), problems
    finally:
        pg_roles.run_admin(
            admin_database_url, "REVOKE SELECT ON content_ideas FROM ai_smm_ideation"
        )
        pg_roles.run_admin(
            admin_database_url, f"REVOKE EXECUTE ON {claim} FROM ai_smm_ideation"
        )
        ideation_db.rollback()


# -- role membership, both directions (P1 of the SMM-035B3B1 review) ----------

TEMP_ROLES = ("tmp_ideation_login", "tmp_ideation_mid", "tmp_ideation_parent")


@pytest.fixture
def temp_roles(admin_database_url: str) -> Iterator[None]:
    def drop() -> None:
        for role in TEMP_ROLES:
            pg_roles.run_admin(admin_database_url, f"DROP ROLE IF EXISTS {role}")

    drop()
    for role in TEMP_ROLES:
        pg_roles.run_admin(admin_database_url, f"CREATE ROLE {role} NOLOGIN")
    yield
    drop()


def _failed(admin_database_url: str) -> set[str]:
    return {
        line.split("|")[1]
        for line in pg_roles.failures(pg_roles.verify_ideation(admin_database_url))
    }


def test_clean_role_passes_membership_checks(
    ideation_role, ideation_db: Session, admin_database_url: str, temp_roles
) -> None:
    del ideation_role, temp_roles
    assert _failed(admin_database_url) == set()
    assert check_readiness(ideation_db) == []


@pytest.mark.parametrize(
    "setup",
    [
        ["GRANT ai_smm_ideation TO tmp_ideation_login"],
        [
            "GRANT ai_smm_ideation TO tmp_ideation_mid",
            "GRANT tmp_ideation_mid TO tmp_ideation_login",
        ],
        ["GRANT tmp_ideation_parent TO ai_smm_ideation"],
        [
            "GRANT tmp_ideation_mid TO ai_smm_ideation",
            "GRANT tmp_ideation_parent TO tmp_ideation_mid",
        ],
    ],
    ids=["outgoing-direct", "outgoing-via-intermediate", "incoming-direct",
         "incoming-via-intermediate"],
)
def test_membership_in_either_direction_is_detected_and_not_revoked(
    ideation_role, ideation_db: Session, admin_database_url: str, temp_roles,
    setup: list[str],
) -> None:
    del ideation_role, temp_roles
    for statement in setup:
        pg_roles.run_admin(admin_database_url, statement)

    assert {"no_memberships", "no_transitive_membership"} <= _failed(
        admin_database_url
    )
    problems = check_readiness(ideation_db)
    assert any("member" in p for p in problems), problems
    ideation_db.rollback()

    # Fail closed, change nothing: the edges are still there.
    edges = pg_roles.query_admin(
        admin_database_url,
        "SELECT count(*) FROM pg_auth_members m JOIN pg_roles r"
        " ON r.oid IN (m.member, m.roleid) WHERE r.rolname = 'ai_smm_ideation'",
    )[0][0]
    assert edges >= 1


@pytest.mark.parametrize(
    "grant",
    ["GRANT ai_smm_ideation TO tmp_ideation_login",
     "GRANT tmp_ideation_parent TO ai_smm_ideation"],
)
def test_provisioning_refuses_a_role_with_memberships_and_keeps_them(
    ideation_role, admin_database_url: str, temp_roles, grant: str
) -> None:
    del ideation_role, temp_roles
    pg_roles.run_admin(admin_database_url, grant)

    with pytest.raises(Exception, match="unsafe attributes or role memberships"):
        pg_roles.run_script(admin_database_url, pg_roles.IDEATION_GRANTS_SQL)

    assert "no_memberships" in _failed(admin_database_url)


def test_membership_checks_leave_other_worker_roles_alone(
    ideation_role, generation_role, admin_database_url: str, temp_roles
) -> None:
    del ideation_role, generation_role, temp_roles
    pg_roles.run_admin(
        admin_database_url, "GRANT ai_smm_ideation TO tmp_ideation_login"
    )

    assert pg_roles.failures(pg_roles.verify_generation(admin_database_url)) == []
    assert _failed(admin_database_url) == {"no_memberships", "no_transitive_membership"}
