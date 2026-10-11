"""deploy/control-center/cc-compose.sh: the allowlist, the pinned Docker
daemon and the image revision check.

Run against a fake `docker` (tests/cc_stub.py) that records argv and
environment, so each test asserts the exact Compose command line a verb
produces -- or that nothing reached Docker at all. The documented
invocations in the deployment and rollback runbooks are extracted and run
through the same stub, so the documents cannot drift from what the
wrapper accepts.
"""
from __future__ import annotations

import hashlib
import re
import shlex
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.cc_stub import (
    BACKUP,
    FIXTURE_DSN,
    FIXTURE_ENGINE,
    FIXTURE_NETWORK,
    FIXTURE_VERSION,
    ROOT,
    STUB_DUMP,
    WRAPPER,
    DockerStub,
    compose_prefix,
    docker_socket,
    run_script,
    write_deploy_dir,
)


pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash not available"
)

DEPLOY_DOC = ROOT / "docs" / "control-center-deployment.md"
ROLLBACK_DOC = ROOT / "docs" / "control-center-rollback.md"


@pytest.fixture
def socket_host() -> Iterator[str]:
    with docker_socket() as host:
        yield host


@pytest.fixture
def deploy_dir(tmp_path: Path, socket_host: str) -> Path:
    return write_deploy_dir(
        tmp_path / "ai-smm-control-center", docker_host=socket_host
    )


@pytest.fixture
def stub(tmp_path: Path) -> DockerStub:
    return DockerStub(tmp_path / "stub-bin")


def _run(deploy_dir: Path, stub: DockerStub, *args: str, **kwargs):
    return run_script(WRAPPER, deploy_dir, *args, stub=stub, **kwargs)


def _accepted(deploy_dir: Path, stub: DockerStub, *args: str) -> list[str]:
    """The Compose argv after the fixed prefix, for an accepted command."""

    stub.reset_calls()
    result = _run(deploy_dir, stub, *args)

    assert result.returncode == 0, (args, result.stderr)

    calls = stub.compose_calls()

    assert len(calls) == 1, calls

    prefix = compose_prefix(deploy_dir)

    assert calls[0][: len(prefix)] == prefix

    return calls[0][len(prefix):]


def _refused(
    deploy_dir: Path, stub: DockerStub, *args: str, message: str = "",
    extra_env: dict[str, str] | None = None,
) -> list[dict]:
    """Refused with exit 2 and no Compose call; returns the docker calls."""

    stub.reset_calls()
    result = _run(deploy_dir, stub, *args, extra_env=extra_env)

    assert result.returncode == 2, (args, result.stdout, result.stderr)
    assert message in result.stderr, result.stderr
    assert stub.compose_calls() == [], args

    return stub.calls()


# -- accepted: the exact command line of each verb ----------------------------

ACCEPTED = [
    (("config", "--quiet"), ["config", "--quiet"]),
    (("config",), ["config", "--no-env-resolution"]),
    (("config", "--json"), ["config", "--format", "json", "--no-env-resolution"]),
    (
        ("config", "--json", "--with-tools"),
        ["--profile", "cc-migrate", "--profile", "cc-admin",
         "config", "--format", "json", "--no-env-resolution"],
    ),
    (("config", "--services"), ["config", "--services"]),
    (("config", "--profiles"), ["config", "--profiles"]),
    (
        ("up",),
        ["up", "--detach", "--wait", "--wait-timeout", "180", "--no-build",
         "--pull", "never", "smm-api", "smm-web"],
    ),
    (("down",), ["down"]),
    (("down", "--remove-orphans"), ["down", "--remove-orphans"]),
    (("ps",), ["ps"]),
    (("ps", "-a"), ["ps", "--all"]),
    (("ps", "-q", "smm-api"), ["ps", "--quiet", "smm-api"]),
    (("ps", "--services"), ["ps", "--services"]),
    (
        ("logs", "--since", "15m", "smm-api", "smm-web"),
        ["logs", "--no-color", "--since", "15m", "smm-api", "smm-web"],
    ),
    (
        ("logs", "--tail", "200", "--timestamps"),
        ["logs", "--no-color", "--tail", "200", "--timestamps"],
    ),
    (("restart", "smm-api"), ["restart", "smm-api"]),
    (("restart", "smm-api", "smm-web"), ["restart", "smm-api", "smm-web"]),
    (
        ("migrate", "current"),
        ["--profile", "cc-migrate", "run", "--rm", "--no-deps", "-T",
         "migrate", "current"],
    ),
    (
        ("migrate", "heads"),
        ["--profile", "cc-migrate", "run", "--rm", "--no-deps", "-T",
         "migrate", "heads"],
    ),
    (
        ("migrate", "upgrade", "3d4ac1986f07"),
        ["--profile", "cc-migrate", "run", "--rm", "--no-deps", "-T",
         "migrate", "upgrade", "3d4ac1986f07"],
    ),
    (
        ("migrate", "downgrade", "79731eadacd7"),
        ["--profile", "cc-migrate", "run", "--rm", "--no-deps", "-T",
         "migrate", "downgrade", "79731eadacd7"],
    ),
    (
        ("create-owner", "--email", "owner@example.test",
         "--display-name", "--project other", "--project", "demo"),
        # Not a terminal here, so -T; --opt=value keeps a value that looks
        # like an option a value.
        ["--profile", "cc-admin", "run", "--rm", "--no-deps", "-T", "cli",
         "create-owner", "--email=owner@example.test",
         "--display-name=--project other", "--project=demo"],
    ),
]


@pytest.mark.parametrize(
    ("args", "expected"), ACCEPTED, ids=[" ".join(a) for a, _ in ACCEPTED]
)
def test_accepted_verbs_build_exactly_this_command(
    deploy_dir: Path, stub: DockerStub, args: tuple[str, ...], expected: list[str]
) -> None:
    assert _accepted(deploy_dir, stub, *args) == expected


def test_compose_gets_the_pinned_daemon_and_nothing_else(
    deploy_dir: Path, stub: DockerStub, socket_host: str
) -> None:
    _run(
        deploy_dir, stub, "ps",
        extra_env={
            "COMPOSE_PROJECT_NAME": "ai-smm",
            "COMPOSE_FILE": "/root/ai-smm/compose.yaml",
            "COMPOSE_PROFILES": "tools,cli",
            "SMM_CC_VERSION": "0" * 40,
            "DOCKER_CONFIG": "/nonexistent",
            "DOCKER_DEFAULT_PLATFORM": "linux/arm64",
        },
    )

    calls = stub.calls()

    assert calls, "nothing reached docker"

    for call in calls:
        env = call["env"]

        assert env["DOCKER_HOST"] == socket_host, call["argv"]

        for key in env:
            assert not key.startswith(("COMPOSE_", "SMM_CC_")), key
            assert key not in (
                "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_DEFAULT_PLATFORM"
            ), key


def test_daemon_is_verified_before_compose_runs(
    deploy_dir: Path, stub: DockerStub
) -> None:
    _accepted(deploy_dir, stub, "up")

    argvs = [c["argv"] for c in stub.calls()]

    assert argvs[0] == ["info", "--format", "{{.ID}}"]
    assert argvs[-1][0] == "compose"
    assert [a[:2] for a in argvs].count(["image", "inspect"]) == 2


def test_config_does_not_need_the_daemon(
    deploy_dir: Path, stub: DockerStub
) -> None:
    stub.update(info_fails=True)

    _accepted(deploy_dir, stub, "config", "--quiet")


# -- refused: attempts to get around the allowlist ------------------------------

BYPASSES = {
    # global Compose options, in any position
    "project-name": ("-p", "ai-smm", "down"),
    "project-name-long": ("--project-name", "ai-smm", "ps"),
    "project-name-after-verb": ("down", "-p", "ai-smm"),
    "file": ("-f", "/root/ai-smm/compose.yaml", "config"),
    "file-after-verb": ("config", "--file", "/root/ai-smm/compose.yaml"),
    "project-directory": ("--project-directory", "/root/ai-smm", "ps"),
    "project-directory-after-verb": ("ps", "--project-directory", "/root/ai-smm"),
    "env-file": ("--env-file", "/root/ai-smm/.env", "config"),
    "env-file-after-verb": ("config", "--env-file", "/root/ai-smm/.env"),
    "profile": ("--profile", "tools", "up"),
    "profile-after-verb": ("up", "--profile", "cli"),
    "context": ("--context", "other", "ps"),
    "host": ("-H", "tcp://10.0.0.1:2375", "ps"),
    "compose-passthrough": ("compose", "down"),
    # raw Compose verbs that are not offered
    "run": ("run", "--rm", "cli", "queue"),
    "run-migrate": ("run", "--rm", "migrate", "upgrade", "head"),
    "exec": ("exec", "smm-api", "sh"),
    "start": ("start",),
    "stop": ("stop",),
    "kill": ("kill",),
    "rm": ("rm", "-f"),
    "create": ("create",),
    "pull": ("pull",),
    "build": ("build",),
    "push": ("push",),
    "cp": ("cp", "smm-api:/etc/passwd", "."),
    "scale": ("scale", "smm-api=0"),
    "watch": ("watch",),
    "attach": ("attach", "smm-api"),
    "port": ("port", "smm-web", "8080"),
    "top": ("top",),
    "unknown": ("frobnicate",),
    # up: no services, profiles or flags of the caller's
    "up-with-service": ("up", "smm-api"),
    "up-detach": ("up", "-d"),
    "up-worker": ("up", "worker"),
    "up-scale": ("up", "--scale", "smm-api=0"),
    "up-force-recreate": ("up", "--force-recreate"),
    "up-build": ("up", "--build"),
    # down: never volumes, images or another project's services
    "down-volumes": ("down", "-v"),
    "down-volumes-long": ("down", "--volumes"),
    "down-rmi": ("down", "--rmi", "all"),
    "down-service": ("down", "worker"),
    "down-timeout": ("down", "-t", "0"),
    # ps / logs / restart: this project's services only
    "ps-worker": ("ps", "ai-smm-worker"),
    "ps-format": ("ps", "--format", "{{.Name}}"),
    "logs-worker": ("logs", "worker"),
    "logs-migrate": ("logs", "migrate"),
    "logs-follow": ("logs", "-f"),
    "logs-since-injection": ("logs", "--since", "15m;id"),
    "logs-since-date": ("logs", "--since", "2026-01-01T00:00:00"),
    "logs-since-missing": ("logs", "--since"),
    "logs-tail-all": ("logs", "--tail", "all"),
    "restart-nothing": ("restart",),
    "restart-worker": ("restart", "ai-smm-worker"),
    "restart-migrate": ("restart", "migrate"),
    "restart-flag": ("restart", "-t", "0", "smm-api"),
    # migrate: alembic subcommands and explicit revisions only
    "migrate-nothing": ("migrate",),
    "migrate-head": ("migrate", "upgrade", "head"),
    "migrate-heads-target": ("migrate", "upgrade", "heads"),
    "migrate-relative-up": ("migrate", "upgrade", "+1"),
    "migrate-base": ("migrate", "downgrade", "base"),
    "migrate-relative-down": ("migrate", "downgrade", "-1"),
    "migrate-range": ("migrate", "upgrade", "79731eadacd7:3d4ac1986f07"),
    "migrate-branch": ("migrate", "upgrade", "ai_smm@3d4ac1986f07"),
    "migrate-short-rev": ("migrate", "upgrade", "3d4ac198"),
    "migrate-sql": ("migrate", "upgrade", "3d4ac1986f07", "--sql"),
    "migrate-sql-first": ("migrate", "upgrade", "--sql", "3d4ac1986f07"),
    "migrate-stamp": ("migrate", "stamp", "3d4ac1986f07"),
    "migrate-history": ("migrate", "history"),
    "migrate-current-extra": ("migrate", "current", "-v"),
    "migrate-injection": ("migrate", "upgrade", "3d4ac1986f07;id"),
    "migrate-entrypoint": ("migrate", "--entrypoint", "sh"),
    "migrate-env": ("migrate", "-e", "PGOPTIONS=", "upgrade", "3d4ac1986f07"),
    # create-owner: the three options, validated, each once
    "owner-missing-email": ("create-owner", "--display-name", "O", "--project", "demo"),
    "owner-twice": (
        "create-owner", "--email", "a@b.test", "--email", "c@d.test",
        "--display-name", "O", "--project", "demo",
    ),
    "owner-password-arg": (
        "create-owner", "--email", "a@b.test", "--display-name", "O",
        "--project", "demo", "--password", "x",
    ),
    "owner-bad-email": (
        "create-owner", "--email", "a@b.test --project other",
        "--display-name", "O", "--project", "demo",
    ),
    "owner-bad-project": (
        "create-owner", "--email", "a@b.test", "--display-name", "O",
        "--project", "../demo",
    ),
    "owner-newline-name": (
        "create-owner", "--email", "a@b.test", "--display-name", "O\nX",
        "--project", "demo",
    ),
    "owner-entrypoint": (
        "create-owner", "--entrypoint", "sh", "--email", "a@b.test",
        "--display-name", "O", "--project", "demo",
    ),
    "owner-env": (
        "create-owner", "-e", "AI_SMM_DATABASE_URL=x", "--email", "a@b.test",
        "--display-name", "O", "--project", "demo",
    ),
    "owner-volume": (
        "create-owner", "-v", "/:/host", "--email", "a@b.test",
        "--display-name", "O", "--project", "demo",
    ),
    "owner-user": (
        "create-owner", "--user", "0", "--email", "a@b.test",
        "--display-name", "O", "--project", "demo",
    ),
    "owner-dangling": ("create-owner", "--email"),
    # config: no options of Compose's own
    "config-output": ("config", "--output", "/root/ai-smm/compose.yaml"),
    "config-hash": ("config", "--hash", "*"),
    "config-two-modes": ("config", "--json", "--quiet"),
    "config-no-interpolate": ("config", "--no-interpolate"),
}


@pytest.mark.parametrize("case", sorted(BYPASSES))
def test_bypass_attempts_are_refused_before_docker(
    deploy_dir: Path, stub: DockerStub, case: str
) -> None:
    calls = _refused(deploy_dir, stub, *BYPASSES[case])

    # Refused while parsing: not even the daemon was asked anything.
    assert calls == [], calls


def test_no_arguments_prints_usage_and_refuses(
    deploy_dir: Path, stub: DockerStub
) -> None:
    result = _run(deploy_dir, stub)

    assert result.returncode == 2
    assert "usage: cc-compose.sh" in result.stderr
    assert stub.calls() == []


# -- refused: the deployment directory and its files ----------------------------

FILE_REFUSALS = {
    "dotenv-in-dir": (lambda d: (d / ".env").write_text("X=1\n"), ".env"),
    "dotenv-symlink-in-dir": (
        lambda d: (d / ".env").symlink_to("/nonexistent/.env"), ".env",
    ),
    "worker-compose-in-dir": (
        lambda d: (d / "compose.yaml").write_text("name: ai-smm\n"),
        "compose.yaml",
    ),
    "worker-env-in-dir": (
        lambda d: (d / "ai-smm.env").write_text("X=1\n"), "ai-smm.env",
    ),
    "secret-world-readable": (
        lambda d: (d / "control-center.env").chmod(0o644), "must be 600",
    ),
    "secret-group-readable": (
        lambda d: (d / "control-center.env").chmod(0o640), "must be 600",
    ),
    "secret-extra-key": (
        lambda d: (d / "control-center.env").write_text(
            f"AI_SMM_DATABASE_URL={FIXTURE_DSN}\nTHREADS_ACCESS_TOKEN=fixture\n"
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
        lambda d: (d / "control-center.env").unlink(), "missing",
    ),
    "secret-symlink": (
        lambda d: _replace_with_symlink(d / "control-center.env"), "symlink",
    ),
    "compose-symlink": (
        lambda d: _replace_with_symlink(d / "compose.control-center.yml"),
        "symlink",
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
        lambda d: _set_version(d, "latest"), "full 40-hex commit",
    ),
    "version-short": (
        lambda d: _set_version(d, FIXTURE_VERSION[:12]), "full 40-hex commit",
    ),
    "version-uppercase": (
        lambda d: _set_version(d, FIXTURE_VERSION.upper()), "full 40-hex commit",
    ),
    "version-too-long": (
        lambda d: _set_version(d, FIXTURE_VERSION + "0"), "full 40-hex commit",
    ),
    "network-empty": (
        lambda d: (d / "control-center.compose.env").write_text(
            f"SMM_CC_VERSION={FIXTURE_VERSION}\nSMM_CC_SHARED_NETWORK=\n"
        ),
        "SMM_CC_SHARED_NETWORK is empty",
    ),
    "network-weird": (
        lambda d: (d / "control-center.compose.env").write_text(
            f"SMM_CC_VERSION={FIXTURE_VERSION}\nSMM_CC_SHARED_NETWORK=a b\n"
        ),
        "not a network name",
    ),
    "target-missing": (
        lambda d: (d / "control-center.target").unlink(), "missing",
    ),
    "target-extra-key": (
        lambda d: _append(d / "control-center.target", "DOCKER_CONTEXT=x\n"),
        "must define exactly",
    ),
    "target-tcp": (
        lambda d: _set_target(d, "tcp://127.0.0.1:2375"), "must be unix:///",
    ),
    "target-ssh": (
        lambda d: _set_target(d, "ssh://root@example.test"), "must be unix:///",
    ),
    "target-relative": (
        lambda d: _set_target(d, "unix://docker.sock"), "must be unix:///",
    ),
    "target-dotdot": (
        lambda d: _set_target(d, "unix:///tmp/../var/run/docker.sock"),
        "must be unix:///",
    ),
    "target-not-a-socket": (
        lambda d: _set_target(d, f"unix://{d}/control-center.env"),
        "no Docker socket",
    ),
    "target-bad-engine-id": (
        lambda d: _set_target(d, None, engine="x y"), "not a Docker engine id",
    ),
}


def _replace_with_symlink(path: Path) -> None:
    real = path.with_name(path.name + ".real")
    path.rename(real)
    path.symlink_to(real)


def _append(path: Path, text: str) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)


def _set_version(directory: Path, version: str) -> None:
    (directory / "control-center.compose.env").write_text(
        f"SMM_CC_VERSION={version}\nSMM_CC_SHARED_NETWORK={FIXTURE_NETWORK}\n"
    )


def _set_target(
    directory: Path, host: str | None, engine: str = FIXTURE_ENGINE
) -> None:
    target = directory / "control-center.target"

    if host is None:
        host = target.read_text().split("SMM_CC_DOCKER_HOST=")[1].split("\n")[0]

    target.write_text(
        f"SMM_CC_DOCKER_HOST={host}\nSMM_CC_DOCKER_ENGINE_ID={engine}\n"
    )


@pytest.mark.parametrize("case", sorted(FILE_REFUSALS))
@pytest.mark.parametrize("verb", [("config", "--quiet"), ("up",)])
def test_deployment_directory_rules(
    deploy_dir: Path, stub: DockerStub, case: str, verb: tuple[str, ...]
) -> None:
    break_rule, message = FILE_REFUSALS[case]
    break_rule(deploy_dir)

    calls = _refused(deploy_dir, stub, *verb, message=message)

    assert calls == [], calls


# -- the Docker daemon ------------------------------------------------------------


@pytest.mark.parametrize(
    "env",
    [
        {"DOCKER_CONTEXT": "desktop-linux"},
        {"DOCKER_CONTEXT": "default"},
        {"DOCKER_HOST": "tcp://10.0.0.1:2375"},
        {"DOCKER_HOST": "unix:///var/run/other.sock"},
    ],
    ids=["context", "context-default", "host-tcp", "host-other-socket"],
)
def test_a_shell_naming_another_daemon_is_refused(
    deploy_dir: Path, stub: DockerStub, env: dict[str, str]
) -> None:
    calls = _refused(deploy_dir, stub, "ps", extra_env=env)

    assert calls == []


def test_a_shell_naming_the_pinned_daemon_is_accepted(
    deploy_dir: Path, stub: DockerStub, socket_host: str
) -> None:
    stub.reset_calls()
    result = _run(deploy_dir, stub, "ps", extra_env={"DOCKER_HOST": socket_host})

    assert result.returncode == 0, result.stderr


def test_another_engine_at_the_pinned_socket_is_refused(
    deploy_dir: Path, stub: DockerStub
) -> None:
    stub.update(engine_id="some-other-engine-42")

    for verb in (("ps",), ("down",), ("up",), ("migrate", "current")):
        calls = _refused(deploy_dir, stub, *verb, message="not the pinned")

        assert [c["argv"][0] for c in calls] == ["info"], verb


def test_an_unreachable_daemon_is_refused(
    deploy_dir: Path, stub: DockerStub
) -> None:
    stub.update(info_fails=True)

    _refused(deploy_dir, stub, "down", message="does not answer")


# -- image revision -------------------------------------------------------------

IMAGE_VERBS = [
    ("up",),
    ("restart", "smm-api"),
    ("migrate", "current"),
    ("migrate", "upgrade", "3d4ac1986f07"),
    ("create-owner", "--email", "a@b.test", "--display-name", "O", "--project", "demo"),
]


def _set_image(stub: DockerStub, name: str, **fields: str) -> None:
    images = stub.state["images"]
    key = f"{name}:{FIXTURE_VERSION}"
    images[key] = {**images[key], **fields}
    stub.save()


@pytest.mark.parametrize(
    ("image", "fields", "message"),
    [
        ("ai-smm-api", {"revision": FIXTURE_VERSION[:12]}, "expected exactly"),
        ("ai-smm-web", {"revision": FIXTURE_VERSION[:7]}, "expected exactly"),
        ("ai-smm-api", {"revision": FIXTURE_VERSION + "0"}, "expected exactly"),
        ("ai-smm-api", {"revision": FIXTURE_VERSION.upper()}, "expected exactly"),
        ("ai-smm-web", {"revision": "f" * 40}, "expected exactly"),
        ("ai-smm-api", {"revision": "<no value>"}, "expected exactly"),
        ("ai-smm-web", {"arch": "arm64"}, "the daemon is amd64"),
    ],
    ids=["api-12-prefix", "web-7-prefix", "api-longer", "api-uppercase",
         "web-other-commit", "api-no-label", "web-wrong-arch"],
)
@pytest.mark.parametrize("verb", IMAGE_VERBS, ids=lambda v: " ".join(v[:2]))
def test_images_must_carry_exactly_the_pinned_revision(
    deploy_dir: Path, stub: DockerStub, image: str, fields: dict[str, str],
    message: str, verb: tuple[str, ...],
) -> None:
    _set_image(stub, image, **fields)

    _refused(deploy_dir, stub, *verb, message=message)


@pytest.mark.parametrize("verb", IMAGE_VERBS, ids=lambda v: " ".join(v[:2]))
def test_a_missing_image_is_refused(
    deploy_dir: Path, stub: DockerStub, verb: tuple[str, ...]
) -> None:
    del stub.state["images"][f"ai-smm-web:{FIXTURE_VERSION}"]
    stub.save()

    _refused(deploy_dir, stub, *verb, message="is not loaded on this daemon")


def test_verbs_that_create_no_container_do_not_need_the_images(
    deploy_dir: Path, stub: DockerStub
) -> None:
    stub.update(images={})

    for args in (("ps",), ("down",), ("logs", "--since", "5m"), ("config", "--quiet")):
        _accepted(deploy_dir, stub, *args)


# -- the runbooks use only what the wrapper accepts -------------------------------

PLACEHOLDERS = {
    "<owner-email>": "owner@example.test",
    "<name>": "Owner Name",
    "<project-id>": "demo",
    "$PGC": "pgstub",
    "$s": "smm-api",
}


def _documented(script: str) -> list[tuple[str, list[str]]]:
    found = []

    for doc in (DEPLOY_DOC, ROLLBACK_DOC):
        text = doc.read_text(encoding="utf-8")

        blocks = re.findall(r"```bash\n(.*?)```", text, re.DOTALL)
        # Inline code outside the fences, e.g. in rollback tables.
        prose = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
        blocks += re.findall(r"`(\./[^`]+)`", prose)

        for block in blocks:
            for line in block.replace("\\\n", " ").splitlines():
                # Invocations only (./script), not mentions such as chmod.
                for match in re.finditer(rf"\./{re.escape(script)}\s+(.*)", line):
                    rest = match.group(1)

                    for placeholder, value in PLACEHOLDERS.items():
                        rest = rest.replace(f'"{placeholder}"', shlex.quote(value))
                        rest = rest.replace(placeholder, shlex.quote(value))

                    lexer = shlex.shlex(rest, posix=True, punctuation_chars=True)
                    lexer.whitespace_split = True
                    tokens = []

                    for token in lexer:
                        if token and token[0] in "|<>&;)":
                            break
                        tokens.append(token)

                    found.append((doc.name, tokens))

    return found


def test_runbooks_document_every_operation_through_the_wrapper() -> None:
    commands = {" ".join(tokens[:2]) for _, tokens in _documented("cc-compose.sh")}

    for expected in (
        "config --quiet", "up", "down", "ps", "ps -a", "logs --since",
        "restart smm-api", "migrate current", "migrate upgrade",
        "migrate downgrade", "create-owner --email",
    ):
        assert any(c == expected or c.startswith(expected) for c in commands), (
            expected, sorted(commands),
        )


def test_every_documented_wrapper_invocation_is_accepted(
    deploy_dir: Path, stub: DockerStub
) -> None:
    invocations = _documented("cc-compose.sh")

    assert len(invocations) >= 15

    for doc, tokens in invocations:
        stub.reset_calls()
        result = _run(deploy_dir, stub, *tokens)

        assert result.returncode == 0, (doc, tokens, result.stderr)
        assert len(stub.compose_calls()) == 1, (doc, tokens)


def test_every_documented_backup_invocation_is_accepted(
    deploy_dir: Path, stub: DockerStub, tmp_path: Path
) -> None:
    backups = tmp_path / "backups"
    backups.mkdir(mode=0o700)
    dump = backups / "ai_smm-pre-control-center-20261011T000000Z.dump"
    dump.write_bytes(STUB_DUMP)
    (backups / f"{dump.name}.sha256").write_text(
        f"{hashlib.sha256(STUB_DUMP).hexdigest()}  {dump.name}\n"
    )

    PLACEHOLDERS.update({"$BK": str(backups), "$DUMP": str(dump)})

    try:
        invocations = _documented("cc-db-backup.sh")
    finally:
        del PLACEHOLDERS["$BK"], PLACEHOLDERS["$DUMP"]

    assert {tokens[0] for _, tokens in invocations} == {"dump", "verify"}

    for doc, tokens in invocations:
        result = run_script(BACKUP, deploy_dir, *tokens, stub=stub)

        assert result.returncode == 0, (doc, tokens, result.stderr)


def test_runbooks_never_call_compose_directly() -> None:
    for doc in (DEPLOY_DOC, ROLLBACK_DOC):
        text = doc.read_text(encoding="utf-8")

        for block in re.findall(r"```bash\n(.*?)```", text, re.DOTALL):
            for use in re.findall(r"docker compose(?: [a-z-]+)?", block):
                assert use in ("docker compose ls", "docker compose version"), (
                    doc.name, use,
                )

            assert "--profile" not in block, doc.name
