# syntax=docker/dockerfile:1.7
#
# AI SMM Department runtime image.
#
# Two stages so that uv, build tooling and the lockfile resolution never ship
# to production. Base images are pinned by digest: a moving tag would make a
# rebuild non-reproducible, and "latest" is forbidden for this service.

# python:3.12-slim-bookworm == Python 3.12.15, pinned by digest
FROM python@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258 AS builder

# uv 0.12.17 — the same version used to produce uv.lock.
COPY --from=ghcr.io/astral-sh/uv@sha256:10787c682e4184e4f290de1171fd4703dc63de99221f10fe1c99002ce7fa9acc /uv /usr/local/bin/uv

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first: this layer is reused whenever only source changes.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini

# Install the project itself into the same virtualenv.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


FROM python@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258 AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=UTC \
    PATH="/app/.venv/bin:$PATH" \
    AI_SMM_KNOWLEDGE_ROOT=/srv/ai-smm/knowledge \
    AI_SMM_MEDIA_ROOT=/srv/ai-smm \
    STORAGE_LOCAL_ROOT=/srv/ai-smm/media

# A fixed uid/gid so a bind-mounted file's ownership is predictable.
RUN groupadd --gid 10001 aismm \
 && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin aismm

WORKDIR /app

COPY --from=builder --chown=root:root /app/.venv /app/.venv
COPY --from=builder --chown=root:root /app/src /app/src
COPY --from=builder --chown=root:root /app/alembic /app/alembic
COPY --from=builder --chown=root:root /app/alembic.ini /app/alembic.ini

# Mount points: the knowledge base (read-only) and the media directory
# the public storage serves from (the only writable path at runtime).
RUN mkdir -p /srv/ai-smm/knowledge /srv/ai-smm/media \
 && chown -R root:root /srv/ai-smm

# Nothing in the image is writable by the runtime user; the container also
# runs with read_only: true and a tmpfs for /tmp.
USER 10001:10001

# No EXPOSE: this service never listens on a port.

HEALTHCHECK --interval=60s --timeout=10s --start-period=45s --retries=3 \
    CMD ["/app/.venv/bin/ai-smm-healthcheck"]

# PID 1 is the worker itself so it receives SIGTERM directly and can finish
# the current cycle before exiting.
ENTRYPOINT ["/app/.venv/bin/ai-smm-worker"]
