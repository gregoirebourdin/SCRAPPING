"""Job handler registry. Handlers: `async def handler(ctx: JobContext) -> dict | None`."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import sqlalchemy as sa

from scout.db.engine import session_scope
from scout.db.enums import CampaignStatus
from scout.db.models import Campaign
from scout.errors import CampaignPaused
from scout.jobs.events import emit


@dataclass
class JobContext:
    job_id: uuid.UUID
    workspace_id: uuid.UUID
    campaign_id: uuid.UUID | None
    type: str
    payload: dict[str, Any]
    attempt: int
    worker_id: str
    reschedule_after: float | None = None  # set by handler to re-run later without consuming an attempt
    reschedule_payload: dict[str, Any] | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    async def emit(self, type_: str, payload: dict[str, Any] | None = None) -> None:
        await emit(self.workspace_id, type_, payload or {}, campaign_id=self.campaign_id, job_id=self.job_id)

    async def campaign_status(self) -> CampaignStatus | None:
        if not self.campaign_id:
            return None
        async with session_scope() as s:
            return await s.scalar(sa.select(Campaign.status).where(Campaign.id == self.campaign_id))

    async def checkpoint(self) -> None:
        """Cooperative pause/cancel point between pipeline stages."""
        status = await self.campaign_status()
        if status is not None and status not in (CampaignStatus.planning, CampaignStatus.running):
            raise CampaignPaused(str(status))

    def later(self, delay_s: float, payload: dict[str, Any] | None = None) -> None:
        self.reschedule_after = delay_s
        self.reschedule_payload = payload


Handler = Callable[[JobContext], Awaitable[dict[str, Any] | None]]


@dataclass
class HandlerSpec:
    type: str
    fn: Handler
    timeout_s: float
    max_concurrency: int | None


_handlers: dict[str, HandlerSpec] = {}


def job_handler(type_: str, *, timeout_s: float = 600.0, max_concurrency: int | None = None) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        _handlers[type_] = HandlerSpec(type=type_, fn=fn, timeout_s=timeout_s, max_concurrency=max_concurrency)
        return fn

    return deco


def get_handler(type_: str) -> HandlerSpec | None:
    return _handlers.get(type_)


def handler_types() -> list[str]:
    return sorted(_handlers)
