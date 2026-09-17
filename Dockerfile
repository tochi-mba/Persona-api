# syntax=docker/dockerfile:1

# A plain slim base. This service makes one kind of outbound call -- fetching keyring's
# public keys -- and writes one small file. There is nothing else in it to go wrong.
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_FROZEN=1 \
    VIRTUAL_ENV=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Dependencies first, in their own layer: application edits then rebuild in seconds
# rather than re-resolving the whole tree.
COPY pyproject.toml uv.lock README.md ./
# git: uv fetches the family's client packages from tagged git sources.
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
# The token exists only for this RUN, in git's process environment, never a layer.
# Without a secret, public sources are fetched anonymously.
RUN --mount=type=secret,id=github_token,required=false \
    if [ -s /run/secrets/github_token ]; then \
        export GIT_CONFIG_COUNT=1 \
          GIT_CONFIG_KEY_0="url.https://x-access-token:$(cat /run/secrets/github_token)@github.com/.insteadOf" \
          GIT_CONFIG_VALUE_0="https://github.com/"; \
    fi \
    && uv sync --no-install-project --no-dev

COPY src/ src/
RUN --mount=type=secret,id=github_token,required=false \
    if [ -s /run/secrets/github_token ]; then \
        export GIT_CONFIG_COUNT=1 \
          GIT_CONFIG_KEY_0="url.https://x-access-token:$(cat /run/secrets/github_token)@github.com/.insteadOf" \
          GIT_CONFIG_VALUE_0="https://github.com/"; \
    fi \
    && uv sync --no-dev

# The persona database is written at runtime and must not live in the image layers.
# 0700 because the contents are not encrypted -- unlike keyring, there is no second
# layer here, so the directory mode is doing real work rather than defence in depth.
RUN useradd --create-home --uid 10001 persona \
    && mkdir -p /var/lib/persona \
    && chown -R persona:persona /var/lib/persona /app \
    && chmod 700 /var/lib/persona
VOLUME ["/var/lib/persona"]

USER persona

ENV PERSONA_HOST=0.0.0.0 \
    PERSONA_PORT=8004 \
    PERSONA_DATABASE_PATH=/var/lib/persona/persona.db \
    PERSONA_LOG_FORMAT=json

EXPOSE 8004

# PERSONA_KEYRING_ISSUER and PERSONA_KEYRING_JWKS_URL are deliberately NOT set here.
# They name the keyring this deployment trusts, and baking a default into the image is
# how a container ends up trusting the wrong one.

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8004/healthy', timeout=4).status == 200 else 1)"

CMD ["persona-api"]
