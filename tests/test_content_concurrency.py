"""Editorial commands under real contention. Nothing about locking is mocked.

Every participant has its own connection to the same PostgreSQL, the row
locks are the database's, and where an interleaving matters a thread is
made to genuinely block on a lock another transaction holds.
"""
from __future__ import annotations

import contextlib
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from ai_smm.application.content import (
    ContentVersionConflict,
    RevisionInput,
    StaleRevision,
    _lock_item,
    approve_revision,
    create_revision,
    materialize_approved_revision,
    publication_link,
    submit_for_review,
)
from ai_smm.db.models import (
    ContentApproval,
    ContentItem,
    ContentPublicationLink,
    ContentRevision,
    ContentStatus,
    Publication,
)


def _revision(body: str) -> RevisionInput:
    return RevisionInput(body=body, format="text")


def _wait_until_blocked(future, seconds: float = 0.5) -> None:
    """Give a thread time to reach a lock; it must still be waiting."""

    with contextlib.suppress(Exception):
        future.result(timeout=seconds)

    assert not future.done(), (
        "the command did not block on the row lock, so it never "
        "contended for it"
    )


def _approve_in(
    session_factory: sessionmaker[Session], item: ContentItem, user
) -> None:
    db = session_factory()

    try:
        submit_for_review(
            db,
            content_item_id=item.id,
            expected_project_id=item.project_id,
            revision_id=item.current_revision_id,
            actor_user=user,
        )
        approve_revision(
            db,
            content_item_id=item.id,
            expected_project_id=item.project_id,
            revision_id=item.current_revision_id,
            actor_user=user,
        )
        db.commit()
    finally:
        db.close()


# -- revision creation ---------------------------------------------------


def test_two_writers_with_the_same_version_produce_one_revision(
    session_factory: sessionmaker[Session],
    session: Session,
    make_content,
    make_user,
) -> None:
    """Both read version 1. Exactly one wins; the other gets a conflict."""

    item, _ = make_content()
    user = make_user()
    holder = session_factory()

    def write(body: str):
        db = session_factory()

        try:
            create_revision(
                db,
                content_item_id=item.id,
                expected_project_id=item.project_id,
                expected_item_version=1,
                revision=_revision(body),
                created_by_user=user,
            )
            db.commit()

            return "created"
        except ContentVersionConflict as exc:
            db.rollback()

            return exc
        finally:
            db.close()

    try:
        # Hold the item so both writers queue up behind the same lock.
        _lock_item(
            holder,
            content_item_id=item.id,
            expected_project_id=item.project_id,
        )

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(write, "A")
            second = pool.submit(write, "B")

            _wait_until_blocked(first)
            _wait_until_blocked(second)

            holder.rollback()

            outcomes = [first.result(timeout=10), second.result(timeout=10)]
    finally:
        holder.close()

    assert outcomes.count("created") == 1

    conflict = next(o for o in outcomes if o != "created")

    assert isinstance(conflict, ContentVersionConflict)
    assert conflict.current == 2

    session.expire_all()
    numbers = list(
        session.scalars(
            select(ContentRevision.revision_number)
            .where(ContentRevision.content_item_id == item.id)
            .order_by(ContentRevision.revision_number)
        )
    )

    assert numbers == [1, 2]
    assert session.get(ContentItem, item.id).version == 2


def test_many_sequential_writers_number_revisions_without_gaps(
    session_factory: sessionmaker[Session],
    session: Session,
    make_content,
    make_user,
) -> None:
    """Writers that retry on conflict all land, numbered 2..N+1."""

    item, _ = make_content()
    user = make_user()
    barrier = threading.Barrier(5)

    def write_with_retry(body: str) -> int:
        barrier.wait()

        for _ in range(20):
            db = session_factory()

            try:
                version = db.scalar(
                    select(ContentItem.version).where(ContentItem.id == item.id)
                )
                _, revision = create_revision(
                    db,
                    content_item_id=item.id,
                    expected_project_id=item.project_id,
                    expected_item_version=version,
                    revision=_revision(body),
                    created_by_user=user,
                )
                db.commit()

                return revision.revision_number
            except ContentVersionConflict:
                db.rollback()
            finally:
                db.close()

        raise AssertionError("writer never got a turn")

    with ThreadPoolExecutor(max_workers=5) as pool:
        numbers = sorted(
            pool.map(write_with_retry, [f"w{i}" for i in range(5)])
        )

    assert numbers == [2, 3, 4, 5, 6]

    session.expire_all()

    assert session.get(ContentItem, item.id).version == 6


# -- approval races ------------------------------------------------------


def test_an_approval_waiting_on_a_new_revision_is_refused_as_stale(
    session_factory: sessionmaker[Session],
    session: Session,
    make_content,
    make_user,
) -> None:
    """The reviewer read revision 1 and clicks approve while an editor
    is saving revision 2. The approval queues on the lock, the revision
    commits, and the approval must not land on text nobody reviewed."""

    item, first = make_content()
    user = make_user()

    submitter = session_factory()
    submit_for_review(
        submitter,
        content_item_id=item.id,
        expected_project_id=item.project_id,
        revision_id=first.id,
        actor_user=user,
    )
    submitter.commit()
    submitter.close()

    writer = session_factory()

    def approve():
        db = session_factory()

        try:
            approve_revision(
                db,
                content_item_id=item.id,
                expected_project_id=item.project_id,
                revision_id=first.id,
                actor_user=user,
            )
            db.commit()

            return "approved"
        except StaleRevision as exc:
            db.rollback()

            return exc
        finally:
            db.close()

    try:
        create_revision(
            writer,
            content_item_id=item.id,
            expected_project_id=item.project_id,
            expected_item_version=2,
            revision=_revision("revision two"),
            created_by_user=user,
        )  # holds the item lock, uncommitted

        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(approve)
            _wait_until_blocked(pending)

            writer.commit()

            outcome = pending.result(timeout=10)
    finally:
        writer.close()

    assert isinstance(outcome, StaleRevision)
    assert outcome.requested == first.id

    session.expire_all()
    stored = session.get(ContentItem, item.id)

    assert stored.status is ContentStatus.DRAFT
    assert stored.approved_revision_id is None
    assert session.scalars(select(ContentApproval)).all() == []


def test_a_stale_snapshot_cannot_approve_an_old_revision(
    session_factory: sessionmaker[Session],
    session: Session,
    make_content,
    make_user,
) -> None:
    """The approve session already holds the item in its identity map.

    Another connection then creates a revision. Without populate_existing
    the approval would check against the stale current_revision_id and
    succeed; with it, the approval sees the new revision and refuses.
    """

    item, first = make_content()
    user = make_user()

    prep = session_factory()
    submit_for_review(
        prep,
        content_item_id=item.id,
        expected_project_id=item.project_id,
        revision_id=first.id,
        actor_user=user,
    )
    prep.commit()
    prep.close()

    reviewer = session_factory()

    try:
        # What the authorisation dependency does: load the row.
        snapshot = reviewer.get(ContentItem, item.id)

        assert snapshot.current_revision_id == first.id

        other = session_factory()
        create_revision(
            other,
            content_item_id=item.id,
            expected_project_id=item.project_id,
            expected_item_version=2,
            revision=_revision("written meanwhile"),
            created_by_user=user,
        )
        other.commit()
        other.close()

        # The snapshot still names revision 1...
        assert snapshot.current_revision_id == first.id

        # ...but the command reads the row under its lock.
        with pytest.raises(StaleRevision):
            approve_revision(
                reviewer,
                content_item_id=item.id,
                expected_project_id=item.project_id,
                revision_id=first.id,
                actor_user=user,
            )

        reviewer.rollback()
    finally:
        reviewer.close()

    session.expire_all()

    assert session.scalars(select(ContentApproval)).all() == []


# -- materialisation races -----------------------------------------------


def test_concurrent_materialisations_of_one_item_make_one_publication(
    session_factory: sessionmaker[Session],
    session: Session,
    make_content,
    make_user,
) -> None:
    item, _ = make_content()
    user = make_user()
    _approve_in(session_factory, item, user)

    barrier = threading.Barrier(4)

    def materialize() -> tuple[str, int]:
        barrier.wait()
        db = session_factory()

        try:
            result = materialize_approved_revision(
                db,
                content_item_id=item.id,
                expected_project_id=item.project_id,
                actor_user=user,
            )
            db.commit()

            return result.result, result.publication.id
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(lambda _: materialize(), range(4)))

    results = sorted(result for result, _ in outcomes)

    assert results == ["created", "unchanged", "unchanged", "unchanged"]
    assert len({publication_id for _, publication_id in outcomes}) == 1

    session.expire_all()
    count = session.scalar(
        select(func.count())
        .select_from(Publication)
        .where(Publication.source_ref == publication_link(item.id))
    )

    assert count == 1


def test_concurrent_materialisations_in_one_project_get_distinct_ordinals(
    session_factory: sessionmaker[Session],
    session: Session,
    make_content,
    make_user,
    make_publication,
) -> None:
    """Different items lock different rows; the ordinal must not collide."""

    make_publication()  # an existing legacy row at ordinal 1
    user = make_user()
    items = [make_content()[0] for _ in range(4)]

    for item in items:
        _approve_in(session_factory, item, user)

    barrier = threading.Barrier(len(items))

    def materialize(item: ContentItem) -> int:
        barrier.wait()
        db = session_factory()

        try:
            result = materialize_approved_revision(
                db,
                content_item_id=item.id,
                expected_project_id=item.project_id,
                actor_user=user,
            )
            db.commit()

            return result.publication.ordinal
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=len(items)) as pool:
        ordinals = sorted(pool.map(materialize, items))

    assert ordinals == [2, 3, 4, 5]


# -- the earlier snapshot, contended -------------------------------------


def _materialised_then_revised(session_factory, make_content, make_user):
    """Item with Publication A from revision 1, and revision 2 approved."""

    item, _ = make_content(body="revision one")
    user = make_user()
    _approve_in(session_factory, item, user)

    db = session_factory()

    try:
        first = materialize_approved_revision(
            db,
            content_item_id=item.id,
            expected_project_id=item.project_id,
            actor_user=user,
        )
        db.commit()
        publication_a = first.publication.id

        create_revision(
            db,
            content_item_id=item.id,
            expected_project_id=item.project_id,
            expected_item_version=db.get(ContentItem, item.id).version,
            revision=_revision("revision two"),
            created_by_user=user,
        )
        db.commit()
    finally:
        db.close()

    db = session_factory()

    try:
        _approve_in(session_factory, db.get(ContentItem, item.id), user)
    finally:
        db.close()

    return item, user, publication_a


def _timed_materialise(session_factory, item, user):
    """Run materialise on its own connection; report outcome and duration."""

    import time

    from ai_smm.application.content import PublicationNotEditable

    started = time.monotonic()
    db = session_factory()

    try:
        result = materialize_approved_revision(
            db,
            content_item_id=item.id,
            expected_project_id=item.project_id,
            actor_user=user,
        )
        db.commit()
        outcome = result.result
    except PublicationNotEditable as exc:
        db.rollback()
        outcome = exc
    finally:
        db.close()

    return outcome, time.monotonic() - started


def test_an_in_flight_cancel_neither_blocks_nor_unblocks_materialisation(
    session_factory: sessionmaker[Session],
    session: Session,
    make_content,
    make_user,
) -> None:
    """Materialisation decides on committed state and never locks A.

    An operator is cancelling A in a transaction that has not committed
    yet, and holds A's row lock. Materialising revision 2 must neither
    wait for that lock (it never takes it) nor act on the uncommitted
    cancel: A is still approved as far as anyone can see, so the answer
    is 409. Once the cancel commits, revision 2 materialises.
    """

    from ai_smm.application.content import PublicationNotEditable
    from ai_smm.queue import cancel

    item, user, publication_a = _materialised_then_revised(
        session_factory, make_content, make_user
    )
    canceller = session_factory()

    try:
        cancel(
            canceller,
            canceller.get(Publication, publication_a),
            actor="test:operator",
        )
        canceller.flush()  # A is now row-locked by the uncommitted cancel

        with ThreadPoolExecutor(max_workers=1) as pool:
            outcome, seconds = pool.submit(
                _timed_materialise, session_factory, item, user
            ).result(timeout=10)

        assert isinstance(outcome, PublicationNotEditable)
        assert outcome.publication_id == publication_a
        # Well under the 3 s lock_timeout: it never queued on A's lock.
        assert seconds < 1.5

        canceller.commit()
    finally:
        canceller.close()

    outcome, _ = _timed_materialise(session_factory, item, user)

    assert outcome == "created"

    session.expire_all()
    a = session.get(Publication, publication_a)

    assert a.status.value == "cancelled"
    assert a.body == "revision one"


def test_scheduling_the_earlier_snapshot_races_safely_with_materialisation(
    session_factory: sessionmaker[Session],
    session: Session,
    make_content,
    make_user,
) -> None:
    """The review's race, with both sides genuinely concurrent.

    An admin is scheduling A (the 022B command holds A's row lock, not yet
    committed) while an editor materialises the newer revision. The
    editor is refused without waiting, nothing is written to A by the
    editorial side, and when the admin commits, A is scheduled with
    exactly the text it had when the admin started.
    """

    from datetime import datetime, timedelta, timezone

    from ai_smm.application.content import PublicationNotEditable
    from ai_smm.application.publications import schedule_publication

    item, user, publication_a = _materialised_then_revised(
        session_factory, make_content, make_user
    )
    admin = session_factory()

    try:
        schedule_publication(
            admin,
            publication_id=publication_a,
            expected_project_id=item.project_id,
            scheduled_at=datetime.now(timezone.utc) + timedelta(hours=1),
            actor="user:admin",
        )
        admin.flush()

        with ThreadPoolExecutor(max_workers=1) as pool:
            outcome, seconds = pool.submit(
                _timed_materialise, session_factory, item, user
            ).result(timeout=10)

        assert isinstance(outcome, PublicationNotEditable)
        assert seconds < 1.5

        admin.commit()
    finally:
        admin.close()

    session.expire_all()
    a = session.get(Publication, publication_a)

    assert a.status.value == "scheduled"
    assert a.body == "revision one"

    linked = session.scalars(
        select(Publication)
        .join(
            ContentPublicationLink,
            ContentPublicationLink.publication_id == Publication.id,
        )
        .where(ContentPublicationLink.content_item_id == item.id)
    ).all()

    assert [p.id for p in linked] == [publication_a]


def test_concurrent_materialisations_after_a_cancel_make_one_new_snapshot(
    session_factory: sessionmaker[Session],
    session: Session,
    make_content,
    make_user,
) -> None:
    from ai_smm.queue import cancel

    item, user, publication_a = _materialised_then_revised(
        session_factory, make_content, make_user
    )
    cancel(session, session.get(Publication, publication_a), actor="test")
    session.commit()

    barrier = threading.Barrier(4)

    def materialise():
        barrier.wait()

        return _timed_materialise(session_factory, item, user)[0]

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = sorted(pool.map(lambda _: materialise(), range(4)))

    assert outcomes == ["created", "unchanged", "unchanged", "unchanged"]

    session.expire_all()
    links = session.scalars(
        select(ContentPublicationLink).where(
            ContentPublicationLink.content_item_id == item.id
        )
    ).all()

    # A's historical link and exactly one new link for revision 2.
    assert len(links) == 2
    assert len({link.publication_id for link in links}) == 2
