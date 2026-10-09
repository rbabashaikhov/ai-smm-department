
from __future__ import annotations

from typing import Any

from langchain_openai import ChatOpenAI
from langfuse import observe
from langfuse.langchain import CallbackHandler
from pydantic import BaseModel, Field

from ai_smm.logging_setup import get_logger
from ai_smm.state import SMMState


logger = get_logger(__name__)

PILOT_PROJECT_ID = "ai-catalog-consultant"

#: Threads caption limit for a post carrying media.
MAX_ITEM_LENGTH = 450


class DraftItem(BaseModel):
    order: int
    text: str


class DraftPublication(BaseModel):
    title: str
    format: str
    items: list[DraftItem]
    key_facts_used: list[str] = Field(default_factory=list)

    position: int = Field(
        default=0, description="Позиция части в серии, начиная с 1"
    )
    main_thesis: str = Field(
        default="", description="Главный тезис этой части"
    )


class DraftPackage(BaseModel):
    publications: list[DraftPublication]
    series_title: str = Field(default="")
    publishing_strategy: str = Field(default="standalone_series")


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

    strategy = (
        content_plan.get("publishing_strategy") or "standalone_series"
    )
    series_title = content_plan.get("series_title") or ""
    planned_total = content_plan.get("planned_total") or len(
        content_plan.get("ideas", [])
    )

    logger.info(
        "Copywriter starting",
        extra={
            "context": {
                "project_id": project_id,
                "revision": revision_count,
                "pilot_mode": pilot_mode,
                "strategy": strategy,
                "parts": planned_total,
            }
        },
    )

    if strategy == "reply_thread":
        series_instructions = """
СТРАТЕГИЯ: reply_thread.

Первая часть — обычный пост, он задаёт тему.
Остальные части выходят ОТВЕТАМИ на предыдущую.

Правила:

1. Первая часть формулирует проблему или контекст
   и обозначает, что будет разбор.

2. Каждый следующий ответ ПРОДОЛЖАЕТ предыдущий.
   Читатель уже прочитал то, что было выше.

3. НЕ повторяй вступление в каждой части.
   Не начинай несколько частей одинаково.
   Не представляй проект заново в каждом ответе.

4. Не нумеруй части вручную в тексте: в треде
   порядок виден сам по себе.

5. Ответы в Threads — только текст, без изображений.

6. Каждый ответ всё равно должен нести
   законченную мысль, а не обрываться.
"""
    else:
        series_instructions = f"""
СТРАТЕГИЯ: standalone_series.

Каждая часть — самостоятельный пост. Читатель может
увидеть любую из них первой.

Для КАЖДОЙ части обязательно:

1. Понятный заголовок в начале текста.

2. Обозначение серии и номера части в формате:
   «{series_title or "Название серии"} — часть N из {planned_total}»
   Используй это ровно один раз, рядом с заголовком.

3. Если это НЕ первая часть — одно короткое
   предложение о том, на чём остановились
   в предыдущей части. Не пересказывай её целиком.

4. Один главный тезис. Не пытайся вместить всё.

5. Логичное завершение: часть не должна обрываться.

6. Если следующая часть существует — одна короткая
   строка о том, что будет в ней.
   Для последней части анонса нет.

7. Текст должен быть понятен тому, кто не читал
   остальные части.
"""

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

7. Каждый текст должен быть понятен
   без чтения предыдущих публикаций.

СЕРИЯ

Это не набор независимых постов, а серия:
части читаются в заданном порядке и развивают
одну мысль.

Общая тема серии: {series_title or "см. content plan"}
Количество частей: {planned_total}

{series_instructions}

Для каждой публикации заполни:
- position — позицию части из content plan;
- main_thesis — её главный тезис.

Сохрани порядок частей из content plan.
Используй transition_from_previous и transition_to_next
из плана как основу для переходов, но пиши их
естественным языком, а не копируй дословно.

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

    draft["series_title"] = draft.get("series_title") or series_title
    draft["publishing_strategy"] = strategy

    # The model is asked for positions, but order is what the series is
    # built on, so it is assigned here rather than trusted.
    for index, publication in enumerate(
        draft.get("publications", []), start=1
    ):
        publication["position"] = index
        publication["series_total"] = len(draft["publications"])

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
        logger.warning(
            "Injecting an intentionally bad draft for the revision test"
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
