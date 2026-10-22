
from __future__ import annotations

import re
from typing import Any

from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from ai_smm.agents.continuity import (
    check_series_continuity,
    remove_safe_sequel_teaser_from_draft,
)
from ai_smm.agents.pilot import (
    PILOT_PROJECT_ID,
    expected_publication_count,
    resolve_publishing_strategy,
)
from ai_smm.logging_setup import get_logger
from ai_smm.state import SMMState
from ai_smm.tracing import langchain_callbacks, observe


logger = get_logger(__name__)

MAX_ITEM_LENGTH = 450

FORBIDDEN_PILOT_TERMS = (
    "samsung",
    "самсунг",
)

#: A backstop for the Copywriter's own explicit forbidden list (its
#: prompt rule #5), not a fact-checker: these are the handful of blatant,
#: unconditional claims the editorial policy already names by name, kept
#: here as well so approval does not depend on the Editor's LLM noticing
#: them on a given pass. Subtler unsupported claims ("обеспечивает
#: точность и надёжность" with nothing behind it) are not something a
#: regex can tell from a legitimate engineering sentence; those stay the
#: Editor's own judgement call, with the prompt below pointing at them.
UNSUPPORTED_CLAIM_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"лучш\w*\b.{0,40}?\bна\s+рынке", re.IGNORECASE),
        'an unqualified "best on the market" claim',
    ),
    (
        re.compile(r"гарантир\w*", re.IGNORECASE),
        "a guarantee the knowledge base does not make",
    ),
    (
        re.compile(r"без\s+ошибок\b", re.IGNORECASE),
        "a claim of working without errors",
    ),
    (
        re.compile(r"100\s*%", re.IGNORECASE),
        "a 100% precision or accuracy claim",
    ),
    (
        re.compile(r"никогда\s+не\s+ошиба\w*", re.IGNORECASE),
        "a claim of never making mistakes",
    ),
)


class EditorReview(BaseModel):
    approved: bool
    score: float = Field(ge=0, le=10)
    feedback: str
    issues: list[str] = Field(default_factory=list)

    #: Judged on the series as a whole, not on any single part.
    narrative_score: float = Field(
        default=10.0,
        ge=0,
        le=10,
        description="Насколько части складываются в единое повествование",
    )
    narrative_issues: list[str] = Field(
        default_factory=list,
        description="Проблемы связности серии",
    )


llm = ChatOpenAI(
    model="gpt-4.1-mini",
    temperature=0,
    timeout=30,
    max_retries=1,
)

structured_llm = llm.with_structured_output(EditorReview)


def validate_draft(
    draft: dict[str, Any],
    project_id: str,
    expected_count: int | None = None,
) -> list[str]:
    """Deterministic checks that the LLM cannot override.

    expected_count is how many publications the Generation Job asked for.
    Without it the pilot still needs its three and another project has no
    count to enforce.
    """

    issues: list[str] = []
    publications = draft.get("publications")

    if not isinstance(publications, list) or not publications:
        return ["Draft has no valid publications list."]

    required = expected_publication_count(project_id, expected_count)

    if required is not None and len(publications) != required:
        subject = "Pilot requires" if expected_count is None else "Draft requires"
        noun = "publication" if required == 1 else "publications"
        issues.append(
            f"{subject} exactly {required} {noun}; "
            f"received {len(publications)}."
        )

    for pub_index, publication in enumerate(
        publications,
        start=1,
    ):
        if not isinstance(publication, dict):
            issues.append(
                f"Publication {pub_index}: invalid structure."
            )
            continue

        pub_format = publication.get("format")
        items = publication.get("items")

        if not isinstance(items, list) or not items:
            issues.append(
                f"Publication {pub_index}: missing items."
            )
            continue

        if pub_format in {"single_post", "carousel"}:
            expected = 1

            if len(items) != expected:
                issues.append(
                    f"Publication {pub_index}: "
                    f"{pub_format} requires 1 item."
                )

        elif pub_format == "thread":
            if not 2 <= len(items) <= 5:
                issues.append(
                    f"Publication {pub_index}: "
                    "thread requires 2-5 items."
                )

        else:
            issues.append(
                f"Publication {pub_index}: "
                f"unsupported format {pub_format!r}."
            )

        if (
            project_id == PILOT_PROJECT_ID
            and pub_format != "carousel"
        ):
            issues.append(
                f"Publication {pub_index}: pilot Copywriter "
                "must use carousel draft format."
            )

        for item_index, item in enumerate(
            items,
            start=1,
        ):
            if not isinstance(item, dict):
                issues.append(
                    f"Publication {pub_index}, item "
                    f"{item_index}: invalid structure."
                )
                continue

            text = item.get("text")

            if not isinstance(text, str) or not text.strip():
                issues.append(
                    f"Publication {pub_index}, item "
                    f"{item_index}: empty text."
                )
                continue

            if len(text) > MAX_ITEM_LENGTH:
                issue = (
                    f"Publication {pub_index}, item "
                    f"{item_index}: {len(text)} characters; "
                    f"maximum {MAX_ITEM_LENGTH}. "
                    "Shorten this text."
                )

                # The Copywriter already tried its bounded, automatic
                # shortening pass and did not converge: a controlled
                # rejection, not a crash and not a silent truncation.
                # Asking for the same "shorten it" again would just
                # repeat what already failed, so the next revision needs
                # a different instruction: write it shorter from the
                # start.
                if item.get("shortening_failed"):
                    issue += (
                        " Automatic shortening was attempted and could "
                        "not fit it; rewrite this part shorter from "
                        "scratch instead of trimming it further."
                    )

                issues.append(issue)

            for pattern, description in UNSUPPORTED_CLAIM_PATTERNS:
                if pattern.search(text):
                    issues.append(
                        f"Publication {pub_index}, item "
                        f"{item_index}: {description}; editorial policy "
                        "forbids unsupported absolute claims."
                    )

            if project_id == PILOT_PROJECT_ID:
                normalized = text.casefold()

                for forbidden in FORBIDDEN_PILOT_TERMS:
                    if forbidden in normalized:
                        issues.append(
                            f"Publication {pub_index}, item "
                            f"{item_index}: historical brand "
                            f"'{forbidden}' is forbidden in "
                            "public-facing copy. Use "
                            "'AI Catalog Consultant'."
                        )

    return issues


@observe(name="editor", as_type="evaluator")
def editor_node(state: SMMState) -> dict[str, Any]:
    knowledge = state["knowledge"]
    draft = state["draft"]
    project_id = state.get("project_id", "")

    policy = knowledge.get("editorial_policy", "")

    content_plan = state.get("content_plan") or {}

    strategy = resolve_publishing_strategy(
        project_id,
        draft.get("publishing_strategy")
        or content_plan.get("publishing_strategy"),
    )
    series_title = (
        draft.get("series_title")
        or content_plan.get("series_title")
        or ""
    )
    narrative_goal = content_plan.get("narrative_goal", "")

    # Nothing currently sets this on a content plan; the lookup is a hook
    # for when one does, not a guess at what the plan means.
    expects_sequel = bool(content_plan.get("expects_sequel"))

    # A trailing "Продолжение скоро" is scaffolding the Copywriter was
    # told not to write for the last part, and mechanically separable
    # from the real content around it -- drop it before anything else
    # looks at the draft, rather than spend a whole revision loop
    # regenerating three publications to fix one sentence. When it is
    # not cleanly separable (the sentence also makes a real claim about
    # what comes next), this makes no change at all, and the continuity
    # check below reports and blocks the draft exactly as it did before.
    draft, _sequel_teaser_removed = remove_safe_sequel_teaser_from_draft(
        draft,
        series_title=series_title,
        expects_sequel=expects_sequel,
    )

    deterministic_issues = validate_draft(
        draft=draft,
        project_id=project_id,
        expected_count=state.get("series_size"),
    )

    # Continuity is checked deterministically before the model sees the
    # draft, so a judgement call cannot wave through a missing part
    # number or two parts with identical text. This also re-verifies the
    # removal above: length, non-emptiness and every other guard run
    # again against whatever the draft now actually is.
    continuity_issues = check_series_continuity(
        draft,
        strategy=strategy,
        series_title=series_title,
        expects_sequel=expects_sequel,
    )

    # validate_draft already names every over-long item, with the
    # instruction to shorten it; the same fact twice only made the
    # feedback longer.
    continuity_issues = [
        issue
        for issue in continuity_issues
        if f"over the {MAX_ITEM_LENGTH} limit" not in issue
    ]

    prompt = f"""
Ты — Editor технического блога AI SMM Department.

Твоя задача — проверить пакет публикаций перед
отправкой в Threads.

Проверяй:

1. Соответствие каждого факта knowledge base.
2. Отсутствие выдуманных цифр, технологий
   и неподтверждённых возможностей.
3. Корректное публичное название проекта.
4. Соответствие редакционной политике.
5. Естественный технический стиль.
6. Отсутствие рекламных преувеличений.
7. Различие смысловых углов публикаций.
8. Самостоятельность каждого текста.
9. Корректное описание ошибок и ограничений.
10. Разделение результатов разных тестов.

СВЯЗНОСТЬ СЕРИИ

Это не набор независимых постов, а серия.
Оцени её и как отдельные публикации,
и как единое повествование.

Общая тема серии: {series_title or "не задана"}
Цель серии: {narrative_goal or "не задана"}
Стратегия публикации: {strategy}

Проверь:

1. Каждая часть соответствует общей теме серии.
2. Порядок частей логичен: часть N опирается
   на часть N-1, а не повторяет её.
3. Между частями нет противоречий:
   одно и то же не описано по-разному.
4. Нет ненужных повторов: вступление и описание
   проекта не дублируются в каждой части.
5. Заголовки понятны и различимы.
6. Между частями есть переходы: связь
   с предыдущей и анонс следующей там, где
   они существуют. У ПОСЛЕДНЕЙ части анонса
   быть не должно: она завершает серию, а не
   обещает продолжение.
7. Каждая часть имеет самостоятельную ценность:
   читатель, увидевший только её, получает
   законченную мысль.
8. Нумерация частей корректна и соответствует
   фактическому порядку.
9. Соблюдены ограничения Threads: не более
   {MAX_ITEM_LENGTH} символов в элементе.

Для стратегии standalone_series каждая часть
обязана обозначать серию и свой номер.

Для стратегии reply_thread нумерация в тексте
не нужна, но каждый ответ обязан продолжать
предыдущий, а не начинаться заново.

Заполни narrative_score от 0 до 10 — насколько
части складываются в единое повествование,
и narrative_issues — конкретные проблемы связности.

Оценка score остаётся оценкой отдельных публикаций.

Особое внимание:

- Не выдавай архитектурные решения
  за доказательство безошибочной работы.
- Не утверждай, что известные ограничения
  полностью устранены.
- Не объединяй результаты разных тестов.
- Не используй исторический бренд проекта
  в публичных публикациях.
- Неподтверждённые оценочные обещания — тоже
  issue, даже без явно запрещённого слова:
  «обеспечивает точность и надёжность»,
  «повышает эффективность», «гарантирует
  качество» без конкретного основания из
  knowledge base — ничем не лучше прямой
  рекламы, просто без восклицательного знака.
- Semantic guard в этом проекте убирает
  выдуманное моделью ограничение из обращения
  к каталогу — и в этом успешен. Известное,
  задокументированное ограничение в другом:
  формулировка финального ответа всё равно
  может его повторить. Текст не должен путать
  эти два утверждения: «guard иногда не
  справляется» или «ограничение не устранено» —
  искажение, даже если сама отсылка к guard
  присутствует.

Если есть существенные ошибки,
установи approved=false и объясни,
что конкретно нужно исправить.

Если текст фактологически корректен,
можешь одобрить его.

Верни оценку score от 0 до 10,
обратную связь и список замечаний.

EDITORIAL POLICY:
{policy}

KNOWLEDGE BASE:
{knowledge}

DRAFT:
{draft}
"""

    result = structured_llm.invoke(
        prompt,
        config={"callbacks": langchain_callbacks()},
    )

    all_issues = list(result.issues)
    all_issues.extend(result.narrative_issues)
    all_issues.extend(deterministic_issues)
    all_issues.extend(continuity_issues)

    # Continuity problems block approval the same way factual ones do: a
    # series whose parts contradict or repeat each other is not publishable
    # however correct each part is on its own.
    approved = (
        result.approved
        and not deterministic_issues
        and not continuity_issues
    )

    feedback_parts = []

    if result.feedback:
        feedback_parts.append(result.feedback)

    if deterministic_issues:
        feedback_parts.append(
            "Обязательные исправления:\n- "
            + "\n- ".join(deterministic_issues)
        )

    if continuity_issues:
        feedback_parts.append(
            "Связность серии — обязательные исправления:\n- "
            + "\n- ".join(continuity_issues)
        )

    if result.narrative_issues:
        feedback_parts.append(
            "Замечания по повествованию:\n- "
            + "\n- ".join(result.narrative_issues)
        )

    feedback = "\n\n".join(feedback_parts)

    score = result.score

    if deterministic_issues:
        score = min(score, 5.0)

    if continuity_issues:
        score = min(score, 5.0)

    # The series score is the weaker of the two judgements: a set of
    # individually good posts that do not follow on is still a bad series.
    narrative_score = result.narrative_score

    if continuity_issues:
        narrative_score = min(narrative_score, 5.0)

    logger.info(
        "Editor review complete",
        extra={
            "context": {
                "approved": approved,
                "score": score,
                "narrative_score": narrative_score,
                "issues": len(all_issues),
                "continuity_issues": len(continuity_issues),
                "strategy": strategy,
                "sequel_teaser_removed": _sequel_teaser_removed,
            }
        },
    )

    return {
        # Possibly the same dict Copywriter produced, possibly a copy
        # with the last part's teaser sentence removed; either way, this
        # is the version every check above actually ran against, and the
        # one the Publisher must see too.
        "draft": draft,
        "editor_approved": approved,
        "editor_score": score,
        "editor_narrative_score": narrative_score,
        "editor_feedback": feedback,
        "editor_issues": all_issues,
        "current_agent": "editor",
    }
