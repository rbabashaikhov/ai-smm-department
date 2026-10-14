"""What the API answers when the database guards refuse (SMM-025D).

Connected as ai_smm_cc_rw, with a plain test client: nothing plays
smm-auth's step-up here, so an approval meets the guard on
content_approvals exactly as it would from a compromised or buggy API.
The answer must be a 403 with a stable code -- never a 500, never a
partial write.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from ai_smm.api.errors import ErrorCode, guard_refusal
from ai_smm.db.models import ApprovalGrant, ContentApproval, ContentItem, MembershipRole


@pytest.fixture
def plain_client(api_app) -> Iterator[Any]:
    from fastapi.testclient import TestClient

    with TestClient(api_app) as test_client:
        yield test_client


def _post(client, path: str, csrf: str, **body):
    return client.post(path, json=body, headers={"X-CSRF-Token": csrf})


def test_an_approval_without_a_step_up_is_a_403_and_writes_nothing(
    plain_client, open_session, session: Session, member, project: str
) -> None:
    owner = member(MembershipRole.OWNER)
    csrf = open_session(plain_client, owner)

    item = _post(
        plain_client,
        f"/api/v1/projects/{project}/content-items",
        csrf,
        title="Без подтверждения",
        content_type="post",
        revision={"body": "Текст.", "format": "text"},
    ).json()
    revision_id = item["current_revision_id"]

    assert (
        _post(
            plain_client,
            f"/api/v1/content-items/{item['id']}/submit-review",
            csrf,
            revision_id=revision_id,
        ).status_code
        == 200
    )

    response = _post(
        plain_client,
        f"/api/v1/content-items/{item['id']}/approve",
        csrf,
        revision_id=revision_id,
    )

    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == ErrorCode.APPROVAL_CONFIRMATION_REQUIRED
    # The guard's text (ids, SQL) is logged, not returned.
    assert "guard:" not in response.text

    session.expire_all()
    stored = session.get(ContentItem, item["id"])

    assert stored.status.value == "in_review"
    assert stored.approved_revision_id is None
    assert (
        session.scalar(
            select(func.count())
            .select_from(ContentApproval)
            .where(ContentApproval.content_item_id == stored.id)
        )
        == 0
    )
    assert session.scalar(select(func.count()).select_from(ApprovalGrant)) == 0


def test_a_rejection_needs_no_step_up(
    plain_client, open_session, member, project: str
) -> None:
    owner = member(MembershipRole.OWNER)
    csrf = open_session(plain_client, owner)

    item = _post(
        plain_client,
        f"/api/v1/projects/{project}/content-items",
        csrf,
        title="Отклонить",
        content_type="post",
        revision={"body": "Текст.", "format": "text"},
    ).json()
    revision_id = item["current_revision_id"]
    _post(
        plain_client,
        f"/api/v1/content-items/{item['id']}/submit-review",
        csrf,
        revision_id=revision_id,
    )

    response = _post(
        plain_client,
        f"/api/v1/content-items/{item['id']}/reject",
        csrf,
        revision_id=revision_id,
        note="Не то.",
    )

    assert response.status_code == 200, response.text


# -- the mapping itself --------------------------------------------------------


class _Orig(Exception):
    def __init__(self, message: str, sqlstate: str) -> None:
        super().__init__(message)
        self.sqlstate = sqlstate


def _error(message: str, sqlstate: str) -> DBAPIError:
    return DBAPIError("INSERT ...", {}, _Orig(message, sqlstate))


@pytest.mark.parametrize(
    ("message", "sqlstate", "expected"),
    [
        (
            "guard: an approval by this role needs a step-up confirmation",
            "42501",
            ErrorCode.APPROVAL_CONFIRMATION_REQUIRED,
        ),
        (
            "guard: no session is bound to this transaction",
            "42501",
            ErrorCode.GUARD_REFUSED,
        ),
        # Not the guards: a plain privilege error stays an unexpected 500.
        ("permission denied for table publications", "42501", None),
        # Not a privilege error at all, whatever it says.
        ("guard: look-alike", "23505", None),
    ],
)
def test_only_guard_refusals_become_a_403(
    message: str, sqlstate: str, expected: str | None
) -> None:
    refusal = guard_refusal(_error(message, sqlstate))

    assert (refusal[0] if refusal else None) == expected
