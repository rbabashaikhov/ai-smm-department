"""Langfuse tracing -- on only when it is deliberately configured (SMM-035C).

The Langfuse SDK treats an EMPTY key as a key: it checks `is None`, so with
LANGFUSE_PUBLIC_KEY="" and LANGFUSE_SECRET_KEY="" its client is enabled and
exports OTLP spans, with an empty Basic credential, to its built-in default
https://cloud.langfuse.com. That is where the generation worker's
"Failed to export spans batch code: 401" came from (PRJ-013 W4-C and R1):
compose.generation.yml sets both keys to "" to mean "no tracing".

Here tracing is on only when LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY and an
endpoint are all non-empty and LANGFUSE_TRACING_ENABLED is unset or "true".
The endpoint is LANGFUSE_BASE_URL, else the SDK's older LANGFUSE_HOST; with
neither there is no tracing -- the SDK's default endpoint is never used.
Otherwise no Langfuse client is created at all: observe() calls the plain
function, langchain_callbacks() is empty, propagate_attributes() and flush()
do nothing, so nothing is exported. LANGFUSE_TRACING_ENABLED="false" turns
it off on purpose; a partial configuration, or any other value of the flag,
is logged (names only) and also means off.

The decision is made once, at the first traced call: after an entry point
has loaded its environment (a local .env included), never at import.
"""
from __future__ import annotations

import contextlib
import functools
import os
from collections.abc import Callable, Mapping
from typing import Any, TypeVar

from ai_smm.logging_setup import get_logger


logger = get_logger(__name__)

F = TypeVar("F", bound=Callable[..., Any])

KEY_ENV = ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY")
#: In order of precedence; the first non-empty one is the endpoint.
ENDPOINT_ENV = ("LANGFUSE_BASE_URL", "LANGFUSE_HOST")
FLAG_ENV = "LANGFUSE_TRACING_ENABLED"


def tracing_config(environ: Mapping[str, str]) -> dict[str, str] | None:
    """The Langfuse client arguments, or None when tracing must stay off."""

    flag = environ.get(FLAG_ENV, "").strip().lower()

    if flag == "false":
        return None

    if flag not in ("", "true"):
        logger.warning(
            "Langfuse tracing is off: %s must be 'true' or 'false'", FLAG_ENV
        )
        return None

    values = {name: environ.get(name, "").strip() for name in KEY_ENV}
    endpoint = next(
        (url for url in (environ.get(n, "").strip() for n in ENDPOINT_ENV) if url),
        "",
    )
    missing = [name for name, value in values.items() if not value]

    if not endpoint:
        missing.append(" or ".join(ENDPOINT_ENV))

    if len(missing) == len(KEY_ENV) + 1 and not flag:
        return None  # not configured at all: off, nothing to report

    if missing:
        logger.warning(
            "Langfuse tracing is off: %s not set",
            ", ".join(missing),
        )
        return None

    return {
        "public_key": values["LANGFUSE_PUBLIC_KEY"],
        "secret_key": values["LANGFUSE_SECRET_KEY"],
        "base_url": endpoint,
    }


@functools.cache
def _handler() -> Any | None:
    """The process's one LangChain handler; None when tracing is off."""

    config = tracing_config(os.environ)

    if config is None:
        return None

    from langfuse import Langfuse
    from langfuse.langchain import CallbackHandler

    # The first client is the SDK's singleton: observe() and the handler
    # both use it, so every span goes to this base URL with these keys.
    Langfuse(**config)
    logger.info("Langfuse tracing is on: %s", config["base_url"])

    return CallbackHandler()


def tracing_enabled() -> bool:
    return _handler() is not None


def langchain_callbacks() -> list[Any]:
    """Callbacks for an LLM call: the Langfuse handler, or none."""

    handler = _handler()

    return [] if handler is None else [handler]


def observe(*, name: str, as_type: str) -> Callable[[F], F]:
    """langfuse.observe, applied only while tracing is on."""

    from langfuse import observe as langfuse_observe

    def decorate(func: F) -> F:
        traced = langfuse_observe(name=name, as_type=as_type)(func)

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if tracing_enabled():
                return traced(*args, **kwargs)

            return func(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorate


def propagate_attributes(**attributes: Any) -> contextlib.AbstractContextManager[Any]:
    """langfuse.propagate_attributes while tracing is on, else nothing."""

    if not tracing_enabled():
        return contextlib.nullcontext()

    from langfuse import propagate_attributes as langfuse_propagate

    return langfuse_propagate(**attributes)


def flush() -> None:
    """Send what is buffered -- never creates a client to do it."""

    if not tracing_enabled():
        return

    from langfuse import get_client

    get_client().flush()
