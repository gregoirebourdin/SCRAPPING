"""Website conditions for the campaign pipeline's `website_conditions` stage (PIPELINE §3.3).

keyword_any / keyword_all / regex are deterministic; semantic_* use the semantic classifier (AI, or the
offline heuristic); technology uses tech detection. Results are cached in `website_condition_results`
keyed by (company, condition_hash, content_hash): unchanged pages ⇒ no recomputation, no AI call.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import orjson
import sqlalchemy as sa
import structlog
from sqlalchemy.dialects.postgresql import insert as pg_insert

from scout.db.engine import session_scope
from scout.db.enums import CellStatus, ColumnDataType, CostClass, ResolverType
from scout.db.ids import uuid7
from scout.db.models import Company, WebsiteConditionResult
from scout.enrich.matching import excerpt
from scout.enrich.resolvers.base import ResolveContext
from scout.enrich.resolvers.keyword import match_keywords
from scout.enrich.resolvers.regex import UnsafePattern, compile_safe, find_pattern
from scout.enrich.types import CellResult, EnrichmentPlan
from scout.schemas.campaign import (
    KeywordCondition,
    RegexCondition,
    SemanticCondition,
    TechnologyCondition,
    WebsiteCondition,
)
from scout.util.text import sha256_hex

log = structlog.get_logger("enrich.conditions")
_HASH_EXCLUDE = {"label", "required"}  # presentation-only fields never invalidate the cache


@dataclass
class ConditionResult:
    passed: bool | None  # None = unknown (insufficient evidence / site not crawled)
    confidence: float
    evidence: str | None
    source_url: str | None
    resolver: str
    cached: bool = False


# Bumped when an evaluator changes meaning, so cached verdicts are recomputed (keyword v2: social icon links count).
_EVALUATOR_VERSION = {"keyword_any": 2, "keyword_all": 2}


def condition_hash(condition: WebsiteCondition) -> str:
    data = condition.model_dump(mode="json", exclude=_HASH_EXCLUDE)
    if (v := _EVALUATOR_VERSION.get(condition.type)) is not None:
        data["_v"] = v
    return sha256_hex(orjson.dumps(data, option=orjson.OPT_SORT_KEYS))


def pages_content_hash(pages: Sequence[Any]) -> str:
    return sha256_hex("|".join(sorted(str(getattr(p, "content_hash", "") or "") for p in pages)))


def _from_cell(cell: CellResult, min_confidence: float = 0.0) -> ConditionResult:
    conf = float(cell.confidence or 0.0)
    passed: bool | None = None
    if cell.status == CellStatus.success and isinstance(cell.value, bool) and conf >= min_confidence:
        passed = cell.value
    return ConditionResult(
        passed=passed,
        confidence=conf,
        evidence=cell.evidence or cell.error,
        source_url=cell.source_url,
        resolver=cell.resolver,
    )


def _plan(condition: SemanticCondition | TechnologyCondition, name: str) -> EnrichmentPlan:
    if isinstance(condition, TechnologyCondition):
        return EnrichmentPlan(
            name=name,
            data_type=ColumnDataType.boolean,
            resolver=ResolverType.TECH_DETECTION,
            strategy="tech_detection",
            technologies=condition.technologies,
            cost_class=CostClass.CHEAP,
            concept=f"Uses {' / '.join(condition.technologies)}",
        )
    return EnrichmentPlan(
        name=name,
        data_type=ColumnDataType.boolean,
        resolver=ResolverType.AI_ON_CACHED_CONTENT,
        strategy="semantic_classifier",
        concept=condition.concept,
        keywords=condition.keywords,
        confidence_threshold=condition.min_confidence,
        cost_class=CostClass.AI,
    )


def _linked_term(pages: Sequence[Any], terms: Sequence[str]) -> tuple[str | None, str, str] | None:
    """A term named only by a link — social icons (instagram.com/…, tiktok.com/@…) carry no text."""
    from scout.enrich.matching import contains_any

    for page in pages:
        links = getattr(page, "links", None) or {}
        social = links.get("social") if isinstance(links, dict) else None
        urls = [str(u) for u in (social or {}).values()] if isinstance(social, dict) else []
        if isinstance(links, dict):
            urls += [str(x.get("url") if isinstance(x, dict) else x) for x in links.get("external") or []]
        for term in terms:
            for url in urls:
                host = url.split("//", 1)[-1].split("/", 1)[0]
                if contains_any(host.replace(".", " "), [term]):
                    return getattr(page, "url", None), term, url[:200]
    return None


async def _compute(
    workspace_id: uuid.UUID, company: Company, condition: WebsiteCondition, pages: Sequence[Any]
) -> ConditionResult:
    name = condition.label or condition.type
    if isinstance(condition, KeywordCondition):
        out = match_keywords(pages, condition.terms, match_all=condition.type == "keyword_all")
        if out.passed is None:
            return ConditionResult(None, 0.0, "Website not crawled", None, "keyword")
        if out.passed:
            first = out.hits[0]
            return ConditionResult(True, 1.0, first.snippet, getattr(first.page, "url", None), "keyword")
        if condition.type == "keyword_any" and (linked := _linked_term(pages, condition.terms)):
            # "Instagram" as a footer icon: no text, but the site links to its Instagram account
            page_url, term, link = linked
            return ConditionResult(True, 0.85, f"Links to {term}: {link}", page_url, "keyword")
        missing = ", ".join(out.missing[:4])
        return ConditionResult(
            False, 0.9, f"No mention of {missing} across {len(pages)} crawled pages", None, "keyword"
        )
    if isinstance(condition, RegexCondition):
        if not pages:
            return ConditionResult(None, 0.0, "Website not crawled", None, "regex")
        try:
            rx = compile_safe(condition.pattern)
            found = await find_pattern(rx, pages)
        except (UnsafePattern, TimeoutError) as exc:
            return ConditionResult(None, 0.0, f"Pattern not evaluated: {exc}", None, "regex")
        if found:
            url, text, s, e, _raw = found
            return ConditionResult(True, 0.95, excerpt(text, s, e, 160), url, "regex")
        return ConditionResult(False, 0.85, f"No match across {len(pages)} crawled pages", None, "regex")

    from scout.enrich.resolvers import semantic_classifier, tech_detection

    rc = ResolveContext(
        plan=_plan(condition, name), workspace_id=workspace_id, company=company, pages=list(pages)
    )
    if isinstance(condition, TechnologyCondition):
        if condition.match == "all" and len(condition.technologies) > 1:
            results = []
            for tech in condition.technologies:
                sub = rc.plan.model_copy(update={"technologies": [tech], "concept": f"Uses {tech}"})
                results.append(
                    _from_cell(
                        await tech_detection.resolve(
                            ResolveContext(
                                plan=sub, workspace_id=workspace_id, company=company, pages=list(pages)
                            )
                        )
                    )
                )
            failed_check = next((r for r in results if r.passed is False), None)
            if failed_check is not None:
                return failed_check
            if all(r.passed for r in results):
                return ConditionResult(
                    True,
                    min(r.confidence for r in results),
                    " | ".join(r.evidence or "" for r in results),
                    results[0].source_url,
                    "tech_detection",
                )
            return ConditionResult(
                None, 0.0, "Some technologies could not be checked", None, "tech_detection"
            )
        return _from_cell(await tech_detection.resolve(rc))
    return _from_cell(await semantic_classifier.resolve(rc), condition.min_confidence)


async def evaluate_condition(
    workspace_id: uuid.UUID,
    company: Company,
    condition: WebsiteCondition,
    pages: Sequence[Any],
    *,
    use_cache: bool = True,
) -> ConditionResult:
    """Evaluate one website condition for a company, reusing a cached result for identical page content.

    Technical errors (e.g. a failed AI request → RetryableError) propagate so the calling job retries;
    nothing is cached in that case.
    """
    c_hash = condition_hash(condition)
    content = pages_content_hash(pages)
    if use_cache and pages:
        async with session_scope() as s:
            row = await s.scalar(
                sa.select(WebsiteConditionResult).where(
                    WebsiteConditionResult.company_id == company.id,
                    WebsiteConditionResult.condition_hash == c_hash,
                    WebsiteConditionResult.content_hash == content,
                )
            )
        if row is not None:
            return ConditionResult(
                passed=row.passed,
                confidence=row.confidence,
                evidence=row.evidence,
                source_url=row.source_url,
                resolver=row.resolver,
                cached=True,
            )
    result = await _compute(workspace_id, company, condition, pages)
    if pages:
        async with session_scope() as s:
            stmt = pg_insert(WebsiteConditionResult).values(
                id=uuid7(),
                workspace_id=workspace_id,
                company_id=company.id,
                condition_hash=c_hash,
                content_hash=content,
                passed=result.passed,
                confidence=result.confidence,
                evidence=(result.evidence or "")[:2000] or None,
                source_url=result.source_url,
                resolver=result.resolver,
            )
            await s.execute(
                stmt.on_conflict_do_nothing(index_elements=["company_id", "condition_hash", "content_hash"])
            )
    log.debug(
        "enrich.condition",
        company_id=str(company.id),
        type=condition.type,
        passed=result.passed,
        resolver=result.resolver,
    )
    return result


_DETERMINISTIC_TYPES = {"keyword_any", "keyword_all", "regex"}


async def evaluate_conditions(
    workspace_id: uuid.UUID,
    company: Company,
    conditions: Sequence[WebsiteCondition],
    pages: Sequence[Any],
    *,
    stop_on_required_failure: bool = True,
) -> list[tuple[WebsiteCondition, ConditionResult]]:
    """Deterministic conditions first, then technology, then semantic (AI); stops at the first *required*
    condition that fails (passed is False) so no AI is spent on an already-rejected company."""
    order = sorted(
        conditions, key=lambda c: 0 if c.type in _DETERMINISTIC_TYPES else 1 if c.type == "technology" else 2
    )
    out: list[tuple[WebsiteCondition, ConditionResult]] = []
    for cond in order:
        res = await evaluate_condition(workspace_id, company, cond, pages)
        out.append((cond, res))
        if stop_on_required_failure and cond.required and res.passed is False:
            break
    return out
