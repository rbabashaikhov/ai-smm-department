"""Narrative continuity checks, run without a database or a model."""
from __future__ import annotations

from typing import Any

import pytest

from ai_smm.agents.continuity import (
    MAX_ITEM_LENGTH,
    check_series_continuity,
)


def part(
    position: int,
    title: str,
    text: str,
    *,
    thesis: str = "",
    publication_format: str = "image",
) -> dict[str, Any]:
    return {
        "position": position,
        "title": title,
        "format": publication_format,
        "main_thesis": thesis or f"thesis number {position} about catalogs",
        "items": [{"order": 1, "text": text}],
    }


def standalone_series() -> dict[str, Any]:
    return {
        "publications": [
            part(
                1,
                "Почему обычного чат-бота недостаточно",
                "AI Catalog Consultant — часть 1 из 3. Обычный чат-бот не "
                "выбирает товары из каталога надёжно. Дальше — про поиск.",
                thesis="каталогу нужен детерминированный выбор товаров",
            ),
            part(
                2,
                "Почему vector search не выбирает товары",
                "AI Catalog Consultant — часть 2 из 3. В прошлой части "
                "говорили про чат-бота. Теперь про векторный поиск "
                "товаров каталога. Дальше — про ошибку.",
                thesis="векторный поиск подтверждает, SQL выбирает товары",
            ),
            part(
                3,
                "Запрос подешевле сломал архитектуру",
                "AI Catalog Consultant — часть 3 из 3. Векторный поиск "
                "работал, но запрос «подешевле» сломал выбор товаров "
                "каталога.",
                thesis="неопределённые ограничения ломают выбор товаров",
            ),
        ]
    }


# --- the good case -------------------------------------------------------


def test_a_coherent_series_passes() -> None:
    assert check_series_continuity(standalone_series()) == []


def test_an_empty_draft_is_reported() -> None:
    issues = check_series_continuity({"publications": []})

    assert issues
    assert "no publications" in issues[0]


# --- numbering -----------------------------------------------------------


def test_duplicate_positions_are_caught() -> None:
    draft = standalone_series()
    draft["publications"][1]["position"] = 1

    issues = check_series_continuity(draft)

    assert any("complete 1..3 sequence" in i for i in issues)


def test_a_missing_part_number_in_the_text_is_caught() -> None:
    """A standalone reader has no other way to know where they are."""

    draft = standalone_series()
    draft["publications"][1]["items"][0]["text"] = (
        "Про векторный поиск и выбор товаров из каталога, без номера."
    )

    issues = check_series_continuity(draft)

    assert any("does not say which part" in i for i in issues)


@pytest.mark.parametrize(
    "phrasing",
    [
        "AI Catalog Consultant — часть 2 из 3. Про каталог товаров.",
        "Part 2 of 3. Про каталог товаров и поиск.",
        "Разбор каталога товаров [2 из 3] и поиска.",
        "Каталог товаров и поиск (2/3).",
    ],
)
def test_numbering_is_accepted_in_several_phrasings(
    phrasing: str,
) -> None:
    """The editor checks continuity, not one fixed template."""

    draft = standalone_series()
    draft["publications"][1]["items"][0]["text"] = phrasing

    issues = check_series_continuity(draft)

    assert not any("does not say which part" in i for i in issues)


# --- titles ---------------------------------------------------------------


def test_a_missing_title_is_caught() -> None:
    draft = standalone_series()
    draft["publications"][0]["title"] = ""

    assert any(
        "has no title" in i for i in check_series_continuity(draft)
    )


def test_an_uninformative_title_is_caught() -> None:
    draft = standalone_series()
    draft["publications"][0]["title"] = "Обзор"

    assert any(
        "too short to be informative" in i
        for i in check_series_continuity(draft)
    )


def test_duplicate_titles_are_caught() -> None:
    draft = standalone_series()
    draft["publications"][1]["title"] = draft["publications"][0]["title"]

    assert any(
        "More than one part is titled" in i
        for i in check_series_continuity(draft)
    )


# --- repetition -----------------------------------------------------------


def test_identical_text_is_caught() -> None:
    draft = standalone_series()
    draft["publications"][1]["items"][0]["text"] = draft["publications"][
        0
    ]["items"][0]["text"]

    assert any(
        "identical text" in i for i in check_series_continuity(draft)
    )


def test_a_repeated_opening_is_caught() -> None:
    """Re-introducing the project in every part wastes the reader's time."""

    draft = standalone_series()
    opening = (
        "AI Catalog Consultant — это AI-консультант по каталогу "
        "телевизоров, построенный на детерминированном выборе. "
    )
    draft["publications"][0]["items"][0]["text"] = (
        opening + "Часть 1 из 3."
    )
    draft["publications"][1]["items"][0]["text"] = (
        opening + "Часть 2 из 3."
    )

    assert any(
        "open with the same sentence" in i
        for i in check_series_continuity(draft)
    )


def test_two_parts_arguing_the_same_thesis_are_caught() -> None:
    draft = standalone_series()
    draft["publications"][1]["main_thesis"] = draft["publications"][0][
        "main_thesis"
    ]

    assert any(
        "nearly the same thesis" in i
        for i in check_series_continuity(draft)
    )


def test_an_unrelated_part_is_caught() -> None:
    """A part sharing no subject matter with the previous one breaks the
    thread of the argument."""

    draft = standalone_series()
    draft["publications"][1]["items"][0]["text"] = (
        "Часть 2 из 3. Совершенно другая тема: погода, велосипеды, "
        "кулинария и путешествия."
    )

    assert any(
        "shares almost no subject matter" in i
        for i in check_series_continuity(draft)
    )


# --- platform limits ------------------------------------------------------


def test_an_overlong_item_is_caught() -> None:
    draft = standalone_series()
    draft["publications"][0]["items"][0]["text"] = "x" * (
        MAX_ITEM_LENGTH + 1
    )

    assert any(
        f"over the {MAX_ITEM_LENGTH} limit" in i
        for i in check_series_continuity(draft)
    )


def test_a_missing_thesis_is_caught() -> None:
    draft = standalone_series()
    draft["publications"][0]["main_thesis"] = ""

    assert any(
        "states no main thesis" in i
        for i in check_series_continuity(draft)
    )


# --- reply threads --------------------------------------------------------


def reply_thread() -> dict[str, Any]:
    return {
        "publications": [
            part(
                1,
                "Как каталог ломает обычного чат-бота",
                "Обычный чат-бот не выбирает товары из каталога надёжно. "
                "Разберу, почему и что с этим делать.",
                thesis="каталогу нужен детерминированный выбор",
                publication_format="text",
            ),
            part(
                2,
                "Что делает векторный поиск",
                "Векторный поиск хорошо находит формулировки, но плохо "
                "выбирает конкретные товары по ограничениям.",
                thesis="векторный поиск подтверждает, а не выбирает",
                publication_format="text",
            ),
            part(
                3,
                "Где это всё сломалось",
                "Запрос «подешевле» без числа ломал выбор: модель "
                "придумывала значения, которых в каталоге нет.",
                thesis="неопределённые ограничения ломают выбор",
                publication_format="text",
            ),
        ]
    }


def test_a_coherent_reply_thread_passes() -> None:
    issues = check_series_continuity(
        reply_thread(), strategy="reply_thread"
    )

    assert issues == []


def test_a_reply_that_numbers_itself_is_caught() -> None:
    """In a thread the order is already visible; numbering is noise."""

    draft = reply_thread()
    draft["publications"][1]["items"][0]["text"] = (
        "Часть 2 из 3. Векторный поиск находит формулировки."
    )

    issues = check_series_continuity(draft, strategy="reply_thread")

    assert any("numbers itself" in i for i in issues)


def test_a_reply_restating_the_opening_is_caught() -> None:
    draft = reply_thread()
    draft["publications"][1]["items"][0]["text"] = draft["publications"][
        0
    ]["items"][0]["text"]

    issues = check_series_continuity(draft, strategy="reply_thread")

    assert any(
        "restates the opening post" in i or "identical text" in i
        for i in issues
    )


def test_a_reply_with_media_is_caught() -> None:
    """Threads replies carry text only."""

    draft = reply_thread()
    draft["publications"][1]["format"] = "carousel"

    issues = check_series_continuity(draft, strategy="reply_thread")

    assert any("text only" in i for i in issues)


def test_the_opening_post_of_a_thread_may_carry_media() -> None:
    draft = reply_thread()
    draft["publications"][0]["format"] = "image"

    issues = check_series_continuity(draft, strategy="reply_thread")

    assert not any("text only" in i for i in issues)


def test_a_thread_does_not_require_part_numbers() -> None:
    issues = check_series_continuity(
        reply_thread(), strategy="reply_thread"
    )

    assert not any("does not say which part" in i for i in issues)


# --- single publications --------------------------------------------------


def test_a_single_publication_needs_no_numbering() -> None:
    """One post is not a series; it must not be told to number itself."""

    draft = {
        "publications": [
            part(
                1,
                "Один самостоятельный пост",
                "Текст одиночной публикации про каталог товаров.",
            )
        ]
    }

    assert check_series_continuity(draft) == []


def test_the_series_label_alone_is_not_a_link() -> None:
    """Repeating the series title does not make two parts related."""

    draft = standalone_series()
    draft["publications"][1]["items"][0]["text"] = (
        "AI Catalog Consultant — часть 2 из 3. Погода, велосипеды "
        "и кулинария."
    )

    issues = check_series_continuity(
        draft, series_title="AI Catalog Consultant"
    )

    assert any("shares almost no subject matter" in i for i in issues)


def test_the_series_title_does_not_mask_a_real_link() -> None:
    """Excluding the title must not make a coherent series look broken."""

    issues = check_series_continuity(
        standalone_series(), series_title="AI Catalog Consultant"
    )

    assert issues == []
