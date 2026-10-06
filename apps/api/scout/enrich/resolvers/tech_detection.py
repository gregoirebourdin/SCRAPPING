"""Technology strategy: fingerprint detection (scout.tech.detector) with a cached page-script fallback."""

from __future__ import annotations

from typing import Any

from scout.db.enums import ColumnDataType
from scout.enrich.matching import contains_any, fold, tokens
from scout.enrich.planner import known_technology_aliases
from scout.enrich.resolvers.base import NOT_CRAWLED, ResolveContext, ok, ordered_pages, unknown
from scout.enrich.types import CellResult

RESOLVER = "tech_detection"


def _canon(name: str) -> str:
    return "".join(tokens(name))


def _script_hit(pages: list[Any], name: str) -> tuple[Any, str] | None:
    """Tech name referenced in captured <head>/script markup (e.g. cdn.shopify.com)."""
    slug = _canon(name)
    if len(slug) < 4:
        return None
    for page in ordered_pages(pages):
        head = getattr(page, "head_html", None)
        if head and (slug in fold(head).replace("-", "").replace(" ", "") or contains_any(head, [name])):
            return page, f"{name} referenced in page scripts of {page.url}"
    return None


async def resolve(rc: ResolveContext) -> CellResult:
    plan = rc.plan
    if rc.company is None:
        return unknown(plan, resolver=RESOLVER, error="Missing dependency: company")
    techs = await rc.technologies()
    aliases = known_technology_aliases()

    def canon(n: str) -> str:
        return _canon(aliases.get(_canon(n), n))

    if not plan.technologies:  # generic "tech stack" list
        category = fold(plan.field or "")
        rows = [t for t in techs if not category or category in fold(t.category or "")]
        rows.sort(key=lambda t: (-(t.confidence or 0), t.name))
        if rows:
            names = [t.name for t in rows]
            value: Any = names if plan.data_type in (ColumnDataType.json,) else ", ".join(names)
            return ok(plan, value, resolver=RESOLVER, confidence=0.9, evidence=f"Detected {len(names)} technologies",
                      source_url=rows[0].source_url, source_id="tech_scan")
        if not rc.pages and not techs:
            return unknown(plan, resolver=RESOLVER, error=NOT_CRAWLED)
        return unknown(plan, resolver=RESOLVER, evidence="No technologies detected", source_id="tech_scan")

    by_name = {canon(t.name): t for t in techs}
    matched = [by_name[canon(w)] for w in plan.technologies if canon(w) in by_name]
    boolean = plan.data_type == ColumnDataType.boolean
    if matched:
        t = matched[0]
        version = f" {t.version}" if t.version else ""
        evidence = f"{t.name}{version} detected by {t.detector}"
        value = True if boolean else ", ".join(m.name for m in matched)
        return ok(plan, value, resolver=RESOLVER, confidence=t.confidence or 0.9, evidence=evidence,
                  source_url=t.source_url, source_id="tech_scan")
    for wanted in plan.technologies:
        hit = _script_hit(rc.pages, wanted)
        if hit:
            page, evidence = hit
            return ok(plan, True if boolean else wanted, resolver="page_scripts", confidence=0.8, evidence=evidence,
                      source_url=page.url, source_id="tech_scan")
    if not rc.pages and not techs:
        return unknown(plan, resolver=RESOLVER, error=NOT_CRAWLED)
    known = all(_canon(w) in aliases or canon(w) in {_canon(v) for v in aliases.values()} for w in plan.technologies)
    evidence = f"{' / '.join(plan.technologies)} not detected among {len(techs)} detected technologies"
    if boolean:
        return ok(plan, False, resolver=RESOLVER, confidence=0.85 if known else 0.6, evidence=evidence,
                  source_id="tech_scan")
    return unknown(plan, resolver=RESOLVER, evidence=evidence, source_id="tech_scan")
