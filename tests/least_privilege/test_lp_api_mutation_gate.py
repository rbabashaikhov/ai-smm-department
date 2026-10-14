"""tests/test_api_mutation_gate.py, the API connected as ai_smm_cc (conftest.py)."""

from tests.test_api_mutation_gate import *  # noqa: F403


# Signing in and out through smm-api itself is not part of the packaged
# configuration (SMM-025D, grants G2): this role may not create or revoke a
# session. Those two run as the owner role in tests/test_api_mutation_gate.py
# and against smm-auth in tests/test_auth_service.py.
del test_login_csrf_me_and_logout_work_read_only  # noqa: F821
del test_a_failed_login_is_still_audited_read_only  # noqa: F821
