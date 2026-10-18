"""Deterministic narrative-continuity checks for a drafted series.

These run before the Editor's LLM review and cannot be overridden by it.
They catch the failures that are a matter of fact rather than judgement:
a missing part number, two parts opening with the same sentence, a
transition promised by the plan but absent from the text.

Everything here works on the draft dictionary, so it can be used without a
database and without contacting any model.
"""
from __future__ import annotations

import copy
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

#: A coarse, truncation-based Russian "stem": not a morphological
#: analyzer, just enough that "решение"/"решения"/"решений" collapse to
#: one token for a word-overlap heuristic. Over-collapsing is an accepted
#: risk of this approach; it only ever adds matches, so it cannot turn a
#: genuinely unrelated part into one that looks continuous, and the test
#: suite below checks that it does not.
STEM_LENGTH = 5


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def _stem(word: str) -> str:
    """Truncate to a fixed prefix so common endings stop mattering.

    "товар", "товара", "товаров" and "товарами" all truncate to "товар";
    a word no longer than STEM_LENGTH is returned whole. This is cheap and
    wrong in plenty of individual cases (two unrelated words can share a
    prefix), but for the one thing it is used for here -- does the next
    part pick up roughly the same subject -- collapsing too much only
    ever adds a match, never removes a real link, so it cannot report two
    genuinely unrelated parts as continuing one another.
    """

    return word if len(word) <= STEM_LENGTH else word[:STEM_LENGTH]


def _words(text: str, *, exclude: frozenset[str] | None = None) -> set[str]:
    """Content words, with the series scaffolding removed.

    exclude carries the series title on top of the fixed boilerplate: the
    title appears in every part by design, so counting it as shared
    subject matter would make any two parts look related.
    """

    excluded = SERIES_BOILERPLATE | (exclude or frozenset())
    excluded_stems = {_stem(word) for word in excluded}

    return {
        _stem(word)
        for word in re.findall(r"\w+", _normalize(text))
        if len(word) > 3
        and word not in excluded
        and _stem(word) not in excluded_stems
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


#: The body of a series label: the part number, with or without
#: brackets, in any of the accepted phrasings.
_PART_LABEL_BODY = (
    r"[\(\[]?\s*(?:часть|part)\s*\d+\s*(?:из|of|/)\s*\d+\s*[\)\]]?"
    r"|[\(\[]?\s*\d+\s*/\s*\d+\s*[\)\]]?"
)

#: The label a standalone part opens with: «<series> — часть N из M».
_PART_LABEL = re.compile(rf"^\s*(?:{_PART_LABEL_BODY})", re.IGNORECASE)

#: The same body, anchored to the end instead, for a label the Copywriter
#: wrote as a closing line rather than an opening one.
_PART_LABEL_TAIL = re.compile(
    rf"(?:{_PART_LABEL_BODY})\s*\.?\s*$", re.IGNORECASE
)

_LABEL_SEPARATORS = " \t—–-:|·.,"


def split_series_label(text: str, series_title: str = "") -> tuple[str, str]:
    """Split the leading series label off a part: (label, rest).

    The label is the series title followed by the part number, or the part
    number alone, at the very start of the text. It is scaffolding the
    Copywriter is told to put there, identical in every part except for
    the number, so it says nothing about how a part opens. Returns
    ("", text) when the text does not start with one.
    """

    rest = text.lstrip()
    title = series_title.strip()

    if title and rest.casefold().startswith(title.casefold()):
        rest = rest[len(title):].lstrip(_LABEL_SEPARATORS)

    match = _PART_LABEL.match(rest)

    if match is None:
        return "", text

    rest = rest[match.end():].lstrip(_LABEL_SEPARATORS + "\n")

    return text[: len(text) - len(rest)].rstrip(), rest


def format_series_label(series_title: str, position: int, total: int) -> str:
    """The one deterministic label every standalone part opens with.

    Earlier versions left the Copywriter to word this itself, which
    produced several formats within the same series -- sometimes with the
    title, sometimes without, sometimes at the end of the text instead of
    the start. The Copywriter no longer writes this: the system puts it
    at the front of the body and checks the combined length, so there is
    exactly one format and it is always where the checks below expect it.
    """

    title = series_title.strip()

    if title:
        return f"{title} — часть {position} из {total}"

    return f"Часть {position} из {total}"


def strip_series_label(text: str, series_title: str = "") -> str:
    """Remove a series label from wherever it was written, start or end.

    split_series_label only looks at the start of the text, which is
    where a part is meant to carry its label. The Copywriter sometimes
    puts an equivalent phrase at the very end instead, which the
    start-anchored check does not recognise as a label at all, so it is
    compared, shortened and reported as ordinary body text. This looks at
    both ends and returns the body alone either way, so the canonical
    label this module writes is never stacked on top of one the model
    already wrote.
    """

    label, body = split_series_label(text, series_title)

    if label:
        return body

    stripped = text.rstrip()
    match = _PART_LABEL_TAIL.search(stripped)

    if match is None:
        return text

    head = stripped[: match.start()].rstrip(_LABEL_SEPARATORS + ".")
    title = series_title.strip()

    if title and head.casefold().endswith(title.casefold()):
        head = head[: len(head) - len(title)].rstrip(_LABEL_SEPARATORS)

    return head


#: Phrases that promise a sequel: a next part, episode, or "to be
#: continued". Matched only against the tail of the last part, so a
#: mid-series "Далее — ..." transition toward an existing next part (the
#: Copywriter is told to write exactly that) is never confused with one.
_SEQUEL_TEASER_PATTERNS = (
    re.compile(r"следующ\w*\s+(част\w*|публикаци\w*|пост\w*)", re.IGNORECASE),
    re.compile(r"в\s+следующ\w*\s+(раз|части)", re.IGNORECASE),
    re.compile(r"продолжени\w*\s+(скоро|следует)", re.IGNORECASE),
    re.compile(r"\bдалее\s*[—:-]\s*\S", re.IGNORECASE),
    re.compile(r"to\s+be\s+continued", re.IGNORECASE),
)

#: How much of the end of the last part is checked for a teaser. A short
#: window keeps a legitimate mid-text mention (how a feature continues to
#: behave, say) from being read as a promise of a sequel.
SEQUEL_TEASER_WINDOW_CHARS = 160


def _announces_a_sequel(text: str) -> bool:
    tail = _normalize(text)[-SEQUEL_TEASER_WINDOW_CHARS:]

    return any(pattern.search(tail) for pattern in _SEQUEL_TEASER_PATTERNS)


#: A sentence boundary: one of .!? followed by whitespace. Not a
#: linguistic parser -- good enough to split off the last sentence of a
#: part from the real content before it.
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")

#: Below this many characters, what is left after removing a teaser
#: sentence is treated as having nothing meaningful left to publish --
#: closer to an accidental empty post than a conclusion.
MIN_BODY_AFTER_TEASER_REMOVAL = 20


def _trailing_sentence(text: str) -> tuple[str, str]:
    """Split a text's last sentence off: (everything before it, it).

    An empty first element means the whole text is one sentence (or has
    no sentence-ending punctuation at all).
    """

    stripped = text.rstrip()
    parts = _SENTENCE_BOUNDARY.split(stripped)

    if len(parts) < 2:
        return "", stripped

    *head, last = parts

    return " ".join(head), last


def remove_safe_sequel_teaser(
    text: str, *, series_title: str = ""
) -> tuple[str, bool]:
    """Deterministically drop a trailing sequel teaser, only when doing so
    is unambiguous: the teaser is its own sentence and nothing else.

    "Продолжение скоро." at the end of the last part is pure scaffolding
    the Copywriter was told not to write; dropping that one sentence is a
    mechanical edit with no judgement call in it. "Следующая часть
    расскажет о новых решениях." is the same trigger phrase, but fused
    with a real claim about what comes next -- removing just the trigger
    would leave a dangling fragment ("расскажет о новых решениях"), so
    this refuses to touch it at all.

    Returns (text, False) unchanged whenever removal is not unambiguously
    safe: nothing to remove, the teaser sentence also carries real
    content, or too little would be left to call it a conclusion. Only
    when it returns (new_text, True) has anything actually changed; the
    caller still has to re-run every other check against the result, not
    trust this in isolation.
    """

    if not _announces_a_sequel(text):
        return text, False

    label, body = split_series_label(text, series_title)
    head, last_sentence = _trailing_sentence(body)

    if not head:
        return text, False  # the whole body was (or reduces to) the teaser

    # Strip every teaser pattern out of the trailing sentence; whatever
    # survives is content the sentence also carried.
    remainder = last_sentence

    for pattern in _SEQUEL_TEASER_PATTERNS:
        remainder = pattern.sub("", remainder)

    if _words(remainder):
        return text, False  # fused with real content: not a safe cut

    cleaned_body = head.strip()

    if len(cleaned_body) < MIN_BODY_AFTER_TEASER_REMOVAL:
        return text, False  # next to nothing left to call a conclusion

    separator = "\n\n" if label else ""
    cleaned = f"{label}{separator}{cleaned_body}"

    # Belt and suspenders: the edit must actually have worked. A second,
    # still-unremoved teaser earlier in the text (rare, but not
    # impossible) means this is not a complete fix, so it is not applied.
    if _announces_a_sequel(cleaned):
        return text, False

    return cleaned, True


def remove_safe_sequel_teaser_from_draft(
    draft: dict[str, Any],
    *,
    series_title: str = "",
    expects_sequel: bool = False,
) -> tuple[dict[str, Any], bool]:
    """Apply remove_safe_sequel_teaser to the last item of the last part.

    Returns (draft, False) -- the exact same object, untouched -- unless
    a safe removal both applies and fully resolves the check; only then
    does it return a deep copy with that one item's text edited. The
    caller (the Editor) runs its normal validate_draft and
    check_series_continuity pass on whatever comes back either way, so a
    draft this leaves unchanged is reported and blocked exactly as before.
    """

    publications = draft.get("publications")

    if expects_sequel or not isinstance(publications, list) or not publications:
        return draft, False

    last_publication = publications[-1]
    items = last_publication.get("items")

    if not isinstance(items, list) or not items:
        return draft, False

    last_item = items[-1]
    original_text = last_item.get("text", "")

    if not isinstance(original_text, str):
        return draft, False

    cleaned_text, removed = remove_safe_sequel_teaser(
        original_text, series_title=series_title
    )

    if not removed:
        return draft, False

    candidate = copy.deepcopy(draft)
    candidate["publications"][-1]["items"][-1]["text"] = cleaned_text

    # The publication as a whole (every item joined), not just the one
    # edited item, must no longer trip the check.
    if _announces_a_sequel(_publication_text(candidate["publications"][-1])):
        return draft, False

    return candidate, True


def check_series_continuity(
    draft: dict[str, Any],
    *,
    strategy: str = "standalone_series",
    series_title: str = "",
    expects_sequel: bool = False,
) -> list[str]:
    """Return every continuity problem found, in reading order.

    expects_sequel is a hook for a content plan that explicitly schedules a
    continuation beyond this series; nothing currently sets one, so unless
    a caller says otherwise the last part is checked as the series' end.
    """

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
    # The series label is compared away: with a long series title it fills
    # the whole comparison window, and every part "opens the same way".
    openings = [
        _normalize(split_series_label(text, series_title)[1])[
            :OPENING_COMPARISON_CHARS
        ]
        for text in texts
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

    # --- no unannounced continuation -------------------------------------
    # Nothing in the data model currently lets a plan say more parts are
    # coming, so unless a caller says otherwise the last part has to read
    # as an ending, not a cliffhanger.
    if not expects_sequel and texts and _announces_a_sequel(texts[-1]):
        issues.append(
            f"Part {total} promises a sequel, but the series has no "
            "further parts planned; the last part must conclude it "
            "instead of announcing one."
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
