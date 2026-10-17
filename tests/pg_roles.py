"""Test helpers for the Control Center API's PostgreSQL role (SMM-024C-2).

The role is created by the same files an operator runs in production
(deploy/control-center/db/*.sql), executed here as a superuser of the
disposable test cluster: AI_SMM_TEST_ADMIN_DATABASE_URL. Nothing in this
module is a fixture; conftest.py wires it.

The role's password exists only in memory for one test session. It is
generated here, set with ALTER ROLE over the admin connection, and only
ever handed to SQLAlchemy inside a URL -- whose repr masks it.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url


ROOT = Path(__file__).resolve().parents[1]
DB_DIR = ROOT / "deploy" / "control-center" / "db"
GRANTS_SQL = DB_DIR / "ai_smm_cc.grants.sql"
VERIFY_SQL = DB_DIR / "ai_smm_cc.verify.sql"
REVOKE_SQL = DB_DIR / "ai_smm_cc.revoke.sql"
#: The privileges of every OTHER role; must not move when ours is made.
SNAPSHOT_SQL = DB_DIR / "acl-snapshot.sql"

API_ROLE = "ai_smm_cc"

# -- smm-auth (SMM-024C-3B-1) -------------------------------------------------
AUTH_ROLE = "ai_smm_auth"
AUTH_FUNCTIONS_SQL = DB_DIR / "smm_auth.functions.sql"
AUTH_DROP_SQL = DB_DIR / "smm_auth.drop.sql"
AUTH_GRANTS_SQL = DB_DIR / "ai_smm_auth.grants.sql"
AUTH_VERIFY_SQL = DB_DIR / "ai_smm_auth.verify.sql"
AUTH_REVOKE_SQL = DB_DIR / "ai_smm_auth.revoke.sql"
#: The privileges of every role but ai_smm_auth -- ai_smm_cc included.
AUTH_SNAPSHOT_SQL = DB_DIR / "ai_smm_auth.acl-snapshot.sql"


def libpq_url(url: str) -> str:
    """postgresql+psycopg://... -> postgresql://... for psycopg.connect."""

    return make_url(url).set(drivername="postgresql").render_as_string(
        hide_password=False
    )


def run_script(admin_url: str, path: Path) -> None:
    """Execute one of the operator's .sql files, as psql -f would.

    Autocommit, so the file's own BEGIN/COMMIT is the transaction; a
    failure closes the connection, which rolls that transaction back.
    """

    with psycopg.connect(libpq_url(admin_url), autocommit=True) as conn:
        conn.execute(path.read_text(encoding="utf-8"))


def run_admin(admin_url: str, statement: str | sql.Composable) -> None:
    with psycopg.connect(libpq_url(admin_url), autocommit=True) as conn:
        conn.execute(statement)


def query_admin(
    admin_url: str, statement: str, *, read_only: bool = True
) -> list[tuple]:
    options = "-c default_transaction_read_only=on" if read_only else ""

    with psycopg.connect(
        libpq_url(admin_url), autocommit=True, options=options
    ) as conn:
        return list(conn.execute(statement).fetchall())


def verify(admin_url: str) -> list[str]:
    """ai_smm_cc.verify.sql, read-only, as the lines psql -tA prints."""

    return [
        row[0]
        for row in query_admin(
            admin_url, VERIFY_SQL.read_text(encoding="utf-8")
        )
    ]


def failures(lines: list[str]) -> list[str]:
    return [line for line in lines if line.startswith("FAIL|")]


def snapshot_others(admin_url: str) -> list[str]:
    return [
        row[0]
        for row in query_admin(
            admin_url, SNAPSHOT_SQL.read_text(encoding="utf-8")
        )
    ]


@dataclass(frozen=True)
class ApiRole:
    """The provisioned role: how to connect as it, and the evidence."""

    url: str
    others_before: list[str]
    others_after: list[str]


def set_new_password(admin_url: str) -> str:
    password = secrets.token_hex(24)

    run_admin(
        admin_url,
        sql.SQL("ALTER ROLE ai_smm_cc PASSWORD {}").format(
            sql.Literal(password)
        ),
    )

    return password


def restore_password(admin_url: str, url: str) -> None:
    """Set the role's password back to the one in its session URL."""

    run_admin(
        admin_url,
        sql.SQL("ALTER ROLE ai_smm_cc PASSWORD {}").format(
            sql.Literal(make_url(url).password)
        ),
    )


def role_url(database_url: str, password: str) -> str:
    return make_url(database_url).set(
        username=API_ROLE, password=password
    ).render_as_string(hide_password=False)


def provision(admin_url: str, database_url: str) -> ApiRole:
    before = snapshot_others(admin_url)
    run_script(admin_url, GRANTS_SQL)
    after = snapshot_others(admin_url)
    password = set_new_password(admin_url)

    return ApiRole(
        url=role_url(database_url, password),
        others_before=before,
        others_after=after,
    )


def retire(admin_url: str) -> None:
    run_script(admin_url, REVOKE_SQL)


# -- smm-auth (SMM-024C-3B-1) -------------------------------------------------


def verify_auth(admin_url: str) -> list[str]:
    """ai_smm_auth.verify.sql, read-only, as the lines psql -tA prints."""

    return [
        row[0]
        for row in query_admin(
            admin_url, AUTH_VERIFY_SQL.read_text(encoding="utf-8")
        )
    ]


def snapshot_except_auth(admin_url: str) -> list[str]:
    return [
        row[0]
        for row in query_admin(
            admin_url, AUTH_SNAPSHOT_SQL.read_text(encoding="utf-8")
        )
    ]


def set_auth_password(admin_url: str, password: str | None = None) -> str:
    """Set ai_smm_auth's password; a new random one unless given."""

    password = password or secrets.token_hex(24)

    run_admin(
        admin_url,
        sql.SQL("ALTER ROLE ai_smm_auth PASSWORD {}").format(
            sql.Literal(password)
        ),
    )

    return password


def auth_role_url(database_url: str, password: str) -> str:
    return make_url(database_url).set(
        username=AUTH_ROLE, password=password
    ).render_as_string(hide_password=False)


def install_auth(admin_url: str) -> None:
    """P3, as the operator runs it: the functions, then the role's grants."""

    run_script(admin_url, AUTH_FUNCTIONS_SQL)
    run_script(admin_url, AUTH_GRANTS_SQL)


def uninstall_auth(admin_url: str) -> None:
    """The P3 rollback, in its required order: the role, then the functions."""

    run_script(admin_url, AUTH_REVOKE_SQL)
    run_script(admin_url, AUTH_DROP_SQL)


# -- the business-mutation role (SMM-025C, FU-025B-1) -------------------------
MUTATION_ROLE = "ai_smm_cc_rw"
MUTATION_GRANTS_SQL = DB_DIR / "ai_smm_cc_rw.grants.sql"
MUTATION_VERIFY_SQL = DB_DIR / "ai_smm_cc_rw.verify.sql"
MUTATION_REVOKE_SQL = DB_DIR / "ai_smm_cc_rw.revoke.sql"
#: The privileges of every role but ai_smm_cc_rw -- ai_smm_cc included.
MUTATION_SNAPSHOT_SQL = DB_DIR / "ai_smm_cc_rw.acl-snapshot.sql"

# -- generation worker (SMM-026A/2.1-FIX) -----------------------------------
GENERATION_ROLE = "ai_smm_generation"
GENERATION_GRANTS_SQL = DB_DIR / "ai_smm_generation.grants.sql"
GENERATION_VERIFY_SQL = DB_DIR / "ai_smm_generation.verify.sql"
GENERATION_REVOKE_SQL = DB_DIR / "ai_smm_generation.revoke.sql"


def verify_mutation(admin_url: str) -> list[str]:
    return [
        row[0]
        for row in query_admin(
            admin_url, MUTATION_VERIFY_SQL.read_text(encoding="utf-8")
        )
    ]


def snapshot_except_mutation(admin_url: str) -> list[str]:
    return [
        row[0]
        for row in query_admin(
            admin_url, MUTATION_SNAPSHOT_SQL.read_text(encoding="utf-8")
        )
    ]


def provision_mutation(admin_url: str, database_url: str) -> ApiRole:
    before = snapshot_except_mutation(admin_url)
    run_script(admin_url, MUTATION_GRANTS_SQL)
    after = snapshot_except_mutation(admin_url)
    password = secrets.token_hex(24)

    run_admin(
        admin_url,
        sql.SQL("ALTER ROLE ai_smm_cc_rw PASSWORD {}").format(
            sql.Literal(password)
        ),
    )

    return ApiRole(
        url=make_url(database_url)
        .set(username=MUTATION_ROLE, password=password)
        .render_as_string(hide_password=False),
        others_before=before,
        others_after=after,
    )


def retire_mutation(admin_url: str) -> None:
    run_script(admin_url, MUTATION_REVOKE_SQL)


def verify_generation(admin_url: str) -> list[str]:
    return [
        row[0]
        for row in query_admin(
            admin_url, GENERATION_VERIFY_SQL.read_text(encoding="utf-8")
        )
    ]


def provision_generation(admin_url: str, database_url: str) -> ApiRole:
    run_script(admin_url, GENERATION_GRANTS_SQL)
    password = secrets.token_hex(24)
    run_admin(
        admin_url,
        sql.SQL("ALTER ROLE ai_smm_generation PASSWORD {}").format(
            sql.Literal(password)
        ),
    )
    return ApiRole(
        url=make_url(database_url)
        .set(username=GENERATION_ROLE, password=password)
        .render_as_string(hide_password=False),
        others_before=[],
        others_after=[],
    )


def retire_generation(admin_url: str) -> None:
    run_script(admin_url, GENERATION_REVOKE_SQL)
