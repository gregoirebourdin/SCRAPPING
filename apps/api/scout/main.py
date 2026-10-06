"""FastAPI application factory. Runs the REST API, the chat operator and (optionally) in-process workers."""

from __future__ import annotations

import importlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import ORJSONResponse

from scout.config import get_settings
from scout.db.engine import dispose_engine
from scout.errors import AppError
from scout.logging import configure_logging

log = structlog.get_logger("app")

# Modules that register job handlers (import side effects).
HANDLER_MODULES = [
    "scout.pipeline.jobs",
    "scout.pipeline.processor",
    "scout.pipeline.maintenance",
    "scout.services.imports",
    "scout.email.jobs",
    "scout.enrich.jobs",
]


def load_handlers() -> list[str]:
    loaded = []
    for mod in HANDLER_MODULES:
        try:
            importlib.import_module(mod)
            loaded.append(mod)
        except ModuleNotFoundError as exc:
            log.warning("handlers.missing", module=mod, error=str(exc))
    return loaded


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    s = get_settings()
    configure_logging(s.log_level, s.log_json)
    load_handlers()
    try:
        from scout.discovery.catalog import seed_sources  # type: ignore[import-not-found]

        await seed_sources()
    except Exception as exc:  # never block startup
        log.warning("sources.seed_failed", error=str(exc))
    worker = None
    if s.worker_enabled:
        from scout.jobs.worker import Worker

        worker = Worker()
        await worker.start()
        app.state.worker = worker
    log.info("app.started", env=s.app_env, ai=s.resolved_ai_provider, worker=bool(worker))
    try:
        yield
    finally:
        if worker is not None:
            await worker.stop()
        try:
            from scout.crawl.http import close_client  # type: ignore[import-not-found]

            await close_client()
        except Exception:
            pass
        await dispose_engine()


def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(
        title="Scout API",
        version="0.1.0",
        description="AI-native B2B lead intelligence — REST API (SSE for progress and chat).",
        default_response_class=ORJSONResponse,
        lifespan=lifespan,
        openapi_url="/v1/openapi.json",
        docs_url="/v1/docs" if not s.is_production else None,
        redoc_url=None,
    )
    app.add_middleware(
        CORSMiddleware, allow_origins=s.cors_origins, allow_credentials=True, allow_methods=["*"], allow_headers=["*"]
    )

    @app.exception_handler(AppError)
    async def app_error(_: Request, exc: AppError) -> ORJSONResponse:
        return ORJSONResponse(exc.to_dict(), status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> ORJSONResponse:
        errs = [{"loc": list(e.get("loc", [])), "msg": e.get("msg")} for e in exc.errors()[:10]]
        return ORJSONResponse({"error": {"code": "validation_failed", "message": "Invalid request", "details": errs}}, status_code=422)

    from scout.api import (
        routes_activity,
        routes_campaigns,
        routes_chat,
        routes_columns,
        routes_core,
        routes_io,
        routes_leads,
        routes_lists,
    )

    for r in (routes_core, routes_lists, routes_leads, routes_campaigns, routes_columns, routes_io, routes_activity, routes_chat):
        app.include_router(r.router, prefix="/v1")
    return app


app = create_app()
