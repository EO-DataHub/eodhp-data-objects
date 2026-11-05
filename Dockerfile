# syntax=docker/dockerfile:1@sha256:ecfaec9ed6d810b56388c508f4121597bfbba70d41a6dfeee4d8cad5f295fc32
FROM ghcr.io/astral-sh/uv:python3.13-trixie-slim@sha256:f4adb3fe11f03f693466c36afd17b43ffd11eb9df3cd4f7a9337cf2e6ec4c8e8 AS builder

ENV UV_NO_DEV=1 UV_COMPILE_BYTECODE=1

WORKDIR /app

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-project

COPY pyproject.toml uv.lock README.md LICENSE ./
COPY data_objects ./data_objects

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen


FROM ghcr.io/astral-sh/uv:python3.13-trixie-slim@sha256:f4adb3fe11f03f693466c36afd17b43ffd11eb9df3cd4f7a9337cf2e6ec4c8e8

RUN apt-get update -y && apt-get upgrade -y && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 app

# The virtualenv's own binaries, not `uv run`: the app user has no home directory for uv's
# cache, and the environment is complete at build time.
ENV PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY --from=builder --chown=app:app /app /app

USER 10001

EXPOSE 8000

# --proxy-headers: the access log records the client's address from X-Forwarded-For, as set
# by the ingress controller, not the controller's own address.
# --no-access-log: the app writes its own JSON access line for each object request, and
# uvicorn's would add a second, plain-text line, plus one for every health probe.
CMD ["uvicorn", "data_objects.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*", "--no-access-log"]
