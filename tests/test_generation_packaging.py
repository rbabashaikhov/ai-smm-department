"""compose.generation.yml: the production packaging of the generation worker
(SMM-Generation-A1). Pins what it must have -- and above all what it must not.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
COMPOSE_PATH = ROOT / "compose.generation.yml"
ENV_EXAMPLE = ROOT / "deploy" / "control-center" / "generation.env.example"
COMPOSE_ENV_EXAMPLE = (
    ROOT / "deploy" / "control-center" / "generation.compose.env.example"
)
WRAPPER = ROOT / "deploy" / "control-center" / "gen-compose.sh"
DOC = ROOT / "docs" / "generation-worker-deployment.md"


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    return yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def service(compose: dict[str, Any]) -> dict[str, Any]:
    return compose["services"]["smm-generation"]


def _keys(path: Path) -> list[str]:
    return [
        line.split("=", 1)[0]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]


def test_it_is_a_project_of_its_own_with_one_service(compose: dict[str, Any]) -> None:
    assert compose["name"] == "ai-smm-generation"
    assert compose["name"] not in {"ai-smm", "ai-smm-control-center"}
    assert list(compose["services"]) == ["smm-generation"]


def test_it_runs_the_generation_entry_point_from_a_prebuilt_image(
    service: dict[str, Any],
) -> None:
    assert service["entrypoint"] == ["/app/.venv/bin/ai-smm-generation-worker"]
    assert "command" not in service
    assert "build" not in service
    assert service["pull_policy"] == "never"
    assert service["image"].startswith("ai-smm:${SMM_GEN_VERSION:?")


def test_it_is_started_and_stopped_by_hand_only(service: dict[str, Any]) -> None:
    assert service["restart"] == "no"
    assert "profiles" not in service  # its own project: `up` starts only it
    assert "ports" not in service
    assert "depends_on" not in service


def test_it_is_a_single_bounded_instance(service: dict[str, Any]) -> None:
    env = service["environment"]

    assert service["container_name"] == "ai-smm-generation-worker"
    assert "deploy" not in service and "scale" not in service
    assert service["mem_limit"] == "512m" == service["memswap_limit"]
    assert float(service["cpus"]) == 0.5
    assert service["pids_limit"] == 64
    assert int(env["AI_SMM_GENERATION_LEASE_SECONDS"]) <= 3600
    assert int(env["AI_SMM_GENERATION_MAX_REVISIONS"]) == 3
    # retries are the job's own max_attempts (1..10), not a service setting
    assert "AI_SMM_GENERATION_MAX_ATTEMPTS" not in env


def test_it_is_hardened(service: dict[str, Any]) -> None:
    assert service["read_only"] is True
    assert service["user"] == "10001:10001"
    assert service["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in service["security_opt"]
    assert service["tmpfs"] == ["/tmp:size=64m,mode=1777"]
    assert service["labels"]["traefik.enable"] == "false"


def test_env_file_is_exactly_the_database_login_and_the_model_key(
    service: dict[str, Any],
) -> None:
    assert service["env_file"] == ["generation.env"]
    assert _keys(ENV_EXAMPLE) == ["AI_SMM_DATABASE_URL", "OPENAI_API_KEY"]
    text = ENV_EXAMPLE.read_text(encoding="utf-8")

    assert "ai_smm_generation:" in text
    assert re.search(r"^OPENAI_API_KEY=$", text, re.MULTILINE)  # empty by default


def test_no_publishing_storage_or_tracing_credential_can_reach_it(
    service: dict[str, Any],
) -> None:
    env = service["environment"]

    for name in (
        "THREADS_ACCESS_TOKEN",
        "STORAGE_SSH_PASSWORD",
        "STORAGE_SSH_KEY_PATH",
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
    ):
        assert env[name] == "", name  # literals outrank env_file

    assert env["THREADS_API_BASE_URL"] == "http://127.0.0.1:9"
    assert "AI_SMM_DATABASE_URL" not in env  # only from generation.env
    assert "OPENAI_API_KEY" not in env
    assert not any(name.startswith("AI_SMM_CREDENTIALS_") for name in env)
    assert env["AI_SMM_DRY_RUN"] == "true"


def test_only_the_knowledge_base_is_mounted_read_only(service: dict[str, Any]) -> None:
    assert service["volumes"] == [
        "${SMM_GEN_KNOWLEDGE_PATH:-/srv/ai-smm/knowledge}:/srv/ai-smm/knowledge:ro"
    ]
    assert service["environment"]["AI_SMM_KNOWLEDGE_ROOT"] == "/srv/ai-smm/knowledge"
    rendered = yaml.safe_dump(service)

    for forbidden in ("docker.sock", "credential-keys", "/srv/ai-smm/media", "media"):
        assert forbidden not in rendered, forbidden


def test_the_shared_network_is_external_and_never_created(
    compose: dict[str, Any], service: dict[str, Any]
) -> None:
    assert service["networks"] == ["shared"]
    assert compose["networks"]["shared"]["external"] is True
    assert list(compose["networks"]) == ["shared"]


def test_it_has_a_readiness_and_liveness_probe(service: dict[str, Any]) -> None:
    assert service["healthcheck"]["test"] == [
        "CMD",
        "/app/.venv/bin/ai-smm-generation-worker",
        "--check",
    ]
    assert int(service["stop_grace_period"].rstrip("s")) >= 120


def test_compose_interpolation_example_has_exactly_its_keys() -> None:
    assert _keys(COMPOSE_ENV_EXAMPLE) == [
        "SMM_GEN_VERSION",
        "SMM_GEN_SHARED_NETWORK",
        "SMM_GEN_KNOWLEDGE_PATH",
    ]


def test_production_projects_do_not_include_it() -> None:
    for name in ("compose.yaml", "compose.control-center.yml"):
        text = (ROOT / name).read_text(encoding="utf-8")

        assert "ai-smm-generation-worker" not in text, name


def test_the_worker_dockerfile_target_carries_the_entry_point() -> None:
    import tomllib

    scripts = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]["scripts"]

    assert scripts["ai-smm-generation-worker"] == "ai_smm.generation_worker:main"


def test_the_runbook_states_it_is_not_deployed_and_names_the_rollback() -> None:
    text = DOC.read_text(encoding="utf-8")

    assert "NOT deployed" in text
    assert "gen-compose.sh down" in text
    assert "ai_smm_generation.revoke.sql" in text
    assert 'restart: "no"' in text


@pytest.mark.parametrize(
    "path",
    [COMPOSE_PATH, ENV_EXAMPLE, COMPOSE_ENV_EXAMPLE, DOC],
)
def test_no_secret_looking_values(path: Path) -> None:
    text = path.read_text(encoding="utf-8")

    for pattern in (
        r"sk-[A-Za-z0-9]{10,}",
        r"THAA[A-Za-z0-9]{10,}",
        r"BEGIN [A-Z ]*PRIVATE KEY",
    ):
        assert not re.search(pattern, text), pattern


@pytest.mark.skipif(shutil.which("docker") is None, reason="no docker CLI")
def test_it_renders_and_a_pasted_secret_cannot_override_a_literal(
    tmp_path: Path,
) -> None:
    probe = subprocess.run(
        ["docker", "compose", "version"], capture_output=True, text=True, check=False
    )
    if probe.returncode != 0:
        pytest.skip("no docker compose")

    (tmp_path / "compose.generation.yml").write_text(
        COMPOSE_PATH.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (tmp_path / "generation.env").write_text(
        "AI_SMM_DATABASE_URL=postgresql+psycopg://ai_smm_generation:x@pg:5432/ai_smm\n"
        "OPENAI_API_KEY=\nTHREADS_ACCESS_TOKEN=leaked\nAI_SMM_DRY_RUN=false\n",
        encoding="utf-8",
    )
    (tmp_path / "generation.compose.env").write_text(
        "SMM_GEN_VERSION=test\nSMM_GEN_SHARED_NETWORK=some-net\n", encoding="utf-8"
    )
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            "generation.compose.env",
            "-f",
            "compose.generation.yml",
            "config",
            "--format",
            "json",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    import json

    rendered = json.loads(result.stdout)
    env = rendered["services"]["smm-generation"]["environment"]

    assert rendered["name"] == "ai-smm-generation"
    assert env["THREADS_ACCESS_TOKEN"] == ""
    assert env["AI_SMM_DRY_RUN"] == "true"
    assert rendered["services"]["smm-generation"]["image"] == "ai-smm:test"


# -- the wrapper: gen-compose.sh --------------------------------------------

REAL_KEY = "sk-" + "A1b2C3d4E5f6G7h8I9j0K1"
DSN = "postgresql+psycopg://ai_smm_generation:pw%2Bsecret@pg:5432/ai_smm"
GOOD_STATUS = (
    "id               : 3d6bd0f8-7451-49fe-8b66-3316d68f1e4d\n"
    "project          : ai-catalog-consultant\n"
    "status           : queued\n"
    "attempt_count    : 0\n"
    "max_attempts     : 2\n"
    'params           : {"series_size": 1, "publishing_strategy": '
    '"standalone_series", "topic_hint": null}\n'
    "open_jobs        : 1\n"
)


def _deploy(tmp_path: Path, secret: str | None = None, interp: str | None = None):
    """A deployment directory with a stub `docker` that records its argv."""

    d = tmp_path / "gen"
    bin_ = tmp_path / "bin"
    d.mkdir(parents=True)
    bin_.mkdir(parents=True)
    for src in (WRAPPER, ROOT / "deploy" / "control-center" / "cc-common.sh"):
        shutil.copy(src, d / src.name)
    shutil.copy(COMPOSE_PATH, d / COMPOSE_PATH.name)
    (d / "gen-compose.sh").chmod(0o755)
    (d / "generation.compose.env").write_text(
        interp
        or "SMM_GEN_VERSION=9ff2260\nSMM_GEN_SHARED_NETWORK=shared-net\n"
        "SMM_GEN_KNOWLEDGE_PATH=/srv/ai-smm/knowledge\n",
        encoding="utf-8",
    )
    (d / "generation.env").write_text(
        secret or f"AI_SMM_DATABASE_URL={DSN}\nOPENAI_API_KEY={REAL_KEY}\n",
        encoding="utf-8",
    )
    (d / "generation.env").chmod(0o600)
    log = tmp_path / "docker.log"
    stub = bin_ / "docker"
    stub.write_text(f'#!/bin/sh\necho "$@" >> {log}\nexit 0\n', encoding="utf-8")
    stub.chmod(0o755)

    return d, bin_, log


def _run(d: Path, bin_: Path, *args: str, stdin: str = "", env: dict | None = None):
    run_env = {"PATH": f"{bin_}:/usr/bin:/bin", "HOME": str(d)}
    run_env.update(env or {})

    return subprocess.run(
        ["bash", str(d / "gen-compose.sh"), *args],
        input=stdin,
        capture_output=True,
        text=True,
        cwd=d,
        env=run_env,
        check=False,
    )


def _refused(result: subprocess.CompletedProcess, log: Path) -> None:
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert not log.exists(), "Docker was called"
    out = result.stdout + result.stderr
    assert REAL_KEY not in out and "pw%2Bsecret" not in out and "leaked" not in out


def test_wrapper_accepts_the_valid_files_and_builds_one_fixed_command(
    tmp_path: Path,
) -> None:
    d, bin_, log = _deploy(tmp_path)
    result = _run(d, bin_, "config")

    assert result.returncode == 0, result.stderr
    argv = log.read_text(encoding="utf-8").split()
    assert argv[:3] == ["compose", "--project-name", "ai-smm-generation"]
    assert "--env-file" in argv and argv[-2:] == ["config", "--quiet"]
    assert REAL_KEY not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "extra",
    [
        "OPENAI_BASE_URL=http://evil.example",
        "OPENAI_ORG_ID=org-x",
        "OPENAI_PROJECT=proj-x",
        "THREADS_ACCESS_TOKEN=leaked",
        "AI_SMM_DRY_RUN=false",
        "SOMETHING_UNKNOWN=1",
        "OPENAI_API_KEY=" + REAL_KEY,  # duplicate
        "lowercase=1",
        "export OPENAI_BASE_URL=x",
        "OPENAI_BASE_URL = x",
    ],
)
def test_wrapper_refuses_any_extra_duplicate_or_malformed_variable(
    tmp_path: Path, extra: str
) -> None:
    d, bin_, log = _deploy(
        tmp_path,
        secret=f"AI_SMM_DATABASE_URL={DSN}\nOPENAI_API_KEY={REAL_KEY}\n{extra}\n",
    )
    for verb in ("config", "up", "down", "ps", "check"):
        _refused(_run(d, bin_, verb, stdin=GOOD_STATUS), log)


@pytest.mark.parametrize(
    "secret",
    [
        f"AI_SMM_DATABASE_URL={DSN}\n",  # key missing
        f"OPENAI_API_KEY={REAL_KEY}\n",  # url missing
        f"AI_SMM_DATABASE_URL={DSN}\nOPENAI_API_KEY='{REAL_KEY}'\n",  # quotes
        f'AI_SMM_DATABASE_URL={DSN}\nOPENAI_API_KEY="{REAL_KEY}\n',  # multi-line
        f"AI_SMM_DATABASE_URL={DSN}\nOPENAI_API_KEY=$HOME{REAL_KEY}\n",
        f"AI_SMM_DATABASE_URL={DSN}\nOPENAI_API_KEY={REAL_KEY} x\n",
        "AI_SMM_DATABASE_URL=postgresql+psycopg://ai_smm:pw@pg:5432/ai_smm\n"
        f"OPENAI_API_KEY={REAL_KEY}\n",  # the owner, not ai_smm_generation
        f"AI_SMM_DATABASE_URL={DSN}?host=evil\nOPENAI_API_KEY={REAL_KEY}\n",
        f"AI_SMM_DATABASE_URL={DSN.replace('/ai_smm', '/other')}\n"
        f"OPENAI_API_KEY={REAL_KEY}\n",
    ],
)
def test_wrapper_refuses_malformed_secret_files(tmp_path: Path, secret: str) -> None:
    d, bin_, log = _deploy(tmp_path, secret=secret)

    _refused(_run(d, bin_, "config"), log)


def test_wrapper_refuses_a_world_readable_secret_file(tmp_path: Path) -> None:
    d, bin_, log = _deploy(tmp_path)
    (d / "generation.env").chmod(0o644)

    _refused(_run(d, bin_, "config"), log)


def test_wrapper_refuses_foreign_files_symlinks_and_other_daemons(
    tmp_path: Path,
) -> None:
    for foreign in (".env", "compose.override.yml", "ai-smm.env"):
        d, bin_, log = _deploy(tmp_path / foreign.replace(".", "_"))
        (d / foreign).write_text("X=1\n", encoding="utf-8")
        _refused(_run(d, bin_, "config"), log)

    d, bin_, log = _deploy(tmp_path / "link")
    real = d / "real.env"
    real.write_text((d / "generation.env").read_text(encoding="utf-8"))
    real.chmod(0o600)
    (d / "generation.env").unlink()
    os.symlink(real, d / "generation.env")
    _refused(_run(d, bin_, "config"), log)

    d, bin_, log = _deploy(tmp_path / "ctx")
    _refused(_run(d, bin_, "config", env={"DOCKER_CONTEXT": "x"}), log)
    _refused(_run(d, bin_, "config", env={"DOCKER_HOST": "tcp://evil:2375"}), log)


@pytest.mark.parametrize(
    "interp",
    [
        "SMM_GEN_VERSION=latest\nSMM_GEN_SHARED_NETWORK=n\nSMM_GEN_KNOWLEDGE_PATH=/k\n",
        "SMM_GEN_VERSION=1\nSMM_GEN_SHARED_NETWORK=n\n",
        "SMM_GEN_VERSION=1\nSMM_GEN_SHARED_NETWORK=n\nSMM_GEN_KNOWLEDGE_PATH=rel\n",
        "SMM_GEN_VERSION=1\nSMM_GEN_SHARED_NETWORK=n\nSMM_GEN_KNOWLEDGE_PATH=/k\n"
        "COMPOSE_PROJECT_NAME=ai-smm\n",
    ],
)
def test_wrapper_refuses_a_bad_interpolation_file(tmp_path: Path, interp: str) -> None:
    d, bin_, log = _deploy(tmp_path, interp=interp)

    _refused(_run(d, bin_, "config"), log)


def test_wrapper_accepts_only_its_verbs(tmp_path: Path) -> None:
    d, bin_, log = _deploy(tmp_path)
    for args in (
        ["run"],
        ["exec", "x"],
        ["up", "-d"],
        ["down", "-v"],
        ["config", "-p", "x"],
        ["logs", "--follow"],
    ):
        _refused(_run(d, bin_, *args, stdin=GOOD_STATUS), log)


def test_up_with_an_empty_key_is_refused_before_docker(tmp_path: Path) -> None:
    d, bin_, log = _deploy(
        tmp_path,
        secret=f"AI_SMM_DATABASE_URL={DSN}\nOPENAI_API_KEY=\n",
    )
    result = _run(d, bin_, "up", stdin=GOOD_STATUS)

    _refused(result, log)
    assert "OPENAI_API_KEY" in result.stderr


def test_key_check_is_presence_only_and_does_not_claim_validity(
    tmp_path: Path,
) -> None:
    # Any non-empty value passes: OpenAI's acceptance is not checkable offline.
    d, bin_, log = _deploy(
        tmp_path,
        secret=f"AI_SMM_DATABASE_URL={DSN}\nOPENAI_API_KEY=not-a-real-looking-key\n",
    )
    result = _run(d, bin_, "up", stdin=GOOD_STATUS)

    assert result.returncode == 0, result.stderr
    assert "not-a-real-looking-key" not in result.stdout + result.stderr
    text = WRAPPER.read_text(encoding="utf-8")
    assert "sk-" not in text and "is a real key" not in text


@pytest.mark.parametrize(
    "status",
    [
        "",  # nothing piped in
        GOOD_STATUS.replace("queued", "claimed"),
        GOOD_STATUS.replace("attempt_count    : 0", "attempt_count    : 1"),
        GOOD_STATUS.replace("max_attempts     : 2", "max_attempts     : 3"),
        GOOD_STATUS.replace("max_attempts     : 2", "max_attempts     : 10"),
        GOOD_STATUS.replace("max_attempts     : 2\n", ""),  # field absent
        GOOD_STATUS.replace('"series_size": 1', '"series_size": 3'),
        GOOD_STATUS.replace('"series_size": 1', '"series_size": 11'),
        GOOD_STATUS.replace("standalone_series", "other"),
        GOOD_STATUS.replace("open_jobs        : 1", "open_jobs        : 2"),
        GOOD_STATUS.replace("open_jobs        : 1\n", ""),
        # Codex F3 scenario 1: nested values must not stand in for top-level ones.
        GOOD_STATUS.replace(
            '{"series_size": 1, "publishing_strategy": "standalone_series", '
            '"topic_hint": null}',
            '{"series_size": 3, "publishing_strategy": "other", "x": '
            '{"series_size": 1, "publishing_strategy": "standalone_series"}}',
        ),
        # Codex F3 scenario 2: a repeated key must not override.
        GOOD_STATUS.replace('"series_size": 1,', '"series_size": 3, "series_size": 1,'),
        GOOD_STATUS.replace('"series_size": 1,', '"series_size": 1, "series_size": 3,'),
        GOOD_STATUS.replace('"series_size": 1', '"series_size": true'),
        GOOD_STATUS.replace('"series_size": 1', '"series_size": "1"'),
        GOOD_STATUS.replace('"series_size": 1', '"series_size": 1.0'),
        GOOD_STATUS.replace("params           : {", "params           : [1, {").replace(
            "null}", "null}]"
        ),
        GOOD_STATUS.replace("params           : {", "params           : {{"),
        GOOD_STATUS + "status           : queued\n",  # repeated line
        GOOD_STATUS.replace("max_attempts     : 2", "max_attempts     : two"),
    ],
)
def test_up_requires_one_fresh_bounded_job(tmp_path: Path, status: str) -> None:
    d, bin_, log = _deploy(tmp_path)

    _refused(_run(d, bin_, "up", stdin=status), log)


@pytest.mark.parametrize("attempts", ["1", "2"])
def test_up_starts_the_worker_for_a_valid_key_and_job(
    tmp_path: Path, attempts: str
) -> None:
    d, bin_, log = _deploy(tmp_path)
    result = _run(d, bin_, "up", stdin=GOOD_STATUS.replace(": 2\n", f": {attempts}\n"))

    assert result.returncode == 0, result.stderr
    argv = log.read_text(encoding="utf-8").split()
    assert argv[-1] == "smm-generation" and "up" in argv and "--no-build" in argv
    assert REAL_KEY not in result.stdout + result.stderr


def test_the_runbook_covers_the_wrapper_the_empty_key_and_the_real_retry_ceiling() -> (
    None
):
    text = DOC.read_text(encoding="utf-8")

    assert "gen-compose.sh" in text
    assert "does not give" in text and "back" in text
    assert "1–10" in text and "`max_attempts` **1 or 2**" in text
    assert "at most two" not in text.lower() and "2 attempts" not in text


def test_docs_state_attestation_and_promise_no_spending_cap() -> None:
    text = DOC.read_text(encoding="utf-8")
    wrapper = WRAPPER.read_text(encoding="utf-8")
    env_example = ENV_EXAMPLE.read_text(encoding="utf-8")

    assert "operator attestation, not a database guarantee" in text
    assert "ATTESTATION" in wrapper
    assert "no spend cap" in text.lower() and "NO spend cap" in env_example
    for promised in ("hard spend", "spend limit", "budget cap"):
        assert promised not in text.lower() + env_example.lower(), promised


# -- SMM-035A: `down` stays one plain command; the logs are kept by hand ----
#
# A first design saved the logs inside `down` (stop, save, supervise a
# subprocess under a deadline). It was abandoned as more machinery than a
# one-shot worker needs: Docker already keeps the logs of a container until
# it is removed, and the runbook says what to run before removing it.


def _calls_of(log: Path) -> list[list[str]]:
    return [line.split() for line in log.read_text(encoding="utf-8").splitlines()]


def test_down_is_one_plain_compose_command_in_its_own_project(tmp_path: Path) -> None:
    d, bin_, log = _deploy(tmp_path)

    result = _run(d, bin_, "down")

    assert result.returncode == 0, (result.stdout, result.stderr)
    (argv,) = _calls_of(log)  # exactly one Docker call, nothing around it
    assert argv[:3] == ["compose", "--project-name", "ai-smm-generation"]
    assert argv[-1] == "down" and argv.count("down") == 1
    assert not (d / "logs").exists()  # nothing is saved, nothing is created
    for other in ("stop", "inspect", "logs", "ps", "rm", "kill"):
        assert other not in argv, other


def test_down_cannot_reach_the_live_publisher_or_external_resources(
    tmp_path: Path,
) -> None:
    d, bin_, log = _deploy(tmp_path)

    _run(d, bin_, "down")

    (argv,) = _calls_of(log)
    text = " ".join(argv)
    assert "--project-name ai-smm-generation" in text
    assert text.count("--file") == 1 and "compose.generation.yml" in text
    assert "compose.yaml" not in text and "--project-name ai-smm " not in text + " "
    assert "ai-smm-worker" not in text  # the LIVE publisher's container
    for dangerous in ("-v", "--volumes", "--rmi", "--remove-orphans", "-f "):
        assert dangerous not in argv, dangerous
    # the shared network is external to the project: `down` never removes it
    compose = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    assert compose["networks"]["shared"]["external"] is True


def test_the_exit_status_of_down_is_the_wrappers(tmp_path: Path) -> None:
    d, bin_, log = _deploy(tmp_path)
    (bin_ / "docker").write_text('#!/bin/sh\necho "$@" >> ' + str(log) + "\nexit 7\n")

    assert _run(d, bin_, "down").returncode == 7


def test_the_wrapper_has_no_process_supervision() -> None:
    code = "\n".join(
        line
        for line in WRAPPER.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    ).replace("--wait-timeout", "").replace("--wait", "")  # compose flags

    for machinery in ("setsid", "kill", "trap ", "timeout", "sleep", "wait ", "disown",
                      "nohup", "mktemp", "logs/"):
        assert machinery not in code, machinery
    assert not re.search(r"\)\s*&\s*$|\s&\s*$", code, re.MULTILINE)  # no background job


def test_down_still_takes_no_arguments(tmp_path: Path) -> None:
    d, bin_, log = _deploy(tmp_path)

    for extra in (["-v"], ["--rmi", "all"], ["--volumes"]):
        assert _run(d, bin_, "down", *extra).returncode == 2
    assert not log.exists()


def test_the_verbs_the_manual_procedure_uses_exist_and_are_read_only(
    tmp_path: Path,
) -> None:
    d, bin_, log = _deploy(tmp_path)

    assert _run(d, bin_, "ps").returncode == 0
    assert _run(d, bin_, "logs").returncode == 0

    ps, logs = _calls_of(log)
    assert ps[-2:] == ["ps", "-a"]  # a stopped or crashed worker is listed too
    assert logs[-3:] == ["logs", "--no-color", "smm-generation"]


def test_the_runbook_tells_the_operator_to_keep_the_logs_before_down() -> None:
    text = DOC.read_text(encoding="utf-8")

    assert "Keep the worker's output before you remove it" in text
    assert "does **not** save anything" in text
    assert "./gen-compose.sh logs >" in text and "./gen-compose.sh ps" in text
    assert "umask 077" in text and "before" in text.lower()
    # the order in the procedure: keep first, remove last
    assert text.index("./gen-compose.sh logs >") < text.index(
        "./gen-compose.sh down\n```"
    )
    assert "series_size` is the contract of a job" in text
