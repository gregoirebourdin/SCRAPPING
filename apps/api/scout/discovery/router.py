"""Source router: which adapters a campaign should use, and in what order.

score = suitability × quality × cost factor × health factor. Unconfigured, excluded and cooling-down sources are
skipped; preferred sources are boosted to the front. When the fixture manifest is configured (test/dev), it is
used alone for determinism.
"""

from __future__ import annotations

from functools import lru_cache

from scout.discovery.base import DiscoverySource
from scout.discovery.catalog import canonical_source_key
from scout.discovery.health import SourceHealth
from scout.schemas.campaign import CampaignDefinition

_COST_FACTOR = {"FREE": 1.0, "CHEAP": 1.0, "AI": 0.9, "WEB_SEARCH": 0.85, "EXPENSIVE": 0.7}


@lru_cache(maxsize=1)
def _registry() -> tuple[DiscoverySource, ...]:
    from scout.discovery.fixture import FixtureSource
    from scout.discovery.fr_registry import FrRegistrySource
    from scout.discovery.gemini_search import GeminiSearchSource
    from scout.discovery.github import GitHubSource
    from scout.discovery.google_maps import GoogleMapsSource
    from scout.discovery.hn import HNHiringSource
    from scout.discovery.osm import OsmSource
    from scout.discovery.web_search import WebSearchSource
    from scout.discovery.yc import YCSource

    return (
        FixtureSource(),
        FrRegistrySource(),
        GoogleMapsSource(),
        OsmSource(),
        YCSource(),
        WebSearchSource(),
        GeminiSearchSource(),
        HNHiringSource(),
        GitHubSource(),
    )


def all_sources() -> list[DiscoverySource]:
    """Every discovery adapter (configured or not)."""
    return list(_registry())


def get_source(key: str) -> DiscoverySource | None:
    k = canonical_source_key(key)
    return next((s for s in _registry() if s.key == k), None)


def health_factor(h: SourceHealth | None) -> float:
    """1.0 without data; penalises failures, blocks and empty results once there is enough evidence."""
    if h is None or h.requests < 5:
        return 1.0
    f = (0.4 + 0.6 * h.success_rate) * (1.0 - 0.5 * h.block_rate)
    if h.requests >= 10 and h.results_per_query <= 0.0:
        f *= 0.5
    if h.requests >= 20 and h.qualification_rate > 0:
        f *= min(1.2, 0.9 + h.qualification_rate)  # sources that actually produce qualified leads float up
    return max(0.05, min(1.2, f))


def score_source(src: DiscoverySource, defn: CampaignDefinition, health: SourceHealth | None = None) -> float:
    suit = src.suitability(defn)
    if suit <= 0:
        return 0.0
    return suit * src.quality * _COST_FACTOR.get(src.cost_class, 1.0) * health_factor(health)


def select_sources(
    defn: CampaignDefinition,
    *,
    health: dict[str, SourceHealth] | None = None,
    limit: int = 4,
) -> list[tuple[DiscoverySource, int]]:
    """Ranked (source, priority) pairs, priority ints strictly descending.

    Local businesses → Maps / OSM / registry first; digital & SaaS → YC / web search / grounded search / GitHub;
    French B2B → the registry first (encoded in each adapter's ``suitability``).
    """
    excluded = {canonical_source_key(k) for k in defn.sources.excluded}
    preferred = [canonical_source_key(k) for k in defn.sources.preferred]
    health = health or {}

    fixture = get_source("fixture")
    if fixture is not None and "fixture" not in excluded and fixture.is_configured():
        return [(fixture, 100)]

    scored: list[tuple[float, DiscoverySource]] = []
    for src in _registry():
        if src.key == "fixture" or src.key in excluded or not src.is_configured():
            continue
        h = health.get(src.key)
        if h is not None and not h.healthy:
            continue
        score = score_source(src, defn, h)
        if score <= 0:
            continue
        if src.key in preferred:
            score += 10.0 - preferred.index(src.key) * 0.1
        scored.append((score, src))
    scored.sort(key=lambda x: -x[0])
    n_pref = sum(1 for _, s in scored if s.key in preferred)
    chosen = scored[: max(limit, n_pref)]

    out: list[tuple[DiscoverySource, int]] = []
    prev = 10_000
    for score, src in chosen:
        prio = max(1, min(prev - 1, round(score * 100)))
        out.append((src, prio))
        prev = prio
    return out
