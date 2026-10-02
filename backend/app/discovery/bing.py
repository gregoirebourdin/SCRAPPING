"""Bing web search over plain HTTP.

Bing serves a fully static SERP; links are wrapped in ``bing.com/ck/a?...&u=a1<base64>`` and are decoded here.
Pagination is not honoured for anonymous clients, so breadth comes from the query matrix and ``cc`` rotation.
"""

from __future__ import annotations

import base64
import html as htmllib
import re
from urllib.parse import parse_qs, urlparse

from selectolax.parser import HTMLParser

from ..config import settings
from ..fetch.http import Fetcher
from ..util.ua import browser_headers
from .base import EngineBlocked, EngineError, SearchProvider, SerpResult

_MARKET = {"us": "en-US", "gb": "en-GB", "uk": "en-GB", "au": "en-AU", "ca": "en-CA", "nz": "en-NZ", "ie": "en-IE", "sg": "en-SG", "in": "en-IN", "za": "en-ZA"}


def decode_bing_url(u: str) -> str:
    if "bing.com/ck/a" not in u:
        return u
    q = parse_qs(urlparse(u).query)
    raw = q.get("u", [""])[0]
    if raw.startswith("a1"):
        raw = raw[2:]
    raw += "=" * (-len(raw) % 4)
    try:
        return base64.urlsafe_b64decode(raw).decode("utf-8", "ignore")
    except Exception:
        return u


class BingProvider(SearchProvider):
    name = "bing"

    def __init__(self, fetcher: Fetcher) -> None:
        super().__init__()
        self.fetcher = fetcher

    async def search(self, query: str, *, country: str = "us", pages: int = 1) -> list[SerpResult]:
        cc = (country or "us").lower()
        params = {"q": query, "cc": cc, "setlang": "en", "mkt": _MARKET.get(cc, "en-US"), "count": "30"}
        try:
            async with self.fetcher.throttle("bing", concurrency=1, min_delay=settings.search_min_delay):
                r = await self.fetcher.client.get("https://www.bing.com/search", params=params, headers=browser_headers())
        except Exception as e:
            raise EngineError(f"{type(e).__name__}: {e}") from e
        if r.status_code == 429 or r.status_code == 403:
            raise EngineBlocked(f"http_{r.status_code}")
        if r.status_code != 200:
            raise EngineError(f"http_{r.status_code}")
        html = r.text
        results = self.parse(html, query, cc)
        if not results:
            low = html.lower()
            if "captcha" in low or "unusual traffic" in low or "verify you are" in low:
                raise EngineBlocked("captcha")
            if "b_no" in low or "no results" in low or "there are no results" in low:
                return []
            raise EngineError("empty_parse")
        return results

    @staticmethod
    def parse(html: str, query: str, country: str) -> list[SerpResult]:
        tree = HTMLParser(html)
        out: list[SerpResult] = []
        rank = 0
        for li in tree.css("li.b_algo"):
            a = li.css_first("h2 a") or li.css_first("a[href]")
            if a is None:
                continue
            href = decode_bing_url(htmllib.unescape(a.attributes.get("href") or ""))
            if not href.startswith("http"):
                continue
            title = a.text(strip=True)
            snippet = ""
            cap = li.css_first(".b_caption p") or li.css_first("p")
            if cap is not None:
                snippet = cap.text(strip=True)
            rank += 1
            out.append(SerpResult(engine="bing", query=query, country=country, rank=rank, url=href, title=title[:300], snippet=re.sub(r"\s+", " ", snippet)[:500]))
        return out
