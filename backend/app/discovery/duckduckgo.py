"""DuckDuckGo HTML endpoint (``html.duckduckgo.com``) with real pagination.

DDG answers with HTTP 202 + a "select all squares containing a duck" challenge when it suspects a bot,
which is reported as ``EngineBlocked`` so the router cools the engine down.
"""

from __future__ import annotations

import html as htmllib
import re
from urllib.parse import parse_qs, urlparse

from selectolax.parser import HTMLParser

from ..config import settings
from ..fetch.http import Fetcher
from ..util.ua import browser_headers
from .base import EngineBlocked, EngineError, SearchProvider, SerpResult

_KL = {"us": "us-en", "gb": "uk-en", "uk": "uk-en", "au": "au-en", "ca": "ca-en", "nz": "nz-en", "ie": "ie-en", "sg": "sg-en", "in": "in-en", "za": "za-en"}


def _unwrap(href: str) -> str:
    href = htmllib.unescape(href)
    if href.startswith("//"):
        href = "https:" + href
    if "duckduckgo.com/l/" in href:
        q = parse_qs(urlparse(href).query)
        return q.get("uddg", [href])[0]
    return href


class DuckDuckGoProvider(SearchProvider):
    name = "duckduckgo"
    supports_pagination = True

    def __init__(self, fetcher: Fetcher) -> None:
        super().__init__()
        self.fetcher = fetcher

    async def _request(self, data: dict[str, str] | None, params: dict[str, str] | None) -> str:
        hdrs = browser_headers()
        hdrs["Referer"] = "https://html.duckduckgo.com/"
        try:
            async with self.fetcher.throttle("duckduckgo", concurrency=1, min_delay=settings.search_min_delay):
                if data is not None:
                    r = await self.fetcher.client.post("https://html.duckduckgo.com/html/", data=data, headers=hdrs)
                else:
                    r = await self.fetcher.client.get("https://html.duckduckgo.com/html/", params=params, headers=hdrs)
        except Exception as e:
            raise EngineError(f"{type(e).__name__}: {e}") from e
        if r.status_code in (202, 403, 429) or "bots use duckduckgo too" in r.text.lower() or "challenge" in r.text.lower()[:3000]:
            raise EngineBlocked(f"challenge_{r.status_code}")
        if r.status_code != 200:
            raise EngineError(f"http_{r.status_code}")
        return r.text

    async def search(self, query: str, *, country: str = "us", pages: int = 1) -> list[SerpResult]:
        kl = _KL.get((country or "us").lower(), "us-en")
        html = await self._request(None, {"q": query, "kl": kl})
        results = self.parse(html, query, country)
        rank = len(results)
        page = 1
        while page < pages and results:
            nxt = self._next_form(html)
            if not nxt:
                break
            html = await self._request(nxt, None)
            more = self.parse(html, query, country, start_rank=rank)
            if not more:
                break
            results.extend(more)
            rank += len(more)
            page += 1
        return results

    @staticmethod
    def _next_form(html: str) -> dict[str, str] | None:
        for form in re.findall(r"<form[^>]*action=\"/html/\"[^>]*>(.*?)</form>", html, re.S):
            if "Next" not in form:
                continue
            inputs = dict(re.findall(r"<input[^>]*name=\"([^\"]+)\"[^>]*value=\"([^\"]*)\"", form))
            if "s" in inputs and "q" in inputs:
                return {k: htmllib.unescape(v) for k, v in inputs.items()}
        return None

    @staticmethod
    def parse(html: str, query: str, country: str, start_rank: int = 0) -> list[SerpResult]:
        tree = HTMLParser(html)
        out: list[SerpResult] = []
        rank = start_rank
        for res in tree.css("div.result"):
            a = res.css_first("a.result__a")
            if a is None:
                continue
            href = _unwrap(a.attributes.get("href") or "")
            if not href.startswith("http"):
                continue
            if "ad_provider" in (res.attributes.get("class") or "") or "result--ad" in (res.attributes.get("class") or ""):
                continue
            sn = res.css_first(".result__snippet")
            rank += 1
            out.append(SerpResult(engine="duckduckgo", query=query, country=country, rank=rank, url=href, title=a.text(strip=True)[:300], snippet=(sn.text(strip=True) if sn else "")[:500]))
        return out
