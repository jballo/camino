# syntax=docker/dockerfile:1.7
#
# Production image for the Camino API and job worker (one image, two commands).
#
#   docker build --platform linux/arm64 -t camino-backend:<git-sha> Backend
#
# API    (default CMD): uvicorn on 0.0.0.0:8000, RUN_WORKER=false
# worker:                python -m app.worker
#
# Configuration comes from environment variables only (see .env.example for
# the names). No .env file, key, or credential is copied into the image; the
# .dockerignore next to this file enforces that. Image tags should be the git
# SHA so a rollback is redeploying the previous tag.

# Exact patch version: a bare 3.14 tag moves with every Python release, so two
# builds of one commit could ship different interpreters.
ARG PYTHON_VERSION=3.14.10
ARG UV_VERSION=0.12.23
ARG DEBIAN_RELEASE=trixie

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

# Both stages start from this one image, so the interpreter the venv was built
# with is the one it runs on.
FROM python:${PYTHON_VERSION}-slim-${DEBIAN_RELEASE} AS python-base

# ---- build stage: resolve the locked dependencies into /opt/venv ------------
FROM python-base AS build

COPY --from=uv /uv /uvx /bin/

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    # The venv is copied to another stage, so hardlinks into the cache would break
    UV_LINK_MODE=copy \
    # Compile .pyc at build time: the runtime user cannot write to the venv
    UV_COMPILE_BYTECODE=1 \
    # The image's interpreter is the only one allowed
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first so code edits do not invalidate the dependency layer.
# --no-dev leaves pytest out; --no-install-project is a no-op for this virtual
# project but keeps the layer honest if a build backend is ever added.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    uv sync --frozen --no-dev --no-install-project

# ---- runtime stage ---------------------------------------------------------
FROM python-base AS runtime

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /app --shell /usr/sbin/nologin app

WORKDIR /app

# The venv's python symlink points at /usr/local/bin/python3, which exists in
# python-base.
COPY --from=build --chown=root:root /opt/venv /opt/venv

# Runtime code only (.dockerignore drops tests, eval fixtures, and secrets).
# Owned by root and read-only to the app user: a compromised process cannot
# rewrite its own code.
COPY --chown=root:root pyproject.toml uv.lock alembic.ini ./
COPY --chown=root:root migrations ./migrations
COPY --chown=root:root scripts ./scripts
COPY --chown=root:root app ./app

USER app

EXPOSE 8000

# Pinned production start command. Caddy is the only client of this port and
# the port is never published from the host, so trusting the proxy headers from
# any peer is safe; without --proxy-headers the app would see Caddy's address
# as every client's address.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*", "--no-server-header"]
