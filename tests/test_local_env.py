"""OI-6: no module loads a .env when imported; entry points opt in.

The repository's .env holds a developer's real tokens. These tests never
read it: the import check replaces dotenv's functions with spies before
anything is imported, in a fresh interpreter, and the loader test points
find_dotenv at a temporary file.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest


SRC = Path(__file__).resolve().parents[1] / "src" / "ai_smm"

#: Every module an import of the package's processes pulls in.
IMPORTED = (
    "ai_smm.publishing.threads",
    "ai_smm.storage",
    "ai_smm.publishing.service",
    "ai_smm.worker",
    "ai_smm.cli",
    "ai_smm.api.app",
    "ai_smm.application.content",
    "ai_smm.application.social_accounts",
    "ai_smm.platform_identity",
)

_SPY = """
import dotenv, dotenv.main
calls = []
def spy(*args, **kwargs):
    calls.append("called")
    return False
dotenv.load_dotenv = dotenv.main.load_dotenv = spy
def find_spy(*args, **kwargs):
    calls.append("find")
    return ""
dotenv.find_dotenv = dotenv.main.find_dotenv = find_spy
import importlib
for name in {modules!r}:
    importlib.import_module(name)
print(len(calls))
"""


def test_importing_the_package_never_loads_a_dotenv() -> None:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("THREADS_", "OPENAI_", "LANGFUSE_", "STORAGE_"))
    }
    # Belt and braces: even a regression could not read a real file.
    env["PYTHON_DOTENV_DISABLED"] = "1"

    result = subprocess.run(
        [sys.executable, "-c", _SPY.format(modules=IMPORTED)],
        capture_output=True,
        text=True,
        env=env,
        check=True,
        timeout=120,
    )

    assert result.stdout.strip().splitlines()[-1] == "0", result.stdout


def _module_level_calls(tree: ast.Module, name: str) -> list[int]:
    """Lines of calls to `name` outside every function and class body."""

    found: list[int] = []

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue

        for child in ast.walk(node):
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Name)
                and child.func.id == name
            ):
                found.append(child.lineno)

    return found


def test_no_module_of_the_package_calls_load_dotenv_at_import() -> None:
    offenders = []

    for source in sorted(SRC.rglob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"))

        for line in _module_level_calls(tree, "load_dotenv"):
            offenders.append(f"{source.relative_to(SRC)}:{line}")

    assert offenders == []


def test_the_loader_reads_the_file_and_never_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import dotenv

    from ai_smm.local_env import load_local_dotenv

    env_file = tmp_path / ".env"
    env_file.write_text("SMM_LOCAL_ENV_A=from-file\nSMM_LOCAL_ENV_B=from-file\n")
    monkeypatch.setattr(dotenv, "find_dotenv", lambda *a, **k: str(env_file))
    monkeypatch.delenv("PYTHON_DOTENV_DISABLED", raising=False)
    monkeypatch.delenv("SMM_LOCAL_ENV_A", raising=False)
    monkeypatch.setenv("SMM_LOCAL_ENV_B", "already-set")

    assert load_local_dotenv() is True
    assert os.environ["SMM_LOCAL_ENV_A"] == "from-file"
    # A variable the environment already has -- every one a container
    # gets from compose -- is never replaced.
    assert os.environ["SMM_LOCAL_ENV_B"] == "already-set"

    monkeypatch.delenv("SMM_LOCAL_ENV_A")


def test_the_loader_honours_the_disable_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import dotenv

    from ai_smm.local_env import load_local_dotenv

    env_file = tmp_path / ".env"
    env_file.write_text("SMM_LOCAL_ENV_C=from-file\n")
    monkeypatch.setattr(dotenv, "find_dotenv", lambda *a, **k: str(env_file))
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.delenv("SMM_LOCAL_ENV_C", raising=False)

    load_local_dotenv()

    assert "SMM_LOCAL_ENV_C" not in os.environ


def test_no_dotenv_found_is_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import dotenv

    from ai_smm.local_env import load_local_dotenv

    monkeypatch.setattr(dotenv, "find_dotenv", lambda *a, **k: "")

    assert load_local_dotenv() is False


def test_the_cli_entry_point_opts_in_before_reading_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from ai_smm import cli

    order: list[str] = []
    real_settings = cli.get_settings
    monkeypatch.setattr(cli, "load_local_dotenv", lambda: order.append("dotenv"))
    monkeypatch.setattr(
        cli, "get_settings", lambda: order.append("settings") or real_settings()
    )

    assert (
        cli.main(
            ["credentials", "keygen", "--private-key-file", str(tmp_path / "k.pem")]
        )
        == 0
    )
    assert order[:2] == ["dotenv", "settings"]


def test_the_worker_entry_point_opts_in_before_reading_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ai_smm.worker as worker

    order: list[str] = []
    real_settings = worker.get_settings
    monkeypatch.setattr(worker, "load_local_dotenv", lambda: order.append("dotenv"))
    monkeypatch.setattr(
        worker, "get_settings", lambda: order.append("settings") or real_settings()
    )
    monkeypatch.setattr(worker.Worker, "run", lambda self: None)

    assert worker.main() == 0
    assert order[:2] == ["dotenv", "settings"]
