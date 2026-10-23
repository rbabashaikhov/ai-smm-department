"""Fail closed before a disposable browser helper opens any connection."""

from __future__ import annotations

import os
import sys
from pathlib import Path


# An editable install may otherwise silently import a different checkout.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))


def local_settings():
    from sqlalchemy.engine import make_url

    from ai_smm.config import load_settings

    if os.getenv("AI_SMM_LOCAL_IDEATION") != "1":
        raise RuntimeError("set AI_SMM_LOCAL_IDEATION=1 for disposable browser helpers")
    settings = load_settings()
    url = make_url(settings.require_database_url())
    if url.host not in {"127.0.0.1", "localhost"} or not any(
        marker in (url.database or "").lower() for marker in ("test", "e2e")
    ):
        raise RuntimeError(
            "browser helpers require a loopback disposable test/e2e database"
        )
    return settings


def block_external_connections(settings):
    """Permit only this helper's PostgreSQL destination, through Python sockets."""
    from sqlalchemy.engine import make_url

    url = make_url(settings.require_database_url())
    port = url.port or 5432

    def guard(event, args):
        if event == "socket.connect":
            address = args[1]
            if (
                not isinstance(address, tuple)
                or address[0] not in {"127.0.0.1", "::1"}
                or address[1] != port
            ):
                raise RuntimeError(
                    "browser fake workers may connect only to disposable PostgreSQL"
                )

    sys.addaudithook(guard)
