"""The worker's fail-closed preflight (ai_smm.preflight).

The point under test: a new worker started with a configuration that
does not fit the queue refuses to start, and so never turns the
existing scheduled rows into `failed`.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from ai_smm.config import Settings
from ai_smm.db.models import Publication, PublicationAttempt, PublicationStatus
from ai_smm.preflight import (
    EXIT_CONFIG,
    EXPECTED_SCHEMA_REVISION,
    FAIL,
    INFO,
    PreflightFailed,
    failures,
    worker_preflight,
)
from ai_smm.publishing.routing import build_opener
from ai_smm.worker import Worker


@pytest.fixture
def migrated(engine: Engine, session: Session):
    """The test schema is built with create_all; give it the revision row."""

    def set_revision(revision: str | None) -> None:
        # The preflight reads alembic_version in the test session; end that
        # transaction so the DDL below does not wait for its lock.
        session.rollback()

        with engine.begin() as connection:
            connection.execute(text("DROP TABLE IF EXISTS alembic_version"))

            if revision is not None:
                connection.execute(
                    text(
                        "CREATE TABLE alembic_version "
                        "(version_num varchar(32) NOT NULL PRIMARY KEY)"
                    )
                )
                connection.execute(
                    text("INSERT INTO alembic_version VALUES (:v)"), {"v": revision}
                )

    set_revision(EXPECTED_SCHEMA_REVISION)

    yield set_revision

    set_revision(None)


def _env(token: str | None):
    return lambda name: token if name == "THREADS_ACCESS_TOKEN" else None


def _run(session, settings, opener="build", token="THQtest-deployment-0123456789"):
    return worker_preflight(
        session,
        settings,
        build_opener(settings) if opener == "build" else opener,
        getenv=_env(token),
    )


def _fitting(settings: Settings) -> Settings:
    """The allow-list of a deployment whose only legacy project exists."""

    return replace(settings, legacy_threads_projects=("test-project",))


def _by_name(checks):
    return {check.name: check for check in checks}


def test_a_fitting_configuration_passes(
    session: Session,
    migrated,
    account_settings: Settings,
    make_publication,
    make_account,
) -> None:
    make_publication()  # legacy row of the allow-listed test project
    account, _ = make_account()
    make_publication(social_account_id=account.id)

    checks = _run(session, _fitting(account_settings))

    assert failures(checks) == []
    assert _by_name(checks)["schema.revision"].detail == EXPECTED_SCHEMA_REVISION


def test_the_read_only_rollout_needs_no_private_key(
    session: Session, migrated, account_settings: Settings, make_publication
) -> None:
    """Production before R7: legacy rows only, no credential, no key at all."""

    make_publication()  # a scheduled legacy row of the allow-listed project
    settings = replace(_fitting(account_settings), credentials_private_key_files=())

    assert build_opener(settings) is None

    checks = _run(session, settings, opener=None)

    assert failures(checks) == []
    assert _by_name(checks)["accounts.credential_keys"].status == "PASS"
    assert _by_name(checks)["legacy.pending_rows_allowed"].status == "PASS"


def test_a_forgotten_allow_list_with_pending_legacy_rows_fails(
    session: Session, migrated, account_settings: Settings, make_publication
) -> None:
    make_publication()
    make_publication()

    checks = _run(session, replace(account_settings, legacy_threads_projects=()))

    check = _by_name(checks)["legacy.pending_rows_allowed"]
    assert check.status == FAIL
    assert "test-project=2" in check.detail


def test_an_allow_listed_project_that_does_not_exist_fails(
    session: Session, migrated, account_settings: Settings
) -> None:
    settings = replace(
        account_settings,
        legacy_threads_projects=("test-project", "ai-catalog-consltant"),
    )

    check = _by_name(_run(session, settings))["legacy.allow_list.known_projects"]

    assert check.status == FAIL
    assert "ai-catalog-consltant" in check.detail


def test_a_live_worker_without_the_deployment_token_fails(
    session: Session, migrated, account_settings: Settings, make_publication
) -> None:
    make_publication()

    live = _by_name(_run(session, account_settings, token=None))
    dry = _by_name(_run(session, replace(account_settings, dry_run=True), token=None))

    assert live["legacy.deployment_token"].status == FAIL
    assert dry["legacy.deployment_token"].status == INFO


def test_stored_credentials_without_the_private_key_fail(
    session: Session, migrated, account_settings: Settings, make_account
) -> None:
    from ai_smm.security.credentials import CredentialOpener, generate_key_pair

    make_account()

    no_key = _by_name(_run(session, account_settings, opener=None))
    other_key = _by_name(
        _run(
            session,
            account_settings,
            opener=CredentialOpener([generate_key_pair()[0]]),
        )
    )
    right_key = _by_name(_run(session, account_settings))

    assert no_key["accounts.credential_keys"].status == FAIL
    assert other_key["accounts.credential_keys"].status == FAIL
    assert right_key["accounts.credential_keys"].status != FAIL


def test_an_unmigrated_or_wrong_schema_fails_and_stops_there(
    session: Session, migrated, account_settings: Settings
) -> None:
    migrated("3d4ac1986f07")
    old = _run(session, account_settings)
    migrated(None)
    missing = _run(session, account_settings)

    for checks in (old, missing):
        assert [c.name for c in checks] == ["schema.revision"]
        assert checks[0].status == FAIL


def test_an_owners_choice_is_reported_not_fatal(
    session: Session,
    migrated,
    account_settings: Settings,
    make_account,
    make_publication,
) -> None:
    disabled, _ = make_account(status="disabled")
    make_publication(social_account_id=disabled.id)

    checks = _run(session, _fitting(account_settings))

    assert failures(checks) == []
    assert _by_name(checks)["accounts.pending_rows_ready"].status == INFO
    assert (
        "account_disabled=1" in _by_name(checks)["accounts.pending_rows_ready"].detail
    )


def test_no_check_prints_a_secret(
    session: Session,
    migrated,
    account_settings: Settings,
    make_account,
    make_publication,
) -> None:
    _, token = make_account()
    make_publication()
    deployment = "THQtest-deployment-secret-0123456789"

    lines = "\n".join(
        c.line() for c in _run(session, account_settings, token=deployment)
    )

    assert token not in lines
    assert deployment not in lines


def test_the_expected_revision_is_the_alembic_head() -> None:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))

    assert ScriptDirectory.from_config(config).get_heads() == [EXPECTED_SCHEMA_REVISION]


# -- the worker refuses to start ---------------------------------------------


@pytest.fixture
def worker_with(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    import ai_smm.db.session as session_module

    monkeypatch.setattr(
        session_module, "get_session_factory", lambda _settings=None: session_factory
    )

    def build(settings: Settings) -> Worker:
        return Worker(
            settings=settings,
            publisher_factory=lambda: pytest.fail("nothing may be published"),
            media_root=tmp_path,
            heartbeat_path=tmp_path / "heartbeat",
        )

    return build


def test_a_misconfigured_worker_never_claims_and_leaves_the_queue_as_it_was(
    session: Session,
    migrated,
    account_settings: Settings,
    make_publication,
    worker_with,
) -> None:
    """The regression the preflight exists for."""

    rows = [make_publication(), make_publication()]
    worker = worker_with(replace(account_settings, legacy_threads_projects=()))

    with pytest.raises(PreflightFailed, match="legacy.pending_rows_allowed"):
        worker.run()

    session.expire_all()

    assert {session.get(Publication, r.id).status for r in rows} == {
        PublicationStatus.SCHEDULED
    }
    assert session.scalar(select(func.count()).select_from(PublicationAttempt)) == 0


def test_main_exits_with_ex_config_on_a_failed_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ai_smm.worker as worker_module

    def refuse(self):
        raise PreflightFailed([])

    monkeypatch.setattr(worker_module, "load_local_dotenv", lambda: None)
    monkeypatch.setattr(worker_module.Worker, "run", refuse)

    assert worker_module.main() == EXIT_CONFIG


def test_main_exits_with_ex_config_on_a_bad_key_file(
    monkeypatch: pytest.MonkeyPatch, account_settings: Settings, credential_keys
) -> None:
    import os

    import ai_smm.worker as worker_module

    os.chmod(credential_keys.path, 0o644)
    monkeypatch.setattr(worker_module, "load_local_dotenv", lambda: None)
    monkeypatch.setattr(worker_module, "get_settings", lambda: account_settings)

    assert worker_module.main() == EXIT_CONFIG


def test_the_operator_command_reports_and_exits_nonzero(
    session: Session,
    migrated,
    account_settings: Settings,
    make_publication,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    from ai_smm import cli

    make_publication()
    monkeypatch.setattr(cli, "load_local_dotenv", lambda: None)
    monkeypatch.setenv("THREADS_ACCESS_TOKEN", "THQtest-deployment-secret-0123456789")

    monkeypatch.setattr(cli, "get_settings", lambda: _fitting(account_settings))
    assert cli.main(["preflight", "worker"]) == 0

    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: replace(account_settings, legacy_threads_projects=()),
    )
    assert cli.main(["preflight", "worker"]) == 1

    out = capsys.readouterr().out
    assert "FAIL|legacy.pending_rows_allowed|" in out
    assert out.strip().splitlines()[-1].startswith("END|checks|")
    assert "THQtest-deployment-secret" not in out
