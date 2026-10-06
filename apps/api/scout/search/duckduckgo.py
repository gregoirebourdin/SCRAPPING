"""DuckDuckGo HTML endpoint (free, no key) as a ``WebSearchProvider``.

Fragile by nature: one request every 2 s process-wide (``Throttle``), the shared ``search`` pool, and anomaly /
bot-challenge pages (or HTTP 202) raise ``SearchBlockedError``. Pagination follows the hidden "next page" form
(its fields are the provider ``state``). Used as the fallback behind SearXNG.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlsplit

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from scout.config import get_settings
from scout.discovery import geo
from scout.discovery.common import Throttle, http_request
from scout.errors import BlockedError, RateLimitedError
from scout.search.types import (
    SearchBlockedError,
    SearchPage,
    SearchProviderError,
    SearchRateLimitedError,
    SearchResult,
    TimeRange,
)
from scout.util.pools import pool
from scout.util.text import collapse_ws

NAME = "duckduckgo"
DEFAULT_THROTTLE = Throttle(2.0)  # be gentle with the free endpoint (shared by every caller in the process)

_ANOMALY_MARKERS = (
    "anomaly-modal",
    "anomaly_modal",
    "challenge-form",
    "bots use DuckDuckGo too",
    "/anomaly.js",
)
_DF = {"day": "d", "week": "w", "month": "m", "year": "y"}


def _unwrap(href: str | None) -> str | None:
    """DDG redirect links (//duckduckgo.com/l/?uddg=<url>) → target URL; ad links (y.js) → None."""
    if not href:
        return None
    h = href.strip()
    if h.startswith("//"):
        h = "https:" + h
    parts = urlsplit(h)
    host = (parts.hostname or "").lower()
    if (not host or host.endswith("duckduckgo.com")) and parts.path.startswith("/l/"):
        return parse_qs(parts.query).get("uddg", [None])[0]
    if host.endswith("duckduckgo.com"):
        return None  # ads (/y.js) and internal links
    return h if parts.scheme in ("http", "https") else None


def is_blocked_page(html: str) -> bool:
    return any(m in html for m in _ANOMALY_MARKERS)


def parse_results(html: str) -> tuple[list[SearchResult], dict[str, str] | None]:
    """Organic results (ads skipped) and the hidden inputs of the "next page" form, if any."""
    tree = HTMLParser(html)
    results: list[SearchResult] = []
    for node in tree.css("div.result"):
        classes = node.attributes.get("class") or ""
        if "result--ad" in classes:
            continue
        a = node.css_first("a.result__a")
        if a is None:
            continue
        url = _unwrap(a.attributes.get("href"))
        if not url:
            continue
        snip = node.css_first(".result__snippet")
        results.append(
            SearchResult(
                url=url,
                title=collapse_ws(a.text(separator=" ")),
                snippet=collapse_ws(snip.text(separator=" ")) if snip else "",
                position=len(results) + 1,
                engines=(NAME,),
            )
        )
    next_form: dict[str, str] | None = None
    best_s = -1
    for form in tree.css("div.nav-link form"):
        fields = {
            (i.attributes.get("name") or ""): (i.attributes.get("value") or "")
            for i in form.css("input[type=hidden]")
            if i.attributes.get("name")
        }
        try:
            s = int(fields.get("s", "-1"))
        except ValueError:
            continue
        if s > best_s:
            best_s, next_form = s, fields
    return results, next_form


def kl_for(region: str | None) -> str:
    """ISO country → DDG ``kl`` region code ("FR" → "fr-fr"; unknown / None → "wt-wt", worldwide)."""
    return geo.ddg_region(region) if region else "wt-wt"


def region_from_kl(kl: str | None) -> str | None:
    """Inverse of ``kl_for`` for stored DDG plans ("fr-fr" → "FR", "uk-en" → "GB", "wt-wt" → None)."""
    head = (kl or "").split("-")[0].upper()
    if not head or head == "WT":
        return None
    return {"UK": "GB"}.get(head, head)


class DuckDuckGoProvider:
    name = NAME
    cost_usd = 0.0

    def __init__(self, *, url: str | None = None, throttle: Throttle | None = None) -> None:
        self._url = url
        self.throttle = throttle or DEFAULT_THROTTLE

    @property
    def url(self) -> str:
        return self._url or get_settings().ddg_html_url

    def is_configured(self) -> bool:
        return bool(self.url)

    async def search(
        self,
        query: str,
        *,
        num: int = 10,
        lang: str | None = None,
        region: str | None = None,
        time_range: TimeRange | None = None,
        site: str | None = None,
    ) -> list[SearchResult]:
        page = await self.search_page(query, lang=lang, region=region, time_range=time_range, site=site)
        return page.results[: max(0, num)]

    async def search_page(
        self,
        query: str,
        *,
        lang: str | None = None,
        region: str | None = None,
        time_range: TimeRange | None = None,
        site: str | None = None,
        page: int = 1,
        state: dict[str, Any] | None = None,
    ) -> SearchPage:
        if state:
            form = {str(k): str(v) for k, v in state.items()}
        else:
            q = f"{query} site:{site}" if site else query
            form = {"q": q, "kl": kl_for(region)}
            if time_range in _DF:
                form["df"] = _DF[time_range]
        try:
            async with pool("search"):
                await self.throttle.wait()
                resp = await http_request(
                    "POST",
                    self.url,
                    source=NAME,
                    data=form,
                    headers={"Referer": "https://html.duckduckgo.com/", "Accept": "text/html"},
                )
        except RateLimitedError as exc:
            raise SearchRateLimitedError(str(exc), provider=NAME) from exc
        except BlockedError as exc:
            raise SearchBlockedError(str(exc), provider=NAME) from exc
        except Exception as exc:
            raise SearchProviderError(f"{NAME}: {exc}", provider=NAME) from exc
        html = resp.text
        if resp.status_code == 202 or is_blocked_page(html):
            raise SearchBlockedError(f"{NAME}: anomaly / bot challenge page", provider=NAME)
        results, next_form = parse_results(html)
        return SearchPage(results=results, provider=NAME, next_state=next_form if results else None)
