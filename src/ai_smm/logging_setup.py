"""Structured logging with mandatory secret redaction.

Every record passes through RedactingFilter before it reaches a handler, so a
secret cannot be logged even by code that is unaware of this module (the
Threads client used to print raw API response bodies). Redaction works on the
formatted message, the arguments and the exception text.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from typing import Any

from ai_smm.config import Settings, get_settings


MASK = "***REDACTED***"

# Token shapes that must be masked even when the value is not in the
# environment of this process (for example a token pasted into an API
# response body, or a key read from a file).
_PATTERN_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    # OpenAI style keys.
    ("openai", re.compile(r"sk-[A-Za-z0-9_\-]{12,}")),
    # Meta / Threads long-lived tokens.
    ("meta", re.compile(r"\bTHQ[A-Za-z0-9_\-]{12,}")),
    # Langfuse keys.
    ("langfuse", re.compile(r"\b(?:pk|sk)-lf-[A-Za-z0-9_\-]{8,}")),
    # Bearer headers.
    (
        "bearer",
        re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]{8,}", re.IGNORECASE),
    ),
    # Credentials inside a URL, e.g. postgresql://user:password@host/db
    (
        "url_password",
        re.compile(r"(://[^/\s:@]+:)[^@/\s]+(@)"),
    ),
    # key=value and "key": "value" for sensitive names. The optional quote
    # after the key name is what makes a JSON body match, e.g.
    # {"error":{"token":"..."}} as returned by the Threads API.
    (
        "assignment",
        re.compile(
            r"(?i)\b("
            r"access_token|api_key|apikey|password|passwd|secret|"
            r"secret_key|token|authorization|private_key"
            r")\b(\\?[\"']?\s*[=:]\s*\\?[\"']?)([^\s\"'\\,;)}\]]+)"
        ),
    ),
)


def _mask_patterns(text: str) -> str:
    for name, pattern in _PATTERN_RULES:
        if name == "bearer":
            text = pattern.sub(rf"\1{MASK}", text)
        elif name == "url_password":
            text = pattern.sub(rf"\1{MASK}\2", text)
        elif name == "assignment":
            text = pattern.sub(rf"\1\2{MASK}", text)
        else:
            text = pattern.sub(MASK, text)

    return text


def collect_secret_values(settings: Settings | None = None) -> list[str]:
    """Literal secret values present in this process environment."""

    settings = settings or get_settings()

    values: list[str] = []

    for name in settings.secret_env_names:
        value = os.getenv(name)

        if value and len(value.strip()) >= 8:
            values.append(value.strip())

    # Longest first, so a secret that contains another is masked whole.
    return sorted(set(values), key=len, reverse=True)


class RedactingFilter(logging.Filter):
    """Masks known secrets and secret-shaped substrings on every record."""

    def __init__(self, secret_values: list[str] | None = None) -> None:
        super().__init__()

        self._secret_values = (
            secret_values
            if secret_values is not None
            else collect_secret_values()
        )

    def refresh(self) -> None:
        self._secret_values = collect_secret_values()

    def redact(self, text: str) -> str:
        for secret in self._secret_values:
            if secret and secret in text:
                text = text.replace(secret, MASK)

        return _mask_patterns(text)

    def filter(self, record: logging.LogRecord) -> bool:
        # Render once, then store the redacted result as the message so no
        # handler can re-expand the original arguments.
        try:
            rendered = record.getMessage()
        except Exception:
            rendered = str(record.msg)

        record.msg = self.redact(rendered)
        record.args = ()

        # A traceback is normally rendered by the formatter, i.e. after this
        # filter would have run. Render it here so the secret inside an
        # exception message is masked too, and drop exc_info so no handler
        # can re-render the original.
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(
                record.exc_info
            )

        if record.exc_text:
            record.exc_text = self.redact(record.exc_text)
            record.exc_info = None

        if record.stack_info:
            record.stack_info = self.redact(record.stack_info)

        extra = getattr(record, "context", None)

        if isinstance(extra, dict):
            record.context = {
                key: (
                    self.redact(value)
                    if isinstance(value, str)
                    else value
                )
                for key, value in extra.items()
            }

        return True

    def redact_exception(self, exc: BaseException) -> str:
        return self.redact(f"{type(exc).__name__}: {exc}")


class JsonFormatter(logging.Formatter):
    _RESERVED = frozenset(
        logging.LogRecord("", 0, "", 0, "", (), None).__dict__
    ) | {"message", "asctime", "taskName"}

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        context = getattr(record, "context", None)

        if isinstance(context, dict):
            payload["context"] = context

        for key, value in record.__dict__.items():
            if key in self._RESERVED or key == "context":
                continue

            if key.startswith("_"):
                continue

            if isinstance(value, (str, int, float, bool)) or value is None:
                payload[key] = value

        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)

        if record.exc_text:
            payload["exception"] = record.exc_text

        return json.dumps(payload, ensure_ascii=False, default=str)


_redacting_filter: RedactingFilter | None = None


def get_redacting_filter() -> RedactingFilter:
    global _redacting_filter

    if _redacting_filter is None:
        _redacting_filter = RedactingFilter()

    return _redacting_filter


def setup_logging(settings: Settings | None = None) -> None:
    """Install redacting handlers on the root logger. Safe to call twice."""

    settings = settings or get_settings()

    redaction = get_redacting_filter()
    redaction.refresh()

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.addFilter(redaction)

    if settings.log_format == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)-8s %(name)s %(message)s"
            )
        )

    root = logging.getLogger()

    for existing in list(root.handlers):
        root.removeHandler(existing)

    root.addHandler(handler)
    root.setLevel(settings.log_level)
    root.addFilter(redaction)

    # Third-party loggers that are chatty or may echo request bodies.
    for noisy in ("httpx", "httpcore", "paramiko", "apscheduler.executors"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.addFilter(get_redacting_filter())

    return logger
