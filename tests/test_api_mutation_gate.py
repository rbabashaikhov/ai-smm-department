"""Read-only Control Center: AI_SMM_API_MUTATIONS_ENABLED=false.

Every API test in this module runs against an app built with the gate
OFF (the packaged default): `api_settings` is overridden below, so the
shared `client` and `login` fixtures talk to a read-only API.

What is proven here:

* every business-state route refuses every role, owner included, with
  403 MUTATIONS_DISABLED -- and the database is byte-for-byte unchanged
  afterwards, including the caller's own session row;
* the list of gated routes is exhaustive: a new unsafe route that is not
  added here fails the suite;
* authentication and CSRF keep answering first and keep working;
* the queue commands refuse again on their own when the central gate is
  taken out of the way;
* the worker does not read the switch, and nothing implements full_auto.

The same routes with the gate ON are covered by the existing API suites,
which run with mutations enabled (tests/conftest.py, api_settings).
"""
from __future__ import annotations

import inspect
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.routing import iter_route_contexts
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

from ai_smm.application.content import (
    approve_revision,
    materialize_approved_revision,
    submit_for_review,
)
from ai_smm.application.control_plane import (
    MutationsDisabled,
    WorkerMode,
    observed_worker_mode,
)
from ai_smm.application.publications import (
    cancel_publication,
    reschedule_publication,
    schedule_publication,
)
from ai_smm.config import PROJECT_ROOT, Settings
from ai_smm.db.models import (
    AuditLog,
    Base,
    MembershipRole,
    PublicationStatus,
    User,
)
from tests.conftest import TEST_PASSWORD


SRC = PROJECT_ROOT / "src" / "ai_smm"


@pytest.fixture
def api_settings(settings: Settings) -> Settings:
    # The packaged default. Overrides the conftest fixture for this module.
    return replace(
        settings, api_cookie_secure=False, api_mutations_enabled=False
    )


# -- helpers --------------------------------------------------------------


def _fingerprint(engine: Engine) -> dict[str, str]:
    """One digest per table over every column of every row."""

    digests: dict[str, str] = {}

    with engine.connect() as connection:
        for table in Base.metadata.sorted_tables:
            digests[table.name] = connection.execute(
                text(
                    "SELECT md5(coalesce(string_agg(t::text, '|' "
                    f'ORDER BY t::text), \'\')) FROM "{table.name}" t'
                )
            ).scalar_one()

    return digests


def _future() -> str:
    return (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()


@dataclass
class World:
    approved_publication: int
    scheduled_publication: int
    draft_item: uuid.UUID
    draft_revision: uuid.UUID
    draft_version: int
    in_review_item: uuid.UUID
    in_review_revision: uuid.UUID
    approved_item: uuid.UUID
    approved_revision: uuid.UUID


@pytest.fixture
def world(
    session: Session,
    project: str,
    make_publication,
    make_content,
    make_user,
    make_membership,
) -> World:
    """One row in every state a gated command could act on.

    Built through the services and fixtures, never through the API, so
    the read-only app under test has not written any of it.
    """

    reviewer = make_user(email="reviewer@example.com")
    make_membership(
        user=reviewer, project_id=project, role=MembershipRole.OWNER
    )

    approved_publication = make_publication(
        status=PublicationStatus.APPROVED, scheduled_at=None
    )
    scheduled_publication = make_publication(
        status=PublicationStatus.SCHEDULED,
        scheduled_at=datetime.now(timezone.utc) + timedelta(days=1),
    )

    draft, draft_revision = make_content()
    in_review, in_review_revision = make_content()
    approved, approved_revision = make_content()

    for item, revision in (
        (in_review, in_review_revision),
        (approved, approved_revision),
    ):
        submit_for_review(
            session,
            content_item_id=item.id,
            expected_project_id=project,
            revision_id=revision.id,
            actor_user=reviewer,
        )

    approve_revision(
        session,
        content_item_id=approved.id,
        expected_project_id=project,
        revision_id=approved_revision.id,
        actor_user=reviewer,
    )
    session.commit()

    return World(
        approved_publication=approved_publication.id,
        scheduled_publication=scheduled_publication.id,
        draft_item=draft.id,
        draft_revision=draft_revision.id,
        draft_version=draft.version,
        in_review_item=in_review.id,
        in_review_revision=in_review_revision.id,
        approved_item=approved.id,
        approved_revision=approved_revision.id,
    )


#: (method, route template, request builder). Each request is one that
#: an authorised caller could make successfully with the gate ON.
GATED: dict[str, tuple[str, str, Any]] = {
    "create_content": (
        "POST",
        "/api/v1/projects/{project_id}/content-items",
        lambda w, p: (
            f"/api/v1/projects/{p}/content-items",
            {"title": "New", "revision": {"body": "Text", "format": "text"}},
        ),
    ),
    "create_revision": (
        "POST",
        "/api/v1/content-items/{content_item_id}/revisions",
        lambda w, p: (
            f"/api/v1/content-items/{w.draft_item}/revisions",
            {
                "body": "Second version",
                "format": "text",
                "expected_item_version": w.draft_version,
            },
        ),
    ),
    "submit_review": (
        "POST",
        "/api/v1/content-items/{content_item_id}/submit-review",
        lambda w, p: (
            f"/api/v1/content-items/{w.draft_item}/submit-review",
            {"revision_id": str(w.draft_revision)},
        ),
    ),
    "approve": (
        "POST",
        "/api/v1/content-items/{content_item_id}/approve",
        lambda w, p: (
            f"/api/v1/content-items/{w.in_review_item}/approve",
            {"revision_id": str(w.in_review_revision)},
        ),
    ),
    "reject": (
        "POST",
        "/api/v1/content-items/{content_item_id}/reject",
        lambda w, p: (
            f"/api/v1/content-items/{w.in_review_item}/reject",
            {"revision_id": str(w.in_review_revision)},
        ),
    ),
    "materialize": (
        "POST",
        "/api/v1/content-items/{content_item_id}/materialize",
        lambda w, p: (
            f"/api/v1/content-items/{w.approved_item}/materialize",
            {"revision_id": str(w.approved_revision)},
        ),
    ),
    "patch_settings": (
        "PATCH",
        "/api/v1/projects/{project_id}/settings",
        lambda w, p: (
            f"/api/v1/projects/{p}/settings",
            {"expected_version": 0, "content_config": {"tone": "calm"}},
        ),
    ),
    "schedule": (
        "POST",
        "/api/v1/publications/{publication_id}/schedule",
        lambda w, p: (
            f"/api/v1/publications/{w.approved_publication}/schedule",
            {"scheduled_at": _future()},
        ),
    ),
    "reschedule": (
        "POST",
        "/api/v1/publications/{publication_id}/reschedule",
        lambda w, p: (
            f"/api/v1/publications/{w.scheduled_publication}/reschedule",
            {"scheduled_at": _future()},
        ),
    ),
    "cancel": (
        "POST",
        "/api/v1/publications/{publication_id}/cancel",
        lambda w, p: (
            f"/api/v1/publications/{w.scheduled_publication}/cancel",
            {"note": "not now"},
        ),
    ),
}

#: The session lifecycle: the only unsafe routes open in read-only mode.
EXEMPT = {
    ("POST", "/api/v1/auth/login"),
    ("POST", "/api/v1/auth/logout"),
}

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _request(client, csrf: str, name: str, world: World, project: str):
    method, _, build = GATED[name]
    path, body = build(world, project)

    return client.request(
        method, path, json=body, headers={"X-CSRF-Token": csrf}
    )


def _assert_refused(response) -> None:
    assert response.status_code == 403, response.text

    error = response.json()["error"]

    assert error["code"] == "MUTATIONS_DISABLED"
    assert error["details"]["reason"] == "read_only_control_plane"
    assert "Nothing was changed" in error["message"]
    assert error["request_id"]


# -- configuration -----------------------------------------------------------


def test_the_api_is_read_only_unless_configured(monkeypatch) -> None:
    from ai_smm.config import load_settings

    monkeypatch.delenv("AI_SMM_API_MUTATIONS_ENABLED", raising=False)

    assert load_settings().api_mutations_enabled is False


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("true", True),
        ("1", True),
        ("false", False),
        ("0", False),
        # An empty value is "unset", and unset is read-only.
        ("", False),
    ],
)
def test_mutations_setting_parsing(
    monkeypatch, value: str, expected: bool
) -> None:
    from ai_smm.config import load_settings

    monkeypatch.setenv("AI_SMM_API_MUTATIONS_ENABLED", value)

    assert load_settings().api_mutations_enabled is expected


def test_an_invalid_mutations_setting_is_rejected(monkeypatch) -> None:
    """A typo must stop the API, not silently pick a mode."""

    from ai_smm.config import load_settings

    monkeypatch.setenv("AI_SMM_API_MUTATIONS_ENABLED", "sometimes")

    with pytest.raises(ValueError, match="must be a boolean"):
        load_settings()


# -- the gate ----------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(GATED))
@pytest.mark.parametrize(
    "role",
    [
        MembershipRole.VIEWER,
        MembershipRole.EDITOR,
        MembershipRole.ADMIN,
        MembershipRole.OWNER,
    ],
    ids=lambda role: role.value,
)
def test_no_role_can_change_business_state(
    client,
    login,
    member,
    engine: Engine,
    world: World,
    project: str,
    role: MembershipRole,
    name: str,
) -> None:
    csrf = login(member(role))
    before = _fingerprint(engine)

    response = _request(client, csrf, name, world, project)

    _assert_refused(response)
    # Every table, every column -- audit_log, the publication queue, and
    # the caller's own user_sessions row (last_seen_at is not touched by
    # a refused request).
    assert _fingerprint(engine) == before


def _unsafe_routes(app) -> list[Any]:
    """Every effective route with an unsafe method, included routers flattened."""

    return [
        route
        for route in iter_route_contexts(app.routes)
        if (route.methods or set()) & UNSAFE_METHODS
    ]


def test_the_gated_routes_are_every_unsafe_route_but_the_session_ones(
    api_app,
) -> None:
    unsafe = {
        (method, route.path)
        for route in _unsafe_routes(api_app)
        for method in route.methods
        if method in UNSAFE_METHODS
    }
    gated = {(method, template) for method, template, _ in GATED.values()}

    # A new unsafe route makes this fail until it is added to GATED (and
    # so proven refused) or, with a reason, to EXEMPT.
    assert unsafe == gated | EXEMPT


def test_every_unsafe_route_carries_the_gate(api_app) -> None:
    from ai_smm.api.mutation_gate import mutation_gate

    def calls(dependant) -> set[Any]:
        found = {dependant.call}

        for sub in dependant.dependencies:
            found |= calls(sub)

        return found

    routes = _unsafe_routes(api_app)

    # Not vacuous: the enumeration must see every unsafe route.
    assert len(routes) == len(GATED) + len(EXEMPT)

    for route in routes:
        assert mutation_gate in calls(route.dependant), route.path


def test_an_anonymous_caller_is_asked_to_sign_in_first(
    client, world: World, project: str
) -> None:
    """Authentication answers before the gate: 401, not 403."""

    method, _, build = GATED["schedule"]
    path, body = build(world, project)

    response = client.request(method, path, json=body)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_REQUIRED"


def test_csrf_answers_before_the_gate(
    client, login, member, world: World, project: str
) -> None:
    csrf = login(member(MembershipRole.OWNER))
    method, _, build = GATED["schedule"]
    path, body = build(world, project)

    missing = client.request(method, path, json=body)
    wrong = client.request(
        method, path, json=body, headers={"X-CSRF-Token": "x" * 43}
    )
    valid = client.request(
        method, path, json=body, headers={"X-CSRF-Token": csrf}
    )

    assert missing.json()["error"]["code"] == "CSRF_REQUIRED"
    assert wrong.json()["error"]["code"] == "CSRF_INVALID"
    _assert_refused(valid)


def test_the_gate_answers_before_body_validation(
    client, login, member, world: World
) -> None:
    """A read-only API does not grade a request it will never run."""

    csrf = login(member(MembershipRole.OWNER))

    response = client.post(
        f"/api/v1/publications/{world.approved_publication}/schedule",
        json={"scheduled_at": "not a date", "unknown": 1},
        headers={"X-CSRF-Token": csrf},
    )

    _assert_refused(response)


def test_turning_mutations_on_lets_the_same_request_through(
    api_settings: Settings,
    session_factory,
    member,
    world: World,
    project: str,
) -> None:
    """Control: the refusal comes from the switch, not from the request."""

    from fastapi.testclient import TestClient

    from ai_smm.api.app import create_app

    owner = member(MembershipRole.OWNER)
    app = create_app(
        replace(api_settings, api_mutations_enabled=True),
        session_factory=session_factory,
        access_log=False,
    )

    with TestClient(app) as editable:
        csrf = editable.post(
            "/api/v1/auth/login",
            json={"email": owner.email, "password": TEST_PASSWORD},
        ).json()["csrf_token"]

        response = _request(editable, csrf, "schedule", world, project)

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "scheduled"


# -- the session lifecycle stays open ----------------------------------------


def test_login_csrf_me_and_logout_work_read_only(
    client, member, session: Session
) -> None:
    user = member(MembershipRole.OWNER)

    login = client.post(
        "/api/v1/auth/login",
        json={"email": user.email, "password": TEST_PASSWORD},
    )

    assert login.status_code == 200

    me = client.get("/api/v1/auth/me")

    assert me.status_code == 200
    assert me.json()["control_plane"] == {"mutations_enabled": False}

    csrf = client.get("/api/v1/auth/csrf")

    assert csrf.status_code == 200
    assert csrf.json()["csrf_token"] == login.json()["csrf_token"]

    # Logout is exempt from the gate, not from CSRF.
    missing = client.post("/api/v1/auth/logout")

    assert missing.status_code == 403
    assert missing.json()["error"]["code"] == "CSRF_REQUIRED"

    logout = client.post(
        "/api/v1/auth/logout",
        headers={"X-CSRF-Token": csrf.json()["csrf_token"]},
    )

    assert logout.status_code == 204
    assert client.get("/api/v1/auth/me").status_code == 401

    session.expire_all()
    actions = session.scalars(
        select(AuditLog.action).order_by(AuditLog.id)
    ).all()

    assert actions[-2:] == ["auth.login", "auth.logout"]


def test_a_failed_login_is_still_audited_read_only(
    client, make_user, session: Session
) -> None:
    user = make_user()

    response = client.post(
        "/api/v1/auth/login",
        json={"email": user.email, "password": "wrong-password-value"},
    )

    assert response.status_code == 401

    session.expire_all()

    assert session.scalars(select(AuditLog.action)).all() == [
        "auth.login_failed"
    ]


def test_reads_keep_working_read_only(
    client, login, member, world: World, project: str
) -> None:
    login(member(MembershipRole.VIEWER))

    for path in (
        f"/api/v1/projects/{project}",
        f"/api/v1/projects/{project}/settings",
        f"/api/v1/projects/{project}/publications",
        f"/api/v1/projects/{project}/content-items",
        f"/api/v1/projects/{project}/operations/summary",
        f"/api/v1/projects/{project}/operations/attention",
        f"/api/v1/publications/{world.approved_publication}",
        f"/api/v1/publications/{world.approved_publication}/preview",
        f"/api/v1/content-items/{world.approved_item}",
    ):
        assert client.get(path).status_code == 200, path


# -- worker mode reporting ---------------------------------------------------


@pytest.mark.parametrize("api_dry_run", [True, False])
def test_the_api_never_reports_its_own_dry_run_as_the_worker_mode(
    api_settings: Settings,
    session_factory,
    member,
    project: str,
    api_dry_run: bool,
) -> None:
    from fastapi.testclient import TestClient

    from ai_smm.api.app import create_app

    viewer = member(MembershipRole.VIEWER)
    app = create_app(
        replace(api_settings, dry_run=api_dry_run),
        session_factory=session_factory,
        access_log=False,
    )

    with TestClient(app) as reader:
        reader.post(
            "/api/v1/auth/login",
            json={"email": viewer.email, "password": TEST_PASSWORD},
        )
        body = reader.get(
            f"/api/v1/projects/{project}/operations/summary"
        ).json()

    assert body["api_dry_run"] is api_dry_run
    assert body["api_mutations_enabled"] is False
    assert body["worker_mode"] == "unknown"
    assert body["worker_mode_source"] is None
    # The old field invited exactly the misreading this release removes.
    assert "dry_run" not in body


def test_there_is_no_source_of_the_worker_mode_yet() -> None:
    observation = observed_worker_mode()

    assert observation.mode is WorkerMode.UNKNOWN
    assert observation.source is None


# -- the second barrier: queue commands refuse on their own ------------------


def test_queue_commands_require_an_explicit_decision() -> None:
    for command in (
        schedule_publication,
        reschedule_publication,
        cancel_publication,
        materialize_approved_revision,
    ):
        parameter = inspect.signature(command).parameters["mutations_enabled"]

        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is inspect.Parameter.empty, command.__name__


@pytest.mark.parametrize("decision", [False, None, "true", 1])
def test_queue_commands_refuse_anything_but_true(
    session: Session,
    engine: Engine,
    world: World,
    project: str,
    decision: Any,
) -> None:
    reviewer = session.scalars(
        select(User).where(User.email == "reviewer@example.com")
    ).one()
    before = _fingerprint(engine)
    when = datetime.now(timezone.utc) + timedelta(days=2)

    calls = [
        lambda: schedule_publication(
            session,
            publication_id=world.approved_publication,
            expected_project_id=project,
            scheduled_at=when,
            actor="user:test",
            mutations_enabled=decision,
        ),
        lambda: reschedule_publication(
            session,
            publication_id=world.scheduled_publication,
            expected_project_id=project,
            scheduled_at=when,
            actor="user:test",
            mutations_enabled=decision,
        ),
        lambda: cancel_publication(
            session,
            publication_id=world.scheduled_publication,
            expected_project_id=project,
            actor="user:test",
            mutations_enabled=decision,
        ),
        lambda: materialize_approved_revision(
            session,
            content_item_id=world.approved_item,
            expected_project_id=project,
            actor_user=reviewer,
            mutations_enabled=decision,
        ),
    ]

    for call in calls:
        with pytest.raises(MutationsDisabled):
            call()

    session.commit()

    assert _fingerprint(engine) == before


@pytest.mark.parametrize(
    "name", ["schedule", "reschedule", "cancel", "materialize"]
)
def test_queue_routes_refuse_even_without_the_central_gate(
    api_app,
    client,
    login,
    member,
    engine: Engine,
    world: World,
    project: str,
    name: str,
) -> None:
    """Take the router-level gate away: the command still says no."""

    from ai_smm.api.mutation_gate import mutation_gate

    api_app.dependency_overrides[mutation_gate] = lambda: None

    try:
        csrf = login(member(MembershipRole.OWNER))
        before = _fingerprint(engine)

        response = _request(client, csrf, name, world, project)
    finally:
        api_app.dependency_overrides.clear()

    _assert_refused(response)
    assert response.json()["error"]["details"]["command"] == name
    assert _fingerprint(engine) == before


# -- the execution plane is out of reach -------------------------------------


@pytest.mark.parametrize(
    "module",
    [
        "worker.py",
        "queue.py",
        "cli.py",
        "healthcheck.py",
        "series.py",
        "publishing",
    ],
)
def test_the_execution_plane_does_not_read_the_api_switch(
    module: str,
) -> None:
    path = SRC / module
    files = sorted(path.rglob("*.py")) if path.is_dir() else [path]

    for file in files:
        source = file.read_text(encoding="utf-8")

        assert "api_mutations_enabled" not in source, file
        assert "AI_SMM_API_MUTATIONS_ENABLED" not in source, file
        assert "control_plane" not in source, file


def test_the_worker_compose_file_does_not_carry_the_api_switch() -> None:
    compose = (PROJECT_ROOT / "compose.yaml").read_text(encoding="utf-8")

    assert "AI_SMM_API_MUTATIONS_ENABLED" not in compose


def test_full_auto_is_not_implemented() -> None:
    """ADR: full_auto is reserved. No code path may implement it."""

    for file in sorted(Path(SRC).rglob("*.py")):
        source = file.read_text(encoding="utf-8").lower()

        assert "full_auto" not in source, file
        assert "full-auto" not in source, file
