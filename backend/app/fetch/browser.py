"""Headless Chromium pool (Playwright) for JS-rendered pages and browser-only search engines."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from ..config import settings
from ..util.ua import random_ua

log = logging.getLogger(__name__)

_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
window.chrome = window.chrome || { runtime: {} };
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
"""


def _executable() -> str | None:
    if settings.browser_executable:
        return settings.browser_executable
    for cand in ("/opt/pw-browsers/chromium",):
        if os.path.exists(cand):
            return cand
    return None


class BrowserPool:
    def __init__(self) -> None:
        self._pw = None
        self._browser = None
        self._sem = asyncio.Semaphore(settings.browser_pages)
        self._lock = asyncio.Lock()
        self.available = settings.browser_enabled
        self.stats = {"renders": 0, "errors": 0}
        self._persistent: dict[str, object] = {}

    async def start(self) -> None:
        if not self.available or self._browser is not None:
            return
        async with self._lock:
            if self._browser is not None:
                return
            try:
                from playwright.async_api import async_playwright

                self._pw = await async_playwright().start()
                kwargs = {
                    "headless": settings.browser_headless,
                    "args": ["--no-sandbox", "--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage"],
                }
                exe = _executable()
                if exe:
                    kwargs["executable_path"] = exe
                if settings.proxy_url:
                    kwargs["proxy"] = {"server": settings.proxy_url}
                try:
                    self._browser = await self._pw.chromium.launch(**kwargs)
                except Exception:
                    kwargs.pop("executable_path", None)
                    self._browser = await self._pw.chromium.launch(**kwargs)
                log.info("browser started")
            except Exception as e:
                log.warning("browser unavailable: %s", e)
                self.available = False

    async def close(self) -> None:
        try:
            for ctx in self._persistent.values():
                await ctx.close()  # type: ignore[attr-defined]
            self._persistent.clear()
            if self._browser:
                await self._browser.close()
            if self._pw:
                await self._pw.stop()
        finally:
            self._browser = None
            self._pw = None

    async def _new_context(self):
        return await self._browser.new_context(
            user_agent=random_ua(),
            locale="en-US",
            viewport={"width": 1366, "height": 900},
            java_script_enabled=True,
            ignore_https_errors=False,
        )

    @asynccontextmanager
    async def page(self, *, persistent_key: str | None = None) -> AsyncIterator[object]:
        """Yield a page.  ``persistent_key`` keeps cookies between calls (needed for Google)."""
        await self.start()
        if not self.available:
            raise RuntimeError("browser unavailable")
        async with self._sem:
            if persistent_key:
                ctx = self._persistent.get(persistent_key)
                if ctx is None:
                    ctx = await self._new_context()
                    await ctx.add_init_script(_STEALTH_JS)
                    self._persistent[persistent_key] = ctx
                page = await ctx.new_page()
                try:
                    yield page
                finally:
                    await page.close()
            else:
                ctx = await self._new_context()
                await ctx.add_init_script(_STEALTH_JS)
                page = await ctx.new_page()
                try:
                    yield page
                finally:
                    await ctx.close()

    async def reset_context(self, persistent_key: str) -> None:
        ctx = self._persistent.pop(persistent_key, None)
        if ctx is not None:
            try:
                await ctx.close()
            except Exception:
                pass

    async def render(self, url: str, *, wait_ms: int = 1500, timeout_ms: int = 30000) -> tuple[str, str, int | None]:
        """Return (html, final_url, status) after JS execution."""
        try:
            async with self.page() as page:
                # block heavy assets
                await page.route(
                    "**/*",
                    lambda route: route.abort()
                    if route.request.resource_type in ("image", "media", "font", "stylesheet")
                    else route.continue_(),
                )
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                await page.wait_for_timeout(wait_ms)
                html = await page.content()
                self.stats["renders"] += 1
                return html, page.url, (resp.status if resp else None)
        except Exception as e:
            self.stats["errors"] += 1
            log.debug("render failed %s: %s", url, e)
            return "", url, None


_pool: BrowserPool | None = None


def get_browser() -> BrowserPool:
    global _pool
    if _pool is None:
        _pool = BrowserPool()
    return _pool
