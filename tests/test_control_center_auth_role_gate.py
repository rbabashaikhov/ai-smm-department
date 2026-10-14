"""The auth role gate (cc-gate.sh, SMM-025D): smm-auth starts only as
ai_smm_auth, and only when PostgreSQL says the role and every smm_auth
function are what was designed.

Guarded: cc-compose.sh up, restart, preflight. Not guarded: the one-shot
tools (owner role) and the verbs that start nothing.

Each refusal must exit 2, name the reason, call no Compose and print no
password. Most cases answer the verify query from the fake `docker`
(tests/cc_stub.py); the live ones run the real ai_smm_auth.verify.sql
against the role installed as the operator installs it
(tests/test_db_auth_contracts.py's auth_stack).
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests import pg_roles
from tests.cc_stub import (
    FIXTURE_API_DSN,
    FIXTURE_AUTH_DSN,
    FIXTURE_DSN,
    WRAPPER,
    DockerStub,
    auth_role_answer,
    docker_socket,
    run_script,
    write_deploy_dir,
)
from tests.test_db_auth_contracts import AuthStack, auth_stack  # noqa: F401


pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash not available"
)

PASSWORDS = ("fixture-pw", "fixture-api-pw", "fixture-auth-pw")


@pytest.fixture
def deploy_dir(tmp_path: Path) -> Iterator[Path]:
    with docker_socket() as host:
        yield write_deploy_dir(tmp_path / "ai-smm-control-center", docker_host=host)


@pytest.fixture
def stub(tmp_path: Path) -> DockerStub:
    return DockerStub(tmp_path / "stub-bin")


def _auth_verify_calls(stub: DockerStub) -> list[list[str]]:
    return [
        call["argv"]
        for call in stub.calls()
        if call["argv"][:1] == ["exec"]
        and "psql" in call["argv"]
        and any("'checks'" in arg and "'ai_smm_auth'" in arg for arg in call["argv"])
    ]


def _assert_refused(result, stub: DockerStub, reason: str) -> None:
    output = result.stdout + result.stderr

    assert result.returncode == 2, output
    assert reason in output, output
    assert stub.compose_calls() == []

    for password in PASSWORDS:
        assert password not in output


def _write_auth_env(deploy_dir: Path, content: str, mode: int = 0o600) -> None:
    path = deploy_dir / "control-center.auth.env"
    path.write_text(content, encoding="utf-8")
    path.chmod(mode)


# -- what is guarded ---------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [("up",), ("restart", "smm-auth"), ("restart", "smm-api"), ("preflight",)],
)
def test_starting_the_control_center_passes_the_auth_role_gate(
    deploy_dir: Path, stub: DockerStub, args: tuple[str, ...]
) -> None:
    result = run_script(WRAPPER, deploy_dir, *args, stub=stub)
    output = result.stdout + result.stderr

    assert result.returncode == 0, output
    assert "gate auth-role: PASS -- smm-auth connects as ai_smm_auth" in output
    assert len(_auth_verify_calls(stub)) == 1

    call = _auth_verify_calls(stub)[0]

    assert "PGOPTIONS=-c default_transaction_read_only=on" in call
    assert call[call.index("-U") + 1] == "pgadmin"

    for password in PASSWORDS:
        assert password not in output
        assert not any(password in arg for arg in call)


def test_up_starts_the_three_http_services(deploy_dir: Path, stub: DockerStub) -> None:
    result = run_script(WRAPPER, deploy_dir, "up", stub=stub)

    assert result.returncode == 0, result.stderr
    assert stub.compose_calls()[0][-3:] == ["smm-api", "smm-auth", "smm-web"]


@pytest.mark.parametrize(
    "args",
    [
        ("migrate", "current"),
        (
            "create-owner",
            "--email",
            "o@example.test",
            "--display-name",
            "O",
            "--project",
            "demo",
        ),
        ("ps",),
        ("down",),
        ("logs", "smm-auth"),
    ],
)
def test_verbs_that_start_no_auth_service_do_not_run_it(
    deploy_dir: Path, stub: DockerStub, args: tuple[str, ...]
) -> None:
    result = run_script(WRAPPER, deploy_dir, *args, stub=stub)

    assert result.returncode == 0, result.stderr
    assert _auth_verify_calls(stub) == []


def test_the_gate_answers_before_compose_runs(
    deploy_dir: Path, stub: DockerStub
) -> None:
    result = run_script(WRAPPER, deploy_dir, "up", stub=stub)

    assert result.returncode == 0, result.stderr

    argvs = [call["argv"] for call in stub.calls()]
    verify_at = next(
        i for i, argv in enumerate(argvs) if argv in _auth_verify_calls(stub)
    )
    compose_at = [i for i, argv in enumerate(argvs) if argv[:1] == ["compose"]]

    assert compose_at and min(compose_at) > verify_at


# -- the URL ------------------------------------------------------------------------


AUTH_DSN_CASES = {
    "the API role": (
        FIXTURE_API_DSN.replace("fixture-api-pw", "other-pw"),
        "not the sign-in role ai_smm_auth",
    ),
    "the owner role": (
        FIXTURE_DSN.replace("fixture-pw", "other-pw"),
        "not the sign-in role ai_smm_auth",
    ),
    "the mutation role": (
        FIXTURE_AUTH_DSN.replace("ai_smm_auth:", "ai_smm_cc_rw:"),
        "not the sign-in role ai_smm_auth",
    ),
    "another host": (
        FIXTURE_AUTH_DSN.replace("@fixture-db:", "@postgres:"),
        "points at postgres:5432/ai_smm",
    ),
    "another port": (
        FIXTURE_AUTH_DSN.replace(":5432/", ":5433/"),
        "points at fixture-db:5433/ai_smm",
    ),
    "another database": (
        FIXTURE_AUTH_DSN.replace(":5432/ai_smm", ":5432/n8n"),
        "points at fixture-db:5432/n8n",
    ),
    "a query string": (
        FIXTURE_AUTH_DSN + "?host=elsewhere",
        "cannot be read unambiguously",
    ),
    "no password": (
        FIXTURE_AUTH_DSN.replace("ai_smm_auth:fixture-auth-pw@", "ai_smm_auth@"),
        "carries no password",
    ),
}


@pytest.mark.parametrize("case", sorted(AUTH_DSN_CASES))
def test_the_auth_url_must_be_the_sign_in_role_on_the_recorded_database(
    deploy_dir: Path, stub: DockerStub, case: str
) -> None:
    dsn, reason = AUTH_DSN_CASES[case]
    _write_auth_env(deploy_dir, f"AI_SMM_DATABASE_URL={dsn}\n")

    result = run_script(WRAPPER, deploy_dir, "up", stub=stub)

    _assert_refused(result, stub, reason)
    assert _auth_verify_calls(stub) == []


def test_the_api_file_must_not_hold_the_sign_in_role(
    deploy_dir: Path, stub: DockerStub
) -> None:
    """The API's file naming ai_smm_auth is refused (by the API role gate
    first: either way smm-api never gets the sign-in role)."""

    (deploy_dir / "control-center.api.env").write_text(
        f"AI_SMM_DATABASE_URL={FIXTURE_AUTH_DSN}\n"
    )

    result = run_script(WRAPPER, deploy_dir, "up", stub=stub)

    _assert_refused(result, stub, "not the API role ai_smm_cc")


FILE_CASES = {
    "missing": (lambda d: (d / "control-center.auth.env").unlink(), "missing"),
    "world-readable": (
        lambda d: (d / "control-center.auth.env").chmod(0o644),
        "must be 600",
    ),
    "extra key": (
        lambda d: _write_auth_env(
            d,
            f"AI_SMM_DATABASE_URL={FIXTURE_AUTH_DSN}\n"
            "AI_SMM_AUTH_ALLOWED_PEERS=0.0.0.0/0\n",
        ),
        "must define exactly: AI_SMM_DATABASE_URL",
    ),
    "verify file missing": (
        lambda d: (d / "ai_smm_auth.verify.sql").unlink(),
        "missing",
    ),
    "verify file edited": (
        lambda d: (d / "ai_smm_auth.verify.sql").write_text(
            (d / "ai_smm_auth.verify.sql").read_text() + "\n"
        ),
        "ai_smm_auth.verify.sql is not the file this release ships",
    ),
}


@pytest.mark.parametrize("case", sorted(FILE_CASES))
def test_the_files_are_checked_before_anything_runs(
    deploy_dir: Path, stub: DockerStub, case: str
) -> None:
    change, reason = FILE_CASES[case]
    change(deploy_dir)

    result = run_script(WRAPPER, deploy_dir, "up", stub=stub)

    assert result.returncode == 2, result.stderr
    assert reason in result.stderr, result.stderr
    assert stub.compose_calls() == []


def test_down_needs_the_auth_file_too(deploy_dir: Path, stub: DockerStub) -> None:
    (deploy_dir / "control-center.auth.env").unlink()

    result = run_script(WRAPPER, deploy_dir, "down", stub=stub)

    assert result.returncode == 2
    assert "missing" in result.stderr and "control-center.auth.env" in result.stderr


# -- the verify output ---------------------------------------------------------------


def _answer_without(line: str) -> str:
    return "\n".join(
        part for part in auth_role_answer().splitlines() if part != line
    ).replace("END|checks|950", "END|checks|949")


VERIFY_OUTPUT_CASES = {
    "a FAIL": (
        auth_role_answer(
            "FAIL|function.smm_auth.session_create(text).body|md5 differs"
        ),
        "ai_smm_auth is not as designed: function.smm_auth.session_create(text).body",
    ),
    "no END line": (
        "\n".join(auth_role_answer().splitlines()[:-1]),
        "incomplete output",
    ),
    "output after END": (auth_role_answer() + "\nPASS|x|y", "after the END line"),
    "too few checks": (auth_role_answer(checks=40), "ran only 40 checks"),
    "unparseable line": (auth_role_answer("WARN|x|y"), "unexpected output line"),
    "functions not checked": (
        _answer_without("PASS|schema.smm_auth.objects|designed functions only"),
        "did not report 'PASS|schema.smm_auth.objects|designed functions only'",
    ),
}


@pytest.mark.parametrize("case", sorted(VERIFY_OUTPUT_CASES))
def test_the_verify_output_is_read_strictly(
    deploy_dir: Path, stub: DockerStub, case: str
) -> None:
    answer, reason = VERIFY_OUTPUT_CASES[case]
    stub.update(auth_role_answer=answer)

    result = run_script(WRAPPER, deploy_dir, "up", stub=stub)

    _assert_refused(result, stub, reason)


def test_an_unreachable_database_closes_the_gate(
    deploy_dir: Path, stub: DockerStub
) -> None:
    stub.update(auth_role_rc=2)

    result = run_script(WRAPPER, deploy_dir, "up", stub=stub)

    _assert_refused(result, stub, "cannot run ai_smm_auth.verify.sql")


# -- the real verify file, on the real role -------------------------------------


@pytest.fixture
def live(
    stub: DockerStub,
    auth_stack: AuthStack,  # noqa: F811
    admin_database_url: str,
) -> DockerStub:
    stub.update(auth_sql_url=pg_roles.libpq_url(admin_database_url))

    return stub


def test_live_the_installed_role_passes(deploy_dir: Path, live: DockerStub) -> None:
    result = run_script(WRAPPER, deploy_dir, "up", stub=live)
    output = result.stdout + result.stderr

    assert result.returncode == 0, output
    assert "gate auth-role: PASS" in output
    assert len(live.compose_calls()) == 1


def test_live_a_drifted_function_stops_up(
    deploy_dir: Path, live: DockerStub, admin_database_url: str
) -> None:
    """A redefined smm_auth function body is caught by its md5 pin."""

    source = pg_roles.query_admin(
        admin_database_url,
        "SELECT pg_get_functiondef("
        "'smm_auth.recent_login_failures(text)'::regprocedure)",
    )[0][0]
    drifted = source.replace("interval '15 minutes'", "interval '1 second'")

    assert drifted != source

    pg_roles.run_admin(admin_database_url, drifted)

    try:
        result = run_script(WRAPPER, deploy_dir, "up", stub=live)

        _assert_refused(result, live, "ai_smm_auth is not as designed")
    finally:
        pg_roles.run_admin(admin_database_url, source)
