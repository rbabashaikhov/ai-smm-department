from dotenv import load_dotenv

load_dotenv()

from pprint import pprint

from ai_smm.agents.strategist import strategist_node
from ai_smm.knowledge.loader import load_project_knowledge


knowledge = load_project_knowledge("ai-catalog-consultant")

state = {
    "project_id": "ai-catalog-consultant",
    "knowledge": knowledge,
}

result = strategist_node(state)

pprint(result["content_plan"])