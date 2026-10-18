"""The API's business-mutation role ai_smm_cc_rw (SMM-025C, FU-025B-1).

Created by the operator's own deploy/control-center/db/ai_smm_cc_rw.*.sql
against a real PostgreSQL, as a superuser of the disposable test cluster
(AI_SMM_TEST_ADMIN_DATABASE_URL), like ai_smm_cc in
tests/test_db_least_privilege.py.

The matrix below is written independently of the grants file, so a
change to either one without the other fails here. tests/mutation_role/
re-runs the API's business-command suites connected as this role, which
proves every grant is enough; the refusals below prove nothing else is.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError, ProgrammingError

from ai_smm.db.models import Base
from tests import pg_roles
from tests.test_api_mutation_gate import World, world  # noqa: F401 -- fixture
from tests.test_db_least_privilege import (  # noqa: F401 -- fixtures
    COLUMN_SELECTS,
    COLUMN_WRITES,
    INSUFFICIENT_PRIVILEGE,
    KEY_MATERIAL_COLUMNS,
    NO_ACCESS_TABLES,
    SELECT_TABLES,
    TABLE_PRIVILEGES,
    _fingerprint,
    _sqlstate,
    owner_name,
    seeded,
)


ROLE = pg_roles.MUTATION_ROLE


def _cols(spec: str) -> frozenset[str]:
    return frozenset(c.strip() for c in spec.split(","))


#: Business writes, column by column. ai_smm_cc's session write
#: (COLUMN_WRITES) comes on top.
BUSINESS_WRITES: dict[tuple[str, str], frozenset[str]] = {
    # SMM-025D: business audit entries, guarded (as the bound user, business
    # actions only); never id, at or written_by.
    ("audit_log", "INSERT"): _cols("actor, action, subject, details"),
    ("projects", "INSERT"): _cols(
        "id, display_name, description, knowledge_path, default_platform,"
        " is_active, default_timezone, default_language, version, created_at,"
        " updated_at"
    ),
    ("projects", "UPDATE"): _cols(
        "display_name, description, default_timezone, default_language,"
        " version, updated_at"
    ),
    ("project_memberships", "INSERT"): _cols(
        "id, project_id, user_id, role, created_at, updated_at"
    ),
    ("project_settings", "INSERT"): _cols(
        "project_id, content_config, publishing_config, brand_config,"
        " version, updated_at"
    ),
    ("project_settings", "UPDATE"): _cols(
        "content_config, publishing_config, brand_config, version, updated_at"
    ),
    ("social_accounts", "INSERT"): _cols(
        "id, project_id, platform, display_name, status, external_account_id,"
        " external_username, version, created_by_user_id, created_at,"
        " updated_at, disabled_at"
    ),
    ("social_accounts", "UPDATE"): _cols(
        "display_name, status, disabled_at, external_account_id,"
        " external_username, version, updated_at"
    ),
    ("social_account_credentials", "INSERT"): _cols(
        "id, social_account_id, status, scheme, key_id, ephemeral_public_key,"
        " nonce, ciphertext, fingerprint, verification_status, verified_at,"
        " expires_at, created_by_user_id, created_at, superseded_at,"
        " revoked_at"
    ),
    ("social_account_credentials", "UPDATE"): _cols(
        "status, ephemeral_public_key, nonce, ciphertext, superseded_at, revoked_at"
    ),
    ("content_items", "INSERT"): _cols(
        "id, project_id, title, content_type, status, current_revision_id,"
        " approved_revision_id, version, created_by_user_id,"
        " generation_job_id, created_at,"
        " updated_at, archived_at"
    ),
    ("content_items", "UPDATE"): _cols(
        "current_revision_id, approved_revision_id, status, version, updated_at"
    ),
    ("content_revisions", "INSERT"): _cols(
        "id, content_item_id, revision_number, title, body, format, images,"
        " items, metadata, source, source_ref, editor_score, editor_notes,"
        " content_hash, social_account_id, created_by_user_id, created_at"
    ),
    ("content_approvals", "INSERT"): _cols(
        "id, content_item_id, revision_id, decision, actor_user_id, note, created_at"
    ),
    ("content_publication_links", "INSERT"): _cols(
        "content_item_id, revision_id, platform, publication_id, created_at"
    ),
    ("publications", "INSERT"): _cols(
        "project_id, platform, ordinal, title, format, body, images, items,"
        " status, scheduled_at, idempotency_key, threads_post_id,"
        " published_at, claimed_by, claimed_at, lease_expires_at,"
        " attempt_count, next_attempt_at, last_error, editor_score,"
        " human_reviewed, source_ref, social_account_id, series_id,"
        " series_position, series_total, parent_publication_id,"
        " previous_publication_id, created_at, updated_at"
    ),
    ("publications", "UPDATE"): _cols(
        "status, scheduled_at, next_attempt_at, claimed_by, claimed_at,"
        " lease_expires_at, last_error, attempt_count, updated_at"
    ),
    # POST /projects/{id}/generation-jobs (SMM-026A/2.1). The ORM sends
    # every column; the worker-owned ones (claimed_*, next_attempt_at,
    # last_error, finished_at) go in as NULL -- the guard's insert trigger
    # refuses any of them carrying a real value regardless of this grant.
    ("content_generation_jobs", "INSERT"): _cols(
        "id, project_id, requested_by_user_id, status, params, max_attempts,"
        " last_error,"
        " next_attempt_at, claimed_by, claimed_at, lease_expires_at,"
        " created_at, updated_at, finished_at"
    ),
    # POST /generation-jobs/{id}/cancel: queued|claimed -> cancelled only.
    # claimed_by/claimed_at/lease_expires_at/next_attempt_at/attempt_count/
    # result/last_error are granted too, same as publications' own claim
    # columns, so tampering with them reaches the trigger's refusal
    # instead of a bare permission error; the trigger never lets any of
    # them actually change through this role.
    ("content_generation_jobs", "UPDATE"): _cols(
        "status, updated_at, finished_at, claimed_by, claimed_at,"
        " lease_expires_at, next_attempt_at, attempt_count, result,"
        " last_error"
    ),
}

#: Columns no API command may ever change after the row exists.
NEVER_UPDATED = {
    "publications": {
        "human_reviewed",
        "threads_post_id",
        "published_at",
        "body",
        "title",
        "images",
        "items",
        "format",
        "project_id",
        "social_account_id",
        "idempotency_key",
        "series_id",
        "parent_publication_id",
    },
    "social_accounts": {"project_id", "platform"},
    "social_account_credentials": {"social_account_id", "key_id", "fingerprint"},
    "projects": {"id", "knowledge_path", "default_platform", "is_active"},
    "content_items": {"project_id"},
    "content_generation_jobs": {
        "project_id", "requested_by_user_id", "params", "max_attempts"
    },
}


@pytest.fixture
def rw_engine(mutation_engine: Engine) -> Engine:
    return mutation_engine


def _q(admin_url: str, sql: str):
    return pg_roles.query_admin(admin_url, sql)


# -- the matrix ----------------------------------------------------------------


def test_every_model_table_has_a_decision_for_the_mutation_role() -> None:
    decided = SELECT_TABLES | NO_ACCESS_TABLES | set(COLUMN_SELECTS)

    assert set(Base.metadata.tables) <= decided
    assert {t for t, _ in BUSINESS_WRITES} <= set(Base.metadata.tables)


def test_effective_privileges_match_the_matrix(
    mutation_role, admin_database_url: str
) -> None:
    tables = _q(
        admin_database_url,
        "SELECT c.relname, p.priv, has_table_privilege('ai_smm_cc_rw', c.oid, p.priv)"
        "  FROM pg_class c CROSS JOIN unnest(ARRAY["
        + ",".join(f"'{p}'" for p in TABLE_PRIVILEGES)
        + "]) AS p(priv) WHERE c.relnamespace = 'public'::regnamespace"
        "   AND c.relkind IN ('r', 'p', 'v', 'm', 'f')",
    )

    # Table level: SELECT only, exactly as ai_smm_cc. Every write is per
    # column, so no table-level INSERT/UPDATE/DELETE/TRUNCATE exists.
    assert {(n, p) for n, p, held in tables if held} == {
        (n, "SELECT") for n in SELECT_TABLES
    }

    columns = _q(
        admin_database_url,
        "SELECT c.relname, a.attname, p.priv,"
        "       has_column_privilege('ai_smm_cc_rw', c.oid, a.attnum, p.priv)"
        "  FROM pg_class c JOIN pg_attribute a ON a.attrelid = c.oid"
        "   AND a.attnum > 0 AND NOT a.attisdropped"
        " CROSS JOIN unnest(ARRAY['INSERT', 'UPDATE', 'REFERENCES']) AS p(priv)"
        " WHERE c.relnamespace = 'public'::regnamespace"
        "   AND c.relkind IN ('r', 'p', 'v', 'm', 'f')",
    )
    actual = {(t, p, c) for t, c, p, held in columns if held}
    expected = {
        (t, p, c)
        for matrix in (COLUMN_WRITES, BUSINESS_WRITES)
        for (t, p), names in matrix.items()
        for c in names
    }

    assert actual == expected

    readable = _q(
        admin_database_url,
        "SELECT c.relname, a.attname FROM pg_class c JOIN pg_attribute a"
        "    ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped"
        " WHERE c.relnamespace = 'public'::regnamespace"
        "   AND c.relkind IN ('r', 'p', 'v', 'm', 'f')"
        "   AND NOT has_table_privilege('ai_smm_cc_rw', c.oid, 'SELECT')"
        "   AND has_column_privilege('ai_smm_cc_rw', c.oid, a.attnum, 'SELECT')",
    )
    selects: dict[str, set[str]] = {}

    for table, column in readable:
        selects.setdefault(table, set()).add(column)

    assert selects == {t: set(c) for t, c in COLUMN_SELECTS.items()}
    # Write-only key material: insertable, wipeable, never readable.
    assert not (selects["social_account_credentials"] & KEY_MATERIAL_COLUMNS)


def test_no_column_that_must_never_change_is_updatable() -> None:
    for table, columns in NEVER_UPDATED.items():
        assert not (BUSINESS_WRITES.get((table, "UPDATE"), set()) & columns), table


def test_sequences_audit_and_publications_only(
    mutation_role, admin_database_url: str
) -> None:
    held = _q(
        admin_database_url,
        "SELECT s.relname, p.priv FROM pg_class s"
        " CROSS JOIN unnest(ARRAY['USAGE', 'SELECT', 'UPDATE']) AS p(priv)"
        " WHERE s.relnamespace = 'public'::regnamespace AND s.relkind = 'S'"
        "   AND has_sequence_privilege('ai_smm_cc_rw', s.oid, p.priv)",
    )
    names = _q(
        admin_database_url,
        "SELECT split_part(pg_get_serial_sequence('public.audit_log', 'id'), '.', 2),"
        "       split_part("
        "pg_get_serial_sequence('public.publications', 'id'), '.', 2)",
    )[0]

    assert set(held) == {(names[0], "USAGE"), (names[1], "USAGE")}


def test_the_role_has_the_same_bounds_as_the_read_only_role(
    mutation_role, admin_database_url: str
) -> None:
    rows = _q(
        admin_database_url,
        "SELECT r.rolsuper, r.rolinherit, r.rolcreaterole, r.rolcreatedb,"
        "       r.rolreplication, r.rolbypassrls, r.rolcanlogin, r.rolconnlimit,"
        "       (SELECT count(*) FROM pg_auth_members m"
        "         WHERE m.member = r.oid OR m.roleid = r.oid),"
        "       (SELECT array_to_string(setconfig, ' ') FROM pg_db_role_setting"
        "         WHERE setrole = r.oid AND setdatabase = 0)"
        "  FROM pg_roles r WHERE r.rolname = 'ai_smm_cc_rw'",
    )

    assert rows == [
        (
            False,
            False,
            False,
            False,
            False,
            False,
            True,
            10,
            0,
            "statement_timeout=15s lock_timeout=5s"
            " idle_in_transaction_session_timeout=60s",
        )
    ]


def test_the_verify_file_passes(mutation_role, admin_database_url: str) -> None:
    lines = pg_roles.verify_mutation(admin_database_url)

    assert pg_roles.failures(lines) == []
    assert lines[-1].startswith("END|checks|")


def test_provisioning_changed_no_other_role(mutation_role) -> None:
    """ai_smm_cc, the owner, the worker, PUBLIC: all as they were."""

    assert mutation_role.others_after == mutation_role.others_before
    assert mutation_role.others_before


def test_the_grants_file_converges_on_drift(
    mutation_role, admin_database_url: str
) -> None:
    pg_roles.run_admin(
        admin_database_url,
        "GRANT DELETE ON publications, social_account_credentials TO ai_smm_cc_rw;"
        " GRANT SELECT (ciphertext) ON social_account_credentials TO ai_smm_cc_rw",
    )

    drift = pg_roles.failures(pg_roles.verify_mutation(admin_database_url))

    assert any("publications.delete" in line for line in drift)
    assert any("ciphertext.select" in line for line in drift)

    pg_roles.run_script(admin_database_url, pg_roles.MUTATION_GRANTS_SQL)

    assert pg_roles.failures(pg_roles.verify_mutation(admin_database_url)) == []


def test_the_read_only_role_did_not_gain_anything(
    mutation_role, api_role, admin_database_url: str
) -> None:
    """Provisioning the mutation role leaves ai_smm_cc at its own matrix."""

    assert pg_roles.failures(pg_roles.verify(admin_database_url)) == []


# -- refusals ------------------------------------------------------------------

#: Each one a compromised or buggy API in mutation mode could try.
DENIED = {
    # the queue: content, approval and the proof of publication
    "delete publications": "DELETE FROM publications",
    "truncate publications": "TRUNCATE publications",
    "mark reviewed": "UPDATE publications SET human_reviewed = true",
    "forge a post id": "UPDATE publications SET threads_post_id = 'x'",
    "forge published_at": "UPDATE publications SET published_at = now()",
    "rewrite the text": "UPDATE publications SET body = 'x'",
    "reroute a publication": "UPDATE publications SET social_account_id = NULL",
    "move a publication": "UPDATE publications SET project_id = project_id",
    "relink a series": "UPDATE publications SET parent_publication_id = NULL",
    "insert an attempt": (
        "INSERT INTO publication_attempts (publication_id, attempt_number,"
        " worker_id, phase) SELECT id, 99, 'x', 'publish' FROM publications"
        " LIMIT 1"
    ),
    "update attempts": "UPDATE publication_attempts SET outcome = 'unknown'",
    "delete attempts": "DELETE FROM publication_attempts",
    # credentials and accounts
    "read key material": "SELECT ciphertext, nonce FROM social_account_credentials",
    "read whole credential rows": "SELECT * FROM social_account_credentials",
    "move a credential": (
        "UPDATE social_account_credentials SET social_account_id = social_account_id"
    ),
    "delete credentials": "DELETE FROM social_account_credentials",
    "move an account": "UPDATE social_accounts SET project_id = project_id",
    "delete accounts": "DELETE FROM social_accounts",
    # editorial history
    "rewrite a revision": "UPDATE content_revisions SET body = 'x'",
    "delete revisions": "DELETE FROM content_revisions",
    "delete approvals": "DELETE FROM content_approvals",
    "rewrite an approval": "UPDATE content_approvals SET decision = 'approved'",
    "move content": "UPDATE content_items SET project_id = project_id",
    "delete content": "DELETE FROM content_items",
    "relink lineage": (
        "UPDATE content_publication_links SET publication_id = publication_id"
    ),
    "write series": "UPDATE content_series SET status = 'active'",
    "insert series": (
        "INSERT INTO content_series (project_id, title) VALUES ('test-project', 'x')"
    ),
    # projects, membership, identity
    "change a role": "UPDATE project_memberships SET role = 'owner'",
    "delete memberships": "DELETE FROM project_memberships",
    "rename a project id": "UPDATE projects SET id = id",
    "repoint knowledge": "UPDATE projects SET knowledge_path = '/etc'",
    "deactivate a project": "UPDATE projects SET is_active = false",
    "delete projects": "DELETE FROM projects",
    "set a password": "UPDATE users SET password_hash = 'x'",
    "insert user": (
        "INSERT INTO users (id, email, password_hash, display_name)"
        " VALUES (gen_random_uuid(), 'x@example.com', 'h', 'n')"
    ),
    "delete users": "DELETE FROM users",
    "delete sessions": "DELETE FROM user_sessions",
    # the audit trail
    "update audit": "UPDATE audit_log SET action = 'x'",
    "delete audit": "DELETE FROM audit_log",
    "backdate audit": (
        "INSERT INTO audit_log (at, actor, action) VALUES ('2000-01-01', 'a', 'b')"
    ),
    # schema, other roles (alembic_version exists only on a migrated
    # database: test_migrations_run_as_the_owner_alongside_the_role)
    "create table": "CREATE TABLE public.rw_probe (id int)",
    "drop trigger": (
        "DROP TRIGGER trg_publications_account_claim_guard ON publications"
    ),
    "alter table": "ALTER TABLE publications ADD COLUMN rw_probe int",
    "become owner": "SET ROLE {owner}",
    "disable triggers": "SET session_replication_role = replica",
    "lock the queue": "LOCK TABLE publications IN EXCLUSIVE MODE",
    "read the role catalog": "SELECT * FROM pg_authid",
}


@pytest.mark.parametrize("name", sorted(DENIED))
def test_postgresql_refuses_it(
    seeded: World,  # noqa: F811
    engine: Engine,
    rw_engine: Engine,
    owner_name: str,  # noqa: F811
    make_account,
    name: str,
) -> None:
    make_account()
    before = _fingerprint(engine)

    with rw_engine.connect() as connection:
        transaction = connection.begin()

        try:
            with pytest.raises(DBAPIError) as raised:
                connection.execute(text(DENIED[name].format(owner=owner_name)))

            assert _sqlstate(raised.value) == INSUFFICIENT_PRIVILEGE, raised.value
        finally:
            transaction.rollback()

    assert _fingerprint(engine) == before


def test_the_old_worker_guard_holds_for_the_mutation_role(
    rw_engine: Engine, make_account, make_publication
) -> None:
    """Granted status may not claim an account row: the trigger still asks
    for the account-routing declaration only the worker makes."""

    account, _ = make_account()
    publication = make_publication(social_account_id=account.id)

    with rw_engine.connect() as connection, pytest.raises(DBAPIError) as raised:
        connection.execute(
            text("UPDATE publications SET status = 'claimed' WHERE id = :id"),
            {"id": publication.id},
        )

    assert "only an account-routing worker" in str(raised.value)


def test_closed_risk_a_reviewed_scheduled_publication_cannot_be_forged(
    rw_engine: Engine, engine: Engine, project: str
) -> None:
    """CLOSED by SMM-025D (FU-025C-1), was a residual risk of SMM-025C.

    The forged row of the former residual-risk test -- already `scheduled`
    and `human_reviewed`, with no approval or lineage -- is refused by
    PostgreSQL. tests/test_db_guards.py covers every other way in, with
    and without a bound owner session.
    """

    with rw_engine.connect() as connection, pytest.raises(DBAPIError) as raised:
        connection.execute(
            text(
                "INSERT INTO publications (project_id, ordinal, title, format,"
                " body, status, scheduled_at, human_reviewed, idempotency_key)"
                " VALUES (:project, 990001, 'forged', 'text', 'forged',"
                " 'scheduled', now(), true, 'forged-990001')"
            ),
            {"project": project},
        )

    assert _sqlstate(raised.value) == INSUFFICIENT_PRIVILEGE

    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT count(*) FROM publications WHERE ordinal = 990001")
        ).scalar_one() == 0


def test_closed_risk_a_credential_cannot_be_stored_without_an_owner_session(
    rw_engine: Engine, make_account
) -> None:
    """CLOSED by SMM-025D for a credential alone: without a transaction
    bound to a live owner session, nothing is stored or retired. With one,
    `verified` is still what the API concluded from GET /me -- the worker
    re-checks the identity before every publish (ai_smm.publishing.service)
    and a bound identity never changes (tests/test_db_guards.py)."""

    account, _ = make_account()

    with rw_engine.connect() as connection, pytest.raises(DBAPIError) as raised:
        connection.execute(
            text(
                "UPDATE social_account_credentials SET status = 'superseded',"
                " superseded_at = now(), ciphertext = NULL, nonce = NULL,"
                " ephemeral_public_key = NULL"
                " WHERE social_account_id = :account AND status = 'active'"
            ),
            {"account": account.id},
        )

    assert _sqlstate(raised.value) == INSUFFICIENT_PRIVILEGE


def test_the_role_cannot_grant_what_it_holds(
    rw_engine: Engine, mutation_role, admin_database_url: str
) -> None:
    with rw_engine.connect() as connection:
        connection.execute(text("GRANT SELECT ON projects TO PUBLIC"))
        connection.rollback()

    assert _q(
        admin_database_url,
        "SELECT count(*) FROM pg_class c, aclexplode(c.relacl) a"
        " WHERE c.relnamespace = 'public'::regnamespace"
        "   AND a.grantor = to_regrole('ai_smm_cc_rw')",
    ) == [(0,)]


def test_the_revoke_file_refuses_while_the_role_is_in_use(
    rw_engine: Engine, mutation_role, admin_database_url: str
) -> None:
    import psycopg

    with rw_engine.connect() as connection:
        connection.execute(text("SELECT 1"))

        with pytest.raises(psycopg.errors.RaiseException, match="open sessions"):
            pg_roles.run_script(admin_database_url, pg_roles.MUTATION_REVOKE_SQL)

    assert pg_roles.failures(pg_roles.verify_mutation(admin_database_url)) == []


def test_the_api_package_never_configures_a_private_key() -> None:
    """Plaintext needs the private key; no API configuration names it."""

    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    compose = (root / "compose.control-center.yml").read_text(encoding="utf-8")

    assert "CREDENTIALS_PRIVATE_KEY" not in compose
    for path in (root / "src" / "ai_smm" / "api").rglob("*.py"):
        assert "credentials_private_key_files" not in path.read_text(encoding="utf-8")


def test_reads_still_work_and_key_material_stays_hidden(
    rw_engine: Engine, make_account
) -> None:
    make_account()

    with rw_engine.connect() as connection:
        rows = connection.execute(
            text("SELECT id, fingerprint, status FROM social_account_credentials")
        ).all()

        with pytest.raises(ProgrammingError) as raised:
            connection.execute(
                text("SELECT ciphertext FROM social_account_credentials")
            )

    assert rows
    assert _sqlstate(raised.value) == INSUFFICIENT_PRIVILEGE


# -- on a migrated database ----------------------------------------------------


@pytest.fixture
def migrated_db(database_url: str, admin_database_url: str, mutation_role):
    """A migrated copy of the schema, owned like production's."""

    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url

    from tests.conftest import drop_scratch_database

    name = "ai_smm_rw_migration_test"
    owner = create_engine(database_url, isolation_level="AUTOCOMMIT")

    with owner.connect() as connection:
        drop_scratch_database(connection, name)
        connection.execute(text(f'CREATE DATABASE "{name}"'))

    def at(url: str) -> str:
        return make_url(url).set(database=name).render_as_string(hide_password=False)

    yield at(database_url), at(admin_database_url), at(mutation_role.url)

    with owner.connect() as connection:
        drop_scratch_database(connection, name)

    owner.dispose()


def test_on_a_migrated_database_the_role_neither_reads_nor_runs_migrations(
    migrated_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    from alembic import command
    from tests.test_migrations import _alembic_config

    owner_url, admin_url, role_url = migrated_db

    monkeypatch.setenv("AI_SMM_DATABASE_URL", owner_url)
    command.upgrade(_alembic_config(owner_url), "head")
    pg_roles.run_script(admin_url, pg_roles.MUTATION_GRANTS_SQL)

    lines = pg_roles.verify_mutation(admin_url)

    assert pg_roles.failures(lines) == []
    assert "PASS|table.alembic_version.select|denied" in lines

    monkeypatch.setenv("AI_SMM_DATABASE_URL", role_url)

    with pytest.raises(ProgrammingError) as raised:
        command.upgrade(_alembic_config(role_url), "head")

    assert _sqlstate(raised.value) == INSUFFICIENT_PRIVILEGE

    # A table recreated by a migration carries no grant: fail closed until
    # the grants file is re-run.
    monkeypatch.setenv("AI_SMM_DATABASE_URL", owner_url)
    monkeypatch.setenv("AI_SMM_DOWNGRADE_REMOVES_MUTATION_GUARDS", "yes")
    command.downgrade(_alembic_config(owner_url), "3d4ac1986f07")
    command.upgrade(_alembic_config(owner_url), "head")

    lost = pg_roles.failures(pg_roles.verify_mutation(admin_url))

    assert "FAIL|table.social_accounts.select|granted" in lost
    # Recreated guard functions carry no EXECUTE either: fail closed.
    assert "FAIL|function.guard_bind_session.execute|granted" in lost

    pg_roles.run_script(admin_url, pg_roles.MUTATION_GRANTS_SQL)

    assert pg_roles.failures(pg_roles.verify_mutation(admin_url)) == []
