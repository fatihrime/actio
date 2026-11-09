# syntax=docker/dockerfile:1
# The API's production image: the `actio` package and its locked dependencies in a
# virtual environment, on the official Python image. Build from the repository root:
#   docker build --file apps/api/Dockerfile .

FROM ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 AS uv

FROM python:3.13.18-slim-trixie@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS build
COPY --from=uv /uv /usr/local/bin/uv
# Use this image's Python (pyproject.toml otherwise asks for a uv-managed one), compile
# bytecode now so the read-only container never writes any, and copy files out of uv's
# cache so the environment can move to the final stage.
ENV UV_PYTHON_PREFERENCE=only-system \
    UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /repo

# Dependencies first. This layer is reused until the lockfile or a manifest changes.
# --locked checks uv.lock against every workspace member, so all manifests are copied.
COPY pyproject.toml uv.lock .python-version ./
COPY apps/api/pyproject.toml apps/api/
COPY packages/fakellm/pyproject.toml packages/fakellm/
COPY packages/quota/pyproject.toml packages/quota/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --package actio --no-install-workspace

# Then the app itself, installed as a regular package: the source tree stays behind.
COPY apps/api/src apps/api/src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --package actio --no-editable

FROM python:3.13.18-slim-trixie@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS runtime
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin actio
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ACTIO_HOST=0.0.0.0
USER actio
EXPOSE 8000
CMD ["actio", "--role", "api"]
