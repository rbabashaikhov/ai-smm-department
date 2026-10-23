"""Local browser helpers must never target an arbitrary database/checkout."""

import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "url,enabled,success",
    [
        ("postgresql+psycopg://local@127.0.0.1:55432/smm_e2e_test", "1", True),
        ("postgresql+psycopg://local@remote.invalid:5432/smm_e2e_test", "1", False),
        ("postgresql+psycopg://local@127.0.0.1:55432/production", "1", False),
        ("postgresql+psycopg://local@127.0.0.1:55432/smm_e2e_test", "0", False),
    ],
)
def test_helper_target_and_checkout(url, enabled, success):
    env = {k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "LANG"}}
    env.update(
        AI_SMM_DATABASE_URL=url,
        AI_SMM_LOCAL_IDEATION=enabled,
        PYTHON_DOTENV_DISABLED="1",
    )
    code = (
        "import sys; "
        f"sys.path.insert(0, {str(ROOT / 'scripts')!r}); "
        "from local_browser_safety import local_settings; "
        "local_settings(); import ai_smm; print(ai_smm.__file__)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd="/tmp",
        env=env,
        text=True,
        capture_output=True,
    )
    assert (result.returncode == 0) is success
    if success:
        assert str(ROOT / "src" / "ai_smm") in result.stdout
