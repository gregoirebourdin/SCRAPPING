"""Scrapling L2 adapter against the local fixture server (skipped when the `scraping` extra is absent).

The fixture hosts do not exist in DNS: a successful fetch proves curl used our CURLOPT_RESOLVE pin
(and not its own resolver, nor an environment proxy).
"""

from __future__ import annotations

import pytest

from scout.crawl import ssrf
from scout.crawl.scrapling_tiers import ScraplingFetcherTier
from scout.crawl.tiers import TierChain
from tests.unit.crawl.fixture_server import configure_overrides

pytest.importorskip("scrapling")
pytest.importorskip("curl_cffi")

HOST = "pinned-site.fr"
PAGE = "<html><head><title>Pinned</title></head><body><p>Bonjour depuis pinned-site.</p></body></html>"


@pytest.fixture
def site(fixture_server, monkeypatch):
    configure_overrides(monkeypatch, {HOST: fixture_server.target(), "other-site.fr": fixture_server.target()})
    return fixture_server


async def test_fetch_is_pinned_impersonated_and_parsed(site):
    site.add(HOST, "/", PAGE)
    tier = ScraplingFetcherTier()
    try:
        resp = await tier.fetch(f"http://{HOST}/")
    finally:
        await tier.aclose()
    assert resp is not None and resp.status_code == 200 and resp.tier == "scrapling_fetcher"
    assert "Bonjour depuis pinned-site" in resp.text and resp.final_url == f"http://{HOST}/"
    host, path, headers = site.requests[-1]
    assert (host, path) == (HOST, "/")
    assert "Chrome/" in headers.get("user-agent", "")  # browser impersonation
    assert "referer" not in headers  # no fake Google referer
    assert headers.get("accept-language", "").startswith("fr")


async def test_redirects_are_followed_and_each_hop_revalidated(site):
    site.redirect(HOST, "/", "http://other-site.fr/landing")
    site.add("other-site.fr", "/landing", PAGE)
    site.redirect(HOST, "/evil", "http://169.254.169.254/latest/meta-data/")
    site.redirect(HOST, "/loop", f"http://{HOST}/loop")
    tier = ScraplingFetcherTier()
    try:
        ok = await tier.fetch(f"http://{HOST}/")
        evil = await tier.fetch(f"http://{HOST}/evil")
        loop = await tier.fetch(f"http://{HOST}/loop")
    finally:
        await tier.aclose()
    assert ok is not None and ok.final_url == "http://other-site.fr/landing"
    assert evil is None  # metadata IP refused before any connection
    assert loop is None
    assert not any("meta-data" in p for _h, p, _hd in site.requests)


async def test_private_targets_are_refused_without_requests(site, monkeypatch):
    async def private_dns(host, port):
        return [("10.1.2.3", port)]

    monkeypatch.setattr(ssrf, "_getaddrinfo", private_dns)
    tier = ScraplingFetcherTier()
    try:
        for url in ("http://localhost/", "http://10.0.0.8/", "file:///etc/passwd", "http://rebind-me.fr/"):
            assert await tier.fetch(url) is None, url
    finally:
        await tier.aclose()
    assert site.requests == []


async def test_body_cap_and_non_text_content(site, monkeypatch):
    from scout.config import get_settings

    site.add(HOST, "/big", "<html>" + "x" * 50_000 + "</html>")
    site.add(HOST, "/file.pdf", b"%PDF-1.4", content_type="application/pdf")
    monkeypatch.setenv("CRAWLER_MAX_BYTES", "10000")
    get_settings.cache_clear()
    tier = ScraplingFetcherTier()
    try:
        assert await tier.fetch(f"http://{HOST}/big") is None  # CURLOPT_MAXFILESIZE aborts the transfer
        assert await tier.fetch(f"http://{HOST}/file.pdf") is None
    finally:
        await tier.aclose()


async def test_crawler_recovers_a_blocked_site_through_l2(site):
    """End to end with the real adapter: L1 is blocked (403, simulated) on HTML pages; the crawl
    succeeds through L2 and stays on L2 for inner pages (robots / sitemap still go through L1)."""
    site.add(HOST, "/robots.txt", "User-agent: *\nAllow: /\n", content_type="text/plain")
    home = "<html><head><title>Pinned</title></head><body><p>Accueil</p><a href='/contact'>Contact</a></body></html>"
    site.add(HOST, "/", home)
    site.add(HOST, "/contact", "<html><body><p>Écrivez-nous : contact@pinned-site.fr</p></body></html>")

    from scout.crawl import http as crawl_http
    from scout.crawl.crawler import crawl_site

    real_fetch = crawl_http.fetch

    async def blocked_l1(url, **kw):
        if url.endswith(("/", "/contact")):
            raise crawl_http.HttpBlockedError("blocked (403)", status_code=403, url=url)
        return await real_fetch(url, **kw)

    chain = TierChain(l1=blocked_l1)
    try:
        result = await crawl_site(f"http://{HOST}/", chain=chain)
    finally:
        await chain.aclose()
    assert result.status.value == "ok"
    assert any("contact@pinned-site.fr" in p.emails for p in result.pages)
    assert chain.sticky == "scrapling_fetcher"
    contact = [(h, p, hd) for h, p, hd in site.requests if p == "/contact"]
    assert contact and "Chrome/" in contact[-1][2].get("user-agent", "")
