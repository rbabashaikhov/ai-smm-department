from dotenv import load_dotenv

load_dotenv()

from pprint import pprint

from ai_smm.graph import build_graph
from ai_smm.knowledge.loader import load_project_knowledge


graph = build_graph()

result = graph.invoke(
    {
        "project_id": "ai-catalog-consultant",
        "knowledge": load_project_knowledge(
            "ai-catalog-consultant"
        ),
        "revision_count": 0,
        "max_revisions": 3,
        "errors": [],
        "force_bad_draft": True,
    }
)

print("\n=== FINAL RESULT ===")
print("Approved:", result.get("editor_approved"))
print("Score:", result.get("editor_score"))
print("Feedback:", result.get("editor_feedback"))
print("Revisions:", result.get("revision_count"))

print("\n=== FINAL DRAFT ===")
pprint(result.get("draft"))

print("\n=== PUBLICATION ===")
pprint(result.get("publication"))

print("Bad draft injected:", result.get("bad_draft_injected"))