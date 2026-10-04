# One image for the worker, the CLI and bao-bootstrap. Lockfile-driven, non-root.
FROM python:3.13-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.22 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /src
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev

FROM python:3.13-slim
RUN useradd --create-home --uid 10001 app \
 && mkdir -p /bootstrap/init /bootstrap/approle /run/secrets/bao /data && chown -R app /bootstrap /data
COPY --from=build /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH PYTHONPATH=/app/src PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY src ./src
COPY data ./data
COPY openbao/policies ./openbao/policies
USER app
