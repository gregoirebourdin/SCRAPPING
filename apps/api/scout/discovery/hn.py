"""Hacker News "Ask HN: Who is hiring?" threads (Algolia HN API). Free; startups/tech ICPs only.

Each top-level comment's first line usually reads ``Company | Role | Location | URL``. A comment becomes a
candidate only when it names a company and links a company website; the comment text is kept as a hiring
signal in ``raw_data``.
"""

from __future__ import annotations

import html as html_lib
import re
from typing import Any

import structlog

from scout.config import get_settings
from scout.db.enums import UsageCategory
from scout.discovery import geo
from scout.discovery.base import DiscoveryPage, DiscoveryQuery, RawCandidate
from scout.discovery.common import (
    candidate_domain,
    http_request,
    is_digital_icp,
    profiles_for,
    target_countries,
)
from scout.schemas.campaign import CampaignDefinition
from scout.services.usage import record_usage
from scout.util.pools import pool
from scout.util.text import collapse_ws, normalize_key
from scout.util.urls import normalize_website

log = structlog.get_logger(__name__)

HITS_PER_PAGE = 100
_TITLE_RE = re.compile(r"^Ask HN: Who is hiring\?", re.I)
_HREF_RE = re.compile(r'href="([^"]+)"', re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_URL_RE = re.compile(r"https?://[^\s|<>\"')]+", re.I)
_LOCATION_HINT = re.compile(r"\b(remote|onsite|on-site|hybrid|in-office|relocation)\b", re.I)
_TECH_TAGS = {"software_saas", "ai_company", "fintech", "healthtech", "it_services", "cybersecurity", "ecommerce_brand"}


def _to_text(fragment: str) -> str:
    text = fragment.replace("<p>", "\n").replace("<br>", "\n").replace("<br/>", "\n")
    text = _TAG_RE.sub("", text)
    return html_lib.unescape(text)


def parse_comment(comment_html: str) -> dict[str, Any] | None:
    """First line → {company, roles, location, url, first_line, text}; None when not in the usual format."""
    if not comment_html:
        return None
    raw_first = comment_html.split("<p>", 1)[0]
    first_line = collapse_ws(_to_text(raw_first).split("\n", 1)[0])
    parts = [p.strip() for p in first_line.split("|") if p.strip()]
    if len(parts) < 2:
        return None
    company = re.sub(r"\s*\((YC|W|S)[^)]*\)\s*$", "", parts[0]).strip()
    if not company or len(company) > 80 or company.lower().startswith(("http", "we ", "hiring", "senior ", "remote")):
        return None
    hrefs = [html_lib.unescape(h) for h in _HREF_RE.findall(comment_html)]
    urls = hrefs + _URL_RE.findall(_to_text(comment_html))
    website = next((u for u in urls if candidate_domain(u)), None)
    location = next((p for p in parts[1:] if _LOCATION_HINT.search(p) or geo.find_city(p.split(",")[0])), None)
    roles = [p for p in parts[1:] if p != location and not _URL_RE.match(p)]
    return {
        "company": company,
        "roles": roles[:5],
        "location": location,
        "url": website,
        "first_line": first_line[:300],
        "text": _to_text(comment_html)[:4000],
    }


def _location_ok(info: dict[str, Any], countries: list[str], cities: list[str]) -> bool:
    if not countries and not cities:
        return True
    hay = normalize_key(f"{info.get('location') or ''} {info.get('first_line') or ''}")
    for city in cities:
        if normalize_key(city) in hay:
            return True
    for cc in countries:
        names = {geo.country_name(cc), geo.country_name(cc, "fr")}
        if cc == "US":
            names |= {"USA", "US"}
        if cc == "GB":
            names |= {"UK", "London"}
        if any(f" {normalize_key(n)} " in f" {hay} " for n in names):
            return True
        cities_cc = geo.country_cities(cc)
        if any(f" {normalize_key(n)} " in f" {hay} " for c in cities_cc for n in (c.name, *c.aliases)):
            return True
    return False


class HNHiringSource:
    key = "hn_hiring"
    name = "Hacker News — Who is hiring?"
    quality = 0.6
    cost_class = "FREE"

    def __init__(self) -> None:
        self._stories: list[dict[str, Any]] | None = None

    def is_configured(self) -> bool:
        return bool(get_settings().hn_algolia_url)

    def suitability(self, defn: CampaignDefinition) -> float:
        profiles = profiles_for(defn)
        if not profiles or not is_digital_icp(profiles) or profiles[0].key not in _TECH_TAGS:
            return 0.0
        return 0.6

    def plan(self, defn: CampaignDefinition, *, expansion: int = 0) -> list[DiscoveryQuery]:
        if self.suitability(defn) <= 0:
            return []
        params = {"countries": target_countries(defn), "cities": list(defn.company_filters.cities)}
        return [
            DiscoveryQuery(key=f"hn:who_is_hiring:{m}", params={**params, "months_back": m}, weight=1.0 / (1 + m))
            for m in range(0, 2 + 2 * expansion)
        ]

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        url = get_settings().hn_algolia_url.rstrip("/") + path
        async with pool("public_api"):
            resp = await http_request("GET", url, source=self.key, params=params)
        await record_usage(UsageCategory.registry_request, source_key=self.key, resolver="discovery")
        return resp.json()

    async def _story(self, months_back: int) -> dict[str, Any] | None:
        if self._stories is None:
            body = await self._get("/search_by_date", {"tags": "story,author_whoishiring", "hitsPerPage": 30})
            self._stories = [h for h in body.get("hits") or [] if _TITLE_RE.match(h.get("title") or "")]
        return self._stories[months_back] if months_back < len(self._stories) else None

    async def discover(self, query: DiscoveryQuery, cursor: dict[str, Any] | None) -> DiscoveryPage:
        cur = dict(cursor or {})
        story_id = cur.get("story_id")
        title = cur.get("story_title")
        if not story_id:
            story = await self._story(int(query.params.get("months_back", 0)))
            if story is None:
                return DiscoveryPage([], next_cursor=None)
            story_id, title = str(story["objectID"]), story.get("title")
        page = int(cur.get("page", 0))
        body = await self._get("/search", {"tags": f"comment,story_{story_id}", "hitsPerPage": HITS_PER_PAGE, "page": page})
        countries, cities = query.params.get("countries") or [], query.params.get("cities") or []
        candidates: list[RawCandidate] = []
        for hit in body.get("hits") or []:
            if str(hit.get("parent_id")) != str(story_id):
                continue  # replies, not job posts
            info = parse_comment(hit.get("comment_text") or "")
            if not info or not info["url"] or not _location_ok(info, countries, cities):
                continue
            dom = candidate_domain(info["url"])
            city = geo.find_city((info.get("location") or "").split(",")[0].strip()) if info.get("location") else None
            candidates.append(
                RawCandidate(
                    source=self.key,
                    source_entity_id=str(hit.get("objectID")),
                    name=info["company"],
                    website=normalize_website(info["url"]),
                    domain=dom,
                    location={"city": city.name, "country": city.country} if city else {},
                    source_url=f"https://news.ycombinator.com/item?id={hit.get('objectID')}",
                    raw_data={
                        "signal": "hiring",
                        "story_id": story_id,
                        "story_title": title,
                        "posted_at": hit.get("created_at"),
                        "first_line": info["first_line"],
                        "roles": info["roles"],
                        "location_raw": info["location"],
                        "comment_text": info["text"],
                    },
                )
            )
        nb_pages = int(body.get("nbPages") or 0)
        nxt = {"story_id": story_id, "story_title": title, "page": page + 1} if page + 1 < nb_pages else None
        return DiscoveryPage(candidates=candidates, next_cursor=nxt)
