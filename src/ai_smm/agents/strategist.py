from typing import Any

from langchain_openai import ChatOpenAI
from langfuse import observe
from langfuse.langchain import CallbackHandler
from pydantic import BaseModel, Field

from ai_smm.state import SMMState


class ContentIdea(BaseModel):
    title: str = Field(description="Короткий заголовок публикации")
    angle: str = Field(description="Основная идея поста")
    format: str = Field(description="Формат: single_post, thread или carousel")
    key_facts: list[str] = Field(
        description="Факты из knowledge base, на которых строится публикация"
    )

    # --- narrative continuity -------------------------------------------
    position: int = Field(
        default=0,
        description="Позиция части в серии, начиная с 1",
    )
    main_thesis: str = Field(
        default="",
        description="Главный тезис именно этой части, одним предложением",
    )
    standalone_value: str = Field(
        default="",
        description=(
            "Что читатель унесёт из этой части, если не читал остальные"
        ),
    )
    transition_from_previous: str = Field(
        default="",
        description=(
            "Короткая связь с предыдущей частью. Пусто для первой части"
        ),
    )
    transition_to_next: str = Field(
        default="",
        description=(
            "Чем эта часть подводит к следующей. Пусто для последней части"
        ),
    )


class ContentPlan(BaseModel):
    project_name: str

    # --- the series as a whole -------------------------------------------
    series_title: str = Field(
        default="",
        description="Общая тема серии, одна строка",
    )
    series_description: str = Field(
        default="",
        description="О чём серия целиком, 1-2 предложения",
    )
    target_audience: str = Field(
        default="",
        description="Для кого написана серия",
    )
    narrative_goal: str = Field(
        default="",
        description=(
            "Главная идея: что читатель должен понять, прочитав всю серию"
        ),
    )
    publishing_strategy: str = Field(
        default="standalone_series",
        description=(
            "standalone_series — каждая часть самостоятельный пост; "
            "reply_thread — первая часть задаёт тему, остальные ответы"
        ),
    )
    publishing_strategy_reason: str = Field(
        default="",
        description="Почему выбрана именно эта стратегия",
    )
    planned_total: int = Field(
        default=0,
        description="Сколько публикаций в серии",
    )

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

    series_size = state.get("series_size", 3)
    requested_strategy = state.get("publishing_strategy", "")

    strategy_hint = (
        f"\nЗаказчик уже выбрал стратегию публикации: "
        f"{requested_strategy}. Используй именно её.\n"
        if requested_strategy
        else """
Выбери стратегию публикации сам и объясни выбор:

- standalone_series — части выходят как отдельные посты.
  Подходит, когда у каждой части своя ценность и читатель
  может прийти с любой из них.

- reply_thread — первая часть задаёт тему, остальные выходят
  ответами на неё. Подходит, когда части теряют смысл
  по отдельности и важен непрерывный разбор.
  Внимание: ответы в Threads — только текст, без изображений.
"""
    )

    prompt = f"""
Ты — Strategist в AI SMM-команде.

Твоя задача — подготовить не набор независимых постов,
а СЕРИЮ из {series_size} публикаций для Threads
по одному техническому проекту.

Серия — это единое повествование: части читаются
в заданном порядке и развивают одну мысль.

Используй ТОЛЬКО факты из knowledge base ниже.
Ничего не выдумывай.

СНАЧАЛА определи серию целиком:

1. series_title — общая тема.
2. series_description — о чём серия.
3. target_audience — для кого.
4. narrative_goal — что читатель должен понять,
   прочитав все части.
5. planned_total — количество публикаций.
{strategy_hint}

ЗАТЕМ спланируй каждую часть:

1. position — позиция в серии, начиная с 1.
2. title — понятный заголовок.
3. main_thesis — один главный тезис этой части.
4. standalone_value — что читатель унесёт из этой части,
   даже если не читал остальные.
5. transition_from_previous — короткая связь с предыдущей
   частью. Для первой части оставь пустым.
6. transition_to_next — чем эта часть подводит к следующей.
   Для последней части оставь пустым.
7. key_facts — факты из knowledge base.

ОБЯЗАТЕЛЬНЫЕ ТРЕБОВАНИЯ

1. Логическая последовательность: часть N должна
   опираться на часть N-1, а не повторять её.

2. Каждая часть имеет самостоятельную ценность.
   Читатель, увидевший только её, должен получить
   законченную мысль.

3. Разные смысловые углы. Не делай несколько частей
   об одном и том же.

4. Это технический блог, а не рекламный канал.

Для технического проекта обычно работает такая логика:
обзор задачи → выбранное решение → проблема, ошибка
или инженерный вывод. Но следуй фактам из knowledge base,
а не этому шаблону.

Knowledge base:
{knowledge}
"""

    result = structured_llm.invoke(
        prompt,
        config={
            "callbacks": [langfuse_handler],
        },
    )

    plan = result.model_dump()

    # Positions are the backbone of the series: renumber rather than
    # trust the model, so a gap or a duplicate cannot reach the queue.
    for index, idea in enumerate(plan.get("ideas", []), start=1):
        idea["position"] = index

    total = len(plan.get("ideas", []))
    plan["planned_total"] = total

    if plan.get("publishing_strategy") not in {
        "standalone_series",
        "reply_thread",
    }:
        plan["publishing_strategy"] = "standalone_series"

    # The last part announces nothing; the first follows nothing.
    if plan.get("ideas"):
        plan["ideas"][0]["transition_from_previous"] = ""
        plan["ideas"][-1]["transition_to_next"] = ""

    return {
        "content_plan": plan,
        "current_agent": "strategist",
    }