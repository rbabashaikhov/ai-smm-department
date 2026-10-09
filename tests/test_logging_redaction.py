"""No secret may reach a log record, whatever code produced it."""
from __future__ import annotations

import io
import json
import logging

import pytest

from ai_smm.logging_setup import (
    MASK,
    JsonFormatter,
    RedactingFilter,
    collect_secret_values,
)


@pytest.fixture
def captured() -> tuple[logging.Logger, io.StringIO, RedactingFilter]:
    stream = io.StringIO()
    redaction = RedactingFilter(
        secret_values=["super-secret-token-value", "db-password-1234"]
    )

    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(redaction)

    logger = logging.getLogger(f"test.redaction.{id(stream)}")
    logger.handlers = [handler]
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    return logger, stream, redaction


def _records(stream: io.StringIO) -> list[dict]:
    return [
        json.loads(line)
        for line in stream.getvalue().splitlines()
        if line.strip()
    ]


def test_known_secret_value_is_masked(captured) -> None:
    logger, stream, _ = captured

    logger.info("token is super-secret-token-value here")

    payload = _records(stream)[0]

    assert "super-secret-token-value" not in json.dumps(payload)
    assert MASK in payload["message"]


def test_secret_in_lazy_arguments_is_masked(captured) -> None:
    """Formatting happens inside the filter, so %s arguments are covered."""

    logger, stream, _ = captured

    logger.info("value=%s", "super-secret-token-value")

    assert "super-secret-token-value" not in stream.getvalue()


def test_secret_in_context_extra_is_masked(captured) -> None:
    logger, stream, _ = captured

    logger.info(
        "uploading",
        extra={"context": {"url": "https://x/?t=super-secret-token-value"}},
    )

    assert "super-secret-token-value" not in stream.getvalue()


def test_secret_in_exception_text_is_masked(captured) -> None:
    logger, stream, _ = captured

    try:
        raise RuntimeError("failed with token super-secret-token-value")
    except RuntimeError:
        logger.exception("call failed")

    assert "super-secret-token-value" not in stream.getvalue()


@pytest.mark.parametrize(
    "line",
    [
        "sk-abcdefghijklmnopqrstuvwxyz0123",
        "Authorization: Bearer abcdefghijklmnop",
        'access_token="THQWJabcdefghijklmnop"',
        "postgresql+psycopg://ai_smm:hunter2hunter2@db:5432/ai_smm",
        "pk-lf-12345678-aaaa-bbbb",
        '{"error":{"message":"bad","token":"abcdef123456"}}',
    ],
)
def test_secret_shaped_text_is_masked_without_knowing_the_value(
    captured, line: str
) -> None:
    """Covers tokens this process never had in its environment."""

    logger, stream, _ = captured

    logger.error("threads api error body=%s", line)

    output = stream.getvalue()

    assert MASK in output

    for leaked in (
        "abcdefghijklmnopqrstuvwxyz0123",
        "hunter2hunter2",
        "THQWJabcdefghijklmnop",
        "abcdef123456",
    ):
        assert leaked not in output


def test_threads_error_body_is_redacted_in_place() -> None:
    """The specific regression: the client used to print raw bodies."""

    redaction = RedactingFilter(secret_values=[])

    body = (
        '{"error":{"message":"Invalid OAuth access token",'
        '"access_token":"THQWJsecretvalue12345"}}'
    )

    redacted = redaction.redact(body)

    assert "THQWJsecretvalue12345" not in redacted
    assert MASK in redacted


def test_collect_secret_values_reads_configured_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("THREADS_ACCESS_TOKEN", "a-long-enough-token")
    monkeypatch.setenv("OPENAI_API_KEY", "short")

    values = collect_secret_values()

    assert "a-long-enough-token" in values
    # Too short to be a real secret; masking it would mangle normal text.
    assert "short" not in values


def test_non_secret_text_survives(captured) -> None:
    logger, stream, _ = captured

    logger.info(
        "Published",
        extra={"context": {"publication_id": 7, "format": "carousel"}},
    )

    payload = _records(stream)[0]

    assert payload["context"]["publication_id"] == 7
    assert payload["context"]["format"] == "carousel"
    assert payload["message"] == "Published"
