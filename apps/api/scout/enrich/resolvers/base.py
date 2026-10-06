"""Resolver context and result helpers shared by every strategy."""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

import sqlalchemy as sa

from scout.db.engine import session_scope
from scout.db.enums import CellStatus, ColumnDataType, PageType
from scout.db.models import Company, Email, Person, Technology, WebsitePage
from scout.enrich.types import CellResult, EnrichmentPlan
from scout.enrich.values import display_for

# Order in which pages are scanned by deterministic resolvers (first match wins).
PAGE_ORDER: dict[str, int] = {
    "home": 0,
    "services": 1,
    "solutions": 2,
    "about": 3,
    "case_studies": 4,
    "pricing": 5,
    "contact": 6,
    "team": 7,
    "careers": 8,
    "blog": 9,
    "news": 10,
    "other": 11,
    "legal": 12,
}


def page_type_of(page: Any) -> str:
    pt = getattr(page, "page_type", None) or "other"
    return str(getattr(pt, "value", pt))


def ordered_pages(pages: Iterable[Any], only: Sequence[PageType | str] = ()) -> list[Any]:
    """Pages sorted home → services → … → legal; optionally restricted to `only` page types."""
    allowed = {str(getattr(t, "value", t)) for t in only}
    items = [p for p in pages if not allowed or page_type_of(p) in allowed]
    return sorted(
        items, key=lambda p: (PAGE_ORDER.get(page_type_of(p), 11), len(getattr(p, "url", "") or ""))
    )


async def load_technologies(
    workspace_id: uuid.UUID, company: Company | None, *, force: bool = False, max_age_days: int = 30
) -> list[Technology]:
    """Technologies via the detector (lazy import); falls back to rows already stored."""
    if company is None:
        return []
    try:
        from scout.tech.detector import detect_technologies
    except ImportError:
        detect_technologies = None  # type: ignore[assignment]
    if detect_technologies is not None:
        return list(
            await detect_technologies(workspace_id, company.id, force=force, max_age_days=max_age_days)
        )
    async with session_scope() as s:
        rows = await s.scalars(
            sa.select(Technology).where(
                Technology.workspace_id == workspace_id, Technology.company_id == company.id
            )
        )
        return list(rows.all())


@dataclass
class ResolveContext:
    """Everything a resolver may read. Pages may be empty (site not crawled / no website)."""

    plan: EnrichmentPlan
    workspace_id: uuid.UUID
    company: Company | None
    person: Person | None = None
    pages: list[WebsitePage] = field(default_factory=list)
    column_id: uuid.UUID | None = None
    factual_values: dict[str, str] = field(
        default_factory=dict
    )  # other factual columns (generated text input)
    dependency_values: dict[str, Any] = field(default_factory=dict)
    force: bool = False
    _technologies: list[Technology] | None = None
    _emails: list[Email] | None = None

    @property
    def crawled(self) -> bool:
        return bool(self.pages)

    @property
    def subject_name(self) -> str:
        if self.company is not None:
            return self.company.name
        return self.person.full_name if self.person is not None else "this lead"

    async def technologies(self) -> list[Technology]:
        if self._technologies is None:
            self._technologies = await load_technologies(
                self.workspace_id, self.company, force=self.force, max_age_days=self.plan.refresh_days
            )
        return self._technologies

    async def emails(self) -> list[Email]:
        """Emails known for the entity (the person's when person-level, else the company's)."""
        if self._emails is None:
            cond: list[Any] = [Email.workspace_id == self.workspace_id]
            if self.person is not None:
                cond.append(Email.person_id == self.person.id)
            elif self.company is not None:
                cond.append(Email.company_id == self.company.id)
            else:
                self._emails = []
                return self._emails
            async with session_scope() as s:
                rows = await s.scalars(
                    sa.select(Email)
                    .where(*cond)
                    .order_by(Email.is_primary.desc(), Email.overall_confidence.desc().nulls_last())
                )
                self._emails = list(rows.all())
        return self._emails


def _dtype(plan: EnrichmentPlan) -> ColumnDataType:
    return ColumnDataType(str(getattr(plan.data_type, "value", plan.data_type)))


def ok(
    plan: EnrichmentPlan,
    value: Any,
    *,
    resolver: str,
    confidence: float | None,
    evidence: str | None = None,
    source_url: str | None = None,
    source_id: str | None = "website",
    model: str | None = None,
    cost_usd: float = 0.0,
) -> CellResult:
    """A successful cell. Invariant: success cells always carry a non-null value."""
    if value is None:
        return unknown(
            plan,
            resolver=resolver,
            evidence=evidence,
            source_url=source_url,
            source_id=source_id,
            model=model,
            cost_usd=cost_usd,
        )
    return CellResult(
        status=CellStatus.success,
        value=value,
        display_value=display_for(value, plan.data_type),
        confidence=None if confidence is None else round(max(0.0, min(1.0, confidence)), 3),
        evidence=evidence,
        source_url=source_url,
        source_id=source_id,
        resolver=resolver,
        model=model,
        cost_usd=cost_usd,
    )


def unknown(
    plan: EnrichmentPlan,
    *,
    resolver: str,
    evidence: str | None = None,
    error: str | None = None,
    confidence: float | None = None,
    source_url: str | None = None,
    source_id: str | None = None,
    model: str | None = None,
    cost_usd: float = 0.0,
) -> CellResult:
    """Insufficient evidence — distinct from a false value and from a technical failure."""
    return CellResult(
        status=CellStatus.unknown,
        value=None,
        display_value="unknown" if _dtype(plan) == ColumnDataType.boolean else None,
        confidence=None if confidence is None else round(max(0.0, min(1.0, confidence)), 3),
        evidence=evidence,
        source_url=source_url,
        source_id=source_id,
        resolver=resolver,
        error=error,
        model=model,
        cost_usd=cost_usd,
    )


def failed(plan: EnrichmentPlan, *, resolver: str, error: str) -> CellResult:
    """Technical failure with a human-readable reason (retryable from the UI)."""
    return CellResult(status=CellStatus.failed, resolver=resolver, error=error)


NOT_CRAWLED = "Website not crawled"
NO_AI = "AI provider not configured"
