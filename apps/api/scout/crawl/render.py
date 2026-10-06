"""JavaScript rendering tiers (L3 Crawl4AI, L4 Playwright) — optional extras, used only when a page
is clearly client-rendered. Both degrade to ``None`` when disabled, not installed, or failing.

Browsers resolve DNS themselves, so besides the URL pre-check every request the page makes is
re-validated (scheme/port/raw IP + resolved addresses) through a request interceptor (Playwright).
"""

from __future__ import annotations

import asyncio
import importlib
import os
import re
from typing import Any
from urllib.parse import urlsplit

import structlog

from scout.config import get_settings
from scout.crawl.parser import ParsedPage, parse_html
from scout.crawl.ssrf import SSRFBlocked, resolve_safe, validate_url
from scout.db.enums import UsageCategory
from scout.services.usage import record_usage
from scout.util.pools import pool

log = structlog.get_logger(__name__)

JS_TEXT_THRESHOLD = 300
RENDER_TIMEOUT_S = 20.0
_NOSCRIPT_JS_RE = re.compile(
    r"(enable|activate|turn on|activer|activez|autoriser|aktivieren|habilita|abilita)[^.]{0,40}javascript"
    r"|javascript[^.]{0,40}(required|is disabled|n[ée]cessaire|requis|d[ée]sactiv|erforderlich|deaktiviert)",
    re.IGNORECASE,
)
_SPA_ROOT_RE = re.compile(
    r"<(?:div|main|section|body)[^>]+id=[\"'](?:root|__next|app|__nuxt|___gatsby|svelte|q-app)[\"']"
    r"|\bng-app\b|\bng-version=|<app-root",
    re.IGNORECASE,
)


def needs_js(html: str, parsed: ParsedPage | None = None) -> bool:
    """True when the HTML looks client-rendered: little visible text plus an SPA root or a
    ``<noscript>`` asking to enable JavaScript."""
    if not html:
        return False
    parsed = parsed or parse_html(html, "https://localhost.invalid/")
    if len((parsed.content_text or "").strip()) >= JS_TEXT_THRESHOLD:
        return False
    if parsed.spa_root or _SPA_ROOT_RE.search(html[:200_000]):
        return True
    return bool(parsed.noscript_text and _NOSCRIPT_JS_RE.search(parsed.noscript_text))


async def _host_is_safe(url: str, cache: dict[str, bool]) -> bool:
    try:
        validate_url(url)
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if host not in cache:
            port = parts.port or (443 if parts.scheme == "https" else 80)
            try:
                await resolve_safe(host, port)
                cache[host] = True
            except SSRFBlocked:
                cache[host] = False
            except Exception:
                cache[host] = True  # unresolvable hosts simply fail to load
        return cache[host]
    except SSRFBlocked:
        return False


async def render_crawl4ai(url: str) -> str | None:
    """Rendered HTML via Crawl4AI (L3) or None (disabled / not installed / failed)."""
    settings = get_settings()
    if not settings.crawler_enable_crawl4ai:
        return None
    try:
        validate_url(url)
        parts = urlsplit(url)
        await resolve_safe((parts.hostname or "").lower(), parts.port or (443 if parts.scheme == "https" else 80))
    except Exception as exc:
        log.info("render_crawl4ai_refused", url=url, error=str(exc))
        return None
    try:
        crawl4ai: Any = importlib.import_module("crawl4ai")
    except ImportError:
        log.debug("crawl4ai_not_installed")
        return None
    async with pool("browser"):
        await record_usage(UsageCategory.browser_request, cost_usd=settings.cost_browser_request_usd)
        try:
            browser_cfg = crawl4ai.BrowserConfig(headless=True, user_agent=settings.crawler_user_agent, verbose=False)
            run_cfg = crawl4ai.CrawlerRunConfig(page_timeout=int(RENDER_TIMEOUT_S * 1000), verbose=False)
            async with crawl4ai.AsyncWebCrawler(config=browser_cfg) as crawler:
                result = await asyncio.wait_for(crawler.arun(url=url, config=run_cfg), timeout=RENDER_TIMEOUT_S + 5)
            if not getattr(result, "success", False):
                return None
            final = getattr(result, "url", None) or url
            if final != url:
                validate_url(final)
            html = getattr(result, "html", None) or getattr(result, "cleaned_html", None)
            return html if isinstance(html, str) and html.strip() else None
        except Exception as exc:
            log.info("render_crawl4ai_failed", url=url, error=str(exc)[:300])
            return None


async def render_playwright(url: str) -> str | None:
    """Rendered HTML via Playwright Chromium (L4) or None. Images/fonts/media are blocked and every
    request is SSRF-validated."""
    settings = get_settings()
    if not settings.crawler_enable_browser:
        return None
    host_cache: dict[str, bool] = {}
    if not await _host_is_safe(url, host_cache):
        log.info("render_playwright_refused", url=url)
        return None
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        log.debug("playwright_not_installed")
        return None

    async def _route(route: Any) -> None:
        req = route.request
        if req.resource_type in ("image", "font", "media"):
            await route.abort()
            return
        if not await _host_is_safe(req.url, host_cache):
            await route.abort()
            return
        await route.continue_()

    executable = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or None
    async with pool("browser"):
        await record_usage(UsageCategory.browser_request, cost_usd=settings.cost_browser_request_usd)
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True, executable_path=executable)
                try:
                    context = await browser.new_context(user_agent=settings.crawler_user_agent, java_script_enabled=True)
                    await context.route("**/*", _route)
                    page = await context.new_page()
                    await page.goto(url, wait_until="networkidle", timeout=RENDER_TIMEOUT_S * 1000)
                    if not await _host_is_safe(page.url, host_cache):
                        return None
                    return await page.content()
                finally:
                    await browser.close()
        except Exception as exc:
            log.info("render_playwright_failed", url=url, error=str(exc)[:300])
            return None
