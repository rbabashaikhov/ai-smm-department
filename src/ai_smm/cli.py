"""Operator CLI for the publication queue.

Nothing here publishes implicitly. The only command that can contact Threads
is `publish-now`, and it requires both --live and --confirm-reviewed, in the
same spirit as scripts/publish_saved.py.
"""
from __future__ import annotations

import argparse
import getpass
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select

from ai_smm.config import Settings, get_settings
from ai_smm.db.models import (
    AuditLog,
    Publication,
    PublicationAttempt,
    PublicationStatus,
)
from ai_smm.db.session import session_scope
from ai_smm.importer import import_json_queue
from ai_smm.logging_setup import setup_logging
from ai_smm.publishing.service import (
    publish_claimed_publication,
    validate_ready_to_publish,
)
from ai_smm.queue import (
    cancel,
    claim_due_publication,
    get_publication,
    list_publications,
    quarantine_abandoned_publishing,
    reconcile_as_published,
    record_audit,
    release_expired_leases,
    reschedule,
    utcnow,
)


def _actor() -> str:
    try:
        return f"cli:{getpass.getuser()}"
    except Exception:
        return "cli:unknown"


def _local(dt: datetime | None, settings: Settings) -> str:
    if dt is None:
        return "-"

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(settings.display_tz).strftime("%Y-%m-%d %H:%M")


def _parse_when(value: str, settings: Settings) -> datetime:
    """Accept an explicit offset, or a naive time in the display timezone.

    A naive value is never read as UTC: the operator timezone is the one they
    typed in, and silently shifting it by three hours would move a publish.
    """

    raw = value.strip()

    if raw.lower() in {"now", "сейчас"}:
        return utcnow()

    if raw.startswith("+"):
        minutes = int(raw[1:].rstrip("m"))

        return utcnow() + timedelta(minutes=minutes)

    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=settings.display_tz)

    return parsed.astimezone(timezone.utc)


# -- commands -------------------------------------------------------------


def cmd_queue(args: argparse.Namespace, settings: Settings) -> int:
    statuses = (
        [PublicationStatus(s) for s in args.status] if args.status else None
    )

    with session_scope(settings) as session:
        publications = list_publications(
            session,
            project_id=args.project,
            statuses=statuses,
            limit=args.limit,
        )

        if not publications:
            print("Queue is empty for the given filter.")

            return 0

        header = (
            f"{'ID':>5}  {'PROJECT':<24} {'#':>3} {'FORMAT':<9} "
            f"{'STATUS':<13} {'SCHEDULED (' + settings.display_tz_name + ')':<22}"
            f" {'ATT':>3} {'REV':>3}  TITLE"
        )
        print(header)
        print("-" * len(header))

        for item in publications:
            print(
                f"{item.id:>5}  {item.project_id:<24} {item.ordinal:>3} "
                f"{item.format:<9} {item.status.value:<13} "
                f"{_local(item.scheduled_at, settings):<22} "
                f"{item.attempt_count:>3} "
                f"{'yes' if item.human_reviewed else 'no':>3}  "
                f"{item.title[:46]}"
            )

        print()
        print(f"{len(publications)} row(s). Times shown in "
              f"{settings.display_tz_name}; stored in UTC.")

    return 0


def cmd_show(args: argparse.Namespace, settings: Settings) -> int:
    with session_scope(settings) as session:
        publication = get_publication(session, args.publication)

        if publication is None:
            print(f"Publication {args.publication} not found.",
                  file=sys.stderr)

            return 1

        print(f"id              : {publication.id}")
        print(f"project         : {publication.project_id}")
        print(f"platform        : {publication.platform}")
        print(f"ordinal         : {publication.ordinal}")
        print(f"title           : {publication.title}")
        print(f"format          : {publication.format}")
        print(f"status          : {publication.status.value}")
        print(f"scheduled_at    : {_local(publication.scheduled_at, settings)}"
              f"  ({publication.scheduled_at})")
        print(f"human_reviewed  : {publication.human_reviewed}")
        print(f"attempt_count   : {publication.attempt_count}")
        print(f"threads_post_id : {publication.threads_post_id or '-'}")
        print(f"published_at    : {_local(publication.published_at, settings)}")
        print(f"claimed_by      : {publication.claimed_by or '-'}")
        print(f"lease_expires_at: {_local(publication.lease_expires_at, settings)}")
        print(f"idempotency_key : {publication.idempotency_key}")
        print(f"images          : {len(publication.images)}")
        print(f"characters      : {len(publication.body)}")

        if publication.last_error:
            print(f"last_error      : {publication.last_error}")

        print()
        print("--- text ---")
        print(publication.body)

        if publication.attempts:
            print()
            print("--- attempts ---")

            for attempt in publication.attempts:
                print(
                    f"#{attempt.attempt_number} {attempt.phase.value:<9} "
                    f"{(attempt.outcome.value if attempt.outcome else 'open'):<8} "
                    f"worker={attempt.worker_id} "
                    f"started={_local(attempt.started_at, settings)} "
                    f"creation_id={attempt.threads_creation_id or '-'} "
                    f"post_id={attempt.threads_post_id or '-'}"
                )

                if attempt.error_message:
                    print(f"    error: {attempt.error_message}")

    return 0


def cmd_errors(args: argparse.Namespace, settings: Settings) -> int:
    with session_scope(settings) as session:
        statuses = [
            PublicationStatus.NEEDS_REVIEW,
            PublicationStatus.FAILED,
        ]

        publications = list_publications(
            session, statuses=statuses, limit=args.limit
        )

        if not publications:
            print("No publications need attention.")

            return 0

        for item in publications:
            print(
                f"[{item.status.value}] id={item.id} "
                f"project={item.project_id} #{item.ordinal} "
                f"attempts={item.attempt_count}"
            )
            print(f"  title : {item.title[:70]}")
            print(f"  error : {item.last_error or '-'}")

            open_attempts = [
                a for a in item.attempts if a.outcome is None
                or a.outcome.value in {"unknown", "timeout"}
            ]

            for attempt in open_attempts:
                print(
                    f"  ambiguous attempt #{attempt.attempt_number}: "
                    f"phase={attempt.phase.value} "
                    f"creation_id={attempt.threads_creation_id or '-'} "
                    f"started={_local(attempt.started_at, settings)}"
                )

            if item.status is PublicationStatus.NEEDS_REVIEW:
                print(
                    "  next  : verify the Threads account, then "
                    f"'reconcile {item.id} --published <post_id>' or "
                    f"'reconcile {item.id} --retry'"
                )

            print()

    return 0


def cmd_reconcile(args: argparse.Namespace, settings: Settings) -> int:
    """Close an ambiguous record after a human checked the real account."""

    if not (args.published or args.retry or args.cancel):
        print(
            "Choose one: --published <threads_post_id>, --retry or --cancel.",
            file=sys.stderr,
        )

        return 2

    with session_scope(settings) as session:
        publication = get_publication(session, args.publication)

        if publication is None:
            print(f"Publication {args.publication} not found.",
                  file=sys.stderr)

            return 1

        if publication.status is not PublicationStatus.NEEDS_REVIEW and not (
            args.force
        ):
            print(
                f"Publication {publication.id} has status "
                f"{publication.status.value}, not needs_review. "
                "Pass --force only if you know why.",
                file=sys.stderr,
            )

            return 1

        if args.published:
            reconcile_as_published(
                session,
                publication,
                threads_post_id=args.published,
                actor=_actor(),
                note=args.note,
            )
            print(
                f"Publication {publication.id} closed as published "
                f"(threads_post_id={args.published})."
            )

            return 0

        if args.cancel:
            cancel(
                session, publication, actor=_actor(), note=args.note
            )
            print(f"Publication {publication.id} cancelled.")

            return 0

        # --retry: only legitimate when the operator established that no post
        # was created.
        if not args.confirm_not_published:
            print(
                "Refusing to reschedule an ambiguous record without "
                "--confirm-not-published. Check the Threads account first: "
                "if the post exists, use --published <id> instead.",
                file=sys.stderr,
            )

            return 2

        when = (
            _parse_when(args.at, settings)
            if args.at
            else utcnow()
        )

        reschedule(
            session,
            publication,
            scheduled_at=when,
            actor=_actor(),
            note=args.note or "operator confirmed no post was created",
            reset_attempts=args.reset_attempts,
        )

        print(
            f"Publication {publication.id} rescheduled for "
            f"{_local(when, settings)} {settings.display_tz_name}."
        )

    return 0


def cmd_schedule(args: argparse.Namespace, settings: Settings) -> int:
    with session_scope(settings) as session:
        publication = get_publication(session, args.publication)

        if publication is None:
            print(f"Publication {args.publication} not found.",
                  file=sys.stderr)

            return 1

        if publication.status is PublicationStatus.NEEDS_REVIEW:
            print(
                f"Publication {publication.id} is in needs_review; use "
                "'reconcile' so the ambiguity is resolved explicitly.",
                file=sys.stderr,
            )

            return 1

        when = _parse_when(args.at, settings)

        try:
            reschedule(
                session,
                publication,
                scheduled_at=when,
                actor=_actor(),
                note=args.note,
                reset_attempts=args.reset_attempts,
            )
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)

            return 1

        print(
            f"Publication {publication.id} scheduled for "
            f"{_local(when, settings)} {settings.display_tz_name} "
            f"(stored as {when.isoformat()})."
        )

    return 0


def cmd_approve(args: argparse.Namespace, settings: Settings) -> int:
    """Mark that a human read the exact text and images of this record."""

    with session_scope(settings) as session:
        publication = get_publication(session, args.publication)

        if publication is None:
            print(f"Publication {args.publication} not found.",
                  file=sys.stderr)

            return 1

        publication.human_reviewed = True
        publication.updated_at = utcnow()

        if publication.status is PublicationStatus.DRAFT:
            publication.status = PublicationStatus.APPROVED

        record_audit(
            session,
            actor=_actor(),
            action="human_reviewed",
            subject=f"publication:{publication.id}",
            details={"note": args.note},
        )

        print(
            f"Publication {publication.id} marked human_reviewed "
            f"(status={publication.status.value})."
        )

    return 0


def cmd_dry_run(args: argparse.Namespace, settings: Settings) -> int:
    """Run one cycle with publishing disabled, whatever the environment says."""

    from dataclasses import replace

    dry_settings = replace(settings, dry_run=True)

    from ai_smm.worker import Worker

    worker = Worker(
        settings=dry_settings,
        heartbeat_path=Path(args.heartbeat) if args.heartbeat else None,
    )

    worker.recover_on_start()
    results = worker.run_cycle()

    if not results:
        print("Nothing is due right now.")

        return 0

    for status in results:
        print(f"cycle result: {status}")

    return 0


def cmd_publish_now(args: argparse.Namespace, settings: Settings) -> int:
    """Publish one specific record for real. Deliberately hard to trigger."""

    if not args.live:
        print(
            "Preview only. Re-run with --live --confirm-reviewed to publish.",
        )

    if args.live and not args.confirm_reviewed:
        print(
            "For a live publish, pass --confirm-reviewed after checking the "
            "exact text and images.",
            file=sys.stderr,
        )

        return 2

    from dataclasses import replace

    run_settings = replace(settings, dry_run=not args.live)

    with session_scope(run_settings) as session:
        publication = get_publication(session, args.publication)

        if publication is None:
            print(f"Publication {args.publication} not found.",
                  file=sys.stderr)

            return 1

        print(f"id      : {publication.id}")
        print(f"format  : {publication.format}")
        print(f"images  : {len(publication.images)}")
        print(f"chars   : {len(publication.body)}")
        print("--- text ---")
        print(publication.body)
        print()

        if not args.live:
            try:
                validate_ready_to_publish(publication)
                print("Validation passed; nothing was sent.")
            except Exception as exc:
                print(f"Validation failed: {exc}", file=sys.stderr)

                return 1

            return 0

        # Go through the normal claim path so the row is reserved exactly as
        # the worker would reserve it.
        if publication.status is not PublicationStatus.SCHEDULED:
            print(
                f"Publication must be 'scheduled' to publish, found "
                f"'{publication.status.value}'. Use 'schedule' first.",
                file=sys.stderr,
            )

            return 1

        publication.scheduled_at = min(
            publication.scheduled_at or utcnow(), utcnow()
        )
        publication.next_attempt_at = None
        session.commit()

        claimed = claim_due_publication(
            session,
            worker_id=f"cli-{run_settings.worker_id}",
            lease_seconds=run_settings.lease_seconds,
        )

        if claimed is None or claimed.id != publication.id:
            print(
                "Could not claim this publication (another worker may hold "
                "it, or a different row was due first). Nothing was sent.",
                file=sys.stderr,
            )

            return 1

        session.commit()

        outcome = publish_claimed_publication(
            session,
            claimed,
            settings=run_settings,
            publisher_factory=_threads_factory,
            media_root=run_settings.media_root,
        )

        print(f"result : {outcome.status}")

        if outcome.threads_post_id:
            print(f"post_id: {outcome.threads_post_id}")

        if outcome.detail:
            print(f"detail : {outcome.detail}")

        return 0 if outcome.status in {"published", "dry_run"} else 1


def _threads_factory() -> Any:
    from ai_smm.publishing.threads import ThreadsPublisher

    return ThreadsPublisher()


def cmd_token_check(args: argparse.Namespace, settings: Settings) -> int:
    """Read-only probe of the Threads credentials. Publishes nothing.

    Threads tokens are not accepted by Meta's /debug_token endpoint, so
    there is no way to read an expiry date back from the token itself. What
    can be established is whether the token is accepted right now, and
    which account it belongs to -- which is the check worth running before
    a live publish, because a token for the wrong account would publish to
    the wrong place.
    """

    import os

    import httpx

    token = os.getenv("THREADS_ACCESS_TOKEN", "").strip()
    base = os.getenv(
        "THREADS_API_BASE_URL", "https://graph.threads.net"
    ).rstrip("/")

    if not token:
        print(
            "THREADS_ACCESS_TOKEN is not set. Publishing is impossible "
            "until it is.",
            file=sys.stderr,
        )

        return 1

    print(f"token: present, {len(token)} characters (value not shown)")
    print(f"api   : {base}")

    try:
        response = httpx.get(
            f"{base}/v1.0/me",
            params={"fields": "id,username"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=20,
        )
    except httpx.HTTPError as exc:
        print(f"could not reach the API: {type(exc).__name__}", file=sys.stderr)

        return 1

    if response.status_code != 200:
        # The body can echo the token back, so it is redacted before print.
        from ai_smm.logging_setup import get_redacting_filter

        detail = get_redacting_filter().redact(response.text[:300])
        print(
            f"token rejected: HTTP {response.status_code} {detail}",
            file=sys.stderr,
        )

        return 1

    identity = response.json()

    print(f"account: @{identity.get('username')} (id {identity.get('id')})")
    print("status : token is accepted for reading")
    print()
    print(
        "Publishing needs threads_content_publish in addition to "
        "threads_basic. That scope cannot be verified without publishing, "
        "so the first live post is the real test."
    )
    print(
        "Long-lived tokens last 60 days and are refreshed in place at "
        f"{base}/refresh_access_token?grant_type=th_refresh_token "
        "(the token must be at least 24h old and not yet expired)."
    )

    if args.project:
        print()
        print("Recent posts on this account (read-only check):")

        recent = httpx.get(
            f"{base}/v1.0/me/threads",
            params={"fields": "id,permalink,timestamp,media_type", "limit": 5},
            headers={"Authorization": f"Bearer {token}"},
            timeout=20,
        )

        if recent.status_code == 200:
            for item in recent.json().get("data", []):
                print(
                    f"  {item.get('timestamp')} | {item.get('media_type')} "
                    f"| {item.get('id')} | {item.get('permalink')}"
                )
        else:
            print(f"  could not list posts: HTTP {recent.status_code}")

    return 0


def cmd_import(args: argparse.Namespace, settings: Settings) -> int:
    queue_path = Path(args.path).expanduser().resolve()

    if not queue_path.is_file():
        print(f"Queue file not found: {queue_path}", file=sys.stderr)

        return 1

    with session_scope(settings) as session:
        report = import_json_queue(
            session,
            queue_path=queue_path,
            actor=_actor(),
            project_display_name=args.display_name,
        )

    print(f"created: {report.created}")
    print(f"updated: {report.updated}")

    for skipped in report.skipped:
        print(f"skipped: {skipped}")

    print(
        f"\nSource file left unchanged: {queue_path}"
    )

    return 0


def cmd_recover(args: argparse.Namespace, settings: Settings) -> int:
    with session_scope(settings) as session:
        quarantined = quarantine_abandoned_publishing(
            session, actor=_actor()
        )
        released = release_expired_leases(session, actor=_actor())

    print(f"quarantined to needs_review: {quarantined}")
    print(f"leases released            : {released}")

    return 0


def cmd_audit(args: argparse.Namespace, settings: Settings) -> int:
    with session_scope(settings) as session:
        stmt = (
            select(AuditLog)
            .order_by(AuditLog.at.desc())
            .limit(args.limit)
        )

        entries = list(session.execute(stmt).scalars())

        for entry in reversed(entries):
            print(
                f"{_local(entry.at, settings)}  {entry.actor:<22} "
                f"{entry.action:<26} {entry.subject or '-':<26} "
                f"{json.dumps(entry.details, ensure_ascii=False)}"
            )

    return 0


def cmd_stats(args: argparse.Namespace, settings: Settings) -> int:
    from sqlalchemy import func

    with session_scope(settings) as session:
        rows = session.execute(
            select(
                Publication.status,
                func.count(),
            ).group_by(Publication.status)
        ).all()

        print("publications by status:")

        for status, count in sorted(rows, key=lambda r: r[0].value):
            print(f"  {status.value:<14} {count}")

        attempts = session.execute(
            select(func.count()).select_from(PublicationAttempt)
        ).scalar_one()

        print(f"attempts recorded: {attempts}")
        print(f"dry_run mode     : {settings.dry_run}")
        print(f"display timezone : {settings.display_tz_name}")

    return 0


# -- parser ---------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai-smm",
        description=(
            "Operate the AI SMM publication queue. No command publishes "
            "without --live and --confirm-reviewed."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("queue", help="List the queue")
    p.add_argument("--project")
    p.add_argument(
        "--status",
        action="append",
        choices=[s.value for s in PublicationStatus],
    )
    p.add_argument("--limit", type=int, default=100)
    p.set_defaults(func=cmd_queue)

    p = sub.add_parser("show", help="Show one publication with its attempts")
    p.add_argument("publication", type=int)
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("errors", help="List failures and ambiguous records")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_errors)

    p = sub.add_parser(
        "reconcile",
        help="Close a needs_review record after checking the real account",
    )
    p.add_argument("publication", type=int)
    p.add_argument("--published", metavar="THREADS_POST_ID")
    p.add_argument("--retry", action="store_true")
    p.add_argument("--cancel", action="store_true")
    p.add_argument(
        "--confirm-not-published",
        action="store_true",
        help="Required with --retry: you verified no post was created",
    )
    p.add_argument("--at", help="When to retry (default: now)")
    p.add_argument("--reset-attempts", action="store_true")
    p.add_argument("--note")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_reconcile)

    p = sub.add_parser("schedule", help="Schedule or reschedule a record")
    p.add_argument("publication", type=int)
    p.add_argument(
        "--at",
        required=True,
        help=(
            "ISO time, 'now', or '+15m'. A naive time is read in the "
            "display timezone, never as UTC."
        ),
    )
    p.add_argument("--reset-attempts", action="store_true")
    p.add_argument("--note")
    p.set_defaults(func=cmd_schedule)

    p = sub.add_parser(
        "approve", help="Record that a human reviewed this exact content"
    )
    p.add_argument("publication", type=int)
    p.add_argument("--note")
    p.set_defaults(func=cmd_approve)

    p = sub.add_parser(
        "dry-run", help="Run one worker cycle with publishing disabled"
    )
    p.add_argument("--heartbeat")
    p.set_defaults(func=cmd_dry_run)

    p = sub.add_parser(
        "publish-now",
        help="Publish one record for real (requires --live and confirmation)",
    )
    p.add_argument("publication", type=int)
    p.add_argument("--live", action="store_true")
    p.add_argument("--confirm-reviewed", action="store_true")
    p.set_defaults(func=cmd_publish_now)

    p = sub.add_parser(
        "token-check",
        help="Verify the Threads credentials read-only (publishes nothing)",
    )
    p.add_argument(
        "--project",
        action="store_true",
        help="Also list the most recent posts on the account",
    )
    p.set_defaults(func=cmd_token_check)

    p = sub.add_parser(
        "import-json", help="Import a legacy JSON queue (idempotent)"
    )
    p.add_argument("path")
    p.add_argument("--display-name")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser(
        "recover", help="Release expired leases, quarantine crashed rows"
    )
    p.set_defaults(func=cmd_recover)

    p = sub.add_parser("audit", help="Show the audit log")
    p.add_argument("--limit", type=int, default=40)
    p.set_defaults(func=cmd_audit)

    p = sub.add_parser("stats", help="Queue counters")
    p.set_defaults(func=cmd_stats)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    settings = get_settings()
    setup_logging(settings)

    return int(args.func(args, settings))


if __name__ == "__main__":
    sys.exit(main())
