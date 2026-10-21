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

from tests.cc_stub import (
    FIXTURE_API_DSN,
    FIXTURE_AUTH_DSN,
    FIXTURE_DSN,
    FIXTURE_NETWORK,
    FIXTURE_PUBLIC_KEY,
    FIXTURE_VERSION,
    docker_socket,
    run_script,
    write_deploy_dir,
)


ROOT = Path(__file__).resolve().parents[1]
COMPOSE_PATH = ROOT / "compose.control-center.yml"
WORKER_COMPOSE_PATH = ROOT / "compose.yaml"
DEPLOY_DIR = ROOT / "deploy" / "control-center"
WRAPPER = DEPLOY_DIR / "cc-compose.sh"
SECRET_EXAMPLE = DEPLOY_DIR / "control-center.env.example"
API_SECRET_EXAMPLE = DEPLOY_DIR / "control-center.api.env.example"
VERIFY_SQL = DEPLOY_DIR / "db" / "ai_smm_cc.verify.sql"
MUTATIONS_COMPOSE_PATH = ROOT / "compose.control-center.mutations.yml"
RW_VERIFY_SQL = DEPLOY_DIR / "db" / "ai_smm_cc_rw.verify.sql"
RW_SECRET_EXAMPLE = DEPLOY_DIR / "control-center.api-rw.env.example"
INTERP_EXAMPLE = DEPLOY_DIR / "control-center.compose.env.example"
TARGET_EXAMPLE = DEPLOY_DIR / "control-center.target.example"
BACKUP = DEPLOY_DIR / "cc-db-backup.sh"
COMMON = DEPLOY_DIR / "cc-common.sh"
ALEMBIC_ENV = ROOT / "alembic" / "env.py"

PROJECT = "ai-smm-control-center"
HTTP_SERVICES = ("smm-api", "smm-auth", "smm-web")
TOOL_SERVICES = ("migrate", "cli")
PYTHON_SERVICES = ("smm-api", "smm-auth", "migrate", "cli")
AUTH_VERIFY_SQL = DEPLOY_DIR / "db" / "ai_smm_auth.verify.sql"
AUTH_SECRET_EXAMPLE = DEPLOY_DIR / "control-center.auth.env.example"
#: smm-auth's one address, in cc-auth (ADR-024C-3A §9.2).
AUTH_SUBNET = "10.231.18.0/29"
AUTH_ADDRESS = "10.231.18.2"
NGINX_CONF = ROOT / "web" / "nginx" / "default.conf"
NGINX_AUTH_SNIPPET = ROOT / "web" / "nginx" / "smm-auth-proxy.inc"
WEB_DOCKERFILE = ROOT / "web" / "Dockerfile"
ALL_SERVICES = (*HTTP_SERVICES, *TOOL_SERVICES)



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

    for name in (*TOOL_SERVICES, "smm-auth"):
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

    assert set(networks) == {"cc-internal", "cc-edge", "cc-auth", "shared"}

    assert networks["cc-internal"]["internal"] is True
    # smm-web <-> smm-auth only; a fixed subnet so smm-auth binds one address.
    assert networks["cc-auth"]["internal"] is True
    assert networks["cc-auth"]["name"] == "ai-smm-cc-auth"
    assert networks["cc-auth"]["ipam"] == {"config": [{"subnet": AUTH_SUBNET}]}
    assert networks["cc-edge"].get("internal", False) is False
    assert networks["cc-edge"].get("external", False) is False

    shared = networks["shared"]

    assert shared["external"] is True
    assert shared["name"].startswith("${SMM_CC_SHARED_NETWORK:?")
    assert "internal" not in shared


def test_network_attachments(services: dict[str, Any]) -> None:
    # The web tier never sees the shared network (database, other projects).
    assert _networks(services["smm-web"]) == {"cc-edge", "cc-internal", "cc-auth"}
    # The API: reached by web on cc-internal, reaches the database on shared.
    assert _networks(services["smm-api"]) == {"cc-internal", "shared"}
    # smm-auth: reached by web on cc-auth, at its fixed address; reaches
    # the database on shared. Never on cc-internal: smm-api and smm-auth
    # cannot reach each other.
    assert services["smm-auth"]["networks"] == {
        "cc-auth": {"ipv4_address": AUTH_ADDRESS},
        "shared": {},
    }

    # One-shot tools: the database only.
    for name in TOOL_SERVICES:
        assert _networks(services[name]) == {"shared"}, name

    attached_to_shared = {
        name for name, service in services.items()
        if "shared" in _networks(service)
    }

    assert attached_to_shared == {"smm-api", "smm-auth", *TOOL_SERVICES}
    assert {
        name for name, service in services.items() if "cc-auth" in _networks(service)
    } == {"smm-auth", "smm-web"}

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

    # Not on smm-auth: browsing with a session goes on while it is down.
    for name in ("smm-api", "smm-auth", *TOOL_SERVICES):
        assert "depends_on" not in services[name], name


# -- configuration and secrets -----------------------------------------------


def test_only_the_dedicated_env_file_is_read(services: dict[str, Any]) -> None:
    # The API connects as its own restricted role (SMM-024C-2); only the
    # one-shot tools that migrate and bootstrap keep the owner URL.
    assert services["smm-api"]["env_file"] == ["control-center.api.env"]
    # smm-auth: the sign-in role ai_smm_auth only (SMM-025D).
    assert services["smm-auth"]["env_file"] == ["control-center.auth.env"]

    for name in TOOL_SERVICES:
        assert services[name]["env_file"] == ["control-center.env"], name

    assert "env_file" not in services["smm-web"]

    text = COMPOSE_PATH.read_text(encoding="utf-8")
    uncommented = re.sub(r"#[^\n]*", "", text)

    assert "ai-smm.env" not in uncommented
    assert ".env\n" not in uncommented.replace(
        "control-center.api.env\n", ""
    ).replace("control-center.auth.env\n", "").replace("control-center.env\n", "")


def test_the_gate_pins_the_files_of_this_release() -> None:
    """cc-gate.sh pins compose and the verify file: keep the pins current.

    Changing either file without updating its pin makes the API role gate
    refuse `up` -- which is the point on a host, and this test says so in
    the repository first.
    """

    import hashlib

    gate = (DEPLOY_DIR / "cc-gate.sh").read_text(encoding="utf-8")

    for name, path in (
        ("CC_PINNED_COMPOSE_SHA256", COMPOSE_PATH),
        ("CC_PINNED_VERIFY_SHA256", VERIFY_SQL),
        ("CC_PINNED_MUTATIONS_COMPOSE_SHA256", MUTATIONS_COMPOSE_PATH),
        ("CC_PINNED_RW_VERIFY_SHA256", RW_VERIFY_SQL),
        ("CC_PINNED_AUTH_VERIFY_SHA256", AUTH_VERIFY_SQL),
    ):
        pinned = re.search(rf"^readonly {name}=([0-9a-f]{{64}})$", gate, re.M)

        assert pinned, name
        assert pinned.group(1) == hashlib.sha256(path.read_bytes()).hexdigest(), (
            f"{name} is stale: update it to the sha256 of {path.name}"
        )


def test_the_mutations_override_changes_only_the_role_and_the_flag() -> None:
    """The mutations override: smm-api's env file and flag, nothing else."""

    text = MUTATIONS_COMPOSE_PATH.read_text(encoding="utf-8")
    # `!override` is a Compose tag; PyYAML only needs to know it exists.
    loader = type("Loader", (yaml.SafeLoader,), {})
    loader.add_constructor("!override", lambda ld, node: ld.construct_sequence(node))
    override = yaml.load(text, Loader=loader)  # noqa: S506 -- SafeLoader subclass

    assert override == {
        "services": {
            "smm-api": {
                "env_file": ["control-center.api-rw.env"],
                "environment": {"AI_SMM_API_MUTATIONS_ENABLED": "true"},
            },
        },
    }
    # Replaces the read-only role's file instead of adding to it.
    assert "env_file: !override" in text


#: The release in production when SMM-025C was prepared (2026-10-14).
DEPLOYED_RELEASE = "0cbd641074ad3bc0501e54f59dac90bd1ccc9006"
GRANTS_IN_GIT = "deploy/control-center/db/ai_smm_cc.grants.sql"


def _write_grants(sql: str) -> set[tuple[str, str, str]]:
    """(privilege, table, column) of every INSERT/UPDATE/DELETE/TRUNCATE
    grant; '*' for a table-level one."""

    sql = re.sub(r"--[^\n]*", "", sql)
    found: set[tuple[str, str, str]] = set()

    for privilege, columns, table in re.findall(
        r"GRANT\s+(INSERT|UPDATE|DELETE|TRUNCATE)\b\s*(?:\(([^)]*)\))?\s*ON\s+(\w+)",
        sql,
    ):
        for column in (columns.split(",") if columns else ["*"]):
            found.add((privilege, table, column.strip()))

    return found


def test_the_read_only_role_gains_no_write_over_the_deployed_release() -> None:
    """SMM-025B/025C add only SELECTs to ai_smm_cc: its writes stay login's."""

    if shutil.which("git") is None or not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")

    deployed = subprocess.run(
        ["git", "show", f"{DEPLOYED_RELEASE}:{GRANTS_IN_GIT}"],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )

    if deployed.returncode != 0:
        pytest.skip(f"{DEPLOYED_RELEASE[:12]} is not in this clone")

    current = (DEPLOY_DIR / "db" / "ai_smm_cc.grants.sql").read_text(encoding="utf-8")

    # Nothing new; G2 (SMM-025D) only takes away: session creation and
    # revocation, the login columns of users and the audit entry are
    # smm-auth's now.
    current_writes = _write_grants(current)

    assert current_writes <= _write_grants(deployed.stdout)
    assert current_writes == {("UPDATE", "user_sessions", "last_seen_at")}


#: The two FU-025C-2 values, interpolated into smm-api only.
API_KEY_ENV = {
    "AI_SMM_CREDENTIALS_PUBLIC_KEY": "${SMM_CC_CREDENTIALS_PUBLIC_KEY:?",
    "AI_SMM_LEGACY_THREADS_PROJECTS": "${SMM_CC_LEGACY_THREADS_PROJECTS?",
}


def test_only_the_api_gets_the_public_key_and_no_service_a_private_one(
    services: dict[str, Any],
) -> None:
    """FU-025C-2: the public key and the legacy allow-list reach smm-api
    and nothing else here; no Control Center file names a private key."""

    for key, prefix in API_KEY_ENV.items():
        assert services["smm-api"]["environment"][key].startswith(prefix), key

        for name, service in services.items():
            if name != "smm-api":
                assert key not in (service.get("environment") or {}), (name, key)

    for path in (COMPOSE_PATH, MUTATIONS_COMPOSE_PATH):
        text = path.read_text(encoding="utf-8")

        assert "PRIVATE_KEY" not in text, path.name
        assert ".pem" not in text, path.name
        assert "/run/ai-smm/credential-keys" not in text, path.name


def test_the_worker_mounts_the_private_key_read_only_and_migrate_does_not() -> None:
    worker = yaml.safe_load(WORKER_COMPOSE_PATH.read_text(encoding="utf-8"))
    key_mount = (
        "${AI_SMM_CREDENTIAL_KEYS_HOST_PATH:?set AI_SMM_CREDENTIAL_KEYS_HOST_PATH"
    )

    for name in ("worker", "cli"):
        mounts = [
            m for m in worker["services"][name]["volumes"]
            if "/run/ai-smm/credential-keys" in m
        ]

        assert len(mounts) == 1, name
        assert mounts[0].startswith(key_mount), name
        assert mounts[0].endswith(":/run/ai-smm/credential-keys:ro"), name
        # The knowledge and media mounts are still there, unchanged.
        assert len(worker["services"][name]["volumes"]) == 3, name

    for mount in worker["services"]["migrate"]["volumes"]:
        assert "credential" not in mount


def test_api_secret_example_names_the_api_role() -> None:
    assert _env_keys(API_SECRET_EXAMPLE) == ["AI_SMM_DATABASE_URL"]

    text = API_SECRET_EXAMPLE.read_text(encoding="utf-8")

    assert (
        "AI_SMM_DATABASE_URL=postgresql+psycopg://ai_smm_cc:CHANGE_ME@"
        "<postgres-container>:5432/ai_smm"
    ) in text


def test_auth_secret_example_names_the_sign_in_role() -> None:
    assert _env_keys(AUTH_SECRET_EXAMPLE) == ["AI_SMM_DATABASE_URL"]

    text = AUTH_SECRET_EXAMPLE.read_text(encoding="utf-8")

    assert (
        "AI_SMM_DATABASE_URL=postgresql+psycopg://ai_smm_auth:CHANGE_ME@"
        "<postgres-container>:5432/ai_smm"
    ) in text


def test_rw_secret_example_names_the_mutation_role() -> None:
    assert _env_keys(RW_SECRET_EXAMPLE) == ["AI_SMM_DATABASE_URL"]

    text = RW_SECRET_EXAMPLE.read_text(encoding="utf-8")

    assert (
        "AI_SMM_DATABASE_URL=postgresql+psycopg://ai_smm_cc_rw:CHANGE_ME@"
        "<postgres-container>:5432/ai_smm"
    ) in text


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

        # Sign-in is smm-auth's: smm-api mounts no login or logout.
        assert env["AI_SMM_API_AUTH_ROUTES"] == "false", name

        # The database URL comes from control-center.env only.
        assert "AI_SMM_DATABASE_URL" not in env, name

        for key, value in env.items():
            if name == "smm-api" and key in API_KEY_ENV:
                continue  # FU-025C-2, validated by the wrapper

            assert "${" not in str(value), (name, key)


def test_interpolation_is_limited_and_required() -> None:
    """Four variables, all required, none with a default. The legacy
    allow-list may be empty (no legacy project) but never unset."""

    text = COMPOSE_PATH.read_text(encoding="utf-8")
    uncommented = re.sub(r"#[^\n]*", "", text)
    references = re.findall(r"\$\{([A-Za-z0-9_]+)([^}]*)\}", uncommented)

    assert {name for name, _ in references} == {
        "SMM_CC_VERSION",
        "SMM_CC_SHARED_NETWORK",
        "SMM_CC_CREDENTIALS_PUBLIC_KEY",
        "SMM_CC_LEGACY_THREADS_PROJECTS",
    }

    for name, modifier in references:
        if name == "SMM_CC_LEGACY_THREADS_PROJECTS":
            assert modifier.startswith("?"), (name, modifier)
        else:
            assert modifier.startswith(":?"), (name, modifier)

    assert "$$" not in uncommented


def test_secret_example_holds_only_the_database_url() -> None:
    assert _env_keys(SECRET_EXAMPLE) == ["AI_SMM_DATABASE_URL"]

    text = SECRET_EXAMPLE.read_text(encoding="utf-8")

    assert ":CHANGE_ME@<postgres-container>:5432/ai_smm" in text


def test_interpolation_example_holds_only_its_four_keys() -> None:
    assert sorted(_env_keys(INTERP_EXAMPLE)) == [
        "SMM_CC_CREDENTIALS_PUBLIC_KEY",
        "SMM_CC_LEGACY_THREADS_PROJECTS",
        "SMM_CC_SHARED_NETWORK",
        "SMM_CC_VERSION",
    ]


def test_target_example_holds_only_its_two_keys() -> None:
    assert sorted(_env_keys(TARGET_EXAMPLE)) == [
        "SMM_CC_DOCKER_ENGINE_ID",
        "SMM_CC_DOCKER_HOST",
    ]
    assert "SMM_CC_DOCKER_HOST=unix:///var/run/docker.sock" in (
        TARGET_EXAMPLE.read_text(encoding="utf-8")
    )


def test_filled_env_files_are_ignored_by_git() -> None:
    if shutil.which("git") is None or not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")

    for path in (
        "deploy/control-center/control-center.env",
        "deploy/control-center/control-center.api.env",
        "deploy/control-center/control-center.api-rw.env",
        "deploy/control-center/control-center.mutations",
        "deploy/control-center/control-center.compose.env",
        "deploy/control-center/control-center.target",
        "control-center.env",
        "control-center.api.env",
        "control-center.api-rw.env",
        "control-center.mutations",
        "control-center.compose.env",
        "control-center.target",
    ):
        result = subprocess.run(
            ["git", "check-ignore", "--quiet", path], cwd=ROOT, check=False
        )

        assert result.returncode == 0, f"{path} would be committable"

    for path in (
        SECRET_EXAMPLE, API_SECRET_EXAMPLE, RW_SECRET_EXAMPLE,
        INTERP_EXAMPLE, TARGET_EXAMPLE, MUTATIONS_COMPOSE_PATH,
    ):
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
    "path",
    [
        COMPOSE_PATH, WRAPPER, BACKUP, COMMON,
        SECRET_EXAMPLE, API_SECRET_EXAMPLE, INTERP_EXAMPLE, TARGET_EXAMPLE,
        RW_SECRET_EXAMPLE, MUTATIONS_COMPOSE_PATH, AUTH_SECRET_EXAMPLE,
        WORKER_COMPOSE_PATH, NGINX_CONF, NGINX_AUTH_SNIPPET,
        *sorted((DEPLOY_DIR / "db").glob("*.sql")),
    ],
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


#: SMM-034A: the one mount of the project -- the knowledge base, read-only,
#: smm-api only, at the worker's own host path and container path.
KNOWLEDGE_ROOT = "/srv/ai-smm/knowledge"
KNOWLEDGE_MOUNT = {
    "type": "bind",
    "source": KNOWLEDGE_ROOT,
    "target": KNOWLEDGE_ROOT,
    "read_only": True,
    "bind": {"create_host_path": False},
}


def test_only_smm_api_mounts_and_only_the_knowledge_base_read_only(
    services: dict[str, Any],
) -> None:
    """No Docker socket, no volume; one literal read-only bind (SMM-034A)."""

    for name, service in services.items():
        if name != "smm-api":
            assert "volumes" not in service, name

    assert services["smm-api"]["volumes"] == [KNOWLEDGE_MOUNT]
    assert (
        services["smm-api"]["environment"]["AI_SMM_KNOWLEDGE_ROOT"]
        == KNOWLEDGE_ROOT
    )
    # Not in the shared app-env: no other service learns the path.
    for name in ("smm-auth", "migrate", "cli"):
        assert "AI_SMM_KNOWLEDGE_ROOT" not in services[name]["environment"], name

    assert "volumes" not in yaml.safe_load(
        COMPOSE_PATH.read_text(encoding="utf-8")
    )
    assert "docker.sock" not in COMPOSE_PATH.read_text(encoding="utf-8")


def test_the_api_reads_the_directory_the_worker_loads() -> None:
    """The same host directory and path as the worker's (read-only) mount."""

    worker = yaml.safe_load(WORKER_COMPOSE_PATH.read_text(encoding="utf-8"))
    mount = worker["x-common"]["volumes"][0]
    source, target, mode = mount.rsplit(":", 2)

    assert source == "${AI_SMM_KNOWLEDGE_HOST_PATH:-" + KNOWLEDGE_ROOT + "}"
    assert (target, mode) == (KNOWLEDGE_ROOT, "ro")


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


def test_auth_runs_the_auth_application_bound_to_its_address_only(
    services: dict[str, Any],
) -> None:
    """ADR-024C-3A §9.2: no listening socket on the shared network; the
    application refuses any other peer as well."""

    auth = services["smm-auth"]

    assert auth["entrypoint"] == [
        "/app/.venv/bin/uvicorn", "ai_smm.api.auth_service:create_auth_app",
        "--factory", "--host", AUTH_ADDRESS, "--port", "8000",
        "--no-access-log", "--no-server-header",
    ]
    assert "command" not in auth
    assert auth["environment"]["AI_SMM_AUTH_ALLOWED_PEERS"] == AUTH_SUBNET
    assert auth["environment"]["PGAPPNAME"] == "ai-smm-control-center-auth"
    assert AUTH_ADDRESS in " ".join(auth["healthcheck"]["test"])
    # Argon2 at 64 MiB, two at a time, plus the interpreter.
    assert auth["mem_limit"] == "320m"


def test_nginx_sends_sign_in_sign_out_and_step_up_to_smm_auth() -> None:
    conf = NGINX_CONF.read_text(encoding="utf-8")
    snippet = NGINX_AUTH_SNIPPET.read_text(encoding="utf-8")

    assert "set $smm_auth http://smm-auth:8000;" in conf

    for location in (
        "location = /api/v1/auth/login {",
        "location = /api/v1/auth/logout {",
        "location ^~ /api/v1/auth/step-up/ {",
    ):
        start = conf.index(location)
        block = conf[start:conf.index("}", start)]

        assert "include /etc/nginx/snippets/smm-auth-proxy.inc;" in block, location

        if "logout" not in location:
            assert "limit_req zone=smm_auth" in block, location

    assert "proxy_pass $smm_auth;" in snippet
    assert "$smm_api" not in snippet
    assert "error_page 502 504 = @auth_unavailable;" in snippet
    assert '"dependency":"auth"' in conf
    # Every other /api path still goes to smm-api, which mounts no login.
    assert "proxy_pass $smm_api;" in (
        ROOT / "web" / "nginx" / "smm-proxy.inc"
    ).read_text(encoding="utf-8")

    dockerfile = WEB_DOCKERFILE.read_text(encoding="utf-8")

    assert (
        "COPY --chown=root:root nginx/smm-auth-proxy.inc "
        "/etc/nginx/snippets/smm-auth-proxy.inc"
    ) in dockerfile


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


# -- rendered configuration ---------------------------------------------------
#
# Through the wrapper's `config` verb with the real Docker Compose CLI.
# `config` does not contact a daemon; the pinned socket only has to exist.


INTERP_FIXTURE = (
    f"SMM_CC_VERSION={FIXTURE_VERSION}\n"
    f"SMM_CC_SHARED_NETWORK={FIXTURE_NETWORK}\n"
    f"SMM_CC_CREDENTIALS_PUBLIC_KEY={FIXTURE_PUBLIC_KEY}\n"
    "SMM_CC_LEGACY_THREADS_PROJECTS=\n"
)


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
def deploy_dir(tmp_path: Path) -> Any:
    with docker_socket() as docker_host:
        yield write_deploy_dir(
            tmp_path / "ai-smm-control-center", docker_host=docker_host
        )


def _render(
    deploy_dir: Path, *args: str, extra_env: dict[str, str] | None = None
) -> dict[str, Any]:
    result = run_script(
        WRAPPER, deploy_dir, "config", "--json", *args, extra_env=extra_env
    )

    assert result.returncode == 0, result.stderr

    return json.loads(result.stdout)


def _compose_direct(
    directory: Path, *args: str, home: Path
) -> subprocess.CompletedProcess[str]:
    """Bypasses the wrapper on purpose: what Compose itself does."""

    return subprocess.run(
        ["docker", "compose", *args],
        capture_output=True,
        text=True,
        cwd=directory,
        env={"PATH": os.environ.get("PATH", ""), "HOME": str(home)},
        check=False,
    )


@needs_compose
def test_rendered_default_up_starts_web_api_and_auth_only(deploy_dir: Path) -> None:
    rendered = _render(deploy_dir)

    assert rendered["name"] == PROJECT
    assert set(rendered["services"]) == set(HTTP_SERVICES)


@needs_compose
def test_rendered_ports_and_networks(deploy_dir: Path) -> None:
    rendered = _render(deploy_dir, "--with-tools")
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
    assert networks["cc-auth"]["internal"] is True
    assert networks["cc-auth"]["ipam"]["config"] == [{"subnet": AUTH_SUBNET}]
    assert services["smm-auth"]["networks"]["cc-auth"]["ipv4_address"] == AUTH_ADDRESS

    # FU-025C-2: the wrapper-validated values, on smm-api only.
    api_env = services["smm-api"]["environment"]

    assert api_env["AI_SMM_CREDENTIALS_PUBLIC_KEY"] == FIXTURE_PUBLIC_KEY
    assert api_env["AI_SMM_LEGACY_THREADS_PROJECTS"] == ""

    for name in ("smm-auth", "migrate", "cli"):
        env = services[name]["environment"]

        assert "AI_SMM_CREDENTIALS_PUBLIC_KEY" not in env, name


@needs_compose
def test_rendered_knowledge_mount_is_read_only_and_smm_api_only(
    deploy_dir: Path,
) -> None:
    services = _render(deploy_dir, "--with-tools")["services"]

    for name, service in services.items():
        if name != "smm-api":
            assert not service.get("volumes"), name

    assert services["smm-api"]["volumes"] == [KNOWLEDGE_MOUNT]


@needs_compose
def test_wrapper_config_never_prints_the_database_url(deploy_dir: Path) -> None:
    passwords = [
        dsn.split(":")[2].split("@")[0]
        for dsn in (FIXTURE_DSN, FIXTURE_API_DSN, FIXTURE_AUTH_DSN)
    ]

    for args in ((), ("--json",), ("--json", "--with-tools"), ("--services",)):
        result = run_script(WRAPPER, deploy_dir, "config", *args)

        assert result.returncode == 0, (args, result.stderr)

        for password in passwords:
            assert password not in result.stdout, args
            assert password not in result.stderr, args


@needs_compose
def test_rendered_env_holds_only_the_fixture_database_url(
    deploy_dir: Path, tmp_path: Path
) -> None:
    """Resolving env_file is Compose's job; checked without the wrapper,
    which never prints the resolved URL."""

    result = _compose_direct(
        deploy_dir,
        "-p", PROJECT, "-f", COMPOSE_PATH.name,
        "--env-file", "control-center.compose.env",
        "--profile", "cc-migrate", "config", "--format", "json",
        home=tmp_path,
    )

    assert result.returncode == 0, result.stderr

    services = json.loads(result.stdout)["services"]

    # The API as ai_smm_cc; smm-auth as ai_smm_auth; migrate as the owner,
    # the worker's URL.
    for name, dsn in (
        ("smm-api", FIXTURE_API_DSN),
        ("smm-auth", FIXTURE_AUTH_DSN),
        ("migrate", FIXTURE_DSN),
    ):
        env = services[name]["environment"]

        assert env["AI_SMM_DATABASE_URL"] == dsn, name
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
            "COMPOSE_ENV_FILES": "/root/ai-smm/.env",
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
        "COMPOSE_PROJECT_NAME=ai-smm\n" + INTERP_FIXTURE,
        encoding="utf-8",
    )
    (directory / "control-center.env").write_text(
        f"AI_SMM_DATABASE_URL={FIXTURE_DSN}\n", encoding="utf-8"
    )
    (directory / "control-center.api.env").write_text(
        f"AI_SMM_DATABASE_URL={FIXTURE_API_DSN}\n", encoding="utf-8"
    )
    (directory / "control-center.auth.env").write_text(
        f"AI_SMM_DATABASE_URL={FIXTURE_AUTH_DSN}\n", encoding="utf-8"
    )

    result = _compose_direct(
        directory, "-f", COMPOSE_PATH.name, "config", "--format", "json",
        home=tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["name"] == "ai-smm"


@needs_compose
def test_env_file_cannot_override_the_literals(tmp_path: Path) -> None:
    """Second line of defence, without the wrapper's key allowlist."""

    directory = tmp_path / "cc"
    directory.mkdir()
    shutil.copy(COMPOSE_PATH, directory / COMPOSE_PATH.name)
    (directory / "interp.env").write_text(INTERP_FIXTURE, encoding="utf-8")
    overrides = (
        "AI_SMM_API_MUTATIONS_ENABLED=true\n"
        "AI_SMM_API_DOCS_ENABLED=true\n"
        "AI_SMM_DRY_RUN=false\n"
        "THREADS_ACCESS_TOKEN=fixture-not-a-token\n"
        "OPENAI_API_KEY=fixture-not-a-key\n"
        "THREADS_API_BASE_URL=https://graph.threads.net\n"
        "AI_SMM_API_AUTH_ROUTES=true\n"
        "AI_SMM_AUTH_ALLOWED_PEERS=0.0.0.0/0\n"
        "AI_SMM_CREDENTIALS_PUBLIC_KEY=fixture-not-the-key\n"
    )

    for name, dsn in (
        ("control-center.env", FIXTURE_DSN),
        ("control-center.api.env", FIXTURE_API_DSN),
        ("control-center.auth.env", FIXTURE_AUTH_DSN),
    ):
        (directory / name).write_text(
            f"AI_SMM_DATABASE_URL={dsn}\n" + overrides, encoding="utf-8"
        )

    result = _compose_direct(
        directory,
        "-p", PROJECT, "-f", COMPOSE_PATH.name,
        "--env-file", "interp.env", "--profile", "cc-migrate",
        "config", "--format", "json",
        home=tmp_path,
    )

    assert result.returncode == 0, result.stderr

    rendered = json.loads(result.stdout)["services"]

    auth_env = rendered["smm-auth"]["environment"]

    assert auth_env["AI_SMM_AUTH_ALLOWED_PEERS"] == AUTH_SUBNET
    assert (
        rendered["smm-api"]["environment"]["AI_SMM_CREDENTIALS_PUBLIC_KEY"]
        == FIXTURE_PUBLIC_KEY
    )

    for service in ("smm-api", "smm-auth", "migrate"):
        env = rendered[service]["environment"]

        assert env["AI_SMM_API_MUTATIONS_ENABLED"] == "false", service
        assert env["AI_SMM_API_DOCS_ENABLED"] == "false", service
        assert env["AI_SMM_DRY_RUN"] == "true", service
        assert env["THREADS_ACCESS_TOKEN"] == "", service
        assert env["OPENAI_API_KEY"] == "", service
        assert env["THREADS_API_BASE_URL"] == "http://127.0.0.1:9", service
        assert env["AI_SMM_API_AUTH_ROUTES"] == "false", service


@needs_compose
def test_missing_version_or_network_fails_to_render(tmp_path: Path) -> None:
    directory = tmp_path / "cc"
    directory.mkdir()
    shutil.copy(COMPOSE_PATH, directory / COMPOSE_PATH.name)
    (directory / "control-center.env").write_text(
        f"AI_SMM_DATABASE_URL={FIXTURE_DSN}\n", encoding="utf-8"
    )

    for missing in (
        "SMM_CC_VERSION",
        "SMM_CC_SHARED_NETWORK",
        "SMM_CC_CREDENTIALS_PUBLIC_KEY",
        "SMM_CC_LEGACY_THREADS_PROJECTS",
    ):
        interp = "".join(
            line + "\n"
            for line in INTERP_FIXTURE.splitlines()
            if not line.startswith(f"{missing}=")
        )
        (directory / "interp.env").write_text(interp, encoding="utf-8")

        result = _compose_direct(
            directory,
            "-p", PROJECT, "-f", COMPOSE_PATH.name,
            "--env-file", "interp.env", "config",
            home=tmp_path,
        )

        assert result.returncode != 0, missing
        assert missing in result.stderr, result.stderr


def test_scripts_are_executable_and_strict() -> None:
    for script in (WRAPPER, BACKUP):
        mode = script.stat().st_mode
        text = script.read_text(encoding="utf-8")

        assert mode & stat.S_IXUSR, script
        assert text.startswith("#!/usr/bin/env bash\n"), script
        assert "set -euo pipefail" in text, script
        assert 'source "$here/cc-common.sh"' in text, script

    # Sourced, never executed.
    assert not COMMON.stat().st_mode & stat.S_IXUSR

    wrapper = WRAPPER.read_text(encoding="utf-8")

    assert '--project-name "$CC_PROJECT"' in wrapper
    assert '--env-file "$interp_file"' in wrapper
    assert "exec env -i" in wrapper
    # The operator's arguments never reach Compose unvetted.
    assert '"$@"' not in wrapper.split("# -- run")[1]
