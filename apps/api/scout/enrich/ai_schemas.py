"""Structured-output schemas for enrichment AI calls (schema names are what FakeProvider keys on)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Out(BaseModel):
    model_config = ConfigDict(extra="ignore")


class SemanticVerdict(_Out):
    """Semantic classifier output."""

    verdict: Literal["true", "false", "unknown"] = Field(description="true / false / unknown")
    confidence: float = Field(default=0.0, ge=0, le=1)
    evidence_quote: str = Field(default="", description="Sentence copied verbatim from one passage")
    source_url: str | None = Field(default=None, description="The source attribute of the quoted passage")


class ExtractedValue(_Out):
    """AI extraction output."""

    value: str | None = Field(default=None, description="Extracted value, or null when not stated")
    confidence: float = Field(default=0.0, ge=0, le=1)
    evidence_quote: str = Field(default="", description="Sentence copied verbatim from one passage")
    source_url: str | None = None


class ResearchAnswer(_Out):
    """Grounded web research output."""

    value: str | None = Field(default=None, description="Answer, or null when no reliable source exists")
    confidence: float = Field(default=0.0, ge=0, le=1)
    evidence_quote: str = Field(default="", description="Short supporting excerpt from a source")
    source_url: str | None = Field(default=None, description="URL of the source supporting the answer")


class GeneratedText(_Out):
    """Generated copy (summary, outreach angle, opener)."""

    text: str = Field(default="", description="The generated text only")


PlannerStrategy = Literal[
    "semantic_classifier", "ai_extraction", "web_research", "generated_text", "website_field", "deterministic_field"
]


class ColumnPlanDraft(_Out):
    """Restricted plan proposed by the AI planner (validated and normalized by the planner)."""

    strategy: PlannerStrategy
    data_type: Literal["boolean", "text", "number", "url", "email", "enum", "date"] = "text"
    concept: str = Field(description="What to determine or extract, in plain English")
    keywords: list[str] = Field(default_factory=list, max_length=10)
    input_sources: list[
        Literal["home", "about", "team", "services", "solutions", "contact", "pricing", "careers", "blog",
                "news", "legal", "case_studies", "other"]
    ] = Field(default_factory=list)
    field: str | None = None
    enum_values: list[str] = Field(default_factory=list, max_length=20)
    explanation: str = ""
