from dotenv import load_dotenv


load_dotenv()

from pprint import pprint

from langfuse import get_client, observe, propagate_attributes

from ai_smm.graph import build_graph
from ai_smm.knowledge.loader import load_project_knowledge


@observe(name="ai-smm-pipeline", as_type="agent")
def run_pipeline():
    graph = build_graph()

    initial_state = {
        "project_id": "ai-catalog-consultant",
        "knowledge": load_project_knowledge(
            "ai-catalog-consultant"
        ),
        "revision_count": 0,
        "max_revisions": 3,
        "errors": [],
        "publish_live": False,
    }

    with propagate_attributes(
        trace_name="ai-smm-pipeline",
        metadata={
            "project_id": "ai-catalog-consultant",
            "platform": "threads",
            "max_revisions": "3",
            "environment": "local",
            "test_type": "normal-run",
        },
        tags=[
            "otus",
            "langgraph",
            "multi-agent",
            "threads",
        ],
    ):
        return graph.invoke(initial_state)


result = run_pipeline()

print("\n=== CONTENT PLAN ===")
pprint(result["content_plan"])

print("\n=== FINAL DRAFT ===")
pprint(result["draft"])

print("\n=== EDITOR ===")
print("Approved:", result.get("editor_approved"))
print("Score:", result.get("editor_score"))
print("Feedback:", result.get("editor_feedback"))
print("Revisions:", result.get("revision_count"))

print("\n=== PUBLICATION ===")
pprint(result.get("publication"))

# Чтобы CLI не завершился раньше отправки trace
langfuse = get_client()
langfuse.flush()