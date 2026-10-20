"""Generation-only agent pipeline: Strategist -> Copywriter -> Editor.

The generation worker's graph. It is ai_smm.graph.build_graph() with the
Publisher removed: where that graph goes on to the publisher node, this one
ends, and returns the same state (draft, content_plan, editor verdict,
revision_count). Nothing here imports ai_smm.agents.publisher or
ai_smm.publishing, so the graph the generation worker runs has no step that
could call a publishing API.

route_after_editor and increment_revision mirror the ones in ai_smm.graph
rather than importing them: importing ai_smm.graph would import the
Publisher. tests/test_generation_graph.py keeps the two in step.
"""
from langgraph.graph import END, START, StateGraph

from ai_smm.agents.copywriter import copywriter_node
from ai_smm.agents.editor import editor_node
from ai_smm.agents.strategist import strategist_node
from ai_smm.state import SMMState


def route_after_editor(state: SMMState) -> str:
    if state.get("editor_approved"):
        return "done"

    revision_count = state.get("revision_count", 0)
    max_revisions = state.get("max_revisions", 3)

    if revision_count >= max_revisions:
        return "done"

    return "revise"


def increment_revision(state: SMMState) -> dict:
    return {
        "revision_count": state.get("revision_count", 0) + 1,
        "current_agent": "revision",
    }


def build_generation_graph():
    graph = StateGraph(SMMState)

    graph.add_node("strategist", strategist_node)
    graph.add_node("copywriter", copywriter_node)
    graph.add_node("editor", editor_node)
    graph.add_node("increment_revision", increment_revision)

    graph.add_edge(START, "strategist")
    graph.add_edge("strategist", "copywriter")
    graph.add_edge("copywriter", "editor")

    graph.add_conditional_edges(
        "editor",
        route_after_editor,
        {
            "revise": "increment_revision",
            "done": END,
        },
    )

    graph.add_edge("increment_revision", "copywriter")

    return graph.compile()
