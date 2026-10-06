"""Empirical Source Scoring: learned reliability per resolver / source (admin "Sources & health" page)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from scout.api.deps import Ctx
from scout.db.engine import session_scope
from scout.db.enums import MemberRole
from scout.learning.report import source_reliability

router = APIRouter(tags=["activity"])


@router.get("/learning/sources")
async def learning_sources(ctx: Ctx, dimension: str | None = None) -> list[dict[str, Any]]:
    """Attempts, coverage, confirmed correct/wrong, smoothed + raw precision (Wilson 90 % interval), latency,
    cost, prior and evidence level for every (dimension, key) — learned rows plus prior-only keys.

    ``dimension``: ``discovery.source`` | ``people.source`` | ``enrich.resolver`` | ``search.engine`` |
    ``crawl.tier`` | email ``resolver`` / ``source`` / ``pattern`` / ``technique`` / ``provider``. Counters are
    global (aggregated across workspaces, no lead data). Admin only.
    """
    ctx.require(MemberRole.admin)
    async with session_scope() as s:
        return await source_reliability(s, (dimension or "").strip() or None)
