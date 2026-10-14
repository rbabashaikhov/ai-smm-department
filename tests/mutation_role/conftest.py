"""The API's business-command suites, re-run connected as ai_smm_cc_rw.

Every module here star-imports one suite that sends business commands
with mutations ON. This conftest switches only the API's connection to
the mutation role (SMM-025C, FU-025B-1); fixtures keep seeding data as the
owner, as the worker and the CLI do in production. Passing here is the
proof that the role's grants are enough for every command the API makes;
tests/test_db_mutation_role.py is the proof that they are nothing more.

Skipped without AI_SMM_TEST_ADMIN_DATABASE_URL, like the role itself.
"""
from __future__ import annotations

import pytest
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture
def api_session_factory(
    mutation_session_factory: sessionmaker[Session],
) -> sessionmaker[Session]:
    return mutation_session_factory
