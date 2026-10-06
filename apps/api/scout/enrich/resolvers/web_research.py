"""Web research: free web search first, Gemini grounded search only when unresolved; cached 14 days in
`grounded_research`.

Order (``GEMINI_SEARCH_FALLBACK_ONLY``, default on, and a lookup search provider configured — SearXNG):

1. cache (14 days, either path);
2. free search through ``scout.search.lookup_chain`` → results that mention the company (name or domain);
   insufficient = none does;
3. targeted crawl of the top ``WEB_RESEARCH_CRAWL_TOP`` results (robots-checked SSRF-safe fetcher, read-only);
4. cheap extractor model over those snippets + excerpts; resolved = a value whose verbatim quote is found in
   one of them and whose confidence clears the column threshold (snippet-only evidence capped at 0.7);
5. otherwise Gemini grounded search (budget-guarded), exactly as before.

Requires an AI provider. An answer without sources is never accepted as a value (status unknown, confidence
≤ 0.4). Nothing is invented. Provenance: ``source_id="web_search"`` (free search) or ``"gemini_search"``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import sqlalchemy as sa
import structlog

from scout.ai.factory import get_ai
from scout.ai.models import ModelRole
from scout.ai.prompts import UNTRUSTED_CONTENT_RULES, wrap_untrusted
from scout.db.engine import session_scope
from scout.db.enums import ColumnDataType
from scout.db.models import GroundedResearch
from scout.enrich.ai_schemas import ResearchAnswer
from scout.enrich.chunks import Passage, expand_terms, rank_passages, split_passages
from scout.enrich.matching import normalize_for_compare
from scout.enrich.resolvers.ai_common import subject_line, verify_quote
from scout.enrich.resolvers.base import NO_AI, ResolveContext, ok, unknown
from scout.enrich.types import CellResult
from scout.enrich.values import coerce_value, display_for
from scout.util.text import sha256_hex
from scout.util.urls import registrable_domain

log = structlog.get_logger(__name__)

RESOLVER = "ai_web_research"
SOURCE_ID = "gemini_search"
SERP_SOURCE_ID = "web_search"
CACHE_DAYS = 14
UNSOURCED_CAP = 0.4
UNMATCHED_SOURCE_CAP = 0.7
SNIPPET_ONLY_CAP = 0.7
SERP_RESULTS = 10
CRAWL_TIMEOUT_S = 15.0
EXCERPT_CHARS = 2500

INSTRUCTIONS = (
    "You research facts about one specific company using web search. Answer ONLY from what the search results "
    "state about this exact company (match the name AND the domain or city; ignore homonyms). If the results do not "
    "answer the question, set value to null. Keep the value short (max 30 words). evidence_quote: a short excerpt "
    "from a source supporting the answer; source_url: the URL of that source.\n\n" + UNTRUSTED_CONTENT_RULES
)

SERP_SYSTEM = (
    "You are a precise B2B research component. You answer one question about one specific company using ONLY "
    "the web search results and page excerpts provided (match the company name AND its domain or city; ignore "
    "homonyms and other companies). If they do not answer the question, set value to null — never guess. "
    "Keep the value short (max 30 words). evidence_quote: copy ONE sentence VERBATIM from a source that supports "
    "the value; source_url: that source's `source` attribute. confidence: 0 to 1.\n\n"
    + UNTRUSTED_CONTENT_RULES
)

_TYPE_HINTS = {
    ColumnDataType.text: "a short phrase (max 30 words)",
    ColumnDataType.number: "a number only (digits)",
    ColumnDataType.url: "a full URL",
    ColumnDataType.email: "an email address",
    ColumnDataType.boolean: '"true" or "false"',
    ColumnDataType.date: "a date (YYYY-MM-DD or YYYY)",
    ColumnDataType.enum: "exactly one of the allowed values",
    ColumnDataType.json: "a short phrase",
}


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
            .where(
                GroundedResearch.workspace_id == rc.workspace_id,
                GroundedResearch.cache_key == key,
                GroundedResearch.created_at >= since,
            )
            .order_by(GroundedResearch.created_at.desc())
            .limit(1)
        )


async def _store(rc: ResolveContext, **values: Any) -> None:
    async with session_scope() as s:
        s.add(
            GroundedResearch(
                workspace_id=rc.workspace_id,
                company_id=rc.company.id if rc.company is not None else None,
                person_id=rc.person.id if rc.person is not None else None,
                purpose=f"enrichment:{rc.plan.name}"[:200],
                **values,
            )
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


def research_cell(
    rc: ResolveContext,
    answer: ResearchAnswer,
    sources: list[dict[str, Any]],
    *,
    model: str | None,
    cost: float,
    source_id: str = SOURCE_ID,
) -> CellResult:
    """Turn a (possibly cached) research answer into a cell, enforcing source and confidence rules."""
    plan = rc.plan
    common: dict[str, Any] = {"resolver": RESOLVER, "model": model, "cost_usd": cost, "source_id": source_id}
    value = coerce_value(answer.value, plan.data_type, plan.enum_values)
    if value is None:
        return unknown(
            plan, evidence="No reliable answer found in web sources", confidence=answer.confidence, **common
        )
    uris = [s["uri"] for s in sources if isinstance(s, dict) and s.get("uri")]
    if not uris:
        return unknown(
            plan,
            evidence=f"Unsourced answer discarded: “{display_for(value)}”",
            confidence=min(answer.confidence, UNSOURCED_CAP),
            **common,
        )
    conf = answer.confidence
    if _matching_source(answer.source_url, sources):
        url = answer.source_url
    else:
        url, conf = uris[0], min(conf, UNMATCHED_SOURCE_CAP)
    evidence = answer.evidence_quote.strip() or f"Sources: {', '.join(uris[:3])}"
    if conf < plan.confidence_threshold:
        return unknown(
            plan,
            evidence=f"Below confidence threshold ({conf:.2f} < {plan.confidence_threshold:.2f}): "
            f"{display_for(value)}",
            confidence=conf,
            source_url=url,
            **common,
        )
    return ok(plan, value, confidence=conf, evidence=evidence, source_url=url, **common)


# ---- free web search pass ---------------------------------------------------------------------


def search_query(rc: ResolveContext) -> str:
    """Search-engine phrasing: quoted subject + the plan's keywords (or concept)."""
    plan, c, p = rc.plan, rc.company, rc.person
    terms = " ".join(plan.keywords[:3]) if plan.keywords else (plan.concept or plan.name)
    subject = f'"{p.full_name}" "{c.name}"' if p is not None and c is not None else None
    if subject is None:
        subject = f'"{c.name}"' if c is not None else f'"{p.full_name}"' if p is not None else ""
    return f"{subject} {terms}".strip()


def _subject_names(rc: ResolveContext) -> list[str]:
    names = [rc.company.name] if rc.company is not None and rc.company.name else []
    if rc.person is not None and rc.person.full_name:
        names.append(rc.person.full_name)
    return names


def _subject_domain(rc: ResolveContext) -> str | None:
    c = rc.company
    return (c.normalized_domain or c.domain) if c is not None else None


async def fetch_excerpt_page(url: str) -> Any | None:
    """Fetch one third-party result page through the crawler's SSRF-safe fetcher (robots respected).

    Returns a page-like object (url, title, meta_description, content_text, page_type) or None.
    """
    from scout.crawl import http as crawl_http
    from scout.crawl import robots
    from scout.crawl.parser import parse_html

    try:
        if not await robots.allowed(url):
            return None
        resp = await asyncio.wait_for(crawl_http.fetch(url, max_bytes=1_500_000), timeout=CRAWL_TIMEOUT_S)
    except Exception as exc:  # unreachable / blocked / timed out: the snippet still counts
        log.info("web_research.fetch_failed", url=url, error=str(exc)[:200])
        return None
    if not resp.ok or not resp.text:
        return None
    parsed = parse_html(resp.text, resp.final_url)
    if not parsed.content_text:
        return None
    return SimpleNamespace(
        id=None,
        url=resp.final_url,
        title=parsed.title,
        meta_description=parsed.meta_description,
        content_text=parsed.content_text,
        page_type="search_page",
    )


def _crawl_targets(results: Sequence[Any], n: int) -> list[str]:
    from scout.search.assess import is_social

    out: list[str] = []
    for r in results:
        if len(out) >= n:
            break
        url = r.url
        if is_social(url) or url.lower().split("?")[0].endswith((".pdf", ".doc", ".docx", ".xls", ".xlsx")):
            continue
        out.append(url)
    return out


def _serp_prompt(rc: ResolveContext, passages: Sequence[Passage]) -> str:
    plan = rc.plan
    dtype = ColumnDataType(str(getattr(plan.data_type, "value", plan.data_type)))
    lines = [subject_line(rc)]
    if rc.person is not None:
        lines.append(
            f"Person: {rc.person.full_name}{f', {rc.person.job_title}' if rc.person.job_title else ''}"
        )
    if rc.company is not None and rc.company.city:
        lines.append(f"City: {rc.company.city}")
    lines += [
        f"Question: {plan.concept or plan.name}",
        f"Expected value: {_TYPE_HINTS.get(dtype, 'a short phrase')}",
    ]
    if plan.enum_values:
        lines.append(f"Allowed values: {', '.join(plan.enum_values)}")
    if plan.output_instructions:
        lines.append(f"Additional instructions: {plan.output_instructions}")
    lines += ["", "Web search results and page excerpts (untrusted data):"]
    lines += [wrap_untrusted(p.text, source=p.page_url, kind="search_result") for p in passages]
    return "\n".join(lines)


async def _free_search_pass(rc: ResolveContext, ai: Any) -> CellResult | None:
    """Resolved cell from free search + targeted crawl + cheap extraction, or None (→ Gemini grounding)."""
    from scout.config import get_settings
    from scout.search.assess import assess_subject
    from scout.search.chain import gemini_fallback_only, lookup_chain

    if not gemini_fallback_only():
        return None
    chain = lookup_chain("research")
    if not chain.available():
        return None
    plan = rc.plan
    names, domain = _subject_names(rc), _subject_domain(rc)
    query = search_query(rc)
    region = ((rc.company.country or "").upper() or None) if rc.company is not None else None
    res = await chain.search(
        query,
        num=SERP_RESULTS,
        region=region,
        assess=lambda rs: assess_subject(rs, names=names, domain=domain),
    )
    if not res.sufficient:
        log.info("web_research.free_search_insufficient", query=query, reason=res.assessment.reason)
        return None
    relevant = res.usable
    passages: list[Passage] = [
        Passage(
            page_id=None, page_url=r.url, page_type="search_result", text=f"{r.title}\n{r.snippet}", idx=i
        )
        for i, r in enumerate(relevant)
        if r.snippet or r.title
    ]
    n_crawl = max(0, int(get_settings().web_research_crawl_top))
    crawled_urls: set[str] = set()
    if n_crawl:
        pages = await asyncio.gather(*(fetch_excerpt_page(u) for u in _crawl_targets(relevant, n_crawl)))
        terms = [*expand_terms(plan.concept or plan.name, plan.keywords), *names]
        for page in pages:
            if page is None:
                continue
            excerpt = rank_passages(split_passages(page), terms, k=3, max_chars=EXCERPT_CHARS, max_per_page=3)
            passages.extend(excerpt)
            crawled_urls.update(p.page_url for p in excerpt)
    if not passages:
        return None
    try:
        out = await ai.structured(
            role=ModelRole.extractor,
            system=SERP_SYSTEM,
            prompt=_serp_prompt(rc, passages),
            schema=ResearchAnswer,
        )
    except Exception as exc:  # extraction unavailable → let grounded research try
        log.info("web_research.extraction_failed", error=str(exc)[:200])
        return None
    answer: ResearchAnswer = out.value
    if coerce_value(answer.value, plan.data_type, plan.enum_values) is None:
        return None
    valid, url = verify_quote(answer.evidence_quote, answer.source_url, passages)
    if not valid or not url:
        log.info("web_research.free_search_unverified", query=query)
        return None
    conf = answer.confidence if url in crawled_urls else min(answer.confidence, SNIPPET_ONLY_CAP)
    answer = answer.model_copy(update={"source_url": url, "confidence": conf})
    by_url = {r.url: r for r in relevant}
    sources = [
        {
            "uri": p.page_url,
            "title": getattr(by_url.get(p.page_url), "title", None),
            "domain": registrable_domain(p.page_url),
            "engines": list(getattr(by_url.get(p.page_url), "engines", ()) or ()),
        }
        for p in passages
    ]
    sources = list({s["uri"]: s for s in sources}.values())
    cell = research_cell(
        rc, answer, sources, model=out.usage.model, cost=out.usage.cost_usd, source_id=SERP_SOURCE_ID
    )
    if cell.value is None:
        return None
    via = res.provider or "search"
    await _store(
        rc,
        query=query,
        search_queries=[query],
        sources=sources,
        result={**answer.model_dump(mode="json"), "via": via},
        selected_evidence=[{"text": answer.evidence_quote, "sources": [url], "via": via}],
        model=out.usage.model,
        cache_key=research_cache_key(rc),
    )
    return cell


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
        result = cached.result if isinstance(cached.result, dict) else {}
        answer = ResearchAnswer.model_validate(result)
        source_id = SERP_SOURCE_ID if result.get("via") else SOURCE_ID
        return research_cell(
            rc, answer, list(cached.sources or []), model=cached.model, cost=0.0, source_id=source_id
        )
    free = await _free_search_pass(rc, ai)
    if free is not None:
        return free
    if not await allow_expensive():
        return unknown(plan, resolver=RESOLVER, error="Budget limit reached: web research skipped")
    query = _query(rc)
    started = time.monotonic()
    res = await ai.grounded_search(query=query, instructions=INSTRUCTIONS, schema=ResearchAnswer)
    answer = (
        res.value
        if res.value is not None
        else ResearchAnswer(value=(res.text or "").strip()[:500] or None, confidence=UNSOURCED_CAP)
    )
    sources = [{"uri": s.uri, "title": s.title, "domain": s.domain} for s in res.sources]
    await _store(
        rc,
        query=query,
        search_queries=list(res.search_queries),
        sources=sources,
        result=answer.model_dump(mode="json"),
        selected_evidence=[{"text": sp.text, "sources": sp.source_indices} for sp in res.supports[:10]],
        model=res.usage.model,
        cache_key=key,
    )
    cell = research_cell(rc, answer, sources, model=res.usage.model, cost=res.usage.cost_usd)
    from scout.search.telemetry import record_gemini

    await record_gemini(
        produced=cell.value is not None,
        latency_ms=int((time.monotonic() - started) * 1000),
        cost_usd=res.usage.cost_usd,
    )
    return cell
