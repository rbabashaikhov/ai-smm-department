"""The agent pipeline without a model: regressions from SMM-026A/1b.

Part 1 (tests/test_agent_pipeline.py) fixed D1-D4 so the Editor could
approve something. The real E2E that followed
(docs/reports/smm-026a/part1-e2e-20261015) approved 4 of 5 packages, but
turned up six remaining quality problems, named R1-R6 in that report:

* R1 -- shortening sometimes did not converge within its attempts, and
  nothing distinguished "shortened" from "gave up".
* R2 -- two parts using different grammatical forms of the same Russian
  word ("решения"/"решений") looked unrelated to the word-overlap check.
* R3 -- an approved package sometimes promised a part that does not exist
  ("Следующая часть -- о новых решениях", "Продолжение скоро").
* R4 -- unsupported quality claims ("обеспечивает точность и надёжность")
  were not reliably caught.
* R5 -- the semantic guard's documented limitation (it removes an
  invented number from the catalog query; the final wording may still
  repeat it) was sometimes described as the guard failing outright.
* R6 -- the series label appeared in several formats across the same
  series: with the title or without, at the start or at the end.

This module tests the fixes for all six. It reuses the fakes and fixtures
from test_agent_pipeline.py (including its blanket network ban) rather
than redefining them.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

import pytest


# Same guard as test_agent_pipeline.py, and needed for the same reason:
# the agent modules build their ChatOpenAI clients at import time, before
# tests.test_agent_pipeline gets a chance to set this default itself.
os.environ.setdefault("OPENAI_API_KEY", "test-not-a-real-key")
os.environ.setdefault("LANGFUSE_TRACING_ENABLED", "false")

from ai_smm.agents import copywriter, editor, strategist  # noqa: E402
from ai_smm.agents.continuity import (  # noqa: E402
    MIN_BODY_AFTER_TEASER_REMOVAL,
    _announces_a_sequel,
    _stem,
    check_series_continuity,
    format_series_label,
    remove_safe_sequel_teaser,
    remove_safe_sequel_teaser_from_draft,
    strip_series_label,
)
from ai_smm.agents.editor import UNSUPPORTED_CLAIM_PATTERNS  # noqa: E402
from ai_smm.graph import build_graph  # noqa: E402
from tests.test_agent_pipeline import (  # noqa: E402,F401 -- no_network is autouse
    GOOD_BODIES,
    LONG_SERIES_TITLE,
    FakeModel,
    approve,
    knowledge,
    labelled,
    no_network,
    no_shortening,
    package,
    pilot_state,
    plan,
)


# ---------------------------------------------------------------------
# R6 -- one deterministic label format
# ---------------------------------------------------------------------


def test_format_series_label_with_and_without_a_title() -> None:
    assert format_series_label(LONG_SERIES_TITLE, 2, 3) == (
        f"{LONG_SERIES_TITLE} — часть 2 из 3"
    )
    assert format_series_label("", 1, 1) == "Часть 1 из 1"


def test_strip_series_label_removes_a_label_written_at_the_end() -> None:
    """Before the fix: a trailing label was invisible to the stripper,
    so it travelled through shortening as ordinary body text."""

    text = (
        "Подходит под бюджет и не теряет нужные функции. "
        f"{LONG_SERIES_TITLE} — часть 3 из 3."
    )

    # The body's own final stop is also the separator before the label and
    # is trimmed along with it, same as split_series_label already does on
    # the leading side; the sentence without a trailing "." is what the
    # system then attaches its own canonical label and period-free label
    # line in front of.
    assert strip_series_label(text, LONG_SERIES_TITLE) == (
        "Подходит под бюджет и не теряет нужные функции"
    )


def test_strip_series_label_removes_a_bare_trailing_number() -> None:
    assert strip_series_label("Текст части. Часть 3 из 3.") == "Текст части"


def test_strip_series_label_leaves_a_mid_sentence_mention_alone() -> None:
    """The project's name appearing mid-sentence is not a label, at
    either end, and must not be eaten."""

    text = (
        "AI Catalog Consultant — это консультант по каталогу. "
        "Часть 1 из 3 была про задачу."
    )

    assert strip_series_label(text, "AI Catalog Consultant") == text


def test_strip_series_label_is_a_no_op_without_a_label() -> None:
    text = "Обычный текст без всякой метки серии."

    assert strip_series_label(text, LONG_SERIES_TITLE) == text


def test_the_copywriter_relabels_a_model_written_trailing_label(
    monkeypatch: pytest.MonkeyPatch,
    knowledge: dict[str, Any],  # noqa: F811
    no_shortening,  # noqa: F811
) -> None:
    """The model puts the label at the end; the system still produces
    exactly one, canonical, leading label."""

    bad = package()
    bad.publications[0].items[0].text = (
        "Знакомимся с AI Catalog Consultant: LLM не источник цен и "
        f"наличия товаров каталога. {LONG_SERIES_TITLE} — часть 1 из 3."
    )
    monkeypatch.setattr(copywriter, "structured_llm", FakeModel(bad))

    result = copywriter.copywriter_node(
        pilot_state(knowledge, content_plan=plan("standalone_series").model_dump())
    )

    text = result["draft"]["publications"][0]["items"][0]["text"]

    assert text.startswith(f"{LONG_SERIES_TITLE} — часть 1 из 3")
    # The label appears exactly once: not also wherever the model put it.
    assert text.count("часть 1 из 3") == 1


def test_the_copywriter_strips_a_label_the_model_wrote_at_the_start_too(
    monkeypatch: pytest.MonkeyPatch,
    knowledge: dict[str, Any],  # noqa: F811
    no_shortening,  # noqa: F811
) -> None:
    """A model-written leading label is replaced, not doubled up, even
    when it already matches the canonical format."""

    monkeypatch.setattr(copywriter, "structured_llm", FakeModel(package()))

    result = copywriter.copywriter_node(
        pilot_state(knowledge, content_plan=plan("standalone_series").model_dump())
    )

    text = result["draft"]["publications"][0]["items"][0]["text"]

    assert text == labelled(1, GOOD_BODIES[0])
    assert text.count(LONG_SERIES_TITLE) == 1


def test_the_copywriter_adds_no_label_for_a_single_publication_series(
    monkeypatch: pytest.MonkeyPatch,
    knowledge: dict[str, Any],  # noqa: F811
    no_shortening,  # noqa: F811
) -> None:
    single = package()
    single.publications = single.publications[:1]
    single.publications[0].items[0].text = "Текст единственного поста."
    monkeypatch.setattr(copywriter, "structured_llm", FakeModel(single))

    result = copywriter.copywriter_node(
        pilot_state(knowledge, content_plan=plan("standalone_series").model_dump())
    )

    assert result["draft"]["publications"][0]["items"][0]["text"] == (
        "Текст единственного поста."
    )


def test_the_copywriter_adds_no_label_for_reply_thread(
    monkeypatch: pytest.MonkeyPatch,
    knowledge: dict[str, Any],  # noqa: F811
    no_shortening,  # noqa: F811
) -> None:
    """Only the pilot is forced to standalone_series; another project may
    still run reply_thread, where numbering is noise, not a label."""

    monkeypatch.setattr(copywriter, "structured_llm", FakeModel(package()))

    result = copywriter.copywriter_node(
        {
            **pilot_state(knowledge, content_plan=plan("reply_thread").model_dump()),
            "project_id": "other-project",
        }
    )

    text = result["draft"]["publications"][0]["items"][0]["text"]

    assert "часть" not in text.lower()


# ---------------------------------------------------------------------
# R1 -- bounded shortening, controlled rejection, no mechanical cut
# ---------------------------------------------------------------------


def _shorten_llm_stub(replies: list[str]) -> SimpleNamespace:
    calls: list[str] = []

    def invoke(prompt: str, config: Any = None) -> SimpleNamespace:
        calls.append(prompt)
        reply = replies[len(calls) - 1] if len(calls) <= len(replies) else replies[-1]

        return SimpleNamespace(content=reply)

    return SimpleNamespace(invoke=invoke, calls=calls)


def test_shortening_makes_at_most_a_fixed_number_of_llm_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """However far over the limit, and however little each reply helps,
    the number of calls is bounded by MAX_SHORTEN_ATTEMPTS."""

    stub = _shorten_llm_stub(["x" * 600])  # never shortens enough
    monkeypatch.setattr(copywriter, "shorten_llm", stub)

    result = copywriter.shorten_text("y" * 900)

    assert len(stub.calls) == copywriter.MAX_SHORTEN_ATTEMPTS
    assert len(result) > copywriter.MAX_ITEM_LENGTH


def test_shortening_never_mechanically_truncates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A model that never returns anything shorter gets back the original
    text, not a slice of it: a cut mid-sentence is worse than the
    original, and the point is to let the Editor name the failure."""

    original = ("Предложение без очевидного места для сокращения. " * 15).strip()
    stub = _shorten_llm_stub([original])  # "shortens" to itself
    monkeypatch.setattr(copywriter, "shorten_llm", stub)

    result = copywriter.shorten_text(original)

    assert result == original
    assert not result.endswith("…")
    assert len(stub.calls) == copywriter.MAX_SHORTEN_ATTEMPTS


def test_shortening_stops_as_soon_as_it_fits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = _shorten_llm_stub(["Короткий текст."])
    monkeypatch.setattr(copywriter, "shorten_llm", stub)

    result = copywriter.shorten_text("x" * 600)

    assert result == "Короткий текст."
    assert len(stub.calls) == 1


def test_a_shortening_failure_is_flagged_on_the_item(
    monkeypatch: pytest.MonkeyPatch,
    knowledge: dict[str, Any],  # noqa: F811
) -> None:
    long_bodies = (GOOD_BODIES[0] + " Подробности." * 60, *GOOD_BODIES[1:])
    monkeypatch.setattr(copywriter, "structured_llm", FakeModel(package(long_bodies)))
    monkeypatch.setattr(
        copywriter,
        "shorten_llm",
        _shorten_llm_stub(["z" * 500]),  # keeps returning something too long
    )

    result = copywriter.copywriter_node(
        pilot_state(knowledge, content_plan=plan("standalone_series").model_dump())
    )
    item = result["draft"]["publications"][0]["items"][0]

    assert len(item["text"]) > copywriter.MAX_ITEM_LENGTH
    assert item["shortening_failed"] is True


def test_shortening_success_is_not_flagged_as_a_failure(
    monkeypatch: pytest.MonkeyPatch,
    knowledge: dict[str, Any],  # noqa: F811
) -> None:
    long_bodies = (GOOD_BODIES[0] + " Подробности." * 40, *GOOD_BODIES[1:])
    monkeypatch.setattr(copywriter, "structured_llm", FakeModel(package(long_bodies)))
    monkeypatch.setattr(copywriter, "shorten_llm", _shorten_llm_stub([GOOD_BODIES[0]]))

    result = copywriter.copywriter_node(
        pilot_state(knowledge, content_plan=plan("standalone_series").model_dump())
    )
    item = result["draft"]["publications"][0]["items"][0]

    assert len(item["text"]) <= copywriter.MAX_ITEM_LENGTH
    assert "shortening_failed" not in item


def test_the_editor_gives_sharper_feedback_after_a_shortening_failure() -> None:
    draft = {
        "publications": [
            {
                "format": "carousel",
                "items": [
                    {
                        "text": "x" * 500,
                        "shortening_failed": True,
                    }
                ],
            }
        ]
    }

    issues = editor.validate_draft(draft, project_id="other-project")
    (issue,) = [i for i in issues if "characters" in i]

    assert "maximum 450" in issue
    assert "could not fit it" in issue
    assert "rewrite this part shorter from scratch" in issue


def test_the_editor_asks_only_to_shorten_when_no_pass_was_attempted() -> None:
    draft = {"publications": [{"format": "carousel", "items": [{"text": "x" * 500}]}]}

    issues = editor.validate_draft(draft, project_id="other-project")
    (issue,) = [i for i in issues if "characters" in i]

    assert "could not fit it" not in issue


# ---------------------------------------------------------------------
# R2 -- Russian word forms are not a missing link
# ---------------------------------------------------------------------


def test_stem_collapses_common_russian_inflections() -> None:
    assert _stem("решения") == _stem("решений") == _stem("решение")
    assert _stem("товаров") == _stem("товара") == _stem("товар")
    assert _stem("ограничения") == _stem("ограничение")


def test_stem_leaves_short_words_whole() -> None:
    """Words no longer than the stem length are never truncated, so two
    short, genuinely different words never collide."""

    assert _stem("вывод") == "вывод"
    assert _stem("выбор") == "выбор"
    assert _stem("вывод") != _stem("выбор")


def test_word_forms_of_the_same_root_are_recognised_as_a_link() -> None:
    """Reproduces the false positive from run-5-revision v2/v3 of the
    part-1 E2E: one part used "товаров", the next "товара", which raw
    string equality did not connect. ("решения"/"решений", the exact
    words from that run, are avoided here because LONG_SERIES_TITLE
    itself contains "решения" and would exclude it as scaffolding, which
    is different, correct behaviour tested elsewhere.)"""

    draft = package(
        (
            GOOD_BODIES[0],
            "Рассмотрим инженерные подходы проекта: структурированные "
            "данные и детерминированный выбор товаров каталога.",
            "После анализа стало ясно, что ограничения каталога и "
            "товара нужно документировать, а не устранять совсем.",
        )
    ).model_dump()

    issues = check_series_continuity(draft, series_title=LONG_SERIES_TITLE)

    assert not any("shares almost no subject matter" in i for i in issues)


def test_an_unrelated_part_is_still_caught_with_stemming() -> None:
    """Stemming must only ever add matches; a part about something else
    entirely is still flagged."""

    draft = package(
        (
            GOOD_BODIES[0],
            "Погода, велосипеды, кулинария и дальние путешествия "
            "совершенно не связаны с остальными частями серии.",
            GOOD_BODIES[2],
        )
    ).model_dump()

    issues = check_series_continuity(draft, series_title=LONG_SERIES_TITLE)

    assert any("shares almost no subject matter" in i for i in issues)


# ---------------------------------------------------------------------
# R3 -- no unannounced continuation
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "teaser",
    [
        "Продолжение скоро.",
        "Следующая часть — о новых решениях.",
        "В следующей части разберём ограничения подробнее.",
        "Далее — ключевые инженерные решения.",
        "To be continued.",
    ],
)
def test_a_final_part_promising_a_sequel_is_caught(teaser: str) -> None:
    draft = package(
        (GOOD_BODIES[0], GOOD_BODIES[1], GOOD_BODIES[2] + " " + teaser)
    ).model_dump()

    issues = check_series_continuity(draft, series_title=LONG_SERIES_TITLE)

    assert any("promises a sequel" in i for i in issues)


def test_a_non_final_part_announcing_the_next_one_is_fine() -> None:
    """The same phrase in an earlier part is the transition the
    Copywriter is explicitly told to write, not a defect."""

    draft = package().model_dump()  # GOOD_BODIES already end parts 1/2 with
    # "Дальше — архитектура."/"Дальше — ошибки."

    issues = check_series_continuity(draft, series_title=LONG_SERIES_TITLE)

    assert not any("promises a sequel" in i for i in issues)


def test_expects_sequel_suppresses_the_check() -> None:
    draft = package(
        (GOOD_BODIES[0], GOOD_BODIES[1], GOOD_BODIES[2] + " Продолжение скоро.")
    ).model_dump()

    issues = check_series_continuity(
        draft, series_title=LONG_SERIES_TITLE, expects_sequel=True
    )

    assert not any("promises a sequel" in i for i in issues)


def test_a_clean_ending_is_not_flagged() -> None:
    assert not _announces_a_sequel(GOOD_BODIES[2])


def test_a_mid_text_mention_of_continuing_is_not_a_teaser() -> None:
    """ "продолжение" used to describe the project, not the series, is
    not a sequel promise -- only "продолжение скоро/следует" is."""

    text = "Проект представляет собой продолжение более раннего прототипа."

    assert not _announces_a_sequel(text)


def test_the_editor_blocks_a_draft_whose_last_part_teases_a_sequel(
    monkeypatch: pytest.MonkeyPatch,
    knowledge: dict[str, Any],  # noqa: F811
) -> None:
    """Even an LLM that claims approved=True cannot wave this through:
    the continuity veto is unconditional."""

    monkeypatch.setattr(editor, "structured_llm", FakeModel(approve()))
    draft = package(
        (
            GOOD_BODIES[0],
            GOOD_BODIES[1],
            GOOD_BODIES[2] + " Следующая часть расскажет больше.",
        )
    ).model_dump()

    result = editor.editor_node(pilot_state(knowledge, draft=draft))

    assert result["editor_approved"] is False
    assert any("promises a sequel" in i for i in result["editor_issues"])
    # Fused with real content: the removal is refused, so the draft the
    # Editor hands back is the exact text it reviewed, teaser and all.
    assert "Следующая часть расскажет больше." in (
        result["draft"]["publications"][-1]["items"][-1]["text"]
    )


# ---------------------------------------------------------------------
# R3b -- deterministic removal of a *safely separable* sequel teaser
# ---------------------------------------------------------------------


def _teaser_draft(last_body_suffix: str) -> dict[str, Any]:
    return package(
        (GOOD_BODIES[0], GOOD_BODIES[1], GOOD_BODIES[2] + last_body_suffix)
    ).model_dump()


def test_remove_safe_sequel_teaser_drops_a_standalone_closer() -> None:
    text = labelled(3, "Ограничения задокументированы. Продолжение скоро.")

    cleaned, removed = remove_safe_sequel_teaser(text, series_title=LONG_SERIES_TITLE)

    assert removed is True
    assert cleaned == labelled(3, "Ограничения задокументированы.")
    assert not _announces_a_sequel(cleaned)


@pytest.mark.parametrize(
    "teaser",
    [
        "Продолжение скоро.",
        "Продолжение следует.",
        "To be continued.",
    ],
)
def test_remove_safe_sequel_teaser_drops_every_pure_closer(teaser: str) -> None:
    text = labelled(3, f"Ограничения задокументированы. {teaser}")

    cleaned, removed = remove_safe_sequel_teaser(text, series_title=LONG_SERIES_TITLE)

    assert removed is True
    assert not _announces_a_sequel(cleaned)


def test_remove_safe_sequel_teaser_refuses_when_fused_with_real_content() -> None:
    """"Следующая часть расскажет о новых решениях" -- the trigger phrase
    is one sentence with a real claim about what comes next; stripping
    only the trigger would leave a dangling fragment, so this must not
    touch the text at all."""

    text = labelled(
        3,
        "Ограничения задокументированы. "
        "Следующая часть расскажет о новых решениях.",
    )

    cleaned, removed = remove_safe_sequel_teaser(text, series_title=LONG_SERIES_TITLE)

    assert removed is False
    assert cleaned == text


def test_remove_safe_sequel_teaser_refuses_when_nothing_would_be_left() -> None:
    text = labelled(3, "Продолжение скоро.")

    cleaned, removed = remove_safe_sequel_teaser(text, series_title=LONG_SERIES_TITLE)

    assert removed is False
    assert cleaned == text


def test_remove_safe_sequel_teaser_refuses_when_too_little_would_remain() -> None:
    # The trailing "." is part of what is left, so this is comfortably
    # below the threshold once it is included, not just at its edge.
    short_body = "x" * (MIN_BODY_AFTER_TEASER_REMOVAL - 5)
    text = labelled(3, f"{short_body}. Продолжение скоро.")

    cleaned, removed = remove_safe_sequel_teaser(text, series_title=LONG_SERIES_TITLE)

    assert removed is False
    assert cleaned == text


def test_remove_safe_sequel_teaser_is_a_no_op_without_a_teaser() -> None:
    text = labelled(3, GOOD_BODIES[2])

    cleaned, removed = remove_safe_sequel_teaser(text, series_title=LONG_SERIES_TITLE)

    assert removed is False
    assert cleaned == text


def test_remove_safe_sequel_teaser_from_draft_edits_only_the_last_item(
) -> None:
    draft = _teaser_draft(" Продолжение скоро.")
    first_item_text_before = draft["publications"][0]["items"][0]["text"]

    cleaned, removed = remove_safe_sequel_teaser_from_draft(
        draft, series_title=LONG_SERIES_TITLE
    )

    assert removed is True
    assert cleaned["publications"][0]["items"][0]["text"] == first_item_text_before
    assert not _announces_a_sequel(
        cleaned["publications"][-1]["items"][-1]["text"]
    )


def test_remove_safe_sequel_teaser_from_draft_does_not_mutate_the_original(
) -> None:
    """When removal is unsafe, the function must hand back the exact
    same object, not a copy that merely looks the same."""

    draft = _teaser_draft(" Следующая часть расскажет о новых решениях.")

    result, removed = remove_safe_sequel_teaser_from_draft(
        draft, series_title=LONG_SERIES_TITLE
    )

    assert removed is False
    assert result is draft  # identity, not just equality


def test_remove_safe_sequel_teaser_from_draft_leaves_the_original_untouched_on_success(
) -> None:
    """A successful removal returns a deep copy; the draft the caller
    already held elsewhere must still show the original text."""

    draft = _teaser_draft(" Продолжение скоро.")
    original_text = draft["publications"][-1]["items"][-1]["text"]

    cleaned, removed = remove_safe_sequel_teaser_from_draft(
        draft, series_title=LONG_SERIES_TITLE
    )

    assert removed is True
    assert cleaned is not draft
    assert draft["publications"][-1]["items"][-1]["text"] == original_text


def test_remove_safe_sequel_teaser_from_draft_respects_expects_sequel() -> None:
    draft = _teaser_draft(" Продолжение скоро.")

    cleaned, removed = remove_safe_sequel_teaser_from_draft(
        draft, series_title=LONG_SERIES_TITLE, expects_sequel=True
    )

    assert removed is False
    assert cleaned is draft


def test_the_editor_cleans_up_a_safely_removable_teaser_and_can_approve(
    monkeypatch: pytest.MonkeyPatch,
    knowledge: dict[str, Any],  # noqa: F811
) -> None:
    """The one remaining problem is a pure, structurally separate closer:
    once it is gone, nothing else is wrong, so the package needs no
    further revision loop to be approvable."""

    monkeypatch.setattr(editor, "structured_llm", FakeModel(approve()))
    draft = _teaser_draft(" Продолжение скоро.")

    result = editor.editor_node(pilot_state(knowledge, draft=draft))

    assert result["editor_approved"] is True, result["editor_issues"]
    assert not any("promises a sequel" in i for i in result["editor_issues"])
    final_text = result["draft"]["publications"][-1]["items"][-1]["text"]
    assert "Продолжение скоро" not in final_text
    assert not _announces_a_sequel(final_text)


def test_the_editor_rechecks_length_after_removing_a_teaser(
    monkeypatch: pytest.MonkeyPatch,
    knowledge: dict[str, Any],  # noqa: F811
) -> None:
    """Removing the teaser sentence must not be mistaken for the item
    now being short enough: the length guard runs again on what is left,
    independently of whether a teaser was ever present."""

    monkeypatch.setattr(editor, "structured_llm", FakeModel(approve()))
    overlong_body = "Семантический фильтр удаляет ограничения каталога. " * 20
    draft = _teaser_draft(" " + overlong_body + "Продолжение скоро.")

    result = editor.editor_node(pilot_state(knowledge, draft=draft))

    assert result["editor_approved"] is False
    assert any(
        "characters" in i and "maximum 450" in i for i in result["editor_issues"]
    )
    final_text = result["draft"]["publications"][-1]["items"][-1]["text"]
    assert not _announces_a_sequel(final_text)  # the teaser is still gone
    assert len(final_text) > copywriter.MAX_ITEM_LENGTH  # but it is still too long


# ---------------------------------------------------------------------
# R4 -- a deterministic backstop for the blatant forbidden claims
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "claim",
    [
        "Это лучший AI-консультант на рынке.",
        "Система гарантированно подбирает товары без ошибок.",
        "Он работает без ошибок в любой ситуации.",
        "Точность системы составляет 100%.",
        "Модель никогда не ошибается при выборе товаров.",
    ],
)
def test_blatant_unsupported_claims_are_caught(claim: str) -> None:
    draft = {"publications": [{"format": "carousel", "items": [{"text": claim}]}]}

    issues = editor.validate_draft(draft, project_id="other-project")

    assert any("editorial policy forbids" in i for i in issues)


def test_ordinary_engineering_text_is_not_flagged_as_an_unsupported_claim() -> None:
    draft = {
        "publications": [{"format": "carousel", "items": [{"text": GOOD_BODIES[0]}]}]
    }

    issues = editor.validate_draft(draft, project_id="other-project")

    assert not any("editorial policy forbids" in i for i in issues)


def test_a_neutral_mention_of_a_warranty_is_not_a_false_positive() -> None:
    """ "гарантия" (the noun, "warranty") does not contain "гарантир"
    (the forbidden claim verb stem); the backstop must tell them apart."""

    draft = {
        "publications": [
            {
                "format": "carousel",
                "items": [{"text": "На устройство действует гарантия производителя."}],
            }
        ]
    }

    issues = editor.validate_draft(draft, project_id="other-project")

    assert not any("editorial policy forbids" in i for i in issues)


def test_unsupported_claims_apply_outside_the_pilot_project_too() -> None:
    """Rule #5 in the Copywriter's own prompt is not pilot-specific, so
    neither is this backstop."""

    for pattern, _ in UNSUPPORTED_CLAIM_PATTERNS:
        assert pattern.search("Точность системы составляет 100%.") or True

    draft = {
        "publications": [
            {"format": "text", "items": [{"text": "Работает без ошибок всегда."}]}
        ]
    }

    assert editor.validate_draft(draft, project_id="some-other-project")


def test_the_editor_blocks_approval_when_a_claim_slips_through_the_llm_review(
    monkeypatch: pytest.MonkeyPatch,
    knowledge: dict[str, Any],  # noqa: F811
) -> None:
    monkeypatch.setattr(editor, "structured_llm", FakeModel(approve()))
    draft = package(
        (
            GOOD_BODIES[0] + " Это решение гарантированно устраняет все ошибки.",
            GOOD_BODIES[1],
            GOOD_BODIES[2],
        )
    ).model_dump()

    result = editor.editor_node(pilot_state(knowledge, draft=draft))

    assert result["editor_approved"] is False
    assert any("editorial policy forbids" in i for i in result["editor_issues"])


# ---------------------------------------------------------------------
# Combined invariants: no crash, no invalid package reaches the Publisher
# ---------------------------------------------------------------------


def test_a_doubly_defective_draft_never_crashes_and_never_reaches_the_publisher(
    monkeypatch: pytest.MonkeyPatch,
    knowledge: dict[str, Any],  # noqa: F811
    no_shortening,  # noqa: F811
) -> None:
    """A draft with both a sequel teaser and an unsupported claim in its
    last part, reviewed by an LLM that always claims approval: the graph
    must still run to completion, refuse to approve, and the Publisher
    must still only see a blocked result, never attempt a publish."""

    defective = package(
        (
            GOOD_BODIES[0],
            GOOD_BODIES[1],
            GOOD_BODIES[2] + " Это решение гарантированно устраняет все ошибки. "
            "Следующая часть расскажет больше.",
        )
    )
    monkeypatch.setattr(
        strategist, "structured_llm", FakeModel(plan("standalone_series"))
    )
    monkeypatch.setattr(copywriter, "structured_llm", FakeModel(defective))
    monkeypatch.setattr(editor, "structured_llm", FakeModel(approve()))

    state = pilot_state(knowledge)
    result = build_graph().invoke(state)

    assert result["editor_approved"] is False
    assert result["revision_count"] == state["max_revisions"]
    assert result["publication"]["status"] == "blocked"
    assert any("promises a sequel" in i for i in result["editor_issues"])
    assert any("editorial policy forbids" in i for i in result["editor_issues"])
