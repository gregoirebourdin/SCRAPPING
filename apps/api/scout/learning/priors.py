"""Default priors for Empirical Source Scoring — the only place where starting values live.

A prior is what the system believes about a resolver/source before it has evidence: precision (P(result is
correct)), coverage (P(an attempt produces a usable result)), expected cost and latency per attempt. Learned
counters (``scout.learning.stats``) move away from these values as evidence accumulates
(``scout.learning.routing.MIN_OUTCOMES`` / ``MIN_ATTEMPTS``).

Email resolvers keep their historic constants in ``scout.email.confidence.RESOLVER_PRIORS``; they are
mirrored here (read-only) so every dimension is listed in one place.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Prior:
    precision: float
    coverage: float = 0.5
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    label: str | None = None


DEFAULT_PRIOR = Prior(precision=0.5, coverage=0.5)

# Dimensions and their human labels (API / admin page ordering).
DIMENSIONS: dict[str, str] = {
    "discovery.source": "Discovery sources",
    "people.source": "People sources",
    "enrich.resolver": "Enrichment resolvers",
    "search.engine": "Search engines",
    "crawl.tier": "Crawl tiers",
    "resolver": "Email resolvers",
    "source": "Email evidence sources",
    "pattern": "Email patterns",
    "technique": "Email techniques",
    "provider": "Email providers",
}

_GROUNDED_SEARCH_USD = 0.014  # mirrors Settings.cost_grounded_search_usd (default)
_AI_CALL_USD = 0.001  # one flash-lite extraction / classification call on cached chunks

# ---- discovery sources (scout.discovery.*) ------------------------------------------------------
# precision: P(a discovered company passes company qualification | it was judged) — "ICP precision".
# One common prior for every adapter: per-source trust already lives in the catalog quality used by the
# router; learned precision moves each source relative to this shared baseline (router.learned_factor).
# coverage: P(a request returns at least one candidate).
DISCOVERY_ICP_PRIOR = 0.4
DISCOVERY_SOURCE_PRIORS: dict[str, Prior] = {
    "fr_registry": Prior(DISCOVERY_ICP_PRIOR, 0.85, 0.0, 600, "French registry (SIRENE / RNE)"),
    "google_maps": Prior(DISCOVERY_ICP_PRIOR, 0.8, 0.0, 8000, "Google Maps"),
    "osm": Prior(DISCOVERY_ICP_PRIOR, 0.7, 0.0, 3000, "OpenStreetMap"),
    "yc": Prior(DISCOVERY_ICP_PRIOR, 0.6, 0.0, 800, "Y Combinator directory"),
    "web_search": Prior(DISCOVERY_ICP_PRIOR, 0.7, 0.0, 2000, "Web search"),
    "gemini_search": Prior(DISCOVERY_ICP_PRIOR, 0.6, _GROUNDED_SEARCH_USD, 9000, "Gemini grounded search"),
    "hn_hiring": Prior(DISCOVERY_ICP_PRIOR, 0.5, 0.0, 1500, "Hacker News hiring"),
    "github": Prior(DISCOVERY_ICP_PRIOR, 0.6, 0.0, 1200, "GitHub organisations"),
    "fixture": Prior(DISCOVERY_ICP_PRIOR, 1.0, 0.0, 0, "Fixture manifest"),
}

# ---- people sources (decision-maker identity + title) -------------------------------------------
# precision: P(name + title correct for this company). Website keys are by page type (where the person was
# found); the extraction method's own constant (scout.extract.people.CONF_*) is shifted by the learned
# precision of the source (routing.blend_confidence).
PEOPLE_SOURCE_PRIORS: dict[str, Prior] = {
    "registry": Prior(0.95, 0.6, 0.0, 0, "Official registry (directors)"),
    "legal_notice": Prior(0.9, 0.5, 0.0, 0, "Legal notice page"),
    "official_team_page": Prior(0.88, 0.7, 0.0, 0, "Official team page"),
    "about_page": Prior(0.85, 0.4, 0.0, 0, "About page"),
    "home_page": Prior(0.78, 0.2, 0.0, 0, "Home page"),
    "contact_page": Prior(0.8, 0.15, 0.0, 0, "Contact page signature"),
    "website_other": Prior(0.75, 0.1, 0.0, 0, "Other website page"),
    "ai_extraction": Prior(0.75, 0.4, _AI_CALL_USD, 2500, "AI extraction (quote-checked)"),
    "gemini_grounded_result": Prior(0.7, 0.5, _GROUNDED_SEARCH_USD, 9000, "Gemini grounded result"),
    "searxng_result": Prior(0.6, 0.4, 0.0, 1500, "SearXNG result"),
    "directory": Prior(0.75, 0.3, 0.0, 1500, "Business directory"),
    "linkedin_snippet": Prior(0.7, 0.4, 0.0, 1500, "LinkedIn search snippet"),
    "import": Prior(0.8, 1.0, 0.0, 0, "CSV import"),
    "user": Prior(1.0, 1.0, 0.0, 0, "User"),
}

# ---- enrichment resolvers (keyed by plan strategy, scout.enrich.planner) ---------------------------
# precision: P(a produced cell value is right); coverage: P(a run produces a value, i.e. status success).
ENRICH_RESOLVER_PRIORS: dict[str, Prior] = {
    "keyword": Prior(0.95, 0.95, 0.0, 50, "Website keyword match"),
    "regex": Prior(0.9, 0.9, 0.0, 50, "Pattern match on website"),
    "social_profile": Prior(0.92, 0.6, 0.0, 50, "Social link on website"),
    "website_field": Prior(0.88, 0.7, 0.0, 80, "Website extractor"),
    "deterministic_field": Prior(0.95, 0.8, 0.0, 10, "Lead record"),
    "tech_detection": Prior(0.9, 0.9, 0.0, 400, "Technology detection"),
    "semantic_classifier": Prior(0.85, 0.75, _AI_CALL_USD, 2000, "AI classifier on website"),
    "ai_extraction": Prior(0.8, 0.6, _AI_CALL_USD, 2500, "AI extraction from website"),
    "web_research": Prior(0.7, 0.6, _GROUNDED_SEARCH_USD, 9000, "Web research (grounded)"),
    "generated_text": Prior(0.7, 0.9, _AI_CALL_USD, 2500, "AI writer"),
    "composite": Prior(0.85, 0.5, 0.0, 100, "Combined lookup"),
    "user": Prior(1.0, 1.0, 0.0, 0, "User"),
}

# ---- web search engines (scout.search) — coverage: P(query returns ≥ 1 usable result) --------------
SEARCH_ENGINE_PRIORS: dict[str, Prior] = {
    "searxng": Prior(0.6, 0.7, 0.0, 1500, "SearXNG"),
    "duckduckgo": Prior(0.55, 0.6, 0.0, 2000, "DuckDuckGo HTML"),
    "gemini_grounded": Prior(0.7, 0.8, _GROUNDED_SEARCH_USD, 9000, "Gemini grounded search"),
}

# ---- crawl tiers (scout.crawl) — coverage: P(fetch returns usable content) --------------------------
CRAWL_TIER_PRIORS: dict[str, Prior] = {
    "http": Prior(0.9, 0.8, 0.00002, 800, "Plain HTTP"),
    "crawl4ai": Prior(0.9, 0.9, 0.0001, 3000, "Crawl4AI"),
    "browser": Prior(0.92, 0.95, 0.0004, 8000, "Headless browser"),
}

_STATIC: dict[str, dict[str, Prior]] = {
    "discovery.source": DISCOVERY_SOURCE_PRIORS,
    "people.source": PEOPLE_SOURCE_PRIORS,
    "enrich.resolver": ENRICH_RESOLVER_PRIORS,
    "search.engine": SEARCH_ENGINE_PRIORS,
    "crawl.tier": CRAWL_TIER_PRIORS,
}


def email_resolver_priors() -> dict[str, Prior]:
    """Mirror of ``scout.email.confidence.RESOLVER_PRIORS`` (the email engine keeps using its own dict)."""
    try:
        from scout.email.confidence import RESOLVER_PRIORS
    except ImportError:  # pragma: no cover - email engine always present
        return {}
    return {k: Prior(precision=v, coverage=1.0) for k, v in RESOLVER_PRIORS.items()}


def priors_for(dimension: str) -> dict[str, Prior]:
    """Known priors for a dimension (empty for open-ended dimensions such as email ``pattern``)."""
    if dimension == "resolver":
        return email_resolver_priors()
    return dict(_STATIC.get(dimension, {}))


def known(dimension: str, key: str) -> Prior | None:
    return priors_for(dimension).get(key)


def prior(dimension: str, key: str) -> Prior:
    """Prior for ``(dimension, key)``; ``DEFAULT_PRIOR`` (0.5 / 0.5) for unknown keys."""
    return known(dimension, key) or DEFAULT_PRIOR


def label(dimension: str, key: str) -> str:
    p = known(dimension, key)
    return (p.label if p and p.label else None) or key.replace("_", " ")
