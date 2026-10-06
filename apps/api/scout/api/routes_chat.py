"""Chat operator routes: threads, history, streaming messages (SSE), confirmations."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import orjson
import sqlalchemy as sa
from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field
from sse_starlette.sse import EventSourceResponse

import scout.chat.local_router  # noqa: F401  (registers the deterministic router)
from scout.api.deps import Ctx
from scout.chat import operator
from scout.chat.context import UIContext
from scout.chat.tools import TOOLS, json_schema_for
from scout.db.engine import session_scope
from scout.db.models import ChatMessage, ChatThread
from scout.errors import NotFound

router = APIRouter(tags=["chat"])


class MessageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=8000)
    thread_id: uuid.UUID | None = None
    context: UIContext = Field(default_factory=UIContext)
    # Structured answers to a clarification card (the content is their readable summary).
    clarification: operator.ClarificationIn | None = None


class LaunchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    context: UIContext = Field(default_factory=UIContext)
    target: int | None = Field(default=None, ge=1, le=100_000)


class ConfirmIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approve: bool = True
    context: UIContext = Field(default_factory=UIContext)


@router.get("/chat/threads")
async def threads(ctx: Ctx, list_id: uuid.UUID | None = None, limit: int = 30) -> list[dict[str, Any]]:
    async with session_scope() as s:
        q = sa.select(ChatThread).where(ChatThread.workspace_id == ctx.workspace_id)
        if list_id:
            q = q.where(ChatThread.list_id == list_id)
        rows = (await s.scalars(q.order_by(ChatThread.updated_at.desc()).limit(min(limit, 100)))).all()
        return [
            {"id": t.id, "title": t.title, "list_id": t.list_id, "updated_at": t.updated_at} for t in rows
        ]


@router.get("/chat/threads/{thread_id}/messages")
async def messages(thread_id: uuid.UUID, ctx: Ctx) -> list[dict[str, Any]]:
    async with session_scope() as s:
        t = await s.get(ChatThread, thread_id)
        if t is None or t.workspace_id != ctx.workspace_id:
            raise NotFound("Thread not found")
        rows = (
            await s.scalars(
                sa.select(ChatMessage)
                .where(ChatMessage.thread_id == thread_id)
                .order_by(ChatMessage.created_at)
            )
        ).all()
        return [
            {"id": m.id, "role": m.role, "content": m.content, "parts": m.parts, "created_at": m.created_at}
            for m in rows
        ]


@router.post("/chat/messages")
async def send(body: MessageIn, ctx: Ctx) -> EventSourceResponse:
    thread = await operator.get_or_create_thread(ctx, body.thread_id, body.context.list_id)

    async def gen() -> AsyncIterator[dict[str, str]]:
        async for event, data in operator.run_turn(
            ctx, thread, body.content, body.context, clarification=body.clarification
        ):
            yield {"event": event, "data": orjson.dumps(data, default=str).decode()}

    return EventSourceResponse(
        gen(), ping=10, headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"}
    )


@router.post("/chat/actions/{action_id}/confirm")
async def confirm(action_id: uuid.UUID, body: ConfirmIn, ctx: Ctx) -> dict[str, Any]:
    return await operator.confirm_action(ctx, action_id, body.context, approve=body.approve)


@router.post("/chat/actions/{action_id}/launch")
async def launch(action_id: uuid.UUID, body: LaunchIn, ctx: Ctx) -> dict[str, Any]:
    """Launch the search prepared by a plan card ("Here is what I'll search" → Launch). Idempotent."""
    return await operator.launch_plan(ctx, action_id, body.context, target=body.target)


@router.get("/meta/tools", tags=["meta"])
async def tools() -> list[dict[str, Any]]:
    return [
        {
            "name": t.name,
            "title": t.title,
            "description": t.description,
            "parameters": json_schema_for(t.args),
            "requires_confirmation": t.needs_confirmation is not None,
        }
        for t in TOOLS.values()
    ]
