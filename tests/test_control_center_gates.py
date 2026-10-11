"""deploy/control-center/cc-gate.sh: the PostgreSQL identity gate and the
publication-queue gate fail CLOSED, and a closed gate stops the operation
it guards.

Guarded operations:

  identity + queue   cc-compose.sh preflight / migrate upgrade / migrate
                     downgrade, cc-db-backup.sh dump
  identity           cc-compose.sh create-owner, cc-db-backup.sh verify

Each refusal must exit non-zero (2), name the reason, leave no trace --
no Compose call, no pg_dump, no pg_restore, no CREATE/DROP, no file in the
backup directory -- and never print the database password.

Most cases run against the fake `docker` (tests/cc_stub.py). The queue
SQL itself is also run against a real, disposable PostgreSQL
(AI_SMM_TEST_DATABASE_URL) with publications in each state, so the window
and margin arithmetic is tested on the database, not on canned answers.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from ai_smm.db.models import PublicationStatus
from tests.cc_stub import (
    BACKUP,
    FIXTURE_DSN,
    FIXTURE_NETWORK,
    FIXTURE_OTHER_ID,
    FIXTURE_PG_CONTAINER,
    FIXTURE_PG_ID,
    FIXTURE_SYSTEM_ID,
    FIXTURE_WORKER,
    IDENTITY_ANSWER,
    QUEUE_QUIET,
    WRAPPER,
    DockerStub,
    docker_socket,
    run_script,
    write_deploy_dir,
    write_dump,
    write_identity,
    write_window,
)


pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash not available"
)

PASSWORD = "fixture-pw"
ALIAS_DSN = FIXTURE_DSN.replace("@fixture-db", "@postgres")


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


@pytest.fixture
def dump(tmp_path: Path) -> Path:
    directory = tmp_path / "kept"
    directory.mkdir(mode=0o700)

    return write_dump(directory)


# -- the guarded operations ------------------------------------------------------

QUEUE_OPS = {
    "preflight": ("wrapper", ("preflight",)),
    "migrate-upgrade": ("wrapper", ("migrate", "upgrade", "3d4ac1986f07")),
    "migrate-downgrade": ("wrapper", ("migrate", "downgrade", "79731eadacd7")),
    "backup-dump": ("dump", ()),
}
IDENTITY_ONLY_OPS = {
    "up": ("wrapper", ("up",)),
    "restart": ("wrapper", ("restart", "smm-api")),
    "create-owner": (
        "wrapper",
        (
            "create-owner",
            "--email",
            "o@example.test",
            "--display-name",
            "O",
            "--project",
            "demo",
        ),
    ),
    "backup-verify": ("verify", ()),
}
IDENTITY_OPS = {**QUEUE_OPS, **IDENTITY_ONLY_OPS}


def _run(op, deploy_dir: Path, stub: DockerStub, backups: Path, dump: Path):
    kind, args = op
    stub.reset_calls()

    if kind == "wrapper":
        return run_script(WRAPPER, deploy_dir, *args, stub=stub, stdin="pw\npw\n")
    if kind == "dump":
        return run_script(
            BACKUP,
            deploy_dir,
            "dump",
            "--container",
            FIXTURE_PG_CONTAINER,
            "--out-dir",
            str(backups),
            stub=stub,
        )

    return run_script(
        BACKUP,
        deploy_dir,
        "verify",
        "--container",
        FIXTURE_PG_CONTAINER,
        "--dump",
        str(dump),
        stub=stub,
    )


def _side_effects(stub: DockerStub) -> list[list[str]]:
    """Every docker call that is more than a read."""

    effects = []

    for call in stub.calls():
        argv = call["argv"]

        dumps = argv[:1] == ["exec"] and ("pg_dump" in argv or "pg_restore" in argv)

        if argv[:1] == ["compose"] or dumps:
            effects.append(argv)
        elif argv[:1] == ["exec"] and "psql" in argv:
            sql = argv[argv.index("-c") + 1]
            read_only = any("default_transaction_read_only=on" in a for a in argv)

            if not read_only or sql.lstrip().upper().startswith(("CREATE", "DROP")):
                effects.append(argv)

    return effects


def _closed(op, deploy_dir, stub, backups, dump, message: str | tuple[str, ...]) -> str:
    result = _run(op, deploy_dir, stub, backups, dump)
    output = result.stdout + result.stderr
    messages = (message,) if isinstance(message, str) else message

    assert result.returncode == 2, (result.returncode, output)
    assert any(m in result.stderr for m in messages), result.stderr
    assert _side_effects(stub) == [], _side_effects(stub)
    assert list(backups.iterdir()) == []
    assert PASSWORD not in output
    assert "preflight: PASS" not in result.stdout

    return result.stderr


def test_every_guarded_operation_passes_when_everything_matches(
    deploy_dir: Path, stub: DockerStub, backups: Path, dump: Path
) -> None:
    for name, op in IDENTITY_OPS.items():
        result = _run(op, deploy_dir, stub, backups, dump)

        assert result.returncode == 0, (name, result.stderr)
        assert "gate identity: PASS" in result.stdout, name
        assert ("gate queue: PASS" in result.stdout) == (name in QUEUE_OPS), name
        assert PASSWORD not in result.stdout + result.stderr


def test_the_gates_run_read_only_and_before_the_guarded_step(
    deploy_dir: Path, stub: DockerStub, backups: Path, dump: Path
) -> None:
    for name, op in QUEUE_OPS.items():
        _run(op, deploy_dir, stub, backups, dump)
        argvs = [c["argv"] for c in stub.calls()]
        sqls = [
            (i, a)
            for i, a in enumerate(argvs)
            if a[:1] == ["exec"]
            and "psql" in a
            and (
                "pg_control_system" in a[a.index("-c") + 1]
                or "'queue'," in a[a.index("-c") + 1]
            )
        ]

        assert len(sqls) == 2, name
        for _, argv in sqls:
            assert "PGOPTIONS=-c default_transaction_read_only=on" in argv, name
            assert argv[argv.index("exec") + 1 :].count(FIXTURE_PG_CONTAINER) == 1

        guarded = [
            i
            for i, a in enumerate(argvs)
            if a[:1] == ["compose"] or (a[:1] == ["exec"] and "pg_dump" in a)
        ]

        if name == "preflight":
            assert guarded == []
        else:
            assert len(guarded) == 1 and guarded[0] > sqls[-1][0], name


def test_preflight_reports_pass_and_runs_nothing(
    deploy_dir: Path, stub: DockerStub
) -> None:
    result = run_script(WRAPPER, deploy_dir, "preflight", stub=stub)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("preflight: PASS")
    assert stub.compose_calls() == []
    assert (
        run_script(
            WRAPPER, deploy_dir, "preflight", "--window", "x", stub=stub
        ).returncode
        == 2
    )


def test_read_only_verbs_need_no_gate(deploy_dir: Path, stub: DockerStub) -> None:
    # Stopping or inspecting the Control Center must work even when the
    # database is gone -- that is when a rollback needs them.
    stub.update(identity_rc=2, queue_rc=1)
    (deploy_dir / "control-center.window").unlink()

    for args in (
        ("down",),
        ("ps", "-a"),
        ("logs", "--since", "5m"),
        ("migrate", "current"),
        ("config", "--quiet"),
    ):
        result = run_script(WRAPPER, deploy_dir, *args, stub=stub)

        assert result.returncode == 0, (args, result.stderr)


# -- identity: wrong PostgreSQL ---------------------------------------------------


def _set_container(stub: DockerStub, name: str, **fields) -> None:
    stub.state["containers"][name].update(fields)
    stub.save()


def _net(stub: DockerStub, name: str) -> dict:
    return stub.state["containers"][name]["networks"][FIXTURE_NETWORK]


def _wrong_container_id(stub, deploy_dir):
    _set_container(stub, FIXTURE_PG_CONTAINER, id="e" * 64)


def _other_instance(stub, deploy_dir):
    stub.update(
        identity_answer=IDENTITY_ANSWER.replace(
            FIXTURE_SYSTEM_ID, "7111111111111111111"
        )
    )


def _other_database(stub, deploy_dir):
    stub.update(identity_answer=IDENTITY_ANSWER.replace("|16384|", "|99999|"))


def _host_resolves_twice(stub, deploy_dir):
    _net(stub, "fixture-other")["aliases"] = [FIXTURE_PG_CONTAINER]
    stub.save()


def _host_resolves_to_another_container(stub, deploy_dir):
    del stub.state["containers"][FIXTURE_PG_CONTAINER]["networks"][FIXTURE_NETWORK]
    _net(stub, "fixture-other")["aliases"] = [FIXTURE_PG_CONTAINER.upper()]
    stub.save()


def _host_resolves_to_nothing(stub, deploy_dir):
    del stub.state["containers"][FIXTURE_PG_CONTAINER]["networks"][FIXTURE_NETWORK]
    stub.save()


def _database_stopped(stub, deploy_dir):
    _set_container(stub, FIXTURE_PG_CONTAINER, running=False)


def _port_differs(stub, deploy_dir):
    stub.update(identity_answer=IDENTITY_ANSWER.replace("|5432|", "|5433|"))


def _identity_records_another_container(stub, deploy_dir):
    write_identity(
        deploy_dir,
        SMM_CC_PG_CONTAINER="fixture-other",
        SMM_CC_PG_CONTAINER_ID=FIXTURE_OTHER_ID,
    )


WRONG_POSTGRES = {
    "container-replaced": (_wrong_container_id, "not the recorded"),
    "other-instance": (_other_instance, "another instance"),
    "other-database-oid": (_other_database, "another database"),
    "host-ambiguous-on-network": (_host_resolves_twice, "resolves to 2 containers"),
    "host-is-another-container": (
        _host_resolves_to_another_container,
        "not the recorded",
    ),
    "host-not-on-network": (_host_resolves_to_nothing, "resolves to 0 containers"),
    "database-stopped": (_database_stopped, "is not running"),
    "port-differs": (_port_differs, "listens on 5433"),
    # The backup refuses it one step earlier: --container is not the record.
    "dsn-host-not-recorded-container": (
        _identity_records_another_container,
        ("points at host", "is not the recorded database container"),
    ),
}


@pytest.mark.parametrize("case", sorted(WRONG_POSTGRES))
@pytest.mark.parametrize("op", sorted(IDENTITY_OPS))
def test_a_wrong_postgresql_closes_the_gate(
    deploy_dir, stub, backups, dump, op: str, case: str
) -> None:
    breaks, message = WRONG_POSTGRES[case]
    breaks(stub, deploy_dir)

    _closed(IDENTITY_OPS[op], deploy_dir, stub, backups, dump, message)


def test_backup_refuses_a_container_other_than_the_recorded_one(
    deploy_dir, stub, backups
) -> None:
    result = run_script(
        BACKUP,
        deploy_dir,
        "dump",
        "--container",
        "fixture-other",
        "--out-dir",
        str(backups),
        stub=stub,
    )

    assert result.returncode == 2
    assert "is not the recorded database container" in result.stderr
    assert _side_effects(stub) == []
    # Refused before any query reached any container.
    assert not [c for c in stub.calls() if c["argv"][:1] == ["exec"]]


# -- identity: mismatched database URL ----------------------------------------------


def _worker_env(stub: DockerStub, *env: str) -> None:
    _set_container(stub, FIXTURE_WORKER, env=list(env))


def _set_dsn(deploy_dir: Path, dsn: str) -> None:
    secret = deploy_dir / "control-center.env"
    secret.write_text(f"AI_SMM_DATABASE_URL={dsn}\n")
    secret.chmod(0o600)


MISMATCHED_DSN = {
    "worker-other-password": (
        lambda s, d: _worker_env(
            s, f"AI_SMM_DATABASE_URL={FIXTURE_DSN.replace(PASSWORD, 'other-pw')}"
        ),
        "differs from the running worker's",
    ),
    "worker-other-host": (
        lambda s, d: _worker_env(s, "AI_SMM_DATABASE_URL=" + ALIAS_DSN),
        "differs from the running worker's",
    ),
    "worker-no-url": (lambda s, d: _worker_env(s, "AI_SMM_DRY_RUN=false"), "0 times"),
    "worker-two-urls": (
        lambda s, d: _worker_env(
            s,
            f"AI_SMM_DATABASE_URL={FIXTURE_DSN}",
            f"AI_SMM_DATABASE_URL={FIXTURE_DSN}",
        ),
        "2 times",
    ),
    "cc-alias-host": (
        # Both say "postgres": the same URL, but not the recorded container
        # name -- an alias another container could also claim.
        lambda s, d: (
            _set_dsn(d, ALIAS_DSN),
            _worker_env(s, "AI_SMM_DATABASE_URL=" + ALIAS_DSN),
        ),
        "points at host 'postgres'",
    ),
    "cc-other-database": (
        lambda s, d: _set_dsn(d, FIXTURE_DSN.replace("/ai_smm", "/finance")),
        "names database 'finance'",
    ),
    "cc-second-at-sign": (
        lambda s, d: _set_dsn(d, FIXTURE_DSN.replace(PASSWORD, "pw@evil-db:5432/x?")),
        "cannot be read unambiguously",
    ),
    "cc-query-overrides-host": (
        lambda s, d: _set_dsn(d, FIXTURE_DSN + "?host=evil-db"),
        "cannot be read unambiguously",
    ),
    "cc-no-database": (
        lambda s, d: _set_dsn(d, FIXTURE_DSN.rsplit("/", 1)[0]),
        "cannot be read unambiguously",
    ),
    "worker-not-running": (
        lambda s, d: _set_container(s, FIXTURE_WORKER, running=False),
        "is not running",
    ),
    "worker-not-on-network": (
        lambda s, d: (
            s.state["containers"][FIXTURE_WORKER]["networks"].clear(),
            s.save(),
        ),
        "is not attached to",
    ),
}


@pytest.mark.parametrize("case", sorted(MISMATCHED_DSN))
@pytest.mark.parametrize("op", sorted(IDENTITY_OPS))
def test_a_mismatched_database_url_closes_the_gate(
    deploy_dir, stub, backups, dump, op: str, case: str
) -> None:
    breaks, message = MISMATCHED_DSN[case]
    breaks(stub, deploy_dir)

    stderr = _closed(IDENTITY_OPS[op], deploy_dir, stub, backups, dump, message)

    assert "other-pw" not in stderr and "evil" not in stderr


# -- identity: unreachable, failing or ambiguous answers ----------------------------

UNREADABLE = {
    "database-unreachable": ({"identity_rc": 2}, "cannot query ai_smm"),
    "identity-two-rows": (
        {"identity_answer": IDENTITY_ANSWER + "\n" + IDENTITY_ANSWER},
        "ambiguous identity answer",
    ),
    "identity-garbage": (
        {"identity_answer": "psql: warning"},
        "ambiguous identity answer",
    ),
    "identity-empty": ({"identity_answer": ""}, "ambiguous identity answer"),
    "identity-missing-field": (
        {"identity_answer": f"identity|{FIXTURE_SYSTEM_ID}|16384|5432"},
        "ambiguous identity answer",
    ),
    "network-inspect-fails": (
        {"network_inspect_fails": [FIXTURE_NETWORK]},
        "cannot list the containers",
    ),
    "network-lists-garbage": (
        {"network_extra": "not-a-container-id\n"},
        "unexpected container id",
    ),
}


@pytest.mark.parametrize("case", sorted(UNREADABLE))
@pytest.mark.parametrize("op", sorted(IDENTITY_OPS))
def test_an_unreadable_identity_closes_the_gate(
    deploy_dir, stub, backups, dump, op: str, case: str
) -> None:
    changes, message = UNREADABLE[case]
    stub.update(**changes)

    _closed(IDENTITY_OPS[op], deploy_dir, stub, backups, dump, message)


@pytest.mark.parametrize(
    ("container", "fields", "message"),
    [
        (FIXTURE_PG_CONTAINER, {"inspect_fails": True}, "not found on this daemon"),
        (FIXTURE_WORKER, {"inspect_fails": True}, "not found on this daemon"),
        (FIXTURE_PG_CONTAINER, {"state_answer": "running"}, "unexpected answer"),
        (FIXTURE_PG_CONTAINER, {"env": ["POSTGRES_DB=x"]}, "POSTGRES_USER 0 times"),
        (
            FIXTURE_PG_CONTAINER,
            {"env": ["POSTGRES_USER=a", "POSTGRES_USER=b"]},
            "POSTGRES_USER 2 times",
        ),
    ],
    ids=[
        "pg-inspect-fails",
        "worker-inspect-fails",
        "state-ambiguous",
        "no-superuser",
        "two-superusers",
    ],
)
@pytest.mark.parametrize("op", sorted(IDENTITY_OPS))
def test_an_unreadable_container_closes_the_gate(
    deploy_dir,
    stub,
    backups,
    dump,
    op: str,
    container: str,
    fields: dict,
    message: str,
) -> None:
    _set_container(stub, container, **fields)

    _closed(IDENTITY_OPS[op], deploy_dir, stub, backups, dump, message)


def test_a_missing_worker_container_closes_the_gate(
    deploy_dir, stub, backups, dump
) -> None:
    del stub.state["containers"][FIXTURE_WORKER]
    stub.save()

    for op in IDENTITY_OPS.values():
        _closed(op, deploy_dir, stub, backups, dump, "not found on this daemon")


IDENTITY_FILE_REFUSALS = {
    "missing": (lambda d: (d / "control-center.identity").unlink(), "missing"),
    "extra-key": (
        lambda d: (d / "control-center.identity").write_text(
            (d / "control-center.identity").read_text() + "SMM_CC_PG_HOST=x\n"
        ),
        "must define exactly",
    ),
    "short-container-id": (
        lambda d: write_identity(d, SMM_CC_PG_CONTAINER_ID=FIXTURE_PG_ID[:12]),
        "full 64-hex",
    ),
    "system-id-not-a-number": (
        lambda d: write_identity(d, SMM_CC_PG_SYSTEM_IDENTIFIER="unknown"),
        "not a system identifier",
    ),
    "oid-empty": (lambda d: write_identity(d, SMM_CC_PG_DATABASE_OID=""), "not an oid"),
    "worker-is-the-database": (
        lambda d: write_identity(d, SMM_CC_WORKER_CONTAINER=FIXTURE_PG_CONTAINER),
        "same container",
    ),
    "symlink": (
        lambda d: (
            (d / "elsewhere").write_text((d / "control-center.identity").read_text()),
            (d / "control-center.identity").unlink(),
            (d / "control-center.identity").symlink_to(d / "elsewhere"),
        ),
        "is a symlink",
    ),
}


@pytest.mark.parametrize("case", sorted(IDENTITY_FILE_REFUSALS))
@pytest.mark.parametrize("op", sorted(IDENTITY_OPS))
def test_the_recorded_identity_must_be_complete_and_well_formed(
    deploy_dir, stub, backups, dump, op: str, case: str
) -> None:
    breaks, message = IDENTITY_FILE_REFUSALS[case]
    breaks(deploy_dir)

    result = _run(IDENTITY_OPS[op], deploy_dir, stub, backups, dump)

    assert result.returncode == 2
    assert message in result.stderr, result.stderr
    assert _side_effects(stub) == []
    # Refused before anything was asked of the database.
    assert not [c for c in stub.calls() if c["argv"][:1] == ["exec"]]


# -- queue: busy, outside the window, failing or ambiguous ---------------------------


def _queue(**fields: str) -> str:
    keys = [
        "in_window",
        "length_ok",
        "publishing",
        "lease",
        "due",
        "retry",
        "published",
        "attempts",
        "review",
        "now",
    ]
    values = dict(zip(keys, QUEUE_QUIET.split("|")[1:], strict=True))
    values.update(fields)

    return "queue|" + "|".join(values[k] for k in keys)


BUSY_QUEUE = {
    "publishing": (_queue(publishing="1"), "publishing=1"),
    "active-lease": (_queue(lease="1"), "active-lease=1"),
    "due-in-window": (_queue(due="2"), "due-in-window=2"),
    "retry-in-window": (_queue(due="1", retry="1"), "retry-in-window=1"),
    "published-in-margin": (_queue(published="1"), "published-in-margin=1"),
    "attempt-unfinished": (_queue(attempts="1"), "attempts-active-or-in-margin=1"),
    "needs-review": (_queue(review="1"), "needs-review=1"),
    "outside-window": (_queue(in_window="0"), "outside the agreed window"),
    "window-too-long": (_queue(length_ok="0"), "longer than 12 hours"),
}


@pytest.mark.parametrize("case", sorted(BUSY_QUEUE))
@pytest.mark.parametrize("op", sorted(QUEUE_OPS))
def test_a_busy_queue_closes_the_gate(
    deploy_dir, stub, backups, dump, op: str, case: str
) -> None:
    answer, message = BUSY_QUEUE[case]
    stub.update(queue_answer=answer, queue_rows="7|scheduled|2026-10-11 10:45:00+00|||")

    stderr = _closed(QUEUE_OPS[op], deploy_dir, stub, backups, dump, message)

    assert "GATE CLOSED" in stderr


def test_a_busy_queue_lists_the_rows_without_their_content(
    deploy_dir, stub, backups, dump
) -> None:
    stub.update(
        queue_answer=_queue(due="1"), queue_rows="7|scheduled|2026-10-11 10:45:00+00|||"
    )

    stderr = _closed(
        QUEUE_OPS["preflight"], deploy_dir, stub, backups, dump, "due-in-window=1"
    )

    assert "publication 7|scheduled|2026-10-11 10:45:00+00" in stderr
    rows_sql = [
        c["argv"][c["argv"].index("-c") + 1]
        for c in stub.calls()
        if c["argv"][:1] == ["exec"] and "ORDER BY id" in " ".join(c["argv"])
    ]

    assert len(rows_sql) == 1
    for column in ("body", "title", "images", "items", "last_error"):
        assert column not in rows_sql[0]


UNREADABLE_QUEUE = {
    "sql-error": ({"queue_rc": 1}, "cannot read the publication queue"),
    "two-rows": (
        {"queue_answer": QUEUE_QUIET + "\n" + QUEUE_QUIET},
        "ambiguous answer",
    ),
    "empty": ({"queue_answer": ""}, "ambiguous answer"),
    "missing-count": (
        {"queue_answer": QUEUE_QUIET.replace("|0|0|0|0|0|0|0|", "|0|0|0|0|0|0|")},
        "ambiguous answer",
    ),
    "not-a-number": ({"queue_answer": _queue(due="n/a")}, "ambiguous answer"),
    "negative": ({"queue_answer": _queue(lease="-1")}, "ambiguous answer"),
    "flag-not-boolean": ({"queue_answer": _queue(in_window="t")}, "ambiguous answer"),
    "local-time": (
        {"queue_answer": _queue(now="2026-10-11 13:30:00+03")},
        "ambiguous answer",
    ),
}


@pytest.mark.parametrize("case", sorted(UNREADABLE_QUEUE))
@pytest.mark.parametrize("op", sorted(QUEUE_OPS))
def test_an_unreadable_queue_closes_the_gate(
    deploy_dir, stub, backups, dump, op: str, case: str
) -> None:
    changes, message = UNREADABLE_QUEUE[case]
    stub.update(**changes)

    _closed(QUEUE_OPS[op], deploy_dir, stub, backups, dump, message)


WINDOW_REFUSALS = {
    "missing": (lambda d: (d / "control-center.window").unlink(), "missing"),
    "local-time": (
        lambda d: write_window(
            d, "2026-10-11T13:00:00+03:00", "2026-10-11T15:00:00+03:00"
        ),
        "must be UTC",
    ),
    "no-zone": (
        lambda d: write_window(d, "2026-10-11T10:00:00", "2026-10-11T12:00:00"),
        "must be UTC",
    ),
    "date-only": (lambda d: write_window(d, "2026-10-11", "2026-10-12"), "must be UTC"),
    "end-before-start": (
        lambda d: write_window(d, "2026-10-11T12:00:00Z", "2026-10-11T10:00:00Z"),
        "must be after",
    ),
    "empty-window": (
        lambda d: write_window(d, "2026-10-11T10:00:00Z", "2026-10-11T10:00:00Z"),
        "must be after",
    ),
    "sql-in-timestamp": (
        lambda d: write_window(d, "2026-10-11T10:00:00Z'--", "2026-10-11T12:00:00Z"),
        "must be UTC",
    ),
    "extra-key": (
        lambda d: (d / "control-center.window").write_text(
            "SMM_CC_WINDOW_START=2026-10-11T10:00:00Z\nSMM_CC_WINDOW_END=2026-10-11T12:00:00Z\n"
            "SMM_CC_QUEUE_MARGIN_MINUTES=0\n"
        ),
        "must define exactly",
    ),
}


@pytest.mark.parametrize("case", sorted(WINDOW_REFUSALS))
@pytest.mark.parametrize("op", sorted(QUEUE_OPS))
def test_the_window_must_be_explicit_utc(
    deploy_dir, stub, backups, dump, op: str, case: str
) -> None:
    breaks, message = WINDOW_REFUSALS[case]
    breaks(deploy_dir)

    result = _run(QUEUE_OPS[op], deploy_dir, stub, backups, dump)

    assert result.returncode == 2
    assert message in result.stderr, result.stderr
    assert _side_effects(stub) == []
    assert not [
        c
        for c in stub.calls()
        if c["argv"][:1] == ["exec"] and "'queue'," in " ".join(c["argv"])
    ]


def test_the_margin_is_at_least_an_hour_on_both_sides() -> None:
    gate = (BACKUP.parent / "cc-gate.sh").read_text(encoding="utf-8")

    assert "readonly CC_QUEUE_MARGIN_MINUTES=60\n" in gate
    assert (
        "timestamptz '$CC_WINDOW_START' - interval '$CC_QUEUE_MARGIN_MINUTES minutes'"
        in gate
    )
    assert (
        "timestamptz '$CC_WINDOW_END' + interval '$CC_QUEUE_MARGIN_MINUTES minutes'"
        in gate
    )


# -- the retention of the pre-migration dump -------------------------------------------


def test_dump_refuses_a_directory_the_nightly_rotation_prunes(
    deploy_dir, stub, backups
) -> None:
    nightly = backups / "ai_smm-20261011T003001Z.dump"
    nightly.write_bytes(b"PGDMP")

    result = run_script(
        BACKUP,
        deploy_dir,
        "dump",
        "--container",
        FIXTURE_PG_CONTAINER,
        "--out-dir",
        str(backups),
        stub=stub,
    )

    assert result.returncode == 2
    assert "automatic rotation prunes it" in result.stderr
    assert sorted(p.name for p in backups.iterdir()) == [nightly.name]
    assert _side_effects(stub) == []


def test_the_dump_name_is_outside_the_nightly_rotation_pattern(
    deploy_dir, stub, backups
) -> None:
    result = run_script(
        BACKUP,
        deploy_dir,
        "dump",
        "--container",
        FIXTURE_PG_CONTAINER,
        "--out-dir",
        str(backups),
        stub=stub,
    )

    assert result.returncode == 0, result.stderr
    names = sorted(p.name for p in backups.iterdir())

    assert len(names) == 3  # .dump, .dump.sha256, .dump.source
    for name in names:
        assert name.startswith("cc-premigration-ai_smm-")
        # The nightly backup.sh prunes `ai_smm-*.dump` after 14 days.
        assert not Path(name).match("ai_smm-*.dump")


# -- the queue SQL on a real PostgreSQL ----------------------------------------------

UTC = timezone.utc


def _utc(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def live(deploy_dir: Path, stub: DockerStub, database_url: str, engine):
    """The stub forwards the queue SQL to the test database; the window is
    [now - 10 min, now + 110 min], so the protected span is
    [now - 70 min, now + 170 min]."""

    stub.update(sql_url=database_url.replace("postgresql+psycopg://", "postgresql://"))
    now = datetime.now(UTC)
    write_window(
        deploy_dir,
        _utc(now - timedelta(minutes=10)),
        _utc(now + timedelta(minutes=110)),
    )

    def gate() -> tuple[int, str]:
        result = run_script(WRAPPER, deploy_dir, "preflight", stub=stub)

        return result.returncode, result.stdout + result.stderr

    gate.now = now
    gate.window = lambda start, end: write_window(deploy_dir, _utc(start), _utc(end))

    return gate


def test_live_an_empty_queue_and_old_history_pass(live, make_publication) -> None:
    now = live.now
    make_publication(
        status=PublicationStatus.PUBLISHED,
        scheduled_at=now - timedelta(days=1),
        published_at=now - timedelta(hours=3),
        threads_post_id="p1",
    )
    make_publication(
        status=PublicationStatus.FAILED, scheduled_at=now - timedelta(days=2)
    )
    make_publication(
        status=PublicationStatus.CANCELLED, scheduled_at=now + timedelta(minutes=30)
    )
    make_publication(
        status=PublicationStatus.DRAFT, scheduled_at=now + timedelta(minutes=30)
    )

    code, output = live()

    assert code == 0, output
    assert "gate queue: PASS" in output


@pytest.mark.parametrize(
    ("minutes", "closed"),
    [(-180, True), (-1, True), (30, True), (165, True), (175, False), (24 * 60, False)],
    ids=[
        "overdue-3h",
        "overdue-1m",
        "in-window",
        "in-trailing-margin",
        "after-margin",
        "tomorrow",
    ],
)
def test_live_a_scheduled_publication_closes_the_gate_until_the_margin_after(
    live, make_publication, minutes: int, closed: bool
) -> None:
    make_publication(scheduled_at=live.now + timedelta(minutes=minutes))

    code, output = live()

    assert (code == 2) is closed, output
    if closed:
        assert "due-in-window=1" in output


def test_live_a_retry_due_in_the_window_closes_the_gate(live, make_publication) -> None:
    make_publication(
        scheduled_at=live.now - timedelta(hours=2),
        next_attempt_at=live.now + timedelta(minutes=100),
        attempt_count=1,
    )

    code, output = live()

    assert code == 2
    assert "retry-in-window=1" in output and "due-in-window=1" in output


def test_live_a_retry_deferred_past_the_margin_passes(live, make_publication) -> None:
    make_publication(
        scheduled_at=live.now - timedelta(hours=2),
        next_attempt_at=live.now + timedelta(hours=5),
        attempt_count=1,
    )

    code, output = live()

    assert code == 0, output


@pytest.mark.parametrize(
    "status", [PublicationStatus.CLAIMED, PublicationStatus.PUBLISHING]
)
def test_live_an_active_publication_and_its_lease_close_the_gate(
    live, make_publication, status
) -> None:
    make_publication(
        status=status,
        scheduled_at=live.now - timedelta(minutes=2),
        claimed_by="fixture-worker-1",
        claimed_at=live.now - timedelta(minutes=1),
        lease_expires_at=live.now + timedelta(minutes=14),
    )

    code, output = live()

    assert code == 2
    assert "publishing=1" in output and "active-lease=1" in output


def test_live_a_lease_that_expired_within_the_margin_closes_the_gate(
    live, make_publication
) -> None:
    make_publication(
        status=PublicationStatus.SCHEDULED,
        scheduled_at=live.now + timedelta(days=1),
        lease_expires_at=live.now - timedelta(minutes=30),
    )

    code, output = live()

    assert code == 2
    assert "active-lease=1" in output


@pytest.mark.parametrize(
    ("minutes_ago", "closed"),
    [(5, True), (65, True), (75, False)],
)
def test_live_a_publication_in_the_margin_before_closes_the_gate(
    live, make_publication, minutes_ago: int, closed: bool
) -> None:
    make_publication(
        status=PublicationStatus.PUBLISHED,
        scheduled_at=live.now - timedelta(minutes=minutes_ago),
        published_at=live.now - timedelta(minutes=minutes_ago),
        threads_post_id="p",
    )

    code, output = live()

    assert (code == 2) is closed, output


def test_live_an_unfinished_attempt_closes_the_gate(
    live, make_publication, session: Session
) -> None:
    publication = make_publication(
        status=PublicationStatus.PUBLISHED,
        scheduled_at=live.now - timedelta(days=1),
        published_at=live.now - timedelta(days=1),
        threads_post_id="p",
    )
    session.execute(
        text(
            "INSERT INTO publication_attempts"
            " (publication_id, attempt_number, worker_id, phase, started_at)"
            " VALUES (:p, 1, 'w', 'publish', now() - interval '1 day')"
        ),
        {"p": publication.id},
    )
    session.commit()

    code, output = live()

    assert code == 2
    assert "attempts-active-or-in-margin=1" in output


def test_live_a_publication_awaiting_review_closes_the_gate(
    live, make_publication
) -> None:
    make_publication(
        status=PublicationStatus.NEEDS_REVIEW, scheduled_at=live.now - timedelta(days=1)
    )

    code, output = live()

    assert code == 2
    assert "needs-review=1" in output


def test_live_now_outside_the_window_closes_the_gate(live) -> None:
    live.window(live.now + timedelta(hours=1), live.now + timedelta(hours=2))
    code, output = live()

    assert code == 2 and "outside the agreed window" in output

    live.window(live.now - timedelta(hours=3), live.now - timedelta(hours=1))
    code, output = live()

    assert code == 2 and "outside the agreed window" in output


def test_live_a_window_longer_than_twelve_hours_closes_the_gate(live) -> None:
    live.window(live.now - timedelta(hours=1), live.now + timedelta(hours=12))
    code, output = live()

    assert code == 2 and "longer than 12 hours" in output


def test_live_an_impossible_date_is_a_sql_error_and_closes_the_gate(
    live, deploy_dir: Path
) -> None:
    write_window(deploy_dir, "2026-02-30T10:00:00Z", "2026-02-30T12:00:00Z")
    code, output = live()

    assert code == 2 and "cannot read the publication queue" in output


def test_network_templates_do_not_use_join_on_container_lists() -> None:
    # The Docker CLI hands DNSNames/Aliases to --format templates untyped
    # ([]interface{}), so `join` fails on a real daemon ("wrong type for
    # value; expected []string") -- which the stub cannot reproduce; the
    # rehearsal caught it. `range` works on both.
    gate = (BACKUP.parent / "cc-gate.sh").read_text(encoding="utf-8")

    assert "join ." not in gate
    assert "{{range .DNSNames}}" in gate and "{{range .Aliases}}" in gate
