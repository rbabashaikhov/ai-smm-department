"""The PostgreSQL authentication primitives of smm-auth (SMM-024C-3B-1).

docs/adr/ADR-024C-3A-auth-service-isolation.md moves login and logout
into an isolated service with its own role, ai_smm_auth, which writes only
through SECURITY DEFINER functions in schema smm_auth. This suite proves
those contracts on a real PostgreSQL, installed by the operator's files
(deploy/control-center/db/smm_auth.functions.sql, ai_smm_auth.grants.sql)
and removed by their rollback (ai_smm_auth.revoke.sql, smm_auth.drop.sql):

* the role and the functions are exactly what the files say -- checked
  by ai_smm_auth.verify.sql, which pins every function body;
* the token digest is the API's, byte for byte, and a session made by
  either side is accepted, refused and revoked identically by the other;
* nothing the caller sends becomes a timestamp, an expiry or an actor;
* session and audit row are written together or not at all;
* ai_smm_cc, PUBLIC and any other role cannot use any of it, and
  ai_smm_auth can do nothing else -- no table write, no password change;
* ai_smm_cc's G1 grants, the schema, alembic_version and every other
  role are left exactly as they were, and the rollback restores the rest.

Fixtures seed data as the owner. The functions are called as ai_smm_auth
over plain psycopg connections (no pool, so the role can be retired
between tests). Needs AI_SMM_TEST_ADMIN_DATABASE_URL, like the API role.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic.config import Config
from psycopg import sql as pgsql
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from ai_smm.application.auth import create_session, resolve_session, revoke_session
from ai_smm.config import Settings
from ai_smm.db.models import UserSession
from ai_smm.security.tokens import generate_token, hash_token
from alembic import command
from tests import pg_roles
from tests.conftest import drop_scratch_database


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ANOMALIES_SQL = pg_roles.DB_DIR / "session-anomalies.sql"

INSUFFICIENT_PRIVILEGE = "42501"
INVALID_PARAMETER_VALUE = "22023"

#: The contracts ai_smm_auth may execute, with one valid argument list each.
CONTRACTS = {
    "smm_auth.session_create(uuid,text)": (
        "SELECT * FROM smm_auth.session_create(%s, %s)"
    ),
    "smm_auth.session_resolve(text)": "SELECT * FROM smm_auth.session_resolve(%s)",
    "smm_auth.session_revoke(text)": "SELECT * FROM smm_auth.session_revoke(%s)",
    "smm_auth.record_login_failure(text)": "SELECT smm_auth.record_login_failure(%s)",
    "smm_auth.recent_login_failures(text)": "SELECT smm_auth.recent_login_failures(%s)",
    # SMM-025D: the step-up approval
    "smm_auth.approval_preview(text,uuid)": (
        "SELECT * FROM smm_auth.approval_preview(%s, %s)"
    ),
    "smm_auth.approval_grant_issue(text,uuid)": (
        "SELECT * FROM smm_auth.approval_grant_issue(%s, %s)"
    ),
}
#: Internal helpers: the owner only.
HELPERS = {
    "smm_auth.token_digest(text)": "SELECT smm_auth.token_digest(%s)",
    "smm_auth.failure_subject(text)": "SELECT smm_auth.failure_subject(%s)",
}

#: Tables the auth functions never touch.
BUSINESS_TABLES = (
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
)


def _args_for(signature: str, user_id: UUID) -> tuple:
    if signature.endswith("(uuid,text)"):
        return (user_id, generate_token())

    if signature.endswith("(text,uuid)"):
        return (generate_token(), uuid4())

    if "login_failure" in signature or "failure_subject" in signature:
        return ("someone@example.com",)

    return (generate_token(),)


# -- connections -----------------------------------------------------------------


@dataclass
class AuthStack:
    url: str
    password: str
    baseline_except_auth: list[str]
    cc_verify_before: list[str]
    schema_before: list[tuple]


@contextmanager
def connect(url: str, *, autocommit: bool = True) -> Iterator[psycopg.Connection]:
    with psycopg.connect(pg_roles.libpq_url(url), autocommit=autocommit) as conn:
        yield conn


def _sqlstate(exc: BaseException) -> str | None:
    return getattr(getattr(exc, "orig", exc), "sqlstate", None)


def _refused(
    conn: psycopg.Connection, statement: str, params: tuple = ()
) -> str | None:
    """Run a statement that must fail; return its SQLSTATE."""

    try:
        with conn.transaction():
            conn.execute(statement, params)
    except psycopg.Error as exc:
        return exc.sqlstate

    return None


def schema_fingerprint(admin_url: str) -> list[tuple]:
    """Every table, column, constraint, index, trigger and function of
    schema public, plus alembic_version's content if it exists."""

    rows = pg_roles.query_admin(
        admin_url,
        """
        SELECT 'column', c.relname || '.' || a.attname,
               format_type(a.atttypid, a.atttypmod) || ' notnull=' || a.attnotnull
               || ' default=' || coalesce(pg_get_expr(d.adbin, d.adrelid), '')
          FROM pg_class c
          JOIN pg_attribute a
            ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
          LEFT JOIN pg_attrdef d ON d.adrelid = c.oid AND d.adnum = a.attnum
         WHERE c.relnamespace = 'public'::regnamespace
        UNION ALL
        SELECT 'constraint', conrelid::regclass::text || '.' || conname,
               pg_get_constraintdef(oid)
          FROM pg_constraint WHERE connamespace = 'public'::regnamespace
        UNION ALL
        SELECT 'index', indexrelid::regclass::text, pg_get_indexdef(indexrelid)
          FROM pg_index i JOIN pg_class c ON c.oid = i.indrelid
         WHERE c.relnamespace = 'public'::regnamespace
        UNION ALL
        SELECT 'trigger', tgrelid::regclass::text || '.' || tgname,
               pg_get_triggerdef(t.oid)
          FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
         WHERE c.relnamespace = 'public'::regnamespace AND NOT t.tgisinternal
        UNION ALL
        SELECT 'function', p.oid::regprocedure::text, md5(p.prosrc)
          FROM pg_proc p WHERE p.pronamespace = 'public'::regnamespace
        ORDER BY 1, 2, 3
        """,
    )

    if pg_roles.query_admin(
        admin_url, "SELECT to_regclass('public.alembic_version') IS NOT NULL"
    )[0][0]:
        rows += [
            ("alembic_version", row[0], "")
            for row in pg_roles.query_admin(
                admin_url, "SELECT version_num FROM alembic_version ORDER BY 1"
            )
        ]

    return rows


def business_fingerprint(admin_url: str) -> dict[str, str]:
    return {
        table: pg_roles.query_admin(
            admin_url,
            f"SELECT count(*)::text || ':' || coalesce(md5(string_agg(t::text, '|' "
            f"ORDER BY t::text)), '') FROM {table} t",
        )[0][0]
        for table in BUSINESS_TABLES
    }


# -- the stack, installed for this module and removed after it ---------------------


@pytest.fixture(scope="module")
def auth_stack(
    engine: Engine, api_role, admin_database_url: str, database_url: str
) -> Iterator[AuthStack]:
    """P3 as the operator runs it, on top of ai_smm_cc's G1 (api_role).

    Torn down by the P3 rollback, which must restore everything: no
    schema, no role, the snapshot of every other role -- ai_smm_cc
    included -- unchanged, and G1 still verifying.
    """

    present = pg_roles.query_admin(
        admin_database_url,
        "SELECT to_regnamespace('smm_auth') IS NOT NULL, "
        "to_regrole('ai_smm_auth') IS NOT NULL",
    )[0]

    assert present == (False, False), "stale smm_auth state from an earlier run"

    baseline = pg_roles.snapshot_except_auth(admin_database_url)
    cc_before = pg_roles.verify(admin_database_url)
    schema_before = schema_fingerprint(admin_database_url)

    assert pg_roles.failures(cc_before) == []

    pg_roles.install_auth(admin_database_url)
    password = pg_roles.set_auth_password(admin_database_url)

    yield AuthStack(
        url=pg_roles.auth_role_url(database_url, password),
        password=password,
        baseline_except_auth=baseline,
        cc_verify_before=cc_before,
        schema_before=schema_before,
    )

    pg_roles.uninstall_auth(admin_database_url)

    assert pg_roles.snapshot_except_auth(admin_database_url) == baseline
    assert pg_roles.failures(pg_roles.verify(admin_database_url)) == []
    assert pg_roles.query_admin(
        admin_database_url,
        "SELECT to_regnamespace('smm_auth') IS NULL "
        "AND to_regrole('ai_smm_auth') IS NULL",
    )[0][0]


@pytest.fixture
def auth(auth_stack: AuthStack) -> Iterator[psycopg.Connection]:
    with connect(auth_stack.url) as conn:
        yield conn


@pytest.fixture
def admin(admin_database_url: str) -> Iterator[psycopg.Connection]:
    with connect(admin_database_url) as conn:
        yield conn


@pytest.fixture
def user(make_user):
    return make_user(email="owner@example.com")


def _create(conn: psycopg.Connection, user_id: UUID, token: str) -> list[tuple]:
    return conn.execute(
        "SELECT session_id, expires_at FROM smm_auth.session_create(%s, %s)",
        (user_id, token),
    ).fetchall()


def _resolve(conn: psycopg.Connection, token: str) -> list[tuple]:
    return conn.execute(
        "SELECT session_id, user_id, expires_at FROM smm_auth.session_resolve(%s)",
        (token,),
    ).fetchall()


def _revoke(conn: psycopg.Connection, token: str) -> list[tuple]:
    return conn.execute(
        "SELECT session_id, user_id, revoked FROM smm_auth.session_revoke(%s)",
        (token,),
    ).fetchall()


def _record(conn: psycopg.Connection, email: str) -> int:
    return conn.execute(
        "SELECT smm_auth.record_login_failure(%s)", (email,)
    ).fetchone()[0]


def _recent(conn: psycopg.Connection, email: str) -> int:
    return conn.execute(
        "SELECT smm_auth.recent_login_failures(%s)", (email,)
    ).fetchone()[0]


def _session_row(admin: psycopg.Connection, session_id: UUID) -> dict:
    cur = admin.execute(
        "SELECT * FROM user_sessions WHERE id = %s", (session_id,)
    )
    names = [d.name for d in cur.description]
    row = cur.fetchone()

    return dict(zip(names, row, strict=True)) if row else {}


def _audit(admin: psycopg.Connection) -> list[tuple]:
    return admin.execute(
        "SELECT actor, action, subject, details, at FROM audit_log ORDER BY id"
    ).fetchall()


# -- the installation itself ------------------------------------------------------


def test_the_verify_file_reports_no_failure(auth_stack, admin_database_url) -> None:
    lines = pg_roles.verify_auth(admin_database_url)

    assert pg_roles.failures(lines) == []
    assert lines[-1] == f"END|checks|{len(lines) - 1}"

    for required in (
        "PASS|role.password|set, SCRAM-SHA-256",
        "PASS|role.member_of|none",
        "PASS|role.owns_nothing|0 owned objects",
        "PASS|schema.smm_auth.acl|owner and ai_smm_auth USAGE only",
        "PASS|schema.smm_auth.objects|designed functions only",
        "PASS|acl.relations|none",
        "PASS|acl.columns.unexpected|none",
        "PASS|column.users.password_hash.select|granted",
        "PASS|column.users.password_hash.update|denied",
        "PASS|table.users.select|denied",
        "PASS|table.user_sessions.insert|denied",
        "PASS|table.audit_log.insert|denied",
        "PASS|function.smm_auth.token_digest(text).execute|denied",
        "PASS|function.smm_auth.session_create(uuid,text).execute|granted",
        "PASS|function.smm_auth.session_create(uuid,text).public_execute|denied",
        "PASS|function.smm_auth.session_create(uuid,text).ai_smm_cc_execute|denied",
        "PASS|schema.smm_auth.ai_smm_cc_usage|denied",
    ):
        assert required in lines, required

    for signature in (*CONTRACTS, *HELPERS):
        assert any(
            line.startswith(f"PASS|function.{signature}.definition|") for line in lines
        ), signature


def test_the_verify_file_pins_the_bodies_of_the_functions_file(
    auth_stack, admin_database_url
) -> None:
    """A changed body in smm_auth.functions.sql must change its pin too."""

    installed = dict(
        pg_roles.query_admin(
            admin_database_url,
            "SELECT p.oid::regprocedure::text, md5(p.prosrc) FROM pg_proc p "
            "WHERE p.pronamespace = 'smm_auth'::regnamespace",
        )
    )
    verify_text = pg_roles.AUTH_VERIFY_SQL.read_text(encoding="utf-8")

    assert set(installed) == set(CONTRACTS) | set(HELPERS)

    for signature, digest in installed.items():
        assert f"('{signature}'," in verify_text, signature
        assert f"'{digest}')" in verify_text, signature


def test_the_role_and_its_connection_are_bounded(auth) -> None:
    row = auth.execute(
        "SELECT current_user, session_user, "
        "current_setting('statement_timeout'), current_setting('lock_timeout'), "
        "current_setting('idle_in_transaction_session_timeout'), "
        "current_setting('log_parameter_max_length'), "
        "current_setting('log_parameter_max_length_on_error'), "
        "current_setting('is_superuser')"
    ).fetchone()

    assert row == ("ai_smm_auth", "ai_smm_auth", "5s", "2s", "30s", "0", "0", "off")

    # A superuser-only setting: the role cannot turn parameter logging on.
    assert _refused(auth, "SET log_parameter_max_length = -1") == INSUFFICIENT_PRIVILEGE
    assert (
        _refused(auth, "ALTER ROLE ai_smm_auth SET log_parameter_max_length = -1")
        == INSUFFICIENT_PRIVILEGE
    )


def test_the_functions_are_hardened(auth_stack, admin_database_url) -> None:
    rows = pg_roles.query_admin(
        admin_database_url,
        """
        SELECT p.oid::regprocedure::text, p.prosecdef, p.proconfig,
               pg_get_userbyid(p.proowner) = pg_get_userbyid(
                   (SELECT relowner FROM pg_class
                     WHERE oid = 'public.users'::regclass)),
               has_function_privilege('public', p.oid, 'EXECUTE'),
               (SELECT rolsuper FROM pg_roles WHERE oid = p.proowner),
               p.prosrc ~* 'execute\\s|password_hash'
          FROM pg_proc p
         WHERE p.pronamespace = 'smm_auth'::regnamespace
        """,
    )

    assert {row[0] for row in rows} == set(CONTRACTS) | set(HELPERS)

    for signature, secdef, config, by_owner, public_exec, super_owner, risky in rows:
        assert secdef == (signature in CONTRACTS), signature
        assert config == ["search_path=pg_catalog, pg_temp"], signature
        assert by_owner and not super_owner, signature
        assert public_exec is False, signature
        # No dynamic SQL, and no function that even mentions password_hash.
        assert risky is False, signature


def test_installation_is_convergent(auth_stack, admin_database_url) -> None:
    """Re-running both files -- twice, and the functions file alone --
    changes nothing: same oids, owners, ACLs, verify, and G1."""

    def state() -> list[tuple]:
        return pg_roles.query_admin(
            admin_database_url,
            "SELECT p.oid, p.oid::regprocedure::text, "
            "pg_get_userbyid(p.proowner), p.proacl::text "
            "FROM pg_proc p WHERE p.pronamespace = 'smm_auth'::regnamespace "
            "UNION ALL SELECT n.oid, n.nspname, pg_get_userbyid(n.nspowner), "
            "n.nspacl::text FROM pg_namespace n WHERE n.nspname = 'smm_auth' "
            "ORDER BY 1",
        )

    before = state()
    verify_before = pg_roles.verify_auth(admin_database_url)

    pg_roles.install_auth(admin_database_url)
    pg_roles.run_script(admin_database_url, pg_roles.AUTH_FUNCTIONS_SQL)
    pg_roles.install_auth(admin_database_url)

    assert state() == before
    assert pg_roles.verify_auth(admin_database_url) == verify_before
    assert (
        pg_roles.snapshot_except_auth(admin_database_url)
        == auth_stack.baseline_except_auth
    )


# -- G1, the schema, alembic_version and every other role --------------------------


def test_ai_smm_cc_g1_and_every_other_role_are_unchanged(
    auth_stack, admin_database_url
) -> None:
    assert (
        pg_roles.snapshot_except_auth(admin_database_url)
        == auth_stack.baseline_except_auth
    )

    lines = pg_roles.verify(admin_database_url)

    assert pg_roles.failures(lines) == []
    # The one new line: the new schema, which ai_smm_cc cannot use.
    assert "PASS|schema.other.usage|smm_auth" in lines
    assert set(lines) - set(auth_stack.cc_verify_before) == {
        "PASS|schema.other.usage|smm_auth",
        f"END|checks|{len(lines) - 1}",
    }


def test_the_schema_is_unchanged(auth_stack, admin_database_url) -> None:
    assert schema_fingerprint(admin_database_url) == auth_stack.schema_before


def _alembic_config(url: str) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url)

    return config


def test_on_a_migrated_database_alembic_and_the_schema_are_untouched(
    auth_stack, database_url, admin_database_url, monkeypatch
) -> None:
    """The same files on the schema Alembic builds: alembic_version and every
    table, constraint, index, trigger and function stay as they are; the
    migrations still run with smm_auth present; G1 and GA both verify."""

    name = "ai_smm_auth_migration_test"
    owner = create_engine(database_url, isolation_level="AUTOCOMMIT")
    owner_url = make_url(database_url).set(database=name).render_as_string(
        hide_password=False
    )
    admin_url = make_url(admin_database_url).set(database=name).render_as_string(
        hide_password=False
    )

    with owner.connect() as connection:
        drop_scratch_database(connection, name)
        connection.execute(text(f'CREATE DATABASE "{name}"'))

    try:
        monkeypatch.setenv("AI_SMM_DATABASE_URL", owner_url)
        command.upgrade(_alembic_config(owner_url), "head")
        pg_roles.run_script(admin_url, pg_roles.GRANTS_SQL)

        before = schema_fingerprint(admin_url)

        assert any(row[0] == "alembic_version" for row in before)

        pg_roles.install_auth(admin_url)

        assert schema_fingerprint(admin_url) == before
        assert pg_roles.failures(pg_roles.verify(admin_url)) == []
        assert pg_roles.failures(pg_roles.verify_auth(admin_url)) == []

        # The drop refuses while the role exists anywhere on the instance.
        with pytest.raises(psycopg.Error, match="run ai_smm_auth.revoke.sql first"):
            pg_roles.run_script(admin_url, pg_roles.AUTH_DROP_SQL)

        # The owner's migrations are not hindered by the new schema.
        monkeypatch.setenv("AI_SMM_DOWNGRADE_REMOVES_MUTATION_GUARDS", "yes")
        command.downgrade(_alembic_config(owner_url), "-1")
        command.upgrade(_alembic_config(owner_url), "head")
        pg_roles.run_script(admin_url, pg_roles.GRANTS_SQL)

        assert schema_fingerprint(admin_url) == before
        assert pg_roles.failures(pg_roles.verify_auth(admin_url)) == []
    finally:
        with owner.connect() as connection:
            drop_scratch_database(connection, name)

        owner.dispose()


# -- the token digest -------------------------------------------------------------


def test_the_token_digest_is_the_apis(auth_stack, admin) -> None:
    tokens = [generate_token() for _ in range(10_000)]

    digests = admin.execute(
        "SELECT smm_auth.token_digest(t) "
        "FROM unnest(%s::text[]) WITH ORDINALITY AS u(t, n) ORDER BY n",
        (tokens,),
    ).fetchall()

    assert [row[0] for row in digests] == [hash_token(token) for token in tokens]


@pytest.mark.parametrize(
    "token",
    [
        "a" * 42,
        "a" * 44,
        "a" * 42 + "+",
        "a" * 42 + "/",
        "a" * 42 + "=",
        "a" * 42 + " ",
        "a" * 42 + "é",
        "",
        "'; DROP TABLE user_sessions; --" + "a" * 12,
    ],
)
def test_a_malformed_token_is_refused_without_echoing_it(auth, user, token) -> None:
    for statement in (
        "SELECT * FROM smm_auth.session_resolve(%s)",
        "SELECT * FROM smm_auth.session_revoke(%s)",
    ):
        with pytest.raises(psycopg.Error) as raised:
            auth.execute(statement, (token,))

        assert raised.value.sqlstate == INVALID_PARAMETER_VALUE
        assert token.strip() == "" or token not in str(raised.value)

    with pytest.raises(psycopg.Error) as raised:
        _create(auth, user.id, token)

    assert raised.value.sqlstate == INVALID_PARAMETER_VALUE


def test_a_null_argument_returns_nothing(auth, user) -> None:
    assert auth.execute("SELECT * FROM smm_auth.session_resolve(NULL)").fetchall() == []
    assert auth.execute(
        "SELECT * FROM smm_auth.session_create(%s, NULL)", (user.id,)
    ).fetchall() == []


# -- session creation ---------------------------------------------------------------


def test_session_create_uses_server_time_and_the_fixed_lifetime(
    auth, admin, user
) -> None:
    token = generate_token()

    with auth.transaction():
        server_now = auth.execute("SELECT now()").fetchone()[0]
        [(session_id, expires_at)] = _create(auth, user.id, token)

    row = _session_row(admin, session_id)
    login = admin.execute(
        "SELECT last_login_at, updated_at FROM users WHERE id = %s", (user.id,)
    ).fetchone()

    assert row["user_id"] == user.id
    assert row["token_hash"] == hash_token(token)
    assert row["created_at"] == row["last_seen_at"] == server_now
    assert row["expires_at"] == expires_at == server_now + timedelta(days=7)
    assert row["revoked_at"] is None
    assert login == (server_now, server_now)


def test_the_caller_cannot_supply_timestamps_expiry_or_actor(auth_stack, admin) -> None:
    """The signature takes a user id and a token, nothing else."""

    rows = admin.execute(
        "SELECT p.oid::regprocedure::text, p.proargnames FROM pg_proc p "
        "WHERE p.pronamespace = 'smm_auth'::regnamespace AND p.prosecdef ORDER BY 1"
    ).fetchall()

    inputs = {
        signature: [n for n in names if n.startswith("p_")] for signature, names in rows
    }

    assert inputs == {
        "smm_auth.approval_grant_issue(text,uuid)": ["p_token", "p_revision_id"],
        "smm_auth.approval_preview(text,uuid)": ["p_token", "p_revision_id"],
        "smm_auth.recent_login_failures(text)": ["p_email"],
        "smm_auth.record_login_failure(text)": ["p_email"],
        "smm_auth.session_create(uuid,text)": ["p_user_id", "p_token"],
        "smm_auth.session_resolve(text)": ["p_token"],
        "smm_auth.session_revoke(text)": ["p_token"],
    }


def test_session_create_writes_the_login_audit_row_in_the_apis_shape(
    auth, admin, user, admin_database_url
) -> None:
    [(session_id, _)] = _create(auth, user.id, generate_token())

    [(actor, action, subject, details, at)] = _audit(admin)

    assert (actor, action, subject, details) == (
        f"user:{user.id}",
        "auth.login",
        f"user:{user.id}",
        {"session_id": str(session_id)},
    )
    assert at == _session_row(admin, session_id)["created_at"]

    anomalies = pg_roles.query_admin(
        admin_database_url, ANOMALIES_SQL.read_text(encoding="utf-8")
    )

    assert anomalies == [("END|anomalies|0",)]


def test_session_create_refuses_inactive_and_unknown_users(
    auth, admin, make_user
) -> None:
    inactive = make_user(email="gone@example.com", is_active=False)

    assert _create(auth, inactive.id, generate_token()) == []
    assert _create(auth, uuid4(), generate_token()) == []

    assert admin.execute("SELECT count(*) FROM user_sessions").fetchone()[0] == 0
    assert _audit(admin) == []
    assert admin.execute(
        "SELECT last_login_at FROM users WHERE id = %s", (inactive.id,)
    ).fetchone()[0] is None


def test_session_and_audit_row_are_written_together_or_not_at_all(
    auth, admin, user
) -> None:
    admin.execute(
        """
        CREATE FUNCTION public.test_refuse_login_audit() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.action = 'auth.login' THEN
                RAISE EXCEPTION 'audit refused by test' USING ERRCODE = 'P0001';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER test_refuse_login_audit BEFORE INSERT ON audit_log
            FOR EACH ROW EXECUTE FUNCTION public.test_refuse_login_audit();
        """
    )

    try:
        with pytest.raises(psycopg.Error) as raised:
            _create(auth, user.id, generate_token())

        assert raised.value.sqlstate == "P0001"
    finally:
        admin.execute(
            "DROP TRIGGER test_refuse_login_audit ON audit_log; "
            "DROP FUNCTION public.test_refuse_login_audit()"
        )

    assert admin.execute("SELECT count(*) FROM user_sessions").fetchone()[0] == 0
    assert admin.execute(
        "SELECT last_login_at FROM users WHERE id = %s", (user.id,)
    ).fetchone()[0] is None


def test_parallel_logins_of_one_user_each_get_their_own_session(
    auth_stack, admin, user
) -> None:
    results: list[list[tuple]] = []
    errors: list[BaseException] = []

    def login() -> None:
        try:
            with connect(auth_stack.url) as conn:
                results.append(_create(conn, user.id, generate_token()))
        except BaseException as exc:  # noqa: BLE001 -- reported below
            errors.append(exc)

    threads = [threading.Thread(target=login) for _ in range(4)]

    for thread in threads:
        thread.start()

    for thread in threads:
        thread.join(30)

    assert errors == []
    assert len({rows[0][0] for rows in results}) == 4
    assert admin.execute("SELECT count(*) FROM user_sessions").fetchone()[0] == 4
    assert len([row for row in _audit(admin) if row[1] == "auth.login"]) == 4


# -- resolution: the same answers as the API's Python ---------------------------------


@pytest.fixture
def cc_session(restricted_session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """A session as smm-api has it: ai_smm_cc under G1."""

    db = restricted_session_factory()

    try:
        yield db
    finally:
        db.rollback()
        db.close()


def test_a_session_made_by_sql_is_the_apis_and_the_reverse(
    auth, admin, user, cc_session: Session, session: Session, settings: Settings
) -> None:
    # SQL-created -> accepted by the API's resolve_session (as ai_smm_cc).
    sql_token = generate_token()
    [(sql_session_id, _)] = _create(auth, user.id, sql_token)

    resolved = resolve_session(cc_session, token=sql_token, settings=settings)

    assert resolved is not None and resolved.session.id == sql_session_id
    cc_session.rollback()

    # Python-created (as a release before smm-auth made it, and as the
    # owner's tooling still can; G2 took this from ai_smm_cc) -> accepted
    # by session_resolve.
    api_user = session.get(type(resolved.user), user.id)
    api_session, api_token = create_session(session, user=api_user, settings=settings)
    session.commit()

    assert _resolve(auth, api_token) == [
        (api_session.id, user.id, api_session.expires_at)
    ]

    # Revoked by SQL -> refused by the API, and the reverse.
    assert _revoke(auth, sql_token) == [(sql_session_id, user.id, True)]
    assert resolve_session(cc_session, token=sql_token, settings=settings) is None
    cc_session.rollback()

    row = session.get(UserSession, api_session.id)
    revoke_session(row)
    session.commit()

    assert _resolve(auth, api_token) == []


@pytest.mark.parametrize(
    ("change", "valid"),
    [
        ("expires_at = now() + interval '5 seconds'", True),
        ("expires_at = now() - interval '5 seconds'", False),
        ("last_seen_at = now() - interval '24 hours' + interval '5 seconds'", True),
        ("last_seen_at = now() - interval '24 hours' - interval '5 seconds'", False),
        ("revoked_at = now()", False),
    ],
    ids=["expires-soon", "expired", "idle-almost", "idle-over", "revoked"],
)
def test_expiry_idle_and_revocation_agree_with_the_api(
    auth, admin, user, cc_session: Session, settings: Settings, change: str, valid: bool
) -> None:
    token = generate_token()
    [(session_id, _)] = _create(auth, user.id, token)

    # A plausible 25-hour-old session (seen just now, 7-day lifetime), then
    # one deadline moved across its boundary.
    admin.execute(
        "UPDATE user_sessions SET created_at = now() - interval '25 hours', "
        "last_seen_at = now(), "
        "expires_at = now() - interval '25 hours' + interval '7 days' WHERE id = %s",
        (session_id,),
    )
    admin.execute(f"UPDATE user_sessions SET {change} WHERE id = %s", (session_id,))

    in_sql = _resolve(auth, token) != []
    in_python = resolve_session(cc_session, token=token, settings=settings) is not None

    assert in_sql is valid
    assert in_python is valid


def test_a_deactivated_user_loses_every_session(auth, admin, user) -> None:
    token = generate_token()
    _create(auth, user.id, token)
    admin.execute("UPDATE users SET is_active = false WHERE id = %s", (user.id,))

    assert _resolve(auth, token) == []
    assert _revoke(auth, token) == []


@pytest.mark.parametrize(
    "change",
    [
        "created_at = now() + interval '2 minutes', "
        "last_seen_at = now() + interval '2 minutes'",
        "last_seen_at = now() + interval '2 minutes'",
        "last_seen_at = created_at - interval '2 minutes'",
        "expires_at = created_at + interval '8 days'",
    ],
    ids=[
        "created-in-future",
        "seen-in-future",
        "seen-before-created",
        "lifetime-too-long",
    ],
)
def test_an_implausible_session_is_refused(auth, admin, user, change: str) -> None:
    """ADR §12 RS-3, in SQL. The API's Python gets the same bounds in a
    later 3B step; until then this is a documented, one-sided difference."""

    token = generate_token()
    [(session_id, _)] = _create(auth, user.id, token)
    admin.execute(f"UPDATE user_sessions SET {change} WHERE id = %s", (session_id,))

    assert _resolve(auth, token) == []
    assert _revoke(auth, token) == []


def test_the_touch_is_throttled_to_once_a_minute(auth, admin, user) -> None:
    token = generate_token()
    [(session_id, _)] = _create(auth, user.id, token)

    admin.execute(
        "UPDATE user_sessions SET created_at = now() - interval '10 minutes', "
        "expires_at = now() - interval '10 minutes' + interval '7 days', "
        "last_seen_at = now() - interval '30 seconds' WHERE id = %s",
        (session_id,),
    )
    recent = _session_row(admin, session_id)["last_seen_at"]

    assert len(_resolve(auth, token)) == 1

    assert _session_row(admin, session_id)["last_seen_at"] == recent

    admin.execute(
        "UPDATE user_sessions SET last_seen_at = now() - interval '2 minutes' "
        "WHERE id = %s",
        (session_id,),
    )
    old = _session_row(admin, session_id)["last_seen_at"]

    with auth.transaction():
        server_now = auth.execute("SELECT now()").fetchone()[0]

        assert len(_resolve(auth, token)) == 1

    assert _session_row(admin, session_id)["last_seen_at"] == server_now > old




def test_resolution_never_waits_for_a_locked_session(
    auth_stack, admin, admin_database_url, user
) -> None:
    """smm-api's request transaction may hold the row; logout's check must
    not queue behind it. The role's lock_timeout is 2 s, so a wait would
    show as an error, not merely as time."""

    token = generate_token()

    with connect(auth_stack.url) as auth:
        [(session_id, _)] = _create(auth, user.id, token)

    admin.execute(
        "UPDATE user_sessions SET created_at = now() - interval '10 minutes', "
        "expires_at = now() - interval '10 minutes' + interval '7 days', "
        "last_seen_at = now() - interval '5 minutes' WHERE id = %s",
        (session_id,),
    )
    before = _session_row(admin, session_id)["last_seen_at"]

    with connect(admin_database_url, autocommit=False) as locker, connect(
        auth_stack.url
    ) as auth:
        locker.execute(
            "SELECT 1 FROM user_sessions WHERE id = %s FOR UPDATE", (session_id,)
        )

        started = time.monotonic()
        rows = _resolve(auth, token)
        elapsed = time.monotonic() - started

        locker.rollback()

    assert [(r[0], r[1]) for r in rows] == [(session_id, user.id)]
    assert elapsed < 1.0
    assert _session_row(admin, session_id)["last_seen_at"] == before


# -- logout ------------------------------------------------------------------------


def test_logout_revokes_once_and_writes_one_audit_row(auth, admin, user) -> None:
    token = generate_token()
    [(session_id, _)] = _create(auth, user.id, token)

    with auth.transaction():
        server_now = auth.execute("SELECT now()").fetchone()[0]
        first = _revoke(auth, token)

    assert first == [(session_id, user.id, True)]
    assert _session_row(admin, session_id)["revoked_at"] == server_now
    assert _resolve(auth, token) == []
    assert _revoke(auth, token) == []

    logouts = [row for row in _audit(admin) if row[1] == "auth.logout"]

    assert [(a, s, d) for a, _, s, d, _ in logouts] == [
        (f"user:{user.id}", f"user:{user.id}", {"session_id": str(session_id)})
    ]


def test_logout_refuses_sessions_that_are_not_live(auth, admin, user) -> None:
    expired, unknown = generate_token(), generate_token()
    [(session_id, _)] = _create(auth, user.id, expired)
    admin.execute(
        "UPDATE user_sessions SET created_at = now() - interval '8 days', "
        "last_seen_at = now() - interval '8 days', "
        "expires_at = now() - interval '1 day' "
        "WHERE id = %s",
        (session_id,),
    )
    audit_before = _audit(admin)

    assert _revoke(auth, expired) == []
    assert _revoke(auth, unknown) == []
    assert _audit(admin) == audit_before
    assert _session_row(admin, session_id)["revoked_at"] is None


def test_concurrent_double_logout_revokes_once(auth_stack, admin, user) -> None:
    token = generate_token()

    with connect(auth_stack.url) as auth:
        [(session_id, _)] = _create(auth, user.id, token)

    second: list[list[tuple]] = []

    with connect(auth_stack.url, autocommit=False) as first:
        assert _revoke(first, token) == [(session_id, user.id, True)]

        # The second logout queues behind the first one's row lock ...
        def logout_again() -> None:
            with connect(auth_stack.url) as conn:
                second.append(_revoke(conn, token))

        waiter = threading.Thread(target=logout_again)
        waiter.start()
        time.sleep(0.5)

        assert waiter.is_alive()

        first.commit()

    waiter.join(10)

    # ... and, once it commits, finds nothing left to revoke.
    assert second == [[]]
    assert len([row for row in _audit(admin) if row[1] == "auth.logout"]) == 1


# -- failed logins -----------------------------------------------------------------


def test_failed_logins_are_recorded_in_the_apis_shape_and_counted(auth, admin) -> None:
    email = "someone@example.com"

    assert _recent(auth, email) == 0
    assert _record(auth, email) == 1
    assert _record(auth, email) == 2
    auth.execute("SELECT smm_auth.record_login_failure(%s)", ("other@example.com",))

    assert [(a, act, s, d) for a, act, s, d, _ in _audit(admin)][:1] == [
        ("anonymous", "auth.login_failed", f"email:{email}", {})
    ]

    # Older than the 15-minute window: not counted.
    admin.execute(
        "UPDATE audit_log SET at = now() - interval '16 minutes' "
        "WHERE id = (SELECT min(id) FROM audit_log)"
    )

    assert _recent(auth, email) == 1


@pytest.mark.parametrize(
    "email",
    [
        "Someone@example.com",
        " someone@example.com",
        "no-at-sign",
        "a@b",
        "x" * 150 + "@example.com",
    ],
    ids=["upper-case", "untrimmed", "no-at", "no-dot", "too-long-for-audit-subject"],
)
def test_a_failure_for_a_non_normalised_address_is_refused(auth, admin, email) -> None:
    for statement in (
        "SELECT smm_auth.record_login_failure(%s)",
        "SELECT smm_auth.recent_login_failures(%s)",
    ):
        with pytest.raises(psycopg.Error) as raised:
            auth.execute(statement, (email,))

        assert raised.value.sqlstate == INVALID_PARAMETER_VALUE

    assert _audit(admin) == []


# -- what nobody else can do, and what ai_smm_auth cannot do -----------------------


def test_ai_smm_cc_cannot_use_any_auth_function(
    auth_stack, restricted_engine: Engine, user
) -> None:
    raw = restricted_engine.raw_connection()

    try:
        for signature, statement in {**CONTRACTS, **HELPERS}.items():
            cursor = raw.cursor()

            with pytest.raises(psycopg.Error) as raised:
                cursor.execute(statement, _args_for(signature, user.id))

            assert raised.value.sqlstate == INSUFFICIENT_PRIVILEGE, signature
            raw.rollback()
    finally:
        raw.close()


def test_no_definer_function_lets_ai_smm_cc_create_a_session(
    auth_stack, admin_database_url
) -> None:
    """The new primitives add no path from ai_smm_cc to a session. (Its
    direct INSERT under G1 is the known residual G2 removes later:
    test_residual_risk_the_role_can_mint_a_session_for_any_user.)"""

    callable_definers = pg_roles.query_admin(
        admin_database_url,
        """
        SELECT p.oid::regprocedure::text
          FROM pg_proc p
          JOIN pg_namespace n ON n.oid = p.pronamespace
         WHERE p.prosecdef
           AND n.nspname NOT IN ('pg_catalog', 'information_schema')
           AND has_function_privilege('ai_smm_cc', p.oid, 'EXECUTE')
        """,
    )

    assert callable_definers == []


def test_public_and_any_other_role_are_shut_out(
    auth_stack, admin, database_url, admin_database_url, user
) -> None:
    """A role that can reach the database and schema public -- which PUBLIC
    itself may, on a database with PostgreSQL's defaults -- still cannot
    touch smm_auth."""

    password = generate_token()
    database = make_url(database_url).database
    admin.execute(
        pgsql.SQL(
            "CREATE ROLE ai_smm_probe LOGIN PASSWORD {}; "
            "GRANT CONNECT ON DATABASE {} TO ai_smm_probe; "
            "GRANT USAGE ON SCHEMA public TO ai_smm_probe"
        ).format(pgsql.Literal(password), pgsql.Identifier(database))
    )

    try:
        probe_url = make_url(database_url).set(
            username="ai_smm_probe", password=password
        ).render_as_string(hide_password=False)

        with connect(probe_url) as probe:
            for signature, statement in {**CONTRACTS, **HELPERS}.items():
                assert (
                    _refused(probe, statement, _args_for(signature, user.id))
                    == INSUFFICIENT_PRIVILEGE
                ), signature
    finally:
        admin.execute("DROP OWNED BY ai_smm_probe; DROP ROLE ai_smm_probe")

    assert admin.execute(
        "SELECT bool_or(has_function_privilege('public', p.oid, 'EXECUTE')) "
        "FROM pg_proc p WHERE p.pronamespace = 'smm_auth'::regnamespace"
    ).fetchone()[0] is False
    assert admin.execute(
        "SELECT has_schema_privilege('public', 'smm_auth', 'USAGE')"
    ).fetchone()[0] is False


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO user_sessions (id, user_id, token_hash, expires_at) "
        "VALUES (gen_random_uuid(), '{user}', 'x', now())",
        "UPDATE user_sessions SET revoked_at = NULL",
        "UPDATE user_sessions SET last_seen_at = now()",
        "DELETE FROM user_sessions",
        "SELECT token_hash FROM user_sessions",
        "UPDATE users SET password_hash = 'x' WHERE id = '{user}'",
        "UPDATE users SET is_active = true",
        "UPDATE users SET last_login_at = now()",
        "INSERT INTO audit_log (actor, action) VALUES ('worker:worker-1', 'published')",
        "SELECT * FROM audit_log",
        "SELECT * FROM publications",
        "UPDATE publications SET status = 'scheduled'",
        "TRUNCATE audit_log",
        "CREATE TABLE public.planted (id int)",
        "CREATE FUNCTION smm_auth.planted() RETURNS int LANGUAGE sql AS 'SELECT 1'",
        "SET ROLE ai_smm",
        "SELECT smm_auth.token_digest('" + "a" * 43 + "')",
    ],
)
def test_ai_smm_auth_can_do_nothing_but_its_contracts(auth, user, statement) -> None:
    statement = statement.replace("{user}", str(user.id))

    assert _refused(auth, statement) == INSUFFICIENT_PRIVILEGE


def test_ai_smm_auth_reads_only_the_named_user_columns(auth, user) -> None:
    row = auth.execute(
        "SELECT id, email, password_hash, display_name, is_active, last_login_at, "
        "created_at, updated_at FROM users WHERE id = %s",
        (user.id,),
    ).fetchone()

    assert row[0] == user.id and row[2].startswith("$argon2id$")


def test_the_contracts_write_no_business_table(auth, admin_database_url, user) -> None:
    before = business_fingerprint(admin_database_url)
    token = generate_token()

    _create(auth, user.id, token)
    _resolve(auth, token)
    _revoke(auth, token)
    auth.execute("SELECT smm_auth.record_login_failure(%s)", ("someone@example.com",))
    auth.execute("SELECT smm_auth.recent_login_failures(%s)", ("someone@example.com",))

    assert business_fingerprint(admin_database_url) == before


# -- C1: the definer owner's preconditions, and pre-existing objects --------------


@pytest.fixture
def owner_name(database_url: str) -> str:
    return make_url(database_url).username


@dataclass
class Scratch:
    owner_url: str
    admin_url: str


@pytest.fixture
def scratch(database_url: str, admin_database_url: str) -> Iterator[Scratch]:
    """A throwaway database with the application tables, owned like
    production's (by the suite's owner role), for installs that are meant
    to fail or to find a tampered state."""

    from ai_smm.db.models import Base

    name = "ai_smm_auth_scratch_test"
    owner = create_engine(database_url, isolation_level="AUTOCOMMIT")
    scratch = Scratch(
        owner_url=make_url(database_url).set(database=name).render_as_string(
            hide_password=False
        ),
        admin_url=make_url(admin_database_url).set(database=name).render_as_string(
            hide_password=False
        ),
    )

    with owner.connect() as connection:
        drop_scratch_database(connection, name)
        connection.execute(text(f'CREATE DATABASE "{name}"'))

    tables = create_engine(scratch.owner_url)
    Base.metadata.create_all(tables)
    tables.dispose()

    try:
        yield scratch
    finally:
        with owner.connect() as connection:
            drop_scratch_database(connection, name)

        owner.dispose()


def _install_refused(admin_url: str, fragment: str) -> None:
    """The functions file fails with this message and leaves no trace."""

    with pytest.raises(psycopg.Error) as raised:
        pg_roles.run_script(admin_url, pg_roles.AUTH_FUNCTIONS_SQL)

    assert fragment in str(raised.value)
    # Nothing in the message but object and role names.
    assert "password" not in str(raised.value).lower()


def _has_schema(admin_url: str) -> bool:
    return pg_roles.query_admin(
        admin_url, "SELECT to_regnamespace('smm_auth') IS NOT NULL"
    )[0][0]


@pytest.mark.parametrize(
    ("tamper", "fragment"),
    [
        (
            "ALTER SCHEMA public OWNER TO CURRENT_USER; "
            "REVOKE ALL ON SCHEMA public FROM PUBLIC; "
            "REVOKE ALL ON SCHEMA public FROM pg_database_owner",
            "USAGE on public",
        ),
        ("REVOKE SELECT ON users FROM {owner}", "SELECT on public.users"),
        ("REVOKE UPDATE ON users FROM {owner}", "UPDATE on public.users"),
        (
            "REVOKE INSERT ON user_sessions FROM {owner}",
            "INSERT on public.user_sessions",
        ),
        (
            "REVOKE UPDATE ON user_sessions FROM {owner}",
            "UPDATE on public.user_sessions",
        ),
        ("REVOKE SELECT ON audit_log FROM {owner}", "SELECT on public.audit_log"),
        ("REVOKE INSERT ON audit_log FROM {owner}", "INSERT on public.audit_log"),
        (
            "REVOKE ALL ON SEQUENCE audit_log_id_seq FROM {owner}",
            "USAGE on public.audit_log_id_seq",
        ),
        (
            "REVOKE EXECUTE ON FUNCTION pg_catalog.gen_random_uuid() FROM PUBLIC",
            "cannot execute built-in functions: {gen_random_uuid}",
        ),
        (
            "ALTER TABLE users OWNER TO CURRENT_USER; "
            "ALTER TABLE user_sessions OWNER TO CURRENT_USER; "
            "ALTER TABLE audit_log OWNER TO CURRENT_USER",
            "is a superuser",
        ),
    ],
    ids=[
        "no-usage-on-public",
        "no-select-users",
        "no-update-users",
        "no-insert-sessions",
        "no-update-sessions",
        "no-select-audit",
        "no-insert-audit",
        "no-usage-audit-sequence",
        "no-execute-builtin",
        "superuser-owner",
    ],
)
def test_the_install_refuses_an_owner_without_the_needed_privileges(
    scratch: Scratch, owner_name: str, tamper: str, fragment: str
) -> None:
    pg_roles.run_admin(scratch.admin_url, tamper.replace("{owner}", owner_name))

    _install_refused(scratch.admin_url, fragment)

    assert not _has_schema(scratch.admin_url)


def test_the_install_fails_closed_on_default_privileges_of_the_installer(
    scratch: Scratch,
) -> None:
    """Default privileges of the role running the file would hand the new
    functions to someone else; the whole install rolls back instead."""

    pg_roles.run_admin(
        scratch.admin_url,
        "ALTER DEFAULT PRIVILEGES FOR ROLE CURRENT_USER "
        "GRANT EXECUTE ON FUNCTIONS TO pg_monitor",
    )

    _install_refused(scratch.admin_url, "unexpected grants")

    assert not _has_schema(scratch.admin_url)


@pytest.mark.parametrize(
    ("tamper", "fragment"),
    [
        (
            "CREATE FUNCTION smm_auth.planted() RETURNS int LANGUAGE sql AS 'SELECT 1'",
            "objects this file does not make",
        ),
        (
            "ALTER FUNCTION smm_auth.token_digest(text) OWNER TO CURRENT_USER",
            "functions in the schema have another owner",
        ),
        (
            "GRANT EXECUTE ON FUNCTION smm_auth.token_digest(text) TO PUBLIC",
            "unexpected grants",
        ),
        (
            "GRANT EXECUTE ON FUNCTION smm_auth.session_create(uuid, text) "
            "TO pg_monitor",
            "unexpected grants",
        ),
        ("GRANT USAGE ON SCHEMA smm_auth TO pg_monitor", "unexpected grants"),
        ("ALTER SCHEMA smm_auth OWNER TO CURRENT_USER", "is not owned by"),
    ],
    ids=[
        "planted-function",
        "foreign-owner",
        "public-execute-helper",
        "foreign-execute-contract",
        "foreign-schema-usage",
        "foreign-schema-owner",
    ],
)
def test_a_rerun_refuses_a_tampered_schema_instead_of_repairing_it(
    scratch: Scratch, tamper: str, fragment: str
) -> None:
    pg_roles.run_script(scratch.admin_url, pg_roles.AUTH_FUNCTIONS_SQL)

    def state() -> list[tuple]:
        return pg_roles.query_admin(
            scratch.admin_url,
            "SELECT p.oid::regprocedure::text, pg_get_userbyid(p.proowner), "
            "p.proacl::text, md5(p.prosrc) FROM pg_proc p "
            "WHERE p.pronamespace = 'smm_auth'::regnamespace "
            "UNION ALL SELECT 'schema', pg_get_userbyid(nspowner), nspacl::text, '' "
            "FROM pg_namespace WHERE nspname = 'smm_auth' ORDER BY 1",
        )

    pg_roles.run_admin(scratch.admin_url, tamper)
    tampered = state()

    _install_refused(scratch.admin_url, fragment)

    # Refused, not repaired: the tampered state is exactly as it was.
    assert state() == tampered


def test_the_install_is_one_transaction(auth_stack) -> None:
    """Every function is created and stripped of PUBLIC's EXECUTE before
    the single COMMIT; nothing commits in between."""

    sql_text = pg_roles.AUTH_FUNCTIONS_SQL.read_text(encoding="utf-8")
    code = "\n".join(
        line for line in sql_text.splitlines() if not line.lstrip().startswith("--")
    )
    statements = [s.strip().upper() for s in code.split(";")]

    assert statements.count("BEGIN") == 1
    assert statements.count("COMMIT") == 1
    assert [s for s in statements if s][-1] == "COMMIT"
    assert "ROLLBACK" not in code.upper()
    upper = code.upper()

    assert upper.index("BEGIN;") < upper.index("CREATE OR REPLACE FUNCTION")
    assert upper.rindex("REVOKE ALL ON FUNCTION") < upper.rindex("COMMIT;")


def test_no_committed_state_lets_public_execute(scratch: Scratch) -> None:
    """Watched from another session while the file runs: either no smm_auth
    function is visible yet, or none is executable by PUBLIC."""

    seen: list[tuple[int, bool]] = []
    done = threading.Event()

    def watch() -> None:
        with connect(scratch.admin_url) as conn:
            while not done.is_set():
                seen.append(
                    conn.execute(
                        "SELECT count(*), coalesce(bool_or("
                        "p.proacl IS NULL OR has_function_privilege('public', p.oid, "
                        "'EXECUTE')), false) FROM pg_proc p "
                        "WHERE p.pronamespace = "
                        "coalesce(to_regnamespace('smm_auth')::oid, 0)"
                    ).fetchone()
                )

    watcher = threading.Thread(target=watch)
    watcher.start()

    try:
        for _ in range(3):
            pg_roles.run_script(scratch.admin_url, pg_roles.AUTH_FUNCTIONS_SQL)
    finally:
        done.set()
        watcher.join(10)

    counts = {count for count, _ in seen}

    # The watcher saw the functions (after the first commit), never a part.
    assert 9 in counts
    assert counts <= {0, 9}
    assert not any(public for _, public in seen)


# -- C2: every executable kind is verified ----------------------------------------


def test_verify_covers_procedures_aggregates_and_window_functions(
    auth_stack, admin, admin_database_url
) -> None:
    """Each kind pg_proc holds gets PUBLIC's EXECUTE by default; verify
    must flag every one in public, and accept them once PUBLIC is shut out.
    A direct grant to ai_smm_auth anywhere -- even in a schema it cannot
    use -- is flagged too. Trigger and event trigger functions are not."""

    admin.execute(
        """
        CREATE FUNCTION public.test_exec_function() RETURNS int
            LANGUAGE sql AS 'SELECT 1';
        CREATE PROCEDURE public.test_exec_procedure()
            LANGUAGE plpgsql AS $$ BEGIN NULL; END $$;
        CREATE AGGREGATE public.test_exec_aggregate(int)
            (sfunc = int4pl, stype = int);
        CREATE FUNCTION public.test_exec_window() RETURNS bigint
            LANGUAGE internal WINDOW IMMUTABLE AS 'window_rank';
        CREATE FUNCTION public.test_exec_event() RETURNS event_trigger
            LANGUAGE plpgsql AS $$ BEGIN NULL; END $$;
        CREATE SCHEMA test_exec_other;
        CREATE FUNCTION test_exec_other.granted() RETURNS int
            LANGUAGE sql AS 'SELECT 1';
        REVOKE ALL ON FUNCTION test_exec_other.granted() FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION test_exec_other.granted() TO ai_smm_auth;
        """
    )

    try:
        failed = pg_roles.failures(pg_roles.verify_auth(admin_database_url))

        assert sorted(failed) == sorted(
            [
                "FAIL|function.test_exec_function().execute|denied",
                "FAIL|procedure.test_exec_procedure().execute|denied",
                "FAIL|aggregate.test_exec_aggregate(integer).execute|denied",
                "FAIL|window.test_exec_window().execute|denied",
                "FAIL|acl.routines.unexpected|test_exec_other.granted()=EXECUTE",
            ]
        )

        admin.execute(
            """
            REVOKE ALL ON FUNCTION public.test_exec_function() FROM PUBLIC;
            REVOKE ALL ON PROCEDURE public.test_exec_procedure() FROM PUBLIC;
            REVOKE ALL ON FUNCTION public.test_exec_aggregate(int) FROM PUBLIC;
            REVOKE ALL ON FUNCTION public.test_exec_window() FROM PUBLIC;
            REVOKE ALL ON FUNCTION test_exec_other.granted() FROM ai_smm_auth;
            """
        )

        lines = pg_roles.verify_auth(admin_database_url)

        assert pg_roles.failures(lines) == []
        assert "PASS|procedure.test_exec_procedure().execute|denied" in lines
        assert "PASS|aggregate.test_exec_aggregate(integer).execute|denied" in lines
        assert "PASS|window.test_exec_window().execute|denied" in lines
        assert not any("test_exec_event" in line for line in lines)
    finally:
        admin.execute(
            """
            DROP FUNCTION public.test_exec_function();
            DROP PROCEDURE public.test_exec_procedure();
            DROP AGGREGATE public.test_exec_aggregate(int);
            DROP FUNCTION public.test_exec_window();
            DROP FUNCTION public.test_exec_event();
            DROP SCHEMA test_exec_other CASCADE;
            """
        )


# -- the rollback, rehearsed --------------------------------------------------------


def test_the_rollback_restores_the_baseline_and_can_be_reinstalled(
    auth_stack, admin_database_url, database_url
) -> None:
    """P3 rollback and re-run: retire refuses while smm-auth is connected,
    the drop refuses while the role exists; in order, both leave every other
    role, G1 and the schema exactly as before; a second install verifies."""

    with (
        connect(auth_stack.url),
        pytest.raises(psycopg.Error, match="still has open sessions"),
    ):
        pg_roles.run_script(admin_database_url, pg_roles.AUTH_REVOKE_SQL)

    with pytest.raises(psycopg.Error, match="run ai_smm_auth.revoke.sql first"):
        pg_roles.run_script(admin_database_url, pg_roles.AUTH_DROP_SQL)

    pg_roles.uninstall_auth(admin_database_url)

    assert (
        pg_roles.snapshot_except_auth(admin_database_url)
        == auth_stack.baseline_except_auth
    )
    assert pg_roles.verify(admin_database_url) == auth_stack.cc_verify_before
    assert schema_fingerprint(admin_database_url) == auth_stack.schema_before
    assert pg_roles.query_admin(
        admin_database_url,
        "SELECT to_regnamespace('smm_auth') IS NULL "
        "AND to_regrole('ai_smm_auth') IS NULL",
    )[0][0]

    # Both rollback files are idempotent.
    pg_roles.uninstall_auth(admin_database_url)

    # Reinstall, with the module's password, for the module teardown.
    pg_roles.install_auth(admin_database_url)
    pg_roles.set_auth_password(admin_database_url, auth_stack.password)

    assert pg_roles.failures(pg_roles.verify_auth(admin_database_url)) == []
