"""GitHub organisations as a discovery source for software / tech ICPs (search users?type=org + org details).

Token optional (``GITHUB_TOKEN``): unauthenticated search is limited to 10 req/min and org lookups to 60/h.
Rate-limit responses (403/429 with ``X-RateLimit-Remaining: 0``) raise ``RateLimitedError``.
"""

from __future__ import annotations

import time
from typing import Any

import structlog

from scout.config import get_settings
from scout.db.enums import UsageCategory
from scout.discovery import geo
from scout.discovery.base import DiscoveryPage, DiscoveryQuery, RawCandidate
from scout.discovery.common import (
    Throttle,
    candidate_domain,
    check_status,
    http_request,
    is_digital_icp,
    profiles_for,
    target_countries,
)
from scout.errors import RateLimitedError
from scout.schemas.campaign import CampaignDefinition
from scout.services.usage import record_usage
from scout.util.pools import pool
from scout.util.urls import normalize_website

log = structlog.get_logger(__name__)

MAX_RESULTS = 300       # search API serves ≤ 1,000 results; we never need that deep
_throttle = Throttle(2.0)
_TECH = {"software_saas", "ai_company", "fintech", "healthtech", "it_services", "cybersecurity", "web_agency", "digital_agency"}
_GH_KEYWORDS = {
    "software_saas": ["saas", "software"], "ai_company": ["ai", "machine learning"], "fintech": ["fintech"],
    "healthtech": ["health"], "it_services": ["consulting", "devops"], "cybersecurity": ["security"],
    "web_agency": ["web agency", "agency"], "digital_agency": ["digital agency", "agency"],
}


def _rate_limited(resp: Any) -> bool:
    return resp.status_code in (403, 429) and (
        resp.headers.get("x-ratelimit-remaining") == "0" or "rate limit" in resp.text.lower()
    )


class GitHubSource:
    key = "github"
    name = "GitHub organisations"
    quality = 0.6
    cost_class = "FREE"

    def __init__(self, *, throttle: Throttle | None = None, per_page: int = 15) -> None:
        self.throttle = throttle or _throttle
        self.per_page = per_page  # each org costs one extra API call

    def is_configured(self) -> bool:
        return bool(get_settings().github_api_url)

    def suitability(self, defn: CampaignDefinition) -> float:
        profiles = profiles_for(defn)
        if not profiles or not is_digital_icp(profiles) or profiles[0].key not in _TECH:
            return 0.0
        return 0.6 if get_settings().github_token else 0.4

    def plan(self, defn: CampaignDefinition, *, expansion: int = 0) -> list[DiscoveryQuery]:
        if self.suitability(defn) <= 0:
            return []
        cf = defn.company_filters
        profiles = [p for p in profiles_for(defn) if p.key in _TECH][:2]
        keywords = list(dict.fromkeys(k for p in profiles for k in _GH_KEYWORDS.get(p.key, [])))[: 1 + expansion]
        locations: list[str] = []
        for cc in target_countries(defn)[:2]:
            if cf.cities:
                locations.extend(cf.cities)
            else:
                locations.extend(c.name for c in geo.cities_for(cc, regions=cf.regions, expansion=expansion)[: 3 + 3 * expansion])
                locations.append(geo.country_name(cc))
        if not locations:
            locations = list(cf.cities) or [""]
        queries = []
        for kw in keywords:
            for rank, loc in enumerate(dict.fromkeys(locations)):
                q = f'{kw} type:org' + (f' location:"{loc}"' if loc else "")
                queries.append(DiscoveryQuery(key=f"gh:{q.lower()}", params={"q": q}, weight=1.0 / (1 + 0.1 * rank)))
        return queries

    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        token = get_settings().github_token
        if token and token.get_secret_value():
            h["Authorization"] = f"Bearer {token.get_secret_value()}"
        return h

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = get_settings().github_api_url.rstrip("/") + path
        async with pool("public_api"):
            await self.throttle.wait()
            resp = await http_request("GET", url, source=self.key, params=params, headers=self._headers(),
                                      allow=(403, 404, 429))
        await record_usage(UsageCategory.registry_request, source_key=self.key, resolver="discovery")
        if _rate_limited(resp):
            reset = resp.headers.get("x-ratelimit-reset")
            wait = max(0, int(reset) - int(time.time())) if reset and reset.isdigit() else None
            raise RateLimitedError(f"github: rate limited{f' (resets in {wait}s)' if wait is not None else ''}")
        if resp.status_code == 404:
            return {}
        check_status(resp, self.key)
        return resp.json()

    async def discover(self, query: DiscoveryQuery, cursor: dict[str, Any] | None) -> DiscoveryPage:
        page = int((cursor or {}).get("page", 1))
        body = await self._get("/search/users", {"q": query.params["q"], "per_page": self.per_page, "page": page})
        items = [i for i in body.get("items") or [] if (i.get("type") or "").lower() == "organization"]
        candidates: list[RawCandidate] = []
        requests = 1
        for item in items:
            login = item.get("login")
            if not login:
                continue
            org = await self._get(f"/orgs/{login}")
            requests += 1
            blog = (org.get("blog") or "").strip()
            dom = candidate_domain(blog)
            if not dom:
                continue
            city = geo.find_city((org.get("location") or "").split(",")[0].strip()) if org.get("location") else None
            candidates.append(
                RawCandidate(
                    source=self.key,
                    source_entity_id=login,
                    name=(org.get("name") or login).strip(),
                    website=normalize_website(blog),
                    domain=dom,
                    location={"city": city.name, "country": city.country} if city else {},
                    source_url=org.get("html_url") or f"https://github.com/{login}",
                    emails=[org["email"].lower()] if org.get("email") else [],
                    raw_data={
                        "login": login,
                        "description": org.get("description"),
                        "location_raw": org.get("location"),
                        "public_repos": org.get("public_repos"),
                        "followers": org.get("followers"),
                        "created_at": org.get("created_at"),
                        "query": query.params["q"],
                    },
                )
            )
        total = int(body.get("total_count") or 0)
        more = bool(body.get("items")) and page * self.per_page < min(total, MAX_RESULTS)
        return DiscoveryPage(candidates=candidates, next_cursor={"page": page + 1} if more else None, requests=requests)
