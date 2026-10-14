"""The database guards of business mutations (SMM-025D, ai_smm.db.guards).

Every test here speaks SQL -- or the application's service functions -- as
the business-mutation role ai_smm_cc_rw itself, bypassing FastAPI, RBAC
and every rule the application follows. What PostgreSQL refuses here, a
compromised API connection cannot do. Fixtures seed data as the owner, as
the worker and the CLI do in production.

A "bound" transaction is one that called guard_bind_session with the raw
token of a live session: the only way a business write is accepted at
all. Grants (step-up confirmations) are issued as the owner, exactly as
smm_auth.approval_grant_issue does after smm-auth re-checked the password
(tests/test_db_auth_contracts.py tests that function).

Skipped without AI_SMM_TEST_ADMIN_DATABASE_URL, like the role itself.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from ai_smm.application.auth import bind_transaction_to_session, create_session
from ai_smm.application.content import (
    RevisionInput,
    approve_revision,
    create_content_item,
    materialize_approved_revision,
    submit_for_review,
)
from ai_smm.application.publications import schedule_publication
from ai_smm.config import Settings
from ai_smm.db.models import (
    ApprovalGrant,
    ContentApproval,
    ContentItem,
    ContentRevision,
    MembershipRole,
    MutationContext,
    Publication,
    PublicationStatus,
    SocialAccount,
    User,
    UserSession,
)
from tests.test_db_least_privilege import INSUFFICIENT_PRIVILEGE, _sqlstate


LEGACY = ("test-project",)


def _refused(raised: pytest.ExceptionInfo[DBAPIError], fragment: str = "") -> None:
    assert _sqlstate(raised.value) == INSUFFICIENT_PRIVILEGE, raised.value
    assert "guard: " in str(raised.value) or "permission denied" in str(raised.value)
    if fragment:
        assert fragment in str(raised.value), raised.value


# -- fixtures ------------------------------------------------------------------


@pytest.fixture
def rw(mutation_session_factory: sessionmaker[Session]) -> sessionmaker[Session]:
    return mutation_session_factory


@pytest.fixture
def owner(make_user, make_membership, project: str) -> User:
    user = make_user(display_name="Owner")
    make_membership(user=user, project_id=project, role=MembershipRole.OWNER)
    return user


@pytest.fixture
def token_for(session: Session, settings: Settings):
    """A live session for a user, as smm-auth opens one. Returns the raw token."""

    def factory(user: User) -> str:
        _, token = create_session(session, user=user, settings=settings)
        session.commit()
        return token

    return factory


@contextmanager
def bound(factory: sessionmaker[Session], token: str) -> Iterator[Session]:
    """A business-role session whose transaction acts for `token`'s user."""

    db = factory()
    try:
        assert bind_transaction_to_session(db, token=token) is not None
        yield db
    finally:
        db.rollback()
        db.close()


def _grant(
    session: Session,
    *,
    token: str,
    item: ContentItem,
    revision: ContentRevision,
    expires_in: timedelta = timedelta(minutes=5),
    external_account_id: str | None = "unset",
) -> ApprovalGrant:
    """What smm_auth.approval_grant_issue writes after the password check."""

    from ai_smm.security.tokens import hash_token

    user_session = session.scalars(
        select(UserSession).where(UserSession.token_hash == hash_token(token))
    ).one()
    account = (
        session.get(SocialAccount, revision.social_account_id)
        if revision.social_account_id
        else None
    )
    now = datetime.now(timezone.utc)
    grant = ApprovalGrant(
        id=uuid.uuid4(),
        user_id=user_session.user_id,
        session_id=user_session.id,
        content_item_id=item.id,
        revision_id=revision.id,
        social_account_id=revision.social_account_id,
        external_account_id=(
            (account.external_account_id if account else None)
            if external_account_id == "unset"
            else external_account_id
        ),
        content_hash=revision.content_hash,
        created_at=now - timedelta(minutes=10) if expires_in < timedelta(0) else now,
        expires_at=now + expires_in,
    )
    session.add(grant)
    session.commit()
    return grant


def _in_review(
    rw: sessionmaker[Session], token: str, owner: User, project: str, **revision
) -> tuple[ContentItem, ContentRevision]:
    """An item created and submitted by the owner, through the business role."""

    with bound(rw, token) as db:
        item, first = create_content_item(
            db,
            project_id=project,
            title="Guarded",
            content_type="post",
            revision=RevisionInput(
                body=revision.pop("body", "Текст для проверки."),
                format="text",
                **revision,
            ),
            created_by_user=owner,
        )
        submit_for_review(
            db,
            content_item_id=item.id,
            expected_project_id=project,
            revision_id=first.id,
            actor_user=owner,
            legacy_projects=LEGACY,
        )
        db.commit()
        return item, first


def _approved(rw, session, token, owner, project, **revision):
    item, first = _in_review(rw, token, owner, project, **revision)
    _grant(session, token=token, item=item, revision=first)

    with bound(rw, token) as db:
        approve_revision(
            db,
            content_item_id=item.id,
            expected_project_id=project,
            revision_id=first.id,
            actor_user=owner,
        )
        db.commit()

    return item, first


# -- the binding ---------------------------------------------------------------


def test_without_a_bound_session_every_business_write_is_refused(
    rw,
    session: Session,
    project: str,
    owner,
    make_content,
    make_account,
    make_publication,
) -> None:
    from ai_smm.db.models import ProjectSettings

    item, revision = make_content(user=owner)
    account, _ = make_account()
    publication = make_publication(status=PublicationStatus.SCHEDULED)
    session.add(ProjectSettings(project_id=project))
    session.commit()
    statements = [
        ("UPDATE projects SET display_name = 'x' WHERE id = :project", {}),
        (
            "UPDATE project_settings SET version = version WHERE project_id = :project",
            {},
        ),
        (
            "INSERT INTO content_items (id, project_id, title, content_type, status,"
            " current_revision_id, version, created_by_user_id)"
            " VALUES (gen_random_uuid(), :project, 't', 'post', 'draft',"
            " gen_random_uuid(), 1, :user)",
            {},
        ),
        ("UPDATE content_items SET status = 'approved' WHERE id = :item", {}),
        (
            "INSERT INTO content_approvals (id, content_item_id, revision_id,"
            " decision, actor_user_id) VALUES (gen_random_uuid(), :item, :revision,"
            " 'approved', :user)",
            {},
        ),
        ("UPDATE social_accounts SET display_name = 'x' WHERE id = :account", {}),
        (
            "UPDATE social_account_credentials SET status = 'revoked',"
            " revoked_at = now(),"
            " ciphertext = NULL, nonce = NULL, ephemeral_public_key = NULL"
            " WHERE social_account_id = :account",
            {},
        ),
        ("UPDATE publications SET status = 'cancelled' WHERE id = :publication", {}),
        (
            "INSERT INTO audit_log (actor, action, subject, details)"
            " VALUES ('user:x', 'content.created', 'x', '{}')",
            {},
        ),
    ]
    params = {
        "project": project,
        "user": owner.id,
        "item": item.id,
        "revision": revision.id,
        "account": account.id,
        "publication": publication.id,
    }

    for statement, _ in statements:
        with rw() as db, pytest.raises(DBAPIError, match="guard: ") as raised:
            result = db.execute(text(statement), params)
            pytest.fail(f"not refused ({result.rowcount} rows): {statement}")

        _refused(raised)


@pytest.mark.parametrize(
    "spoil",
    ["revoked", "expired", "idle", "inactive", "future", "unknown"],
)
def test_postgresql_binds_only_a_session_it_considers_live(
    rw, session: Session, owner, token_for, spoil: str
) -> None:
    from ai_smm.security.tokens import generate_token, hash_token

    token = token_for(owner)
    row = session.scalars(
        select(UserSession).where(UserSession.token_hash == hash_token(token))
    ).one()
    now = datetime.now(timezone.utc)

    if spoil == "revoked":
        row.revoked_at = now
    elif spoil == "expired":
        row.created_at = now - timedelta(days=7, hours=1)
        row.last_seen_at = now - timedelta(minutes=5)
        row.expires_at = now - timedelta(hours=1)
    elif spoil == "idle":
        row.created_at = now - timedelta(days=2)
        row.last_seen_at = now - timedelta(hours=25)
    elif spoil == "inactive":
        session.get(User, owner.id).is_active = False
    elif spoil == "future":
        # RS-3: a last_seen_at in the future would disable the idle timeout.
        row.last_seen_at = now + timedelta(hours=2)
    elif spoil == "unknown":
        token = generate_token()

    session.commit()

    with rw() as db:
        assert bind_transaction_to_session(db, token=token) is None


def test_a_malformed_or_missing_token_binds_nothing(rw) -> None:
    with rw() as db:
        assert bind_transaction_to_session(db, token="not-a-token") is None
        assert bind_transaction_to_session(db, token=None) is None


def test_a_binding_lasts_exactly_one_transaction(
    rw, session: Session, owner, token_for, project: str
) -> None:
    token = token_for(owner)

    with rw() as db:
        assert bind_transaction_to_session(db, token=token) == owner.id
        db.commit()

        # The next transaction of the same connection is not bound.
        with pytest.raises(DBAPIError) as raised:
            db.execute(
                text("UPDATE projects SET display_name = 'x' WHERE id = :p"),
                {"p": project},
            )

        _refused(raised, "session-bound transaction")


def test_one_transaction_cannot_act_for_two_users(
    rw, owner, make_user, make_membership, project: str, token_for
) -> None:
    other = make_user()
    make_membership(user=other, project_id=project, role=MembershipRole.OWNER)

    with bound(rw, token_for(owner)) as db, pytest.raises(DBAPIError) as raised:
        bind_transaction_to_session(db, token=token_for(other))

    _refused(raised, "already bound to another user")


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT * FROM mutation_contexts",
        "INSERT INTO mutation_contexts (txid, user_id, session_id)"
        " VALUES (pg_current_xact_id()::text::bigint, gen_random_uuid(),"
        " gen_random_uuid())",
        "DELETE FROM mutation_contexts",
        "SELECT * FROM approval_grants",
        "UPDATE approval_grants SET consumed_at = NULL",
        "SELECT public.guard_token_digest('x')",
    ],
)
def test_the_role_cannot_touch_the_guards_own_state(rw, statement: str) -> None:
    with rw() as db, pytest.raises(DBAPIError) as raised:
        db.execute(text(statement))

    assert _sqlstate(raised.value) == INSUFFICIENT_PRIVILEGE


# -- who may write ---------------------------------------------------------------


@pytest.mark.parametrize(
    "role", [MembershipRole.VIEWER, MembershipRole.EDITOR, MembershipRole.ADMIN]
)
def test_only_the_projects_owner_writes(
    rw, make_user, make_membership, project: str, token_for, role
) -> None:
    user = make_user()
    make_membership(user=user, project_id=project, role=role)

    with bound(rw, token_for(user)) as db, pytest.raises(DBAPIError) as raised:
        create_content_item(
            db,
            project_id=project,
            title="x",
            content_type="post",
            revision=RevisionInput(body="x", format="text"),
            created_by_user=user,
        )

    _refused(raised, "reserved to its owner")


def test_the_owner_of_another_project_cannot_write_here(
    rw, make_user, make_membership, make_other_project, project: str, token_for
) -> None:
    stranger = make_user()
    make_membership(
        user=stranger, project_id=make_other_project, role=MembershipRole.OWNER
    )

    with bound(rw, token_for(stranger)) as db, pytest.raises(DBAPIError) as raised:
        db.execute(
            text("UPDATE projects SET display_name = 'taken' WHERE id = :p"),
            {"p": project},
        )

    _refused(raised, "reserved to its owner")


def test_creator_actor_and_approver_are_always_the_bound_user(
    rw, session, owner, make_user, make_membership, project: str, token_for
) -> None:
    other = make_user()
    make_membership(user=other, project_id=project, role=MembershipRole.OWNER)
    token = token_for(owner)

    # A content item said to be created by someone else.
    with bound(rw, token) as db, pytest.raises(DBAPIError) as raised:
        create_content_item(
            db,
            project_id=project,
            title="x",
            content_type="post",
            revision=RevisionInput(body="x", format="text"),
            created_by_user=other,
        )

    _refused(raised, "the creator must be the bound user")

    # An audit entry in someone else's name, or a sign-in or worker event.
    for actor, action in (
        (f"user:{other.id}", "content.created"),
        (f"user:{owner.id}", "auth.login"),
        (f"user:{owner.id}", "published"),
    ):
        with bound(rw, token) as db, pytest.raises(DBAPIError) as raised:
            db.execute(
                text(
                    "INSERT INTO audit_log (actor, action, subject, details)"
                    " VALUES (:actor, :action, 'x', '{}')"
                ),
                {"actor": actor, "action": action},
            )

        _refused(raised)

    # An approval in someone else's name (with a grant, so only that differs).
    item, first = _in_review(rw, token, owner, project)
    _grant(session, token=token, item=item, revision=first)

    with bound(rw, token) as db, pytest.raises(DBAPIError) as raised:
        approve_revision(
            db,
            content_item_id=item.id,
            expected_project_id=project,
            revision_id=first.id,
            actor_user=other,
        )
        db.flush()

    _refused(raised, "the approver must be the bound user")


def test_audit_rows_say_which_role_wrote_them(
    rw, session, owner, project: str, token_for
) -> None:
    token = token_for(owner)
    item, _ = _in_review(rw, token, owner, project)

    from ai_smm.db.models import AuditLog

    session.expire_all()
    rows = session.scalars(
        select(AuditLog).where(AuditLog.subject == f"content_item:{item.id}")
    ).all()

    assert rows and {row.written_by for row in rows} == {"ai_smm_cc_rw"}
    assert {row.actor for row in rows} == {f"user:{owner.id}"}


# -- approval: the step-up grant ------------------------------------------------


def test_approval_without_a_step_up_grant_is_refused(
    rw, session, owner, project: str, token_for
) -> None:
    token = token_for(owner)
    item, first = _in_review(rw, token, owner, project)

    with bound(rw, token) as db, pytest.raises(DBAPIError) as raised:
        approve_revision(
            db,
            content_item_id=item.id,
            expected_project_id=project,
            revision_id=first.id,
            actor_user=owner,
        )
        db.flush()

    _refused(raised, "needs a fresh step-up confirmation")


def test_a_grant_is_consumed_by_the_approval_it_authorises(
    rw, session, owner, project: str, token_for
) -> None:
    token = token_for(owner)
    item, first = _in_review(rw, token, owner, project)
    grant = _grant(session, token=token, item=item, revision=first)

    with bound(rw, token) as db:
        _, approval = approve_revision(
            db,
            content_item_id=item.id,
            expected_project_id=project,
            revision_id=first.id,
            actor_user=owner,
        )
        db.commit()
        approval_id = approval.id

    session.expire_all()
    stored = session.get(ApprovalGrant, grant.id)

    assert stored.approval_id == approval_id
    assert stored.consumed_at is not None


def test_a_rejection_needs_no_grant(
    rw, session, owner, project: str, token_for
) -> None:
    from ai_smm.application.content import reject_revision

    token = token_for(owner)
    item, first = _in_review(rw, token, owner, project)

    with bound(rw, token) as db:
        reject_revision(
            db,
            content_item_id=item.id,
            expected_project_id=project,
            revision_id=first.id,
            actor_user=owner,
        )
        db.commit()


@pytest.mark.parametrize("spoil", ["expired", "other_session", "used"])
def test_a_grant_is_good_once_for_its_session_only(
    rw, session, owner, project: str, token_for, spoil: str
) -> None:
    token = token_for(owner)
    item, first = _in_review(rw, token, owner, project)

    if spoil == "expired":
        _grant(
            session,
            token=token,
            item=item,
            revision=first,
            expires_in=timedelta(minutes=-1),
        )
    elif spoil == "other_session":
        # The owner confirmed in another browser; this session did not.
        _grant(session, token=token_for(owner), item=item, revision=first)
    elif spoil == "used":
        grant = _grant(session, token=token, item=item, revision=first)
        grant.consumed_at = datetime.now(timezone.utc)
        grant.approval_id = uuid.uuid4()
        session.commit()

    with bound(rw, token) as db, pytest.raises(DBAPIError) as raised:
        approve_revision(
            db,
            content_item_id=item.id,
            expected_project_id=project,
            revision_id=first.id,
            actor_user=owner,
        )
        db.flush()

    _refused(raised, "needs a fresh step-up confirmation")


def test_only_the_current_revision_can_be_decided(
    rw, session, owner, project: str, token_for
) -> None:
    from ai_smm.application.content import create_revision

    token = token_for(owner)
    item, first = _in_review(rw, token, owner, project)
    _grant(session, token=token, item=item, revision=first)

    with bound(rw, token) as db:
        create_revision(
            db,
            content_item_id=item.id,
            expected_project_id=project,
            expected_item_version=2,
            revision=RevisionInput(body="Вторая версия.", format="text"),
            created_by_user=owner,
        )
        db.commit()

    # Straight SQL: an approval of the superseded first revision.
    with bound(rw, token) as db, pytest.raises(DBAPIError) as raised:
        db.execute(
            text(
                "INSERT INTO content_approvals (id, content_item_id, revision_id,"
                " decision, actor_user_id) VALUES (gen_random_uuid(), :item,"
                " :revision, 'approved', :user)"
            ),
            {"item": item.id, "revision": first.id, "user": owner.id},
        )

    _refused(raised, "only the current revision can be decided")


def test_an_approval_pins_the_destinations_identity(
    rw, session, owner, project: str, token_for, make_account
) -> None:
    account, _ = make_account()
    token = token_for(owner)
    item, first = _in_review(rw, token, owner, project, social_account_id=account.id)
    # Confirmed while the account was bound to another identity.
    _grant(
        session,
        token=token,
        item=item,
        revision=first,
        external_account_id="someone-else",
    )

    with bound(rw, token) as db, pytest.raises(DBAPIError) as raised:
        approve_revision(
            db,
            content_item_id=item.id,
            expected_project_id=project,
            revision_id=first.id,
            actor_user=owner,
        )
        db.flush()

    _refused(raised, "changed identity")


def test_two_approvals_cannot_share_one_grant(
    rw, session, owner, project: str, token_for
) -> None:
    """Concurrency: the grant row is locked, the second waiter sees it used."""

    token = token_for(owner)
    item, first = _in_review(rw, token, owner, project)
    _grant(session, token=token, item=item, revision=first)

    consume = text(
        "SELECT public.guard_consume_approval_grant(gen_random_uuid(), :item, :rev)"
    )
    params = {"item": item.id, "rev": first.id}
    outcome: dict[str, object] = {}

    with bound(rw, token) as first_tx:
        first_tx.execute(consume, params)  # holds the row lock

        def second() -> None:
            with bound(rw, token) as second_tx:
                try:
                    second_tx.execute(consume, params)
                    outcome["second"] = "consumed"
                except DBAPIError as exc:
                    outcome["second"] = str(exc)

        waiter = threading.Thread(target=second)
        waiter.start()
        waiter.join(timeout=1)
        assert waiter.is_alive(), "the second consumer should wait for the lock"

        first_tx.commit()

    waiter.join(timeout=10)

    assert "needs a fresh step-up confirmation" in str(outcome["second"])


# -- publications ----------------------------------------------------------------


_INSERT_PUBLICATION = text(
    "INSERT INTO publications (project_id, platform, ordinal, title, format, body,"
    " images, items, status, scheduled_at, idempotency_key, human_reviewed,"
    " social_account_id, attempt_count)"
    " VALUES (:project, 'threads', :ordinal, :title, 'text', :body, '[]', '[]',"
    " :status, :scheduled_at, :key, true, :account, 0) RETURNING id"
)


def _insert_publication(db: Session, project: str, **values) -> int:
    params = {
        "project": project,
        "ordinal": 990000 + values.pop("ordinal", 1),
        "title": "Guarded",
        "body": "Текст для проверки.",
        "status": "approved",
        "scheduled_at": None,
        "key": f"forged-{uuid.uuid4()}",
        "account": None,
        **values,
    }
    return db.execute(_INSERT_PUBLICATION, params).scalar_one()


def _link(db: Session, item, revision, publication_id: int) -> None:
    db.execute(
        text(
            "INSERT INTO content_publication_links (content_item_id, revision_id,"
            " platform, publication_id) VALUES (:item, :revision, 'threads', :pub)"
        ),
        {"item": item.id, "revision": revision.id, "pub": publication_id},
    )


@pytest.mark.parametrize(
    "values",
    [
        {"status": "scheduled", "scheduled_at": datetime.now(timezone.utc)},
        {"status": "claimed"},
        {"status": "published"},
    ],
)
def test_a_new_publication_is_never_queued_or_delivered(
    rw, owner, project: str, token_for, values
) -> None:
    with bound(rw, token_for(owner)) as db, pytest.raises(DBAPIError) as raised:
        _insert_publication(db, project, **values)

    _refused(raised, "approved, unscheduled delivery snapshot")


def test_a_snapshot_without_lineage_is_refused_at_commit(
    rw, owner, project: str, token_for
) -> None:
    with bound(rw, token_for(owner)) as db:
        _insert_publication(db, project)  # accepted for now ...

        with pytest.raises(DBAPIError) as raised:
            db.commit()  # ... and refused at COMMIT

    _refused(raised, "has no content lineage")


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"body": "Подменённый текст."}, "differs from the approved revision"),
        ({"title": "Другой заголовок"}, "differs from the approved revision"),
    ],
)
def test_a_snapshot_must_be_exactly_what_was_approved(
    rw, session, owner, project: str, token_for, change, reason
) -> None:
    token = token_for(owner)
    item, first = _approved(rw, session, token, owner, project)

    with bound(rw, token) as db:
        publication_id = _insert_publication(db, project, **change)
        _link(db, item, first, publication_id)

        with pytest.raises(DBAPIError) as raised:
            db.commit()

    _refused(raised, reason)


def test_a_snapshot_cannot_change_its_destination(
    rw, session, owner, project: str, token_for, make_account
) -> None:
    account, _ = make_account()
    token = token_for(owner)
    item, first = _approved(rw, session, token, owner, project)  # account-less

    with bound(rw, token) as db:
        publication_id = _insert_publication(db, project, account=account.id)
        _link(db, item, first, publication_id)

        with pytest.raises(DBAPIError) as raised:
            db.commit()

    _refused(raised, "another account than the approved one")


def test_lineage_cannot_be_attached_to_an_existing_publication(
    rw, session, owner, project: str, token_for, make_publication
) -> None:
    """An older row (e.g. a legacy one) never becomes a content snapshot."""

    token = token_for(owner)
    item, first = _approved(rw, session, token, owner, project)
    legacy = make_publication(status=PublicationStatus.APPROVED)

    with bound(rw, token) as db, pytest.raises(DBAPIError) as raised:
        _link(db, item, first, legacy.id)

    _refused(raised, "created in the same transaction")


def test_the_full_flow_passes_every_guard(
    rw, session, owner, project: str, token_for, settings
) -> None:
    """create -> submit -> step-up -> approve -> materialise -> schedule."""

    from dataclasses import replace

    token = token_for(owner)
    item, first = _approved(rw, session, token, owner, project)

    with bound(rw, token) as db:
        result = materialize_approved_revision(
            db,
            content_item_id=item.id,
            expected_project_id=project,
            actor_user=owner,
            mutations_enabled=True,
            legacy_projects=LEGACY,
        )
        db.commit()
        publication_id = result.publication.id

    with bound(rw, token) as db:
        schedule_publication(
            db,
            publication_id=publication_id,
            expected_project_id=project,
            scheduled_at=datetime.now(timezone.utc) + timedelta(hours=1),
            actor=f"user:{owner.id}",
            mutations_enabled=True,
            settings=replace(settings, legacy_threads_projects=LEGACY),
        )
        db.commit()

    session.expire_all()

    assert (
        session.get(Publication, publication_id).status is PublicationStatus.SCHEDULED
    )
    assert (
        session.execute(
            text("SELECT public.guard_publication_problem(:id)"), {"id": publication_id}
        ).scalar_one()
        is None
    )


@pytest.mark.parametrize(
    "assignment",
    [
        "status = 'claimed'",
        "status = 'publishing'",
        "status = 'published', threads_post_id = 'x', published_at = now()",
        "status = 'needs_review'",
        "claimed_by = 'api', status = 'scheduled', scheduled_at = now()",
        "attempt_count = 5, status = 'scheduled', scheduled_at = now()",
    ],
)
def test_the_worker_columns_and_states_are_the_workers(
    rw, owner, project: str, token_for, make_publication, assignment: str
) -> None:
    publication = make_publication(status=PublicationStatus.APPROVED)

    with bound(rw, token_for(owner)) as db, pytest.raises(DBAPIError) as raised:
        db.execute(
            text(f"UPDATE publications SET {assignment} WHERE id = :id"),
            {"id": publication.id},
        )

    _refused(raised)


def test_an_unreviewed_legacy_row_cannot_be_scheduled(
    rw, owner, project: str, token_for, make_publication
) -> None:
    publication = make_publication(
        status=PublicationStatus.APPROVED, human_reviewed=False
    )

    with bound(rw, token_for(owner)) as db, pytest.raises(DBAPIError) as raised:
        db.execute(
            text(
                "UPDATE publications SET status = 'scheduled', scheduled_at = now()"
                " WHERE id = :id"
            ),
            {"id": publication.id},
        )

    _refused(raised, "not human-reviewed")


def test_a_reviewed_legacy_row_is_still_scheduled(
    rw, owner, project: str, token_for, make_publication
) -> None:
    """Pilot rows approved through the operator's CLI keep working."""

    publication = make_publication(status=PublicationStatus.APPROVED)

    with bound(rw, token_for(owner)) as db:
        db.execute(
            text(
                "UPDATE publications SET status = 'scheduled', scheduled_at = now()"
                " WHERE id = :id"
            ),
            {"id": publication.id},
        )
        db.commit()


def test_an_account_row_is_not_scheduled_to_a_disabled_account(
    rw, session, owner, project: str, token_for, make_account
) -> None:
    account, _ = make_account()
    token = token_for(owner)
    item, first = _approved(
        rw, session, token, owner, project, social_account_id=account.id
    )

    with bound(rw, token) as db:
        result = materialize_approved_revision(
            db,
            content_item_id=item.id,
            expected_project_id=project,
            actor_user=owner,
            mutations_enabled=True,
        )
        db.commit()
        publication_id = result.publication.id

    session.get(SocialAccount, account.id).status = "disabled"
    session.get(SocialAccount, account.id).disabled_at = datetime.now(timezone.utc)
    session.commit()

    with bound(rw, token) as db, pytest.raises(DBAPIError) as raised:
        db.execute(
            text(
                "UPDATE publications SET status = 'scheduled', scheduled_at = now()"
                " WHERE id = :id"
            ),
            {"id": publication_id},
        )

    _refused(raised, "not ready to publish")


def test_the_worker_is_not_guarded(session, make_publication) -> None:
    """The owner role -- the worker, the CLI -- passes untouched."""

    from ai_smm.queue import claim_due_publication

    make_publication(
        status=PublicationStatus.SCHEDULED,
        scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )

    claimed = claim_due_publication(session, worker_id="w", lease_seconds=60)
    session.commit()

    assert claimed is not None and claimed.status is PublicationStatus.CLAIMED


# -- accounts, credentials, memberships -----------------------------------------


def test_a_bound_identity_never_changes(
    rw, owner, project: str, token_for, make_account
) -> None:
    account, _ = make_account()

    for value in ("someone-else", None):
        with bound(rw, token_for(owner)) as db, pytest.raises(DBAPIError) as raised:
            db.execute(
                text(
                    "UPDATE social_accounts SET external_account_id = :v WHERE id = :id"
                ),
                {"v": value, "id": account.id},
            )

        _refused(raised, "bound and never changes")


def test_an_account_is_never_created_with_an_identity(
    rw, owner, project: str, token_for
) -> None:
    with bound(rw, token_for(owner)) as db, pytest.raises(DBAPIError) as raised:
        db.execute(
            text(
                "INSERT INTO social_accounts (id, project_id, platform, display_name,"
                " status, external_account_id, created_by_user_id) VALUES"
                " (gen_random_uuid(), :p, 'threads', 'x', 'active', 'ext', :u)"
            ),
            {"p": project, "u": owner.id},
        )

    _refused(raised, "bound by a verified credential")


def test_a_retired_credential_never_comes_back(
    rw, session, owner, project: str, token_for, make_account
) -> None:
    account, _ = make_account()
    token = token_for(owner)
    retire = text(
        "UPDATE social_account_credentials SET status = 'revoked', revoked_at = now(),"
        " ciphertext = NULL, nonce = NULL, ephemeral_public_key = NULL"
        " WHERE social_account_id = :a"
    )

    with bound(rw, token) as db:
        db.execute(retire, {"a": account.id})
        db.commit()

    with bound(rw, token) as db, pytest.raises(DBAPIError) as raised:
        db.execute(
            text(
                "UPDATE social_account_credentials SET status = 'superseded',"
                " superseded_at = now() WHERE social_account_id = :a"
            ),
            {"a": account.id},
        )

    _refused(raised, "only goes from active")


def test_nobody_is_made_a_member_of_an_existing_project(
    rw, owner, make_user, make_other_project, project: str, token_for
) -> None:
    other = make_user()

    for user_id, role, target in (
        (owner.id, "owner", make_other_project),  # self, into another project
        (other.id, "owner", project),  # someone else, into one's own project
        (owner.id, "admin", project),
    ):
        with bound(rw, token_for(owner)) as db, pytest.raises(DBAPIError) as raised:
            db.execute(
                text(
                    "INSERT INTO project_memberships (id, project_id, user_id, role)"
                    " VALUES (gen_random_uuid(), :p, :u, :r)"
                ),
                {"p": target, "u": user_id, "r": role},
            )

        _refused(raised)


def test_an_owner_creates_a_project_with_themselves_as_its_owner(
    rw, session, owner, project: str, token_for
) -> None:
    from ai_smm.application.projects import ProjectInput, create_project
    from ai_smm.db.models import ProjectMembership

    with bound(rw, token_for(owner)) as db:
        result = create_project(
            db,
            actor_user=owner,
            data=ProjectInput(id="second-project", display_name="Второй проект"),
        )
        db.commit()

    assert result.created
    session.expire_all()
    membership = session.scalars(
        select(ProjectMembership).where(
            ProjectMembership.project_id == "second-project"
        )
    ).one()

    assert membership.user_id == owner.id and membership.role is MembershipRole.OWNER


def test_a_bound_context_records_only_the_transaction_it_belongs_to(
    rw, session, owner, token_for
) -> None:
    token = token_for(owner)

    with bound(rw, token) as db:
        db.commit()

    session.expire_all()
    contexts = session.scalars(select(MutationContext)).all()

    assert contexts and all(c.user_id == owner.id for c in contexts)


def test_approval_rows_reference_their_grant(
    rw, session, owner, project: str, token_for
) -> None:
    token = token_for(owner)
    item, first = _approved(rw, session, token, owner, project)
    session.expire_all()
    approval = session.scalars(
        select(ContentApproval).where(ContentApproval.revision_id == first.id)
    ).one()

    assert (
        session.scalars(
            select(ApprovalGrant).where(ApprovalGrant.approval_id == approval.id)
        )
        .one()
        .revision_id
        == first.id
    )
