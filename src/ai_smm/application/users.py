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


class ProjectAlreadyExistsError(ValueError):
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
    create_project_named: str | None = None,
) -> User:
    """Create a user and make them owner of an existing project.

    One transaction: the caller commits. A user without a membership
    would be able to sign in and see nothing, and a membership without a
    user cannot exist, so neither half is allowed to land alone.

    create_project_named is the bootstrap of an empty installation: since
    SMM-025B nothing else creates a project without an owner, and `ai-smm
    project create` needs an existing user. With it, the project is
    created in the same transaction, by the same service as everywhere
    else (create_project): user, project, owner membership, settings and
    audit land together or not at all. It is refused for a project that
    already exists, so a typo cannot silently attach the owner elsewhere.

    The password is hashed here and never returned, printed or logged.
    """

    normalized = normalize_email(email)
    validate_password_strength(password)

    name = (display_name or "").strip()

    if not name:
        raise ValueError("Display name must not be empty.")

    project = db.get(Project, project_id)

    if create_project_named is not None and project is not None:
        raise ProjectAlreadyExistsError(
            f"Project {project_id!r} already exists; omit --create-project "
            "to make the new user its owner."
        )

    if project is None and create_project_named is None:
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

    if project is None:
        from ai_smm.application.projects import ProjectInput, create_project

        # Writes the owner membership itself.
        project = create_project(
            db,
            actor_user=user,
            actor=BOOTSTRAP_ACTOR,
            require_existing_ownership=False,
            data=ProjectInput(id=project_id, display_name=create_project_named),
        ).project
    else:
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
