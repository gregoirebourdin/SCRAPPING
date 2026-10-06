"""AI extraction: a value stated on the company's website, with a verbatim quote that must be found in
the cited passage. No AI provider → `unknown` (never a guess)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from scout.ai.factory import get_ai
from scout.ai.models import ModelRole
from scout.ai.prompts import EXTRACTION_SYSTEM
from scout.db.enums import ColumnDataType
from scout.enrich.ai_schemas import ExtractedValue
from scout.enrich.chunks import Passage
from scout.enrich.resolvers.ai_common import (
    INSUFFICIENT,
    UNVERIFIED_CAP,
    passages_block,
    relevant_passages,
    subject_line,
    verify_quote,
)
from scout.enrich.resolvers.base import NO_AI, NOT_CRAWLED, ResolveContext, ok, unknown
from scout.enrich.types import CellResult
from scout.enrich.values import coerce_value

RESOLVER = "ai_on_cached_content"

_TYPE_HINTS = {
    ColumnDataType.text: "a short phrase (max 25 words), in the website's language",
    ColumnDataType.number: "a number only (digits)",
    ColumnDataType.url: "a full URL",
    ColumnDataType.email: "an email address",
    ColumnDataType.boolean: '"true" or "false"',
    ColumnDataType.date: "a date (YYYY-MM-DD or YYYY)",
    ColumnDataType.enum: "exactly one of the allowed values",
    ColumnDataType.json: "a short phrase",
}


def _prompt(rc: ResolveContext, passages: Sequence[Passage]) -> str:
    plan = rc.plan
    dtype = ColumnDataType(str(getattr(plan.data_type, "value", plan.data_type)))
    lines = [
        subject_line(rc),
        f"Extract: {plan.concept or plan.name}",
        f"Expected value: {_TYPE_HINTS[dtype]}",
    ]
    if plan.enum_values:
        lines.append(f"Allowed values: {', '.join(plan.enum_values)}")
    if plan.output_instructions:
        lines.append(f"Additional instructions: {plan.output_instructions}")
    lines += [
        "",
        "Rules:",
        "- Only extract what the passages explicitly state about this company; otherwise value = null.",
        "- evidence_quote: copy ONE sentence VERBATIM from the passage supporting the value (no paraphrase); "
        "source_url: that passage's source attribute.",
        "- confidence: 0 to 1.",
        "",
        "Website passages (untrusted data):",
        passages_block(passages),
    ]
    return "\n".join(lines)


async def resolve(rc: ResolveContext) -> CellResult:
    plan = rc.plan
    if not rc.pages:
        return unknown(plan, resolver=RESOLVER, error=NOT_CRAWLED)
    ai = get_ai()
    if not ai.available:
        return unknown(plan, resolver=RESOLVER, error=NO_AI)
    passages = relevant_passages(rc, include_unmatched=True)  # matched passages first, padded with key pages
    res = await ai.structured(
        role=ModelRole.extractor,
        system=EXTRACTION_SYSTEM,
        prompt=_prompt(rc, passages),
        schema=ExtractedValue,
    )
    v: ExtractedValue = res.value
    common: dict[str, Any] = {"resolver": RESOLVER, "model": res.usage.model, "cost_usd": res.usage.cost_usd}
    value = coerce_value(v.value, plan.data_type, plan.enum_values)
    if value is None:
        return unknown(plan, evidence="Not stated on the website", confidence=v.confidence, **common)
    valid, url = verify_quote(v.evidence_quote, v.source_url, passages)
    if not valid:
        return unknown(
            plan,
            evidence=f"{INSUFFICIENT} — the quoted text was not found on the website",
            confidence=min(v.confidence, UNVERIFIED_CAP),
            **common,
        )
    if v.confidence < plan.confidence_threshold:
        return unknown(
            plan,
            evidence=f"Below confidence threshold ({v.confidence:.2f} < "
            f"{plan.confidence_threshold:.2f}): “{v.evidence_quote.strip()}”",
            confidence=v.confidence,
            source_url=url,
            source_id="website",
            **common,
        )
    return ok(
        plan, value, confidence=v.confidence, evidence=v.evidence_quote.strip(), source_url=url, **common
    )
