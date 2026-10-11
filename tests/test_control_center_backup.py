"""deploy/control-center/cc-db-backup.sh: the pre-migration dump and the
restore check stop safely on every failure, and never write to ai_smm.

Failures are injected through the fake `docker` (tests/cc_stub.py): a
pg_dump that dies half-way, an empty or corrupt archive, a scratch
database that already exists, a restore that fails, a DROP that fails.
The happy path and a corrupt archive are also run against a real
PostgreSQL by scripts/control_center_rehearsal.sh.
"""
from __future__ import annotations

import hashlib
import re
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.cc_stub import (
    BACKUP,
    STUB_DUMP,
    DockerStub,
    docker_socket,
    run_script,
    write_deploy_dir,
)


pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash not available"
)

SCRATCH = re.compile(r"^ai_smm_restore_check_[0-9]{14}$")
CONTAINER = "pgstub"


@pytest.fixture
def deploy_dir(tmp_path: Path) -> Iterator[Path]:
    with docker_socket() as host:
        yield write_deploy_dir(tmp_path / "ai-smm-control-center", docker_host=host)


@pytest.fixture
def stub(tmp_path: Path) -> DockerStub:
    return DockerStub(tmp_path / "stub-bin")


@pytest.fixture
def backups(tmp_path: Path) -> Path:
    directory = tmp_path / "backups"
    directory.mkdir(mode=0o700)

    return directory


def _dump(deploy_dir: Path, stub: DockerStub, backups: Path):
    return run_script(
        BACKUP, deploy_dir, "dump", "--container", CONTAINER,
        "--out-dir", str(backups), stub=stub,
    )


def _verify(deploy_dir: Path, stub: DockerStub, dump: Path, *extra: str):
    return run_script(
        BACKUP, deploy_dir, "verify", "--container", CONTAINER,
        "--dump", str(dump), *extra, stub=stub,
    )


def _good_dump(backups: Path, content: bytes = STUB_DUMP) -> Path:
    dump = backups / "ai_smm-pre-control-center-20261011T000000Z.dump"
    dump.write_bytes(content)
    (backups / f"{dump.name}.sha256").write_text(
        f"{hashlib.sha256(content).hexdigest()}  {dump.name}\n"
    )

    return dump


def _sql(stub: DockerStub) -> list[tuple[str, str, bool]]:
    """(database, statement, read_only) of every psql call."""

    statements = []

    for call in stub.calls():
        argv = call["argv"]

        if argv[:1] != ["exec"] or "psql" not in argv:
            continue

        read_only = any(
            "default_transaction_read_only=on" in a for a in argv
        )
        statements.append(
            (argv[argv.index("-d") + 1], argv[argv.index("-c") + 1], read_only)
        )

    return statements


def _restore_targets(stub: DockerStub) -> list[str]:
    return [
        c["argv"][c["argv"].index("-d") + 1]
        for c in stub.calls()
        if c["argv"][:1] == ["exec"] and "pg_restore" in c["argv"]
        and "-d" in c["argv"]
    ]


def _assert_ai_smm_only_read(stub: DockerStub) -> None:
    for database, statement, read_only in _sql(stub):
        if database == "ai_smm":
            assert read_only, statement
        if statement.startswith(("CREATE", "DROP")):
            name = statement.split('"')[1]

            assert SCRATCH.match(name), statement
            assert database == "postgres", statement
        else:
            assert read_only, statement

    for target in _restore_targets(stub):
        assert SCRATCH.match(target), target


def _files(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir())


# -- dump --------------------------------------------------------------------


def test_dump_writes_the_archive_and_its_checksum(
    deploy_dir: Path, stub: DockerStub, backups: Path
) -> None:
    result = _dump(deploy_dir, stub, backups)

    assert result.returncode == 0, result.stderr

    files = _files(backups)

    assert len(files) == 2
    dump = backups / files[0]

    assert dump.name.endswith(".dump")
    assert dump.read_bytes() == STUB_DUMP
    assert (dump.stat().st_mode & 0o777) == 0o600
    assert (backups / f"{dump.name}.sha256").read_text().split()[0] == (
        hashlib.sha256(STUB_DUMP).hexdigest()
    )
    _assert_ai_smm_only_read(stub)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"dump_rc": 1}, "pg_dump failed"),
        ({"dump_rc": 1, "dump_hex": STUB_DUMP[:9].hex()}, "pg_dump failed"),
        ({"dump_hex": ""}, "empty file"),
        ({"dump_hex": b"not an archive".hex()}, "unreadable"),
        ({"list_rc": 1}, "unreadable"),
        ({"db_size": 10**18}, "need more than twice"),
        ({"pg_running": False}, "is not running"),
    ],
    ids=["pg_dump-fails", "pg_dump-dies-half-way", "empty", "not-an-archive",
         "toc-unreadable", "no-disk-space", "container-down"],
)
def test_a_failed_backup_leaves_nothing_behind(
    deploy_dir: Path, stub: DockerStub, backups: Path,
    changes: dict, message: str,
) -> None:
    stub.update(**changes)

    result = _dump(deploy_dir, stub, backups)

    assert result.returncode == 2, result.stdout
    assert message in result.stderr, result.stderr
    # No partial file, no half-written archive, no checksum of one.
    assert _files(backups) == []
    _assert_ai_smm_only_read(stub)


def test_dump_refuses_a_directory_others_can_read(
    deploy_dir: Path, stub: DockerStub, backups: Path
) -> None:
    backups.chmod(0o755)

    result = _dump(deploy_dir, stub, backups)

    assert result.returncode == 2
    assert "use a 700 directory" in result.stderr
    assert not any(c["argv"][:1] == ["exec"] and "pg_dump" in c["argv"]
                   for c in stub.calls())


def test_dump_refuses_another_engine(
    deploy_dir: Path, stub: DockerStub, backups: Path
) -> None:
    stub.update(engine_id="some-other-engine-42")

    result = _dump(deploy_dir, stub, backups)

    assert result.returncode == 2
    assert "not the pinned" in result.stderr
    assert [c["argv"][0] for c in stub.calls()] == ["info"]


# -- verify ------------------------------------------------------------------


def test_verify_restores_into_a_scratch_database_and_drops_it(
    deploy_dir: Path, stub: DockerStub, backups: Path
) -> None:
    result = _verify(
        deploy_dir, stub, _good_dump(backups), "--expect-revision", "79731eadacd7"
    )

    assert result.returncode == 0, result.stderr
    assert "restore check passed" in result.stdout
    assert "restored revision: 79731eadacd7" in result.stdout

    statements = [s for _, s, _ in _sql(stub)]
    created = [s for s in statements if s.startswith("CREATE DATABASE")]
    dropped = [s for s in statements if s.startswith("DROP DATABASE")]

    assert len(created) == 1 and len(dropped) == 1
    assert created[0].split('"')[1] == dropped[0].split('"')[1]
    assert len(_restore_targets(stub)) == 1
    _assert_ai_smm_only_read(stub)


def test_a_corrupted_archive_is_refused_before_any_database_is_created(
    deploy_dir: Path, stub: DockerStub, backups: Path
) -> None:
    dump = _good_dump(backups)
    data = bytearray(dump.read_bytes())
    data[-1] ^= 0xFF
    dump.write_bytes(bytes(data))

    result = _verify(deploy_dir, stub, dump)

    assert result.returncode == 2
    assert "checksum mismatch" in result.stderr
    assert not any(s.startswith("CREATE") for _, s, _ in _sql(stub))
    assert _restore_targets(stub) == []


def test_a_dump_without_checksum_is_refused(
    deploy_dir: Path, stub: DockerStub, backups: Path
) -> None:
    dump = _good_dump(backups)
    (backups / f"{dump.name}.sha256").unlink()

    result = _verify(deploy_dir, stub, dump)

    assert result.returncode == 2
    assert ".sha256 is missing" in result.stderr


def test_an_archive_corrupted_before_its_checksum_fails_the_restore(
    deploy_dir: Path, stub: DockerStub, backups: Path
) -> None:
    """The checksum matches, the data does not restore: scratch dropped."""

    stub.update(restore_rc=1)

    result = _verify(deploy_dir, stub, _good_dump(backups))

    assert result.returncode == 2
    assert "pg_restore failed" in result.stderr
    assert "dropped" in result.stderr

    statements = [s for _, s, _ in _sql(stub)]

    assert sum(s.startswith("CREATE DATABASE") for s in statements) == 1
    assert sum(s.startswith("DROP DATABASE") for s in statements) == 1
    _assert_ai_smm_only_read(stub)


def test_an_unreadable_archive_is_refused(
    deploy_dir: Path, stub: DockerStub, backups: Path
) -> None:
    result = _verify(deploy_dir, stub, _good_dump(backups, b"garbage-not-pgdmp"))

    assert result.returncode == 2
    assert "not a readable custom-format archive" in result.stderr
    assert not any(s.startswith("CREATE") for _, s, _ in _sql(stub))


def test_an_existing_scratch_database_is_never_dropped(
    deploy_dir: Path, stub: DockerStub, backups: Path
) -> None:
    stub.update(leftover="ai_smm_restore_check_20261010120000")

    result = _verify(deploy_dir, stub, _good_dump(backups))

    assert result.returncode == 2
    assert "already exist" in result.stderr
    assert "ai_smm_restore_check_20261010120000" in result.stderr

    statements = [s for _, s, _ in _sql(stub)]

    assert not any(s.startswith(("CREATE", "DROP")) for s in statements)
    assert _restore_targets(stub) == []


def test_a_failed_create_leaves_nothing_to_drop(
    deploy_dir: Path, stub: DockerStub, backups: Path
) -> None:
    stub.update(create_rc=1)

    result = _verify(deploy_dir, stub, _good_dump(backups))

    assert result.returncode == 2
    assert "cannot create" in result.stderr
    assert not any(s.startswith("DROP") for _, s, _ in _sql(stub))
    assert _restore_targets(stub) == []


def test_a_failed_drop_exits_3_and_names_the_database(
    deploy_dir: Path, stub: DockerStub, backups: Path
) -> None:
    stub.update(drop_rc=1)

    result = _verify(deploy_dir, stub, _good_dump(backups))

    assert result.returncode == 3, result.stderr
    assert "WAS NOT DROPPED" in result.stderr
    assert "ai_smm was not touched" in result.stderr
    assert re.search(r'DROP DATABASE "ai_smm_restore_check_[0-9]{14}"', result.stderr)
    assert "restore check passed" not in result.stdout
    _assert_ai_smm_only_read(stub)


def test_a_failed_drop_after_a_failed_restore_still_exits_3(
    deploy_dir: Path, stub: DockerStub, backups: Path
) -> None:
    stub.update(restore_rc=1, drop_rc=1)

    result = _verify(deploy_dir, stub, _good_dump(backups))

    assert result.returncode == 3
    assert "WAS NOT DROPPED" in result.stderr


def test_an_unexpected_revision_fails_and_drops_the_scratch_database(
    deploy_dir: Path, stub: DockerStub, backups: Path
) -> None:
    stub.update(revision="3d4ac1986f07")

    result = _verify(
        deploy_dir, stub, _good_dump(backups), "--expect-revision", "79731eadacd7"
    )

    assert result.returncode == 2
    assert "expected 79731eadacd7" in result.stderr
    assert sum(s.startswith("DROP") for _, s, _ in _sql(stub)) == 1


@pytest.mark.parametrize(
    "args",
    [
        ("verify", "--container", CONTAINER, "--dump", "/x", "--out-dir", "/y"),
        ("dump", "--container", CONTAINER, "--dump", "/x"),
        ("dump", "--container", "pg; rm -rf /", "--out-dir", "/tmp"),
        (
            "verify", "--container", CONTAINER, "--dump", "/x",
            "--expect-revision", "head",
        ),
        ("restore", "--container", CONTAINER),
        ("dump", "--container"),
    ],
    ids=["verify-out-dir", "dump-dump", "container-injection",
         "revision-head", "restore-verb", "dangling"],
)
def test_unsupported_arguments_are_refused_before_docker(
    deploy_dir: Path, stub: DockerStub, args: tuple[str, ...]
) -> None:
    result = run_script(BACKUP, deploy_dir, *args, stub=stub)

    assert result.returncode == 2
    assert stub.calls() == []
