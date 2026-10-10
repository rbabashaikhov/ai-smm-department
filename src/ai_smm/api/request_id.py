"""Request correlation.

Every request carries an id: the one the caller sent in X-Request-ID when
it is syntactically acceptable, otherwise a fresh one. The id goes into
request.state, into a context variable the logging and error paths read,
and back out in the response header.

Nothing about a request's credentials is recorded here: no cookie header,
no Authorization header, no body. The access log line is method, path,
status, duration and the id.
"""
from __future__ import annotations

import re
import time
import uuid
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from ai_smm.logging_setup import get_logger


REQUEST_ID_HEADER = "X-Request-ID"

#: An inbound id is echoed only if it looks like an id. Anything else is
#: replaced, so a caller cannot inject newlines or secrets into our logs.
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._\-]{1,64}$")

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)

logger = get_logger(__name__)


def current_request_id() -> str | None:
    return _request_id.get()


def new_request_id() -> str:
    return uuid.uuid4().hex


class RequestIdMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, *, access_log: bool = True) -> None:
        super().__init__(app)

        self._access_log = access_log

    async def dispatch(self, request: Request, call_next) -> Response:
        inbound = request.headers.get(REQUEST_ID_HEADER)
        request_id = (
            inbound
            if inbound and _SAFE_REQUEST_ID.match(inbound)
            else new_request_id()
        )

        request.state.request_id = request_id
        token = _request_id.set(request_id)
        started = time.monotonic()

        try:
            response = await call_next(request)
        finally:
            _request_id.reset(token)

        response.headers[REQUEST_ID_HEADER] = request_id

        if self._access_log:
            logger.info(
                "api request",
                extra={
                    "context": {
                        "request_id": request_id,
                        "method": request.method,
                        "path": request.url.path,
                        "status": response.status_code,
                        "duration_ms": int(
                            (time.monotonic() - started) * 1000
                        ),
                    }
                },
            )

        return response
