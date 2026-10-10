"""One error shape for the whole API.

Every failure -- validation, authorisation, a missing row, an unexpected
exception -- leaves as:

    {"error": {"code", "message", "details", "request_id"}}

The message is written for the operator reading it, not derived from the
exception: a SQL fragment, a stack frame or a secret must never reach a
client. Unexpected exceptions are logged in full (through the redacting
filter) and answered with INTERNAL_ERROR and nothing else.
"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ai_smm.api.request_id import REQUEST_ID_HEADER, current_request_id
from ai_smm.logging_setup import get_logger


logger = get_logger(__name__)


class ErrorCode:
    AUTH_REQUIRED = "AUTH_REQUIRED"
    INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    VERSION_CONFLICT = "VERSION_CONFLICT"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
    #: CSRF is split in two so an operator can tell a client that never
    #: fetched a token from one that sent a stale one.
    CSRF_REQUIRED = "CSRF_REQUIRED"
    CSRF_INVALID = "CSRF_INVALID"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"


#: Fallback mapping for HTTPExceptions raised by the framework itself.
_STATUS_CODES: dict[int, str] = {
    400: ErrorCode.VALIDATION_ERROR,
    401: ErrorCode.AUTH_REQUIRED,
    403: ErrorCode.FORBIDDEN,
    404: ErrorCode.NOT_FOUND,
    405: ErrorCode.METHOD_NOT_ALLOWED,
    409: ErrorCode.VERSION_CONFLICT,
    422: ErrorCode.VALIDATION_ERROR,
    503: ErrorCode.DEPENDENCY_UNAVAILABLE,
}


class ApiError(Exception):
    """An error the API raises on purpose, with a code a client can switch on."""

    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)

        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}
        self.headers = headers or {}


def auth_required() -> ApiError:
    return ApiError(
        status_code=401,
        code=ErrorCode.AUTH_REQUIRED,
        message="Authentication is required.",
    )


def invalid_credentials() -> ApiError:
    # Deliberately identical for an unknown address, a wrong password and
    # a deactivated account: the response must not help enumerate users.
    return ApiError(
        status_code=401,
        code=ErrorCode.INVALID_CREDENTIALS,
        message="Invalid email or password.",
    )


def forbidden(message: str = "Insufficient role for this action.") -> ApiError:
    return ApiError(
        status_code=403, code=ErrorCode.FORBIDDEN, message=message
    )


def not_found(message: str = "Resource not found.") -> ApiError:
    return ApiError(
        status_code=404, code=ErrorCode.NOT_FOUND, message=message
    )


def error_payload(
    *,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details or {},
            "request_id": request_id or current_request_id(),
        }
    }


def error_response(
    *,
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
    request_id: str | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    resolved_id = request_id or current_request_id()

    response = JSONResponse(
        status_code=status_code,
        content=error_payload(
            code=code,
            message=message,
            details=details,
            request_id=resolved_id,
        ),
        headers=headers,
    )

    # Set here too: an unhandled exception is turned into a response
    # outside the middleware that would otherwise add the header.
    if resolved_id:
        response.headers[REQUEST_ID_HEADER] = resolved_id

    return response


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        return error_response(
            status_code=exc.status_code,
            code=exc.code,
            message=exc.message,
            details=exc.details,
            request_id=getattr(request.state, "request_id", None),
            headers=exc.headers or None,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return error_response(
            status_code=422,
            code=ErrorCode.VALIDATION_ERROR,
            message="The request body or parameters are invalid.",
            details={"fields": _safe_validation_details(exc)},
            request_id=getattr(request.state, "request_id", None),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        return error_response(
            status_code=exc.status_code,
            code=_STATUS_CODES.get(exc.status_code, ErrorCode.INTERNAL_ERROR),
            message=str(exc.detail) or "Request failed.",
            request_id=getattr(request.state, "request_id", None),
        )

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        request_id = getattr(request.state, "request_id", None)

        # The traceback goes to the log (redacted by RedactingFilter) and
        # the request id is the only thing the client gets to correlate.
        logger.exception(
            "Unhandled API error",
            extra={
                "context": {
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path,
                    "error_type": type(exc).__name__,
                }
            },
        )

        return error_response(
            status_code=500,
            code=ErrorCode.INTERNAL_ERROR,
            message="Internal server error.",
            request_id=request_id,
        )


def _safe_validation_details(
    exc: RequestValidationError,
) -> list[dict[str, Any]]:
    """Field locations and rule names only -- never the submitted value.

    A validation error on the login body would otherwise echo the
    password back to the client and into any proxy log.
    """

    fields: list[dict[str, Any]] = []

    for error in exc.errors():
        location = [
            part for part in error.get("loc", ()) if isinstance(part, str)
        ]
        fields.append(
            {
                "field": ".".join(location) or "body",
                "rule": str(error.get("type", "invalid")),
            }
        )

    return fields
