"""Web search layer: replaceable free search providers in front of Gemini grounding.

Routing philosophy: deterministic / public sources → cached data → crawl → **SearXNG (→ DuckDuckGo HTML)** →
Gemini Google Search grounding *only when the free search could not resolve the question*.

* ``types`` — ``SearchResult``, ``SearchPage``, the ``WebSearchProvider`` protocol, typed ``Search*Error``s.
* ``searxng`` / ``duckduckgo`` — provider adapters (SearXNG runs as the isolated ``services/searxng`` service).
* ``chain`` — ``SearchChain`` (order, health/cooldown, TTL cache, telemetry), ``discovery_chain()``,
  ``lookup_chain()``, ``gemini_fallback_only()``.
* ``assess`` — explicit sufficiency rules (``assess_companies``, ``assess_subject``).
* ``people`` — decision-maker discovery from search results (public profile titles, press snippets).
* ``telemetry`` — usage ledger ($0 for free engines) + ``scout.learning.stats`` events (optional).
"""

from scout.search.assess import Assessment, assess_any, assess_companies, assess_subject, mentions_subject
from scout.search.chain import (
    ChainResult,
    ProviderHealth,
    SearchCache,
    SearchChain,
    build_providers,
    discovery_chain,
    gemini_fallback_only,
    lookup_chain,
    reset_state,
)
from scout.search.types import (
    SearchBlockedError,
    SearchFailure,
    SearchPage,
    SearchProviderError,
    SearchRateLimitedError,
    SearchResult,
    WebSearchProvider,
)

__all__ = [
    "Assessment",
    "ChainResult",
    "ProviderHealth",
    "SearchBlockedError",
    "SearchCache",
    "SearchChain",
    "SearchFailure",
    "SearchPage",
    "SearchProviderError",
    "SearchRateLimitedError",
    "SearchResult",
    "WebSearchProvider",
    "assess_any",
    "assess_companies",
    "assess_subject",
    "build_providers",
    "discovery_chain",
    "gemini_fallback_only",
    "lookup_chain",
    "mentions_subject",
    "reset_state",
]
