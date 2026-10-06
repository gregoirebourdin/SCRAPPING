"""Web research: grounded search with sources, cached 14 days in `grounded_research`.

Requires an AI provider and budget headroom (`allow_expensive`). An answer without grounding sources
is never accepted as a value (status unknown, confidence ≤ 0.4). Nothing is invented.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa

from scout.ai.factory import get_ai
from scout.ai.prompts import UNTRUSTED_CONTENT_RULES
from scout.db.engine import session_scope
from scout.db.models import GroundedResearch
from scout.enrich.ai_schemas import ResearchAnswer
from scout.enrich.matching import normalize_for_compare
from scout.enrich.resolvers.base import NO_AI, ResolveContext, ok, unknown
from scout.enrich.types import CellResult
from scout.enrich.values import coerce_value, display_for
from scout.util.text import sha256_hex
from scout.util.urls import registrable_domain

RESOLVER = "ai_web_research"
SOURCE_ID = "gemini_search"
CACHE_DAYS = 14
UNSOURCED_CAP = 0.4
UNMATCHED_SOURCE_CAP = 0.7

INSTRUCTIONS = (
    "You research facts about one specific company using web search. Answer ONLY from what the search results "
    "state about this exact company (match the name AND the domain or city; ignore homonyms). If the results do not "
    "answer the question, set value to null. Keep the value short (max 30 words). evidence_quote: a short excerpt "
    "from a source supporting the answer; source_url: the URL of that source.\n\n" + UNTRUSTED_CONTENT_RULES
)


def research_cache_key(rc: ResolveContext) -> str:
    plan, c, p = rc.plan, rc.company, rc.person
    subject = (c.normalized_domain or c.normalized_name) if c is not None else ""
    if p is not None:
        subject += f"|{p.normalized_name}"
    return sha256_hex(f"{normalize_for_compare(plan.concept or plan.name)}|{subject}")


def _query(rc: ResolveContext) -> str:
    plan, c, p = rc.plan, rc.company, rc.person
    parts = [plan.concept or plan.name, "—"]
    if p is not None:
        parts.append(f"{p.full_name}{f', {p.job_title}' if p.job_title else ''}")
        if c is not None:
            parts.append(f"at {c.name}")
    elif c is not None:
        parts.append(c.name)
    if c is not None:
        extra = ", ".join(x for x in (c.domain, c.city) if x)
        if extra:
            parts.append(f"({extra})")
    return " ".join(parts)


async def _cached(rc: ResolveContext, key: str) -> GroundedResearch | None:
    since = datetime.now(UTC) - timedelta(days=CACHE_DAYS)
    async with session_scope() as s:
        return await s.scalar(
            sa.select(GroundedResearch)
            .where(GroundedResearch.workspace_id == rc.workspace_id, GroundedResearch.cache_key == key,
                   GroundedResearch.created_at >= since)
            .order_by(GroundedResearch.created_at.desc())
            .limit(1)
        )


def _matching_source(url: str | None, sources: list[dict[str, Any]]) -> bool:
    if not url:
        return False
    dom = registrable_domain(url)
    for s in sources:
        if s.get("uri") == url:
            return True
        for cand in (s.get("domain"), s.get("title"), s.get("uri")):
            if dom and isinstance(cand, str) and registrable_domain(cand) == dom:
                return True
    return False


def research_cell(rc: ResolveContext, answer: ResearchAnswer, sources: list[dict[str, Any]], *,
                  model: str | None, cost: float) -> CellResult:
    """Turn a (possibly cached) research answer into a cell, enforcing source and confidence rules."""
    plan = rc.plan
    common: dict[str, Any] = {"resolver": RESOLVER, "model": model, "cost_usd": cost, "source_id": SOURCE_ID}
    value = coerce_value(answer.value, plan.data_type, plan.enum_values)
    if value is None:
        return unknown(plan, evidence="No reliable answer found in web sources", confidence=answer.confidence, **common)
    uris = [s["uri"] for s in sources if isinstance(s, dict) and s.get("uri")]
    if not uris:
        return unknown(plan, evidence=f"Unsourced answer discarded: “{display_for(value)}”",
                       confidence=min(answer.confidence, UNSOURCED_CAP), **common)
    conf = answer.confidence
    if _matching_source(answer.source_url, sources):
        url = answer.source_url
    else:
        url, conf = uris[0], min(conf, UNMATCHED_SOURCE_CAP)
    evidence = answer.evidence_quote.strip() or f"Sources: {', '.join(uris[:3])}"
    if conf < plan.confidence_threshold:
        return unknown(plan, evidence=f"Below confidence threshold ({conf:.2f} < {plan.confidence_threshold:.2f}): "
                                      f"{display_for(value)}", confidence=conf, source_url=url, **common)
    return ok(plan, value, confidence=conf, evidence=evidence, source_url=url, **common)


async def resolve(rc: ResolveContext) -> CellResult:
    from scout.services.usage import allow_expensive

    plan = rc.plan
    ai = get_ai()
    if not ai.available:
        return unknown(plan, resolver=RESOLVER, error=NO_AI)
    if rc.company is None and rc.person is None:
        return unknown(plan, resolver=RESOLVER, error="Missing dependency: company")
    key = research_cache_key(rc)
    cached = None if rc.force else await _cached(rc, key)
    if cached is not None:
        answer = ResearchAnswer.model_validate(cached.result or {})
        return research_cell(rc, answer, list(cached.sources or []), model=cached.model, cost=0.0)
    if not await allow_expensive():
        return unknown(plan, resolver=RESOLVER, error="Budget limit reached: web research skipped")
    query = _query(rc)
    res = await ai.grounded_search(query=query, instructions=INSTRUCTIONS, schema=ResearchAnswer)
    answer = res.value if res.value is not None else ResearchAnswer(
        value=(res.text or "").strip()[:500] or None, confidence=UNSOURCED_CAP
    )
    sources = [{"uri": s.uri, "title": s.title, "domain": s.domain} for s in res.sources]
    async with session_scope() as s:
        s.add(GroundedResearch(
            workspace_id=rc.workspace_id,
            company_id=rc.company.id if rc.company is not None else None,
            person_id=rc.person.id if rc.person is not None else None,
            purpose=f"enrichment:{plan.name}"[:200],
            query=query,
            search_queries=list(res.search_queries),
            sources=sources,
            result=answer.model_dump(mode="json"),
            selected_evidence=[{"text": sp.text, "sources": sp.source_indices} for sp in res.supports[:10]],
            model=res.usage.model,
            cache_key=key,
        ))
    return research_cell(rc, answer, sources, model=res.usage.model, cost=res.usage.cost_usd)
