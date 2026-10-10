"""`ai-smm user create-owner`: the only way a user is created."""
from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ai_smm import cli
from ai_smm.config import Settings
from ai_smm.db.models import AuditLog, MembershipRole, ProjectMembership, User
from ai_smm.security.passwords import verify_password


PASSWORD = "bootstrap-password-7731"


@pytest.fixture(autouse=True)
def bind_cli_session(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    import ai_smm.db.session as session_module

    monkeypatch.setattr(
        session_module,
        "get_session_factory",
        lambda _settings=None: session_factory,
    )


@pytest.fixture
def answers(monkeypatch: pytest.MonkeyPatch):
    """Feed getpass, as a terminal would."""

    def install(*values: str) -> list[str]:
        prompts: list[str] = []
        queue = list(values)

        def fake_getpass(prompt: str = "") -> str:
            prompts.append(prompt)

            return queue.pop(0)

        monkeypatch.setattr(cli.getpass, "getpass", fake_getpass)

        return prompts

    return install


def _run(argv: list[str], settings: Settings) -> int:
    parser = cli.build_parser()
    args = parser.parse_args(argv)

    return int(args.func(args, settings))


def test_creates_the_user_and_an_owner_membership(
    session: Session, settings: Settings, project: str, answers, capsys
) -> None:
    answers(PASSWORD, PASSWORD)

    code = _run(
        [
            "user",
            "create-owner",
            "--email",
            "  Owner@Example.COM ",
            "--display-name",
            "Project Owner",
            "--project",
            project,
        ],
        settings,
    )

    assert code == 0

    user = session.scalars(select(User)).one()

    assert user.email == "owner@example.com"
    assert user.display_name == "Project Owner"
    assert user.is_active is True
    assert verify_password(user.password_hash, PASSWORD) is True
    assert user.password_hash.startswith("$argon2id$")

    membership = session.scalars(select(ProjectMembership)).one()

    assert membership.project_id == project
    assert membership.user_id == user.id
    assert membership.role is MembershipRole.OWNER

    out = capsys.readouterr().out

    assert "owner created" in out
    assert str(user.id) in out
    # Nothing about the password, and no token of any kind.
    assert PASSWORD not in out
    assert user.password_hash not in out


def test_the_password_is_never_an_argument() -> None:
    parser = cli.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "user",
                "create-owner",
                "--email",
                "a@b.co",
                "--display-name",
                "A",
                "--project",
                "p",
                "--password",
                "whatever",
            ]
        )


def test_the_password_is_asked_twice(
    session: Session, settings: Settings, project: str, answers
) -> None:
    prompts = answers(PASSWORD, PASSWORD)

    _run(
        [
            "user",
            "create-owner",
            "--email",
            "a@example.com",
            "--display-name",
            "A",
            "--project",
            project,
        ],
        settings,
    )

    assert len(prompts) == 2
    # The prompt must not suggest a default.
    assert all("default" not in prompt.lower() for prompt in prompts)


def test_mismatched_confirmation_creates_nothing(
    session: Session, settings: Settings, project: str, answers, capsys
) -> None:
    answers(PASSWORD, PASSWORD + "typo")

    code = _run(
        [
            "user",
            "create-owner",
            "--email",
            "a@example.com",
            "--display-name",
            "A",
            "--project",
            project,
        ],
        settings,
    )

    assert code == 2
    assert session.scalars(select(User)).all() == []
    assert "do not match" in capsys.readouterr().out


def test_a_weak_password_creates_nothing(
    session: Session, settings: Settings, project: str, answers, capsys
) -> None:
    answers("short", "short")

    code = _run(
        [
            "user",
            "create-owner",
            "--email",
            "a@example.com",
            "--display-name",
            "A",
            "--project",
            project,
        ],
        settings,
    )

    assert code == 2
    assert session.scalars(select(User)).all() == []
    assert "at least" in capsys.readouterr().out


def test_a_duplicate_address_is_refused(
    session: Session, settings: Settings, project: str, answers, capsys
) -> None:
    answers(PASSWORD, PASSWORD, PASSWORD, PASSWORD)

    argv = [
        "user",
        "create-owner",
        "--email",
        "owner@example.com",
        "--display-name",
        "A",
        "--project",
        project,
    ]

    assert _run(argv, settings) == 0

    # Same address in a different case is the same user.
    argv[3] = "OWNER@example.com"

    assert _run(argv, settings) == 1
    assert "already exists" in capsys.readouterr().out
    assert len(session.scalars(select(User)).all()) == 1


def test_an_unknown_project_creates_no_user(
    session: Session, settings: Settings, answers, capsys
) -> None:
    """The two halves land together or not at all."""

    answers(PASSWORD, PASSWORD)

    code = _run(
        [
            "user",
            "create-owner",
            "--email",
            "a@example.com",
            "--display-name",
            "A",
            "--project",
            "no-such-project",
        ],
        settings,
    )

    assert code == 1
    assert session.scalars(select(User)).all() == []
    assert session.scalars(select(ProjectMembership)).all() == []
    assert "does not exist" in capsys.readouterr().out


def test_a_malformed_address_is_refused(
    session: Session, settings: Settings, project: str, answers, capsys
) -> None:
    code = _run(
        [
            "user",
            "create-owner",
            "--email",
            "not-an-address",
            "--display-name",
            "A",
            "--project",
            project,
        ],
        settings,
    )

    assert code == 2
    assert session.scalars(select(User)).all() == []
    assert "valid email" in capsys.readouterr().out


def test_the_bootstrap_is_audited_as_the_system(
    session: Session, settings: Settings, project: str, answers
) -> None:
    answers(PASSWORD, PASSWORD)

    _run(
        [
            "user",
            "create-owner",
            "--email",
            "a@example.com",
            "--display-name",
            "A",
            "--project",
            project,
        ],
        settings,
    )

    entry = session.scalars(
        select(AuditLog).where(AuditLog.action == "user.create_owner")
    ).one()
    user = session.scalars(select(User)).one()

    assert entry.actor == "system:bootstrap"
    assert entry.subject == f"user:{user.id}"
    assert entry.details["role"] == "owner"
    assert entry.details["project_id"] == project
    assert PASSWORD not in str(entry.details)
    assert "password" not in str(entry.details).lower()


def test_the_created_owner_can_log_in(
    session: Session,
    settings: Settings,
    project: str,
    answers,
    api_settings,
    session_factory,
) -> None:
    """End to end: the console bootstrap produces a working login."""

    from fastapi.testclient import TestClient

    from ai_smm.api.app import create_app

    answers(PASSWORD, PASSWORD)

    _run(
        [
            "user",
            "create-owner",
            "--email",
            "owner@example.com",
            "--display-name",
            "Owner",
            "--project",
            project,
        ],
        settings,
    )

    app = create_app(
        api_settings, session_factory=session_factory, access_log=False
    )

    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"email": "owner@example.com", "password": PASSWORD},
        )

        assert login.status_code == 200

        me = client.get("/api/v1/auth/me")

    assert me.json()["projects"] == [
        {
            "project_id": project,
            "display_name": "Test Project",
            "role": "owner",
        }
    ]
