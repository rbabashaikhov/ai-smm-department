"""Test helpers for deploy/control-center/*.sh.

A fake `docker` executable, put first on PATH, records every invocation
(argv and the environment it received) and answers the few questions the
scripts ask -- engine id, architecture, image labels, containers (id,
state, env, networks and DNS names), the containers on a network,
psql / pg_dump / pg_restore -- from a JSON state file. That lets the tests
assert the exact Compose command line a verb produces, and inject failures
(a pg_dump that dies, a DROP that fails, another PostgreSQL behind the
same name, a busy queue) that a real daemon will not produce on demand.
No daemon, container or network is involved.

Every identifier here is a fixture. The real ones are recorded per
deployment in control-center.identity on the host, never in Git.
"""
from __future__ import annotations

import base64
import hashlib
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
FIXTURE_ENGINE = "fixture-engine-0001"
#: Not a real key: 32 fixed bytes, base64 (control-center.compose.env).
FIXTURE_PUBLIC_KEY = base64.b64encode(hashlib.sha256(b"fixture-key").digest()).decode()
FIXTURE_PUBLIC_KEY_ID = hashlib.sha256(
    hashlib.sha256(b"fixture-key").digest()
).hexdigest()[:16]

FIXTURE_PG_CONTAINER = "fixture-db"
FIXTURE_PG_ID = hashlib.sha256(b"fixture-db").hexdigest()
FIXTURE_WORKER = "fixture-worker"
FIXTURE_WORKER_ID = hashlib.sha256(b"fixture-worker").hexdigest()
FIXTURE_OTHER_ID = hashlib.sha256(b"fixture-other").hexdigest()
FIXTURE_SYSTEM_ID = "7000000000000000001"
FIXTURE_DB_OID = "16384"
FIXTURE_DSN = (
    f"postgresql+psycopg://fixture:fixture-pw@{FIXTURE_PG_CONTAINER}:5432/ai_smm"
)
#: smm-api's URL: the API role, same container, port and database.
FIXTURE_API_DSN = (
    f"postgresql+psycopg://ai_smm_cc:fixture-api-pw@{FIXTURE_PG_CONTAINER}:5432/ai_smm"
)
#: smm-auth's URL (SMM-025D): the sign-in role, same container, port, database.
FIXTURE_AUTH_DSN = (
    f"postgresql+psycopg://ai_smm_auth:fixture-auth-pw@{FIXTURE_PG_CONTAINER}:5432/ai_smm"
)
VERIFY_SQL = DEPLOY_DIR / "db" / "ai_smm_cc.verify.sql"
AUTH_VERIFY_SQL = DEPLOY_DIR / "db" / "ai_smm_auth.verify.sql"
FIXTURE_WINDOW = ("2026-10-11T10:00:00Z", "2026-10-11T12:00:00Z")
FIXTURE_REVISION = "79731eadacd7"
#: dump taken, restore-checked, off-host copy confirmed -- inside the window
FIXTURE_TAKEN_AT = "2026-10-11T10:20:00Z"
FIXTURE_VERIFIED_AT = "2026-10-11T10:25:00Z"
FIXTURE_ACK_AT = "2026-10-11T10:40:00Z"

IDENTITY_ANSWER = f"identity|{FIXTURE_SYSTEM_ID}|{FIXTURE_DB_OID}|5432|ai_smm"
#: queue | in window | window length ok | publishing | lease | due | retry
#: | published in margin | attempts | needs review | now
QUEUE_QUIET = "queue|1|1|0|0|0|0|0|0|0|2026-10-11T10:30:00Z"



def api_role_answer(*lines: str, checks: int = 240) -> str:
    """ai_smm_cc.verify.sql output as psql -tA prints it: a passing role.

    `lines` are added (a FAIL, an INFO); the END line counts them all.
    """

    body = [
        "INFO|database.temporary|may create temporary tables (PUBLIC default)",
        "PASS|role.attributes|super=false createrole=false createdb=false",
        "PASS|role.exists|ai_smm_cc",
        "PASS|role.member_of|none",
        "PASS|role.owns_nothing|0 owned objects",
        "PASS|role.password|set, SCRAM-SHA-256",
        "PASS|schema.public.create|",
        "PASS|table.publications.update|denied",
        *lines,
    ]
    body += [
        f"PASS|column.fixture.c{n}.update|denied"
        for n in range(checks - len(body))
    ]

    return "\n".join([*body, f"END|checks|{len(body)}"])


API_ROLE_PASS = api_role_answer()


def auth_role_answer(*lines: str, checks: int = 950) -> str:
    """ai_smm_auth.verify.sql output as psql -tA prints it: a passing role."""

    body = [
        "INFO|database.temporary|may create temporary tables (PUBLIC default)",
        "PASS|role.attributes|super=f createrole=f createdb=f",
        "PASS|role.exists|ai_smm_auth",
        "PASS|role.member_of|none",
        "PASS|role.owns_nothing|0 owned objects",
        "PASS|role.password|set, SCRAM-SHA-256",
        "PASS|schema.public.create|",
        "PASS|schema.smm_auth.objects|designed functions only",
        *lines,
    ]
    body += [
        f"PASS|column.fixture.c{n}.update|denied"
        for n in range(checks - len(body))
    ]

    return "\n".join([*body, f"END|checks|{len(body)}"])


AUTH_ROLE_PASS = auth_role_answer()

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


def container(ref):
    for name, c in state["containers"].items():
        if ref == name or ref == c["id"]:
            return name, c
    fail("Error: No such object: " + ref)


if argv[:2] == ["network", "inspect"]:
    network = argv[-1]
    if network in state.get("network_inspect_fails", []):
        fail("network " + network + " not found")
    ids = [c["id"] for c in state["containers"].values()
           if network in c.get("networks", {{}}) and c.get("running", True)]
    out("".join(i + "\n" for i in ids) + state.get("network_extra", ""))

if argv[:1] == ["inspect"]:
    fmt = argv[argv.index("--format") + 1]
    name, c = container(argv[-1])
    if c.get("inspect_fails"):
        fail("Error response from daemon: inspect failed")
    if fmt == "{{{{.Id}}}} {{{{.State.Running}}}}":
        running = "true" if c.get("running", True) else "false"
        out(c.get("state_answer", c["id"] + " " + running) + "\n")
    if "Config.Env" in fmt:
        out("".join(e + "\n" for e in c.get("env", [])))
    if "NetworkSettings.Networks" in fmt:
        network = fmt.split('Networks "')[1].split('"')[0]
        line = c["id"] + " /" + name
        if network in c.get("networks", {{}}):
            n = c["networks"][network]
            # As the real template renders it: " @on" then " <name>" each.
            names = n.get("dns", []) + n.get("aliases", [])
            line += " @on" + "".join(" " + x for x in names)
        out(line + "\n")
    fail("unexpected inspect format: " + fmt, 64)

if argv[:1] == ["exec"]:
    index = 1
    while argv[index].startswith("-"):
        index += 2 if argv[index] == "-e" else 1
    command = argv[index + 1:]
    tool = command[0]

    if tool == "psql":
        sql = command[command.index("-c") + 1]
        if "'checks'" in sql and "'ai_smm_auth'" in sql:
            # ai_smm_auth.verify.sql (SMM-025D).
            if state.get("auth_role_rc"):
                fail("psql: error: connection to server failed", state["auth_role_rc"])
            if state.get("auth_sql_url"):
                # The file itself, read-only, against the real test database.
                import psycopg
                options = " ".join(
                    a.split("=", 1)[1] for a in argv if a.startswith("PGOPTIONS=")
                )
                with psycopg.connect(state["auth_sql_url"], options=options) as conn:
                    rows = conn.execute(sql).fetchall()
                out("".join(row[0] + "\n" for row in rows))
            out(state.get("auth_role_answer", "") + "\n")
        if "'checks'" in sql and "ai_smm_cc" in sql:
            # ai_smm_cc.verify.sql. With role_sql_url: the file itself, run
            # read-only against a real test database as its superuser.
            if state.get("api_role_rc"):
                fail("psql: error: connection to server failed", state["api_role_rc"])
            if state.get("role_sql_url"):
                import psycopg
                options = " ".join(
                    a.split("=", 1)[1] for a in argv if a.startswith("PGOPTIONS=")
                )
                with psycopg.connect(state["role_sql_url"], options=options) as conn:
                    rows = conn.execute(sql).fetchall()
                out("".join(row[0] + "\n" for row in rows))
            out(state.get("api_role_answer", "") + "\n")
        queue_sql = "'queue'," in sql or (
            "ORDER BY id" in sql and "lease_expires_at" in sql
        )
        if state.get("sql_url") and queue_sql:
            # The queue SQL itself, run against a disposable test database,
            # with the read-only option the script passed to psql.
            import psycopg
            options = " ".join(
                a.split("=", 1)[1] for a in argv if a.startswith("PGOPTIONS=")
            )
            try:
                with psycopg.connect(state["sql_url"], options=options) as conn:
                    rows = conn.execute(sql).fetchall()
            except psycopg.Error as error:
                fail("ERROR:  " + str(error).splitlines()[0], 1)
            out("".join(
                "|".join("" if v is None else str(v) for v in row) + "\n"
                for row in rows
            ))
        if "pg_control_system" in sql:
            if state.get("identity_rc"):
                fail("psql: error: connection to server failed", state["identity_rc"])
            out(state.get("identity_answer", "") + "\n")
        if "'queue'," in sql:
            if state.get("queue_rc"):
                fail('ERROR:  relation "publications" does not exist',
                     state["queue_rc"])
            out(state.get("queue_answer", "") + "\n")
        if "ORDER BY id" in sql and "lease_expires_at" in sql:
            out(state.get("queue_rows", "") + "\n")
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
            # The live database, or the scratch copy a restore check made.
            live = state.get("revision", "79731eadacd7")
            database = command[command.index("-d") + 1]
            if database.startswith("ai_smm_restore_check_"):
                out(state.get("restored_revision", live) + "\n")
            out(live + "\n")
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
            "containers": {
                FIXTURE_PG_CONTAINER: {
                    "id": FIXTURE_PG_ID,
                    "env": [
                        "POSTGRES_PASSWORD=never-printed",
                        "POSTGRES_USER=pgadmin",
                        "POSTGRES_DB=postgres",
                    ],
                    "networks": {
                        FIXTURE_NETWORK: {
                            "dns": [
                                FIXTURE_PG_CONTAINER, "postgres", FIXTURE_PG_ID[:12],
                            ],
                        },
                    },
                },
                FIXTURE_WORKER: {
                    "id": FIXTURE_WORKER_ID,
                    "env": [
                        f"AI_SMM_DATABASE_URL={FIXTURE_DSN}",
                        "AI_SMM_DRY_RUN=false",
                    ],
                    "networks": {
                        FIXTURE_NETWORK: {
                            "dns": [FIXTURE_WORKER, "worker", FIXTURE_WORKER_ID[:12]],
                        },
                    },
                },
                "fixture-other": {
                    "id": FIXTURE_OTHER_ID,
                    "env": [],
                    "networks": {
                        FIXTURE_NETWORK: {
                            "dns": ["fixture-other", FIXTURE_OTHER_ID[:12]],
                        },
                    },
                },
            },
            "identity_answer": IDENTITY_ANSWER,
            "queue_answer": QUEUE_QUIET,
            "api_role_answer": API_ROLE_PASS,
            "auth_role_answer": AUTH_ROLE_PASS,
        }
        self.save()

    def container(self, name: str) -> dict[str, Any]:
        return self.state["containers"][name]

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
    public_key: str = FIXTURE_PUBLIC_KEY,
    legacy_projects: str = "",
    dsn: str = FIXTURE_DSN,
    api_dsn: str = FIXTURE_API_DSN,
    auth_dsn: str = FIXTURE_AUTH_DSN,
    identity: dict[str, str] | None = None,
    window: tuple[str, str] = FIXTURE_WINDOW,
    evidence: bool = True,
) -> Path:
    """A Control Center directory as on the host, with fixture values."""

    directory.mkdir(parents=True, exist_ok=True)
    shutil.copy(COMPOSE_PATH, directory / COMPOSE_PATH.name)

    (directory / "control-center.compose.env").write_text(
        f"SMM_CC_VERSION={version}\nSMM_CC_SHARED_NETWORK={network}\n"
        f"SMM_CC_CREDENTIALS_PUBLIC_KEY={public_key}\n"
        f"SMM_CC_LEGACY_THREADS_PROJECTS={legacy_projects}\n",
        encoding="utf-8",
    )

    secret = directory / "control-center.env"
    secret.write_text(f"AI_SMM_DATABASE_URL={dsn}\n", encoding="utf-8")
    secret.chmod(0o600)

    api_secret = directory / "control-center.api.env"
    api_secret.write_text(f"AI_SMM_DATABASE_URL={api_dsn}\n", encoding="utf-8")
    api_secret.chmod(0o600)

    auth_secret = directory / "control-center.auth.env"
    auth_secret.write_text(f"AI_SMM_DATABASE_URL={auth_dsn}\n", encoding="utf-8")
    auth_secret.chmod(0o600)

    shutil.copy(VERIFY_SQL, directory / VERIFY_SQL.name)
    shutil.copy(AUTH_VERIFY_SQL, directory / AUTH_VERIFY_SQL.name)

    (directory / "control-center.target").write_text(
        f"SMM_CC_DOCKER_HOST={docker_host}\n"
        f"SMM_CC_DOCKER_ENGINE_ID={engine_id}\n",
        encoding="utf-8",
    )

    write_identity(directory, **(identity or {}))
    write_window(directory, *window)

    if evidence:
        kept = directory.parent / "kept-dump"
        kept.mkdir(mode=0o700, exist_ok=True)
        dump = write_dump(kept)
        write_evidence(directory, dump)
        write_offhost_ack(directory, sha256_of(dump))

    return directory


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_dump(
    directory: Path,
    content: bytes = STUB_DUMP,
    *,
    name: str = "cc-premigration-ai_smm-20261011T102000Z.dump",
    **source: str,
) -> Path:
    """A dump as `cc-db-backup.sh dump` leaves it: archive, .sha256, .source."""

    dump = directory / name
    dump.write_bytes(content)
    sha = hashlib.sha256(content).hexdigest()
    (directory / f"{name}.sha256").write_text(f"{sha}  {name}\n")

    values = {
        "SMM_CC_SOURCE_DUMP": name,
        "SMM_CC_SOURCE_SHA256": sha,
        "SMM_CC_SOURCE_PG_CONTAINER_ID": FIXTURE_PG_ID,
        "SMM_CC_SOURCE_SYSTEM_IDENTIFIER": FIXTURE_SYSTEM_ID,
        "SMM_CC_SOURCE_DATABASE_OID": FIXTURE_DB_OID,
        "SMM_CC_SOURCE_REVISION": FIXTURE_REVISION,
        "SMM_CC_SOURCE_TAKEN_AT": FIXTURE_TAKEN_AT,
        **source,
    }
    (directory / f"{name}.source").write_text(
        "".join(f"{k}={v}\n" for k, v in values.items())
    )

    return dump


def evidence_text(dump: Path, **overrides: str) -> str:
    """control-center.backup-evidence as `verify` writes it, self-checksummed.

    An override of SMM_CC_EVIDENCE_CHECKSUM replaces the computed one.
    """

    checksum = overrides.pop("SMM_CC_EVIDENCE_CHECKSUM", None)
    values = {
        "SMM_CC_EVIDENCE_DUMP": str(dump.resolve()),
        "SMM_CC_EVIDENCE_DUMP_SHA256": sha256_of(dump),
        "SMM_CC_EVIDENCE_PG_CONTAINER_ID": FIXTURE_PG_ID,
        "SMM_CC_EVIDENCE_SYSTEM_IDENTIFIER": FIXTURE_SYSTEM_ID,
        "SMM_CC_EVIDENCE_DATABASE_OID": FIXTURE_DB_OID,
        "SMM_CC_EVIDENCE_REVISION": FIXTURE_REVISION,
        "SMM_CC_EVIDENCE_TAKEN_AT": FIXTURE_TAKEN_AT,
        "SMM_CC_EVIDENCE_VERIFIED_AT": FIXTURE_VERIFIED_AT,
        **overrides,
    }
    body = "".join(f"{k}={v}\n" for k, v in values.items())
    checksum = checksum or hashlib.sha256(body.encode()).hexdigest()

    return body + f"SMM_CC_EVIDENCE_CHECKSUM={checksum}\n"


def write_evidence(directory: Path, dump: Path, **overrides: str) -> None:
    (directory / "control-center.backup-evidence").write_text(
        evidence_text(dump, **overrides)
    )


def write_offhost_ack(
    directory: Path,
    sha: str,
    at: str = FIXTURE_ACK_AT,
    by: str = "fixture-owner",
) -> None:
    (directory / "control-center.offhost-ack").write_text(
        f"SMM_CC_OFFHOST_DUMP_SHA256={sha}\n"
        f"SMM_CC_OFFHOST_CONFIRMED_AT={at}\n"
        f"SMM_CC_OFFHOST_CONFIRMED_BY={by}\n"
    )


def write_identity(directory: Path, **overrides: str) -> None:
    values = {
        "SMM_CC_PG_CONTAINER": FIXTURE_PG_CONTAINER,
        "SMM_CC_PG_CONTAINER_ID": FIXTURE_PG_ID,
        "SMM_CC_PG_SYSTEM_IDENTIFIER": FIXTURE_SYSTEM_ID,
        "SMM_CC_PG_DATABASE_OID": FIXTURE_DB_OID,
        "SMM_CC_WORKER_CONTAINER": FIXTURE_WORKER,
        **overrides,
    }
    (directory / "control-center.identity").write_text(
        "".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8"
    )


def write_window(directory: Path, start: str, end: str) -> None:
    (directory / "control-center.window").write_text(
        f"SMM_CC_WINDOW_START={start}\nSMM_CC_WINDOW_END={end}\n",
        encoding="utf-8",
    )


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
