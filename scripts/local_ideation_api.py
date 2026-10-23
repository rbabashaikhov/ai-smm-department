"""Disposable-browser API only; never use this launcher for a deployment."""

from __future__ import annotations

from dataclasses import replace

import uvicorn
from local_browser_safety import local_settings

from ai_smm.api.app import create_app


def local_app():
    settings = local_settings()

    return create_app(
        replace(
            settings,
            api_mutations_enabled=True,
            api_cookie_secure=False,
            topic_ideation_jobs_enabled=True,
        ),
        access_log=False,
    )


if __name__ == "__main__":
    uvicorn.run(local_app(), host="127.0.0.1", port=8000, access_log=False)
