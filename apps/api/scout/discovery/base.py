"""Discovery source adapter contract (spec §46, §49). No source-specific logic leaks outside scout/discovery."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from scout.schemas.campaign import CampaignDefinition


@dataclass
class RawCandidate:
    """Canonical raw structure returned by every adapter."""

    source: str  # adapter key, e.g. "fr_registry"
    source_entity_id: str | None  # stable id at the source (SIREN, place_id, URL…)
    name: str
    website: str | None = None
    domain: str | None = None  # registrable domain if known (adapters may leave None; pipeline canonicalizes)
    location: dict[str, Any] = field(
        default_factory=dict
    )  # {"country","region","city","postal_code","address","lat","lng"}
    category: str | None = None
    source_url: str | None = None
    raw_data: dict[str, Any] = field(default_factory=dict)
    observed_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    # Optional structured hints extracted deterministically by the adapter:
    phone: str | None = None
    employee_min: int | None = None
    employee_max: int | None = None
    registry_source: str | None = None  # e.g. "fr_sirene"
    registry_id: str | None = None  # e.g. SIREN
    status: str | None = None  # "active" | "closed"
    people: list[dict[str, Any]] = field(
        default_factory=list
    )  # e.g. registry directors [{"full_name","title","source_url"}]
    emails: list[str] = field(default_factory=list)


@dataclass
class DiscoveryQuery:
    """One unit of the source's query plan (serializable to JSON for campaign_sources.query_plan)."""

    key: str  # stable id within the plan, e.g. "naf:73.11Z|dep:75"
    params: dict[str, Any]
    weight: float = 1.0


@dataclass
class DiscoveryPage:
    candidates: list[RawCandidate]
    next_cursor: dict[str, Any] | None  # None = this query is exhausted
    requests: int = 1
    blocked: bool = False


class DiscoverySource(Protocol):
    key: str
    name: str
    quality: float  # source quality (0–1), feeds confidence
    cost_class: str  # FREE | CHEAP | WEB_SEARCH | EXPENSIVE

    def is_configured(self) -> bool: ...

    def suitability(self, defn: CampaignDefinition) -> float:
        """0 = not suitable; higher = better fit for this ICP."""
        ...

    def plan(self, defn: CampaignDefinition, *, expansion: int = 0) -> list[DiscoveryQuery]:
        """Query plan. `expansion` > 0 asks for broader queries once the base plan is exhausted."""
        ...

    async def discover(self, query: DiscoveryQuery, cursor: dict[str, Any] | None) -> DiscoveryPage: ...
