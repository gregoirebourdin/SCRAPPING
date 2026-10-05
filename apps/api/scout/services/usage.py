"""Cost & usage tracking (spec §95) and budget guards (spec §96).

Pipeline code records usage through `record_usage(...)`; the active workspace/campaign/job are
taken from a context variable set by the job worker (`usage_scope`).
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from decimal import Decimal

import sqlalchemy as sa

from scout.db.engine import session_scope
from scout.db.enums import UsageCategory
from scout.db.models import CampaignStats, UsageEvent, Workspace


@dataclass(frozen=True)
class UsageContext:
    workspace_id: uuid.UUID
    campaign_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None


_ctx: ContextVar[UsageContext | None] = ContextVar("scout_usage_ctx", default=None)


@contextmanager
def usage_scope(ctx: UsageContext) -> Iterator[None]:
    token = _ctx.set(ctx)
    try:
        yield
    finally:
        _ctx.reset(token)


def current_usage_context() -> UsageContext | None:
    return _ctx.get()


async def record_usage(
    category: UsageCategory,
    *,
    cost_usd: float = 0.0,
    quantity: int = 1,
    tokens_in: int = 0,
    tokens_out: int = 0,
    model: str | None = None,
    resolver: str | None = None,
    source_key: str | None = None,
    ctx: UsageContext | None = None,
) -> None:
    ctx = ctx or _ctx.get()
    if ctx is None:
        return  # outside any workspace scope (e.g. health checks)
    async with session_scope() as s:
        s.add(
            UsageEvent(
                workspace_id=ctx.workspace_id,
                campaign_id=ctx.campaign_id,
                job_id=ctx.job_id,
                category=category,
                resolver=resolver,
                model=model,
                source_key=source_key,
                quantity=quantity,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                estimated_cost_usd=Decimal(str(round(cost_usd, 6))),
            )
        )
        if ctx.campaign_id and cost_usd:
            await s.execute(
                sa.update(CampaignStats)
                .where(CampaignStats.campaign_id == ctx.campaign_id)
                .values(cost_usd=CampaignStats.cost_usd + Decimal(str(round(cost_usd, 6))))
            )
    _budget_cache.pop(ctx.workspace_id, None) if cost_usd > 0.05 else None


# ---- budget guards --------------------------------------------------------------------------

_budget_cache: dict[uuid.UUID, tuple[float, BudgetState]] = {}


@dataclass(frozen=True)
class BudgetState:
    month_spend_usd: float
    monthly_budget_usd: float
    hard_cap: bool

    @property
    def ratio(self) -> float:
        return self.month_spend_usd / self.monthly_budget_usd if self.monthly_budget_usd > 0 else 0.0

    @property
    def exceeded(self) -> bool:
        return self.hard_cap and self.month_spend_usd >= self.monthly_budget_usd

    @property
    def near_limit(self) -> bool:
        """≥ 85 %: reduce expensive fallbacks (grounded search, browser)."""
        return self.ratio >= 0.85


async def workspace_budget(workspace_id: uuid.UUID, *, max_age_s: float = 10.0) -> BudgetState:
    cached = _budget_cache.get(workspace_id)
    now = time.monotonic()
    if cached and now - cached[0] < max_age_s:
        return cached[1]
    async with session_scope() as s:
        ws = await s.get(Workspace, workspace_id)
        spend = await s.scalar(
            sa.select(sa.func.coalesce(sa.func.sum(UsageEvent.estimated_cost_usd), 0)).where(
                UsageEvent.workspace_id == workspace_id,
                UsageEvent.created_at >= sa.func.date_trunc("month", sa.func.now()),
            )
        )
    state = BudgetState(
        month_spend_usd=float(spend or 0),
        monthly_budget_usd=float(ws.monthly_budget_usd) if ws else 30.0,
        hard_cap=bool(ws.hard_budget_cap) if ws else True,
    )
    _budget_cache[workspace_id] = (now, state)
    return state


async def allow_expensive(ctx: UsageContext | None = None) -> bool:
    """False when the workspace is near its budget: callers must skip expensive fallbacks."""
    ctx = ctx or _ctx.get()
    if ctx is None:
        return True
    state = await workspace_budget(ctx.workspace_id)
    return not state.near_limit
