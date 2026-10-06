"""Source catalog: every discovery adapter and evidence source with its quality score (feeds confidence).

``seed_sources()`` idempotently upserts the global ``sources`` table (metadata only: health counters,
``enabled`` and ``priority`` set by operators are never overwritten).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from scout.db.engine import session_scope
from scout.db.models import Source

SourceKind = Literal["discovery", "evidence", "both"]


@dataclass(frozen=True)
class CatalogEntry:
    key: str
    name: str
    kind: SourceKind
    quality: float
    description: str
    priority: int = 0


SOURCE_CATALOG: dict[str, CatalogEntry] = {
    e.key: e
    for e in (
        # --- discovery adapters (scout.discovery.*) ---------------------------------------------
        CatalogEntry(
            "fr_registry",
            "French company registry (SIRENE / RNE)",
            "both",
            0.95,
            "recherche-entreprises.api.gouv.fr: NAF activity, headcount band, address, directors.",
            90,
        ),
        CatalogEntry(
            "google_places",
            "Google Maps (official Places API)",
            "both",
            0.9,
            "Local businesses with website, phone and exact address; free tier ~1,000 searches / month.",
            85,
        ),
        CatalogEntry(
            "google_maps",
            "Google Maps (gosom scraper service)",
            "both",
            0.75,
            "Local businesses with website, phone, address, rating (isolated scraper service).",
            80,
        ),
        CatalogEntry(
            "osm",
            "OpenStreetMap (Overpass)",
            "both",
            0.7,
            "Points of interest with website / phone / address tags.",
            60,
        ),
        CatalogEntry(
            "web_search",
            "Web search (SearXNG / DuckDuckGo)",
            "discovery",
            0.6,
            "Free web search (self-hosted SearXNG, DuckDuckGo HTML fallback), one company per domain; "
            "listicles expanded.",
            50,
        ),
        CatalogEntry(
            "gemini_search",
            "Grounded web research (Gemini + Google Search)",
            "discovery",
            0.7,
            "Segmented research questions answered with cited sources (budget-guarded).",
            40,
        ),
        CatalogEntry(
            "yc",
            "Y Combinator company directory",
            "both",
            0.85,
            "Startups with website, team size, location and one-liner.",
            70,
        ),
        CatalogEntry(
            "hn_hiring",
            "Hacker News — Who is hiring?",
            "both",
            0.6,
            "Monthly hiring threads: company, roles, location (hiring signal).",
            30,
        ),
        CatalogEntry(
            "github",
            "GitHub organisations",
            "discovery",
            0.6,
            "Organisations by keyword and location, with their website.",
            20,
        ),
        CatalogEntry(
            "fixture",
            "Fixture manifest (test / E2E)",
            "discovery",
            0.9,
            "Deterministic test data; never enabled in production.",
            0,
        ),
        # --- evidence sources (field observations) --------------------------------------------
        CatalogEntry(
            "website", "Company website", "evidence", 0.95, "The company's own website (crawled pages)."
        ),
        CatalogEntry("registry", "Official registry", "evidence", 0.95, "Official business registries."),
        CatalogEntry("public_profile", "Public profile", "evidence", 0.85, "Public professional profiles."),
        CatalogEntry("directory", "Reputable directory", "evidence", 0.75, "Reputable business directories."),
        CatalogEntry("maps", "Maps listing", "evidence", 0.75, "Business listing on a maps service."),
        CatalogEntry(
            "search_snippet", "Search snippet", "evidence", 0.6, "Search engine result titles/snippets."
        ),
        CatalogEntry(
            "grounded_search",
            "Grounded AI search",
            "evidence",
            0.7,
            "AI answers grounded on cited web sources.",
        ),
        CatalogEntry(
            "ai_extraction",
            "AI extraction",
            "evidence",
            0.75,
            "AI extraction from cached pages (quote-checked).",
        ),
        CatalogEntry(
            "tech_scan", "Technology scan", "evidence", 0.9, "Technology fingerprints of the website."
        ),
        CatalogEntry(
            "derived", "Derived", "evidence", 0.7, "Deterministically derived from other observations."
        ),
        CatalogEntry("import", "Import", "evidence", 0.8, "User-imported data (CSV)."),
        CatalogEntry("user", "User", "evidence", 1.0, "Edited or confirmed by a user."),
        CatalogEntry(
            "unverified", "Unverified aggregation", "evidence", 0.4, "Unverified third-party aggregation."
        ),
    )
}

# Alternative spellings used in docs, prompts and campaign definitions → canonical keys.
SOURCE_ALIASES: dict[str, str] = {
    "maps": "google_maps",
    "gmaps": "google_maps",
    "google": "google_maps",
    "googlemaps": "google_maps",
    "registry": "fr_registry",
    "sirene": "fr_registry",
    "fr_sirene": "fr_registry",
    "insee": "fr_registry",
    "ddg": "web_search",
    "duckduckgo": "web_search",
    "web": "web_search",
    "web_search_ddg": "web_search",
    "search": "web_search",
    "searxng": "web_search",
    "gemini": "gemini_search",
    "grounding": "gemini_search",
    "grounded": "gemini_search",
    "openstreetmap": "osm",
    "overpass": "osm",
    "ycombinator": "yc",
    "y_combinator": "yc",
    "hn": "hn_hiring",
    "hackernews": "hn_hiring",
    "hacker_news": "hn_hiring",
    "gh": "github",
}


def canonical_source_key(key: str) -> str:
    k = key.strip().lower().replace("-", "_").replace(" ", "_")
    return SOURCE_ALIASES.get(k, k)


def source_quality(key: str) -> float:
    """Quality (0–1) of a source key; unknown sources count as unverified aggregation (0.4)."""
    entry = SOURCE_CATALOG.get(key) or SOURCE_CATALOG.get(canonical_source_key(key))
    return entry.quality if entry else SOURCE_CATALOG["unverified"].quality


async def seed_sources() -> int:
    """Upsert every catalog entry into ``sources`` (idempotent). Returns the number of entries."""
    rows = [
        {
            "key": e.key,
            "name": e.name,
            "kind": e.kind,
            "description": e.description,
            "quality_score": e.quality,
            "priority": e.priority,
        }
        for e in SOURCE_CATALOG.values()
    ]
    stmt = pg_insert(Source).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=[Source.key],
        set_={
            "name": stmt.excluded.name,
            "kind": stmt.excluded.kind,
            "description": stmt.excluded.description,
            "quality_score": stmt.excluded.quality_score,
            "updated_at": sa.func.now(),
        },
    )
    async with session_scope() as s:
        await s.execute(stmt)
    return len(rows)
