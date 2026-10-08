from langgraph.graph import END, START, StateGraph

from ai_smm.agents.copywriter import copywriter_node
from ai_smm.agents.editor import editor_node
from ai_smm.agents.publisher import publisher_node
from ai_smm.agents.strategist import strategist_node
from ai_smm.state import SMMState


def route_after_editor(state: SMMState) -> str:
    if state.get("editor_approved"):
        return "publish"

    revision_count = state.get("revision_count", 0)
    max_revisions = state.get("max_revisions", 3)

    if revision_count >= max_revisions:
        return "publish"

    return "revise"


def increment_revision(state: SMMState) -> dict:
    return {
        "revision_count": state.get("revision_count", 0) + 1,
        "current_agent": "revision",
    }


def build_graph():
    graph = StateGraph(SMMState)

    graph.add_node("strategist", strategist_node)
    graph.add_node("copywriter", copywriter_node)
    graph.add_node("editor", editor_node)
    graph.add_node("increment_revision", increment_revision)
    graph.add_node("publisher", publisher_node)

    graph.add_edge(START, "strategist")
    graph.add_edge("strategist", "copywriter")
    graph.add_edge("copywriter", "editor")

    graph.add_conditional_edges(
        "editor",
        route_after_editor,
        {
            "revise": "increment_revision",
            "publish": "publisher",
        },
    )

    graph.add_edge("increment_revision", "copywriter")
    graph.add_edge("publisher", END)

    return graph.compile()