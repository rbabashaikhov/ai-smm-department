"""Container healthcheck: no HTTP port, no public surface.

Healthy means: the database answers, and the worker completed a cycle
recently enough. A worker that is alive but wedged therefore fails the check.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

from ai_smm.config import get_settings
from ai_smm.db.session import session_scope
from ai_smm.worker import HEARTBEAT_PATH


def main() -> int:
    settings = get_settings()

    try:
        with session_scope(settings) as session:
            from ai_smm.db.session import check_database

            check_database(session)
    except Exception as exc:
        print(f"unhealthy: database check failed: {type(exc).__name__}")

        return 1

    heartbeat = Path(HEARTBEAT_PATH)

    if not heartbeat.is_file():
        # The first cycle has not finished yet; start_period covers this.
        print("starting: no heartbeat yet")

        return 0

    try:
        beat = datetime.fromisoformat(
            heartbeat.read_text(encoding="utf-8").strip()
        )
    except (OSError, ValueError) as exc:
        print(f"unhealthy: unreadable heartbeat: {type(exc).__name__}")

        return 1

    if beat.tzinfo is None:
        beat = beat.replace(tzinfo=timezone.utc)

    age = (datetime.now(timezone.utc) - beat).total_seconds()
    allowed = settings.poll_interval_seconds * settings.health_stale_factor

    if age > allowed:
        print(f"unhealthy: last cycle {int(age)}s ago, allowed {allowed}s")

        return 1

    print(f"healthy: last cycle {int(age)}s ago")

    return 0


if __name__ == "__main__":
    sys.exit(main())
