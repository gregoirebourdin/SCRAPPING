"""BrowserGuard (fake Playwright routes) + opt-in real-browser checks of the air gap.

Real-browser tests run only with ``SCOUT_TEST_BROWSERS=1`` and installed Chromium
(``PLAYWRIGHT_BROWSERS_PATH``); they prove that a redirect to an internal address is never fetched,
which the previous ``route.continue_()`` interceptor did not guarantee.
"""

from __future__ import annotations

import os

import pytest

from scout.crawl import render
from scout.crawl.browser_guard import BrowserGuard, RawResponse
from tests.unit.crawl.fixture_server import configure_overrides


class FakeRequest:
    def __init__(self, url, *, method="GET", resource_type="document", headers=None):
        self.url = url
        self.method = method
        self.resource_type = resource_type
        self._headers = headers or {"accept": "text/html", "cookie": "sid=1", "user-agent": "browser"}

    async def all_headers(self):
        return self._headers


class FakeRoute:
    def __init__(self, request):
        self.request = request
        self.aborted = False
        self.fulfilled: dict | None = None

    async def abort(self):
        self.aborted = True

    async def fulfill(self, **kw):
        self.fulfilled = kw


def _fetcher(result: RawResponse | None):
    calls: list[tuple[str, dict]] = []

    async def fetch(url, headers):
        calls.append((url, headers))
        return result

    return fetch, calls


OK = RawResponse(
    200,
    {"content-type": "text/html", "content-encoding": "br", "content-length": "9", "set-cookie": "a=b"},
    b"<html>ok",
    "https://acme-test.fr/",
)


async def test_guard_serves_allowed_requests_from_our_fetcher():
    fetch, calls = _fetcher(OK)
    guard = BrowserGuard(fetch)
    guard.begin("https://acme-test.fr/")
    route = FakeRoute(FakeRequest("https://acme-test.fr/#top"))
    await guard.handle(route)
    assert route.fulfilled is not None and not route.aborted
    assert route.fulfilled["body"] == b"<html>ok" and route.fulfilled["status"] == 200
    assert route.fulfilled["headers"] == {"content-type": "text/html", "set-cookie": "a=b"}  # encoding dropped
    assert calls == [("https://acme-test.fr/#top", {"cookie": "sid=1", "accept": "text/html"})]  # no UA leak
    assert guard.served("https://acme-test.fr/")


@pytest.mark.parametrize(
    "request_",
    [
        FakeRequest("http://169.254.169.254/latest/", resource_type="fetch"),
        FakeRequest("http://localhost:6379/", resource_type="script"),
        FakeRequest("http://10.0.0.1/", resource_type="document"),
        FakeRequest("file:///etc/passwd", resource_type="document"),
        FakeRequest("https://acme-test.fr/logo.png", resource_type="image"),
        FakeRequest("https://acme-test.fr/font.woff2", resource_type="font"),
        FakeRequest("https://acme-test.fr/api", method="POST", resource_type="xhr"),
    ],
)
async def test_guard_refuses_without_fetching(request_):
    fetch, calls = _fetcher(OK)
    guard = BrowserGuard(fetch)
    route = FakeRoute(request_)
    await guard.handle(route)
    assert route.aborted and route.fulfilled is None and calls == []


async def test_guard_aborts_when_fetcher_refuses_and_never_passes_redirects():
    fetch, _ = _fetcher(None)  # e.g. a hop resolved to a private address
    guard = BrowserGuard(fetch)
    guard.begin("https://acme-test.fr/")
    route = FakeRoute(FakeRequest("https://acme-test.fr/"))
    await guard.handle(route)
    assert route.aborted and not guard.served("https://acme-test.fr/")

    fetch3xx, _ = _fetcher(RawResponse(302, {"location": "http://10.0.0.1/"}, b"", "https://acme-test.fr/"))
    route = FakeRoute(FakeRequest("https://acme-test.fr/"))
    await BrowserGuard(fetch3xx).handle(route)
    assert route.fulfilled is not None and route.fulfilled["status"] == 502
    assert "location" not in route.fulfilled["headers"]


async def test_guard_request_budget_and_handler_errors():
    fetch, calls = _fetcher(OK)
    guard = BrowserGuard(fetch, max_requests=2)
    routes = [FakeRoute(FakeRequest(f"https://acme-test.fr/{i}.js", resource_type="script")) for i in range(3)]
    for r in routes:
        await guard.handle(r)
    assert [r.aborted for r in routes] == [False, False, True] and len(calls) == 2

    async def broken(url, headers):
        raise RuntimeError("boom")

    route = FakeRoute(FakeRequest("https://acme-test.fr/"))
    await BrowserGuard(broken).handle(route)
    assert route.aborted


async def test_guard_install_requires_websocket_routing():
    class OldContext:
        async def route(self, pattern, handler):
            self.routed = pattern

    with pytest.raises(RuntimeError):
        await BrowserGuard(_fetcher(OK)[0]).install(OldContext())


# ---- real browser (opt-in) -----------------------------------------------------------------------

needs_browser = pytest.mark.skipif(
    os.environ.get("SCOUT_TEST_BROWSERS") != "1", reason="set SCOUT_TEST_BROWSERS=1 with Chromium installed"
)
SPA_HOST = "spa-site.fr"


def _spa(server) -> None:
    port = server.port
    server.add(
        SPA_HOST,
        "/",
        f"""<html><head><title>SPA</title><link rel="preconnect" href="http://127.0.0.1:{port}"></head>
        <body><div id="root"></div><img src="http://169.254.169.254/latest/meta-data/x.png">
        <script src="/app.js"></script>
        <script>fetch('http://127.0.0.1:{port}/direct-leak').catch(() => {{}});
        fetch('/go-internal').catch(() => {{}});</script></body></html>""",
    )
    server.add(
        SPA_HOST,
        "/app.js",
        "document.getElementById('root').innerText = 'Rendered by the bundle: ' + 'scheduling software '.repeat(30);",
        content_type="application/javascript",
    )
    server.redirect(SPA_HOST, "/go-internal", f"http://127.0.0.1:{port}/secret")
    server.redirect(SPA_HOST, "/redirect-home", f"http://{SPA_HOST}/")


@needs_browser
async def test_scrapling_dynamic_tier_is_air_gapped(fixture_server, monkeypatch):
    pytest.importorskip("scrapling")
    from scout.config import get_settings
    from scout.crawl.scrapling_tiers import ScraplingDynamicTier, ScraplingFetcherTier

    _spa(fixture_server)
    configure_overrides(monkeypatch, {SPA_HOST: fixture_server.target()})
    monkeypatch.setenv("SCRAPLING_DYNAMIC_ENABLED", "true")
    get_settings.cache_clear()
    fetcher = ScraplingFetcherTier()
    tier = ScraplingDynamicTier(fetcher)
    try:
        resp = await tier.fetch(f"http://{SPA_HOST}/")
        again = await tier.fetch(f"http://{SPA_HOST}/redirect-home")  # same browser, guarded redirect
    finally:
        await tier.aclose()
        await fetcher.aclose()
    assert resp is not None and "Rendered by the bundle" in resp.text and resp.tier == "scrapling_dynamic"
    assert again is not None and "Rendered by the bundle" in again.text
    paths = [p for _h, p, _hd in fixture_server.requests]
    assert "/app.js" in paths and "/go-internal" in paths
    assert "/secret" not in paths and "/direct-leak" not in paths  # redirect target + direct IP never fetched
    assert all(h == SPA_HOST for h, _p, _hd in fixture_server.requests)


@needs_browser
async def test_render_playwright_is_air_gapped(fixture_server, monkeypatch):
    pytest.importorskip("playwright")
    from scout.config import get_settings

    _spa(fixture_server)
    configure_overrides(monkeypatch, {SPA_HOST: fixture_server.target()})
    monkeypatch.setenv("CRAWLER_ENABLE_BROWSER", "true")
    get_settings.cache_clear()
    html = await render.render_playwright(f"http://{SPA_HOST}/")
    assert html is not None and "Rendered by the bundle" in html
    paths = [p for _h, p, _hd in fixture_server.requests]
    assert "/secret" not in paths and "/direct-leak" not in paths
    ua = {hd.get("user-agent", "") for _h, _p, hd in fixture_server.requests}
    assert all(u.startswith("ScoutBot/") for u in ua)  # every byte came through the L1 fetcher


@needs_browser
async def test_crawl_renders_spa_with_scrapling_dynamic(fixture_server, monkeypatch, stats_events):
    pytest.importorskip("scrapling")
    from scout.config import get_settings
    from scout.crawl.crawler import crawl_site
    from scout.db.enums import FetchTier

    _spa(fixture_server)
    configure_overrides(monkeypatch, {SPA_HOST: fixture_server.target()})
    monkeypatch.setenv("SCRAPLING_DYNAMIC_ENABLED", "true")
    get_settings.cache_clear()
    result = await crawl_site(f"http://{SPA_HOST}/")
    assert result.status.value == "ok" and result.tier_max == FetchTier.browser
    assert "scheduling software" in result.pages[0].content_text
    assert result.pages[0].head_html  # fingerprints from the HTTP response are kept
    assert [(e.key, e.produced) for e in stats_events][:2] == [("http", True), ("scrapling_dynamic", True)]
