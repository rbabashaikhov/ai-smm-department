from typing import Any

from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from ai_smm.state import SMMState

from langfuse import observe
from langfuse.langchain import CallbackHandler

class ContentIdea(BaseModel):
    title: str = Field(description="Короткий заголовок публикации")
    angle: str = Field(description="Основная идея поста")
    format: str = Field(description="Формат: single_post, thread или carousel")
    key_facts: list[str] = Field(
        description="Факты из knowledge base, на которых строится публикация"
    )


class ContentPlan(BaseModel):
    project_name: str
    ideas: list[ContentIdea]


llm = ChatOpenAI(
    model="gpt-4.1-mini",
    temperature=0.4,
    timeout=30,
    max_retries=1,
)

langfuse_handler = CallbackHandler()

structured_llm = llm.with_structured_output(ContentPlan)


@observe(name="strategist", as_type="agent")
def strategist_node(state: SMMState) -> dict[str, Any]:
    knowledge = state["knowledge"]

    prompt = f"""
Ты — Strategist в AI SMM-команде.

Твоя задача — подготовить контент-план из 3 публикаций для Threads
по одному техническому проекту.

Используй ТОЛЬКО факты из knowledge base ниже.
Ничего не выдумывай.

Нам нужны три разных угла:
1. обзор проекта;
2. техническое решение;
3. проблема / ошибка / lesson learned.

Не делай три одинаковых рекламных поста.

Knowledge base:
{knowledge}
"""

    result = structured_llm.invoke(
        prompt,
        config={
            "callbacks": [langfuse_handler],
        },
    )

    return {
        "content_plan": result.model_dump(),
        "current_agent": "strategist",
    }