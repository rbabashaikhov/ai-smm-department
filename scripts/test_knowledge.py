from pprint import pprint

from ai_smm.knowledge.loader import load_project_knowledge


knowledge = load_project_knowledge("ai-catalog-consultant")

print("PROJECT:")
print(knowledge["project_id"])

print("\nFILES:")
pprint(list(knowledge["files"].keys()))

print("\nASSETS:")
pprint(knowledge["assets"])