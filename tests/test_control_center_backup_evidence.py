"""Backup evidence (cc-gate.sh, cc-db-backup.sh): `migrate upgrade` runs
only with proof that THIS database, at THIS revision, was dumped, that the
dump restores, and that the operator confirmed a copy off the host.

  dump    -> <dump>.source            sha256, recorded identity, live revision
  verify  -> control-center.backup-evidence   only on full success; every
             verify run deletes the previous one first
  operator-> control-center.offhost-ack       sha256 computed off the host

Every refusal: exit 2, a reason, and no Compose call. `migrate downgrade`
never needs the evidence and voids it.
"""

from __future__ import annotations

import hashlib
import re
import shutil
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.cc_stub import (
    BACKUP,
    FIXTURE_ACK_AT,
    FIXTURE_DB_OID,
    FIXTURE_PG_CONTAINER,
    FIXTURE_PG_ID,
    FIXTURE_SYSTEM_ID,
    FIXTURE_TAKEN_AT,
    FIXTURE_VERIFIED_AT,
    WRAPPER,
    DockerStub,
    docker_socket,
    evidence_text,
    run_script,
    sha256_of,
    write_deploy_dir,
    write_dump,
    write_evidence,
    write_offhost_ack,
    write_window,
)


pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash not available"
)

UPGRADE = ("migrate", "upgrade", "3d4ac1986f07")
DOWNGRADE = ("migrate", "downgrade", "79731eadacd7")
EVIDENCE = "control-center.backup-evidence"
ACK = "control-center.offhost-ack"


@pytest.fixture
def deploy_dir(tmp_path: Path) -> Iterator[Path]:
    with docker_socket() as host:
        yield write_deploy_dir(tmp_path / "ai-smm-control-center", docker_host=host)


@pytest.fixture
def stub(tmp_path: Path) -> DockerStub:
    return DockerStub(tmp_path / "stub-bin")


@pytest.fixture
def kept(deploy_dir: Path) -> Path:
    """The dump the default evidence names (written by write_deploy_dir)."""

    return next((deploy_dir.parent / "kept-dump").glob("*.dump"))


def _wrapper(deploy_dir: Path, stub: DockerStub, *args: str, **kwargs):
    stub.reset_calls()

    return run_script(WRAPPER, deploy_dir, *args, stub=stub, **kwargs)


def _refused(deploy_dir: Path, stub: DockerStub, message: str, *args: str) -> str:
    result = _wrapper(deploy_dir, stub, *(args or UPGRADE))

    assert result.returncode == 2, (result.stdout, result.stderr)
    assert message in result.stderr, result.stderr
    assert stub.compose_calls() == []

    return result.stderr


def _evidence(deploy_dir: Path) -> dict[str, str]:
    return dict(
        line.split("=", 1) for line in (deploy_dir / EVIDENCE).read_text().splitlines()
    )


def test_a_complete_evidence_lets_the_upgrade_run(deploy_dir, stub) -> None:
    result = _wrapper(deploy_dir, stub, *UPGRADE)

    assert result.returncode == 0, result.stderr
    assert "gate backup: PASS" in result.stdout
    assert "operator attestation" in result.stdout
    assert len(stub.compose_calls()) == 1


# -- missing ------------------------------------------------------------------------


def test_no_evidence_refuses_the_upgrade(deploy_dir, stub) -> None:
    (deploy_dir / EVIDENCE).unlink()

    _refused(deploy_dir, stub, "no backup evidence")


def test_no_offhost_confirmation_refuses_the_upgrade(deploy_dir, stub) -> None:
    (deploy_dir / ACK).unlink()

    _refused(deploy_dir, stub, "no off-host copy confirmation")


def test_a_marker_file_is_not_evidence(deploy_dir, stub) -> None:
    (deploy_dir / EVIDENCE).write_text("VERIFIED=true\n")

    _refused(deploy_dir, stub, "must define exactly")


# -- corrupted ----------------------------------------------------------------------


def _edit_line(deploy_dir: Path, key: str, value: str) -> None:
    path = deploy_dir / EVIDENCE
    path.write_text(re.sub(rf"(?m)^{key}=.*$", f"{key}={value}", path.read_text()))


CORRUPTED = {
    # A hand edit that keeps the old self-checksum.
    "revision-edited": (
        lambda d, k: _edit_line(d, "SMM_CC_EVIDENCE_REVISION", "3d4ac1986f07"),
        "checksum does not match",
    ),
    "verified-at-edited": (
        lambda d, k: _edit_line(d, "SMM_CC_EVIDENCE_VERIFIED_AT", FIXTURE_ACK_AT),
        "checksum does not match",
    ),
    "checksum-edited": (
        lambda d, k: _edit_line(d, "SMM_CC_EVIDENCE_CHECKSUM", "0" * 64),
        "checksum does not match",
    ),
    "truncated": (
        lambda d, k: (d / EVIDENCE).write_text(
            "".join((d / EVIDENCE).read_text().splitlines(True)[:-2])
        ),
        "must define exactly",
    ),
    "comment-added": (
        lambda d, k: (d / EVIDENCE).write_text(
            "# looks fine\n" + (d / EVIDENCE).read_text()
        ),
        "unexpected layout",
    ),
    "checksum-not-last": (
        lambda d, k: (d / EVIDENCE).write_text(
            "".join(sorted((d / EVIDENCE).read_text().splitlines(True)))
        ),
        "unexpected layout",
    ),
    "empty": (lambda d, k: (d / EVIDENCE).write_text(""), "must define exactly"),
    "symlink": (
        lambda d, k: (
            (d / "elsewhere").write_text((d / EVIDENCE).read_text()),
            (d / EVIDENCE).unlink(),
            (d / EVIDENCE).symlink_to(d / "elsewhere"),
        ),
        "is a symlink",
    ),
    "malformed-path": (
        lambda d, k: write_evidence(d, k, SMM_CC_EVIDENCE_DUMP="relative/x.dump"),
        "malformed",
    ),
    "malformed-time": (
        lambda d, k: write_evidence(
            d, k, SMM_CC_EVIDENCE_TAKEN_AT="2026-10-11 10:20:00+00"
        ),
        "malformed",
    ),
}


@pytest.mark.parametrize("case", sorted(CORRUPTED))
def test_a_corrupted_evidence_refuses_the_upgrade(
    deploy_dir, stub, kept, case: str
) -> None:
    breaks, message = CORRUPTED[case]
    breaks(deploy_dir, kept)

    _refused(deploy_dir, stub, message)


DUMP_CHANGED = {
    "dump-modified": (
        lambda k: k.write_bytes(k.read_bytes() + b"\x00"),
        "no longer matches",
    ),
    "dump-deleted": (lambda k: k.unlink(), "missing or not a regular file"),
    "dump-is-a-symlink": (
        lambda k: (
            (k.parent / "real").write_bytes(k.read_bytes()),
            k.unlink(),
            k.symlink_to(k.parent / "real"),
        ),
        "missing or not a regular file",
    ),
    "sha256-sidecar-changed": (
        lambda k: Path(f"{k}.sha256").write_text(f"{'0' * 64}  {k.name}\n"),
        ".sha256 does not match",
    ),
}


@pytest.mark.parametrize("case", sorted(DUMP_CHANGED))
def test_a_changed_dump_refuses_the_upgrade(deploy_dir, stub, kept, case) -> None:
    breaks, message = DUMP_CHANGED[case]
    breaks(kept)

    _refused(deploy_dir, stub, message)


# -- substituted --------------------------------------------------------------------


def test_evidence_pointed_at_another_dump_is_refused(deploy_dir, stub, kept) -> None:
    other = write_dump(kept.parent, b"PGDMP other archive", name="other.dump")
    # Re-pointed by hand, the self-checksum recomputed: the sha256 is the
    # original dump's, the file is another.
    write_evidence(
        deploy_dir,
        kept,
        SMM_CC_EVIDENCE_DUMP=str(other.resolve()),
    )

    _refused(deploy_dir, stub, "no longer matches")


def test_an_offhost_confirmation_for_another_dump_is_refused(
    deploy_dir, stub, kept
) -> None:
    write_offhost_ack(deploy_dir, hashlib.sha256(b"another archive").hexdigest())

    _refused(deploy_dir, stub, "for another dump")


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("SMM_CC_EVIDENCE_SYSTEM_IDENTIFIER", "7999999999999999999"),
        ("SMM_CC_EVIDENCE_DATABASE_OID", "99999"),
        ("SMM_CC_EVIDENCE_PG_CONTAINER_ID", "e" * 64),
    ],
    ids=["other-instance", "other-database", "other-container"],
)
def test_evidence_from_another_instance_or_database_is_refused(
    deploy_dir, stub, kept, key: str, value: str
) -> None:
    write_evidence(deploy_dir, kept, **{key: value})

    _refused(deploy_dir, stub, "belongs to another PostgreSQL instance")


# -- stale --------------------------------------------------------------------------


def test_evidence_after_the_schema_moved_is_refused(deploy_dir, stub) -> None:
    # The upgrade already ran (or someone migrated by other means).
    stub.update(revision="3d4ac1986f07")

    _refused(deploy_dir, stub, "stale, take a new dump")


@pytest.mark.parametrize(
    "overrides",
    [
        {
            "SMM_CC_EVIDENCE_TAKEN_AT": "2026-10-10T10:20:00Z",
            "SMM_CC_EVIDENCE_VERIFIED_AT": "2026-10-10T10:25:00Z",
        },
        {"SMM_CC_EVIDENCE_TAKEN_AT": "2026-10-11T09:59:59Z"},
        {"SMM_CC_EVIDENCE_VERIFIED_AT": "2026-10-11T12:00:01Z"},
        {"SMM_CC_EVIDENCE_VERIFIED_AT": "2026-10-11T10:19:59Z"},
    ],
    ids=["previous-day", "before-window", "after-window", "verified-before-taken"],
)
def test_evidence_from_another_window_is_refused(
    deploy_dir, stub, kept, overrides: dict
) -> None:
    write_evidence(deploy_dir, kept, **overrides)

    _refused(deploy_dir, stub, "is not from the window")


@pytest.mark.parametrize(
    ("at", "message"),
    [
        ("2026-10-11T10:24:59Z", "before the restore check"),
        ("2026-10-11T12:00:01Z", "after the window"),
    ],
    ids=["before-verify", "after-window"],
)
def test_an_offhost_confirmation_out_of_order_is_refused(
    deploy_dir, stub, kept, at: str, message: str
) -> None:
    write_offhost_ack(deploy_dir, sha256_of(kept), at=at)

    _refused(deploy_dir, stub, message)


@pytest.mark.parametrize(
    "text",
    [
        "SMM_CC_OFFHOST_DUMP_SHA256=x\nSMM_CC_OFFHOST_CONFIRMED_AT=x\n",
        "SMM_CC_OFFHOST_DUMP_SHA256={sha}\n"
        "SMM_CC_OFFHOST_CONFIRMED_AT=2026-10-11 10:40\n"
        "SMM_CC_OFFHOST_CONFIRMED_BY=owner\n",
        "SMM_CC_OFFHOST_DUMP_SHA256={sha}\n"
        "SMM_CC_OFFHOST_CONFIRMED_AT=2026-10-11T10:40:00Z\n"
        "SMM_CC_OFFHOST_CONFIRMED_BY=\n",
        "VERIFIED=true\n",
    ],
    ids=["missing-key", "local-time", "nobody", "marker"],
)
def test_a_malformed_offhost_confirmation_is_refused(
    deploy_dir, stub, kept, text: str
) -> None:
    (deploy_dir / ACK).write_text(text.format(sha=sha256_of(kept)))

    result = _wrapper(deploy_dir, stub, *UPGRADE)

    assert result.returncode == 2
    assert re.search("malformed|must define exactly", result.stderr), result.stderr
    assert stub.compose_calls() == []


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"revision": "79731eadacd7\n79731eadacd7"}, "ambiguous alembic revision"),
        ({"revision": "head"}, "ambiguous alembic revision"),
        ({"revision": ""}, "ambiguous alembic revision"),
    ],
    ids=["two-rows", "not-a-revision", "empty"],
)
def test_an_unreadable_live_revision_refuses_the_upgrade(
    deploy_dir, stub, changes: dict, message: str
) -> None:
    stub.update(**changes)

    _refused(deploy_dir, stub, message)


# -- no way around it through the wrapper -------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        (*UPGRADE, "--no-evidence"),
        (*UPGRADE, "--force"),
        ("migrate", "upgrade", "--skip-backup", "3d4ac1986f07"),
        ("migrate", "--evidence=/dev/null", "upgrade", "3d4ac1986f07"),
        ("migrate", "stamp", "3d4ac1986f07"),
        ("run", "--rm", "migrate", "upgrade", "3d4ac1986f07"),
    ],
    ids=["no-evidence", "force", "skip-backup", "evidence-option", "stamp", "raw-run"],
)
def test_no_option_skips_the_evidence(deploy_dir, stub, args) -> None:
    (deploy_dir / EVIDENCE).unlink()

    result = _wrapper(deploy_dir, stub, *args)

    assert result.returncode == 2
    assert stub.calls() == [], "refused before Docker"


def test_environment_cannot_supply_or_waive_the_evidence(deploy_dir, stub) -> None:
    (deploy_dir / EVIDENCE).unlink()

    result = _wrapper(
        deploy_dir,
        stub,
        *UPGRADE,
        extra_env={
            "SMM_CC_EVIDENCE_DUMP": "/dev/null",
            "SMM_CC_SKIP_BACKUP_EVIDENCE": "1",
            "CC_EVIDENCE_FILE_NAME": "control-center.identity",
            "gate_backup": "false",
        },
    )

    assert result.returncode == 2
    assert "no backup evidence" in result.stderr
    assert stub.compose_calls() == []


def test_another_deployment_directory_without_evidence_is_refused(
    deploy_dir, stub, tmp_path: Path
) -> None:
    # SMM_CC_DIR at a copy that has everything but the evidence.
    copy = tmp_path / "copy"
    shutil.copytree(deploy_dir, copy, symlinks=True)
    (copy / EVIDENCE).unlink()

    result = _wrapper(copy, stub, *UPGRADE)

    assert result.returncode == 2
    assert "no backup evidence" in result.stderr
    assert stub.compose_calls() == []


# -- downgrade ----------------------------------------------------------------------


def test_downgrade_does_not_need_the_evidence(deploy_dir, stub) -> None:
    (deploy_dir / EVIDENCE).unlink()
    (deploy_dir / ACK).unlink()

    result = _wrapper(deploy_dir, stub, *DOWNGRADE)

    assert result.returncode == 0, result.stderr
    assert len(stub.compose_calls()) == 1


def test_downgrade_ignores_and_voids_stale_evidence(deploy_dir, stub) -> None:
    # The upgrade ran: the evidence is stale, the downgrade must not care.
    stub.update(revision="3d4ac1986f07")

    result = _wrapper(deploy_dir, stub, *DOWNGRADE)

    assert result.returncode == 0, result.stderr
    assert "backup evidence invalidated" in result.stdout
    assert not (deploy_dir / EVIDENCE).exists()
    assert list(deploy_dir.glob(f"{EVIDENCE}.invalidated-*"))

    # Back at the old revision, the old evidence cannot authorise a new
    # upgrade: the data moved on while the schema went up and down.
    stub.update(revision="79731eadacd7")
    _refused(deploy_dir, stub, "no backup evidence")


def test_a_downgrade_refused_by_a_gate_leaves_the_evidence(deploy_dir, stub) -> None:
    stub.update(identity_rc=2)

    _refused(deploy_dir, stub, "cannot query", *DOWNGRADE)

    assert (deploy_dir / EVIDENCE).exists()


# -- verify is the only writer ------------------------------------------------------


UTC = timezone.utc


def _utc(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def fresh(tmp_path: Path) -> Iterator[Path]:
    """A deployment directory with no evidence yet and a window around now."""

    with docker_socket() as host:
        directory = write_deploy_dir(
            tmp_path / "fresh" / "ai-smm-control-center",
            docker_host=host,
            evidence=False,
        )
        now = datetime.now(UTC)
        write_window(
            directory,
            _utc(now - timedelta(minutes=10)),
            _utc(now + timedelta(minutes=110)),
        )
        yield directory


@pytest.fixture
def backups(tmp_path: Path) -> Path:
    directory = tmp_path / "backups"
    directory.mkdir(mode=0o700)

    return directory


def _dump(directory: Path, stub: DockerStub, backups: Path) -> Path:
    result = run_script(
        BACKUP,
        directory,
        "dump",
        "--container",
        FIXTURE_PG_CONTAINER,
        "--out-dir",
        str(backups),
        stub=stub,
    )

    assert result.returncode == 0, result.stderr

    return next(backups.glob("*.dump"))


def _verify(directory: Path, stub: DockerStub, dump: Path):
    return run_script(
        BACKUP,
        directory,
        "verify",
        "--container",
        FIXTURE_PG_CONTAINER,
        "--dump",
        str(dump),
        stub=stub,
    )


def test_dump_verify_confirm_then_upgrade(fresh, stub, backups) -> None:
    dump = _dump(fresh, stub, backups)

    assert not (fresh / EVIDENCE).exists()
    _refused(fresh, stub, "no backup evidence")

    result = _verify(fresh, stub, dump)

    assert result.returncode == 0, result.stderr
    assert "evidence:" in result.stdout
    assert (fresh / EVIDENCE).stat().st_mode & 0o777 == 0o600

    evidence = _evidence(fresh)
    body = "".join((fresh / EVIDENCE).read_text().splitlines(True)[:-1])

    assert evidence["SMM_CC_EVIDENCE_DUMP"] == str(dump.resolve())
    assert evidence["SMM_CC_EVIDENCE_DUMP_SHA256"] == sha256_of(dump)
    assert evidence["SMM_CC_EVIDENCE_PG_CONTAINER_ID"] == FIXTURE_PG_ID
    assert evidence["SMM_CC_EVIDENCE_SYSTEM_IDENTIFIER"] == FIXTURE_SYSTEM_ID
    assert evidence["SMM_CC_EVIDENCE_DATABASE_OID"] == FIXTURE_DB_OID
    assert evidence["SMM_CC_EVIDENCE_REVISION"] == "79731eadacd7"
    assert (
        evidence["SMM_CC_EVIDENCE_CHECKSUM"]
        == hashlib.sha256(body.encode()).hexdigest()
    )

    # Verified, but no off-host copy confirmed yet.
    _refused(fresh, stub, "no off-host copy confirmation")

    write_offhost_ack(fresh, sha256_of(dump), at=_utc(datetime.now(UTC)))
    result = _wrapper(fresh, stub, *UPGRADE)

    assert result.returncode == 0, result.stderr
    assert len(stub.compose_calls()) == 1


def _source_edit(dump: Path, key: str, value: str) -> None:
    path = Path(f"{dump}.source")
    path.write_text(re.sub(rf"(?m)^{key}=.*$", f"{key}={value}", path.read_text()))


FAILED_VERIFY = {
    "restore-fails": (lambda s, d: s.update(restore_rc=1), "pg_restore failed"),
    "checksum-mismatch": (
        lambda s, d: d.write_bytes(d.read_bytes() + b"x"),
        "checksum mismatch",
    ),
    "no-source-record": (
        lambda s, d: Path(f"{d}.source").unlink(),
        "only a dump taken by this script",
    ),
    "source-for-another-archive": (
        lambda s, d: _source_edit(d, "SMM_CC_SOURCE_SHA256", "0" * 64),
        "describes another archive",
    ),
    "source-from-another-instance": (
        lambda s, d: _source_edit(d, "SMM_CC_SOURCE_SYSTEM_IDENTIFIER", "71"),
        "another instance",
    ),
    "source-revision-differs-from-archive": (
        lambda s, d: s.update(restored_revision="2964ac9ceea7"),
        "its source record says",
    ),
    "database-moved-since-the-dump": (
        lambda s, d: s.update(
            revision="3d4ac1986f07", restored_revision="79731eadacd7"
        ),
        "stale, take a new dump",
    ),
    "identity-gate-closed": (lambda s, d: s.update(identity_rc=2), "cannot query"),
}


@pytest.mark.parametrize("case", sorted(FAILED_VERIFY))
def test_a_failed_verify_leaves_no_evidence_and_voids_the_old_one(
    fresh, stub, backups, case: str
) -> None:
    dump = _dump(fresh, stub, backups)
    # An earlier, otherwise valid evidence for the same dump.
    (fresh / EVIDENCE).write_text(
        evidence_text(
            dump,
            SMM_CC_EVIDENCE_TAKEN_AT=FIXTURE_TAKEN_AT,
            SMM_CC_EVIDENCE_VERIFIED_AT=FIXTURE_VERIFIED_AT,
        )
    )
    breaks, message = FAILED_VERIFY[case]
    breaks(stub, dump)

    result = _verify(fresh, stub, dump)

    assert result.returncode == 2, result.stdout
    assert message in result.stderr, result.stderr
    assert not (fresh / EVIDENCE).exists()
    assert not list(fresh.glob(f"{EVIDENCE}.partial"))
