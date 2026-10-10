"""Series views, the operations summary, the attention list and audit RBAC."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from ai_smm.db.models import (
    ContentSeries,
    MembershipRole,
    PublicationStatus,
    PublishingStrategy,
)


# -- series --------------------------------------------------------------


def test_series_list_is_scoped_and_counted(
    client, login, member, project: str, make_publication, make_series
) -> None:
    first = make_publication()
    second = make_publication()
    series = make_series(title="Как мы это делаем", parts=[first, second])
    login(member(MembershipRole.VIEWER))

    body = client.get(f"/api/v1/projects/{project}/series").json()

    assert body["total"] == 1
    assert body["limit"] == 50

    item = body["items"][0]

    assert item["id"] == series.id
    assert item["title"] == "Как мы это делаем"
    assert item["project_id"] == project
    assert item["parts_total"] == 2
    assert item["parts_published"] == 0
    assert item["publishing_strategy"] == "standalone_series"


def test_series_list_counts_published_parts(
    client, session: Session, login, member, project, make_publication, make_series
) -> None:
    first = make_publication(
        status=PublicationStatus.PUBLISHED,
        threads_post_id="17900000000000011",
        published_at=datetime.now(timezone.utc),
    )
    second = make_publication()
    make_series(parts=[first, second])
    login(member(MembershipRole.VIEWER))

    item = client.get(
        f"/api/v1/projects/{project}/series"
    ).json()["items"][0]

    assert item["parts_total"] == 2
    assert item["parts_published"] == 1


def test_series_list_is_paginated(
    client, login, member, project: str, make_series
) -> None:
    for index in range(3):
        make_series(title=f"Series {index}")

    login(member(MembershipRole.VIEWER))

    page = client.get(
        f"/api/v1/projects/{project}/series?limit=2&offset=2"
    ).json()

    assert page["total"] == 3
    assert len(page["items"]) == 1
    assert page["offset"] == 2


def test_series_detail_lists_the_parts_in_order(
    client, login, member, project: str, make_publication, make_series
) -> None:
    first = make_publication()
    second = make_publication()
    series = make_series(
        strategy=PublishingStrategy.REPLY_THREAD, parts=[first, second]
    )
    login(member(MembershipRole.VIEWER))

    body = client.get(f"/api/v1/series/{series.id}").json()

    assert body["id"] == series.id
    assert body["publishing_strategy"] == "reply_thread"
    assert body["enforce_order"] is True
    assert [part["id"] for part in body["parts"]] == [first.id, second.id]
    assert [part["series_position"] for part in body["parts"]] == [1, 2]
    assert body["parts_total"] == 2


def test_series_detail_reports_structural_issues(
    client, login, member, project: str, make_series
) -> None:
    """The report is ai_smm.series.validate_series_structure, not a copy."""

    series = make_series()
    login(member(MembershipRole.VIEWER))

    body = client.get(f"/api/v1/series/{series.id}").json()

    assert body["parts_total"] == 0
    assert any("no publications" in issue for issue in body["issues"])


def test_an_unknown_series_is_404(client, login, member, project) -> None:
    login(member(MembershipRole.VIEWER))

    assert client.get("/api/v1/series/987654").status_code == 404


def test_another_projects_series_is_404(
    client, session: Session, login, member, make_other_project
) -> None:
    """Resource first, then membership derived from the row."""

    other = make_other_project
    foreign = ContentSeries(project_id=other, title="Foreign series")
    session.add(foreign)
    session.commit()

    login(member(MembershipRole.OWNER))

    response = client.get(f"/api/v1/series/{foreign.id}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_a_series_list_never_crosses_projects(
    client, session: Session, login, member, project, make_other_project, make_series
) -> None:
    mine = make_series()
    other = make_other_project
    session.add(ContentSeries(project_id=other, title="Foreign series"))
    session.commit()

    login(member(MembershipRole.VIEWER))

    body = client.get(f"/api/v1/projects/{project}/series").json()

    assert [item["id"] for item in body["items"]] == [mine.id]
    assert client.get(f"/api/v1/projects/{other}/series").status_code == 404


# -- operations summary --------------------------------------------------


def test_summary_counts_by_status(
    client, login, member, project: str, make_publication
) -> None:
    make_publication(status=PublicationStatus.DRAFT)
    make_publication(status=PublicationStatus.FAILED)
    make_publication(status=PublicationStatus.NEEDS_REVIEW)
    make_publication(
        status=PublicationStatus.SCHEDULED,
        scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    make_publication(
        status=PublicationStatus.SCHEDULED,
        scheduled_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
    login(member(MembershipRole.VIEWER))

    body = client.get(
        f"/api/v1/projects/{project}/operations/summary"
    ).json()

    assert body["project_id"] == project
    assert body["total"] == 5
    assert body["by_status"]["draft"] == 1
    assert body["by_status"]["scheduled"] == 2
    assert body["by_status"]["published"] == 0
    assert body["due_now"] == 1
    assert body["needs_attention"] == 2
    assert body["next_scheduled_at"] is not None
    assert body["last_published_at"] is None
    assert body["published_last_24h"] == 0
    assert isinstance(body["dry_run"], bool)


def test_summary_counts_recent_publishes(
    client, login, member, project: str, make_publication
) -> None:
    now = datetime.now(timezone.utc)
    make_publication(
        status=PublicationStatus.PUBLISHED,
        threads_post_id="17900000000000021",
        published_at=now - timedelta(hours=2),
    )
    make_publication(
        status=PublicationStatus.PUBLISHED,
        threads_post_id="17900000000000022",
        published_at=now - timedelta(days=3),
    )
    login(member(MembershipRole.VIEWER))

    body = client.get(
        f"/api/v1/projects/{project}/operations/summary"
    ).json()

    assert body["by_status"]["published"] == 2
    assert body["published_last_24h"] == 1
    assert body["last_published_at"] is not None


def test_summary_counts_series(
    client, login, member, project: str, make_series
) -> None:
    make_series()
    login(member(MembershipRole.VIEWER))

    body = client.get(
        f"/api/v1/projects/{project}/operations/summary"
    ).json()

    assert body["series_total"] == 1
    # A freshly created series is a draft, not active.
    assert body["series_active"] == 0


def test_summary_never_counts_another_projects_rows(
    client,
    session: Session,
    login,
    member,
    project: str,
    make_other_project,
    make_publication,
) -> None:
    """The platform-wide counters would have leaked here."""

    from ai_smm.db.models import Publication

    make_publication(status=PublicationStatus.DRAFT)
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
            status=PublicationStatus.PUBLISHED,
            threads_post_id="17900000000000031",
            published_at=datetime.now(timezone.utc),
            idempotency_key="other-project:threads:31:fixture",
        )
    )
    session.commit()

    login(member(MembershipRole.VIEWER))

    body = client.get(
        f"/api/v1/projects/{project}/operations/summary"
    ).json()

    assert body["total"] == 1
    assert body["by_status"]["published"] == 0
    assert body["published_last_24h"] == 0
    assert body["last_published_at"] is None

    assert (
        client.get(
            f"/api/v1/projects/{other}/operations/summary"
        ).status_code
        == 404
    )


def test_summary_of_an_empty_project_is_all_zeroes(
    client, login, member, project: str
) -> None:
    login(member(MembershipRole.VIEWER))

    body = client.get(
        f"/api/v1/projects/{project}/operations/summary"
    ).json()

    assert body["total"] == 0
    assert set(body["by_status"].values()) == {0}
    assert body["due_now"] == 0
    assert body["needs_attention"] == 0


# -- attention view ------------------------------------------------------


def test_attention_lists_only_failed_and_needs_review(
    client, login, member, project: str, make_publication
) -> None:
    failed = make_publication(status=PublicationStatus.FAILED)
    ambiguous = make_publication(status=PublicationStatus.NEEDS_REVIEW)
    make_publication(status=PublicationStatus.SCHEDULED)
    make_publication(status=PublicationStatus.DRAFT)
    login(member(MembershipRole.VIEWER))

    body = client.get(
        f"/api/v1/projects/{project}/operations/attention"
    ).json()

    assert body["total"] == 2
    assert sorted(item["id"] for item in body["items"]) == sorted(
        [failed.id, ambiguous.id]
    )


def test_attention_is_paginated(
    client, login, member, project: str, make_publication
) -> None:
    for _ in range(3):
        make_publication(status=PublicationStatus.FAILED)

    login(member(MembershipRole.VIEWER))

    page = client.get(
        f"/api/v1/projects/{project}/operations/attention?limit=1"
    ).json()

    assert page["total"] == 3
    assert len(page["items"]) == 1


def test_reading_the_attention_view_retries_nothing(
    client, session: Session, login, member, project: str, make_publication
) -> None:
    """Listing an ambiguous row must never move it."""

    publication = make_publication(
        status=PublicationStatus.NEEDS_REVIEW, attempt_count=2
    )
    login(member(MembershipRole.VIEWER))

    client.get(f"/api/v1/projects/{project}/operations/attention")

    session.expire_all()
    session.refresh(publication)

    assert publication.status is PublicationStatus.NEEDS_REVIEW
    assert publication.attempt_count == 2
    assert publication.claimed_by is None


def test_attention_is_empty_when_nothing_needs_a_human(
    client, login, member, project: str, make_publication
) -> None:
    make_publication(status=PublicationStatus.SCHEDULED)
    login(member(MembershipRole.VIEWER))

    body = client.get(
        f"/api/v1/projects/{project}/operations/attention"
    ).json()

    assert body == {"items": [], "total": 0, "limit": 50, "offset": 0}


# -- audit RBAC ----------------------------------------------------------


@pytest.mark.parametrize(
    "role", [MembershipRole.VIEWER, MembershipRole.EDITOR]
)
def test_viewer_and_editor_cannot_read_the_audit_log(
    client, login, member, project: str, role: MembershipRole
) -> None:
    login(member(role))

    response = client.get(f"/api/v1/projects/{project}/audit")

    assert response.status_code == 403

    error = response.json()["error"]

    assert error["code"] == "FORBIDDEN"
    assert error["details"] == {
        "required_role": "admin",
        "your_role": role.value,
    }


@pytest.mark.parametrize(
    "role", [MembershipRole.ADMIN, MembershipRole.OWNER]
)
def test_admin_and_owner_may_read_the_audit_log(
    client, login, member, project: str, role: MembershipRole
) -> None:
    login(member(role))

    assert client.get(f"/api/v1/projects/{project}/audit").status_code == 200


def test_audit_shows_entries_about_this_project(
    client, session: Session, login, member, project: str, make_publication
) -> None:
    publication = make_publication(status=PublicationStatus.SCHEDULED)
    csrf = login(member(MembershipRole.ADMIN))

    client.post(
        f"/api/v1/publications/{publication.id}/cancel",
        json={"note": "not needed"},
        headers={"X-CSRF-Token": csrf},
    )

    body = client.get(f"/api/v1/projects/{project}/audit").json()
    actions = [entry["action"] for entry in body["items"]]

    assert "cancelled" in actions

    entry = next(
        item for item in body["items"] if item["action"] == "cancelled"
    )

    assert entry["subject"] == f"publication:{publication.id}"
    assert entry["details"] == {"note": "not needed"}


def test_audit_can_be_filtered_by_action(
    client, session: Session, login, member, project: str, make_publication
) -> None:
    first = make_publication(status=PublicationStatus.SCHEDULED)
    second = make_publication(status=PublicationStatus.APPROVED)
    csrf = login(member(MembershipRole.ADMIN))
    headers = {"X-CSRF-Token": csrf}

    client.post(
        f"/api/v1/publications/{first.id}/cancel", json={}, headers=headers
    )
    client.post(
        f"/api/v1/publications/{second.id}/schedule",
        json={
            "scheduled_at": (
                datetime.now(timezone.utc) + timedelta(hours=1)
            ).isoformat()
        },
        headers=headers,
    )

    body = client.get(
        f"/api/v1/projects/{project}/audit?action=cancelled"
    ).json()

    assert body["total"] == 1
    assert body["items"][0]["subject"] == f"publication:{first.id}"


def test_audit_excludes_another_projects_entries(
    client,
    session: Session,
    login,
    member,
    project: str,
    make_other_project,
) -> None:
    from ai_smm.db.models import Publication
    from ai_smm.queue import record_audit

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
        idempotency_key="other-project:threads:41:fixture",
    )
    session.add(foreign)
    session.flush()

    record_audit(
        session,
        actor="worker:other",
        action="cancelled",
        subject=f"publication:{foreign.id}",
        details={"note": "theirs"},
    )
    record_audit(
        session,
        actor="worker:other",
        action="cancelled",
        subject=f"project:{other}",
        details={"note": "theirs too"},
    )
    session.commit()

    login(member(MembershipRole.ADMIN))

    body = client.get(f"/api/v1/projects/{project}/audit").json()

    assert body["total"] == 0
    assert body["items"] == []


def test_audit_includes_project_level_entries(
    client, login, member, project: str
) -> None:
    csrf = login(member(MembershipRole.ADMIN))

    client.patch(
        f"/api/v1/projects/{project}/settings",
        json={"expected_version": 0, "content_config": {"tone": "dry"}},
        headers={"X-CSRF-Token": csrf},
    )

    body = client.get(f"/api/v1/projects/{project}/audit").json()
    actions = [entry["action"] for entry in body["items"]]

    assert "project.settings.update" in actions


def test_audit_is_paginated(
    client, session: Session, login, member, project: str, make_publication
) -> None:
    csrf = login(member(MembershipRole.ADMIN))
    headers = {"X-CSRF-Token": csrf}

    for _ in range(3):
        publication = make_publication(status=PublicationStatus.SCHEDULED)
        client.post(
            f"/api/v1/publications/{publication.id}/cancel",
            json={},
            headers=headers,
        )

    page = client.get(
        f"/api/v1/projects/{project}/audit?limit=2"
    ).json()

    assert page["total"] >= 3
    assert len(page["items"]) == 2
