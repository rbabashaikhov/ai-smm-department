"""Test helpers for deploy/control-center/*.sh.

A fake `docker` executable, put first on PATH, records every invocation
(argv and the environment it received) and answers the few questions the
scripts ask -- engine id, architecture, image labels, container env,
psql / pg_dump / pg_restore -- from a JSON state file. That lets the tests
assert the exact Compose command line a verb produces, and inject failures
(a pg_dump that dies, a DROP that fails) that a real daemon will not
produce on demand. No daemon, container or network is involved.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEPLOY_DIR = ROOT / "deploy" / "control-center"
WRAPPER = DEPLOY_DIR / "cc-compose.sh"
BACKUP = DEPLOY_DIR / "cc-db-backup.sh"
COMPOSE_PATH = ROOT / "compose.control-center.yml"

FIXTURE_VERSION = "abc1234def5678abc1234def5678abc1234def56"
FIXTURE_NETWORK = "cc-fixture-shared"
FIXTURE_DSN = "postgresql+psycopg://fixture:fixture-pw@fixture-db:5432/fixture"
FIXTURE_ENGINE = "fixture-engine-0001"

#: A minimal "custom format" archive as far as the scripts can tell.
STUB_DUMP = b"PGDMP" + b"\x00stub-archive\x00" * 8

_STUB_SOURCE = r'''#!{python}
import json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "state.json")) as handle:
    state = json.load(handle)

argv = sys.argv[1:]
with open(os.path.join(HERE, "calls.jsonl"), "a") as handle:
    handle.write(json.dumps({{"argv": argv, "env": dict(os.environ)}}) + "\n")


def out(text):
    sys.stdout.write(text)
    sys.exit(0)


def fail(message="refused", code=1):
    sys.stderr.write("stub: " + message + "\n")
    sys.exit(code)


if argv[:1] == ["info"]:
    if state.get("info_fails"):
        fail("Cannot connect to the Docker daemon")
    out(state["engine_id"] + "\n")

if argv[:1] == ["version"]:
    out(state.get("arch", "amd64") + "\n")

if argv[:2] == ["image", "inspect"]:
    image = state.get("images", {{}}).get(argv[-1])
    if image is None:
        fail("No such image: " + argv[-1])
    out(image["arch"] + " " + image["revision"] + "\n")

if argv[:1] == ["compose"]:
    sys.exit(state.get("compose_rc", 0))

if argv[:1] == ["inspect"]:
    fmt = argv[argv.index("--format") + 1]
    if "State.Running" in fmt:
        out("true\n" if state.get("pg_running", True) else "false\n")
    out("POSTGRES_PASSWORD=never-printed\nPOSTGRES_USER=pgadmin\nPOSTGRES_DB=postgres\n")

if argv[:1] == ["exec"]:
    index = 1
    while argv[index].startswith("-"):
        index += 2 if argv[index] == "-e" else 1
    command = argv[index + 1:]
    tool = command[0]

    if tool == "psql":
        sql = command[command.index("-c") + 1]
        if sql.startswith("SELECT 1 FROM pg_database"):
            out("1\n")
        if "pg_database_size" in sql:
            out(str(state.get("db_size", 1000)) + "\n")
        if "string_agg(datname" in sql:
            out(state.get("leftover", "") + "\n")
        if sql.startswith("CREATE DATABASE"):
            sys.exit(state.get("create_rc", 0))
        if sql.startswith("DROP DATABASE"):
            sys.exit(state.get("drop_rc", 0))
        if "count(*) FROM pg_database WHERE datname" in sql:
            out("1\n" if state.get("drop_rc", 0) else "0\n")
        if "alembic_version" in sql:
            out(state.get("revision", "79731eadacd7") + "\n")
        if "FROM publications" in sql:
            out("published 3\n")
        fail("unexpected sql: " + sql, 64)

    if tool == "pg_dump":
        sys.stdout.buffer.write(bytes.fromhex(state.get("dump_hex", "")))
        sys.stdout.flush()
        sys.exit(state.get("dump_rc", 0))

    if tool == "pg_restore":
        data = sys.stdin.buffer.read()
        if "--list" in command:
            if not data.startswith(b"PGDMP") or state.get("list_rc"):
                fail("input file does not appear to be a valid archive")
            out("1; 0 0 TABLE DATA public alembic_version pgadmin\n"
                "2; 0 0 TABLE DATA public publications pgadmin\n")
        sys.exit(state.get("restore_rc", 0))

fail("unexpected: " + " ".join(argv), 64)
'''


class DockerStub:
    def __init__(self, directory: Path) -> None:
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)

        executable = self.dir / "docker"
        executable.write_text(
            _STUB_SOURCE.format(python=sys.executable), encoding="utf-8"
        )
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR)

        self.state: dict[str, Any] = {
            "engine_id": FIXTURE_ENGINE,
            "arch": "amd64",
            "images": {
                f"ai-smm-api:{FIXTURE_VERSION}": {
                    "arch": "amd64", "revision": FIXTURE_VERSION,
                },
                f"ai-smm-web:{FIXTURE_VERSION}": {
                    "arch": "amd64", "revision": FIXTURE_VERSION,
                },
            },
            "dump_hex": STUB_DUMP.hex(),
        }
        self.save()

    def save(self) -> None:
        (self.dir / "state.json").write_text(
            json.dumps(self.state), encoding="utf-8"
        )

    def update(self, **changes: Any) -> None:
        self.state.update(changes)
        self.save()

    def calls(self) -> list[dict[str, Any]]:
        log = self.dir / "calls.jsonl"

        if not log.exists():
            return []

        return [
            json.loads(line)
            for line in log.read_text(encoding="utf-8").splitlines()
        ]

    def compose_calls(self) -> list[list[str]]:
        return [c["argv"] for c in self.calls() if c["argv"][:1] == ["compose"]]

    def reset_calls(self) -> None:
        (self.dir / "calls.jsonl").unlink(missing_ok=True)


@contextmanager
def docker_socket() -> Iterator[str]:
    """A real unix socket path, short enough for AF_UNIX."""

    directory = tempfile.mkdtemp(prefix="cc-sock-", dir="/tmp")
    path = os.path.join(directory, "docker.sock")

    with socket.socket(socket.AF_UNIX) as sock:
        sock.bind(path)

    try:
        yield f"unix://{path}"
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def write_deploy_dir(
    directory: Path,
    *,
    docker_host: str,
    engine_id: str = FIXTURE_ENGINE,
    version: str = FIXTURE_VERSION,
    network: str = FIXTURE_NETWORK,
    dsn: str = FIXTURE_DSN,
) -> Path:
    """A Control Center directory as on the host, with fixture values."""

    directory.mkdir(parents=True, exist_ok=True)
    shutil.copy(COMPOSE_PATH, directory / COMPOSE_PATH.name)

    (directory / "control-center.compose.env").write_text(
        f"SMM_CC_VERSION={version}\nSMM_CC_SHARED_NETWORK={network}\n",
        encoding="utf-8",
    )

    secret = directory / "control-center.env"
    secret.write_text(f"AI_SMM_DATABASE_URL={dsn}\n", encoding="utf-8")
    secret.chmod(0o600)

    (directory / "control-center.target").write_text(
        f"SMM_CC_DOCKER_HOST={docker_host}\n"
        f"SMM_CC_DOCKER_ENGINE_ID={engine_id}\n",
        encoding="utf-8",
    )

    return directory


def run_script(
    script: Path,
    deploy_dir: Path,
    *args: str,
    stub: DockerStub | None = None,
    extra_env: dict[str, str] | None = None,
    stdin: str | None = None,
) -> subprocess.CompletedProcess[str]:
    path = os.environ.get("PATH", "")

    if stub is not None:
        path = f"{stub.dir}{os.pathsep}{path}"

    env = {
        "PATH": path,
        "HOME": os.environ.get("HOME", "/tmp"),
        "SMM_CC_DIR": str(deploy_dir),
        **(extra_env or {}),
    }

    return subprocess.run(
        ["bash", str(script), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=deploy_dir,
        input=stdin if stdin is not None else "",
        check=False,
    )


def compose_prefix(deploy_dir: Path) -> list[str]:
    """What every Compose invocation must start with."""

    real = Path(os.path.realpath(deploy_dir))

    return [
        "compose",
        "--project-name", "ai-smm-control-center",
        "--project-directory", str(real),
        "--file", str(real / "compose.control-center.yml"),
        "--env-file", str(real / "control-center.compose.env"),
    ]
