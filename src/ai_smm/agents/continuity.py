"""Deterministic narrative-continuity checks for a drafted series.

These run before the Editor's LLM review and cannot be overridden by it.
They catch the failures that are a matter of fact rather than judgement:
a missing part number, two parts opening with the same sentence, a
transition promised by the plan but absent from the text.

Everything here works on the draft dictionary, so it can be used without a
database and without contacting any model.
"""
from __future__ import annotations

import re
from typing import Any


#: Threads caption limit for a post that carries media.
MAX_ITEM_LENGTH = 450

#: How much of two openings has to coincide before it reads as a repeat.
OPENING_COMPARISON_CHARS = 60

#: Two parts are "the same angle" when their theses overlap this much.
THESIS_OVERLAP_RATIO = 0.8

#: Words that belong to the series scaffolding rather than to the subject.
#: Without this, two parts about entirely different things would look
#: related because both say "часть N из M".
SERIES_BOILERPLATE = frozenset(
    {
        "часть",
        "части",
        "серия",
        "серии",
        "part",
        "series",
        "post",
        "пост",
        "далее",
        "дальше",
        "следующей",
        "предыдущей",
    }
)

#: How many subject words two consecutive parts must share before they
#: read as continuing one another.
MIN_SHARED_SUBJECT_WORDS = 2


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def _words(text: str, *, exclude: frozenset[str] | None = None) -> set[str]:
    """Content words, with the series scaffolding removed.

    exclude carries the series title on top of the fixed boilerplate: the
    title appears in every part by design, so counting it as shared
    subject matter would make any two parts look related.
    """

    excluded = SERIES_BOILERPLATE | (exclude or frozenset())

    return {
        word
        for word in re.findall(r"\w+", _normalize(text))
        if len(word) > 3 and word not in excluded
    }


def _publication_text(publication: dict[str, Any]) -> str:
    return "\n\n".join(
        item.get("text", "") for item in publication.get("items", [])
    ).strip()


def _mentions_part_number(text: str, position: int, total: int) -> bool:
    """Does the text say which part of the series it is?

    Accepts the natural phrasings rather than one fixed template: an
    editor rejecting "часть 2 из 3" because it expected "2/3" would be
    checking formatting, not continuity.
    """

    normalized = _normalize(text)

    patterns = (
        rf"часть\s*{position}\s*(из|/)\s*{total}",
        rf"part\s*{position}\s*(of|/)\s*{total}",
        rf"\b{position}\s*/\s*{total}\b",
        rf"\({position}\s*(из|/|of)\s*{total}\)",
        rf"\[{position}\s*(из|/|of)\s*{total}\]",
        rf"часть\s*{position}\b",
    )

    return any(re.search(p, normalized) for p in patterns)


def check_series_continuity(
    draft: dict[str, Any],
    *,
    strategy: str = "standalone_series",
    series_title: str = "",
) -> list[str]:
    """Return every continuity problem found, in reading order."""

    issues: list[str] = []
    publications = draft.get("publications")

    if not isinstance(publications, list) or not publications:
        return ["Draft has no publications to check for continuity."]

    total = len(publications)
    texts = [_publication_text(p) for p in publications]

    # The series title repeats in every part on purpose, so it carries no
    # information about whether two parts are about the same thing.
    scaffolding = frozenset(
        word
        for word in re.findall(r"\w+", _normalize(series_title))
        if len(word) > 3
    )

    # --- numbering ------------------------------------------------------
    positions = [
        p.get("position") or index
        for index, p in enumerate(publications, start=1)
    ]

    if sorted(positions) != list(range(1, total + 1)):
        issues.append(
            f"Part numbers are not a complete 1..{total} sequence: "
            f"{positions}."
        )

    # --- titles ---------------------------------------------------------
    titles = [(p.get("title") or "").strip() for p in publications]

    for index, title in enumerate(titles, start=1):
        if not title:
            issues.append(f"Part {index} has no title.")
        elif len(title) < 10:
            issues.append(
                f"Part {index} has a title too short to be informative: "
                f"{title!r}."
            )

    duplicate_titles = {
        title for title in titles if title and titles.count(title) > 1
    }

    for title in sorted(duplicate_titles):
        issues.append(f"More than one part is titled {title!r}.")

    # --- identical or near-identical content ----------------------------
    for index, text in enumerate(texts):
        for other_index in range(index + 1, total):
            if text and text == texts[other_index]:
                issues.append(
                    f"Parts {index + 1} and {other_index + 1} have "
                    "identical text."
                )

    # --- repeated openings ----------------------------------------------
    openings = [
        _normalize(text)[:OPENING_COMPARISON_CHARS] for text in texts
    ]

    for index, opening in enumerate(openings):
        if not opening:
            continue

        for other_index in range(index + 1, total):
            if opening == openings[other_index]:
                issues.append(
                    f"Parts {index + 1} and {other_index + 1} open with "
                    "the same sentence; each part should start on its own "
                    "footing."
                )

    # --- one angle per part ---------------------------------------------
    theses = [
        _words(p.get("main_thesis", ""), exclude=scaffolding)
        for p in publications
    ]

    for index, thesis in enumerate(theses):
        if len(thesis) < 4:
            continue

        for other_index in range(index + 1, total):
            other = theses[other_index]

            if len(other) < 4:
                continue

            overlap = len(thesis & other) / min(len(thesis), len(other))

            if overlap >= THESIS_OVERLAP_RATIO:
                issues.append(
                    f"Parts {index + 1} and {other_index + 1} argue nearly "
                    "the same thesis; the series should develop, not repeat."
                )

    # --- per-strategy requirements --------------------------------------
    if strategy == "standalone_series":
        issues.extend(
            _check_standalone(
                publications, texts, total, scaffolding=scaffolding
            )
        )
    elif strategy == "reply_thread":
        issues.extend(
            _check_reply_thread(
                publications, texts, total, scaffolding=scaffolding
            )
        )

    # --- platform limits ------------------------------------------------
    for index, publication in enumerate(publications, start=1):
        for item_index, item in enumerate(
            publication.get("items", []), start=1
        ):
            length = len(item.get("text", ""))

            if length > MAX_ITEM_LENGTH:
                issues.append(
                    f"Part {index}, item {item_index} is {length} "
                    f"characters, over the {MAX_ITEM_LENGTH} limit."
                )

    return issues


def _check_standalone(
    publications: list[dict[str, Any]],
    texts: list[str],
    total: int,
    *,
    scaffolding: frozenset[str] = frozenset(),
) -> list[str]:
    """A standalone part must announce itself: a reader may start here."""

    issues: list[str] = []

    for index, (publication, text) in enumerate(
        zip(publications, texts, strict=True), start=1
    ):
        if not text:
            issues.append(f"Part {index} has no text.")
            continue

        if total > 1 and not _mentions_part_number(text, index, total):
            issues.append(
                f"Part {index} does not say which part of {total} it is; "
                "a reader arriving at it alone cannot tell."
            )

        if index > 1:
            # A later part has to acknowledge what came before. A single
            # word in common is not a link -- the comparison ignores the
            # series scaffolding, so what is left is subject matter.
            previous_words = _words(
                texts[index - 2], exclude=scaffolding
            )
            current_words = _words(text, exclude=scaffolding)
            shared = previous_words & current_words

            if previous_words and len(shared) < MIN_SHARED_SUBJECT_WORDS:
                issues.append(
                    f"Part {index} shares almost no subject matter with "
                    f"part {index - 1}; the link between them is missing."
                )

        if not publication.get("main_thesis", "").strip():
            issues.append(f"Part {index} states no main thesis.")

    return issues


def _check_reply_thread(
    publications: list[dict[str, Any]],
    texts: list[str],
    total: int,
    *,
    scaffolding: frozenset[str] = frozenset(),
) -> list[str]:
    """A reply continues what is above it; it does not reintroduce it."""

    issues: list[str] = []

    if not texts[0]:
        issues.append("The opening post of the thread has no text.")

    first_words = _words(texts[0], exclude=scaffolding)

    for index in range(2, total + 1):
        text = texts[index - 1]

        if not text:
            issues.append(f"Reply {index} has no text.")
            continue

        # A reply that restates the opening wastes the reader's time.
        if first_words:
            overlap = len(
                first_words & _words(text, exclude=scaffolding)
            ) / len(first_words)

            if overlap >= 0.85:
                issues.append(
                    f"Reply {index} restates the opening post almost in "
                    "full instead of continuing it."
                )

        if _mentions_part_number(text, index, total):
            issues.append(
                f"Reply {index} numbers itself; in a thread the order is "
                "already visible and the numbering is noise."
            )

    # Media cannot travel in a reply.
    for index, publication in enumerate(publications, start=1):
        if index == 1:
            continue

        if publication.get("format") in {"image", "carousel"}:
            issues.append(
                f"Reply {index} is formatted as {publication['format']}; "
                "Threads replies carry text only."
            )

    return issues
