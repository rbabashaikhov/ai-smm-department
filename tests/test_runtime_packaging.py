"""Safety properties of the packaged runtime, read from the files themselves.

These tests do not start Docker. They pin the decisions that make the
local runtime safe to bring up on a workstation and the proxy correct --
no worker, no migration on start, no production .env, only a loopback
port, API prefixes that never fall back to index.html -- so that a later
edit which drops one of them fails here rather than in a deployment.
The end-to-end behaviour is exercised by scripts/runtime_smoke.py against
a running stack.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
COMPOSE_PATH = ROOT / "compose.runtime.yml"
API_DOCKERFILE = ROOT / "Dockerfile"
WEB_DOCKERFILE = ROOT / "web" / "Dockerfile"
NGINX_CONF = ROOT / "web" / "nginx" / "default.conf"
NGINX_PROXY = ROOT / "web" / "nginx" / "smm-proxy.inc"

PYTHON_SERVICES = ("smm-api", "migrate", "cli")


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    return yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def services(compose: dict[str, Any]) -> dict[str, Any]:
    return compose["services"]


def _stages(dockerfile: Path) -> list[tuple[str, list[str]]]:
    """(stage name, instruction lines) in file order."""

    stages: list[tuple[str, list[str]]] = []
    text = dockerfile.read_text(encoding="utf-8").replace("\\\n", " ")

    for line in text.splitlines():
        stripped = line.strip()

        if not stripped or stripped.startswith("#"):
            continue

        match = re.match(r"FROM\s+\S+\s+AS\s+(\S+)", stripped, re.IGNORECASE)

        if match:
            stages.append((match.group(1), [stripped]))
        elif stages:
            stages[-1][1].append(stripped)

    return stages


def _stage(dockerfile: Path, name: str) -> list[str]:
    for stage_name, lines in _stages(dockerfile):
        if stage_name == name:
            return lines

    raise AssertionError(f"{dockerfile} has no stage {name!r}")


def _nginx_locations(conf: str) -> dict[str, str]:
    """location modifier+pattern -> body (single nesting level)."""

    conf = re.sub(r"#[^\n]*", "", conf)

    return {
        match.group(1).strip(): match.group(2)
        for match in re.finditer(r"location\s+([^{]+)\{([^}]*)\}", conf)
    }


# -- compose -------------------------------------------------------------


def test_compose_has_no_worker(services: dict[str, Any]) -> None:
    assert set(services) == {"postgres", "smm-api", "smm-web", "migrate", "cli"}

    for name, service in services.items():
        rendered = yaml.safe_dump(service)

        assert "ai-smm-worker" not in rendered, name
        assert "worker" not in name


def test_compose_is_its_own_project(compose: dict[str, Any]) -> None:
    """Never the production project name used by compose.yaml."""

    assert compose["name"] == "ai-smm-runtime-local"


def test_compose_reads_no_env_file(services: dict[str, Any]) -> None:
    for name, service in services.items():
        assert "env_file" not in service, name


def test_only_the_web_origin_is_published_on_loopback(
    services: dict[str, Any],
) -> None:
    for name, service in services.items():
        if name == "smm-web":
            continue

        assert "ports" not in service, f"{name} must not publish a host port"

    ports = services["smm-web"]["ports"]

    assert ports
    for port in ports:
        assert str(port).startswith("127.0.0.1:"), port


def test_web_port_is_configurable(services: dict[str, Any]) -> None:
    assert "${SMM_RUNTIME_WEB_PORT:-" in services["smm-web"]["ports"][0]


def test_runtime_targets_the_disposable_database_only(
    services: dict[str, Any],
) -> None:
    """A literal URL: no ${...} for a .env file to redirect elsewhere."""

    for name in PYTHON_SERVICES:
        url = services[name]["environment"]["AI_SMM_DATABASE_URL"]

        assert "${" not in url, name
        assert "@postgres:5432/ai_smm_runtime_local" in url, name


def test_dry_run_is_forced_on(services: dict[str, Any]) -> None:
    for name in PYTHON_SERVICES:
        assert services[name]["environment"]["AI_SMM_DRY_RUN"] == "true", name


def test_runtime_env_carries_no_credentials(services: dict[str, Any]) -> None:
    for name in PYTHON_SERVICES:
        env = services[name]["environment"]

        assert env.get("OPENAI_API_KEY", "") == ""
        assert env.get("THREADS_ACCESS_TOKEN", "") == ""


def test_cookie_and_docs_defaults_for_local_http(
    services: dict[str, Any],
) -> None:
    env = services["smm-api"]["environment"]

    assert env["AI_SMM_API_COOKIE_SECURE"] == "${SMM_RUNTIME_COOKIE_SECURE:-false}"
    assert env["AI_SMM_API_DOCS_ENABLED"] == (
        "${SMM_RUNTIME_API_DOCS_ENABLED:-false}"
    )


def test_tools_are_never_started_by_up(services: dict[str, Any]) -> None:
    for name in ("migrate", "cli"):
        assert services[name]["profiles"] == ["tools"], name
        assert services[name]["restart"] == "no", name

    for name in ("postgres", "smm-api", "smm-web"):
        assert "profiles" not in services[name], name


def test_migrate_is_alembic_and_api_does_not_override_its_entrypoint(
    services: dict[str, Any],
) -> None:
    assert services["migrate"]["entrypoint"] == ["/app/.venv/bin/alembic"]

    api = services["smm-api"]

    assert "entrypoint" not in api
    assert "command" not in api
    assert api["build"]["target"] == "api"


def test_api_and_database_have_no_egress(compose: dict[str, Any]) -> None:
    services = compose["services"]

    assert compose["networks"]["backend"]["internal"] is True

    for name in ("postgres", "smm-api", "migrate", "cli"):
        assert services[name]["networks"] == ["backend"], name

    assert sorted(services["smm-web"]["networks"]) == ["backend", "edge"]


def test_startup_dependencies_are_acyclic(services: dict[str, Any]) -> None:
    graph = {
        name: set((service.get("depends_on") or {}).keys())
        for name, service in services.items()
    }

    visiting: set[str] = set()
    done: set[str] = set()

    def visit(node: str) -> None:
        assert node not in visiting, f"dependency cycle through {node}"

        if node in done:
            return

        visiting.add(node)
        for dependency in graph[node]:
            visit(dependency)
        visiting.discard(node)
        done.add(node)

    for node in graph:
        visit(node)

    assert graph["smm-web"] == {"smm-api"}
    assert graph["smm-api"] == {"postgres"}


def test_app_containers_are_hardened(services: dict[str, Any]) -> None:
    for name in (*PYTHON_SERVICES, "smm-web"):
        service = services[name]

        assert service["read_only"] is True, name
        assert service["cap_drop"] == ["ALL"], name
        assert "no-new-privileges:true" in service["security_opt"], name


# -- API image -----------------------------------------------------------


def test_worker_remains_the_default_build_target() -> None:
    """A plain `docker build` still produces the worker image."""

    name, lines = _stages(API_DOCKERFILE)[-1]

    assert name == "runtime"
    entrypoints = [line for line in lines if line.startswith("ENTRYPOINT")]

    assert any("ai-smm-worker" in line for line in entrypoints)


def test_api_target_runs_uvicorn_only() -> None:
    lines = _stage(API_DOCKERFILE, "api")
    entrypoint = " ".join(
        line for line in lines if line.startswith(("ENTRYPOINT", "CMD"))
    )

    assert "uvicorn" in entrypoint
    assert "ai_smm.api.app:create_app" in entrypoint
    assert "--factory" in entrypoint

    for forbidden in ("alembic", "ai-smm-worker", "upgrade"):
        assert forbidden not in entrypoint, forbidden

    assert lines[0].split()[1] == "runtime-base"


def test_api_and_worker_share_one_python_stack() -> None:
    runtime_base = _stage(API_DOCKERFILE, "runtime-base")

    assert any("/app/.venv" in line for line in runtime_base)
    assert _stage(API_DOCKERFILE, "runtime")[0].split()[1] == "runtime-base"


# -- web image -----------------------------------------------------------


def test_web_image_final_stage_is_nginx_without_node() -> None:
    name, lines = _stages(WEB_DOCKERFILE)[-1]
    base = lines[0].split()[1]

    assert name == "runtime"
    assert "nginx" in base
    assert "node" not in base


def test_web_build_is_same_origin() -> None:
    builder = _stage(WEB_DOCKERFILE, "builder")

    assert "ENV VITE_API_BASE_URL=" in builder
    assert any(line.startswith("RUN") and "npm ci" in line for line in builder)


def test_web_build_context_excludes_local_env_files() -> None:
    ignored = (ROOT / "web" / ".dockerignore").read_text(encoding="utf-8")

    for pattern in (".env", ".env.*", "node_modules/"):
        assert pattern in ignored.splitlines(), pattern


# -- proxy ---------------------------------------------------------------


def test_api_prefixes_are_proxied_not_served_as_spa() -> None:
    locations = _nginx_locations(NGINX_CONF.read_text(encoding="utf-8"))
    api = locations["~ ^/(?:api|health)(?:/|$)"]

    assert "smm-proxy.inc" in api
    assert "try_files" not in api
    assert "index.html" not in api


def test_api_location_pattern_matches_exactly_the_api_prefixes() -> None:
    pattern = re.compile(r"^/(?:api|health)(?:/|$)")

    for path in ("/api", "/api/", "/api/v1/not-existing", "/health", "/health/live"):
        assert pattern.search(path), path

    for path in ("/apiary", "/healthy", "/projects/demo/content", "/", "/assets/x.js"):
        assert not pattern.search(path), path


def test_spa_fallback_is_only_on_the_root_location() -> None:
    locations = _nginx_locations(NGINX_CONF.read_text(encoding="utf-8"))

    assert "try_files $uri /index.html;" in locations["/"]

    for key, body in locations.items():
        if key != "/":
            assert "/index.html" not in body, key


def test_missing_assets_are_404() -> None:
    locations = _nginx_locations(NGINX_CONF.read_text(encoding="utf-8"))

    assert "try_files $uri =404;" in locations["/assets/"]


def test_docs_paths_are_left_to_the_api() -> None:
    locations = _nginx_locations(NGINX_CONF.read_text(encoding="utf-8"))

    assert "smm-proxy.inc" in locations[r"~ ^/(?:docs|redoc|openapi\.json)(?:/|$)"]


def test_proxy_passes_the_uri_unchanged_and_resolves_per_request() -> None:
    conf = NGINX_CONF.read_text(encoding="utf-8")
    proxy = NGINX_PROXY.read_text(encoding="utf-8")

    assert "resolver 127.0.0.11" in conf
    assert "set $smm_api http://smm-api:8000;" in conf
    # A variable with no URI part: the request line is forwarded as sent.
    assert re.search(r"^proxy_pass \$smm_api;$", proxy, re.MULTILINE)


def test_no_directory_listing_and_no_version_banner() -> None:
    conf = NGINX_CONF.read_text(encoding="utf-8")

    assert "autoindex off;" in conf
    assert "autoindex on" not in conf
    assert "server_tokens off;" in conf


@pytest.mark.parametrize(
    "path", [COMPOSE_PATH, NGINX_CONF, NGINX_PROXY, WEB_DOCKERFILE]
)
def test_packaging_files_hold_no_secret_looking_values(path: Path) -> None:
    text = path.read_text(encoding="utf-8")

    for pattern in (
        r"sk-[A-Za-z0-9]{10,}",
        r"THAA[A-Za-z0-9]{10,}",
        r"BEGIN [A-Z ]*PRIVATE KEY",
    ):
        assert not re.search(pattern, text), pattern
