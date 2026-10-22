"""The Control Center API's PostgreSQL role, ai_smm_cc (SMM-024C-2).

Today the read-only guarantee rests on the application: the central
mutation gate and the service-level checks. This suite proves the same
guarantee one layer down, with the privilege model production would run:

* the role is what the operator's grants file says, and nothing more --
  checked as EFFECTIVE privileges on every table, column and sequence,
  so a grant through PUBLIC or a forgotten table is caught;
* provisioning changes no other role: not the owner the worker and the
  migrations use, not PUBLIC;
* the session lifecycle and every read endpoint work through it;
* each write it holds is needed -- take one away and a named flow breaks;
* every business mutation is refused by PostgreSQL itself, even with the
  application's gate switched off;
* the migration tooling neither needs nor can use it.

Fixtures seed data as the owner, as the worker and the CLI do in
production; only the API under test connects as ai_smm_cc. The role is
created by deploy/control-center/db/ai_smm_cc.grants.sql, the file an
operator runs (tests/pg_roles.py); these tests need a superuser of the
disposable test cluster in AI_SMM_TEST_ADMIN_DATABASE_URL.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.orm import Session, sessionmaker

from ai_smm.api.app import create_app
from ai_smm.config import Settings
from ai_smm.db.models import (
    AttemptOutcome,
    AttemptPhase,
    AuditLog,
    Base,
    MembershipRole,
    PublicationAttempt,
    User,
    UserSession,
)
from ai_smm.db.session import create_db_engine
from ai_smm.queue import record_audit
from alembic import command
from tests import pg_roles
from tests.conftest import TEST_PASSWORD, drop_scratch_database
from tests.test_api_mutation_gate import (
    GATED,
    World,
    _fingerprint,
    _request,
    world,  # noqa: F401 -- fixture
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]

# -- the designed matrix (docs/least-privilege-role.md) ----------------------
# Written out independently of the SQL files: this is the specification
# they are tested against.

#: Table-level SELECT: everything the read endpoints, the session check
#: and the audit view read.
SELECT_TABLES = frozenset(
    {
        "projects",
        "project_memberships",
        "project_settings",
        "publications",
        "publication_attempts",
        "content_series",
        "content_items",
        "content_revisions",
        "content_approvals",
        "content_publication_links",
        "audit_log",
        "user_sessions",
        # SMM-025B: account metadata (labels, status, platform identity).
        "social_accounts",
        # SMM-026A/2.1: generation job status, for a draft's provenance.
        "content_generation_jobs",
        # SMM-035B3A: topic ideation jobs and the ideas they suggested.
        "topic_ideation_jobs",
        "content_ideas",
    }
)

#: Column-level SELECT: readable columns of a table the API may not read
#: whole. Credentials are metadata only -- never the sealed token.
COLUMN_SELECTS: dict[str, frozenset[str]] = {
    # SMM-025D, G2: never password_hash; sign-in is smm-auth's.
    "users": frozenset(
        {
            "id",
            "email",
            "display_name",
            "is_active",
            "last_login_at",
            "created_at",
            "updated_at",
        }
    ),
    "social_account_credentials": frozenset(
        {
            "id",
            "social_account_id",
            "status",
            "scheme",
            "key_id",
            "fingerprint",
            "verification_status",
            "verified_at",
            "expires_at",
            "created_by_user_id",
            "created_at",
            "superseded_at",
            "revoked_at",
        }
    ),
}

#: The columns that hold key material. Never readable by the API.
KEY_MATERIAL_COLUMNS = frozenset({"ciphertext", "nonce", "ephemeral_public_key"})

#: Column-level writes, and nothing else. No table-level INSERT, UPDATE,
#: DELETE, TRUNCATE, REFERENCES or TRIGGER anywhere.
COLUMN_WRITES: dict[tuple[str, str], frozenset[str]] = {
    # SMM-025D, grants G2: sessions are created, revoked and audited by
    # smm-auth only. The session check of a read advances last_seen_at
    # (ADR-024C-3A RS-2) -- the only write this role has left.
    ("user_sessions", "UPDATE"): frozenset({"last_seen_at"}),
}

#: Tables the API must not see at all.
#: SMM-025D: mutation_contexts and approval_grants are written and read
#: only inside the guard and smm_auth functions, as the owner.
NO_ACCESS_TABLES = frozenset(
    {"alembic_version", "mutation_contexts", "approval_grants"}
)

TABLE_PRIVILEGES = (
    "SELECT",
    "INSERT",
    "UPDATE",
    "DELETE",
    "TRUNCATE",
    "REFERENCES",
    "TRIGGER",
)

INSUFFICIENT_PRIVILEGE = "42501"


def _sqlstate(exc: BaseException) -> str | None:
    orig = getattr(exc, "orig", exc)

    return getattr(orig, "sqlstate", None)


@pytest.fixture
def owner_name(database_url: str) -> str:
    return make_url(database_url).username


# -- the matrix itself ---------------------------------------------------------


def test_every_model_table_has_a_privilege_decision() -> None:
    """A new table forces a decision here before the API can read it."""

    decided = SELECT_TABLES | NO_ACCESS_TABLES | set(COLUMN_SELECTS)

    assert set(Base.metadata.tables) <= decided, (
        "tables without a decision for ai_smm_cc: "
        f"{sorted(set(Base.metadata.tables) - decided)}"
    )
    assert {table for table, _ in COLUMN_WRITES} <= SELECT_TABLES


def test_the_role_has_no_power_beyond_its_grants(
    api_role, admin_database_url: str
) -> None:
    rows = pg_roles.query_admin(
        admin_database_url,
        """
        SELECT r.rolsuper, r.rolinherit, r.rolcreaterole, r.rolcreatedb,
               r.rolreplication, r.rolbypassrls, r.rolcanlogin,
               r.rolconnlimit,
               (SELECT count(*) FROM pg_auth_members m
                 WHERE m.member = r.oid OR m.roleid = r.oid),
               (SELECT count(*) FROM pg_class WHERE relowner = r.oid)
                 + (SELECT count(*) FROM pg_namespace WHERE nspowner = r.oid)
                 + (SELECT count(*) FROM pg_proc WHERE proowner = r.oid)
                 + (SELECT count(*) FROM pg_type WHERE typowner = r.oid),
               (SELECT rolpassword LIKE 'SCRAM-SHA-256$%'
                  FROM pg_authid WHERE rolname = r.rolname)
          FROM pg_roles r WHERE r.rolname = 'ai_smm_cc'
        """,
    )

    assert rows == [
        # super inherit createrole createdb replication bypassrls login
        (
            False,
            False,
            False,
            False,
            False,
            False,
            True,
            # connlimit, memberships, owned objects, SCRAM password
            10,
            0,
            0,
            True,
        )
    ]


def test_the_api_connection_is_the_restricted_role_with_its_bounds(
    restricted_engine: Engine,
) -> None:
    with restricted_engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT current_user, session_user, "
                "current_setting('statement_timeout'), "
                "current_setting('lock_timeout'), "
                "current_setting('idle_in_transaction_session_timeout'), "
                "current_setting('is_superuser')"
            )
        ).one()

    assert tuple(row) == ("ai_smm_cc", "ai_smm_cc", "15s", "5s", "1min", "off")


def test_effective_privileges_match_the_matrix(
    api_role, admin_database_url: str
) -> None:
    """Effective = own grants, PUBLIC and inherited, on EVERY relation."""

    tables = pg_roles.query_admin(
        admin_database_url,
        "SELECT c.relname, p.priv, "
        "       has_table_privilege('ai_smm_cc', c.oid, p.priv) "
        "  FROM pg_class c CROSS JOIN unnest(ARRAY["
        + ",".join(f"'{p}'" for p in TABLE_PRIVILEGES)
        + "]) AS p(priv) "
        " WHERE c.relnamespace = 'public'::regnamespace "
        "   AND c.relkind IN ('r', 'p', 'v', 'm', 'f')",
    )
    actual_tables = {(name, priv) for name, priv, held in tables if held}
    expected_tables = {(name, "SELECT") for name in SELECT_TABLES}

    assert actual_tables == expected_tables

    columns = pg_roles.query_admin(
        admin_database_url,
        "SELECT c.relname, a.attname, p.priv, "
        "       has_column_privilege('ai_smm_cc', c.oid, a.attnum, p.priv) "
        "  FROM pg_class c "
        "  JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 "
        "       AND NOT a.attisdropped "
        " CROSS JOIN unnest(ARRAY['INSERT', 'UPDATE', 'REFERENCES']) AS p(priv) "
        " WHERE c.relnamespace = 'public'::regnamespace "
        "   AND c.relkind IN ('r', 'p', 'v', 'm', 'f')",
    )
    actual_columns = {
        (table, priv, column) for table, column, priv, held in columns if held
    }
    expected_columns = {
        (table, priv, column)
        for (table, priv), names in COLUMN_WRITES.items()
        for column in names
    }

    assert actual_columns == expected_columns

    readable = pg_roles.query_admin(
        admin_database_url,
        "SELECT c.relname, a.attname "
        "  FROM pg_class c "
        "  JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 "
        "       AND NOT a.attisdropped "
        " WHERE c.relnamespace = 'public'::regnamespace "
        "   AND c.relkind IN ('r', 'p', 'v', 'm', 'f') "
        "   AND NOT has_table_privilege('ai_smm_cc', c.oid, 'SELECT') "
        "   AND has_column_privilege('ai_smm_cc', c.oid, a.attnum, 'SELECT')",
    )
    actual_selects: dict[str, set[str]] = {}

    for table, column in readable:
        actual_selects.setdefault(table, set()).add(column)

    assert actual_selects == {t: set(c) for t, c in COLUMN_SELECTS.items()}
    assert not (actual_selects["social_account_credentials"] & KEY_MATERIAL_COLUMNS)

    sequences = pg_roles.query_admin(
        admin_database_url,
        "SELECT s.relname, p.priv, "
        "       has_sequence_privilege('ai_smm_cc', s.oid, p.priv) "
        "  FROM pg_class s CROSS JOIN unnest(ARRAY['USAGE', 'SELECT', 'UPDATE']) "
        "       AS p(priv) "
        " WHERE s.relnamespace = 'public'::regnamespace AND s.relkind = 'S'",
    )
    # G2 (SMM-025D): no audit entry, so no sequence at all.
    assert {(name, priv) for name, priv, held in sequences if held} == set()

    schema = pg_roles.query_admin(
        admin_database_url,
        "SELECT has_schema_privilege('ai_smm_cc', 'public', 'USAGE'), "
        "       has_schema_privilege('ai_smm_cc', 'public', 'CREATE'), "
        "       has_database_privilege('ai_smm_cc', current_database(), 'CONNECT'), "
        "       has_database_privilege('ai_smm_cc', current_database(), 'CREATE')",
    )

    assert schema == [(True, False, True, False)]


def test_the_verify_file_passes_and_reports_everything(
    api_role, admin_database_url: str
) -> None:
    lines = pg_roles.verify(admin_database_url)

    assert pg_roles.failures(lines) == []
    assert lines[-1].startswith("END|checks|")
    assert int(lines[-1].split("|")[2]) == len(lines) - 1
    # The grants the API depends on are among the checked, by name.
    for check in (
        "PASS|table.publications.update|denied",
        "PASS|table.publications.select|granted",
        "PASS|column.users.is_active.update|denied",
        # G2 (SMM-025D): sign-in, sign-out and their audit are smm-auth's.
        "PASS|column.user_sessions.last_seen_at.update|granted",
        "PASS|column.user_sessions.revoked_at.update|denied",
        "PASS|column.user_sessions.token_hash.insert|denied",
        "PASS|column.users.password_hash.select|denied",
        "PASS|column.users.email.select|granted",
        "PASS|column.audit_log.actor.insert|denied",
        "PASS|column.audit_log.at.insert|denied",
        "PASS|role.password|set, SCRAM-SHA-256",
        "PASS|role.owns_nothing|0 owned objects",
        "PASS|schema.public.create|",
    ):
        assert check in lines


def test_provisioning_changed_no_other_role(api_role) -> None:
    """Owner (worker, migrations), PUBLIC and every other role: unchanged."""

    assert api_role.others_after == api_role.others_before
    assert api_role.others_before  # the snapshot did capture something


# -- the files are safe to re-run, and refuse what they do not own -----------


@pytest.fixture
def restore_grants(admin_database_url: str):
    """Whatever a test does to the role, the session's role is put back."""

    yield

    pg_roles.run_script(admin_database_url, pg_roles.GRANTS_SQL)


def test_the_grants_file_converges_on_drift(
    api_role, admin_database_url: str, restore_grants
) -> None:
    before = pg_roles.verify(admin_database_url)

    pg_roles.run_admin(
        admin_database_url,
        "GRANT UPDATE, DELETE ON publications TO ai_smm_cc; "
        "GRANT INSERT ON users TO ai_smm_cc",
    )
    drifted = pg_roles.failures(pg_roles.verify(admin_database_url))

    assert "FAIL|table.publications.update|denied" in drifted
    assert "FAIL|table.publications.delete|denied" in drifted
    assert "FAIL|table.users.insert|denied" in drifted

    pg_roles.run_script(admin_database_url, pg_roles.GRANTS_SQL)
    pg_roles.run_script(admin_database_url, pg_roles.GRANTS_SQL)

    assert pg_roles.verify(admin_database_url) == before


def test_the_verify_file_sees_a_privilege_granted_through_public(
    api_role, admin_database_url: str
) -> None:
    pg_roles.run_admin(admin_database_url, "GRANT DELETE ON audit_log TO PUBLIC")

    try:
        failing = pg_roles.failures(pg_roles.verify(admin_database_url))
    finally:
        pg_roles.run_admin(admin_database_url, "REVOKE DELETE ON audit_log FROM PUBLIC")

    assert failing == ["FAIL|table.audit_log.delete|denied"]


def test_the_grants_file_stops_if_public_may_create_in_the_schema(
    api_role, admin_database_url: str, restore_grants
) -> None:
    """It never revokes from PUBLIC itself: that is the worker's privilege too."""

    pg_roles.run_admin(admin_database_url, "GRANT CREATE ON SCHEMA public TO PUBLIC")

    try:
        with pytest.raises(psycopg.errors.RaiseException, match="CREATE in schema"):
            pg_roles.run_script(admin_database_url, pg_roles.GRANTS_SQL)
    finally:
        pg_roles.run_admin(
            admin_database_url, "REVOKE CREATE ON SCHEMA public FROM PUBLIC"
        )


@pytest.mark.parametrize(
    "change",
    [
        "ALTER ROLE ai_smm_cc CREATEDB",
        "ALTER ROLE ai_smm_cc INHERIT",
        "ALTER ROLE ai_smm_cc CONNECTION LIMIT 50",
        "GRANT pg_read_all_data TO ai_smm_cc",
    ],
)
def test_an_existing_role_that_differs_is_refused_not_altered(
    api_role, admin_database_url: str, change: str
) -> None:
    undo = {
        "ALTER ROLE ai_smm_cc CREATEDB": "ALTER ROLE ai_smm_cc NOCREATEDB",
        "ALTER ROLE ai_smm_cc INHERIT": "ALTER ROLE ai_smm_cc NOINHERIT",
        "ALTER ROLE ai_smm_cc CONNECTION LIMIT 50": (
            "ALTER ROLE ai_smm_cc CONNECTION LIMIT 10"
        ),
        "GRANT pg_read_all_data TO ai_smm_cc": (
            "REVOKE pg_read_all_data FROM ai_smm_cc"
        ),
    }[change]
    pg_roles.run_admin(admin_database_url, change)

    try:
        with pytest.raises(psycopg.errors.RaiseException, match="refusing"):
            pg_roles.run_script(admin_database_url, pg_roles.GRANTS_SQL)

        assert pg_roles.failures(pg_roles.verify(admin_database_url))
    finally:
        pg_roles.run_admin(admin_database_url, undo)

    assert pg_roles.failures(pg_roles.verify(admin_database_url)) == []


def test_the_grants_file_refuses_a_database_without_the_schema(
    api_role, admin_database_url: str
) -> None:
    elsewhere = make_url(admin_database_url).set(database="postgres")

    with pytest.raises(psycopg.errors.RaiseException, match="tables missing"):
        pg_roles.run_script(
            elsewhere.render_as_string(hide_password=False),
            pg_roles.GRANTS_SQL,
        )


def test_the_revoke_file_refuses_while_the_role_is_in_use(
    api_role, admin_database_url: str, restricted_engine: Engine
) -> None:
    with restricted_engine.connect() as connection:
        connection.execute(text("SELECT 1"))

        with pytest.raises(psycopg.errors.RaiseException, match="open sessions"):
            pg_roles.run_script(admin_database_url, pg_roles.REVOKE_SQL)

    # Nothing was taken away.
    assert pg_roles.failures(pg_roles.verify(admin_database_url)) == []


# -- PostgreSQL refuses every business change from the API's connection ----


@pytest.fixture
def seeded(
    session: Session,
    world: World,  # noqa: F811 -- the imported fixture
    project: str,
    make_user,
    make_membership,
    make_series,
    make_publication,
) -> World:
    """The mutation-gate world, plus a series, an attempt, audit and a session."""

    parts = [make_publication(), make_publication()]
    make_series(parts=parts)
    session.add(
        PublicationAttempt(
            publication_id=parts[0].id,
            attempt_number=1,
            worker_id="test-worker",
            phase=AttemptPhase.PUBLISH,
            outcome=AttemptOutcome.SUCCESS,
            finished_at=datetime.now(timezone.utc),
        )
    )
    record_audit(
        session, actor="test", action="project.seeded", subject=f"project:{project}"
    )
    user = make_user(email="existing@example.com")
    make_membership(user=user, project_id=project, role=MembershipRole.OWNER)
    session.add(
        UserSession(
            user_id=user.id,
            token_hash="0" * 64,
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        )
    )
    session.commit()

    return world


#: Each statement is one a compromised or buggy API could try. All of them
#: must fail with insufficient_privilege, whatever the row.
DENIED = {
    # the publication queue and its history
    "update publications": "UPDATE publications SET status = 'cancelled'",
    "schedule publications": (
        "UPDATE publications SET scheduled_at = now(), human_reviewed = true"
    ),
    "delete publications": "DELETE FROM publications",
    "insert publications": (
        "INSERT INTO publications (project_id, ordinal, title, format, body, "
        "idempotency_key) VALUES ('test-project', 999, 't', 'text', 'b', 'k')"
    ),
    "lock publications": "SELECT id FROM publications FOR UPDATE",
    "lock publications skip locked": (
        "SELECT id FROM publications FOR UPDATE SKIP LOCKED"
    ),
    "update attempts": "UPDATE publication_attempts SET outcome = 'unknown'",
    "delete attempts": "DELETE FROM publication_attempts",
    "nextval publications": (
        "SELECT nextval(pg_get_serial_sequence('publications', 'id'))"
    ),
    "setval audit sequence": (
        "SELECT setval(pg_get_serial_sequence('audit_log', 'id'), 1)"
    ),
    # editorial state
    "update series": "UPDATE content_series SET status = 'active'",
    "insert series": (
        "INSERT INTO content_series (project_id, title) VALUES ('test-project', 'x')"
    ),
    "update content items": "UPDATE content_items SET status = 'approved'",
    "lock content items": "SELECT id FROM content_items FOR UPDATE",
    "insert revisions": (
        "INSERT INTO content_revisions (id, content_item_id, revision_number, "
        "body, format, content_hash, source) SELECT gen_random_uuid(), id, 99, "
        "'b', 'text', 'h', 'human' FROM content_items LIMIT 1"
    ),
    "insert approvals": (
        "INSERT INTO content_approvals (id, content_item_id, revision_id, "
        "decision, actor_user_id) SELECT gen_random_uuid(), "
        "content_item_id, id, 'approved', (SELECT id FROM users LIMIT 1) "
        "FROM content_revisions LIMIT 1"
    ),
    "delete links": "DELETE FROM content_publication_links",
    # project, membership and identity
    "update projects": "UPDATE projects SET is_active = false",
    "update project settings": "UPDATE project_settings SET version = version + 1",
    "grant membership": "UPDATE project_memberships SET role = 'owner'",
    "insert membership": (
        "INSERT INTO project_memberships (id, project_id, user_id, role) "
        "SELECT gen_random_uuid(), 'test-project', id, 'owner' FROM users LIMIT 1"
    ),
    "reactivate user": "UPDATE users SET is_active = true",
    "set a password": "UPDATE users SET password_hash = 'x'",
    "change email": "UPDATE users SET email = 'x@example.com'",
    "insert user": (
        "INSERT INTO users (id, email, password_hash, display_name) "
        "VALUES (gen_random_uuid(), 'new@example.com', 'h', 'n')"
    ),
    "delete users": "DELETE FROM users",
    "delete sessions": "DELETE FROM user_sessions",
    "extend session": "UPDATE user_sessions SET expires_at = now() + interval '1 year'",
    "steal session": "UPDATE user_sessions SET user_id = user_id",
    # SMM-025B: accounts and sealed credentials
    "read key material": "SELECT ciphertext, nonce FROM social_account_credentials",
    "read whole credential rows": "SELECT * FROM social_account_credentials",
    "repoint an account": "UPDATE social_accounts SET external_account_id = 'x'",
    "insert account": (
        "INSERT INTO social_accounts (id, project_id, platform, display_name) "
        "VALUES (gen_random_uuid(), 'test-project', 'threads', 'x')"
    ),
    "swap credentials": (
        "UPDATE social_account_credentials SET social_account_id = social_account_id"
    ),
    "plant a credential": (
        "INSERT INTO social_account_credentials (id, social_account_id, status, "
        "scheme, key_id, ephemeral_public_key, nonce, ciphertext, fingerprint) "
        "SELECT gen_random_uuid(), id, 'superseded', 's', 'k', NULL, NULL, NULL, "
        "'000000000000' FROM social_accounts LIMIT 1"
    ),
    "route a publication": ("UPDATE publications SET social_account_id = NULL"),
    "declare account routing": (
        "UPDATE publications SET status = 'claimed' WHERE social_account_id IS NOT NULL"
    ),
    # the audit trail
    "update audit": "UPDATE audit_log SET action = 'x'",
    "delete audit": "DELETE FROM audit_log",
    "truncate audit": "TRUNCATE audit_log",
    "backdate audit": (
        "INSERT INTO audit_log (at, actor, action) VALUES ('2000-01-01', 'a', 'b')"
    ),
    "choose audit id": "INSERT INTO audit_log (id, actor, action) VALUES (1, 'a', 'b')",
    # DDL, other roles, trigger bypass
    "create table": "CREATE TABLE public.cc_probe (id int)",
    "create schema": "CREATE SCHEMA cc_probe",
    "alter table": "ALTER TABLE publications ADD COLUMN cc_probe int",
    "drop trigger": (
        "DROP TRIGGER trg_content_revisions_append_only ON content_revisions"
    ),
    "drop table": "DROP TABLE audit_log",
    "grant onwards": "GRANT SELECT ON projects TO PUBLIC",
    "become owner": "SET ROLE {owner}",
    "disable triggers": "SET session_replication_role = replica",
    "block the queue": "LOCK TABLE publications IN SHARE MODE",
}


@pytest.mark.parametrize("name", sorted(DENIED))
def test_postgresql_refuses_business_changes_from_the_api_role(
    seeded: World,
    engine: Engine,
    restricted_engine: Engine,
    owner_name: str,
    name: str,
) -> None:
    before = _fingerprint(engine)

    statement = DENIED[name].format(owner=owner_name)

    with restricted_engine.connect() as connection:
        transaction = connection.begin()

        try:
            if name == "grant onwards":
                # Without the grant option PostgreSQL warns rather than
                # fails; what matters is that nothing was granted.
                connection.execute(text(statement))
                transaction.rollback()
                transaction = None
            else:
                with pytest.raises(DBAPIError) as raised:
                    connection.execute(text(statement))

                assert _sqlstate(raised.value) == INSUFFICIENT_PRIVILEGE, raised.value
        finally:
            if transaction is not None:
                transaction.rollback()

    assert _fingerprint(engine) == before


def test_the_api_role_cannot_grant_what_it_holds(
    api_role, admin_database_url: str
) -> None:
    """No grant option: the 'grant onwards' statement above changed nothing."""

    assert pg_roles.query_admin(
        admin_database_url,
        "SELECT count(*) FROM pg_class c, aclexplode(c.relacl) a "
        " WHERE c.relnamespace = 'public'::regnamespace "
        "   AND (a.is_grantable OR a.grantee = 0) "
        "   AND a.grantor = to_regrole('ai_smm_cc')",
    ) == [(0,)]


def test_the_api_role_cannot_read_the_role_catalog(
    restricted_engine: Engine,
) -> None:
    with (
        restricted_engine.connect() as connection,
        pytest.raises(ProgrammingError) as raised,
    ):
        connection.execute(text("SELECT * FROM pg_authid"))

    assert _sqlstate(raised.value) == INSUFFICIENT_PRIVILEGE


# -- the API through the role, production configuration ----------------------


@pytest.fixture
def production_settings(api_settings: Settings) -> Settings:
    """As compose.control-center.yml: read-only for business operations,
    and no sign-in of its own (smm-auth's, SMM-025D)."""

    return replace(
        api_settings, api_mutations_enabled=False, api_auth_routes_enabled=False
    )


@pytest.fixture
def sign_in(session_factory, api_settings: Settings):
    """Sign a user in as smm-auth does and give `client` the cookie.

    The role itself can no longer create a session (grants G2); this is
    the session smm_auth.session_create would have made.
    """

    from ai_smm.application.auth import SESSION_COOKIE_NAME, csrf_token_for
    from tests.mutation_role.conftest import _open_session

    def do(client: TestClient, user: User) -> str:
        token = _open_session(session_factory, api_settings, user, TEST_PASSWORD)
        client.cookies.set(SESSION_COOKIE_NAME, token)

        return csrf_token_for(token)

    return do


@pytest.fixture
def cc_client(production_settings: Settings, restricted_session_factory):
    """The packaged configuration, connected as ai_smm_cc.

    A server error is answered (500) rather than re-raised into the test,
    as a real client would see it; tests that expect success assert 2xx.
    """

    app = create_app(
        production_settings,
        session_factory=restricted_session_factory,
        access_log=False,
    )

    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def _login(client: TestClient, user: User, password: str = TEST_PASSWORD):
    return client.post(
        "/api/v1/auth/login", json={"email": user.email, "password": password}
    )


def _read_paths(seeded: World, project: str, session: Session) -> list[str]:
    from ai_smm.db.models import ContentSeries, Publication

    series_id = session.scalars(select(ContentSeries.id)).first()
    attempted = session.scalars(select(PublicationAttempt.publication_id)).first()
    publication = session.scalars(select(Publication.id)).first()

    return [
        "/health/live",
        "/health/ready",
        "/api/v1/auth/me",
        "/api/v1/auth/csrf",
        "/api/v1/projects",
        f"/api/v1/projects/{project}",
        f"/api/v1/projects/{project}/settings",
        f"/api/v1/projects/{project}/publications",
        f"/api/v1/projects/{project}/publications?status=scheduled&human_reviewed=true",
        f"/api/v1/publications/{publication}",
        f"/api/v1/publications/{seeded.scheduled_publication}/preview",
        f"/api/v1/publications/{attempted}/attempts",
        f"/api/v1/publications/{attempted}/preview",
        f"/api/v1/projects/{project}/series",
        f"/api/v1/series/{series_id}",
        f"/api/v1/projects/{project}/content-items",
        f"/api/v1/content-items/{seeded.approved_item}",
        f"/api/v1/content-items/{seeded.approved_item}/revisions",
        f"/api/v1/content-items/{seeded.approved_item}/approvals",
        f"/api/v1/projects/{project}/operations/summary",
        f"/api/v1/projects/{project}/operations/attention",
        f"/api/v1/projects/{project}/audit",
        f"/api/v1/projects/{project}/audit?action=project.seeded",
        # SMM-025B: account and credential metadata, never key material.
        f"/api/v1/projects/{project}/social-accounts",
        f"/api/v1/social-accounts/{seeded.account}",
    ]


def test_the_session_lifecycle_and_every_read_work_through_the_role(
    cc_client: TestClient,
    seeded: World,
    project: str,
    session: Session,
    member,
    sign_in,
) -> None:
    owner = member(MembershipRole.OWNER)
    csrf = sign_in(cc_client, owner)

    for path in _read_paths(seeded, project, session):
        response = cc_client.get(path)

        assert response.status_code == 200, (path, response.text)

    me = cc_client.get("/api/v1/auth/me").json()
    assert me["control_plane"] == {"mutations_enabled": False}
    assert cc_client.get("/api/v1/auth/csrf").json()["csrf_token"] == csrf

    # Sign-out is smm-auth's: the packaged API has no such route.
    response = cc_client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf})
    assert response.status_code in {404, 405}, response.text
    assert cc_client.get("/api/v1/auth/me").status_code == 200

    # What the role wrote, read back as the owner: last_seen_at, nothing else.
    session.expire_all()
    user_session = session.scalars(
        select(UserSession).where(UserSession.user_id == owner.id)
    ).one()
    actions = session.scalars(
        select(AuditLog.action).where(AuditLog.subject == f"user:{owner.id}")
    ).all()

    assert user_session.revoked_at is None
    assert user_session.last_seen_at > user_session.created_at
    assert sorted(actions) == ["auth.login"]  # smm-auth's


@pytest.mark.parametrize("path", ["/api/v1/auth/login", "/api/v1/auth/logout"])
def test_the_packaged_api_serves_no_sign_in(
    cc_client: TestClient, session: Session, make_user, path: str
) -> None:
    """SMM-025D: nginx sends these paths to smm-auth; a request that reached
    smm-api anyway finds no route, and nothing is written."""

    user = make_user()
    response = cc_client.post(
        path, json={"email": user.email, "password": TEST_PASSWORD}
    )

    assert response.status_code in {404, 405}
    session.expire_all()
    assert session.scalars(select(UserSession)).all() == []
    assert session.scalars(select(AuditLog)).all() == []


@pytest.mark.parametrize(
    ("role", "name"),
    [
        (role, name)
        for role in (MembershipRole.OWNER, MembershipRole.VIEWER)
        for name in sorted(GATED)
    ],
)
def test_the_gate_still_answers_first_through_the_role(
    cc_client: TestClient,
    seeded: World,
    project: str,
    engine: Engine,
    member,
    role: MembershipRole,
    name: str,
    sign_in,
) -> None:
    csrf = sign_in(cc_client, member(role))
    before = _fingerprint(engine)

    response = _request(cc_client, csrf, name, seeded, project)

    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "MUTATIONS_DISABLED"
    assert _fingerprint(engine) == before


# -- defence in depth: the gate switched off, PostgreSQL still refuses ---------


@pytest.fixture
def ungated_client(production_settings: Settings, restricted_session_factory):
    """mutations ON (a misconfiguration, or a gate bypass) + the role.

    raise_server_exceptions=False: the test asserts the 500 the client
    receives, not the exception the test client would re-raise.
    """

    from ai_smm.security.credentials import encode_public_key, generate_key_pair

    app = create_app(
        replace(
            production_settings,
            api_mutations_enabled=True,
            # So a credential request reaches the database instead of
            # stopping at "not configured": the database must refuse it.
            credentials_public_key=encode_public_key(generate_key_pair()[1]),
        ),
        session_factory=restricted_session_factory,
        access_log=False,
    )

    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.mark.parametrize("name", sorted(GATED))
def test_every_business_command_fails_in_the_database_without_the_gate(
    ungated_client: TestClient,
    seeded: World,
    project: str,
    engine: Engine,
    member,
    name: str,
    sign_in,
) -> None:
    csrf = sign_in(ungated_client, member(MembershipRole.OWNER))
    before = _fingerprint(engine)

    response = _request(ungated_client, csrf, name, seeded, project)

    assert response.status_code == 500, response.text
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    # The whole request rolled back: not even the session's last_seen_at.
    assert _fingerprint(engine) == before


# -- each write the role holds is needed ---------------------------------------


def _flow_authenticated_read(client, user, csrf):
    return client.get("/api/v1/auth/me")


def _flow_audit_view(client, user, csrf):
    return client.get("/api/v1/projects/test-project/audit")


def _flow_publications(client, user, csrf):
    return client.get("/api/v1/projects/test-project/publications")


def _flow_series(client, user, csrf):
    return client.get("/api/v1/projects/test-project/series")


def _flow_content(client, user, csrf):
    return client.get("/api/v1/projects/test-project/content-items")


#: (what is taken away, the flow that must then break, needs the user
#: signed in first). Restored by re-running the grants file. The same
#: flow with every grant in place is the control, run first.
NECESSITY = {
    # SMM-025D, G2: the user columns the session check reads.
    "select users columns": (
        "REVOKE SELECT (email) ON users FROM ai_smm_cc",
        _flow_authenticated_read,
        True,
    ),
    "select user_sessions": (
        "REVOKE SELECT ON user_sessions FROM ai_smm_cc",
        _flow_authenticated_read,
        True,
    ),
    "update user_sessions.last_seen_at": (
        "REVOKE UPDATE (last_seen_at) ON user_sessions FROM ai_smm_cc",
        _flow_authenticated_read,
        True,
    ),
    "select audit_log": (
        "REVOKE SELECT ON audit_log FROM ai_smm_cc",
        _flow_audit_view,
        True,
    ),
    "select project_memberships": (
        "REVOKE SELECT ON project_memberships FROM ai_smm_cc",
        _flow_publications,
        True,
    ),
    "select publications": (
        "REVOKE SELECT ON publications FROM ai_smm_cc",
        _flow_publications,
        True,
    ),
    "select content_series": (
        "REVOKE SELECT ON content_series FROM ai_smm_cc",
        _flow_series,
        True,
    ),
    "select content_items": (
        "REVOKE SELECT ON content_items FROM ai_smm_cc",
        _flow_content,
        True,
    ),
}


@pytest.mark.parametrize("name", sorted(NECESSITY))
def test_each_grant_is_needed(
    cc_client: TestClient,
    seeded: World,
    member,
    admin_database_url: str,
    restore_grants,
    name: str,
    sign_in,
) -> None:
    revoke, flow, signed_in = NECESSITY[name]
    user = member(MembershipRole.OWNER)
    csrf = sign_in(cc_client, user) if signed_in else None

    # Control: the same flow with every grant in place.
    assert flow(cc_client, user, csrf).status_code == 200

    pg_roles.run_admin(admin_database_url, revoke)

    response = flow(cc_client, user, csrf)

    assert response.status_code == 500, (name, response.status_code, response.text)


# -- session security: what the database does and does not guarantee -------
# SMM-025D, grants G2: sessions are smm-auth's. These tests pin down what
# the role can no longer do, and the one thing it still can (RS-2).


def test_the_session_table_has_only_these_constraints(
    api_role, admin_database_url: str
) -> None:
    rows = pg_roles.query_admin(
        admin_database_url,
        "SELECT contype, pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conrelid = 'public.user_sessions'::regclass ORDER BY 1, 2",
    )

    assert rows == [
        ("f", "FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE"),
        ("p", "PRIMARY KEY (id)"),
        ("u", "UNIQUE (token_hash)"),
    ]
    # No CHECK: nothing in the database bounds expires_at or the timestamps.
    assert pg_roles.query_admin(
        admin_database_url,
        "SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal "
        "AND tgrelid = 'public.user_sessions'::regclass",
    ) == [(0,)]


def _forge_session(
    restricted_engine: Engine, *, user_id, token: str, **columns: object
) -> None:
    from ai_smm.security.tokens import hash_token

    values = {
        "id": uuid4(),
        "user_id": user_id,
        "token_hash": hash_token(token),
        "created_at": datetime.now(timezone.utc),
        "last_seen_at": datetime.now(timezone.utc),
        "expires_at": datetime.now(timezone.utc) + timedelta(days=3650),
        **columns,
    }

    with restricted_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO user_sessions (id, user_id, token_hash, "
                "created_at, last_seen_at, expires_at) VALUES (:id, :user_id, "
                ":token_hash, :created_at, :last_seen_at, :expires_at)"
            ),
            values,
        )


def _session_anomalies(admin_database_url: str) -> list[str]:
    return [
        row[0]
        for row in pg_roles.query_admin(
            admin_database_url,
            (pg_roles.DB_DIR / "session-anomalies.sql").read_text(encoding="utf-8"),
        )
    ]


def test_closed_risk_the_role_cannot_mint_a_session(
    restricted_engine: Engine, session: Session, member
) -> None:
    """CLOSED by SMM-025D (FU-024C2-1), was a residual risk: a compromised
    API connection could create a session for any user. Now only
    smm_auth.session_create (as ai_smm_auth) can."""

    victim = member(MembershipRole.OWNER)

    with pytest.raises(DBAPIError) as raised:
        _forge_session(restricted_engine, user_id=victim.id, token="forged-token")

    assert _sqlstate(raised.value) == INSUFFICIENT_PRIVILEGE
    session.expire_all()
    assert session.scalars(select(UserSession)).all() == []


def test_what_the_session_constraints_actually_stop(engine: Engine, member) -> None:
    """For the role that may still insert (the owner): only these two."""

    from ai_smm.security.tokens import generate_token

    user = member(MembershipRole.VIEWER)

    with pytest.raises(DBAPIError) as missing_user:
        _forge_session(engine, user_id=uuid4(), token=generate_token())

    assert _sqlstate(missing_user.value) == "23503"  # foreign_key_violation

    token = generate_token()
    _forge_session(engine, user_id=user.id, token=token)

    with pytest.raises(DBAPIError) as duplicate:
        _forge_session(engine, user_id=user.id, token=token)

    assert _sqlstate(duplicate.value) == "23505"  # unique_violation


def test_residual_risk_the_role_can_refresh_but_not_revoke_any_session(
    restricted_engine: Engine, engine: Engine, member
) -> None:
    """RISK, narrowed (ADR-024C-3A RS-2): UPDATE (last_seen_at) is not limited
    to one's own rows, so an idle session can be kept alive -- never past its
    expiry, and useless without its raw token. Revoking (or un-revoking) is
    CLOSED by G2: only smm_auth.session_revoke can."""

    from ai_smm.security.tokens import generate_token

    for role in (MembershipRole.OWNER, MembershipRole.VIEWER):
        _forge_session(
            engine,
            user_id=member(role).id,
            token=generate_token(),
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        )

    with restricted_engine.begin() as connection:
        refreshed = connection.execute(
            text("UPDATE user_sessions SET last_seen_at = now()")
        ).rowcount

    assert refreshed == 2

    for statement in (
        "UPDATE user_sessions SET revoked_at = now()",
        "UPDATE user_sessions SET revoked_at = NULL",
        "UPDATE user_sessions SET expires_at = now() + interval '1 year'",
    ):
        with (
            restricted_engine.connect() as connection,
            pytest.raises(DBAPIError) as raised,
        ):
            connection.execute(text(statement))

        assert _sqlstate(raised.value) == INSUFFICIENT_PRIVILEGE, statement


def test_a_real_login_leaves_no_session_anomaly(
    cc_client: TestClient, admin_database_url: str, member, sign_in
) -> None:
    sign_in(cc_client, member(MembershipRole.OWNER))
    assert cc_client.get("/api/v1/auth/me").status_code == 200

    assert _session_anomalies(admin_database_url) == ["END|anomalies|0"]


# -- audit integrity: three different properties ------------------------------


def test_the_role_writes_no_audit_entry_at_all(
    restricted_engine: Engine, session: Session
) -> None:
    """GUARANTEED (SMM-025D, G2 and FU-024C2-2): no entry of any kind -- not
    a worker's, an operator's, a business action, nor a sign-in (those are
    smm-auth's, written inside smm_auth functions). Before G2, the
    database guard already confined a role like this one to auth.*."""

    for actor, action in (
        ("worker:worker-1", "publication.published"),
        ("cli:operator", "rescheduled"),
        ("user:x", "content.approved"),
        ("user:someone-else", "auth.login"),
        ("anonymous", "auth.login_failed"),
    ):
        with (
            restricted_engine.connect() as connection,
            pytest.raises(DBAPIError) as raised,
        ):
            connection.execute(
                text(
                    "INSERT INTO audit_log (actor, action, subject, details) "
                    "VALUES (:actor, :action, 'publication:1', '{}')"
                ),
                {"actor": actor, "action": action},
            )

        assert _sqlstate(raised.value) == INSUFFICIENT_PRIVILEGE

    assert session.scalars(select(AuditLog.actor)).all() == []


def test_existing_audit_entries_cannot_be_changed_by_the_role(
    restricted_engine: Engine, session: Session
) -> None:
    """GUARANTEED for this role: no UPDATE, DELETE or TRUNCATE (append-only).

    The owner can still change the table: this is not immutability of the
    log as such, only towards the API.
    """

    record_audit(session, actor="cli:test", action="project.updated")
    session.commit()

    for statement in (
        "UPDATE audit_log SET actor = 'x'",
        "UPDATE audit_log SET at = now() - interval '1 day'",
        "DELETE FROM audit_log",
        "TRUNCATE audit_log",
    ):
        with (
            restricted_engine.connect() as connection,
            pytest.raises(DBAPIError) as raised,
        ):
            connection.execute(text(statement))

        assert _sqlstate(raised.value) == INSUFFICIENT_PRIVILEGE, statement

    session.expire_all()
    assert session.scalars(select(AuditLog.actor)).all() == ["cli:test"]


# -- an outdated Argon2 hash: the optional upgrade, isolated ----------------


def _outdated_hash(password: str = TEST_PASSWORD) -> str:
    """A real Argon2id hash with weaker parameters than the current ones."""

    from argon2 import PasswordHasher, Type

    from ai_smm.security.passwords import password_needs_rehash

    weak = PasswordHasher(
        type=Type.ID, time_cost=1, memory_cost=8 * 1024, parallelism=1
    ).hash(password)

    assert password_needs_rehash(weak)  # the precondition, not mocked

    return weak


def _client_for(settings: Settings, factory) -> TestClient:
    """An app built inside the test, so its log handler writes to capsys."""

    app = create_app(settings, session_factory=factory, access_log=False)

    return TestClient(app, raise_server_exceptions=False)


def test_an_outdated_hash_is_still_upgraded_where_the_role_may(
    production_settings: Settings,
    session_factory,
    session: Session,
    member,
) -> None:
    """The owner connection (deployed today; the CLI) behaves as before."""

    owner = member(MembershipRole.OWNER)
    old_hash = _outdated_hash()
    session.execute(
        text("UPDATE users SET password_hash = :h WHERE id = :id"),
        {"h": old_hash, "id": owner.id},
    )
    session.commit()

    with _client_for(
        replace(production_settings, api_auth_routes_enabled=True), session_factory
    ) as client:
        assert _login(client, owner).status_code == 200

    from ai_smm.security.passwords import password_needs_rehash, verify_password

    session.expire_all()
    stored = session.get(User, owner.id).password_hash

    assert stored != old_hash
    assert not password_needs_rehash(stored)
    assert verify_password(stored, TEST_PASSWORD)


@pytest.mark.parametrize(
    "errcode", ["raise_exception", "lock_not_available", "check_violation"]
)
def test_any_other_error_during_the_upgrade_still_fails_the_login(
    production_settings: Settings,
    session_factory,
    engine: Engine,
    session: Session,
    member,
    errcode: str,
) -> None:
    """Only insufficient_privilege is absorbed; nothing else is swallowed.

    A test-only trigger, created by the owner on the disposable database
    and dropped again, makes the hash write fail with another SQLSTATE.
    """

    owner = member(MembershipRole.OWNER)
    session.execute(
        text("UPDATE users SET password_hash = :h WHERE id = :id"),
        {"h": _outdated_hash(), "id": owner.id},
    )
    session.commit()

    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE FUNCTION test_refuse_hash() RETURNS trigger LANGUAGE plpgsql "
                "AS $$ BEGIN RAISE EXCEPTION 'test refusal' "
                f"USING ERRCODE = '{errcode}'; END $$"
            )
        )
        connection.execute(
            text(
                "CREATE TRIGGER test_refuse_hash BEFORE UPDATE OF password_hash "
                "ON users FOR EACH ROW EXECUTE FUNCTION test_refuse_hash()"
            )
        )

    try:
        with _client_for(
            replace(production_settings, api_auth_routes_enabled=True),
            session_factory,
        ) as client:
            response = _login(client, owner)
    finally:
        with engine.begin() as connection:
            connection.execute(text("DROP TRIGGER test_refuse_hash ON users"))
            connection.execute(text("DROP FUNCTION test_refuse_hash()"))

    assert response.status_code == 500, response.text

    session.expire_all()
    # The whole login rolled back: no session, no login audit.
    assert (
        session.scalars(
            select(UserSession).where(UserSession.user_id == owner.id)
        ).all()
        == []
    )
    assert (
        session.scalars(
            select(AuditLog.action).where(AuditLog.subject == f"user:{owner.id}")
        ).all()
        == []
    )


# -- the database unavailable, or the credentials wrong ------------------------


@pytest.mark.parametrize("failure", ["unreachable", "wrong password", "nologin"])
def test_the_api_fails_closed_without_its_database(
    production_settings: Settings,
    api_role,
    admin_database_url: str,
    capsys,
    failure: str,
) -> None:
    url = make_url(api_role.url)
    secret = url.password

    if failure == "unreachable":
        # A loopback port nothing listens on.
        url = url.set(host="127.0.0.1", port=9)
    elif failure == "wrong password":
        url = url.set(password="not-" + secret)

    engine = create_db_engine(
        url.render_as_string(hide_password=False),
        settings=production_settings,
        pool_size=1,
        max_overflow=0,
        connect_args={"connect_timeout": 3},
    )

    try:
        if failure == "nologin":
            # What the role looks like after rollback stage 2 began.
            pg_roles.run_admin(admin_database_url, "ALTER ROLE ai_smm_cc NOLOGIN")

        app = create_app(
            production_settings,
            session_factory=sessionmaker(bind=engine, expire_on_commit=False),
            access_log=False,
        )

        from ai_smm.application.auth import SESSION_COOKIE_NAME
        from ai_smm.security.tokens import generate_token

        with TestClient(app, raise_server_exceptions=False) as client:
            live = client.get("/health/live")
            ready = client.get("/health/ready")
            # The session check needs the database (sign-in is smm-auth's).
            client.cookies.set(SESSION_COOKIE_NAME, generate_token())
            login = client.get("/api/v1/auth/me")
    finally:
        engine.dispose()

        if failure == "nologin":
            pg_roles.run_admin(admin_database_url, "ALTER ROLE ai_smm_cc LOGIN")

    assert live.status_code == 200
    assert ready.status_code == 503
    assert ready.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
    # Not a 401: a broken database is not a missing session.
    assert login.status_code == 500
    assert login.json()["error"]["code"] == "INTERNAL_ERROR"

    # The process log (stdout, JSON, as in the container) recorded the
    # failure -- and neither the password nor the connection string.
    log = capsys.readouterr().out

    assert "Readiness probe failed" in log
    assert "Unhandled API error" in log

    for output in (ready.text, login.text, log):
        assert secret not in output
        assert api_role.url not in output

    assert "ai_smm_cc" not in ready.text + login.text


# -- the migration tooling neither needs nor can use the role ----------------


def _alembic_config(url: str) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url)

    return config


@pytest.fixture
def migration_db(database_url: str, admin_database_url: str, api_role):
    """A migrated copy of the schema, owned like production's."""

    name = "ai_smm_lp_migration_test"
    owner = create_engine(database_url, isolation_level="AUTOCOMMIT")

    with owner.connect() as connection:
        drop_scratch_database(connection, name)
        connection.execute(text(f'CREATE DATABASE "{name}"'))

    yield (
        make_url(database_url).set(database=name).render_as_string(hide_password=False),
        make_url(admin_database_url)
        .set(database=name)
        .render_as_string(hide_password=False),
        make_url(api_role.url).set(database=name).render_as_string(hide_password=False),
    )

    with owner.connect() as connection:
        drop_scratch_database(connection, name)

    owner.dispose()


def test_migrations_run_as_the_owner_alongside_the_role(
    migration_db, monkeypatch
) -> None:
    owner_url, admin_url, role_url = migration_db
    monkeypatch.setenv("AI_SMM_DATABASE_URL", owner_url)

    command.upgrade(_alembic_config(owner_url), "head")
    pg_roles.run_script(admin_url, pg_roles.GRANTS_SQL)

    assert pg_roles.failures(pg_roles.verify(admin_url)) == []
    assert "PASS|table.alembic_version.select|denied" in pg_roles.verify(admin_url)

    # The role can neither read the migration state nor run a migration.
    monkeypatch.setenv("AI_SMM_DATABASE_URL", role_url)

    with pytest.raises(ProgrammingError) as raised:
        command.upgrade(_alembic_config(role_url), "head")

    assert _sqlstate(raised.value) == INSUFFICIENT_PRIVILEGE

    # The owner's DDL is not hindered by the role's grants: a downgrade
    # drops tables the role can read, the upgrade recreates them. Down to
    # the editorial revision, through the guards (c4d81e2a9f63, which needs
    # its switch) and the social accounts (a7c2e5f19b40).
    monkeypatch.setenv("AI_SMM_DATABASE_URL", owner_url)
    monkeypatch.setenv("AI_SMM_DOWNGRADE_REMOVES_MUTATION_GUARDS", "yes")
    command.downgrade(_alembic_config(owner_url), "3d4ac1986f07")
    command.upgrade(_alembic_config(owner_url), "head")

    # A recreated table carries no grant (no default privileges, by
    # design): the API would fail closed on it, the verify file says
    # which, and re-running the grants file is the repair.
    lost = pg_roles.failures(pg_roles.verify(admin_url))

    # a7c2e5f19b40's tables are among those dropped and recreated.
    assert "FAIL|table.social_accounts.select|granted" in lost
    assert all(".select|granted" in line or "matrix." in line for line in lost)

    pg_roles.run_script(admin_url, pg_roles.GRANTS_SQL)

    assert pg_roles.failures(pg_roles.verify(admin_url)) == []


@pytest.mark.parametrize("held_for", [1.0, None])
def test_dropping_a_scratch_database_outlasts_a_session_it_may_not_end(
    database_url: str, admin_database_url: str, held_for: float | None
) -> None:
    """The race behind the intermittent DROP ... WITH (FORCE) failures.

    A session the owner may not terminate -- an autovacuum worker in the
    real runs, a superuser's session here, deterministically -- makes FORCE
    fail with insufficient_privilege. drop_scratch_database retries only
    that, briefly: a session that goes away is outlasted, one that stays
    still fails the test.
    """

    import threading
    import time

    name = "ai_smm_lp_drop_probe_test"
    owner = create_engine(database_url, isolation_level="AUTOCOMMIT")
    holder_url = pg_roles.libpq_url(
        make_url(admin_database_url)
        .set(database=name)
        .render_as_string(hide_password=False)
    )

    with owner.connect() as connection:
        drop_scratch_database(connection, name)
        connection.execute(text(f'CREATE DATABASE "{name}"'))

    held = psycopg.connect(holder_url, autocommit=True)

    if held_for is not None:
        threading.Timer(held_for, held.close).start()

    started = time.monotonic()

    try:
        with owner.connect() as connection:
            if held_for is None:
                with pytest.raises(DBAPIError) as raised:
                    drop_scratch_database(connection, name, attempts=3)

                assert _sqlstate(raised.value) == INSUFFICIENT_PRIVILEGE
            else:
                drop_scratch_database(connection, name)

                assert time.monotonic() - started >= held_for
    finally:
        held.close()

        with owner.connect() as connection:
            drop_scratch_database(connection, name)

        owner.dispose()


def test_the_alembic_environment_never_reads_the_api_credentials() -> None:
    """migrate keeps the owner URL; nothing in alembic/ knows ai_smm_cc."""

    for path in (PROJECT_ROOT / "alembic").rglob("*.py"):
        assert "ai_smm_cc" not in path.read_text(encoding="utf-8"), path


def test_no_credential_for_the_role_is_committed() -> None:
    """The role's password is set with \\password, never written to a file."""

    for path in pg_roles.DB_DIR.glob("*.sql"):
        content = path.read_text(encoding="utf-8").upper()

        assert "PASSWORD '" not in content, path
        assert "ENCRYPTED PASSWORD" not in content, path
