
from __future__ import annotations

from typing import Any

from langchain_openai import ChatOpenAI
from langfuse import observe
from langfuse.langchain import CallbackHandler
from pydantic import BaseModel, Field

from ai_smm.agents.continuity import (
    format_series_label,
    split_series_label,
    strip_series_label,
)
from ai_smm.agents.pilot import PILOT_PROJECT_ID, resolve_publishing_strategy
from ai_smm.logging_setup import get_logger
from ai_smm.state import SMMState


logger = get_logger(__name__)

#: Threads caption limit for a post carrying media.
MAX_ITEM_LENGTH = 450

#: What the Copywriter is asked for. The model does not count characters
#: and overshoots, so it aims below the hard limit.
TARGET_ITEM_LENGTH = 400

#: Room reserved for the series label the system adds after the model
#: writes the body (MAX_SERIES_TITLE_LENGTH plus the longest phrasing of
#: "— часть N из M" and the blank line). Only used to tell the model a
#: realistic body-only target; the actual combined text is what gets
#: measured against MAX_ITEM_LENGTH.
LABEL_ALLOWANCE = 90

#: Focused rewrites of one over-long item before the Editor sees it. Kept
#: small and fixed: this is a bounded number of extra LLM calls per item,
#: not a loop that keeps trying until it works.
MAX_SHORTEN_ATTEMPTS = 3

#: Slack under the limit asked of a shortening rewrite, for the same reason.
SHORTEN_MARGIN = 40


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

#: Shortens one text and nothing else: no knowledge base, no new facts.
shorten_llm = ChatOpenAI(
    model="gpt-4.1-mini",
    temperature=0,
    timeout=30,
    max_retries=1,
)

langfuse_handler = CallbackHandler()


def shorten_text(
    text: str,
    *,
    limit: int = MAX_ITEM_LENGTH,
    series_title: str = "",
) -> str:
    """Bring one item under the limit by cutting, not by rewriting the package.

    Regenerating the whole package to fix a length does not converge: the
    model fixes one part and lengthens another. Here only the over-long
    text is rewritten, with its exact length and a target, and only
    removal is allowed. The series label is split off first and put back
    verbatim, so shortening can never lose the part number.

    Returns the shortest acceptable version, which may still be over the
    limit; the Editor reports it then, as before.
    """

    if len(text) <= limit:
        return text

    label, body = split_series_label(text, series_title)
    separator = "\n\n" if label else ""
    budget = limit - len(label) - len(separator)
    best = body.strip()

    for _ in range(MAX_SHORTEN_ATTEMPTS):
        if len(best) <= budget:
            break

        target = max(budget - SHORTEN_MARGIN, budget // 2)
        prompt = f"""
Сократи текст публикации для Threads.

Сейчас в тексте {len(best)} символов. Нужно не больше {target}
символов, считая пробелы.

Правила:
- только сокращай: не добавляй фактов, цифр, названий и выводов,
  которых нет в тексте;
- сохрани главный тезис и все цифры, которые оставляешь, без изменений;
- убирай повторы, вводные слова и общие фразы;
- если в конце есть анонс следующей части, оставь его одной короткой
  фразой;
- верни только сокращённый текст, без кавычек и пояснений.

ТЕКСТ:
{best}
"""

        result = shorten_llm.invoke(
            prompt,
            config={"callbacks": [langfuse_handler]},
        )
        candidate = str(result.content).strip()

        if candidate and len(candidate) < len(best):
            best = candidate

    shortened = f"{label}{separator}{best}"

    logger.info(
        "Copywriter shortened an item",
        extra={
            "context": {
                "before": len(text),
                "after": len(shortened),
                "limit": limit,
            }
        },
    )

    return shortened


@observe(name="copywriter", as_type="agent")
def copywriter_node(state: SMMState) -> dict[str, Any]:
    knowledge = state["knowledge"]
    content_plan = state["content_plan"]

    project_id = state.get("project_id", "")
    editor_feedback = state.get("editor_feedback", "")
    revision_count = state.get("revision_count", 0)

    pilot_mode = project_id == PILOT_PROJECT_ID

    strategy = resolve_publishing_strategy(
        project_id, content_plan.get("publishing_strategy")
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

Строку «{series_title or "Название серии"} — часть N из {planned_total}»
НЕ пиши сам: систeма добавляет её перед текстом
автоматически, одинаково для всех частей. Начинай
текст сразу с сути, без заголовка-дубля этой строки.

Для КАЖДОЙ части обязательно:

1. Если это НЕ первая часть — одно короткое
   предложение о том, на чём остановились
   в предыдущей части. Не пересказывай её целиком.

2. Один главный тезис. Не пытайся вместить всё.

3. Логичное завершение: часть не должна обрываться.

4. Анонс следующей части — одна короткая строка —
   пишется ТОЛЬКО когда номер этой части МЕНЬШЕ
   общего числа частей. Прежде чем писать анонс,
   сверь номер части с {planned_total}: если часть
   последняя (номер равен {planned_total}), анонса
   не будет вообще, ни в каком виде.

5. Для ПОСЛЕДНЕЙ части (номер = {planned_total})
   ЗАПРЕЩЕНО писать «следующая часть…», «продолжение
   скоро», «продолжение следует», «далее — …» и любые
   другие слова об ожидающемся продолжении. Серия
   заканчивается на последней части — заверши мысль,
   а не анонс.

6. Текст должен быть понятен тому, кто не читал
   остальные части.

7. Приведённая выше строка серии добавляется системой
   отдельно и не входит в лимит, который ты считаешь
   сам: пиши тело так, чтобы в него поместился главный
   тезис, связь с предыдущей частью и короткий анонс,
   примерно в {TARGET_ITEM_LENGTH - LABEL_ALLOWANCE} символов.
"""

    if pilot_mode:
        format_instructions = f"""
Это пилотная серия из трёх публикаций с реальными
скриншотами проекта AI Catalog Consultant.

Подготовь ровно 3 публикации.

Для каждой публикации:
- ровно один DraftItem;
- order = 1;
- format = "carousel";
- длина тела — как указано выше в СТРАТЕГИЯ: строку серии
  не пиши сам, жёсткий предел {MAX_ITEM_LENGTH} — на текст
  целиком, уже вместе с добавленной системой строкой;
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
        if strategy == "standalone_series":
            length_note = (
                "Длина тела — как указано выше в СТРАТЕГИЯ: строку "
                "серии не пиши сам, жёсткий предел "
                f"{MAX_ITEM_LENGTH} — на текст целиком, уже вместе с "
                "добавленной системой строкой."
            )
        else:
            length_note = (
                f"Каждый item — около {TARGET_ITEM_LENGTH} символов; "
                f"жёсткий предел — {MAX_ITEM_LENGTH}, превышение не "
                "принимается."
            )

        format_instructions = f"""
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

{length_note}
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

    # A wrong shape is the Editor's to report, not a reason to stop the
    # graph: validate_draft names it, the revision loop gets to fix it, and
    # the Publisher refuses a package that still has it.
    if pilot_mode and len(draft["publications"]) != 3:
        logger.warning(
            "Pilot draft has the wrong number of publications",
            extra={"context": {"publications": len(draft["publications"])}},
        )

    # The label is the one piece of scaffolding the system writes itself
    # rather than trusting the model with: wherever the model put its own
    # attempt (start, end, or not at all), it is stripped and the single
    # canonical form is put at the front, before the length is checked.
    # This is what makes "does this part say which one it is" and "do two
    # parts open the same way" checks reliable instead of a matter of
    # the model's mood that revision.
    total_publications = len(draft["publications"])

    for publication in draft["publications"]:
        position = publication.get("position") or 0
        label = (
            format_series_label(
                draft["series_title"], position, total_publications
            )
            if strategy == "standalone_series" and total_publications > 1
            else ""
        )

        for item in publication.get("items", []):
            body = strip_series_label(
                item.get("text", ""), draft["series_title"]
            ).strip()
            combined = f"{label}\n\n{body}" if label else body

            text = shorten_text(combined, series_title=draft["series_title"])
            item["text"] = text

            # Bounded retries (MAX_SHORTEN_ATTEMPTS), not an open-ended
            # loop: if the item is still over the limit once they are
            # spent, that is a controlled rejection, not success wearing
            # a longer text -- validate_draft names it explicitly below,
            # the revision loop gets a chance to rewrite it shorter from
            # scratch, and the Publisher never sees it without that name.
            if len(text) > MAX_ITEM_LENGTH:
                item["shortening_failed"] = True

    bad_draft_injected = False

    if (
        state.get("force_bad_draft", False)
        and revision_count == 0
        and draft["publications"]
        and draft["publications"][0]["items"]
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
