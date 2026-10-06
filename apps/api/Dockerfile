# syntax=docker/dockerfile:1
# Scout API + in-process Postgres job worker (FastAPI, Python 3.12, uv).
FROM python:3.12-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.8.17 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project
COPY scout ./scout
COPY migrations ./migrations
COPY alembic.ini ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev

FROM python:3.12-slim-bookworm
RUN useradd --create-home --uid 10001 scout
WORKDIR /app
COPY --from=build --chown=scout:scout /app /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 APP_ENV=production LOG_JSON=true PORT=8000
USER scout
EXPOSE 8000
# Migrations run as Railway's pre-deploy command (railway.json); locally: `alembic upgrade head`.
CMD ["sh", "-c", "exec uvicorn scout.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*' --timeout-graceful-shutdown 25"]
