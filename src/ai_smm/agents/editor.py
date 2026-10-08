
from __future__ import annotations

from typing import Any

from langchain_openai import ChatOpenAI
from langfuse import observe
from langfuse.langchain import CallbackHandler
from pydantic import BaseModel, Field

from ai_smm.state import SMMState


MAX_ITEM_LENGTH = 450
PILOT_PROJECT_ID = "ai-catalog-consultant"

FORBIDDEN_PILOT_TERMS = (
    "samsung",
    "самсунг",
)


class EditorReview(BaseModel):
    approved: bool
    score: float = Field(ge=0, le=10)
    feedback: str
    issues: list[str] = Field(default_factory=list)


llm = ChatOpenAI(
    model="gpt-4.1-mini",
    temperature=0,
    timeout=30,
    max_retries=1,
)

structured_llm = llm.with_structured_output(EditorReview)
langfuse_handler = CallbackHandler()


def validate_draft(
    draft: dict[str, Any],
    project_id: str,
) -> list[str]:
    """Deterministic checks that the LLM cannot override."""

    issues: list[str] = []
    publications = draft.get("publications")

    if not isinstance(publications, list) or not publications:
        return ["Draft has no valid publications list."]

    if project_id == PILOT_PROJECT_ID and len(publications) != 3:
        issues.append(
            "Pilot requires exactly 3 publications; "
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
                issues.append(
                    f"Publication {pub_index}, item "
                    f"{item_index}: {len(text)} characters; "
                    f"maximum {MAX_ITEM_LENGTH}. "
                    "Shorten this text."
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

    deterministic_issues = validate_draft(
        draft=draft,
        project_id=project_id,
    )

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

Особое внимание:

- Не выдавай архитектурные решения
  за доказательство безошибочной работы.
- Не утверждай, что известные ограничения
  полностью устранены.
- Не объединяй результаты разных тестов.
- Не используй исторический бренд проекта
  в публичных публикациях.

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
        config={"callbacks": [langfuse_handler]},
    )

    all_issues = list(result.issues)
    all_issues.extend(deterministic_issues)

    approved = result.approved and not deterministic_issues

    feedback_parts = []

    if result.feedback:
        feedback_parts.append(result.feedback)

    if deterministic_issues:
        feedback_parts.append(
            "Обязательные исправления:\n- "
            + "\n- ".join(deterministic_issues)
        )

    feedback = "\n\n".join(feedback_parts)

    score = result.score

    if deterministic_issues:
        score = min(score, 5.0)

    print(
        f"[EDITOR] approved={approved}, "
        f"score={score}, "
        f"issues={len(all_issues)}"
    )

    return {
        "editor_approved": approved,
        "editor_score": score,
        "editor_feedback": feedback,
        "current_agent": "editor",
    }
