"""Existing API suites, re-run with the API connected as ai_smm_cc.

Every module here star-imports one suite that holds entirely in the
production privilege model -- reads, the session lifecycle, the read-only
gate -- and this conftest switches only the API's connection to the
restricted role. Fixtures keep seeding data as the owner, as the worker
and the CLI do in production.

Suites that exercise business commands with mutations ON are not here:
with this role PostgreSQL refuses those commands, which is the point
(tests/test_db_least_privilege.py proves it for each command). To see
the whole picture, run every suite with AI_SMM_TEST_API_ROLE=restricted.

Skipped without AI_SMM_TEST_ADMIN_DATABASE_URL, like the role itself.
"""
from __future__ import annotations

import pytest
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture
def api_session_factory(
    restricted_session_factory: sessionmaker[Session],
) -> sessionmaker[Session]:
    return restricted_session_factory
