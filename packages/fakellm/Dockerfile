# syntax=docker/dockerfile:1
# The fake model server as an image, for the end-to-end tests: there it stands in for
# Ollama, as one more container of the stack (compose.e2e.yaml). No installation ships
# it. Built like the API's image, from the repository root:
#   docker build --file packages/fakellm/Dockerfile .

FROM ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 AS uv

FROM python:3.13.18-slim-trixie@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON_PREFERENCE=only-system \
    UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /repo

# Dependencies first. --locked checks uv.lock against every workspace member, so all
# manifests are copied.
COPY pyproject.toml uv.lock .python-version ./
COPY apps/api/pyproject.toml apps/api/
COPY packages/fakellm/pyproject.toml packages/fakellm/
COPY packages/llm/pyproject.toml packages/llm/
COPY packages/quota/pyproject.toml packages/quota/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --package actio-fakellm --no-install-workspace

COPY packages/fakellm/README.md packages/fakellm/
COPY packages/fakellm/src packages/fakellm/src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --package actio-fakellm --no-editable

FROM python:3.13.18-slim-trixie@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS runtime
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin fakellm
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
USER fakellm
EXPOSE 8100
CMD ["fakellm", "--host", "0.0.0.0", "--port", "8100"]
