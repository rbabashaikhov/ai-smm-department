"""content_hashing is the same code under a new path (SMM-033B R2).

ai_smm.application.content re-exports canonical_content and content_hash
from ai_smm.application.content_hashing; both import paths must give
byte-identical results, and the expected digests below pin the algorithm
itself so a change to either path is caught.
"""
from __future__ import annotations

import hashlib
import json
import uuid

import pytest

from ai_smm.application import content, content_hashing


#: content_hash of CASES[0], computed with the implementation as it was in
#: ai_smm.application.content before the move (bf4b5e5).
PINNED_DIGEST = "0dffa2b45f1857ecc7fbecf30433ff8d55f7ec4289ce3f140e22369fde9ace55"

ACCOUNT = uuid.UUID("11111111-2222-3333-4444-555555555555")

CASES = [
    {"title": None, "body": "Текст", "format": "text", "images": [], "items": []},
    {
        "title": "Заголовок «серия» — часть 1",
        "body": "Строка 1\n\nСтрока 2 ✓",
        "format": "carousel",
        "images": [{"path": "knowledge/projects/p/assets/a.png", "alt": "А"}],
        "items": [{"order": 2, "text": "б"}, {"order": 1, "text": "а"}],
    },
    {
        "title": "t",
        "body": "b",
        "format": "image",
        "images": [],
        "items": [],
        "social_account_id": ACCOUNT,
    },
]


def test_the_old_path_is_the_new_function() -> None:
    assert content.content_hash is content_hashing.content_hash
    assert content.canonical_content is content_hashing.canonical_content


@pytest.mark.parametrize("case", CASES)
def test_both_paths_give_byte_identical_results(case: dict) -> None:
    old_canonical = content.canonical_content(**case)
    new_canonical = content_hashing.canonical_content(**case)

    assert old_canonical.encode("utf-8") == new_canonical.encode("utf-8")
    assert content.content_hash(**case) == content_hashing.content_hash(**case)


@pytest.mark.parametrize("case", CASES)
def test_the_algorithm_is_unchanged(case: dict) -> None:
    snapshot = {k: case[k] for k in ("title", "body", "format", "images", "items")}
    if case.get("social_account_id") is not None:
        snapshot["social_account_id"] = str(case["social_account_id"])
    expected_canonical = json.dumps(
        snapshot, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )

    assert content_hashing.canonical_content(**case) == expected_canonical
    assert content_hashing.content_hash(**case) == hashlib.sha256(
        expected_canonical.encode("utf-8")
    ).hexdigest()


def test_a_known_digest() -> None:
    assert content_hashing.content_hash(**CASES[0]) == PINNED_DIGEST
