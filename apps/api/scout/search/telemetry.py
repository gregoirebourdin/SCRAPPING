"""Search telemetry: usage events (cost, $0 for free engines) and empirical learning stats.

Learning stats go to ``scout.learning.stats`` when that module exists (optional dependency, imported lazily;
every call here is a no-op without it and never raises). Dimensions written by the search layer:

* ``search.engine`` — keys ``searxng`` / ``duckduckgo`` / ``gemini_grounded``; ``produced`` = the answer was
  sufficient (free engines) or accepted (Gemini).
* ``people.source`` — keys ``searxng_result`` / ``duckduckgo_result`` / ``gemini_grounded_result``;
  ``produced`` = at least one person was found by that source.
"""

from __future__ import annotations

import importlib
from typing import Any

import structlog

log = structlog.get_logger(__name__)

ENGINE_DIMENSION = "search.engine"
PEOPLE_DIMENSION = "people.source"
GEMINI_KEY = "gemini_grounded"


def stat(
    dimension: str,
    key: str,
    *,
    produced: bool | None = None,
    latency_ms: int = 0,
    cost_usd: float = 0.0,
    outcome: bool = False,
    correct: bool | None = None,
) -> dict[str, Any]:
    return {
        "dimension": dimension,
        "key": key,
        "correct": correct,
        "outcome": outcome,
        "produced": produced,
        "latency_ms": max(0, int(latency_ms)),
        "cost_usd": float(cost_usd),
    }


def _stats_module() -> Any | None:
    try:
        return importlib.import_module("scout.learning.stats")
    except ImportError:
        return None


async def record_stats(events: list[dict[str, Any]]) -> None:
    """Forward events to ``scout.learning.stats.record``.

    Skipped when the module is absent and outside a workspace usage scope (like the usage ledger: pipeline
    and enrichment jobs always run inside one; ad-hoc calls and unit tests do not touch the database).
    """
    from scout.services.usage import current_usage_context

    if not events or current_usage_context() is None:
        return
    mod = _stats_module()
    if mod is None:
        return
    try:
        await mod.record([mod.StatEvent(**e) for e in events])
    except Exception as exc:  # telemetry must never break a search
        log.debug("search.stats_failed", error=str(exc)[:200])


async def record_search_usage(provider: str, *, purpose: str, cost_usd: float = 0.0) -> None:
    """One web search request in the usage ledger (category ``web_search``; no-op outside a usage scope)."""
    from scout.db.enums import UsageCategory
    from scout.services.usage import record_usage

    try:
        await record_usage(UsageCategory.web_search, cost_usd=cost_usd, source_key=provider, resolver=purpose)
    except Exception as exc:  # the ledger must never break a search
        log.warning("search.usage_failed", provider=provider, error=str(exc)[:200])


async def record_gemini(*, produced: bool, latency_ms: int, cost_usd: float) -> None:
    """Gemini grounded search used as the fallback (cost itself is recorded by the AI provider)."""
    await record_stats(
        [stat(ENGINE_DIMENSION, GEMINI_KEY, produced=produced, latency_ms=latency_ms, cost_usd=cost_usd)]
    )


async def record_people_source(
    key: str, *, produced: bool, latency_ms: int = 0, cost_usd: float = 0.0
) -> None:
    await record_stats(
        [stat(PEOPLE_DIMENSION, key, produced=produced, latency_ms=latency_ms, cost_usd=cost_usd)]
    )
