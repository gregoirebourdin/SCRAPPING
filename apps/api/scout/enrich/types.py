"""Dynamic enrichment contracts (spec §39–45, §136)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from scout.db.enums import (
    CellStatus,
    ColumnDataType,
    ColumnKind,
    CostClass,
    EntityType,
    PageType,
    ResolverType,
)

Strategy = Literal[
    "keyword",  # deterministic, cached website text contains term(s)
    "regex",  # deterministic regex on cached text
    "social_profile",  # deterministic: social link from cached pages (field = network)
    "website_field",  # deterministic extractor on cached pages (field = phone|email|address|cta|testimonials|pricing_page|careers_page|blog|newsletter|chat_widget|booking_link)
    "deterministic_field",  # copy/derive a canonical field (field = city|employee_range|email_status|…)
    "tech_detection",  # fingerprints (wappalyzergo service or builtin signatures)
    "semantic_classifier",  # AI true/false/unknown on relevant cached chunks, with evidence
    "ai_extraction",  # AI extracts a value from relevant cached chunks, with verbatim evidence
    "web_research",  # grounded web search with sources
    "generated_text",  # AI-generated copy (summary, outreach angle) — kind=generated
    "composite",  # dependency chain (e.g. CEO email = person → email → verify)
]


class EnrichmentPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    data_type: ColumnDataType
    kind: ColumnKind = ColumnKind.factual
    entity_type: EntityType = EntityType.company
    resolver: ResolverType
    strategy: Strategy
    concept: str | None = Field(default=None, description="What to determine/extract, in plain English")
    keywords: list[str] = Field(default_factory=list)
    pattern: str | None = None
    field: str | None = None
    technologies: list[str] = Field(default_factory=list)
    enum_values: list[str] = Field(default_factory=list)
    input_sources: list[PageType] = Field(default_factory=list)
    confidence_threshold: float = Field(default=0.8, ge=0, le=1)
    refresh_days: int = Field(default=30, ge=1)
    cost_class: CostClass = CostClass.FREE
    depends_on: list[str] = Field(default_factory=list)
    output_instructions: str | None = None
    explanation: str = ""


@dataclass
class CellResult:
    status: CellStatus  # success | unknown | failed
    value: Any = None  # JSON value; booleans stay true/false; unknown → None with status unknown
    display_value: str | None = None  # for sort/filter ("true"/"false"/"unknown"/text)
    confidence: float | None = None
    evidence: str | None = None
    source_url: str | None = None
    source_id: str | None = None  # sources.key ("website", "tech_scan", "gemini_search"…)
    resolver: str = ""
    error: str | None = None
    input_hash: str | None = None
    model: str | None = None
    cost_usd: float = 0.0
