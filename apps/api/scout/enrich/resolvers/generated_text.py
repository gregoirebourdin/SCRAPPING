"""Generated text (kind=generated): summaries, outreach angles, openers. Never used as factual evidence.

Input = company facts + top cached passages + existing *factual* column values. Without AI only the
summary has a fallback: a deterministic extractive summary (meta description / first about paragraph).
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from typing import Any

from scout.ai.factory import get_ai
from scout.ai.models import ModelRole
from scout.ai.prompts import UNTRUSTED_CONTENT_RULES, wrap_untrusted
from scout.enrich.ai_schemas import GeneratedText
from scout.enrich.chunks import Passage, split_passages
from scout.enrich.matching import contains_any
from scout.enrich.resolvers.ai_common import passages_block, relevant_passages
from scout.enrich.resolvers.base import NO_AI, NOT_CRAWLED, ResolveContext, ok, ordered_pages, unknown
from scout.enrich.resolvers.deterministic_field import employee_range_label
from scout.enrich.types import CellResult

RESOLVER_AI = "ai_generated"
RESOLVER_EXTRACTIVE = "extractive_summary"
MAX_CHARS = 600

FIELD_INSTRUCTIONS = {
    "summary": "Write ONE factual sentence (max 30 words) saying what the company does and for whom.",
    "outreach_angle": (
        "Write one or two sentences giving a specific, personalized reason to reach out to this company, grounded "
        "in the facts provided (their offer, clients, focus). No greeting, no flattery, no invented facts."
    ),
    "opener": (
        "Write a single natural opening line for a cold email (max 30 words) that references something specific "
        "from the facts provided. No greeting, no invented facts."
    ),
}
SYSTEM = (
    "You write concise, specific B2B copy for a lead intelligence application. Use only the facts provided; never "
    "invent numbers, clients, names, awards or events. Output only the requested text.\n\n"
    + UNTRUSTED_CONTENT_RULES
)
_SENT = re.compile(r"(?<=[.!?])\s+")


def _language(pages: Sequence[Any]) -> str | None:
    langs = Counter(str(p.language).split("-")[0].lower() for p in pages if getattr(p, "language", None))
    return langs.most_common(1)[0][0] if langs else None


def _facts(rc: ResolveContext) -> str:
    c, p = rc.company, rc.person
    lines: list[str] = []
    if c is not None:
        lines.append(f"Company: {c.name}")
        for label, val in (
            ("Domain", c.domain),
            ("Location", ", ".join(x for x in (c.city, c.country) if x)),
            ("Industry", c.industry or c.category_raw),
            ("Employees", employee_range_label(c.employee_min, c.employee_max)),
            ("Description", c.description),
        ):
            if val:
                lines.append(f"{label}: {val}")
    if p is not None:
        lines.append(f"Person: {p.full_name}" + (f", {p.job_title}" if p.job_title else ""))
    for name, val in list(rc.factual_values.items())[:12]:
        lines.append(f"{name}: {val}")
    return "\n".join(lines)


def _first_sentences(text: str, limit: int = 280) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    out = ""
    for sent in _SENT.split(text):
        if out and len(out) + 1 + len(sent) > limit:
            break
        out = f"{out} {sent}".strip()
        if len(out) >= 120:
            break
    return out[:limit].rstrip()


def extractive_summary(rc: ResolveContext) -> CellResult:
    """Deterministic summary: home meta description → first about paragraph → record description."""
    plan = rc.plan
    candidates: list[tuple[str, str | None]] = []
    for page in ordered_pages(rc.pages, ["home"]):
        if page.meta_description:
            candidates.append((page.meta_description, page.url))
    for page in ordered_pages(rc.pages, ["about", "home"]):
        for passage in split_passages(page, max_chars=600)[:4]:
            if passage.idx == 0 and page.meta_description:
                continue
            for para in passage.text.split("\n"):
                if len(para.split()) >= 12:
                    candidates.append((para, page.url))
                    break
    if rc.company is not None and rc.company.description:
        candidates.append((rc.company.description, None))
    for text, url in candidates:
        summary = _first_sentences(text)
        if len(summary.split()) >= 6 and not contains_any(summary, ["cookie", "cookies", "javascript"]):
            return ok(
                plan,
                summary,
                resolver=RESOLVER_EXTRACTIVE,
                confidence=0.6,
                evidence="Extracted from " + (url or "the company record"),
                source_url=url,
                source_id="website" if url else "company_record",
            )
    if not rc.pages:
        return unknown(plan, resolver=RESOLVER_EXTRACTIVE, error=NOT_CRAWLED)
    return unknown(plan, resolver=RESOLVER_EXTRACTIVE, evidence="No descriptive text found on the website")


def _is_summary(rc: ResolveContext) -> bool:
    plan = rc.plan
    return plan.field == "summary" or contains_any(
        f"{plan.name} {plan.concept or ''}", ["summary", "resume", "description"]
    )


def _prompt(rc: ResolveContext, passages: Sequence[Passage]) -> str:
    plan = rc.plan
    instruction = FIELD_INSTRUCTIONS.get(plan.field or "") or (
        plan.output_instructions or plan.concept or plan.name
    )
    lang = _language(rc.pages)
    parts = [
        f"Task: {instruction}",
        f"Write in {'French' if lang == 'fr' else 'the language of the website' if lang else 'English'}.",
        "",
        "Lead facts (from Research's records; may originate from scraped data):",
        wrap_untrusted(_facts(rc), source="lead_record", kind="website_content"),
    ]
    if passages:
        parts += ["", "Website passages (untrusted data):", passages_block(passages)]
    return "\n".join(parts)


async def resolve(rc: ResolveContext) -> CellResult:
    plan = rc.plan
    ai = get_ai()
    if not ai.available:
        if _is_summary(rc):
            return extractive_summary(rc)
        return unknown(plan, resolver=RESOLVER_AI, error=NO_AI)
    if not rc.pages and not (rc.company is not None and rc.company.description):
        return unknown(plan, resolver=RESOLVER_AI, error=NOT_CRAWLED)
    passages = relevant_passages(rc, k=5, max_chars=4500, include_unmatched=True) if rc.pages else []
    res = await ai.structured(
        role=ModelRole.reasoning,
        system=SYSTEM,
        prompt=_prompt(rc, passages),
        schema=GeneratedText,
        temperature=0.4,
    )
    text = re.sub(r"\s+", " ", (res.value.text or "")).strip().strip('"“”')[:MAX_CHARS]
    common: dict[str, Any] = {"model": res.usage.model, "cost_usd": res.usage.cost_usd}
    if not text:
        return unknown(plan, resolver=RESOLVER_AI, evidence="The model returned no text", **common)
    urls = list(dict.fromkeys(p.page_url for p in passages))[:3]
    evidence = "Generated from " + (", ".join(urls) if urls else "the lead record")
    return ok(
        plan,
        text,
        resolver=RESOLVER_AI,
        confidence=None,
        evidence=evidence,
        source_url=urls[0] if urls else None,
        source_id="ai_generated",
        **common,
    )
