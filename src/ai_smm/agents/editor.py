from typing import Any
from unittest import result

from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from ai_smm.state import SMMState

from langfuse import observe
from langfuse.langchain import CallbackHandler


class EditorReview(BaseModel):
    approved: bool
    score: float = Field(ge=0, le=10)
    feedback: str
    issues: list[str] = Field(default_factory=list)


llm = ChatOpenAI(
    model="gpt-4.1-mini",
    temperature=0,
)

langfuse_handler = CallbackHandler()

structured_llm = llm.with_structured_output(EditorReview)


@observe(name="editor", as_type="evaluator")
def editor_node(state: SMMState) -> dict[str, Any]:
    knowledge = state["knowledge"]
    draft = state["draft"]

    prompt = f"""
Ты — Editor в AI SMM-команде.

Проверь черновики постов по следующим критериям:

1. Факты должны соответствовать knowledge base.
2. Нельзя придумывать цифры, технологии, результаты или возможности.
3. Текст должен быть естественным, без рекламного пафоса.
4. Каждый пост должен быть понятен сам по себе.
5. Каждый текст должен быть не длиннее 450 символов.
6. Посты должны отличаться по смысловому углу.
7. Любые утверждения вроде "100% точность", "без ошибок",
   "гарантированно", "лучший на рынке" должны быть отклонены,
   если такого факта нет в knowledge base.

8. Если найден хотя бы один выдуманный количественный показатель
   или неподтверждённое абсолютное утверждение,
   обязательно установи approved=false.

9. Проверяй структуру формата:
   - single_post должен содержать ровно 1 item;
   - thread должен содержать от 2 до 5 items;
   - carousel должен содержать 1 caption item.
10. Каждый item должен быть не длиннее 450 символов.

Если есть существенная проблема — approved=false.

Если проблема только стилистическая и не критичная,
можно approved=true, но указать замечание.

KNOWLEDGE BASE:
{knowledge}

DRAFT:
{draft}
"""

    result = structured_llm.invoke(
        prompt,
        config={
            "callbacks": [langfuse_handler],
        },
    )

    return {
        "editor_approved": result.approved,
        "editor_score": result.score,
        "editor_feedback": result.feedback,
        "current_agent": "editor",
    }