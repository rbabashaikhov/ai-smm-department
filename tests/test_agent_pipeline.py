"""The agent pipeline without a model: regressions from the functional E2E.

The functional E2E of 2026-10-15 (docs/reports/smm-026a/functional-e2e-20261015)
ran the real graph eleven times and the Editor approved nothing, for four
reasons that had nothing to do with the quality of the text:

* D1 -- the pilot with reply_thread: carousel required, media in a reply
  forbidden; no draft could satisfy both.
* D2 -- the series label «<long title> — часть N из M» filled the 60
  characters compared for a repeated opening, so every part "opened the
  same way".
* D3 -- regenerating the whole package to fix a length never converged.
* D4 -- a package with the wrong number of publications raised and took
  the graph down with it.

Every model is replaced by a fake, and any network connection fails the
test, so these run offline and cost nothing.
"""

from __future__ import annotations

import os
import socket
from types import SimpleNamespace
from typing import Any

import pytest


# The agent modules build their ChatOpenAI clients at import; no request
# is made until invoke, and every invoke here goes to a fake.
os.environ.setdefault("OPENAI_API_KEY", "test-not-a-real-key")
os.environ.setdefault("LANGFUSE_TRACING_ENABLED", "false")

from ai_smm.agents import copywriter, editor, strategist  # noqa: E402
from ai_smm.agents.continuity import (  # noqa: E402
    check_series_continuity,
    split_series_label,
)
from ai_smm.agents.copywriter import (  # noqa: E402
    DraftItem,
    DraftPackage,
    DraftPublication,
    shorten_text,
)
from ai_smm.agents.editor import EditorReview  # noqa: E402
from ai_smm.agents.pilot import (  # noqa: E402
    PILOT_PROJECT_ID,
    expected_publication_count,
    resolve_publishing_strategy,
)
from ai_smm.agents.strategist import ContentIdea, ContentPlan  # noqa: E402
from ai_smm.graph import build_graph  # noqa: E402
from ai_smm.knowledge.loader import load_project_knowledge  # noqa: E402


LONG_SERIES_TITLE = (
    "Технический разбор AI Catalog Consultant: архитектура, решения и уроки"
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(self, address):  # noqa: ANN001
        raise AssertionError(f"network call in an offline test: {address!r}")

    monkeypatch.setattr(socket.socket, "connect", refuse)


class FakeModel:
    """Stands in for a structured-output model; replays given answers."""

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []

    def invoke(self, prompt: str, config: Any = None) -> Any:
        self.prompts.append(prompt)
        answer = self.answers[0] if len(self.answers) == 1 else self.answers.pop(0)

        return answer(prompt) if callable(answer) else answer


def plan(strategy: str = "reply_thread") -> ContentPlan:
    return ContentPlan(
        project_name="AI Catalog Consultant",
        series_title=LONG_SERIES_TITLE,
        publishing_strategy=strategy,
        publishing_strategy_reason="части связаны",
        ideas=[
            ContentIdea(
                title=title,
                angle="угол",
                format="text",
                key_facts=["LLM не является источником каталожной истины."],
                main_thesis=thesis,
            )
            for title, thesis in (
                (
                    "Постановка задачи AI Catalog Consultant",
                    "LLM не источник фактов каталога",
                ),
                (
                    "Архитектура выбора товаров каталога",
                    "SQL выбирает товары, векторы подтверждают",
                ),
                (
                    "Ошибки и уроки выбора товаров",
                    "выдуманные ограничения ломают выбор товаров",
                ),
            )
        ],
    )


def labelled(position: int, body: str, title: str = LONG_SERIES_TITLE) -> str:
    return f"{title} — часть {position} из 3\n\n{body}"


GOOD_BODIES = (
    "Знакомимся с AI Catalog Consultant: LLM не источник цен и наличия "
    "товаров каталога, факты берутся из PostgreSQL. Дальше — архитектура.",
    "В первой части мы говорили о каталоге и фактах. Выбор товаров каталога "
    "делает SQL, векторный поиск только подтверждает. Дальше — ошибки.",
    "После архитектуры выбора товаров каталога — уроки: запрос «подешевле» "
    "приводил к выдуманным ограничениям; semantic guard их убирает.",
)


def package(
    bodies: tuple[str, ...] = GOOD_BODIES, *, publication_format: str = "carousel"
) -> DraftPackage:
    return DraftPackage(
        series_title=LONG_SERIES_TITLE,
        publications=[
            DraftPublication(
                title=title,
                format=publication_format,
                items=[DraftItem(order=1, text=labelled(position, body))],
                position=position,
                main_thesis=thesis,
            )
            for position, (title, thesis, body) in enumerate(
                zip(
                    (
                        "Постановка задачи AI Catalog Consultant",
                        "Архитектура выбора товаров каталога",
                        "Ошибки и уроки выбора товаров",
                    ),
                    (
                        "LLM не источник фактов каталога",
                        "SQL выбирает товары, векторы подтверждают",
                        "выдуманные ограничения ломают выбор товаров",
                    ),
                    bodies,
                    strict=True,
                ),
                start=1,
            )
        ],
    )


def approve(score: float = 9.0) -> EditorReview:
    return EditorReview(
        approved=True, score=score, feedback="Хорошо.", narrative_score=9.0
    )


@pytest.fixture
def knowledge() -> dict[str, Any]:
    return load_project_knowledge(PILOT_PROJECT_ID)


@pytest.fixture
def no_shortening(monkeypatch: pytest.MonkeyPatch) -> FakeModel:
    fake = FakeModel(lambda prompt: pytest.fail("unexpected shortening call"))
    monkeypatch.setattr(copywriter, "shorten_llm", fake)

    return fake


def pilot_state(knowledge: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "project_id": PILOT_PROJECT_ID,
        "knowledge": knowledge,
        "revision_count": 0,
        "max_revisions": 3,
        "errors": [],
        "publish_live": False,
        **extra,
    }


# --- D1: the pilot is always a standalone series ------------------------


def test_the_pilot_strategy_is_standalone_whatever_was_asked() -> None:
    for asked in ("reply_thread", "standalone_series", "", None, "nonsense"):
        assert (
            resolve_publishing_strategy(PILOT_PROJECT_ID, asked) == "standalone_series"
        )


def test_another_project_keeps_its_strategy() -> None:
    assert resolve_publishing_strategy("other", "reply_thread") == "reply_thread"
    assert resolve_publishing_strategy("other", "nonsense") == "standalone_series"


def test_the_strategist_does_not_offer_reply_thread_for_the_pilot(
    monkeypatch: pytest.MonkeyPatch, knowledge: dict[str, Any]
) -> None:
    fake = FakeModel(plan("reply_thread"))
    monkeypatch.setattr(strategist, "structured_llm", fake)

    result = strategist.strategist_node(pilot_state(knowledge))

    assert result["content_plan"]["publishing_strategy"] == "standalone_series"
    assert "скриншотами" in result["content_plan"]["publishing_strategy_reason"]
    assert "Выбери стратегию публикации сам" not in fake.prompts[0]
    assert "зафиксирована: standalone_series" in fake.prompts[0]


def test_the_strategist_lets_another_project_choose(
    monkeypatch: pytest.MonkeyPatch, knowledge: dict[str, Any]
) -> None:
    fake = FakeModel(plan("reply_thread"))
    monkeypatch.setattr(strategist, "structured_llm", fake)

    result = strategist.strategist_node(
        {**pilot_state(knowledge), "project_id": "other"}
    )

    assert result["content_plan"]["publishing_strategy"] == "reply_thread"
    assert "Выбери стратегию публикации сам" in fake.prompts[0]


def test_the_strategist_uses_a_bounded_topic_hint_without_replacing_knowledge(
    monkeypatch: pytest.MonkeyPatch, knowledge: dict[str, Any]
) -> None:
    fake = FakeModel(plan("standalone_series"))
    monkeypatch.setattr(strategist, "structured_llm", fake)
    hint = "Сфокусируйся на восстановлении после аварии"

    strategist.strategist_node(
        {
            **pilot_state(knowledge),
            "project_id": "other",
            "topic_hint": hint,
        }
    )

    prompt = fake.prompts[0]
    assert f"<topic_hint>{hint}</topic_hint>" in prompt
    assert "не источник фактов" in prompt
    assert str(knowledge) in prompt


def test_the_strategist_rejects_an_oversized_topic_hint(
    knowledge: dict[str, Any]
) -> None:
    with pytest.raises(ValueError, match="at most 500"):
        strategist.strategist_node(
            {
                **pilot_state(knowledge),
                "project_id": "other",
                "topic_hint": "x" * 501,
            }
        )


def test_the_copywriter_writes_a_standalone_pilot_from_a_reply_thread_plan(
    monkeypatch: pytest.MonkeyPatch, knowledge: dict[str, Any], no_shortening
) -> None:
    fake = FakeModel(package())
    monkeypatch.setattr(copywriter, "structured_llm", fake)

    result = copywriter.copywriter_node(
        pilot_state(knowledge, content_plan=plan("reply_thread").model_dump())
    )

    assert result["draft"]["publishing_strategy"] == "standalone_series"
    assert "СТРАТЕГИЯ: standalone_series" in fake.prompts[0]


def test_a_carousel_pilot_is_approvable_even_if_the_plan_said_reply_thread(
    monkeypatch: pytest.MonkeyPatch, knowledge: dict[str, Any]
) -> None:
    """Before the fix: «Reply 2 is formatted as carousel» on every version."""

    monkeypatch.setattr(editor, "structured_llm", FakeModel(approve()))
    draft = package().model_dump()
    draft["publishing_strategy"] = "reply_thread"

    result = editor.editor_node(
        pilot_state(
            knowledge, draft=draft, content_plan=plan("reply_thread").model_dump()
        )
    )

    assert not [i for i in result["editor_issues"] if i.startswith("Reply")]
    assert result["editor_approved"] is True, result["editor_issues"]


# --- D2: the series label is not an opening ----------------------------


def test_split_series_label() -> None:
    assert split_series_label(labelled(2, "Текст части."), LONG_SERIES_TITLE) == (
        f"{LONG_SERIES_TITLE} — часть 2 из 3",
        "Текст части.",
    )
    assert split_series_label("Часть 3 из 3. Текст.") == ("Часть 3 из 3.", "Текст.")
    assert split_series_label("(2/3) Текст") == ("(2/3)", "Текст")
    # The project's name at the start of a sentence is not a label.
    assert split_series_label(
        "AI Catalog Consultant — это консультант. Часть 1 из 3.",
        "AI Catalog Consultant",
    ) == ("", "AI Catalog Consultant — это консультант. Часть 1 из 3.")
    assert split_series_label("Просто текст.") == ("", "Просто текст.")


def test_a_long_series_label_is_not_a_repeated_opening() -> None:
    """Before the fix: all three parts «open with the same sentence»."""

    issues = check_series_continuity(
        package().model_dump(), series_title=LONG_SERIES_TITLE
    )

    assert not [i for i in issues if "open with the same sentence" in i]
    assert issues == []


def test_a_real_repeated_opening_after_the_label_is_still_caught() -> None:
    same = (
        "AI Catalog Consultant — AI-консультант по каталогу телевизоров, построенный "
    )
    bodies = (same + "на SQL.", same + "на векторах.", GOOD_BODIES[2])

    issues = check_series_continuity(
        package(bodies).model_dump(), series_title=LONG_SERIES_TITLE
    )

    assert any(
        i.startswith("Parts 1 and 2 open with the same sentence") for i in issues
    )


# --- D3: an over-long item is shortened, not the whole package ---------


def test_a_text_within_the_limit_is_not_touched(no_shortening) -> None:
    text = labelled(1, "Коротко.")

    assert shorten_text(text, series_title=LONG_SERIES_TITLE) == text
    assert no_shortening.prompts == []


def test_shortening_keeps_the_label_and_reaches_the_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = "Длинный текст о каталоге. " * 30
    fake = FakeModel("Короткий текст о каталоге.")
    monkeypatch.setattr(
        copywriter,
        "shorten_llm",
        SimpleNamespace(
            invoke=lambda prompt, config=None: SimpleNamespace(
                content=fake.invoke(prompt)
            )
        ),
    )

    result = shorten_text(labelled(2, body), series_title=LONG_SERIES_TITLE)

    assert result == labelled(2, "Короткий текст о каталоге.")
    assert len(result) <= copywriter.MAX_ITEM_LENGTH
    # The model is given the body alone, with its exact length.
    assert LONG_SERIES_TITLE not in fake.prompts[0]
    assert f"Сейчас в тексте {len(body.strip())} символов" in fake.prompts[0]


def test_shortening_never_accepts_a_longer_text_and_gives_up_after_two_tries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = "x" * 600
    calls: list[str] = []
    monkeypatch.setattr(
        copywriter,
        "shorten_llm",
        SimpleNamespace(
            invoke=lambda prompt, config=None: (
                calls.append(prompt) or SimpleNamespace(content="y" * 700)
            )
        ),
    )

    assert shorten_text(text) == text
    assert len(calls) == copywriter.MAX_SHORTEN_ATTEMPTS


def test_the_copywriter_hands_the_editor_items_within_the_limit(
    monkeypatch: pytest.MonkeyPatch, knowledge: dict[str, Any]
) -> None:
    long_bodies = (GOOD_BODIES[0] + " Подробности." * 40, *GOOD_BODIES[1:])
    monkeypatch.setattr(copywriter, "structured_llm", FakeModel(package(long_bodies)))
    monkeypatch.setattr(
        copywriter,
        "shorten_llm",
        SimpleNamespace(
            invoke=lambda prompt, config=None: SimpleNamespace(content=GOOD_BODIES[0])
        ),
    )

    result = copywriter.copywriter_node(
        pilot_state(knowledge, content_plan=plan("standalone_series").model_dump())
    )
    texts = [p["items"][0]["text"] for p in result["draft"]["publications"]]

    assert all(len(t) <= copywriter.MAX_ITEM_LENGTH for t in texts)
    assert texts[0] == labelled(1, GOOD_BODIES[0])


def test_an_over_long_item_is_reported_once(
    monkeypatch: pytest.MonkeyPatch, knowledge: dict[str, Any]
) -> None:
    monkeypatch.setattr(editor, "structured_llm", FakeModel(approve()))
    draft = package().model_dump()
    draft["publications"][0]["items"][0]["text"] += " Подробности." * 40

    result = editor.editor_node(pilot_state(knowledge, draft=draft))
    length_issues = [i for i in result["editor_issues"] if "characters" in i]

    assert len(length_issues) == 1, length_issues
    assert result["editor_approved"] is False


# --- D4: a wrong number of publications is an issue, not a crash -------


def test_the_copywriter_returns_a_short_package_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch, knowledge: dict[str, Any], no_shortening
) -> None:
    short = package()
    short.publications = short.publications[:2]
    monkeypatch.setattr(copywriter, "structured_llm", FakeModel(short))

    result = copywriter.copywriter_node(
        pilot_state(knowledge, content_plan=plan().model_dump(), force_bad_draft=True)
    )

    assert len(result["draft"]["publications"]) == 2


def test_the_graph_survives_a_wrong_package_and_blocks_it(
    monkeypatch: pytest.MonkeyPatch, knowledge: dict[str, Any], no_shortening
) -> None:
    short = package()
    short.publications = short.publications[:2]
    monkeypatch.setattr(strategist, "structured_llm", FakeModel(plan()))
    monkeypatch.setattr(copywriter, "structured_llm", FakeModel(short))
    monkeypatch.setattr(editor, "structured_llm", FakeModel(approve()))

    result = build_graph().invoke(pilot_state(knowledge))

    assert result["revision_count"] == 3
    assert result["editor_approved"] is False
    assert any(
        "Pilot requires exactly 3 publications" in i for i in result["editor_issues"]
    )
    assert result["publication"]["status"] == "blocked"


def test_the_graph_recovers_when_a_revision_fixes_the_package(
    monkeypatch: pytest.MonkeyPatch, knowledge: dict[str, Any], no_shortening
) -> None:
    short = package()
    short.publications = short.publications[:2]
    monkeypatch.setattr(strategist, "structured_llm", FakeModel(plan()))
    monkeypatch.setattr(copywriter, "structured_llm", FakeModel(short, package()))
    monkeypatch.setattr(editor, "structured_llm", FakeModel(approve()))

    result = build_graph().invoke(pilot_state(knowledge))

    assert result["revision_count"] == 1
    assert result["editor_approved"] is True, result["editor_issues"]
    assert result["publication"]["status"] == "dry_run"
    assert [len(p["images"]) for p in result["publication"]["publications"]] == [
        2,
        1,
        1,
    ]


# --- SMM-035A: series_size is the Generation contract for every project --
#
# The first real generation (job eb41298e, series_size=1) produced three
# drafts: the pilot hard-coded "exactly 3" in the Copywriter prompt and in
# the Editor's deterministic check. A Generation Job states how many
# publications it wants; the agents are told that number. Without it (the
# publishing graph) the pilot still means three.


def one_publication_package() -> DraftPackage:
    single = package()
    single.publications = single.publications[:1]

    return single


@pytest.mark.parametrize(
    ("project_id", "series_size", "expected"),
    [
        (PILOT_PROJECT_ID, None, 3),  # the publishing graph: unchanged
        (PILOT_PROJECT_ID, 1, 1),
        (PILOT_PROJECT_ID, 3, 3),
        (PILOT_PROJECT_ID, 5, 5),
        ("other", 1, 1),
        ("other", 4, 4),
        ("other", None, None),  # no requirement: nothing to enforce
        (PILOT_PROJECT_ID, 0, 3),  # not a usable size: as if absent
        (PILOT_PROJECT_ID, True, 3),  # a bool is not a size
        ("other", "3", None),
        ("other", -1, None),
    ],
)
def test_the_expected_number_of_publications(
    project_id: str, series_size: Any, expected: int | None
) -> None:
    assert expected_publication_count(project_id, series_size) == expected


def test_the_pilot_copywriter_is_asked_for_one_publication_when_one_is_requested(
    monkeypatch: pytest.MonkeyPatch, knowledge: dict[str, Any], no_shortening
) -> None:
    fake = FakeModel(one_publication_package())
    monkeypatch.setattr(copywriter, "structured_llm", fake)

    result = copywriter.copywriter_node(
        pilot_state(
            knowledge,
            series_size=1,
            content_plan=plan("standalone_series").model_dump(),
        )
    )

    assert "Подготовь ровно 1 публикацию" in fake.prompts[0]
    assert "ровно 3" not in fake.prompts[0]
    assert len(result["draft"]["publications"]) == 1


def test_the_pilot_copywriter_is_asked_for_three_when_three_are_requested(
    monkeypatch: pytest.MonkeyPatch, knowledge: dict[str, Any], no_shortening
) -> None:
    fake = FakeModel(package())
    monkeypatch.setattr(copywriter, "structured_llm", fake)

    copywriter.copywriter_node(
        pilot_state(
            knowledge,
            series_size=3,
            content_plan=plan("standalone_series").model_dump(),
        )
    )

    assert "Подготовь ровно 3 публикации" in fake.prompts[0]


def test_the_pilot_copywriter_keeps_three_without_a_series_size(
    monkeypatch: pytest.MonkeyPatch, knowledge: dict[str, Any], no_shortening
) -> None:
    fake = FakeModel(package())
    monkeypatch.setattr(copywriter, "structured_llm", fake)

    copywriter.copywriter_node(
        pilot_state(knowledge, content_plan=plan("standalone_series").model_dump())
    )

    assert "Подготовь ровно 3 публикации" in fake.prompts[0]


def test_another_project_is_told_the_requested_number_too(
    monkeypatch: pytest.MonkeyPatch, knowledge: dict[str, Any], no_shortening
) -> None:
    fake = FakeModel(one_publication_package())
    monkeypatch.setattr(copywriter, "structured_llm", fake)

    copywriter.copywriter_node(
        {
            **pilot_state(knowledge, series_size=1),
            "project_id": "other",
            "content_plan": plan("standalone_series").model_dump(),
        }
    )

    assert "Подготовь ровно 1 публикацию" in fake.prompts[0]


def test_a_wrong_count_for_the_requested_size_is_logged_not_raised(
    monkeypatch: pytest.MonkeyPatch,
    knowledge: dict[str, Any],
    no_shortening,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(copywriter, "structured_llm", FakeModel(package()))

    with caplog.at_level("WARNING"):
        result = copywriter.copywriter_node(
            pilot_state(
                knowledge,
                series_size=1,
                content_plan=plan("standalone_series").model_dump(),
            )
        )

    assert len(result["draft"]["publications"]) == 3
    assert any("wrong number of publications" in r.getMessage() for r in caplog.records)


def test_a_single_standalone_part_is_not_asked_for_an_announcement(
    monkeypatch: pytest.MonkeyPatch, knowledge: dict[str, Any], no_shortening
) -> None:
    """The text budget used to demand «короткий анонс» of every part."""

    fake = FakeModel(one_publication_package())
    monkeypatch.setattr(copywriter, "structured_llm", fake)

    copywriter.copywriter_node(
        pilot_state(
            knowledge,
            series_size=1,
            content_plan=plan("standalone_series").model_dump(),
        )
    )

    prompt = " ".join(fake.prompts[0].split())
    assert "и короткий анонс," not in prompt
    assert "анонса не будет вообще" in prompt  # the last-part rule stays


def test_a_multi_part_series_still_budgets_an_announcement_except_for_the_last(
    monkeypatch: pytest.MonkeyPatch, knowledge: dict[str, Any], no_shortening
) -> None:
    fake = FakeModel(package())
    monkeypatch.setattr(copywriter, "structured_llm", fake)

    copywriter.copywriter_node(
        pilot_state(
            knowledge,
            series_size=3,
            content_plan=plan("standalone_series").model_dump(),
        )
    )

    assert "кроме последней части, короткий анонс" in " ".join(fake.prompts[0].split())


def test_the_editor_accepts_one_publication_when_one_is_requested(
    monkeypatch: pytest.MonkeyPatch, knowledge: dict[str, Any]
) -> None:
    monkeypatch.setattr(editor, "structured_llm", FakeModel(approve()))

    result = editor.editor_node(
        pilot_state(
            knowledge,
            series_size=1,
            draft=one_publication_package().model_dump(),
            content_plan=plan("standalone_series").model_dump(),
        )
    )

    assert not [i for i in result["editor_issues"] if "exactly" in i]


def test_the_editor_names_a_count_that_differs_from_the_request() -> None:
    draft = package().model_dump()

    issues = editor.validate_draft(draft, PILOT_PROJECT_ID, expected_count=1)

    assert any("exactly 1 publication" in i and "received 3" in i for i in issues)


def test_the_editor_keeps_the_legacy_pilot_message_without_a_request() -> None:
    short = package().model_dump()
    short["publications"] = short["publications"][:2]

    issues = editor.validate_draft(short, PILOT_PROJECT_ID)

    assert any("Pilot requires exactly 3 publications" in i for i in issues)


def test_the_editor_checks_the_requested_count_for_another_project() -> None:
    draft = package().model_dump()

    assert any(
        "exactly 2 publications" in i
        for i in editor.validate_draft(draft, "other", expected_count=2)
    )
    assert not [i for i in editor.validate_draft(draft, "other") if "exactly" in i]
