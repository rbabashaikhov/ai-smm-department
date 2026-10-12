"""The API role gate (cc-gate.sh, SMM-024C-2): smm-api starts only as
ai_smm_cc, and only when PostgreSQL says the role is what was designed.

Guarded: cc-compose.sh up, restart, preflight. Not guarded (they keep the
owner role and never start the API): migrate, create-owner.

Each refusal must exit 2, name the reason, call no Compose and print no
password. Most cases answer the verify query from the fake `docker`
(tests/cc_stub.py); the last ones run the real ai_smm_cc.verify.sql
against the real test database and role (tests/test_db_least_privilege.py
explains the role), so a drifted privilege is caught on PostgreSQL itself.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests import pg_roles
from tests.cc_stub import (
    FIXTURE_API_DSN,
    FIXTURE_DSN,
    FIXTURE_WORKER,
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

PASSWORDS = ("fixture-pw", "fixture-api-pw")
#: The commit whose compose file runs in production since 2026-10-11 (its
#: compose file is identical to 8ccccda's).
DEPLOYED_2026_10_11 = "bc60b48bf79eeb44f744f4bd6ff3058794f0c7f3"


@pytest.fixture
def deploy_dir(tmp_path: Path) -> Iterator[Path]:
    with docker_socket() as host:
        yield write_deploy_dir(tmp_path / "ai-smm-control-center", docker_host=host)


@pytest.fixture
def stub(tmp_path: Path) -> DockerStub:
    return DockerStub(tmp_path / "stub-bin")


def _verify_calls(stub: DockerStub) -> list[list[str]]:
    return [
        call["argv"]
        for call in stub.calls()
        if call["argv"][:1] == ["exec"]
        and "psql" in call["argv"]
        and any("'checks'" in arg for arg in call["argv"])
    ]


def _assert_refused(result, stub: DockerStub, reason: str) -> None:
    output = result.stdout + result.stderr

    assert result.returncode == 2, output
    assert reason in output, output
    assert stub.compose_calls() == []

    for password in PASSWORDS:
        assert password not in output


def _write_api_env(deploy_dir: Path, content: str, mode: int = 0o600) -> None:
    path = deploy_dir / "control-center.api.env"
    path.write_text(content, encoding="utf-8")
    path.chmod(mode)


# -- what is guarded ---------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [("up",), ("restart", "smm-api"), ("restart", "smm-web"), ("preflight",)],
)
def test_starting_the_api_passes_the_role_gate(
    deploy_dir: Path, stub: DockerStub, args: tuple[str, ...]
) -> None:
    result = run_script(WRAPPER, deploy_dir, *args, stub=stub)
    output = result.stdout + result.stderr

    assert result.returncode == 0, output
    assert "gate api-role: PASS -- smm-api connects as ai_smm_cc" in output
    assert "gate api-role: INFO database.temporary|" in output
    assert len(_verify_calls(stub)) == 1

    # Read-only, as the container's superuser over its socket.
    call = _verify_calls(stub)[0]

    assert "PGOPTIONS=-c default_transaction_read_only=on" in call
    assert call[call.index("-U") + 1] == "pgadmin"

    for password in PASSWORDS:
        assert password not in output
        assert not any(password in arg for arg in call)


@pytest.mark.parametrize(
    "args",
    [
        ("migrate", "current"),
        (
            "create-owner", "--email", "o@example.test",
            "--display-name", "O", "--project", "demo",
        ),
        ("ps",),
        ("down",),
    ],
)
def test_tools_that_keep_the_owner_role_do_not_run_it(
    deploy_dir: Path, stub: DockerStub, args: tuple[str, ...]
) -> None:
    result = run_script(WRAPPER, deploy_dir, *args, stub=stub)

    assert result.returncode == 0, result.stderr
    assert _verify_calls(stub) == []


def test_a_role_that_fails_verification_stops_up_before_compose(
    deploy_dir: Path, stub: DockerStub
) -> None:
    stub.update(api_role_answer=api_role_answer(
        "FAIL|table.publications.update|denied"
    ))

    result = run_script(WRAPPER, deploy_dir, "up", stub=stub)

    _assert_refused(
        result, stub, "ai_smm_cc is not as designed: table.publications.update|denied"
    )


@pytest.mark.parametrize("args", [("up",), ("restart", "smm-api")])
def test_the_role_gate_answers_before_compose_runs(
    deploy_dir: Path, stub: DockerStub, args: tuple[str, ...]
) -> None:
    result = run_script(WRAPPER, deploy_dir, *args, stub=stub)

    assert result.returncode == 0, result.stderr

    argvs = [call["argv"] for call in stub.calls()]
    verify_at = next(
        i for i, argv in enumerate(argvs) if argv in _verify_calls(stub)
    )
    compose_at = [i for i, argv in enumerate(argvs) if argv[:1] == ["compose"]]

    assert compose_at and min(compose_at) > verify_at


# -- files from another release, or edited on the host ---------------------------


def test_a_substituted_verify_file_is_refused(
    deploy_dir: Path, stub: DockerStub
) -> None:
    """Even one whose output would pass: it is never run."""

    # Its output would satisfy every rule the gate reads: 240 lines, the
    # required checks, a matching END. It must never get to run.
    required = (
        "PASS|role.exists|ai_smm_cc", "PASS|role.attributes|",
        "PASS|role.password|set, SCRAM-SHA-256", "PASS|role.member_of|none",
        "PASS|role.owns_nothing|0 owned objects", "PASS|schema.public.create|",
        "PASS|table.publications.update|denied",
    )
    (deploy_dir / "ai_smm_cc.verify.sql").write_text(
        "-- 'checks' ai_smm_cc -- forged\n"
        "SELECT x FROM (SELECT unnest(ARRAY["
        + ", ".join(f"'{line}'" for line in required)
        + "]) AS x UNION ALL SELECT 'PASS|f.' || g || '|' "
        "FROM generate_series(1, 233) g UNION ALL "
        "SELECT 'END|checks|240') t\n",
        encoding="utf-8",
    )
    stub.update(api_role_answer=api_role_answer())

    result = run_script(WRAPPER, deploy_dir, "up", stub=stub)

    _assert_refused(
        result, stub, "ai_smm_cc.verify.sql is not the file this release ships"
    )
    assert _verify_calls(stub) == []


def test_a_one_byte_change_to_the_verify_file_is_refused(
    deploy_dir: Path, stub: DockerStub
) -> None:
    path = deploy_dir / "ai_smm_cc.verify.sql"
    path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")

    result = run_script(WRAPPER, deploy_dir, "restart", "smm-api", stub=stub)

    _assert_refused(result, stub, "is not the file this release ships")


def _previous_compose() -> str:
    """compose.control-center.yml as deployed on 2026-10-11 (bc60b48)."""

    if shutil.which("git") is None:
        pytest.skip("git not available")

    result = subprocess.run(
        ["git", "show", f"{DEPLOYED_2026_10_11}:compose.control-center.yml"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )

    if result.returncode != 0:
        pytest.skip("the 2026-10-11 compose file is not in this checkout")

    return result.stdout


def test_the_previous_compose_file_with_the_new_scripts_is_refused(
    deploy_dir: Path, stub: DockerStub
) -> None:
    """The divergence that matters: that file gives smm-api the OWNER URL."""

    previous = _previous_compose()
    assert "control-center.api.env" not in previous

    (deploy_dir / "compose.control-center.yml").write_text(previous, encoding="utf-8")

    for args in (("up",), ("restart", "smm-api"), ("preflight",)):
        result = run_script(WRAPPER, deploy_dir, *args, stub=stub)

        _assert_refused(
            result,
            stub,
            "compose.control-center.yml is not the file this release ships",
        )

    # Stopping and inspecting still work with it: a rollback must not be
    # blocked by the gate that guards starting.
    for args in (("ps",), ("down",)):
        stub.reset_calls()
        result = run_script(WRAPPER, deploy_dir, *args, stub=stub)

        assert result.returncode == 0, (args, result.stderr)


# -- the URL in control-center.api.env -----------------------------------------


API_DSN_CASES = {
    "the owner's url": (FIXTURE_DSN, "connects as 'fixture'"),
    "another user": (
        FIXTURE_API_DSN.replace("ai_smm_cc:", "postgres:"),
        "connects as 'postgres'",
    ),
    "another host": (
        FIXTURE_API_DSN.replace("@fixture-db:", "@postgres:"),
        "points at postgres:5432/ai_smm",
    ),
    "another port": (
        FIXTURE_API_DSN.replace(":5432/", ":5433/"),
        "points at fixture-db:5433/ai_smm",
    ),
    "another database": (
        FIXTURE_API_DSN.replace(":5432/ai_smm", ":5432/n8n"),
        "points at fixture-db:5432/n8n",
    ),
    "a query string": (
        FIXTURE_API_DSN + "?host=elsewhere",
        "cannot be read unambiguously",
    ),
    "no password": (
        FIXTURE_API_DSN.replace("ai_smm_cc:fixture-api-pw@", "ai_smm_cc@"),
        "carries no password",
    ),
    "an empty password": (
        FIXTURE_API_DSN.replace("ai_smm_cc:fixture-api-pw@", "ai_smm_cc:@"),
        "carries no password",
    ),
}


@pytest.mark.parametrize("case", sorted(API_DSN_CASES))
def test_the_api_url_must_be_the_api_role_on_the_recorded_database(
    deploy_dir: Path, stub: DockerStub, case: str
) -> None:
    dsn, reason = API_DSN_CASES[case]
    _write_api_env(deploy_dir, f"AI_SMM_DATABASE_URL={dsn}\n")

    result = run_script(WRAPPER, deploy_dir, "up", stub=stub)

    _assert_refused(result, stub, reason)
    # Refused before the database was asked anything about the role.
    assert _verify_calls(stub) == []


def test_the_owner_file_must_not_hold_the_api_role(
    deploy_dir: Path, stub: DockerStub
) -> None:
    """Even when the worker were (wrongly) moved to it as well."""

    (deploy_dir / "control-center.env").write_text(
        f"AI_SMM_DATABASE_URL={FIXTURE_API_DSN}\n"
    )
    worker = stub.container(FIXTURE_WORKER)
    worker["env"] = [f"AI_SMM_DATABASE_URL={FIXTURE_API_DSN}"]
    stub.save()

    result = run_script(WRAPPER, deploy_dir, "up", stub=stub)

    _assert_refused(result, stub, "it must keep the owner role")


FILE_CASES = {
    "missing": (
        lambda d: (d / "control-center.api.env").unlink(), "missing",
    ),
    "world-readable": (
        lambda d: (d / "control-center.api.env").chmod(0o644), "must be 600",
    ),
    "extra key": (
        lambda d: _write_api_env(
            d,
            f"AI_SMM_DATABASE_URL={FIXTURE_API_DSN}\n"
            "AI_SMM_API_MUTATIONS_ENABLED=true\n",
        ),
        "must define exactly: AI_SMM_DATABASE_URL",
    ),
    "verify file missing": (
        lambda d: (d / "ai_smm_cc.verify.sql").unlink(), "missing",
    ),
    "verify file symlink": (
        lambda d: _replace_with_symlink(d / "ai_smm_cc.verify.sql"), "symlink",
    ),
}


def _replace_with_symlink(path: Path) -> None:
    target = path.with_name(path.name + ".real")
    path.rename(target)
    path.symlink_to(target)


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


def test_down_needs_the_api_file_too(deploy_dir: Path, stub: DockerStub) -> None:
    """Compose cannot load the project without it: the wrapper says so first."""

    (deploy_dir / "control-center.api.env").unlink()

    result = run_script(WRAPPER, deploy_dir, "down", stub=stub)

    assert result.returncode == 2
    assert "missing" in result.stderr and "control-center.api.env" in result.stderr


# -- the verify output is read strictly ----------------------------------------


def _answer_without(line: str) -> str:
    return "\n".join(
        part for part in api_role_answer().splitlines() if part != line
    ).replace("END|checks|240", "END|checks|239")


VERIFY_OUTPUT_CASES = {
    "no END line": (
        "\n".join(api_role_answer().splitlines()[:-1]), "incomplete output",
    ),
    "END count differs": (
        api_role_answer().replace("END|checks|240", "END|checks|241"),
        "incomplete output",
    ),
    "output after END": (api_role_answer() + "\nPASS|x|y", "after the END line"),
    "too few checks": (api_role_answer(checks=20), "ran only 20 checks"),
    "unparseable line": (
        api_role_answer("WARN|something|odd"), "unexpected output line",
    ),
    "empty": ("", "unexpected output line"),
    "password check missing": (
        _answer_without("PASS|role.password|set, SCRAM-SHA-256"),
        "did not report 'PASS|role.password|set, SCRAM-SHA-256'",
    ),
    "no password": (
        api_role_answer("FAIL|role.password|not set: the role cannot log in"),
        "role.password|not set",
    ),
}


@pytest.mark.parametrize("case", sorted(VERIFY_OUTPUT_CASES))
def test_the_verify_output_is_read_strictly(
    deploy_dir: Path, stub: DockerStub, case: str
) -> None:
    answer, reason = VERIFY_OUTPUT_CASES[case]
    stub.update(api_role_answer=answer)

    result = run_script(WRAPPER, deploy_dir, "up", stub=stub)

    _assert_refused(result, stub, reason)


def test_an_unreachable_database_closes_the_gate(
    deploy_dir: Path, stub: DockerStub
) -> None:
    stub.update(api_role_rc=2)

    result = run_script(WRAPPER, deploy_dir, "up", stub=stub)

    _assert_refused(result, stub, "cannot run ai_smm_cc.verify.sql")


# -- the real verify file, on the real role -------------------------------------


@pytest.fixture
def live(stub: DockerStub, api_role, admin_database_url: str) -> DockerStub:
    stub.update(role_sql_url=pg_roles.libpq_url(admin_database_url))

    return stub


def test_live_the_provisioned_role_passes(deploy_dir: Path, live: DockerStub) -> None:
    result = run_script(WRAPPER, deploy_dir, "up", stub=live)
    output = result.stdout + result.stderr

    assert result.returncode == 0, output
    assert "gate api-role: PASS" in output
    assert len(live.compose_calls()) == 1


@pytest.mark.parametrize(
    ("drift", "reason"),
    [
        (
            "GRANT UPDATE ON publications TO ai_smm_cc",
            "table.publications.update|denied",
        ),
        ("GRANT DELETE ON audit_log TO PUBLIC", "table.audit_log.delete|denied"),
        ("REVOKE SELECT ON users FROM ai_smm_cc", "table.users.select|granted"),
        ("ALTER ROLE ai_smm_cc CREATEDB", "role.attributes|"),
        ("ALTER ROLE ai_smm_cc PASSWORD NULL", "role.password|not set"),
        ("GRANT pg_read_all_data TO ai_smm_cc", "role.member_of|pg_read_all_data"),
        (
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
            "GRANT SELECT ON TABLES TO ai_smm_cc",
            "role.default_privileges|",
        ),
        (
            "CREATE TABLE public.cc_drift_probe (id int); "
            "GRANT SELECT ON public.cc_drift_probe TO ai_smm_cc",
            "table.cc_drift_probe.select|denied",
        ),
    ],
)
def test_live_a_drifted_role_stops_up(
    deploy_dir: Path,
    live: DockerStub,
    admin_database_url: str,
    api_role,
    drift: str,
    reason: str,
) -> None:
    pg_roles.run_admin(admin_database_url, drift)

    try:
        result = run_script(WRAPPER, deploy_dir, "up", stub=live)
    finally:
        # Put the session's role back exactly as provisioned.
        pg_roles.run_admin(admin_database_url, "REVOKE DELETE ON audit_log FROM PUBLIC")
        pg_roles.run_admin(admin_database_url, "ALTER ROLE ai_smm_cc NOCREATEDB")
        pg_roles.run_admin(admin_database_url, "REVOKE pg_read_all_data FROM ai_smm_cc")
        pg_roles.run_admin(
            admin_database_url,
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
            "REVOKE SELECT ON TABLES FROM ai_smm_cc",
        )
        pg_roles.run_admin(
            admin_database_url, "DROP TABLE IF EXISTS public.cc_drift_probe"
        )
        pg_roles.run_script(admin_database_url, pg_roles.GRANTS_SQL)

        if "PASSWORD" in drift:
            pg_roles.restore_password(admin_database_url, api_role.url)

    _assert_refused(result, live, reason)


# -- what the gate cannot do -------------------------------------------------------


def _compose_available() -> bool:
    if shutil.which("docker") is None:
        return False

    return subprocess.run(
        ["docker", "compose", "version"], capture_output=True, check=False
    ).returncode == 0


@pytest.mark.skipif(not _compose_available(), reason="docker compose not available")
def test_bare_compose_runs_no_gate_the_wrapper_does(
    deploy_dir: Path, stub: DockerStub, tmp_path: Path
) -> None:
    """A documented limit, not a protection: the gate lives in the wrapper.

    With the owner URL in control-center.api.env, Compose itself renders
    smm-api with it without complaint -- only cc-compose.sh refuses. Who
    can run docker on the host can bypass any gate; the gate guards
    against mistakes, and the role guards against the API process.
    """

    _write_api_env(deploy_dir, f"AI_SMM_DATABASE_URL={FIXTURE_DSN}\n")

    rendered = subprocess.run(
        [
            "docker", "compose", "-p", "ai-smm-control-center",
            "-f", "compose.control-center.yml",
            "--env-file", "control-center.compose.env",
            "config", "--format", "json",
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
    env = json.loads(rendered.stdout)["services"]["smm-api"]["environment"]
    assert env["AI_SMM_DATABASE_URL"] == FIXTURE_DSN

    result = run_script(WRAPPER, deploy_dir, "up", stub=stub)

    _assert_refused(result, stub, "connects as 'fixture'")
