"""FastAPI application: ``uvicorn app.main:app --reload`` (or ``python -m app.cli serve``)."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import __version__
from .api.leads import router as leads_router
from .api.runs import router as runs_router
from .db import init_db
from .fetch.browser import get_browser
from .fetch.http import get_fetcher
from .pipeline.orchestrator import manager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await init_db()
    await get_fetcher().start()
    yield
    await manager.stop()
    await get_browser().close()
    await get_fetcher().close()


app = FastAPI(title="LeadForge", version=__version__, lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.include_router(runs_router)
app.include_router(leads_router)


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True, "version": __version__, "active_run": manager.run_id if manager.active else None}
