"""Bing web search.

Two transports share one parser:

* **http** — plain ``httpx`` GET with a warmed-up cookie jar (fast, cheap).  Under anti-bot pressure Bing does not
  block: it silently answers a *truncated* query ("we help coaches ..." → pages about "we").  The router measures
  result relevance and raises ``EngineBlocked("degraded")`` for such answers; the provider then switches itself to
* **browser** — the same SERP loaded in the headless Chromium (persistent cookies), which Bing treats as a person.

Links are wrapped in ``bing.com/ck/a?...&u=a1<base64>`` and are decoded here.  Anonymous pagination is ignored by
Bing, so breadth comes from the query matrix and ``cc`` rotation.
"""

from __future__ import annotations

import asyncio
import base64
import html as htmllib
import logging
import random
import re
from urllib.parse import parse_qs, urlencode, urlparse

from selectolax.parser import HTMLParser

from ..config import settings
from ..fetch.browser import get_browser
from ..fetch.http import Fetcher
from ..util.ua import browser_headers, random_ua
from .base import EngineBlocked, EngineError, SearchProvider, SerpResult

log = logging.getLogger(__name__)

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
        self.use_browser = False
        self._ua = random_ua()
        self._warmed = False
        self._browser_lock = asyncio.Lock()

    def switch_to_browser(self) -> bool:
        """Called by the router after degraded answers.  Returns True when the switch happened."""
        if self.use_browser or not get_browser().available:
            return False
        self.use_browser = True
        log.warning("bing: degraded answers over http, switching to the browser transport")
        return True

    async def _warm(self) -> None:
        if self._warmed:
            return
        try:
            await self.fetcher.client.get("https://www.bing.com/", headers=browser_headers(self._ua))
        except Exception:
            pass
        self._warmed = True

    async def _search_http(self, query: str, cc: str) -> str:
        await self._warm()
        params = {"q": query, "cc": cc, "setlang": "en", "mkt": _MARKET.get(cc, "en-US"), "count": "30", "FORM": "QBLH"}
        hdrs = browser_headers(self._ua)
        hdrs["Referer"] = "https://www.bing.com/"
        try:
            async with self.fetcher.throttle("bing", concurrency=1, min_delay=settings.search_min_delay + random.uniform(0.0, 1.5)):
                r = await self.fetcher.client.get("https://www.bing.com/search", params=params, headers=hdrs)
        except Exception as e:
            raise EngineError(f"{type(e).__name__}: {e}") from e
        if r.status_code in (429, 403):
            raise EngineBlocked(f"http_{r.status_code}")
        if r.status_code != 200:
            raise EngineError(f"http_{r.status_code}")
        return r.text

    async def _search_browser(self, query: str, cc: str) -> str:
        browser = get_browser()
        url = "https://www.bing.com/search?" + urlencode({"q": query, "cc": cc, "setlang": "en", "mkt": _MARKET.get(cc, "en-US")})
        async with self._browser_lock:
            await asyncio.sleep(settings.search_min_delay + random.uniform(0.5, 2.0))
            try:
                async with browser.page(persistent_key="bing") as page:
                    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    try:
                        await page.wait_for_selector("li.b_algo, .b_no, #b_results", timeout=8000)
                    except Exception:
                        pass
                    await page.wait_for_timeout(600 + random.randint(0, 600))
                    return await page.content()
            except Exception as e:
                raise EngineError(f"browser: {type(e).__name__}: {str(e)[:120]}") from e

    async def search(self, query: str, *, country: str = "us", pages: int = 1) -> list[SerpResult]:
        cc = (country or "us").lower()
        html = await (self._search_browser(query, cc) if self.use_browser else self._search_http(query, cc))
        results = self.parse(html, query, cc)
        if not results:
            low = html.lower()
            if "captcha" in low or "unusual traffic" in low or "verify you are" in low or "/challenge" in low:
                raise EngineBlocked("captcha")
            if 'class="b_no"' in low or "no results" in low or "there are no results" in low:
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
