"""End-to-end crawl of local fixture sites (no internet): FR agency, SPA, parked, unreachable, blocked."""

from __future__ import annotations

import socket
from pathlib import Path

from scout.crawl import render
from scout.crawl.crawler import crawl_site
from scout.crawl.parser import parse_html
from scout.crawl.render import needs_js
from scout.db.enums import ErrorCategory, FetchTier, PageType, WebsiteStatus
from scout.util.text import content_hash
from tests.unit.crawl.fixture_server import AGENCE_LUMIERE_PAGES, configure_overrides

FIX = Path(__file__).resolve().parents[2] / "fixtures" / "html"
SITE = "agence-lumiere.fr"


def _agence(server, monkeypatch, *extra_hosts: str) -> None:
    server.add_site(SITE, AGENCE_LUMIERE_PAGES)
    configure_overrides(monkeypatch, {SITE: server.target(), **{h: server.target() for h in extra_hosts}})


async def test_crawl_fr_agency_site(fixture_server, monkeypatch):
    _agence(fixture_server, monkeypatch)
    result = await crawl_site("https://agence-lumiere.fr")  # https fails (no TLS) → http fallback
    assert result.status == WebsiteStatus.ok
    assert result.domain == SITE
    assert result.home_url == "http://agence-lumiere.fr/"
    by_type = {p.page_type: p for p in result.pages}
    for pt in (
        PageType.home,
        PageType.about,
        PageType.team,
        PageType.services,
        PageType.contact,
        PageType.legal,
    ):
        assert pt in by_type, pt
    home = result.pages[0]
    assert home.page_type == PageType.home
    assert home.head_html and "GTM-ABC1234" in home.head_html
    assert home.headers.get("content-type", "").startswith("text/html")
    assert home.content_hash == content_hash(home.content_text)
    assert all(p.head_html is None and p.headers == {} for p in result.pages[1:])
    team = by_type[PageType.team]
    assert "Claire Fontaine\nCo-fondatrice & Directrice générale" in team.content_text
    assert team.canonical_url == "http://agence-lumiere.fr/equipe"
    assert result.pages_failed >= 1  # /realisations is linked from the home page but returns 404
    assert result.tier_max == FetchTier.http and result.bytes > 0
    assert len(result.pages) <= 10
    paths = [p for h, p, _ in fixture_server.requests if h == SITE]
    assert "/robots.txt" in paths and "/sitemap.xml" in paths
    assert not any(p.startswith(("/tag/", "/wp-content/")) for p in paths)


async def test_conditional_recrawl_marks_unchanged_pages(fixture_server, monkeypatch):
    _agence(fixture_server, monkeypatch)
    first = await crawl_site("http://agence-lumiere.fr/")
    known = {p.canonical_url: (p.etag, p.last_modified, p.content_hash) for p in first.pages}
    second = await crawl_site("http://agence-lumiere.fr/", known=known)
    unchanged = [p for p in second.pages if p.not_modified]
    assert len(unchanged) == len(first.pages) - 1  # every page but the (always fully fetched) home
    for p in unchanged:
        assert p.content_hash == known[p.canonical_url][2]
        assert p.status_code == 304


async def test_robots_disallow_is_respected(fixture_server, monkeypatch):
    fixture_server.add(
        "private-site.fr", "/robots.txt", "User-agent: *\nDisallow: /\n", content_type="text/plain"
    )
    fixture_server.add("private-site.fr", "/", "<html><body>secret</body></html>")
    configure_overrides(monkeypatch, {"private-site.fr": fixture_server.target()})
    result = await crawl_site("http://private-site.fr/")
    assert result.status == WebsiteStatus.blocked and result.robots_blocked
    assert (
        "private-site.fr",
        "/",
    ) not in {(h, p) for h, p, _ in fixture_server.requests}


async def test_parked_domain(fixture_server, monkeypatch):
    fixture_server.add("boulangerie-dupont.fr", "/", (FIX / "parked.html").read_text())
    configure_overrides(monkeypatch, {"boulangerie-dupont.fr": fixture_server.target()})
    result = await crawl_site("boulangerie-dupont.fr")
    assert result.status == WebsiteStatus.parked
    assert result.pages == []


async def test_unreachable_site(monkeypatch):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    configure_overrides(
        monkeypatch, {"dead-agency.fr": f"127.0.0.1:{port}", "www.dead-agency.fr": f"127.0.0.1:{port}"}
    )
    result = await crawl_site("dead-agency.fr")
    assert result.status == WebsiteStatus.unreachable
    assert result.error_category in (ErrorCategory.network, ErrorCategory.timeout)
    assert result.pages == []


async def test_blocked_home(fixture_server, monkeypatch):
    fixture_server.add("guarded.fr", "/", "<title>Just a moment...</title>", status=403)
    configure_overrides(monkeypatch, {"guarded.fr": fixture_server.target()})
    result = await crawl_site("http://guarded.fr/")
    assert result.status == WebsiteStatus.blocked
    assert result.error_category == ErrorCategory.blocked


async def test_spa_detection_and_render_tier(fixture_server, monkeypatch):
    spa_html = (FIX / "spa.html").read_text()
    assert needs_js(spa_html, parse_html(spa_html, "https://acme-app.io/"))
    real = (FIX / "agence_lumiere/home.html").read_text()
    assert not needs_js(real, parse_html(real, "http://agence-lumiere.fr/"))

    fixture_server.add("acme-app.io", "/", spa_html)
    configure_overrides(monkeypatch, {"acme-app.io": fixture_server.target()})
    # JS tiers disabled (default) → HTTP content kept, still "ok".
    plain = await crawl_site("http://acme-app.io/")
    assert plain.status == WebsiteStatus.ok and plain.tier_max == FetchTier.http

    rendered = (
        "<html><body><div id='root'><h1>Acme</h1><p>"
        + "Acme builds scheduling software for clinics and independent practitioners. " * 8
        + "</p><a href='/about'>About</a></div></body></html>"
    )

    async def fake_crawl4ai(url: str) -> str | None:
        return rendered

    monkeypatch.setattr(render, "render_crawl4ai", fake_crawl4ai)
    result = await crawl_site("http://acme-app.io/")
    assert result.tier_max == FetchTier.crawl4ai
    assert result.pages[0].fetch_tier == FetchTier.crawl4ai
    assert "scheduling software" in result.pages[0].content_text
    assert result.pages[0].head_html  # fingerprints from the HTTP response are kept


async def test_render_tiers_disabled_return_none():
    assert await render.render_crawl4ai("https://example.com/") is None
    assert await render.render_playwright("https://example.com/") is None


async def test_redirect_to_new_domain_uses_final_host(fixture_server, monkeypatch):
    _agence(fixture_server, monkeypatch, "ancien-nom.fr")
    fixture_server.redirect("ancien-nom.fr", "/", "http://agence-lumiere.fr/")
    result = await crawl_site("http://ancien-nom.fr/")
    assert result.status == WebsiteStatus.ok
    assert result.domain == SITE and result.home_url == "http://agence-lumiere.fr/"
