"""Google web search through a real headless Chromium.

Google refuses non-JS clients, so each query is typed into the search box of a persistent browser context
(cookies survive between queries, which is what makes consecutive searches look human).  A redirect to
``/sorry/`` (captcha) raises ``EngineBlocked``; the router then cools Google down and leans on the other engines.
Datacenter IPs are usually captcha'd immediately — from a residential connection this provider works well.
"""

from __future__ import annotations

import asyncio
import logging
import random

from ..config import settings
from ..fetch.browser import get_browser
from .base import EngineBlocked, EngineError, SearchProvider, SerpResult

log = logging.getLogger(__name__)

_EXTRACT_JS = """
() => {
  const seen = new Set();
  const out = [];
  const nodes = document.querySelectorAll('div#search a[href^="http"], div#rso a[href^="http"], a[href^="http"]');
  for (const a of nodes) {
    const h3 = a.querySelector('h3');
    if (!h3) continue;
    const href = a.href;
    if (!href || seen.has(href) || href.includes('google.com/') || href.startsWith('https://webcache')) continue;
    seen.add(href);
    let container = a.closest('div[data-hveid], div.g, div[jscontroller]') || a.parentElement;
    let snippet = '';
    if (container) {
      const sn = container.querySelector('div[data-sncf], div.VwiC3b, span.aCOpRe, div[style*="-webkit-line-clamp"]');
      if (sn) snippet = sn.innerText;
    }
    out.push({href, title: h3.innerText, snippet});
  }
  return out;
}
"""

_GL = {"us": ("us", "en"), "gb": ("uk", "en"), "uk": ("uk", "en"), "au": ("au", "en"), "ca": ("ca", "en"), "nz": ("nz", "en"), "ie": ("ie", "en")}


class GoogleProvider(SearchProvider):
    name = "google"
    supports_pagination = True

    def __init__(self) -> None:
        super().__init__()
        self.browser = get_browser()
        self._lock = asyncio.Lock()
        self._warm = False

    async def _accept_consent(self, page) -> None:
        for sel in ("button#L2AGLb", "button:has-text('Accept all')", "button:has-text('I agree')", "form[action*='consent'] button"):
            try:
                btn = page.locator(sel).first
                if await btn.count() and await btn.is_visible():
                    await btn.click(timeout=1500)
                    await page.wait_for_timeout(500)
                    return
            except Exception:
                continue

    async def search(self, query: str, *, country: str = "us", pages: int = 1) -> list[SerpResult]:
        if not self.browser.available:
            raise EngineError("browser unavailable")
        gl, hl = _GL.get((country or "us").lower(), ("us", "en"))
        async with self._lock:  # one Google query at a time; humans don't parallelise
            await asyncio.sleep(settings.search_min_delay + random.uniform(0.5, 2.0))
            try:
                async with self.browser.page(persistent_key="google") as page:
                    await page.goto(f"https://www.google.com/?hl={hl}&gl={gl}", wait_until="domcontentloaded", timeout=30000)
                    await self._accept_consent(page)
                    box = page.locator("textarea[name=q], input[name=q]").first
                    await box.click(timeout=5000)
                    await box.fill("")
                    await box.type(query, delay=random.randint(25, 70))
                    await page.keyboard.press("Enter")
                    await page.wait_for_load_state("domcontentloaded")
                    await page.wait_for_timeout(1200 + random.randint(0, 800))
                    results: list[SerpResult] = []
                    for pn in range(pages):
                        if "/sorry/" in page.url:
                            await self.browser.reset_context("google")
                            raise EngineBlocked("captcha")
                        try:
                            await page.wait_for_selector("h3", timeout=8000)
                        except Exception:
                            html = (await page.content()).lower()
                            if "did not match any documents" in html or "no results found" in html:
                                break
                            if "unusual traffic" in html or "recaptcha" in html:
                                await self.browser.reset_context("google")
                                raise EngineBlocked("captcha")
                            raise EngineError("no_results_dom")
                        raw = await page.evaluate(_EXTRACT_JS)
                        for item in raw:
                            results.append(SerpResult(engine="google", query=query, country=country, rank=len(results) + 1, url=item["href"], title=(item.get("title") or "")[:300], snippet=(item.get("snippet") or "")[:500]))
                        if pn + 1 >= pages:
                            break
                        nxt = page.locator("a#pnnext").first
                        if not await nxt.count():
                            break
                        await nxt.click()
                        await page.wait_for_load_state("domcontentloaded")
                        await page.wait_for_timeout(1000 + random.randint(0, 800))
                    return results
            except (EngineBlocked, EngineError):
                raise
            except Exception as e:
                raise EngineError(f"{type(e).__name__}: {str(e)[:120]}") from e
