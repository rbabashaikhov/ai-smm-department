"""User records and the owner bootstrap.

There is no public registration. The first user of a project is created by
an operator at the console (`ai-smm user create-owner`), which is why the
transactional work lives here rather than in the HTTP layer: the API never
creates a user.
"""
from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_smm.db.models import MembershipRole, Project, ProjectMembership, User
from ai_smm.queue import record_audit
from ai_smm.security.passwords import hash_password, validate_password_strength


#: Deliberately permissive: one @, something either side, a dot in the
#: domain. Full RFC validation belongs to a mail server, not to a login
#: form, and a stricter pattern here would only reject valid addresses.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

#: Actor recorded for the bootstrap: no user exists yet to attribute it to.
BOOTSTRAP_ACTOR = "system:bootstrap"


class InvalidEmailError(ValueError):
    pass


class DuplicateUserError(ValueError):
    pass


class ProjectNotFoundError(ValueError):
    pass


def normalize_email(raw: str) -> str:
    """Trim and lowercase. Stored and compared in exactly this form.

    Normalising on the way in is what makes a plain unique index behave
    like a case-insensitive one, so no CITEXT extension is needed.
    """

    email = (raw or "").strip().lower()

    if not _EMAIL_RE.match(email):
        raise InvalidEmailError("Not a valid email address.")

    if len(email) > 320:
        raise InvalidEmailError("Email address is too long.")

    return email


def get_user_by_email(db: Session, email: str) -> User | None:
    return db.scalars(
        select(User).where(User.email == email)
    ).one_or_none()


def create_owner(
    db: Session,
    *,
    email: str,
    display_name: str,
    project_id: str,
    password: str,
) -> User:
    """Create a user and make them owner of an existing project.

    One transaction: the caller commits. A user without a membership
    would be able to sign in and see nothing, and a membership without a
    user cannot exist, so neither half is allowed to land alone.

    The password is hashed here and never returned, printed or logged.
    """

    normalized = normalize_email(email)
    validate_password_strength(password)

    name = (display_name or "").strip()

    if not name:
        raise ValueError("Display name must not be empty.")

    project = db.get(Project, project_id)

    if project is None:
        raise ProjectNotFoundError(
            f"Project {project_id!r} does not exist."
        )

    if get_user_by_email(db, normalized) is not None:
        raise DuplicateUserError(
            f"A user with address {normalized!r} already exists."
        )

    user = User(
        email=normalized,
        password_hash=hash_password(password),
        display_name=name,
        is_active=True,
    )
    db.add(user)
    db.flush()

    db.add(
        ProjectMembership(
            project_id=project.id,
            user_id=user.id,
            role=MembershipRole.OWNER,
        )
    )

    record_audit(
        db,
        actor=BOOTSTRAP_ACTOR,
        action="user.create_owner",
        subject=f"user:{user.id}",
        details={
            "email": normalized,
            "project_id": project.id,
            "role": MembershipRole.OWNER.value,
        },
    )

    db.flush()

    return user
