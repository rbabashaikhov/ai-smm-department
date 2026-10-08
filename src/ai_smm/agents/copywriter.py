
from __future__ import annotations

from typing import Any

from langchain_openai import ChatOpenAI
from langfuse import observe
from langfuse.langchain import CallbackHandler
from pydantic import BaseModel, Field

from ai_smm.state import SMMState


PILOT_PROJECT_ID = "ai-catalog-consultant"


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
    temperature=0.5,
    timeout=30,
    max_retries=1,
)

structured_llm = llm.with_structured_output(DraftPackage)

langfuse_handler = CallbackHandler()


@observe(name="copywriter", as_type="agent")
def copywriter_node(state: SMMState) -> dict[str, Any]:
    knowledge = state["knowledge"]
    content_plan = state["content_plan"]

    project_id = state.get("project_id", "")
    editor_feedback = state.get("editor_feedback", "")
    revision_count = state.get("revision_count", 0)

    pilot_mode = project_id == PILOT_PROJECT_ID

    print(
        f"[COPYWRITER] project={project_id}, "
        f"revision={revision_count}, "
        f"pilot_mode={pilot_mode}"
    )

    if pilot_mode:
        format_instructions = """
Это пилотная серия из трёх публикаций с реальными
скриншотами проекта AI Catalog Consultant.

Подготовь ровно 3 публикации.

Для каждой публикации:
- ровно один DraftItem;
- order = 1;
- format = "carousel";
- текст не длиннее 450 символов;
- не включай ссылки на изображения в текст;
- не перечисляй имена файлов.

Publisher самостоятельно распределит изображения:
1. Рекомендация и уточнение бюджета.
2. Сравнение моделей.
3. Компромисс при экономии.

Важно: format="carousel" здесь означает публикацию
с визуальными материалами. Publisher определит
окончательный формат Threads по количеству изображений.

Не изменяй порядок публикаций в content plan.
"""
    else:
        format_instructions = """
Подготовь публикации в форматах content plan.

Допустимые форматы:

single_post:
- ровно один DraftItem.

thread:
- от 2 до 5 DraftItem;
- каждый item должен быть самостоятельным;
- order должен соответствовать позиции элемента.

carousel:
- ровно один DraftItem с подписью;
- визуальные материалы добавляются отдельно.

Каждый item должен содержать максимум 450 символов.
"""

    prompt = f"""
Ты — Copywriter мультиагентной AI SMM-команды.

Цель блога — рассказывать о реальных технических
проектах автора: архитектуре, реализации,
экспериментах, ошибках и инженерных выводах.

Это технический блог, а не рекламный канал.

У тебя есть:
1. Content plan от Strategist.
2. Подготовленная knowledge base проекта.
3. Замечания Editor, если текст исправляется.

ЗАДАЧА

Подготовь публикации для Threads строго
по content plan.

ОБЯЗАТЕЛЬНЫЕ ПРАВИЛА

1. Используй только подтверждённые факты
   из knowledge base.

2. Не придумывай технологии, цифры, результаты
   тестов, архитектурные решения и возможности.

3. Сохраняй порядок и смысловые углы публикаций,
   определённые Strategist.

4. Не превращай технические материалы
   в рекламные тексты.

5. Не используй неподтверждённые абсолютные
   утверждения:
   - лучший на рынке;
   - работает без ошибок;
   - гарантирует результат;
   - точность 100%.

6. Пиши естественным техническим языком.

7. Каждый текст должен быть понятен
   без чтения предыдущих публикаций.

8. Не добавляй вымышленные цитаты
   пользователей или результаты исследований.

9. Не утверждай, что известные ограничения
   проекта полностью устранены.

10. Для каждой публикации заполни
    key_facts_used фактами из knowledge base,
    на которых основан текст.

ФОРМАТ ВЫВОДА

{format_instructions}

CONTENT PLAN:
{content_plan}

KNOWLEDGE BASE:
{knowledge}

EDITOR FEEDBACK:
{editor_feedback}

REVISION NUMBER:
{revision_count}

Если EDITOR FEEDBACK содержит замечания:
- исправь указанные проблемы;
- сохрани подтверждённые факты;
- не добавляй новые неподтверждённые утверждения;
- верни полный исправленный пакет публикаций.
"""

    result = structured_llm.invoke(
        prompt,
        config={
            "callbacks": [langfuse_handler],
        },
    )

    draft = result.model_dump()

    if pilot_mode:
        if len(draft["publications"]) != 3:
            raise ValueError(
                "Pilot requires exactly 3 publications."
            )

        for index, publication in enumerate(
            draft["publications"],
            start=1,
        ):
            if len(publication["items"]) != 1:
                raise ValueError(
                    f"Publication {index} must contain "
                    "exactly one item in pilot mode."
                )


    bad_draft_injected = False

    if (
        state.get("force_bad_draft", False)
        and revision_count == 0
    ):
        print(
            "[COPYWRITER] Injecting intentionally "
            "bad draft for revision test"
        )

        draft["publications"][0]["items"][0]["text"] = (
            "Лучший AI-консультант на рынке. "
            "Он гарантированно подбирает товары "
            "без ошибок и всегда знает актуальные цены. "
            "Точность системы составляет 100%."
        )

        bad_draft_injected = True

    return {
        "draft": draft,
        "current_agent": "copywriter",
        "bad_draft_injected": bad_draft_injected,
    }
