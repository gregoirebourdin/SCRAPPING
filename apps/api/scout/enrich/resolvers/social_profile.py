"""Social profile strategy: the network's profile link from cached pages (no AI). The absence of a link is
not proof that no account exists, so it yields `unknown`, never `false`."""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlsplit

from scout.db.enums import ColumnDataType
from scout.enrich.planner import NETWORK_LABELS
from scout.enrich.resolvers.base import NOT_CRAWLED, ResolveContext, ok, ordered_pages, unknown
from scout.enrich.types import CellResult

RESOLVER = "social_link"

NETWORK_HOSTS: dict[str, tuple[str, ...]] = {
    "instagram": ("instagram.com",),
    "linkedin": ("linkedin.com",),
    "facebook": ("facebook.com", "fb.com", "fb.me"),
    "tiktok": ("tiktok.com",),
    "youtube": ("youtube.com", "youtu.be"),
    "twitter": ("twitter.com", "x.com"),
    "pinterest": ("pinterest.com", "pinterest.fr"),
}
_NON_PROFILE = re.compile(
    r"/(?:sharer|share|intent|sharearticle|dialog|plugins|p|reel|reels|watch|embed|hashtag|explore|tr|events?|"
    r"posts?|status|feed|search|login|signup|legal|policies|privacy|help)(?:[/?.]|$)",
    re.I,
)


def iter_links(page: Any) -> Iterator[tuple[str, str]]:
    """(url, anchor text) pairs from a cached page's `links` (social, internal, external)."""
    links = getattr(page, "links", None) or {}
    social = links.get("social") or {}
    if isinstance(social, dict):
        for value in social.values():
            for url in value if isinstance(value, list) else [value]:
                if isinstance(url, str):
                    yield url, ""
    for bucket in ("internal", "external"):
        for item in links.get(bucket) or []:
            if isinstance(item, dict) and isinstance(item.get("url"), str):
                yield item["url"], str(item.get("text") or "")
            elif isinstance(item, str):
                yield item, ""


def host_of(url: str) -> str:
    try:
        host = (urlsplit(url if "://" in url else f"https://{url}").hostname or "").lower()
    except ValueError:
        return ""
    return host.removeprefix("www.").removeprefix("m.").removeprefix("fr.")


def is_profile_url(url: str, network: str) -> bool:
    host = host_of(url)
    if not any(host == h or host.endswith("." + h) for h in NETWORK_HOSTS.get(network, ())):
        return False
    path = urlsplit(url if "://" in url else f"https://{url}").path or ""
    if path.strip("/") == "" or _NON_PROFILE.search(path):
        return False
    return True


def _rank(url: str, network: str) -> int:
    if network == "linkedin":
        return 0 if "/company/" in url or "/school/" in url else 1
    return 0


def find_profile(pages: list[Any], network: str) -> tuple[str, Any] | None:
    """Best profile URL for `network` with the page it was found on (home page first)."""
    for page in ordered_pages(pages):
        links = getattr(page, "links", None) or {}
        social = links.get("social") or {}
        direct = social.get(network) or (social.get("x") if network == "twitter" else None)
        candidates = [direct] if isinstance(direct, str) else list(direct or [])
        candidates += [u for u, _ in iter_links(page)]
        valid = [u for u in candidates if isinstance(u, str) and is_profile_url(u, network)]
        if valid:
            return sorted(valid, key=lambda u: (_rank(u, network), len(u)))[0], page
    return None


async def resolve(rc: ResolveContext) -> CellResult:
    plan = rc.plan
    network = (plan.field or "").lower()
    label = NETWORK_LABELS.get(network, network.title() or "social")
    boolean = plan.data_type == ColumnDataType.boolean
    found = find_profile(rc.pages, network) if rc.pages else None
    if found:
        url, page = found
        return ok(plan, True if boolean else url, resolver=RESOLVER, confidence=0.95,
                  evidence=f"{label} profile linked from {page.url}" + (f": {url}" if boolean else ""),
                  source_url=page.url)
    if network == "linkedin" and rc.company is not None and rc.company.linkedin_url:
        url = rc.company.linkedin_url
        return ok(plan, True if boolean else url, resolver="company_record", confidence=0.85,
                  evidence="LinkedIn URL from the company record", source_url=url, source_id="company_record")
    if not rc.pages:
        return unknown(plan, resolver=RESOLVER, error=NOT_CRAWLED)
    return unknown(plan, resolver=RESOLVER, evidence=f"No {label} link found on crawled pages", source_id="website")
