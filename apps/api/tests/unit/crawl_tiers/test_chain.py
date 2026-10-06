"""TierChain: escalation rules, stickiness, render order, telemetry (fake tiers, no network)."""

from __future__ import annotations

import sys

import pytest
import structlog

from scout.crawl import scrapling_tiers, tiers
from scout.crawl.http import HttpBlockedError, HttpRateLimitedError, HttpResponse
from scout.crawl.ssrf import SSRFBlocked
from scout.db.enums import FetchTier
from scout.errors import FetchError

URL = "https://acme-test.fr/"
REAL = "<html><head><title>Acme</title></head><body><p>Acme builds things.</p></body></html>"
CHALLENGE = "<html><head><title>Just a moment...</title></head><body>checking</body></html>"


def _resp(text: str = REAL, *, status: int = 200, tier: str = "http", url: str = URL) -> HttpResponse:
    return HttpResponse(
        url=url,
        final_url=url,
        status_code=status,
        headers={"content-type": "text/html"},
        text=text,
        content_type="text/html",
        elapsed_ms=1,
        size_bytes=len(text),
        tier=tier,
    )


class FakeTier:
    def __init__(self, name: str, result: HttpResponse | None = None, *, available: bool = True):
        self.name = name
        self.result = result
        self._available = available
        self.calls: list[str] = []
        self.closed = False

    def available(self) -> bool:
        return self._available

    async def fetch(self, url: str) -> HttpResponse | None:
        self.calls.append(url)
        return self.result

    async def aclose(self) -> None:
        self.closed = True


class FakeL1:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls: list[str] = []

    async def __call__(self, url, *, etag=None, last_modified=None):
        self.calls.append(url)
        out = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(out, Exception):
            raise out
        return out


def _blocked() -> HttpBlockedError:
    return HttpBlockedError("blocked (403)", status_code=403, url=URL)


@pytest.fixture(autouse=True)
def _no_politeness_delay(monkeypatch):
    monkeypatch.setenv("PER_DOMAIN_DELAY_MS", "0")
    from scout.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_l1_success_never_escalates(stats_events):
    l2 = FakeTier("scrapling_fetcher", _resp(tier="scrapling_fetcher"))
    chain = tiers.TierChain(tiers={"scrapling_fetcher": l2}, l1=FakeL1(_resp()))
    resp = await chain.fetch(URL)
    assert resp.tier == "http" and l2.calls == []
    await chain.aclose()
    assert [(e.dimension, e.key, e.produced) for e in stats_events] == [("crawl.tier", "http", True)]
    assert l2.closed


async def test_blocked_l1_escalates_to_scrapling_fetcher_and_sticks(stats_events):
    l1 = FakeL1(_blocked())
    l2 = FakeTier("scrapling_fetcher", _resp(tier="scrapling_fetcher"))
    dyn = FakeTier("scrapling_dynamic", _resp(tier="scrapling_dynamic"))
    chain = tiers.TierChain(tiers={"scrapling_fetcher": l2, "scrapling_dynamic": dyn}, l1=l1)
    first = await chain.fetch(URL)
    assert first.tier == "scrapling_fetcher" and chain.sticky == "scrapling_fetcher"
    second = await chain.fetch(URL + "contact")
    assert second.tier == "scrapling_fetcher"
    assert l1.calls == [URL]  # sticky: L1 not retried on the next page
    assert dyn.calls == []
    await chain.aclose()
    keys = [(e.key, e.produced) for e in stats_events]
    assert keys == [("http", False), ("scrapling_fetcher", True), ("scrapling_fetcher", True)]
    assert all(e.latency_ms >= 0 and e.cost_usd >= 0 for e in stats_events)


async def test_challenged_l2_escalates_to_dynamic():
    l2 = FakeTier("scrapling_fetcher", _resp(CHALLENGE, tier="scrapling_fetcher"))
    dyn = FakeTier("scrapling_dynamic", _resp(tier="scrapling_dynamic"))
    chain = tiers.TierChain(tiers={"scrapling_fetcher": l2, "scrapling_dynamic": dyn}, l1=FakeL1(_blocked()))
    resp = await chain.fetch(URL)
    assert resp.tier == "scrapling_dynamic" and l2.calls == [URL] and dyn.calls == [URL]
    assert tiers.STORED_TIER[resp.tier] == FetchTier.browser


async def test_unrecovered_block_reraises_l1_error():
    l2 = FakeTier("scrapling_fetcher", _resp("", tier="scrapling_fetcher"))
    l2_403 = FakeTier("scrapling_dynamic", _resp(REAL, status=403, tier="scrapling_dynamic"))
    chain = tiers.TierChain(tiers={"scrapling_fetcher": l2, "scrapling_dynamic": l2_403}, l1=FakeL1(_blocked()))
    with pytest.raises(HttpBlockedError):
        await chain.fetch(URL)
    assert chain.sticky is None


@pytest.mark.parametrize(
    "error",
    [
        HttpRateLimitedError("429", url=URL, retry_after=30),  # the site asked us to slow down
        SSRFBlocked("non-public address"),
        FetchError("connection reset"),
    ],
)
async def test_rate_limit_ssrf_and_network_errors_are_not_escalated(error):
    l2 = FakeTier("scrapling_fetcher", _resp(tier="scrapling_fetcher"))
    chain = tiers.TierChain(tiers={"scrapling_fetcher": l2}, l1=FakeL1(error))
    with pytest.raises(type(error)):
        await chain.fetch(URL)
    assert l2.calls == []


async def test_empty_body_escalates_but_404_does_not():
    l2 = FakeTier("scrapling_fetcher", _resp(tier="scrapling_fetcher"))
    chain = tiers.TierChain(tiers={"scrapling_fetcher": l2}, l1=FakeL1(_resp("   ")))
    assert (await chain.fetch(URL)).tier == "scrapling_fetcher"
    l2b = FakeTier("scrapling_fetcher", _resp(tier="scrapling_fetcher"))
    chain = tiers.TierChain(tiers={"scrapling_fetcher": l2b}, l1=FakeL1(_resp("gone", status=404)))
    assert (await chain.fetch(URL)).status_code == 404 and l2b.calls == []


async def test_unavailable_tiers_are_skipped_without_telemetry(stats_events):
    l2 = FakeTier("scrapling_fetcher", _resp(tier="scrapling_fetcher"), available=False)
    chain = tiers.TierChain(tiers={"scrapling_fetcher": l2}, l1=FakeL1(_blocked()))
    with pytest.raises(HttpBlockedError):
        await chain.fetch(URL)
    await chain.aclose()
    assert l2.calls == [] and [e.key for e in stats_events] == ["http"]


async def test_render_order_dynamic_then_crawl4ai_and_playwright_skipped_after_dynamic():
    spa = "<html><body><div id='root'></div></body></html>"
    rendered = "<html><body><p>" + "Rendered content. " * 30 + "</p></body></html>"
    dyn = FakeTier("scrapling_dynamic", None)  # ran, failed
    c4 = FakeTier("crawl4ai", _resp(rendered, tier="crawl4ai"))
    pw = FakeTier("playwright", _resp(rendered, tier="playwright"))
    chain = tiers.TierChain(tiers={"scrapling_dynamic": dyn, "crawl4ai": c4, "playwright": pw})
    got = await chain.render(URL, lambda html: len(html) if len(html) > len(spa) else None)
    assert got is not None and got[2] == "crawl4ai" and got[1] == len(rendered)
    assert dyn.calls == [URL] and pw.calls == []

    # Dynamic tier unavailable (Scrapling not installed) → Playwright stays the last resort.
    dyn_off = FakeTier("scrapling_dynamic", None, available=False)
    c4_none = FakeTier("crawl4ai", None)
    pw2 = FakeTier("playwright", _resp(rendered, tier="playwright"))
    chain = tiers.TierChain(tiers={"scrapling_dynamic": dyn_off, "crawl4ai": c4_none, "playwright": pw2})
    got = await chain.render(URL, lambda html: html)
    assert got is not None and got[2] == "playwright" and dyn_off.calls == []


async def test_render_rejected_by_judge_returns_none():
    dyn = FakeTier("scrapling_dynamic", _resp(tier="scrapling_dynamic"))
    chain = tiers.TierChain(tiers={"scrapling_dynamic": dyn})
    assert await chain.render(URL, lambda html: None) is None


async def test_stats_module_missing_is_a_noop(monkeypatch):
    monkeypatch.setitem(sys.modules, "scout.learning.stats", None)  # import → ModuleNotFoundError
    chain = tiers.TierChain(tiers={}, l1=FakeL1(_resp()))
    await chain.fetch(URL)
    await chain.aclose()  # must not raise


async def test_stats_failure_never_breaks_a_crawl(monkeypatch, stats_events):
    async def boom(events):
        raise RuntimeError("db down")

    monkeypatch.setattr(sys.modules["scout.learning.stats"], "record", boom)
    chain = tiers.TierChain(tiers={}, l1=FakeL1(_resp()))
    await chain.fetch(URL)
    await chain.aclose()


def test_challenge_detection():
    assert tiers.looks_challenged(_resp(CHALLENGE))
    assert not tiers.looks_challenged(_resp(REAL))
    cf_page = "<html><head><title>Acme</title><script src='/cdn-cgi/challenge-platform/scripts/jsd/main.js'></script>"
    assert not tiers.looks_challenged(_resp(cf_page + "</head><body>ok</body></html>"))  # CF bot JS on a real page
    mitigated = _resp(REAL)
    mitigated.headers["cf-mitigated"] = "challenge"
    assert tiers.looks_challenged(mitigated)


# ---- graceful degradation of the real Scrapling adapters ----------------------------------------


async def test_scrapling_missing_degrades_to_none_and_logs_once(monkeypatch):
    real_import = scrapling_tiers.importlib.import_module

    def fake_import(name, *a, **kw):
        if name.startswith(("scrapling", "curl_cffi")):
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(scrapling_tiers.importlib, "import_module", fake_import)
    fetcher = scrapling_tiers.ScraplingFetcherTier()
    dynamic = scrapling_tiers.ScraplingDynamicTier(fetcher)
    with structlog.testing.capture_logs() as logs:
        assert not fetcher.available()
        assert await fetcher.fetch(URL) is None
        assert await fetcher.fetch_raw(URL) is None
        assert not dynamic.available() and await dynamic.fetch(URL) is None
    assert [e["event"] for e in logs].count("scrapling_unavailable") == 1


async def test_scrapling_disabled_by_settings(monkeypatch):
    from scout.config import get_settings

    monkeypatch.setenv("SCRAPLING_ENABLED", "false")
    get_settings.cache_clear()
    fetcher = scrapling_tiers.ScraplingFetcherTier()
    assert not fetcher.available() and await fetcher.fetch(URL) is None
    # Dynamic is off by default even when Scrapling is enabled.
    monkeypatch.setenv("SCRAPLING_ENABLED", "true")
    get_settings.cache_clear()
    assert not scrapling_tiers.ScraplingDynamicTier(scrapling_tiers.ScraplingFetcherTier()).available()
