from typing import Any

from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from ai_smm import state
from ai_smm.state import SMMState

from langfuse import observe
from langfuse.langchain import CallbackHandler


class DraftItem(BaseModel):
    order: int
    text: str


class DraftPublication(BaseModel):
    title: str
    format: str
    items: list[DraftItem]
    key_facts_used: list[str] = Field(default_factory=list)


class DraftPackage(BaseModel):
    publications: list[DraftPublication]


llm = ChatOpenAI(
    model="gpt-4.1-mini",
    temperature=0.7,
)

langfuse_handler = CallbackHandler()

structured_llm = llm.with_structured_output(DraftPackage)


@observe(name="copywriter", as_type="agent")
def copywriter_node(state: SMMState) -> dict[str, Any]:
    knowledge = state["knowledge"]
    content_plan = state["content_plan"]

    editor_feedback = state.get("editor_feedback", "")
    revision_count = state.get("revision_count", 0)

    print(
        f"[COPYWRITER] revision={revision_count}, "
        f"force_bad_draft={state.get('force_bad_draft')}"
    )

    prompt = f"""
Ты — Copywriter в AI SMM-команде.

На входе у тебя есть:
1. content plan от Strategist;
2. knowledge base проекта.

Твоя задача — подготовить 3 публикации для Threads.

Для каждой публикации выбери формат из:
- single_post
- thread
- carousel

Правила:
- single_post: ровно 1 item;
- thread: от 2 до 5 items;
- carousel: ровно 1 текстовый caption item, визуалы будут добавлены отдельно;
- каждый item должен быть коротким и самостоятельным;
- не выдумывай факты;
- используй только knowledge base;
- стиль живой, технический, без рекламного пафоса;
- текст одного item — до 450 символов.

Требования:
- используй только факты из knowledge base;
- не выдумывай цифры, технологии и результаты;
- стиль: живой, технический, без рекламного пафоса;
- каждая публикация должна быть понятна сама по себе;
- текст каждого поста — до 450 символов;
- сохраняй разные углы из content plan;
- если формат thread, пока всё равно верни один короткий текст-заготовку;
- перечисли факты, которые реально использовал.

Если EDITOR FEEDBACK не пустой, исправь именно замечания редактора.
Не игнорируй предыдущую критику.

CONTENT PLAN:
{content_plan}

KNOWLEDGE BASE:
{knowledge}

EDITOR FEEDBACK:
{editor_feedback}

REVISION NUMBER:
{revision_count}
"""

    result = structured_llm.invoke(
        prompt,
        config={
            "callbacks": [langfuse_handler],
        },
    )

    draft = result.model_dump()

    if state.get("force_bad_draft", False) and revision_count == 0:
        print("[COPYWRITER] Injecting intentionally bad draft")

        draft["posts"][0]["text"] = (
            "Лучший AI-консультант на рынке. "
            "Он гарантированно подбирает товары без ошибок "
            "и всегда знает актуальные цены. "
            "Точность системы составляет 100%."
        )

    return {
        "draft": draft,
        "current_agent": "copywriter",
    }