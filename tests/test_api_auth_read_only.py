"""The whole authentication suite, re-run with the API read-only.

Login, logout, session lifetime, idle expiry, revocation and the failed
login audit must behave identically when AI_SMM_API_MUTATIONS_ENABLED is
off: the session lifecycle is the gate's only allowlist. The tests are
imported from test_api_auth; overriding `api_settings` here rebuilds the
`client` they use with the gate off.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from ai_smm.config import Settings
from tests.test_api_auth import *  # noqa: F403


@pytest.fixture
def api_settings(settings: Settings) -> Settings:
    return replace(
        settings, api_cookie_secure=False, api_mutations_enabled=False
    )
