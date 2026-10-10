"""Reading the publication queue over HTTP: filters, pagination, isolation."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from ai_smm.db.models import (
    AttemptOutcome,
    AttemptPhase,
    MembershipRole,
    Publication,
    PublicationAttempt,
    PublicationStatus,
)


def _ids(body: dict) -> list[int]:
    return [item["id"] for item in body["items"]]


# -- listing -------------------------------------------------------------


def test_listing_requires_authentication(client, project: str) -> None:
    response = client.get(f"/api/v1/projects/{project}/publications")

    assert response.status_code == 401


def test_listing_returns_the_project_rows_with_an_envelope(
    client, login, member, project: str, make_publication
) -> None:
    make_publication()
    make_publication()
    login(member(MembershipRole.VIEWER))

    response = client.get(f"/api/v1/projects/{project}/publications")

    assert response.status_code == 200

    body = response.json()

    assert body["total"] == 2
    assert body["limit"] == 50
    assert body["offset"] == 0
    assert len(body["items"]) == 2

    first = body["items"][0]

    assert first["project_id"] == project
    assert first["body_chars"] > 0
    assert first["image_count"] == 0
    # A list row carries no text: it is a list, and the body can be long.
    assert "body" not in first


@pytest.mark.parametrize(
    "role",
    [
        MembershipRole.VIEWER,
        MembershipRole.EDITOR,
        MembershipRole.ADMIN,
        MembershipRole.OWNER,
    ],
)
def test_every_role_may_read_the_queue(
    client, login, member, project: str, make_publication, role
) -> None:
    make_publication()
    login(member(role))

    assert (
        client.get(f"/api/v1/projects/{project}/publications").status_code
        == 200
    )


def test_filter_by_status(
    client, login, member, project: str, make_publication
) -> None:
    scheduled = make_publication(status=PublicationStatus.SCHEDULED)
    failed = make_publication(status=PublicationStatus.FAILED)
    make_publication(status=PublicationStatus.DRAFT)
    login(member(MembershipRole.VIEWER))

    one = client.get(
        f"/api/v1/projects/{project}/publications?status=failed"
    ).json()

    assert _ids(one) == [failed.id]
    assert one["total"] == 1

    # Repeating the parameter narrows to a set, as `ai-smm queue` does.
    two = client.get(
        f"/api/v1/projects/{project}/publications"
        "?status=failed&status=scheduled"
    ).json()

    assert sorted(_ids(two)) == sorted([failed.id, scheduled.id])


def test_filter_by_format_and_review_flag(
    client, login, member, project: str, make_publication
) -> None:
    text = make_publication(publication_format="text")
    make_publication(
        publication_format="image",
        images=[{"path": "a.jpg", "alt_text": "a"}],
        human_reviewed=False,
    )
    login(member(MembershipRole.VIEWER))

    by_format = client.get(
        f"/api/v1/projects/{project}/publications?format=text"
    ).json()

    assert _ids(by_format) == [text.id]

    reviewed = client.get(
        f"/api/v1/projects/{project}/publications?human_reviewed=true"
    ).json()

    assert _ids(reviewed) == [text.id]

    unreviewed = client.get(
        f"/api/v1/projects/{project}/publications?human_reviewed=false"
    ).json()

    assert _ids(unreviewed) != [text.id]


def test_filter_by_series(
    client, login, member, project: str, make_publication, make_series
) -> None:
    loose = make_publication()
    part = make_publication()
    series = make_series(parts=[part])
    login(member(MembershipRole.VIEWER))

    body = client.get(
        f"/api/v1/projects/{project}/publications?series_id={series.id}"
    ).json()

    assert _ids(body) == [part.id]
    assert loose.id not in _ids(body)


def test_an_unknown_status_is_a_validation_error(
    client, login, member, project: str
) -> None:
    login(member(MembershipRole.VIEWER))

    response = client.get(
        f"/api/v1/projects/{project}/publications?status=not-a-status"
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


# -- pagination ----------------------------------------------------------


def test_pagination_walks_the_whole_set_without_repeats(
    client, login, member, project: str, make_publication
) -> None:
    created = [make_publication().id for _ in range(5)]
    login(member(MembershipRole.VIEWER))

    seen: list[int] = []

    for offset in (0, 2, 4):
        page = client.get(
            f"/api/v1/projects/{project}/publications"
            f"?limit=2&offset={offset}"
        ).json()

        assert page["total"] == 5
        assert page["limit"] == 2
        assert page["offset"] == offset

        seen.extend(_ids(page))

    assert sorted(seen) == sorted(created)
    assert len(set(seen)) == 5


def test_an_offset_past_the_end_is_an_empty_page_not_an_error(
    client, login, member, project: str, make_publication
) -> None:
    make_publication()
    login(member(MembershipRole.VIEWER))

    page = client.get(
        f"/api/v1/projects/{project}/publications?offset=50"
    ).json()

    assert page["items"] == []
    assert page["total"] == 1


def test_the_total_counts_the_filter_not_the_page(
    client, login, member, project: str, make_publication
) -> None:
    for _ in range(3):
        make_publication(status=PublicationStatus.DRAFT)

    make_publication(status=PublicationStatus.FAILED)
    login(member(MembershipRole.VIEWER))

    page = client.get(
        f"/api/v1/projects/{project}/publications?status=draft&limit=1"
    ).json()

    assert len(page["items"]) == 1
    assert page["total"] == 3


@pytest.mark.parametrize(
    "query", ["limit=0", "limit=201", "limit=-1", "offset=-1"]
)
def test_pagination_bounds_are_enforced(
    client, login, member, project: str, query: str
) -> None:
    login(member(MembershipRole.VIEWER))

    response = client.get(
        f"/api/v1/projects/{project}/publications?{query}"
    )

    assert response.status_code == 422


# -- detail --------------------------------------------------------------


def test_detail_returns_the_text_and_operational_fields(
    client, login, member, project: str, make_publication
) -> None:
    publication = make_publication(body="Текст публикации.")
    login(member(MembershipRole.VIEWER))

    body = client.get(f"/api/v1/publications/{publication.id}").json()

    assert body["id"] == publication.id
    assert body["body"] == "Текст публикации."
    assert body["idempotency_key"] == publication.idempotency_key
    assert body["status"] == "scheduled"
    assert body["threads_post_id"] is None


def test_an_unknown_publication_is_404(
    client, login, member, project: str
) -> None:
    login(member(MembershipRole.VIEWER))

    response = client.get("/api/v1/publications/987654")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


# -- project isolation ---------------------------------------------------


def test_a_publication_of_another_project_is_404(
    client,
    session: Session,
    login,
    member,
    make_other_project,
    make_publication,
) -> None:
    """Resource first, then membership: the id alone grants nothing."""

    other = make_other_project
    foreign = Publication(
        project_id=other,
        platform="threads",
        ordinal=1,
        title="Foreign",
        format="text",
        body="not yours",
        images=[],
        items=[],
        status=PublicationStatus.SCHEDULED,
        idempotency_key="other-project:threads:1:fixture",
    )
    session.add(foreign)
    session.commit()

    login(member(MembershipRole.OWNER))

    for path in (
        f"/api/v1/publications/{foreign.id}",
        f"/api/v1/publications/{foreign.id}/preview",
        f"/api/v1/publications/{foreign.id}/attempts",
    ):
        response = client.get(path)

        assert response.status_code == 404, path
        assert response.json()["error"]["code"] == "NOT_FOUND"


def test_a_foreign_publication_looks_like_a_missing_one(
    client, session: Session, login, member, make_other_project
) -> None:
    other = make_other_project
    foreign = Publication(
        project_id=other,
        platform="threads",
        ordinal=1,
        title="Foreign",
        format="text",
        body="not yours",
        images=[],
        items=[],
        status=PublicationStatus.SCHEDULED,
        idempotency_key="other-project:threads:9:fixture",
    )
    session.add(foreign)
    session.commit()

    login(member(MembershipRole.OWNER))

    hidden = client.get(f"/api/v1/publications/{foreign.id}")
    missing = client.get("/api/v1/publications/987654")

    assert hidden.status_code == missing.status_code == 404
    assert hidden.json()["error"] == {
        **missing.json()["error"],
        "request_id": hidden.json()["error"]["request_id"],
    }


def test_a_list_never_includes_another_projects_rows(
    client,
    session: Session,
    login,
    member,
    project: str,
    make_other_project,
    make_publication,
) -> None:
    mine = make_publication()
    other = make_other_project
    session.add(
        Publication(
            project_id=other,
            platform="threads",
            ordinal=1,
            title="Foreign",
            format="text",
            body="not yours",
            images=[],
            items=[],
            status=PublicationStatus.SCHEDULED,
            idempotency_key="other-project:threads:2:fixture",
        )
    )
    session.commit()

    login(member(MembershipRole.OWNER))

    body = client.get(f"/api/v1/projects/{project}/publications").json()

    assert _ids(body) == [mine.id]
    assert body["total"] == 1

    # And the other project's own list is not reachable at all.
    assert (
        client.get(f"/api/v1/projects/{other}/publications").status_code
        == 404
    )


# -- attempts ------------------------------------------------------------


def test_attempts_are_listed_in_order(
    client, session: Session, login, member, project: str, make_publication
) -> None:
    publication = make_publication()

    for number in (1, 2):
        session.add(
            PublicationAttempt(
                publication_id=publication.id,
                attempt_number=number,
                worker_id="worker-1",
                phase=AttemptPhase.PUBLISH,
                outcome=(
                    AttemptOutcome.ERROR if number == 1 else AttemptOutcome.SUCCESS
                ),
                http_status=500 if number == 1 else 200,
                error_type="HTTPError" if number == 1 else None,
                details={"note": f"attempt {number}"},
            )
        )

    session.commit()
    login(member(MembershipRole.VIEWER))

    body = client.get(
        f"/api/v1/publications/{publication.id}/attempts"
    ).json()

    assert body["total"] == 2
    assert [item["attempt_number"] for item in body["items"]] == [1, 2]
    assert body["items"][0]["outcome"] == "error"
    assert body["items"][0]["http_status"] == 500
    assert body["items"][1]["outcome"] == "success"
    assert body["items"][0]["details"] == {"note": "attempt 1"}


def test_attempts_are_paginated(
    client, session: Session, login, member, project: str, make_publication
) -> None:
    publication = make_publication()

    for number in range(1, 4):
        session.add(
            PublicationAttempt(
                publication_id=publication.id,
                attempt_number=number,
                worker_id="worker-1",
                phase=AttemptPhase.PREFLIGHT,
                details={},
            )
        )

    session.commit()
    login(member(MembershipRole.VIEWER))

    page = client.get(
        f"/api/v1/publications/{publication.id}/attempts?limit=2"
    ).json()

    assert page["total"] == 3
    assert [item["attempt_number"] for item in page["items"]] == [1, 2]


def test_a_publication_with_no_attempts_returns_an_empty_page(
    client, login, member, project: str, make_publication
) -> None:
    publication = make_publication()
    login(member(MembershipRole.VIEWER))

    body = client.get(
        f"/api/v1/publications/{publication.id}/attempts"
    ).json()

    assert body == {"items": [], "total": 0, "limit": 50, "offset": 0}


# -- preview -------------------------------------------------------------


def test_preview_reports_a_publishable_row(
    client, login, member, project: str, make_publication
) -> None:
    publication = make_publication()
    login(member(MembershipRole.VIEWER))

    body = client.get(
        f"/api/v1/publications/{publication.id}/preview"
    ).json()

    assert body["content_valid"] is True
    assert body["content_error"] is None
    assert body["human_reviewed"] is True
    assert body["series_ready"] is True
    assert body["blocking_parts"] == []
    assert body["publishable_now"] is True
    assert body["publication"]["body"] == publication.body


def test_preview_works_on_an_unreviewed_draft(
    client, login, member, project: str, make_publication
) -> None:
    """That is the point of a preview: deciding whether to approve it."""

    publication = make_publication(
        status=PublicationStatus.DRAFT, human_reviewed=False
    )
    login(member(MembershipRole.VIEWER))

    body = client.get(
        f"/api/v1/publications/{publication.id}/preview"
    ).json()

    assert body["content_valid"] is True
    assert body["human_reviewed"] is False
    assert body["publishable_now"] is False


def test_preview_reports_invalid_content_without_failing_the_request(
    client, login, member, project: str, make_publication
) -> None:
    """A text post carrying an image is the existing preflight's rule."""

    publication = make_publication(
        publication_format="text",
        images=[{"path": "a.jpg", "alt_text": "a"}],
    )
    login(member(MembershipRole.VIEWER))

    response = client.get(
        f"/api/v1/publications/{publication.id}/preview"
    )

    assert response.status_code == 200

    body = response.json()

    assert body["content_valid"] is False
    assert "image" in body["content_error"].lower()
    assert body["publishable_now"] is False


def test_preview_reports_a_part_waiting_for_its_turn(
    client, login, member, project: str, make_publication, make_series
) -> None:
    first = make_publication()
    second = make_publication()
    make_series(parts=[first, second])
    login(member(MembershipRole.VIEWER))

    body = client.get(f"/api/v1/publications/{second.id}/preview").json()

    assert body["series_ready"] is False
    assert body["blocking_parts"] == [first.id]
    assert body["publishable_now"] is False
    # The content itself is fine; only its turn is not.
    assert body["content_valid"] is True


def test_preview_sends_nothing_to_threads(
    client, login, member, project: str, make_publication, monkeypatch
) -> None:
    import ai_smm.publishing.threads as threads_module

    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("the preview contacted Threads")

    monkeypatch.setattr(threads_module.ThreadsPublisher, "__init__", explode)
    monkeypatch.setattr(
        threads_module.ThreadsPublisher, "publish_text", explode, raising=False
    )

    publication = make_publication()
    login(member(MembershipRole.VIEWER))

    assert (
        client.get(
            f"/api/v1/publications/{publication.id}/preview"
        ).status_code
        == 200
    )


def test_preview_changes_nothing(
    client, session: Session, login, member, project: str, make_publication
) -> None:
    publication = make_publication(
        status=PublicationStatus.DRAFT, human_reviewed=False
    )
    before = (
        publication.status,
        publication.human_reviewed,
        publication.attempt_count,
        publication.scheduled_at,
    )
    login(member(MembershipRole.VIEWER))

    client.get(f"/api/v1/publications/{publication.id}/preview")

    session.expire_all()
    session.refresh(publication)

    assert (
        publication.status,
        publication.human_reviewed,
        publication.attempt_count,
        publication.scheduled_at,
    ) == before


def test_a_published_row_is_reported_as_not_publishable_again(
    client, session: Session, login, member, project: str, make_publication
) -> None:
    publication = make_publication(
        status=PublicationStatus.PUBLISHED,
        threads_post_id="17900000000000001",
        published_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    login(member(MembershipRole.VIEWER))

    body = client.get(
        f"/api/v1/publications/{publication.id}/preview"
    ).json()

    assert body["content_valid"] is False
    assert body["publishable_now"] is False
