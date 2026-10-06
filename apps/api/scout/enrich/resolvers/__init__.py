"""One resolver module per strategy; `resolve(rc)` dispatches on `rc.plan.strategy`."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from scout.enrich.resolvers import (
    ai_extraction,
    composite,
    deterministic_field,
    generated_text,
    keyword,
    regex,
    semantic_classifier,
    social_profile,
    tech_detection,
    web_research,
    website_field,
)
from scout.enrich.resolvers.base import ResolveContext, failed
from scout.enrich.types import CellResult

RESOLVERS: dict[str, Callable[[ResolveContext], Awaitable[CellResult]]] = {
    "keyword": keyword.resolve,
    "regex": regex.resolve,
    "social_profile": social_profile.resolve,
    "website_field": website_field.resolve,
    "deterministic_field": deterministic_field.resolve,
    "tech_detection": tech_detection.resolve,
    "semantic_classifier": semantic_classifier.resolve,
    "ai_extraction": ai_extraction.resolve,
    "web_research": web_research.resolve,
    "generated_text": generated_text.resolve,
    "composite": composite.resolve,
}


async def resolve(rc: ResolveContext) -> CellResult:
    """Run the plan's resolver. Technical exceptions propagate (the engine maps them to `failed`)."""
    fn = RESOLVERS.get(rc.plan.strategy)
    if fn is None:
        return failed(rc.plan, resolver=rc.plan.strategy, error=f"Unsupported strategy: {rc.plan.strategy}")
    return await fn(rc)


__all__ = ["RESOLVERS", "ResolveContext", "resolve"]
