"""Business-mutation mode of the Control Center wrapper (SMM-025C, FU-025B-1).

`cc-compose.sh mutations status | enable --approved-by NAME | disable`:

* mutation mode exists only while control-center.mutations, written by
  `enable` and nothing else, names the running release (SMM_CC_VERSION);
* then every verb adds compose.control-center.mutations.yml (pinned) and
  the API role gate checks ai_smm_cc_rw with ai_smm_cc_rw.verify.sql
  (pinned) before smm-api starts on it;
* a mode file for another release is refused by every verb that could
  start the API: a new release starts read-only;
* `disable` deletes the mode file first, then starts smm-api read-only --
  or, if the read-only role does not check out, stops it;
* none of it works in a release whose cc-gate.sh says
  CC_RELEASE_PERMITS_MUTATIONS=no -- every release so far, this one
  included: `enable` is refused before anything runs, a mode file is never
  honoured. The mechanism is therefore tested on a copy of the scripts
  with that one constant set to `yes` (the `WRAPPER` this module uses);
  the shipped scripts are tested as they are under "this release" below.

Each refusal exits 2, names the reason and prints no password. Most cases
answer from the fake `docker` (tests/cc_stub.py); the live ones run the
real ai_smm_cc_rw.verify.sql against the role tests/test_db_mutation_role.py
provisions, and one renders the override with the real Compose.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests import pg_roles
from tests.cc_stub import (
    FIXTURE_API_DSN,
    FIXTURE_DSN,
    FIXTURE_PG_CONTAINER,
    FIXTURE_VERSION,
    ROOT,
    WRAPPER,
    DockerStub,
    api_role_answer,
    docker_socket,
    run_script,
    write_deploy_dir,
)


pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash not available"
)

MUTATIONS_COMPOSE = ROOT / "compose.control-center.mutations.yml"
RW_VERIFY_SQL = ROOT / "deploy" / "control-center" / "db" / "ai_smm_cc_rw.verify.sql"
FIXTURE_RW_DSN = f"postgresql+psycopg://ai_smm_cc_rw:fixture-rw-pw@{FIXTURE_PG_CONTAINER}:5432/ai_smm"
PASSWORDS = ("fixture-pw", "fixture-api-pw", "fixture-rw-pw")
API_CONTAINER = "ai-smm-control-center-smm-api-1"
OTHER_RELEASE = "0123456789abcdef0123456789abcdef01234567"
STATE = "control-center.mutations"


SHIPPED_WRAPPER = WRAPPER
PERMITS = "readonly CC_RELEASE_PERMITS_MUTATIONS="


@pytest.fixture(scope="module")
def permitting_wrapper(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The shipped scripts with only CC_RELEASE_PERMITS_MUTATIONS=yes."""

    directory = tmp_path_factory.mktemp("permitting") / "control-center"
    directory.mkdir()

    for script in SHIPPED_WRAPPER.parent.glob("*.sh"):
        shutil.copy2(script, directory / script.name)

    gate = directory / "cc-gate.sh"
    text = gate.read_text(encoding="utf-8")
    assert text.count(f"{PERMITS}no\n") == 1
    gate.write_text(text.replace(f"{PERMITS}no\n", f"{PERMITS}yes\n"), encoding="utf-8")

    return directory / SHIPPED_WRAPPER.name


@pytest.fixture(autouse=True)
def _mechanism_under_test(
    monkeypatch: pytest.MonkeyPatch, permitting_wrapper: Path
) -> None:
    monkeypatch.setattr(sys.modules[__name__], "WRAPPER", permitting_wrapper)


@pytest.fixture
def deploy_dir(tmp_path: Path) -> Iterator[Path]:
    with docker_socket() as host:
        directory = write_deploy_dir(
            tmp_path / "ai-smm-control-center", docker_host=host
        )
        shutil.copy(MUTATIONS_COMPOSE, directory / MUTATIONS_COMPOSE.name)
        shutil.copy(RW_VERIFY_SQL, directory / RW_VERIFY_SQL.name)
        _write_rw_env(directory, f"AI_SMM_DATABASE_URL={FIXTURE_RW_DSN}\n")
        yield directory


@pytest.fixture
def stub(tmp_path: Path) -> DockerStub:
    return DockerStub(tmp_path / "stub-bin")


def _write_rw_env(deploy_dir: Path, content: str, mode: int = 0o600) -> None:
    path = deploy_dir / "control-center.api-rw.env"
    path.write_text(content, encoding="utf-8")
    path.chmod(mode)


def _write_state(deploy_dir: Path, **overrides: str) -> Path:
    values = {
        "SMM_CC_MUTATIONS": "on",
        "SMM_CC_MUTATIONS_VERSION": FIXTURE_VERSION,
        "SMM_CC_MUTATIONS_ENABLED_AT": "2026-10-14T10:00:00Z",
        "SMM_CC_MUTATIONS_APPROVED_BY": "fixture-owner",
        **overrides,
    }
    path = deploy_dir / STATE
    path.write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")

    return path


def _running_api(
    stub: DockerStub, *, flag: str = "true", dsn: str = FIXTURE_RW_DSN
) -> None:
    """smm-api as `docker inspect` reports it after Compose started it."""

    containers = dict(stub.state["containers"])
    containers[API_CONTAINER] = {
        "id": "f" * 64,
        "env": [
            f"AI_SMM_DATABASE_URL={dsn}",
            f"AI_SMM_API_MUTATIONS_ENABLED={flag}",
            "AI_SMM_THREADS_ACCESS_TOKEN=",
        ],
        "networks": {},
    }
    stub.update(containers=containers)


def _verify_sqls(stub: DockerStub) -> list[str]:
    """The verify files the API role gate ran, by role: ai_smm_cc or ai_smm_cc_rw."""

    roles = []

    for call in stub.calls():
        argv = call["argv"]

        if argv[:1] == ["exec"] and "psql" in argv:
            sql = argv[argv.index("-c") + 1]

            if "'checks'" in sql:
                roles.append("ai_smm_cc_rw" if "ai_smm_cc_rw" in sql else "ai_smm_cc")

    return roles


def _files_of(argv: list[str]) -> list[str]:
    return [Path(argv[i + 1]).name for i, arg in enumerate(argv) if arg == "--file"]


def _output(result) -> str:
    output = result.stdout + result.stderr

    for password in PASSWORDS:
        assert password not in output

    return output


def _assert_refused(result, stub: DockerStub, reason: str) -> None:
    output = _output(result)

    assert result.returncode == 2, output
    assert reason in output, output
    assert stub.compose_calls() == []


# -- status ----------------------------------------------------------------------


def test_status_without_a_mode_file_is_read_only(
    deploy_dir: Path, stub: DockerStub
) -> None:
    result = run_script(WRAPPER, deploy_dir, "mutations", "status", stub=stub)
    output = _output(result)

    assert result.returncode == 0, output
    assert "mode: read-only" in output
    assert "smm-api: not running" in output
    assert stub.compose_calls() == []
    assert _verify_sqls(stub) == []


def test_status_reports_mutation_mode_and_what_runs(
    deploy_dir: Path, stub: DockerStub
) -> None:
    _write_state(deploy_dir)
    _running_api(stub)

    result = run_script(WRAPPER, deploy_dir, "mutations", "status", stub=stub)
    output = _output(result)

    assert result.returncode == 0, output
    assert "mode: business-mutation (release abc1234def56" in output
    assert "approved by fixture-owner" in output
    assert "smm-api: AI_SMM_API_MUTATIONS_ENABLED=true role=ai_smm_cc_rw" in output
    assert stub.compose_calls() == []


def test_status_reports_a_stale_mode_file_as_read_only(
    deploy_dir: Path, stub: DockerStub
) -> None:
    _write_state(deploy_dir, SMM_CC_MUTATIONS_VERSION=OTHER_RELEASE)

    result = run_script(WRAPPER, deploy_dir, "mutations", "status", stub=stub)
    output = _output(result)

    assert result.returncode == 0, output
    assert "mode: read-only -- a mode file for release 0123456789ab" in output


# -- enable ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "reason"),
    [
        ((), "mutations: status | enable --approved-by NAME | disable"),
        (("enable",), "takes exactly --approved-by NAME"),
        (("enable", "--approved-by"), "takes exactly --approved-by NAME"),
        (("enable", "--by", "x"), "takes exactly --approved-by NAME"),
        (("enable", "--approved-by", ""), "must be a plain name"),
        (
            ("enable", "--approved-by", "a\nSMM_CC_MUTATIONS_VERSION=x"),
            "must be a plain name",
        ),
        (("enable", "--approved-by", "$(id)"), "must be a plain name"),
        (("status", "now"), "takes no arguments"),
        (("disable", "now"), "takes no arguments"),
        (("on",), "unsupported subcommand 'on'"),
    ],
)
def test_bad_arguments_are_refused_before_anything_runs(
    deploy_dir: Path, stub: DockerStub, args: tuple[str, ...], reason: str
) -> None:
    result = run_script(WRAPPER, deploy_dir, "mutations", *args, stub=stub)

    _assert_refused(result, stub, reason)
    assert stub.calls() == []
    assert not (deploy_dir / STATE).exists()


def test_enable_starts_smm_api_on_the_mutation_role(
    deploy_dir: Path, stub: DockerStub
) -> None:
    _running_api(stub)

    result = run_script(
        WRAPPER,
        deploy_dir,
        "mutations",
        "enable",
        "--approved-by",
        "Fixture Owner",
        stub=stub,
    )
    output = _output(result)

    assert result.returncode == 0, output
    assert "gate api-role: PASS -- smm-api connects as ai_smm_cc_rw" in output
    assert "mutations enable: PASS -- smm-api runs as ai_smm_cc_rw" in output

    # The mutation role was verified, with its own file, before Compose ran.
    assert _verify_sqls(stub) == ["ai_smm_cc_rw"]
    argvs = [call["argv"] for call in stub.calls()]
    verify_at = next(
        i
        for i, a in enumerate(argvs)
        if a[:1] == ["exec"] and "'checks'" in " ".join(a)
    )
    compose_at = next(i for i, a in enumerate(argvs) if a[:1] == ["compose"])
    assert verify_at < compose_at

    [compose] = stub.compose_calls()
    assert _files_of(compose) == [
        "compose.control-center.yml",
        "compose.control-center.mutations.yml",
    ]
    assert compose[compose.index("up") :] == [
        "up",
        "--detach",
        "--wait",
        "--wait-timeout",
        "180",
        "--no-build",
        "--pull",
        "never",
        "--no-deps",
        "smm-api",
    ]

    state = (deploy_dir / STATE).read_text(encoding="utf-8").splitlines()
    assert state[0] == "SMM_CC_MUTATIONS=on"
    assert state[1] == f"SMM_CC_MUTATIONS_VERSION={FIXTURE_VERSION}"
    assert state[2].startswith("SMM_CC_MUTATIONS_ENABLED_AT=20")
    assert state[3] == "SMM_CC_MUTATIONS_APPROVED_BY=Fixture Owner"
    assert len(state) == 4
    assert oct((deploy_dir / STATE).stat().st_mode & 0o777) == "0o644"
    assert not list(deploy_dir.glob(".mutations.*"))


def test_enable_without_the_mutation_roles_url_is_refused(
    deploy_dir: Path, stub: DockerStub
) -> None:
    (deploy_dir / "control-center.api-rw.env").unlink()

    result = run_script(
        WRAPPER, deploy_dir, "mutations", "enable", "--approved-by", "o", stub=stub
    )

    _assert_refused(result, stub, "missing")
    assert "control-center.api-rw.env" in result.stderr
    assert not (deploy_dir / STATE).exists()


@pytest.mark.parametrize(
    ("content", "mode", "reason"),
    [
        (f"AI_SMM_DATABASE_URL={FIXTURE_RW_DSN}\n", 0o640, "must be 600"),
        (
            f"AI_SMM_DATABASE_URL={FIXTURE_RW_DSN}\nAI_SMM_API_MUTATIONS_ENABLED=true\n",
            0o600,
            "must define exactly: AI_SMM_DATABASE_URL",
        ),
        (
            f"AI_SMM_DATABASE_URL={FIXTURE_API_DSN}\n",
            0o600,
            "control-center.api-rw.env and control-center.api.env hold the same URL",
        ),
        (
            "AI_SMM_DATABASE_URL=postgresql+psycopg://ai_smm_cc:other-pw"
            f"@{FIXTURE_PG_CONTAINER}:5432/ai_smm\n",
            0o600,
            "connects as 'ai_smm_cc', not the API role ai_smm_cc_rw",
        ),
        (
            f"AI_SMM_DATABASE_URL={FIXTURE_DSN}\n",
            0o600,
            "connects as 'fixture', not the API role ai_smm_cc_rw",
        ),
        (
            "AI_SMM_DATABASE_URL=postgresql+psycopg://ai_smm_cc_rw"
            f"@{FIXTURE_PG_CONTAINER}:5432/ai_smm\n",
            0o600,
            "carries no password",
        ),
        (
            "AI_SMM_DATABASE_URL=postgresql+psycopg://ai_smm_cc_rw:fixture-rw-pw"
            "@elsewhere:5432/ai_smm\n",
            0o600,
            "not the recorded",
        ),
    ],
)
def test_a_wrong_mutation_role_url_is_refused(
    deploy_dir: Path, stub: DockerStub, content: str, mode: int, reason: str
) -> None:
    _write_rw_env(deploy_dir, content, mode)

    result = run_script(
        WRAPPER, deploy_dir, "mutations", "enable", "--approved-by", "o", stub=stub
    )

    _assert_refused(result, stub, reason)
    assert not (deploy_dir / STATE).exists()


def test_enable_needs_the_read_only_way_back(
    deploy_dir: Path, stub: DockerStub
) -> None:
    """`disable` starts smm-api on control-center.api.env: still ai_smm_cc."""

    (deploy_dir / "control-center.api.env").write_text(
        f"AI_SMM_DATABASE_URL={FIXTURE_RW_DSN.replace('fixture-rw-pw', 'x')}\n",
        encoding="utf-8",
    )

    result = run_script(
        WRAPPER, deploy_dir, "mutations", "enable", "--approved-by", "o", stub=stub
    )

    _assert_refused(
        result,
        stub,
        "does not connect as ai_smm_cc; the read-only mode would not start again",
    )


def test_a_mutation_role_that_fails_verification_is_refused(
    deploy_dir: Path, stub: DockerStub
) -> None:
    stub.update(
        api_role_answer=api_role_answer("FAIL|table.publications.delete|granted")
    )

    result = run_script(
        WRAPPER, deploy_dir, "mutations", "enable", "--approved-by", "o", stub=stub
    )

    _assert_refused(
        result,
        stub,
        "ai_smm_cc_rw is not as designed: table.publications.delete|granted",
    )
    assert not (deploy_dir / STATE).exists()


@pytest.mark.parametrize(
    ("name", "reason"),
    [
        (
            "compose.control-center.mutations.yml",
            "compose.control-center.mutations.yml is not the file",
        ),
        ("ai_smm_cc_rw.verify.sql", "ai_smm_cc_rw.verify.sql is not the file"),
    ],
)
def test_an_edited_override_or_verify_file_is_refused(
    deploy_dir: Path, stub: DockerStub, name: str, reason: str
) -> None:
    path = deploy_dir / name
    path.write_text(
        path.read_text(encoding="utf-8") + "\n# edited on the host\n", encoding="utf-8"
    )

    result = run_script(
        WRAPPER, deploy_dir, "mutations", "enable", "--approved-by", "o", stub=stub
    )

    _assert_refused(result, stub, reason)
    assert _verify_sqls(stub) == []
    assert not (deploy_dir / STATE).exists()


def test_a_failed_start_removes_the_mode_file_and_stops_the_api(
    deploy_dir: Path, stub: DockerStub
) -> None:
    """`up --wait` failed: smm-api may run half-started in mutation mode."""

    stub.update(compose_rc=1)

    result = run_script(
        WRAPPER, deploy_dir, "mutations", "enable", "--approved-by", "o", stub=stub
    )
    output = _output(result)

    assert result.returncode == 2, output
    assert "did not come up in mutation mode" in output
    assert not (deploy_dir / STATE).exists()

    up, stop = stub.compose_calls()
    assert "up" in up
    assert _files_of(stop) == ["compose.control-center.yml"]
    assert stop[-2:] == ["stop", "smm-api"]
    # The stub fails every Compose call, the stop too: say so.
    assert "could not be stopped" in output


@pytest.mark.parametrize(
    ("flag", "dsn", "seen"),
    [
        ("false", FIXTURE_RW_DSN, "flag='false' role='ai_smm_cc_rw'"),
        ("true", FIXTURE_API_DSN, "flag='true' role='ai_smm_cc'"),
        (None, None, "flag='<unset>' role='<unknown>'"),
    ],
)
def test_enable_checks_what_actually_runs(
    deploy_dir: Path, stub: DockerStub, flag: str | None, dsn: str | None, seen: str
) -> None:
    if flag is not None:
        _running_api(stub, flag=flag, dsn=dsn)

    result = run_script(
        WRAPPER, deploy_dir, "mutations", "enable", "--approved-by", "o", stub=stub
    )
    output = _output(result)

    assert result.returncode == 2, output
    assert seen in output
    assert "the mode file is removed and smm-api is stopped" in output
    assert not (deploy_dir / STATE).exists()

    up, stop = stub.compose_calls()
    assert _files_of(up)[-1] == "compose.control-center.mutations.yml"
    assert _files_of(stop) == ["compose.control-center.yml"]
    assert stop[-2:] == ["stop", "smm-api"]


# -- in mutation mode ------------------------------------------------------------


@pytest.mark.parametrize(
    "args", [("up",), ("restart", "smm-api"), ("restart", "smm-web"), ("preflight",)]
)
def test_in_mutation_mode_every_start_checks_the_mutation_role(
    deploy_dir: Path, stub: DockerStub, args: tuple[str, ...]
) -> None:
    _write_state(deploy_dir)

    result = run_script(WRAPPER, deploy_dir, *args, stub=stub)
    output = _output(result)

    assert result.returncode == 0, output
    assert _verify_sqls(stub) == ["ai_smm_cc_rw"]

    for compose in stub.compose_calls():
        assert _files_of(compose)[-1] == "compose.control-center.mutations.yml"


@pytest.mark.parametrize("args", [("ps",), ("down",), ("logs", "smm-api")])
def test_in_mutation_mode_other_verbs_see_the_same_project(
    deploy_dir: Path, stub: DockerStub, args: tuple[str, ...]
) -> None:
    _write_state(deploy_dir)

    result = run_script(WRAPPER, deploy_dir, *args, stub=stub)

    assert result.returncode == 0, _output(result)
    [compose] = stub.compose_calls()
    assert _files_of(compose)[-1] == "compose.control-center.mutations.yml"


def test_in_mutation_mode_a_missing_url_stops_up(
    deploy_dir: Path, stub: DockerStub
) -> None:
    _write_state(deploy_dir)
    (deploy_dir / "control-center.api-rw.env").unlink()

    result = run_script(WRAPPER, deploy_dir, "up", stub=stub)

    _assert_refused(result, stub, "control-center.api-rw.env")


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"SMM_CC_MUTATIONS": "off"}, "SMM_CC_MUTATIONS must be 'on'"),
        ({"SMM_CC_MUTATIONS_VERSION": "abc1234"}, "is not a 40-hex commit"),
        ({"SMM_CC_MUTATIONS_ENABLED_AT": "yesterday"}, "is not a UTC timestamp"),
        ({"SMM_CC_MUTATIONS_APPROVED_BY": "$(id)"}, "is not a plain name"),
        ({"SMM_CC_MUTATIONS_EXTRA": "1"}, "must define exactly"),
    ],
)
def test_a_mode_file_not_written_by_the_wrapper_is_refused(
    deploy_dir: Path, stub: DockerStub, overrides: dict[str, str], reason: str
) -> None:
    _write_state(deploy_dir, **overrides)

    result = run_script(WRAPPER, deploy_dir, "up", stub=stub)

    _assert_refused(result, stub, reason)


def test_a_symlinked_mode_file_is_refused(
    deploy_dir: Path, stub: DockerStub, tmp_path: Path
) -> None:
    real = _write_state(tmp_path)
    (deploy_dir / STATE).symlink_to(real)

    result = run_script(WRAPPER, deploy_dir, "up", stub=stub)

    _assert_refused(result, stub, "is a symlink")


# -- a new release ---------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [("up",), ("restart", "smm-api"), ("preflight",), ("migrate", "current")],
)
def test_a_mode_file_for_another_release_is_refused(
    deploy_dir: Path, stub: DockerStub, args: tuple[str, ...]
) -> None:
    _write_state(deploy_dir, SMM_CC_MUTATIONS_VERSION=OTHER_RELEASE)

    result = run_script(WRAPPER, deploy_dir, *args, stub=stub)

    _assert_refused(
        result, stub, "a new release starts read-only: run 'mutations disable'"
    )


@pytest.mark.parametrize("args", [("ps",), ("down",)])
def test_a_stale_mode_file_leaves_inspection_and_down_read_only(
    deploy_dir: Path, stub: DockerStub, args: tuple[str, ...]
) -> None:
    _write_state(deploy_dir, SMM_CC_MUTATIONS_VERSION=OTHER_RELEASE)

    result = run_script(WRAPPER, deploy_dir, *args, stub=stub)

    assert result.returncode == 0, _output(result)
    [compose] = stub.compose_calls()
    assert _files_of(compose) == ["compose.control-center.yml"]


# -- disable ---------------------------------------------------------------------


@pytest.mark.parametrize("release", [FIXTURE_VERSION, OTHER_RELEASE])
def test_disable_removes_the_mode_file_and_starts_read_only(
    deploy_dir: Path, stub: DockerStub, release: str
) -> None:
    _write_state(deploy_dir, SMM_CC_MUTATIONS_VERSION=release)

    result = run_script(WRAPPER, deploy_dir, "mutations", "disable", stub=stub)
    output = _output(result)

    assert result.returncode == 0, output
    assert "mode file removed" in output
    assert not (deploy_dir / STATE).exists()
    assert _verify_sqls(stub) == ["ai_smm_cc"]

    [compose] = stub.compose_calls()
    assert _files_of(compose) == ["compose.control-center.yml"]
    assert compose[compose.index("up") :][-1] == "smm-api"

    # And from here on, every verb is read-only.
    stub.reset_calls()
    result = run_script(WRAPPER, deploy_dir, "up", stub=stub)

    assert result.returncode == 0, _output(result)
    assert _verify_sqls(stub) == ["ai_smm_cc"]


def test_disable_works_without_the_mutation_files(
    deploy_dir: Path, stub: DockerStub
) -> None:
    """The way back depends on nothing that mutation mode added."""

    _write_state(deploy_dir)

    for name in (
        "control-center.api-rw.env",
        "compose.control-center.mutations.yml",
        "ai_smm_cc_rw.verify.sql",
    ):
        (deploy_dir / name).unlink()

    result = run_script(WRAPPER, deploy_dir, "mutations", "disable", stub=stub)

    assert result.returncode == 0, _output(result)
    assert not (deploy_dir / STATE).exists()


def test_disable_stops_the_api_when_the_read_only_role_fails(
    deploy_dir: Path, stub: DockerStub
) -> None:
    _write_state(deploy_dir)
    stub.update(
        api_role_answer=api_role_answer("FAIL|table.publications.update|granted")
    )

    result = run_script(WRAPPER, deploy_dir, "mutations", "disable", stub=stub)
    output = _output(result)

    assert result.returncode == 2, output
    assert "stopping smm-api instead of starting it" in output
    assert "smm-api is stopped and the Control Center is read-only" in output
    assert not (deploy_dir / STATE).exists()

    [compose] = stub.compose_calls()
    assert _files_of(compose) == ["compose.control-center.yml"]
    assert compose[-2:] == ["stop", "smm-api"]


def test_disable_reports_when_even_stop_fails(
    deploy_dir: Path, stub: DockerStub
) -> None:
    _write_state(deploy_dir)
    stub.update(
        api_role_answer=api_role_answer("FAIL|table.publications.update|granted"),
        compose_rc=1,
    )

    result = run_script(WRAPPER, deploy_dir, "mutations", "disable", stub=stub)
    output = _output(result)

    assert result.returncode == 2, output
    assert "could not stop smm-api; stop it by hand" in output
    assert not (deploy_dir / STATE).exists()


# -- the real verify file on the real role --------------------------------------


@pytest.fixture
def live(stub: DockerStub, mutation_role, admin_database_url: str) -> DockerStub:
    stub.update(role_sql_url=pg_roles.libpq_url(admin_database_url))
    _running_api(stub)

    return stub


def test_live_the_provisioned_mutation_role_passes(
    deploy_dir: Path, live: DockerStub
) -> None:
    result = run_script(
        WRAPPER, deploy_dir, "mutations", "enable", "--approved-by", "o", stub=live
    )
    output = _output(result)

    assert result.returncode == 0, output
    assert "gate api-role: PASS -- smm-api connects as ai_smm_cc_rw" in output
    assert (deploy_dir / STATE).exists()


@pytest.mark.parametrize(
    ("drift", "undo", "reason"),
    [
        (
            "GRANT DELETE ON publications TO ai_smm_cc_rw",
            "REVOKE DELETE ON publications FROM ai_smm_cc_rw",
            "table.publications.delete|",
        ),
        (
            "GRANT UPDATE (body) ON content_revisions TO ai_smm_cc_rw",
            "REVOKE UPDATE (body) ON content_revisions FROM ai_smm_cc_rw",
            "content_revisions",
        ),
        (
            "GRANT SELECT (ciphertext) ON social_account_credentials TO ai_smm_cc_rw",
            "REVOKE SELECT (ciphertext) ON social_account_credentials "
            "FROM ai_smm_cc_rw",
            "ciphertext",
        ),
        (
            "GRANT ai_smm_cc TO ai_smm_cc_rw",
            "REVOKE ai_smm_cc FROM ai_smm_cc_rw",
            "role.member_of|",
        ),
    ],
)
def test_live_a_drifted_mutation_role_is_refused(
    deploy_dir: Path,
    live: DockerStub,
    admin_database_url: str,
    drift: str,
    undo: str,
    reason: str,
) -> None:
    pg_roles.run_admin(admin_database_url, drift)

    try:
        result = run_script(
            WRAPPER, deploy_dir, "mutations", "enable", "--approved-by", "o", stub=live
        )
    finally:
        pg_roles.run_admin(admin_database_url, undo)

    _assert_refused(result, live, reason)
    assert not (deploy_dir / STATE).exists()


# -- the override, rendered by the real Compose --------------------------------


def _compose_available() -> bool:
    if shutil.which("docker") is None:
        return False

    return (
        subprocess.run(
            ["docker", "compose", "version"], capture_output=True, check=False
        ).returncode
        == 0
    )


@pytest.mark.skipif(not _compose_available(), reason="docker compose not available")
@pytest.mark.parametrize("with_override", [False, True])
def test_the_override_changes_only_the_role_and_the_flag(
    deploy_dir: Path, tmp_path: Path, with_override: bool
) -> None:
    files = ["-f", "compose.control-center.yml"]

    if with_override:
        files += ["-f", "compose.control-center.mutations.yml"]

    def render(extra: list[str]) -> dict:
        rendered = subprocess.run(
            [
                "docker",
                "compose",
                "-p",
                "ai-smm-control-center",
                *extra,
                "--env-file",
                "control-center.compose.env",
                "config",
                "--format",
                "json",
            ],
            cwd=deploy_dir,
            capture_output=True,
            text=True,
            env={
                "PATH": os.environ.get("PATH", ""),
                "HOME": str(tmp_path),
                "DOCKER_CONFIG": str(tmp_path / ".docker"),
            },
            check=False,
        )
        assert rendered.returncode == 0, rendered.stderr

        return json.loads(rendered.stdout)["services"]

    services = render(files)
    base = render(["-f", "compose.control-center.yml"])
    api = services["smm-api"]["environment"]

    if with_override:
        assert api["AI_SMM_DATABASE_URL"] == FIXTURE_RW_DSN
        assert api["AI_SMM_API_MUTATIONS_ENABLED"] == "true"
    else:
        assert api["AI_SMM_DATABASE_URL"] == FIXTURE_API_DSN
        assert api["AI_SMM_API_MUTATIONS_ENABLED"] == "false"

    # Nothing else moves: not the other services, not smm-api's other keys.
    assert {k: v for k, v in services.items() if k != "smm-api"} == {
        k: v for k, v in base.items() if k != "smm-api"
    }
    changed = {"environment", "env_file"}
    assert {k: v for k, v in services["smm-api"].items() if k not in changed} == {
        k: v for k, v in base["smm-api"].items() if k not in changed
    }
    unchanged = {"AI_SMM_DATABASE_URL", "AI_SMM_API_MUTATIONS_ENABLED"}
    assert {k: v for k, v in api.items() if k not in unchanged} == {
        k: v for k, v in base["smm-api"]["environment"].items() if k not in unchanged
    }
    assert FIXTURE_DSN not in json.dumps(services["smm-api"])


# -- this release: read-only, whatever is on the host ----------------------------
#
# The scripts exactly as shipped (CC_RELEASE_PERMITS_MUTATIONS=no), with
# every file mutation mode would need present in the deployment directory.


def test_this_release_does_not_permit_mutations() -> None:
    gate = (SHIPPED_WRAPPER.parent / "cc-gate.sh").read_text(encoding="utf-8")

    assert f"\n{PERMITS}no\n" in gate


def test_this_release_refuses_enable_before_anything_runs(
    deploy_dir: Path, stub: DockerStub
) -> None:
    result = run_script(
        SHIPPED_WRAPPER, deploy_dir, "mutations", "enable", "--approved-by", "o",
        stub=stub,
    )

    _assert_refused(result, stub, "this release does not permit business mutations")
    assert "FU-025C-1" in result.stderr
    assert stub.calls() == []
    assert not (deploy_dir / STATE).exists()


@pytest.mark.parametrize(
    "args",
    [
        ("up",),
        ("restart", "smm-api"),
        ("restart", "smm-web"),
        ("preflight",),
        ("migrate", "current"),
    ],
)
def test_this_release_never_honours_a_mode_file(
    deploy_dir: Path, stub: DockerStub, args: tuple[str, ...]
) -> None:
    """A mode file it did not write -- by hand, or left by another copy."""

    _write_state(deploy_dir)

    result = run_script(SHIPPED_WRAPPER, deploy_dir, *args, stub=stub)

    _assert_refused(result, stub, "this release does not permit business mutations")
    assert _verify_sqls(stub) == []


def test_this_release_reports_and_removes_a_mode_file(
    deploy_dir: Path, stub: DockerStub
) -> None:
    _write_state(deploy_dir)

    status = run_script(SHIPPED_WRAPPER, deploy_dir, "mutations", "status", stub=stub)
    output = _output(status)

    assert status.returncode == 0, output
    assert "mode: read-only -- a mode file" in output
    assert "release: does not permit business mutations" in output

    result = run_script(SHIPPED_WRAPPER, deploy_dir, "mutations", "disable", stub=stub)

    assert result.returncode == 0, _output(result)
    assert not (deploy_dir / STATE).exists()
    assert _verify_sqls(stub) == ["ai_smm_cc"]
    assert _files_of(stub.compose_calls()[-1]) == ["compose.control-center.yml"]


@pytest.mark.parametrize(
    "args",
    [
        ("up",),
        ("restart", "smm-api"),
        ("preflight",),
        ("ps",),
        ("down",),
        ("config", "--quiet"),
        ("migrate", "current"),
        ("mutations", "status"),
    ],
)
def test_a_normal_deployment_stays_read_only_with_every_mutation_file_present(
    deploy_dir: Path, stub: DockerStub, args: tuple[str, ...]
) -> None:
    """Neither the override nor the mutation role's URL is ever loaded."""

    result = run_script(SHIPPED_WRAPPER, deploy_dir, *args, stub=stub)
    output = _output(result)

    assert result.returncode == 0, output
    assert not (deploy_dir / STATE).exists()

    for compose in stub.compose_calls():
        assert _files_of(compose) == ["compose.control-center.yml"]

    assert set(_verify_sqls(stub)) <= {"ai_smm_cc"}

    for call in stub.calls():
        assert "fixture-rw-pw" not in json.dumps(call)
