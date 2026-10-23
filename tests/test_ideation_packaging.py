"""compose.ideation.yml (SMM-035B3B1): the packaging of the topic ideation
worker. Pins what it must have -- and above all what it must not. Nothing
here starts it.
"""
from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
COMPOSE_PATH = ROOT / "compose.ideation.yml"
DEPLOY = ROOT / "deploy" / "control-center"
ENV_EXAMPLE = DEPLOY / "ideation.env.example"
COMPOSE_ENV_EXAMPLE = DEPLOY / "ideation.compose.env.example"


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    return yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def service(compose: dict[str, Any]) -> dict[str, Any]:
    return compose["services"]["smm-ideation"]


def _keys(path: Path) -> list[str]:
    return [
        line.split("=", 1)[0]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]


def test_it_is_a_project_of_its_own_with_one_service(compose: dict[str, Any]) -> None:
    assert compose["name"] == "ai-smm-ideation"
    assert list(compose["services"]) == ["smm-ideation"]
    assert compose["networks"]["shared"]["external"] is True


def test_it_runs_the_ideation_entry_point_from_a_prebuilt_image(
    service: dict[str, Any],
) -> None:
    assert service["entrypoint"] == ["/app/.venv/bin/ai-smm-ideation-worker"]
    assert service["healthcheck"]["test"] == [
        "CMD", "/app/.venv/bin/ai-smm-ideation-worker", "--check",
    ]
    assert "command" not in service and "build" not in service
    assert service["pull_policy"] == "never"
    assert service["image"].startswith("ai-smm:${SMM_IDEATION_VERSION:?")

    scripts = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["scripts"]
    assert scripts["ai-smm-ideation-worker"] == "ai_smm.ideation_worker:main"


def test_it_is_a_single_bounded_hand_started_instance(service: dict[str, Any]) -> None:
    env = service["environment"]
    assert service["restart"] == "no"
    assert service["container_name"] == "ai-smm-ideation-worker"
    for key in ("ports", "depends_on", "profiles", "deploy", "scale"):
        assert key not in service, key
    assert service["mem_limit"] == "256m" == service["memswap_limit"]
    assert int(env["AI_SMM_IDEATION_LEASE_SECONDS"]) <= 3600


def test_it_holds_no_credential_mount_or_privilege(service: dict[str, Any]) -> None:
    env = service["environment"]
    assert "volumes" not in service  # no knowledge, media, keys or socket
    assert service["read_only"] is True
    assert service["user"] == "10001:10001"
    assert service["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in service["security_opt"]
    assert env["AI_SMM_DRY_RUN"] == "true"
    assert env["LANGFUSE_TRACING_ENABLED"] == "false"
    for secret in (
        "THREADS_ACCESS_TOKEN", "STORAGE_SSH_PASSWORD", "STORAGE_SSH_KEY_PATH",
        "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY",
    ):
        assert env[secret] == "", secret
    assert not any("CREDENTIAL" in key for key in env)
    assert service["env_file"] == ["ideation.env"]


def test_the_env_examples_name_exactly_their_keys() -> None:
    assert _keys(ENV_EXAMPLE) == ["AI_SMM_DATABASE_URL", "OPENAI_API_KEY"]
    assert _keys(COMPOSE_ENV_EXAMPLE) == [
        "SMM_IDEATION_VERSION", "SMM_IDEATION_SHARED_NETWORK",
    ]
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    assert "postgresql+psycopg://ai_smm_ideation:CHANGE_ME@" in text
    assert "\nOPENAI_API_KEY=\n" in text  # empty: no model can be called


def test_the_publish_and_generation_packaging_does_not_mention_it() -> None:
    for name in ("compose.yaml", "compose.runtime.yml", "compose.generation.yml"):
        assert "ideation" not in (ROOT / name).read_text(encoding="utf-8"), name
