"""Fail-closed preflight of the worker's configuration against the queue.

A worker released with SMM-025B routes every publication by account
(ai_smm.account_routing). Started with a configuration that does not fit
the queue it finds -- the legacy allow-list forgotten, the credential key
not mounted, the schema not migrated -- it would not publish anything
wrongly, but it would turn every existing scheduled row it cannot route
into `failed`, one by one, at its scheduled time. This module finds that
before the worker claims a single row.

Worker.run() runs it first and refuses to start on any FAIL (exit code
78, EX_CONFIG): no recovery, no scheduler, no claim. `ai-smm preflight
worker` runs the same checks for an operator, read-only, before a
release is switched on.

Only configuration is a FAIL. A state an owner chose -- an account they
disabled, a credential they revoked -- is reported as INFO: those rows
are refused one by one, as designed, and must not stop every other
project's publishing. No check prints a secret; at most a key id.
"""
from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import func, select, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session

from ai_smm.account_routing import RouteUnavailable, check_route_ready
from ai_smm.config import Settings
from ai_smm.db.models import (
    CREDENTIAL_STATUS_ACTIVE,
    Project,
    Publication,
    PublicationStatus,
    SocialAccountCredential,
)
from ai_smm.security.credentials import CredentialOpener


#: The schema this code is written for. tests/test_preflight.py keeps it
#: equal to the Alembic head, so a new migration cannot be forgotten here.
EXPECTED_SCHEMA_REVISION = "e6b1f3a9c205"

#: EX_CONFIG from sysexits.h: the worker's configuration is wrong.
EXIT_CONFIG = 78

PASS = "PASS"
FAIL = "FAIL"
INFO = "INFO"

#: Rows the worker would try to publish without anyone acting first.
PENDING = (PublicationStatus.SCHEDULED, PublicationStatus.CLAIMED)


@dataclass(frozen=True)
class Check:
    status: str
    name: str
    detail: str

    def line(self) -> str:
        return f"{self.status}|{self.name}|{self.detail}"


class PreflightFailed(Exception):
    def __init__(self, checks: list[Check]) -> None:
        super().__init__(
            "Worker preflight failed: "
            + "; ".join(c.line() for c in checks if c.status == FAIL)
        )

        self.checks = checks


def failures(checks: list[Check]) -> list[Check]:
    return [check for check in checks if check.status == FAIL]


def _schema_revision(session: Session) -> str | None:
    try:
        with session.begin_nested():
            return session.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one_or_none()
    except ProgrammingError:
        return None


def worker_preflight(
    session: Session,
    settings: Settings,
    opener: CredentialOpener | None,
    *,
    getenv: Callable[[str], str | None] = os.getenv,
) -> list[Check]:
    """Every check, PASS / FAIL / INFO. Reads only; changes nothing."""

    checks: list[Check] = []
    allowed = set(settings.legacy_threads_projects)

    # 1. The schema. Without the SMM-025B columns nothing below can even
    #    be asked, and the worker's own queries would fail.
    revision = _schema_revision(session)

    if revision != EXPECTED_SCHEMA_REVISION:
        checks.append(
            Check(
                FAIL,
                "schema.revision",
                f"database at {revision or 'no alembic_version'}, this worker "
                f"needs {EXPECTED_SCHEMA_REVISION}; migrate first",
            )
        )

        return checks

    checks.append(Check(PASS, "schema.revision", revision))

    # 2. Every allow-listed project exists: a typo would silently leave the
    #    intended project out.
    known = set(
        session.scalars(select(Project.id).where(Project.id.in_(allowed))).all()
    )
    unknown = sorted(allowed - known)

    checks.append(
        Check(
            FAIL if unknown else PASS,
            "legacy.allow_list.known_projects",
            f"unknown: {', '.join(unknown)}"
            if unknown
            else f"{len(allowed)} allow-listed: {', '.join(sorted(allowed)) or '-'}",
        )
    )

    # 3. No pending legacy row would be refused for want of an allow-list
    #    entry. This is the check that keeps a release from failing the
    #    existing queue.
    pending_legacy = dict(
        session.execute(
            select(Publication.project_id, func.count())
            .where(
                Publication.social_account_id.is_(None),
                Publication.status.in_(PENDING),
            )
            .group_by(Publication.project_id)
        ).all()
    )
    refused = {p: n for p, n in pending_legacy.items() if p not in allowed}

    checks.append(
        Check(
            FAIL if refused else PASS,
            "legacy.pending_rows_allowed",
            (
                "scheduled legacy rows of projects not in "
                "AI_SMM_LEGACY_THREADS_PROJECTS: "
                + ", ".join(f"{p}={n}" for p, n in sorted(refused.items()))
            )
            if refused
            else f"{sum(pending_legacy.values())} pending legacy rows, all allowed",
        )
    )

    # 4. A live worker that will publish legacy rows needs the deployment
    #    token. Its value is never read here beyond "non-empty".
    allowed_pending = sum(n for p, n in pending_legacy.items() if p in allowed)

    if settings.dry_run:
        checks.append(Check(INFO, "legacy.deployment_token", "dry run: not needed"))
    elif allowed_pending and not (getenv("THREADS_ACCESS_TOKEN") or "").strip():
        checks.append(
            Check(
                FAIL,
                "legacy.deployment_token",
                f"{allowed_pending} pending legacy rows, THREADS_ACCESS_TOKEN not set",
            )
        )
    else:
        checks.append(
            Check(
                PASS,
                "legacy.deployment_token",
                "set" if allowed_pending else "no pending legacy rows",
            )
        )

    # 5. Every stored credential can be opened by this worker.
    stored = set(
        session.scalars(
            select(SocialAccountCredential.key_id)
            .where(SocialAccountCredential.status == CREDENTIAL_STATUS_ACTIVE)
            .distinct()
        ).all()
    )
    held = opener.key_ids if opener is not None else frozenset()
    missing = sorted(stored - held)

    if not stored:
        checks.append(Check(PASS, "accounts.credential_keys", "no stored credentials"))
    else:
        checks.append(
            Check(
                FAIL if missing else PASS,
                "accounts.credential_keys",
                f"no private key for key id(s): {', '.join(missing)} "
                "(AI_SMM_CREDENTIALS_PRIVATE_KEY_FILES)"
                if missing
                else f"keys held for all {len(stored)} key id(s)",
            )
        )

    # 6. Account-bound rows that will be refused because of a choice an
    #    owner made. Reported, not fatal.
    pending_accounts = session.scalars(
        select(Publication).where(
            Publication.social_account_id.is_not(None),
            Publication.status.in_(PENDING),
        )
    ).all()
    not_ready: dict[str, int] = {}

    for publication in pending_accounts:
        try:
            check_route_ready(session, publication, settings)
        except RouteUnavailable as exc:
            not_ready[exc.code] = not_ready.get(exc.code, 0) + 1

    checks.append(
        Check(
            INFO if not_ready else PASS,
            "accounts.pending_rows_ready",
            (
                "will be refused: "
                + ", ".join(f"{code}={n}" for code, n in sorted(not_ready.items()))
            )
            if not_ready
            else f"{len(pending_accounts)} pending account rows, all routable",
        )
    )

    return checks
