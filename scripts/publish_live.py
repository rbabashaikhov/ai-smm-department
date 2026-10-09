from dotenv import load_dotenv


load_dotenv()

from pprint import pprint

from langfuse import get_client, observe

from ai_smm.graph import build_graph
from ai_smm.knowledge.loader import load_project_knowledge


@observe(
    name="ai-smm-live-publication",
    as_type="agent",
)
def run_live():
    graph = build_graph()

    state = {
        "project_id": "ai-catalog-consultant",
        "knowledge": load_project_knowledge(
            "ai-catalog-consultant"
        ),
        "revision_count": 0,
        "max_revisions": 3,
        "errors": [],
        "publish_live": True,
    }

    return graph.invoke(state)


result = run_live()

print("\n=== LIVE PUBLICATION ===")
pprint(result["publication"])

get_client().flush()