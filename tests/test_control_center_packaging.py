"""Safety properties of the PRODUCTION Control Center packaging.

compose.control-center.yml runs smm-api and smm-web next to a live,
publishing worker and the shared PostgreSQL. These tests pin what makes
that safe -- no worker, no database service, no build, one loopback
port, literal read-only settings, migrations only on explicit command,
the shared network attached only where the database is needed -- and
what cc-compose.sh enforces around Compose's own precedence rules.

The YAML tests need nothing. The wrapper tests render the file with
`docker compose config` against fixture env files (no daemon, no
container, no network is created) and are skipped where the Docker CLI
is missing. Nothing here reads a production file or needs a credential.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
COMPOSE_PATH = ROOT / "compose.control-center.yml"
WORKER_COMPOSE_PATH = ROOT / "compose.yaml"
DEPLOY_DIR = ROOT / "deploy" / "control-center"
WRAPPER = DEPLOY_DIR / "cc-compose.sh"
SECRET_EXAMPLE = DEPLOY_DIR / "control-center.env.example"
INTERP_EXAMPLE = DEPLOY_DIR / "control-center.compose.env.example"
ALEMBIC_ENV = ROOT / "alembic" / "env.py"

PROJECT = "ai-smm-control-center"
HTTP_SERVICES = ("smm-api", "smm-web")
TOOL_SERVICES = ("migrate", "cli")
PYTHON_SERVICES = ("smm-api", "migrate", "cli")
ALL_SERVICES = (*HTTP_SERVICES, *TOOL_SERVICES)

FIXTURE_VERSION = "abc1234"
FIXTURE_NETWORK = "cc-fixture-shared"
FIXTURE_DSN = "postgresql+psycopg://fixture:fixture@fixture-db:5432/fixture"


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    return yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def services(compose: dict[str, Any]) -> dict[str, Any]:
    return compose["services"]


def _networks(service: dict[str, Any]) -> set[str]:
    networks = service.get("networks") or {}

    return set(networks)


def _env_keys(path: Path) -> list[str]:
    keys = []

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()

        if not line or line.startswith("#"):
            continue

        keys.append(line.split("=", 1)[0])

    return keys


# -- topology --------------------------------------------------------------


def test_is_its_own_project(compose: dict[str, Any]) -> None:
    worker = yaml.safe_load(WORKER_COMPOSE_PATH.read_text(encoding="utf-8"))

    assert compose["name"] == PROJECT
    assert worker["name"] == "ai-smm"
    assert compose["name"] != worker["name"]


def test_only_the_control_center_services(services: dict[str, Any]) -> None:
    """No worker, scheduler or PostgreSQL in this project."""

    assert set(services) == set(ALL_SERVICES)

    for name, service in services.items():
        rendered = yaml.safe_dump(service)

        for forbidden in ("ai-smm-worker", "ai-smm-healthcheck", "postgres:"):
            assert forbidden not in rendered, (name, forbidden)

        assert "worker" not in name
        assert "scheduler" not in name
        assert not service["image"].startswith(("postgres", "ai-smm:")), name


def test_images_are_prebuilt_never_built_or_pulled(
    services: dict[str, Any],
) -> None:
    for name, service in services.items():
        assert "build" not in service, name
        assert service["pull_policy"] == "never", name
        assert "latest" not in service["image"], name

    version = "${SMM_CC_VERSION:?"

    assert services["smm-api"]["image"].startswith(f"ai-smm-api:{version}")
    assert services["smm-web"]["image"].startswith(f"ai-smm-web:{version}")

    for name in TOOL_SERVICES:
        assert services[name]["image"] == services["smm-api"]["image"], name


def test_only_web_publishes_a_port_on_loopback_8088(
    services: dict[str, Any],
) -> None:
    for name, service in services.items():
        if name != "smm-web":
            assert "ports" not in service, name
            assert "expose" not in service, name

    # A literal: no variable can move it to 0.0.0.0 or another port.
    assert services["smm-web"]["ports"] == ["127.0.0.1:8088:8080"]


def test_network_definitions(compose: dict[str, Any]) -> None:
    networks = compose["networks"]

    assert set(networks) == {"cc-internal", "cc-edge", "shared"}

    assert networks["cc-internal"]["internal"] is True
    assert networks["cc-edge"].get("internal", False) is False
    assert networks["cc-edge"].get("external", False) is False

    shared = networks["shared"]

    assert shared["external"] is True
    assert shared["name"].startswith("${SMM_CC_SHARED_NETWORK:?")
    assert "internal" not in shared


def test_network_attachments(services: dict[str, Any]) -> None:
    # The web tier never sees the shared network (database, other projects).
    assert _networks(services["smm-web"]) == {"cc-edge", "cc-internal"}
    # The API: reached by web on cc-internal, reaches the database on shared.
    assert _networks(services["smm-api"]) == {"cc-internal", "shared"}

    # One-shot tools: the database only.
    for name in TOOL_SERVICES:
        assert _networks(services[name]) == {"shared"}, name

    attached_to_shared = {
        name for name, service in services.items()
        if "shared" in _networks(service)
    }

    assert attached_to_shared == {"smm-api", *TOOL_SERVICES}

    for name, service in services.items():
        assert "network_mode" not in service, name


def test_no_public_routing(services: dict[str, Any]) -> None:
    for name, service in services.items():
        labels = service["labels"]

        assert labels["traefik.enable"] == "false", name
        assert not any(
            key.startswith("traefik.http") for key in labels
        ), name


def test_startup_order(services: dict[str, Any]) -> None:
    assert services["smm-web"]["depends_on"] == {
        "smm-api": {"condition": "service_healthy"}
    }

    for name in ("smm-api", *TOOL_SERVICES):
        assert "depends_on" not in services[name], name


# -- configuration and secrets -----------------------------------------------


def test_only_the_dedicated_env_file_is_read(services: dict[str, Any]) -> None:
    for name in PYTHON_SERVICES:
        assert services[name]["env_file"] == ["control-center.env"], name

    assert "env_file" not in services["smm-web"]

    text = COMPOSE_PATH.read_text(encoding="utf-8")
    uncommented = re.sub(r"#[^\n]*", "", text)

    assert "ai-smm.env" not in uncommented
    assert ".env\n" not in uncommented.replace("control-center.env\n", "")


def test_safety_settings_are_literals(services: dict[str, Any]) -> None:
    """environment: beats env_file:, so these cannot be overridden."""

    for name in PYTHON_SERVICES:
        env = services[name]["environment"]

        assert env["AI_SMM_API_MUTATIONS_ENABLED"] == "false", name
        assert env["AI_SMM_API_DOCS_ENABLED"] == "false", name
        assert env["AI_SMM_DRY_RUN"] == "true", name
        assert env["THREADS_API_BASE_URL"] == "http://127.0.0.1:9", name

        for credential in (
            "OPENAI_API_KEY",
            "THREADS_ACCESS_TOKEN",
            "LANGFUSE_PUBLIC_KEY",
            "LANGFUSE_SECRET_KEY",
            "STORAGE_SSH_PASSWORD",
            "STORAGE_SSH_KEY_PATH",
        ):
            assert env[credential] == "", (name, credential)

        # The database URL comes from control-center.env only.
        assert "AI_SMM_DATABASE_URL" not in env, name

        for key, value in env.items():
            assert "${" not in str(value), (name, key)


def test_interpolation_is_limited_and_required() -> None:
    """Two variables, both required, neither with a default."""

    text = COMPOSE_PATH.read_text(encoding="utf-8")
    uncommented = re.sub(r"#[^\n]*", "", text)
    references = re.findall(r"\$\{([A-Za-z0-9_]+)([^}]*)\}", uncommented)

    assert {name for name, _ in references} == {
        "SMM_CC_VERSION",
        "SMM_CC_SHARED_NETWORK",
    }

    for name, modifier in references:
        assert modifier.startswith(":?"), (name, modifier)

    assert "$$" not in uncommented


def test_secret_example_holds_only_the_database_url() -> None:
    assert _env_keys(SECRET_EXAMPLE) == ["AI_SMM_DATABASE_URL"]

    text = SECRET_EXAMPLE.read_text(encoding="utf-8")

    assert ":CHANGE_ME@<postgres-container>:5432/ai_smm" in text


def test_interpolation_example_holds_only_its_two_keys() -> None:
    assert sorted(_env_keys(INTERP_EXAMPLE)) == [
        "SMM_CC_SHARED_NETWORK",
        "SMM_CC_VERSION",
    ]


def test_filled_env_files_are_ignored_by_git() -> None:
    if shutil.which("git") is None or not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")

    for path in (
        "deploy/control-center/control-center.env",
        "deploy/control-center/control-center.compose.env",
        "control-center.env",
        "control-center.compose.env",
    ):
        result = subprocess.run(
            ["git", "check-ignore", "--quiet", path], cwd=ROOT, check=False
        )

        assert result.returncode == 0, f"{path} would be committable"

    for path in (SECRET_EXAMPLE, INTERP_EXAMPLE):
        result = subprocess.run(
            ["git", "check-ignore", "--quiet", str(path.relative_to(ROOT))],
            cwd=ROOT,
            check=False,
        )

        assert result.returncode == 1, f"{path} must stay tracked"


def test_env_files_never_enter_an_image() -> None:
    """The API build context excludes deploy/ and every .env variant."""

    ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()

    for pattern in (".env", ".env.*", "deploy/"):
        assert pattern in ignored, pattern


@pytest.mark.parametrize(
    "path", [COMPOSE_PATH, WRAPPER, SECRET_EXAMPLE, INTERP_EXAMPLE]
)
def test_packaging_files_hold_no_secret_looking_values(path: Path) -> None:
    text = path.read_text(encoding="utf-8")

    for pattern in (
        r"sk-[A-Za-z0-9]{10,}",
        r"THAA[A-Za-z0-9]{10,}",
        r"BEGIN [A-Z ]*PRIVATE KEY",
        # A DSN with a password other than the CHANGE_ME placeholder.
        r"postgresql\+psycopg://[^:\s]+:(?!CHANGE_ME@)[^@\s]+@",
    ):
        assert not re.search(pattern, text), pattern


# -- hardening ---------------------------------------------------------------


def test_containers_are_hardened(services: dict[str, Any]) -> None:
    for name, service in services.items():
        assert service["read_only"] is True, name
        assert service["cap_drop"] == ["ALL"], name
        assert "cap_add" not in service, name
        assert "no-new-privileges:true" in service["security_opt"], name
        assert service.get("privileged", False) is False, name

        user = str(service["user"])
        uid = user.split(":", 1)[0]

        assert uid not in ("0", "root", ""), name
        assert service["tmpfs"], name

        assert service["pids_limit"] > 0, name
        assert service["mem_limit"], name
        # No swap beyond the memory limit.
        assert service["memswap_limit"] == service["mem_limit"], name
        assert service["cpus"] > 0, name

        logging = service["logging"]

        assert logging["driver"] == "json-file", name
        assert logging["options"]["max-size"], name
        assert logging["options"]["max-file"], name

        for forbidden in ("pid", "ipc", "devices", "userns_mode"):
            assert forbidden not in service, (name, forbidden)


def test_nothing_is_mounted(services: dict[str, Any]) -> None:
    """No Docker socket, no bind mount, no volume: the containers are sealed."""

    for name, service in services.items():
        assert "volumes" not in service, name

    assert "volumes" not in yaml.safe_load(
        COMPOSE_PATH.read_text(encoding="utf-8")
    )
    assert "docker.sock" not in COMPOSE_PATH.read_text(encoding="utf-8")


def test_http_services_restart_and_tools_do_not(
    services: dict[str, Any],
) -> None:
    for name in HTTP_SERVICES:
        assert services[name]["restart"] == "unless-stopped", name
        assert "healthcheck" in services[name], name

    for name in TOOL_SERVICES:
        assert services[name]["restart"] == "no", name


# -- migrations and tools --------------------------------------------------


def test_tools_are_never_started_by_up(services: dict[str, Any]) -> None:
    """Profiles of their own: a COMPOSE_PROFILES=tools left over from the
    worker's project or the local runtime does not activate them."""

    assert services["migrate"]["profiles"] == ["cc-migrate"]
    assert services["cli"]["profiles"] == ["cc-admin"]

    for name in HTTP_SERVICES:
        assert "profiles" not in services[name], name


def test_migrate_has_no_default_command(services: dict[str, Any]) -> None:
    migrate = services["migrate"]

    assert migrate["entrypoint"] == ["/app/.venv/bin/alembic"]
    assert "command" not in migrate
    assert migrate["working_dir"] == "/app"


def test_migrate_fails_fast_on_locks(services: dict[str, Any]) -> None:
    options = services["migrate"]["environment"]["PGOPTIONS"]

    assert re.search(r"-c lock_timeout=\d+s", options)
    assert re.search(r"-c statement_timeout=\d+s", options)


def test_migrations_run_in_one_transaction() -> None:
    """A lock timeout or error rolls back every pending revision, so the
    database is never left between 79731eadacd7 and head."""

    source = ALEMBIC_ENV.read_text(encoding="utf-8")

    assert "transaction_per_migration" not in source
    assert "context.begin_transaction()" in source


def test_the_api_runs_the_image_entrypoint_only(
    services: dict[str, Any],
) -> None:
    api = services["smm-api"]

    assert "entrypoint" not in api
    assert "command" not in api


def test_cli_is_pinned_to_user_management(services: dict[str, Any]) -> None:
    """No schedule, approve, publish-now or import from this project."""

    cli = services["cli"]

    assert cli["entrypoint"] == ["/app/.venv/bin/ai-smm", "user"]
    assert "command" not in cli


def test_worker_compose_is_untouched_by_this_packaging() -> None:
    """The worker keeps its own file, project, env file and network var."""

    worker = yaml.safe_load(WORKER_COMPOSE_PATH.read_text(encoding="utf-8"))

    assert set(worker["services"]) == {"worker", "cli", "migrate"}
    assert worker["x-common"]["env_file"] == ["ai-smm.env"]
    assert "smm-api" not in WORKER_COMPOSE_PATH.read_text(encoding="utf-8")


# -- wrapper and rendered configuration --------------------------------------


def _docker_compose_available() -> bool:
    if shutil.which("docker") is None or shutil.which("bash") is None:
        return False

    result = subprocess.run(
        ["docker", "compose", "version"],
        capture_output=True,
        check=False,
    )

    return result.returncode == 0


needs_compose = pytest.mark.skipif(
    not _docker_compose_available(), reason="docker compose CLI not available"
)


@pytest.fixture
def deploy_dir(tmp_path: Path) -> Path:
    """A Control Center directory as on the host, with fixture values."""

    directory = tmp_path / "ai-smm-control-center"
    directory.mkdir()

    shutil.copy(COMPOSE_PATH, directory / COMPOSE_PATH.name)

    (directory / "control-center.compose.env").write_text(
        f"SMM_CC_VERSION={FIXTURE_VERSION}\n"
        f"SMM_CC_SHARED_NETWORK={FIXTURE_NETWORK}\n",
        encoding="utf-8",
    )

    secret = directory / "control-center.env"
    secret.write_text(f"AI_SMM_DATABASE_URL={FIXTURE_DSN}\n", encoding="utf-8")
    secret.chmod(0o600)

    return directory


def _wrapper(
    deploy_dir: Path, *args: str, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", "/tmp"),
        "SMM_CC_DIR": str(deploy_dir),
        **(extra_env or {}),
    }

    return subprocess.run(
        ["bash", str(WRAPPER), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=deploy_dir,
        check=False,
    )


def _render(deploy_dir: Path, *args: str, **kwargs: Any) -> dict[str, Any]:
    result = _wrapper(deploy_dir, *args, "config", "--format", "json", **kwargs)

    assert result.returncode == 0, result.stderr

    return json.loads(result.stdout)


@needs_compose
def test_rendered_default_up_starts_web_and_api_only(deploy_dir: Path) -> None:
    rendered = _render(deploy_dir)

    assert rendered["name"] == PROJECT
    assert set(rendered["services"]) == set(HTTP_SERVICES)


@needs_compose
def test_rendered_ports_and_networks(deploy_dir: Path) -> None:
    rendered = _render(
        deploy_dir, "--profile", "cc-migrate", "--profile", "cc-admin"
    )
    services = rendered["services"]

    assert set(services) == set(ALL_SERVICES)

    published = [
        (name, port)
        for name, service in services.items()
        for port in service.get("ports", [])
    ]

    assert len(published) == 1
    name, port = published[0]

    assert name == "smm-web"
    assert port["host_ip"] == "127.0.0.1"
    assert str(port["published"]) == "8088"
    assert port["target"] == 8080

    networks = rendered["networks"]

    assert networks["shared"]["name"] == FIXTURE_NETWORK
    assert networks["shared"]["external"] is True
    assert networks["cc-internal"]["internal"] is True


@needs_compose
def test_rendered_env_holds_only_the_fixture_database_url(
    deploy_dir: Path,
) -> None:
    services = _render(deploy_dir, "--profile", "cc-migrate")["services"]

    for name in ("smm-api", "migrate"):
        env = services[name]["environment"]

        assert env["AI_SMM_DATABASE_URL"] == FIXTURE_DSN, name
        assert env["AI_SMM_API_MUTATIONS_ENABLED"] == "false", name
        assert env["THREADS_ACCESS_TOKEN"] == "", name

    assert "environment" not in services["smm-web"]


@needs_compose
def test_the_operator_shell_cannot_redirect_the_project(
    deploy_dir: Path,
) -> None:
    """COMPOSE_* and SMM_CC_* exported in the shell are dropped."""

    rendered = _render(
        deploy_dir,
        extra_env={
            "COMPOSE_PROJECT_NAME": "ai-smm",
            "COMPOSE_FILE": str(WORKER_COMPOSE_PATH),
            "COMPOSE_PROFILES": "cc-migrate,cc-admin,tools,cli",
            "SMM_CC_VERSION": "latest",
            "SMM_CC_SHARED_NETWORK": "host",
        },
    )

    assert rendered["name"] == PROJECT
    assert set(rendered["services"]) == set(HTTP_SERVICES)
    assert rendered["services"]["smm-api"]["image"] == (
        f"ai-smm-api:{FIXTURE_VERSION}"
    )
    assert rendered["networks"]["shared"]["name"] == FIXTURE_NETWORK


@needs_compose
def test_bare_compose_would_be_renamed_by_a_dotenv(tmp_path: Path) -> None:
    """Why the wrapper exists: documents Compose's precedence."""

    directory = tmp_path / "worker-dir"
    directory.mkdir()
    shutil.copy(COMPOSE_PATH, directory / COMPOSE_PATH.name)
    (directory / ".env").write_text(
        "COMPOSE_PROJECT_NAME=ai-smm\n"
        f"SMM_CC_VERSION={FIXTURE_VERSION}\n"
        f"SMM_CC_SHARED_NETWORK={FIXTURE_NETWORK}\n",
        encoding="utf-8",
    )
    (directory / "control-center.env").write_text(
        f"AI_SMM_DATABASE_URL={FIXTURE_DSN}\n", encoding="utf-8"
    )

    result = subprocess.run(
        ["docker", "compose", "-f", COMPOSE_PATH.name, "config", "--format", "json"],
        capture_output=True,
        text=True,
        cwd=directory,
        env={"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path)},
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["name"] == "ai-smm"


@needs_compose
def test_env_file_cannot_override_the_literals(tmp_path: Path) -> None:
    """Second line of defence, without the wrapper's key allowlist."""

    directory = tmp_path / "cc"
    directory.mkdir()
    shutil.copy(COMPOSE_PATH, directory / COMPOSE_PATH.name)
    (directory / "interp.env").write_text(
        f"SMM_CC_VERSION={FIXTURE_VERSION}\n"
        f"SMM_CC_SHARED_NETWORK={FIXTURE_NETWORK}\n",
        encoding="utf-8",
    )
    (directory / "control-center.env").write_text(
        f"AI_SMM_DATABASE_URL={FIXTURE_DSN}\n"
        "AI_SMM_API_MUTATIONS_ENABLED=true\n"
        "AI_SMM_API_DOCS_ENABLED=true\n"
        "AI_SMM_DRY_RUN=false\n"
        "THREADS_ACCESS_TOKEN=fixture-not-a-token\n"
        "OPENAI_API_KEY=fixture-not-a-key\n"
        "THREADS_API_BASE_URL=https://graph.threads.net\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            "docker", "compose", "-p", PROJECT, "-f", COMPOSE_PATH.name,
            "--env-file", "interp.env", "config", "--format", "json",
        ],
        capture_output=True,
        text=True,
        cwd=directory,
        env={"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path)},
        check=False,
    )

    assert result.returncode == 0, result.stderr

    env = json.loads(result.stdout)["services"]["smm-api"]["environment"]

    assert env["AI_SMM_API_MUTATIONS_ENABLED"] == "false"
    assert env["AI_SMM_API_DOCS_ENABLED"] == "false"
    assert env["AI_SMM_DRY_RUN"] == "true"
    assert env["THREADS_ACCESS_TOKEN"] == ""
    assert env["OPENAI_API_KEY"] == ""
    assert env["THREADS_API_BASE_URL"] == "http://127.0.0.1:9"


@needs_compose
def test_missing_version_or_network_fails_to_render(tmp_path: Path) -> None:
    directory = tmp_path / "cc"
    directory.mkdir()
    shutil.copy(COMPOSE_PATH, directory / COMPOSE_PATH.name)
    (directory / "control-center.env").write_text(
        f"AI_SMM_DATABASE_URL={FIXTURE_DSN}\n", encoding="utf-8"
    )

    for interp, missing in (
        (f"SMM_CC_SHARED_NETWORK={FIXTURE_NETWORK}\n", "SMM_CC_VERSION"),
        (f"SMM_CC_VERSION={FIXTURE_VERSION}\n", "SMM_CC_SHARED_NETWORK"),
    ):
        (directory / "interp.env").write_text(interp, encoding="utf-8")

        result = subprocess.run(
            [
                "docker", "compose", "-p", PROJECT, "-f", COMPOSE_PATH.name,
                "--env-file", "interp.env", "config",
            ],
            capture_output=True,
            text=True,
            cwd=directory,
            env={"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path)},
            check=False,
        )

        assert result.returncode != 0, missing
        assert missing in result.stderr, result.stderr


# Each case breaks one rule; the wrapper must refuse before Compose runs.
REFUSALS = {
    "dotenv-in-dir": (lambda d: (d / ".env").write_text("X=1\n"), ".env"),
    "worker-compose-in-dir": (
        lambda d: (d / "compose.yaml").write_text("name: ai-smm\n"),
        "compose.yaml",
    ),
    "worker-env-in-dir": (
        lambda d: (d / "ai-smm.env").write_text("X=1\n"),
        "ai-smm.env",
    ),
    "secret-world-readable": (
        lambda d: (d / "control-center.env").chmod(0o644),
        "must be 600",
    ),
    "secret-extra-key": (
        lambda d: (d / "control-center.env").write_text(
            f"AI_SMM_DATABASE_URL={FIXTURE_DSN}\n"
            "THREADS_ACCESS_TOKEN=fixture\n"
        ),
        "must define exactly",
    ),
    "secret-mutations-on": (
        lambda d: (d / "control-center.env").write_text(
            f"AI_SMM_DATABASE_URL={FIXTURE_DSN}\n"
            "AI_SMM_API_MUTATIONS_ENABLED=true\n"
        ),
        "must define exactly",
    ),
    "secret-missing": (
        lambda d: (d / "control-center.env").unlink(),
        "missing",
    ),
    "interp-project-name": (
        lambda d: (d / "control-center.compose.env").write_text(
            f"SMM_CC_VERSION={FIXTURE_VERSION}\n"
            f"SMM_CC_SHARED_NETWORK={FIXTURE_NETWORK}\n"
            "COMPOSE_PROJECT_NAME=ai-smm\n"
        ),
        "must define exactly",
    ),
    "interp-export-syntax": (
        lambda d: (d / "control-center.compose.env").write_text(
            f"export SMM_CC_VERSION={FIXTURE_VERSION}\n"
            f"SMM_CC_SHARED_NETWORK={FIXTURE_NETWORK}\n"
        ),
        "not a KEY=value line",
    ),
    "version-latest": (
        lambda d: (d / "control-center.compose.env").write_text(
            "SMM_CC_VERSION=latest\n"
            f"SMM_CC_SHARED_NETWORK={FIXTURE_NETWORK}\n"
        ),
        "git commit id",
    ),
    "network-empty": (
        lambda d: (d / "control-center.compose.env").write_text(
            f"SMM_CC_VERSION={FIXTURE_VERSION}\n"
            "SMM_CC_SHARED_NETWORK=\n"
        ),
        "SMM_CC_SHARED_NETWORK is empty",
    ),
}


@pytest.mark.parametrize("case", sorted(REFUSALS))
def test_wrapper_refuses(deploy_dir: Path, case: str) -> None:
    if shutil.which("bash") is None:
        pytest.skip("bash not available")

    break_rule, message = REFUSALS[case]
    break_rule(deploy_dir)

    result = _wrapper(deploy_dir, "config")

    assert result.returncode == 2, (result.stdout, result.stderr)
    assert message in result.stderr
    assert result.stdout == ""


@needs_compose
def test_wrapper_refuses_to_start_images_it_cannot_verify(
    deploy_dir: Path,
) -> None:
    """`up` / `run` check the OCI revision label before Compose runs."""

    (deploy_dir / "control-center.compose.env").write_text(
        "SMM_CC_VERSION=fffffffffffffffffff0\n"
        f"SMM_CC_SHARED_NETWORK={FIXTURE_NETWORK}\n",
        encoding="utf-8",
    )

    for args in (("up", "-d"), ("--profile", "cc-migrate", "run", "--rm", "migrate")):
        result = _wrapper(deploy_dir, *args)

        assert result.returncode == 2, args
        assert "is not loaded on this host" in result.stderr, result.stderr


def test_wrapper_is_executable_and_strict() -> None:
    mode = WRAPPER.stat().st_mode

    assert mode & stat.S_IXUSR
    text = WRAPPER.read_text(encoding="utf-8")

    assert text.startswith("#!/usr/bin/env bash\n")
    assert "set -euo pipefail" in text
    assert '--project-name "$PROJECT"' in text
    assert '--env-file "$interp_file"' in text
    assert "exec env -i" in text
