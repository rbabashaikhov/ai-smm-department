"""The generation-only graph (SMM-033B R1 FIX-01): no Publisher on its path.

Every node is replaced by a stub, and the import check runs in a fresh
interpreter, so nothing here talks to OpenAI, Langfuse or Threads.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Any

import pytest


# The agent modules build their ChatOpenAI clients at import; no request
# is made until invoke, and every node here is a stub.
os.environ.setdefault("OPENAI_API_KEY", "test-not-a-real-key")
os.environ.setdefault("LANGFUSE_TRACING_ENABLED", "false")

import ai_smm.generation_graph as generation_graph  # noqa: E402
import ai_smm.generation_worker as generation_worker_module  # noqa: E402
from ai_smm import graph as publishing_graph  # noqa: E402


PUBLISHING_GRAPH_MODULES = ("ai_smm.graph", "ai_smm.agents.publisher")
PUBLISHING_MODULES = (
    *PUBLISHING_GRAPH_MODULES,
    "ai_smm.publishing",
    "ai_smm.security.credentials",
)


def _loaded_modules_after(code: str, prefixes: tuple[str, ...]) -> list[str]:
    """Run code in a fresh interpreter; the loaded modules matching prefixes."""

    script = (
        "import sys\n"
        f"{code}\n"
        f"loaded = [m for m in sys.modules if m.startswith({prefixes!r})]\n"
        "print(','.join(sorted(loaded)))\n"
    )
    env = {
        **os.environ,
        "OPENAI_API_KEY": "test-not-a-real-key",
        "LANGFUSE_TRACING_ENABLED": "false",
        "PYTHON_DOTENV_DISABLED": "1",
    }

    proc = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr

    out = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    return [m for m in out.split(",") if m]


def _stub_pipeline(
    monkeypatch: pytest.MonkeyPatch, verdicts: list[bool]
) -> list[str]:
    calls: list[str] = []
    remaining = list(verdicts)

    def strategist(state: dict[str, Any]) -> dict[str, Any]:
        calls.append("strategist")
        return {"content_plan": {"series_title": "Серия"}}

    def copywriter(state: dict[str, Any]) -> dict[str, Any]:
        calls.append("copywriter")
        return {
            "draft": {
                "series_title": "Серия",
                "publications": [
                    {"position": 1, "format": "text", "items": [{"text": "Т"}]}
                ],
            }
        }

    def editor(state: dict[str, Any]) -> dict[str, Any]:
        calls.append("editor")
        return {
            "editor_approved": remaining.pop(0),
            "editor_score": 8.5,
            "editor_narrative_score": 9.0,
            "editor_issues": ["одна заметка"],
            "editor_feedback": "Хорошо",
        }

    monkeypatch.setattr(generation_graph, "strategist_node", strategist)
    monkeypatch.setattr(generation_graph, "copywriter_node", copywriter)
    monkeypatch.setattr(generation_graph, "editor_node", editor)

    return calls


def test_generation_graph_has_no_publisher_node() -> None:
    nodes = set(generation_graph.build_generation_graph().get_graph().nodes)

    assert {"strategist", "copywriter", "editor", "increment_revision"} <= nodes
    assert "publisher" not in nodes
    # The publishing graph is unchanged and still has it.
    assert "publisher" in set(publishing_graph.build_graph().get_graph().nodes)


def test_the_worker_builds_the_generation_only_graph() -> None:
    nodes = set(generation_worker_module._build_graph().get_graph().nodes)

    assert "editor" in nodes
    assert "publisher" not in nodes


def test_the_generation_graph_imports_nothing_from_publishing() -> None:
    code = (
        "from ai_smm.generation_graph import build_generation_graph\n"
        "build_generation_graph()"
    )

    assert _loaded_modules_after(code, PUBLISHING_MODULES) == []


def test_the_worker_never_loads_publishing_code() -> None:
    # Includes ai_smm.application.content, which would load the publishing
    # service and credentials; the worker hashes via content_hashing only.
    code = "import ai_smm.generation_worker as w\nw._build_graph()"

    assert _loaded_modules_after(code, PUBLISHING_MODULES) == []
    assert _loaded_modules_after(
        code, ("ai_smm.application.content",)
    ) == ["ai_smm.application.content_hashing"]


@pytest.mark.parametrize(
    "state",
    [
        {},
        {"editor_approved": True},
        {"editor_approved": False},
        {"editor_approved": False, "revision_count": 2},
        {"editor_approved": False, "revision_count": 3},
        {"editor_approved": False, "revision_count": 1, "max_revisions": 1},
        {"editor_approved": True, "revision_count": 5, "max_revisions": 1},
    ],
)
def test_routing_matches_the_publishing_graph(state: dict[str, Any]) -> None:
    expected = {"publish": "done", "revise": "revise"}

    assert generation_graph.route_after_editor(state) == expected[
        publishing_graph.route_after_editor(state)
    ]
    assert generation_graph.increment_revision(
        state
    ) == publishing_graph.increment_revision(state)


def test_an_approved_draft_ends_after_the_editor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ai_smm.publishing.threads as threads

    def no_publisher(*_args, **_kwargs):
        pytest.fail("the generation graph must not construct a publisher")

    monkeypatch.setattr(threads.ThreadsPublisher, "__init__", no_publisher)
    calls = _stub_pipeline(monkeypatch, [False, True])

    result = generation_graph.build_generation_graph().invoke(
        {"project_id": "p", "revision_count": 0, "max_revisions": 3}
    )

    assert calls == ["strategist", "copywriter", "editor", "copywriter", "editor"]
    assert result["revision_count"] == 1
    assert result["editor_approved"] is True
    assert result["editor_score"] == 8.5
    assert result["editor_narrative_score"] == 9.0
    assert result["editor_issues"] == ["одна заметка"]
    assert result["editor_feedback"] == "Хорошо"
    assert result["draft"]["publications"]
    assert result["content_plan"] == {"series_title": "Серия"}
    assert "publication" not in result


def test_a_rejected_draft_ends_at_the_revision_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _stub_pipeline(monkeypatch, [False, False, False])

    result = generation_graph.build_generation_graph().invoke(
        {"project_id": "p", "revision_count": 0, "max_revisions": 2}
    )

    assert calls.count("editor") == 3
    assert calls[-1] == "editor"
    assert result["revision_count"] == 2
    assert result["editor_approved"] is False
    assert "publication" not in result
