"""Content, revisions, human approval, and the bridge into delivery.

The pipeline this module implements:

    ContentItem -> ContentRevision -> human approval of that exact
    revision -> explicit materialisation into a Publication, recorded in
    content_publication_links

and it stops there. Scheduling is ai_smm.application.publications (admin
and above); delivery is the worker. Nothing here schedules, and nothing
here can reach the publishing layer except the pure content validator.

Three rules carry the design.

**A revision is immutable.** Every change of content is a new row; a
trigger in the database rejects UPDATE and DELETE on content_revisions,
so this holds for every client, not only for this code.

**An approval belongs to one exact revision.** Approving names the
revision the reviewer read; if that is no longer the current one, the
request is refused as stale. Creating a revision returns the item to
draft and clears the approval, and a check constraint makes it
impossible for an item to be approved with any revision but its current
one. An approval of revision N therefore never covers revision N+1.

**Only a human approves.** approve_revision and reject_revision take a
User, and content_approvals.actor_user_id is a NOT NULL reference to
users. An agent may author a revision (source = copywriter and so on,
created_by_user_id NULL), but there is no way to store an approval
without an authenticated person behind it.

Every command that changes an item first locks its row FOR UPDATE with
populate_existing, for the reason documented in
ai_smm.application.publications: the authorisation dependency has
already loaded a snapshot, and acting on a snapshot is how two
concurrent requests overwrite each other.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from ai_smm.application.publications import Page
from ai_smm.db.models import (
    CONTENT_FORMATS,
    ApprovalDecision,
    ContentApproval,
    ContentItem,
    ContentPublicationLink,
    ContentRevision,
    ContentStatus,
    Project,
    Publication,
    PublicationStatus,
    RevisionSource,
    User,
)
from ai_smm.publishing.service import PublishBlocked, validate_ready_to_publish
from ai_smm.queue import record_audit


#: Bound on how long a command waits for an item's row lock. Editorial
#: transactions are short, so this is normally microseconds; the bound
#: keeps an HTTP request from hanging on a stalled transaction.
LOCK_TIMEOUT_MS = 3000

#: Delivery states in which an existing, linked Publication may be
#: rewritten with a newer approved revision. Anything else has started
#: delivery, may have reached the platform, or is terminal, and is never
#: touched -- the refusal is a 409, which is the safe direction to fail.
SAFELY_EDITABLE_PUBLICATION_STATUSES = frozenset(
    {PublicationStatus.DRAFT, PublicationStatus.APPROVED}
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# -- errors --------------------------------------------------------------


class ContentUnavailable(Exception):
    """The item (or revision) is gone, or is not in the authorised project."""


class ContentLocked(Exception):
    """The item's row lock could not be taken in time."""


class ContentVersionConflict(Exception):
    def __init__(self, *, expected: int, current: int) -> None:
        super().__init__(
            "The content item was changed by someone else; re-read it "
            "and apply the change again."
        )

        self.expected = expected
        self.current = current


class InvalidContentTransition(Exception):
    def __init__(
        self,
        *,
        command: str,
        status: ContentStatus,
        allowed: tuple[ContentStatus, ...],
        hint: str = "",
    ) -> None:
        message = (
            f"Cannot {command} a content item with status {status.value}."
        )

        if hint:
            message = f"{message} {hint}"

        super().__init__(message)

        self.command = command
        self.status = status
        self.allowed = allowed


class StaleRevision(Exception):
    """The caller acted on a revision that is no longer the current one."""

    def __init__(
        self, *, requested: uuid.UUID, current: uuid.UUID
    ) -> None:
        super().__init__(
            f"Revision {requested} is not the current revision of this "
            f"content item (current is {current}); review the current "
            "revision instead."
        )

        self.requested = requested
        self.current = current


class NotDeliverable(Exception):
    """The approved content would be refused by the delivery preflight."""


class PublicationNotEditable(Exception):
    """A linked Publication has started delivery and must not be changed."""

    def __init__(
        self, *, publication_id: int, status: PublicationStatus, reason: str
    ) -> None:
        super().__init__(reason)

        self.publication_id = publication_id
        self.status = status


# -- input ---------------------------------------------------------------


@dataclass(frozen=True)
class RevisionInput:
    """The content of one revision, as a caller supplies it.

    source and source_ref are provenance. The API sets source=human for a
    person; an agent pipeline can create a draft revision with its own
    source and no user. Neither grants anything: approval is a separate,
    human-only act.
    """

    body: str
    format: str
    title: str | None = None
    images: list[dict[str, Any]] = field(default_factory=list)
    items: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    source: RevisionSource = RevisionSource.HUMAN
    source_ref: str | None = None
    editor_score: float | None = None
    editor_notes: str = ""


def content_hash(
    *,
    title: str | None,
    body: str,
    format: str,
    images: list[Any],
    items: list[Any],
) -> str:
    """SHA-256 of the deliverable snapshot, in a canonical JSON form.

    Covers exactly what would be published. metadata, source and the
    editor's notes are about the revision, not part of the post, and are
    left out so that two revisions which would publish the same thing
    have the same hash.
    """

    canonical = json.dumps(
        {
            "title": title,
            "body": body,
            "format": format,
            "images": images,
            "items": items,
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )

    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_input(revision: RevisionInput) -> None:
    if revision.format not in CONTENT_FORMATS:
        raise ValueError(
            f"format must be one of {', '.join(CONTENT_FORMATS)}."
        )

    if not revision.body.strip():
        raise ValueError("body must not be empty.")


def _build_revision(
    *,
    item_id: uuid.UUID,
    number: int,
    revision: RevisionInput,
    created_by_user: User | None,
    revision_id: uuid.UUID | None = None,
    now: datetime,
) -> ContentRevision:
    return ContentRevision(
        id=revision_id or uuid.uuid4(),
        content_item_id=item_id,
        revision_number=number,
        title=revision.title,
        body=revision.body,
        format=revision.format,
        images=list(revision.images),
        items=list(revision.items),
        meta=dict(revision.metadata),
        source=revision.source,
        source_ref=revision.source_ref,
        editor_score=revision.editor_score,
        editor_notes=revision.editor_notes,
        content_hash=content_hash(
            title=revision.title,
            body=revision.body,
            format=revision.format,
            images=list(revision.images),
            items=list(revision.items),
        ),
        created_by_user_id=created_by_user.id if created_by_user else None,
        created_at=now,
    )


def _actor(user: User | None, revision: RevisionInput | None = None) -> str:
    if user is not None:
        return f"user:{user.id}"

    # An agent writing a draft: attributed to its role, never to a person.
    source = revision.source.value if revision else "unknown"

    return f"agent:{source}"


def _require_human(user: User | None, *, command: str) -> User:
    """Approval and rejection need a signed-in, active person."""

    if user is None or not user.is_active:
        raise PermissionError(
            f"Only an authenticated human may {command} content."
        )

    return user


# -- locking -------------------------------------------------------------


def _lock_item(
    db: Session, *, content_item_id: uuid.UUID, expected_project_id: str
) -> ContentItem:
    """Re-read the item under a row lock and return its real state.

    FOR UPDATE serialises every editorial change to one item, so two
    revisions cannot be numbered alike and an approval cannot interleave
    with a new revision. populate_existing replaces the snapshot the
    authorisation dependency left in the identity map; without it the
    checks below would run against stale values. The project on the
    freshly read row is compared with the one authorisation approved.
    """

    db.execute(text(f"SET LOCAL lock_timeout = '{int(LOCK_TIMEOUT_MS)}ms'"))

    try:
        item = db.scalars(
            select(ContentItem)
            .where(ContentItem.id == content_item_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one_or_none()
    except OperationalError as exc:
        raise ContentLocked(
            f"Content item {content_item_id} is locked by another "
            "transaction; try again."
        ) from exc

    if item is None or item.project_id != expected_project_id:
        raise ContentUnavailable("Content item not found.")

    return item


def _check_version(item: ContentItem, expected: int | None) -> None:
    if expected is not None and item.version != expected:
        raise ContentVersionConflict(expected=expected, current=item.version)


def _require_not_archived(item: ContentItem, *, command: str) -> None:
    if item.status is ContentStatus.ARCHIVED:
        raise InvalidContentTransition(
            command=command,
            status=item.status,
            allowed=(),
            hint="Archived items are read-only.",
        )


def _bump(item: ContentItem, now: datetime) -> None:
    item.version += 1
    item.updated_at = now


# -- commands ------------------------------------------------------------


def create_content_item(
    db: Session,
    *,
    project_id: str,
    title: str,
    content_type: str,
    revision: RevisionInput,
    created_by_user: User | None,
    now: datetime | None = None,
) -> tuple[ContentItem, ContentRevision]:
    """Create an item together with its first revision, in draft.

    The two rows refer to each other. The revision id is generated here
    so the item can be inserted pointing at it; the foreign key from item
    to revision is deferred to commit, by which point both exist.
    """

    _validate_input(revision)

    if not title.strip():
        raise ValueError("title must not be empty.")

    now = now or utcnow()
    item_id = uuid.uuid4()
    revision_id = uuid.uuid4()

    item = ContentItem(
        id=item_id,
        project_id=project_id,
        title=title,
        content_type=content_type,
        status=ContentStatus.DRAFT,
        current_revision_id=revision_id,
        approved_revision_id=None,
        version=1,
        created_by_user_id=created_by_user.id if created_by_user else None,
        created_at=now,
        updated_at=now,
    )
    db.add(item)
    db.flush()

    first = _build_revision(
        item_id=item_id,
        number=1,
        revision=revision,
        created_by_user=created_by_user,
        revision_id=revision_id,
        now=now,
    )
    db.add(first)
    db.flush()

    actor = _actor(created_by_user, revision)

    record_audit(
        db,
        actor=actor,
        action="content.created",
        subject=f"content_item:{item.id}",
        details={
            "project_id": project_id,
            "content_type": content_type,
            "revision_id": str(first.id),
        },
    )
    record_audit(
        db,
        actor=actor,
        action="content.revision_created",
        subject=f"content_item:{item.id}",
        details=_revision_details(first),
    )

    return item, first


def create_revision(
    db: Session,
    *,
    content_item_id: uuid.UUID,
    expected_project_id: str,
    expected_item_version: int,
    revision: RevisionInput,
    created_by_user: User | None,
    now: datetime | None = None,
) -> tuple[ContentItem, ContentRevision]:
    """Append a revision. The item returns to draft and loses its approval.

    1. lock the item and re-read it;
    2. refuse a stale expected_item_version (409);
    3. number the revision max + 1 -- safe because the lock serialises
       every writer of this item, with the unique constraint on
       (content_item_id, revision_number) as the backstop;
    4. insert it, point the item at it, bump the version, return to
       draft and clear approved_revision_id, so an approval of the
       previous revision is no longer in effect;
    5. audit.
    """

    _validate_input(revision)

    now = now or utcnow()

    item = _lock_item(
        db,
        content_item_id=content_item_id,
        expected_project_id=expected_project_id,
    )

    _require_not_archived(item, command="revise")
    _check_version(item, expected_item_version)

    number = (
        db.execute(
            select(func.coalesce(func.max(ContentRevision.revision_number), 0))
            .where(ContentRevision.content_item_id == item.id)
        ).scalar_one()
        + 1
    )

    new_revision = _build_revision(
        item_id=item.id,
        number=number,
        revision=revision,
        created_by_user=created_by_user,
        now=now,
    )
    db.add(new_revision)
    db.flush()

    previous_status = item.status

    item.current_revision_id = new_revision.id
    item.approved_revision_id = None
    item.status = ContentStatus.DRAFT
    _bump(item, now)

    db.flush()

    record_audit(
        db,
        actor=_actor(created_by_user, revision),
        action="content.revision_created",
        subject=f"content_item:{item.id}",
        details={
            **_revision_details(new_revision),
            "previous_status": previous_status.value,
            "item_version": item.version,
        },
    )

    return item, new_revision


def _resolve_current_revision(
    db: Session, *, item: ContentItem, revision_id: uuid.UUID
) -> ContentRevision:
    """The named revision, which must belong to this item and be current."""

    revision = db.get(ContentRevision, revision_id)

    if revision is None or revision.content_item_id != item.id:
        # Never confirm a revision of another item exists.
        raise ContentUnavailable("Revision not found for this content item.")

    if revision.id != item.current_revision_id:
        raise StaleRevision(
            requested=revision.id, current=item.current_revision_id
        )

    return revision


def submit_for_review(
    db: Session,
    *,
    content_item_id: uuid.UUID,
    expected_project_id: str,
    revision_id: uuid.UUID,
    actor_user: User,
    expected_item_version: int | None = None,
    note: str = "",
    now: datetime | None = None,
) -> ContentItem:
    """draft -> in_review, for the exact revision the submitter looked at."""

    now = now or utcnow()

    item = _lock_item(
        db,
        content_item_id=content_item_id,
        expected_project_id=expected_project_id,
    )

    _require_not_archived(item, command="submit")
    _check_version(item, expected_item_version)
    revision = _resolve_current_revision(db, item=item, revision_id=revision_id)

    if item.status is not ContentStatus.DRAFT:
        raise InvalidContentTransition(
            command="submit",
            status=item.status,
            allowed=(ContentStatus.DRAFT,),
            hint=(
                "A rejected or approved revision is not resubmitted; "
                "create a new revision instead."
                if item.status
                in (ContentStatus.REJECTED, ContentStatus.APPROVED)
                else ""
            ),
        )

    item.status = ContentStatus.IN_REVIEW
    _bump(item, now)

    record_audit(
        db,
        actor=_actor(actor_user),
        action="content.submitted_for_review",
        subject=f"content_item:{item.id}",
        details={
            "revision_id": str(revision.id),
            "revision_number": revision.revision_number,
            "item_version": item.version,
            "note": note,
        },
    )

    return item


def _decide(
    db: Session,
    *,
    decision: ApprovalDecision,
    content_item_id: uuid.UUID,
    expected_project_id: str,
    revision_id: uuid.UUID,
    actor_user: User | None,
    expected_item_version: int | None,
    note: str,
    now: datetime | None,
) -> tuple[ContentItem, ContentApproval]:
    command = "approve" if decision is ApprovalDecision.APPROVED else "reject"
    human = _require_human(actor_user, command=command)
    now = now or utcnow()

    # 1-3: lock and reload.
    item = _lock_item(
        db,
        content_item_id=content_item_id,
        expected_project_id=expected_project_id,
    )

    _require_not_archived(item, command=command)
    _check_version(item, expected_item_version)

    # 4-5: the revision belongs to this item and is the current one.
    revision = _resolve_current_revision(db, item=item, revision_id=revision_id)

    # 6: only an item under review is decided.
    if item.status is not ContentStatus.IN_REVIEW:
        raise InvalidContentTransition(
            command=command,
            status=item.status,
            allowed=(ContentStatus.IN_REVIEW,),
            hint="Submit the current revision for review first."
            if item.status is ContentStatus.DRAFT
            else "",
        )

    # 7: append the decision. Never updated, never deleted.
    approval = ContentApproval(
        id=uuid.uuid4(),
        content_item_id=item.id,
        revision_id=revision.id,
        decision=decision,
        actor_user_id=human.id,
        note=note,
        created_at=now,
    )
    db.add(approval)

    # 8-9: the item now points at exactly this revision, or at none.
    if decision is ApprovalDecision.APPROVED:
        item.approved_revision_id = revision.id
        item.status = ContentStatus.APPROVED
    else:
        item.approved_revision_id = None
        item.status = ContentStatus.REJECTED

    _bump(item, now)
    db.flush()

    # 10: audit.
    record_audit(
        db,
        actor=_actor(human),
        action=(
            "content.approved"
            if decision is ApprovalDecision.APPROVED
            else "content.rejected"
        ),
        subject=f"content_item:{item.id}",
        details={
            "approval_id": str(approval.id),
            "revision_id": str(revision.id),
            "revision_number": revision.revision_number,
            "content_hash": revision.content_hash,
            "item_version": item.version,
            "note": note,
        },
    )

    return item, approval


def approve_revision(
    db: Session,
    *,
    content_item_id: uuid.UUID,
    expected_project_id: str,
    revision_id: uuid.UUID,
    actor_user: User,
    expected_item_version: int | None = None,
    note: str = "",
    now: datetime | None = None,
) -> tuple[ContentItem, ContentApproval]:
    """in_review -> approved, for exactly this revision, by a human."""

    return _decide(
        db,
        decision=ApprovalDecision.APPROVED,
        content_item_id=content_item_id,
        expected_project_id=expected_project_id,
        revision_id=revision_id,
        actor_user=actor_user,
        expected_item_version=expected_item_version,
        note=note,
        now=now,
    )


def reject_revision(
    db: Session,
    *,
    content_item_id: uuid.UUID,
    expected_project_id: str,
    revision_id: uuid.UUID,
    actor_user: User,
    expected_item_version: int | None = None,
    note: str = "",
    now: datetime | None = None,
) -> tuple[ContentItem, ContentApproval]:
    """in_review -> rejected. The next step is a new revision."""

    return _decide(
        db,
        decision=ApprovalDecision.REJECTED,
        content_item_id=content_item_id,
        expected_project_id=expected_project_id,
        revision_id=revision_id,
        actor_user=actor_user,
        expected_item_version=expected_item_version,
        note=note,
        now=now,
    )


# -- the bridge into delivery --------------------------------------------


@dataclass(frozen=True)
class MaterializeResult:
    #: created | updated | unchanged
    result: str
    publication: Publication
    revision: ContentRevision
    platform: str


def publication_key(
    *, content_item_id: uuid.UUID, revision_id: uuid.UUID, platform: str
) -> str:
    """The idempotency key of a materialised Publication.

    A secondary barrier, not the mapping: content_publication_links is
    authoritative. publications.idempotency_key is UNIQUE, so even if the
    link table were bypassed the database would refuse a second
    Publication for the same item, revision and platform. The importer's
    keys are `project:platform:ordinal:digest`; a key whose second
    segment is a UUID cannot collide with one of those.
    """

    return f"content:{content_item_id}:{revision_id}:{platform}"


def publication_link(content_item_id: uuid.UUID) -> str:
    """publications.source_ref of every Publication made from this item.

    A human-readable trace only. Nothing reads it to decide anything; the
    lineage is content_publication_links.
    """

    return f"content_item:{content_item_id}"


def _latest_decision(
    db: Session, *, item: ContentItem, revision_id: uuid.UUID
) -> ContentApproval | None:
    return db.scalars(
        select(ContentApproval)
        .where(
            ContentApproval.content_item_id == item.id,
            ContentApproval.revision_id == revision_id,
        )
        .order_by(ContentApproval.created_at.desc(), ContentApproval.id)
        .limit(1)
    ).one_or_none()


def _is_safely_editable(publication: Publication) -> bool:
    """Not started, not attempted, not live, not held by a worker."""

    return (
        publication.status in SAFELY_EDITABLE_PUBLICATION_STATUSES
        and publication.threads_post_id is None
        and publication.published_at is None
        and publication.attempt_count == 0
        and publication.claimed_by is None
    )


def _require_editable(publication: Publication) -> None:
    if _is_safely_editable(publication):
        return

    raise PublicationNotEditable(
        publication_id=publication.id,
        status=publication.status,
        reason=(
            f"Publication {publication.id} built from an earlier revision "
            f"is {publication.status.value} and may not be rewritten. "
            "Cancel it first if the new revision should replace it."
        ),
    )


def _lock_linked_publication(
    db: Session, *, publication_id: int
) -> Publication:
    """Lock the one Publication about to be rewritten, and re-read it."""

    try:
        publication = db.scalars(
            select(Publication)
            .where(Publication.id == publication_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one_or_none()
    except OperationalError as exc:
        raise ContentLocked(
            f"Publication {publication_id} is locked by another "
            "transaction; try again."
        ) from exc

    if publication is None:
        raise ContentUnavailable("Linked publication disappeared.")

    return publication


def _copy_snapshot(
    publication: Publication,
    *,
    item: ContentItem,
    revision: ContentRevision,
    key: str,
) -> None:
    publication.title = revision.title or item.title
    publication.body = revision.body
    publication.format = revision.format
    publication.images = list(revision.images or [])
    publication.items = list(revision.items or [])
    publication.editor_score = revision.editor_score
    publication.idempotency_key = key
    publication.source_ref = publication_link(item.id)
    # The reason this Publication may be delivered at all: a person
    # approved exactly this text. The worker still requires it.
    publication.human_reviewed = True
    publication.status = PublicationStatus.APPROVED


def materialize_approved_revision(
    db: Session,
    *,
    content_item_id: uuid.UUID,
    expected_project_id: str,
    actor_user: User,
    revision_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> MaterializeResult:
    """Turn the approved revision into a delivery Publication. Nothing more.

    The Publication is left in `approved` with human_reviewed set and no
    scheduled_at: a safe pre-delivery state. Scheduling it is a separate,
    admin-only command, and only the worker ever sends it.

    content_publication_links is the authoritative mapping. Its primary
    key is (content item, revision, platform) and publication_id is
    UNIQUE, so the database refuses both a second Publication for one
    revision and one Publication attributed to two revisions. A repeat
    finds the link and returns the same row unchanged. The deterministic
    publications.idempotency_key is kept as a second, independent
    barrier, and source_ref as a human-readable trace; neither is read to
    decide anything.

    One live Publication per (item, platform), found through the links.
    If the item already has one from an earlier revision:

    * still in draft/approved, never attempted, not claimed, no post id
      -> it is rewritten with the new snapshot ("updated"), and in the
      same transaction its link is replaced: the row for the old revision
      is deleted and one for the new revision inserted, so the database
      says unambiguously which revision the Publication now carries. The
      replaced revision is named in the audit entry;
    * anything else -> 409. A row that is scheduled, claimed, publishing,
      needs_review, published or failed has started delivery or may have
      reached the platform, and rewriting it is how one item would be
      posted twice. The operator cancels it first if they mean to.

    A cancelled Publication is history: it is never touched, its link
    stays as the lineage of the revision it carried, and it never blocks
    a new one.
    """

    human = _require_human(actor_user, command="materialize")
    now = now or utcnow()

    item = _lock_item(
        db,
        content_item_id=content_item_id,
        expected_project_id=expected_project_id,
    )

    if item.status is not ContentStatus.APPROVED:
        raise InvalidContentTransition(
            command="materialize",
            status=item.status,
            allowed=(ContentStatus.APPROVED,),
            hint="Only the approved current revision can be materialised.",
        )

    # The check constraints guarantee approved == current here.
    approved_id = item.approved_revision_id
    assert approved_id is not None

    if revision_id is not None and revision_id != approved_id:
        raise StaleRevision(requested=revision_id, current=approved_id)

    revision = db.get(ContentRevision, approved_id)

    if revision is None or revision.content_item_id != item.id:
        raise ContentUnavailable("Approved revision not found.")

    # The approval must be an actual human decision about this exact
    # revision -- not merely the item's status saying so.
    decision = _latest_decision(db, item=item, revision_id=revision.id)

    if (
        decision is None
        or decision.decision is not ApprovalDecision.APPROVED
        or decision.actor_user_id is None
    ):
        raise InvalidContentTransition(
            command="materialize",
            status=item.status,
            allowed=(ContentStatus.APPROVED,),
            hint="No human approval is recorded for the current revision.",
        )

    project = db.get(Project, item.project_id)
    assert project is not None
    platform = project.default_platform
    key = publication_key(
        content_item_id=item.id, revision_id=revision.id, platform=platform
    )

    # Run the delivery preflight on the would-be Publication before any
    # row is written. A transient object, never added to the session.
    candidate = Publication(
        project_id=item.project_id,
        platform=platform,
        ordinal=0,
        title=revision.title or item.title,
        format=revision.format,
        body=revision.body,
        images=list(revision.images or []),
        items=list(revision.items or []),
        status=PublicationStatus.APPROVED,
        human_reviewed=True,
        idempotency_key=key,
        attempt_count=0,
    )

    try:
        validate_ready_to_publish(
            candidate, require_claimed=False, require_human_review=True
        )
    except PublishBlocked as exc:
        raise NotDeliverable(str(exc)) from exc

    # Ordinals are allocated per (project, platform); items of the same
    # project are locked independently, so their materialisations are
    # serialised here instead. Transaction-scoped: released at commit.
    db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"ai-smm:publication-ordinal:{item.project_id}:{platform}"},
    )

    # 1. The exact revision was already materialised: the link says so.
    #    Read without a row lock -- a replay changes nothing, two
    #    materialisations of this item are serialised by the item lock,
    #    and the Publication may be scheduled, which a lock here would
    #    make the worker skip for the length of this request.
    link = db.get(
        ContentPublicationLink,
        (item.id, revision.id, platform),
        populate_existing=True,
    )

    if link is not None:
        linked = db.get(
            Publication, link.publication_id, populate_existing=True
        )
        assert linked is not None  # foreign key

        return MaterializeResult(
            result="unchanged",
            publication=linked,
            revision=revision,
            platform=platform,
        )

    # The secondary barrier: a Publication carrying this revision's key
    # but no link means the lineage was tampered with. Refuse rather than
    # guess which one is right.
    orphan = db.scalar(
        select(Publication.id).where(Publication.idempotency_key == key)
    )

    if orphan is not None:
        raise PublicationNotEditable(
            publication_id=orphan,
            status=db.get(Publication, orphan).status,
            reason=(
                f"Publication {orphan} carries this revision's key but has "
                "no lineage link; refusing to guess. Repair the link first."
            ),
        )

    # 2. The item's live Publication on this platform, found through the
    #    links -- never through source_ref. Legacy Publications have no
    #    link and are invisible here.
    live = list(
        db.execute(
            select(ContentPublicationLink, Publication)
            .join(
                Publication,
                Publication.id == ContentPublicationLink.publication_id,
            )
            .where(
                ContentPublicationLink.content_item_id == item.id,
                ContentPublicationLink.platform == platform,
                Publication.status != PublicationStatus.CANCELLED,
            )
            .order_by(Publication.id)
            .execution_options(populate_existing=True)
        )
    )

    if len(live) > 1:
        raise PublicationNotEditable(
            publication_id=live[0][1].id,
            status=live[0][1].status,
            reason=(
                "More than one live Publication is linked to this content "
                "item; refusing to choose one. Cancel the extra ones first."
            ),
        )

    previous_revision_id: uuid.UUID | None = None

    if live:
        old_link, candidate_publication = live[0]

        # Decide on an unlocked read first, so a row that has started
        # delivery is refused without ever being locked; then lock the
        # one row that will be written and decide again, because an
        # admin may have scheduled it in between.
        _require_editable(candidate_publication)
        publication = _lock_linked_publication(
            db, publication_id=candidate_publication.id
        )
        _require_editable(publication)

        # Replace the lineage in the same transaction as the snapshot.
        # The old row goes first: publication_id is UNIQUE, so the new
        # row cannot coexist with it even for an instant.
        previous_revision_id = old_link.revision_id
        db.execute(
            delete(ContentPublicationLink).where(
                ContentPublicationLink.publication_id == publication.id
            )
        )

        _copy_snapshot(publication, item=item, revision=revision, key=key)
        publication.updated_at = now
        result = "updated"
    else:
        ordinal = (
            db.execute(
                select(func.coalesce(func.max(Publication.ordinal), 0)).where(
                    Publication.project_id == item.project_id,
                    Publication.platform == platform,
                )
            ).scalar_one()
            + 1
        )

        publication = Publication(
            project_id=item.project_id,
            platform=platform,
            ordinal=ordinal,
            scheduled_at=None,
            attempt_count=0,
            created_at=now,
            updated_at=now,
        )
        _copy_snapshot(publication, item=item, revision=revision, key=key)
        db.add(publication)
        result = "created"

    # The Publication row must exist (and carry its new key) before the
    # link that references it.
    db.flush()

    db.add(
        ContentPublicationLink(
            content_item_id=item.id,
            revision_id=revision.id,
            platform=platform,
            publication_id=publication.id,
            created_at=now,
        )
    )

    db.flush()

    record_audit(
        db,
        actor=_actor(human),
        action="content.materialized",
        subject=f"content_item:{item.id}",
        details={
            "result": result,
            "publication_id": publication.id,
            "revision_id": str(revision.id),
            "revision_number": revision.revision_number,
            "content_hash": revision.content_hash,
            "platform": platform,
            # On "updated", the revision this Publication carried until
            # now -- the lineage row for it was replaced.
            "previous_revision_id": (
                str(previous_revision_id) if previous_revision_id else None
            ),
        },
    )

    return MaterializeResult(
        result=result,
        publication=publication,
        revision=revision,
        platform=platform,
    )


# -- reads ---------------------------------------------------------------


def _revision_details(revision: ContentRevision) -> dict[str, Any]:
    return {
        "revision_id": str(revision.id),
        "revision_number": revision.revision_number,
        "source": revision.source.value,
        "source_ref": revision.source_ref,
        "content_hash": revision.content_hash,
    }


def get_content_item(
    db: Session, *, content_item_id: uuid.UUID
) -> ContentItem | None:
    """The row, whatever project owns it; the caller authorises after."""

    return db.get(ContentItem, content_item_id)


def get_revision(
    db: Session, *, revision_id: uuid.UUID
) -> ContentRevision | None:
    return db.get(ContentRevision, revision_id)


def list_content_items_page(
    db: Session,
    *,
    project_id: str,
    statuses: list[ContentStatus] | None,
    content_type: str | None,
    limit: int,
    offset: int,
) -> Page:
    stmt = select(ContentItem).where(ContentItem.project_id == project_id)

    if statuses:
        stmt = stmt.where(ContentItem.status.in_(statuses))

    if content_type:
        stmt = stmt.where(ContentItem.content_type == content_type)

    total = int(
        db.execute(
            select(func.count()).select_from(stmt.subquery())
        ).scalar_one()
    )
    rows = list(
        db.scalars(
            stmt.order_by(ContentItem.updated_at.desc(), ContentItem.id)
            .limit(limit)
            .offset(offset)
        )
    )

    return Page(items=rows, total=total, limit=limit, offset=offset)


def list_revisions_page(
    db: Session, *, item: ContentItem, limit: int, offset: int
) -> Page:
    stmt = select(ContentRevision).where(
        ContentRevision.content_item_id == item.id
    )
    total = int(
        db.execute(
            select(func.count()).select_from(stmt.subquery())
        ).scalar_one()
    )
    rows = list(
        db.scalars(
            stmt.order_by(ContentRevision.revision_number)
            .limit(limit)
            .offset(offset)
        )
    )

    return Page(items=rows, total=total, limit=limit, offset=offset)


def list_approvals_page(
    db: Session, *, item: ContentItem, limit: int, offset: int
) -> Page:
    stmt = select(ContentApproval).where(
        ContentApproval.content_item_id == item.id
    )
    total = int(
        db.execute(
            select(func.count()).select_from(stmt.subquery())
        ).scalar_one()
    )
    rows = list(
        db.scalars(
            stmt.order_by(ContentApproval.created_at, ContentApproval.id)
            .limit(limit)
            .offset(offset)
        )
    )

    return Page(items=rows, total=total, limit=limit, offset=offset)
