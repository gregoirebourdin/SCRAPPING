"""Website cache (DB): crawl once, reuse without network, stable hashes, crawl runs, company status."""

from __future__ import annotations

import socket
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa

from scout.crawl.cache import ensure_crawled, get_cached_pages
from scout.db.engine import session_scope
from scout.db.enums import PageType, WebsiteStatus
from scout.db.models import Company, WebsiteCrawlRun, WebsitePage
from scout.util.pools import reset_pools
from tests.unit.crawl.fixture_server import (
    AGENCE_LUMIERE_PAGES,
    FixtureServer,
    configure_overrides,
    reset_crawl_state,
)

pytestmark = pytest.mark.integration

SITE = "agence-lumiere.fr"


@pytest.fixture
async def site(db, monkeypatch):
    server = FixtureServer()
    await server.start()
    server.add_site(SITE, AGENCE_LUMIERE_PAGES)
    configure_overrides(monkeypatch, {SITE: server.target()})
    reset_pools()
    try:
        yield server
    finally:
        await server.stop()
        await reset_crawl_state()


async def _company(
    ws: uuid.UUID, *, website: str | None, name: str = "Agence Lumière", domain: str | None = None
) -> uuid.UUID:
    async with session_scope() as s:
        c = Company(
            workspace_id=ws,
            name=name,
            normalized_name=name.lower(),
            website_url=website,
            normalized_domain=domain,
        )
        s.add(c)
        await s.flush()
        return c.id


async def _get_company(cid: uuid.UUID) -> Company:
    async with session_scope() as s:
        return await s.get(Company, cid)


async def _runs(cid: uuid.UUID) -> list[WebsiteCrawlRun]:
    async with session_scope() as s:
        return list(
            (
                await s.scalars(
                    sa.select(WebsiteCrawlRun)
                    .where(WebsiteCrawlRun.company_id == cid)
                    .order_by(WebsiteCrawlRun.started_at)
                )
            ).all()
        )


async def test_ensure_crawled_crawls_once_then_serves_cache(site, workspace):
    ws, _ = workspace
    cid = await _company(ws, website="https://agence-lumiere.fr")
    pages = await ensure_crawled(ws, cid)
    assert pages and pages[0].page_type == PageType.home
    types = {p.page_type for p in pages}
    assert {PageType.team, PageType.about, PageType.legal, PageType.contact} <= types
    home = pages[0]
    assert home.head_html and home.response_headers and "content-type" in home.response_headers
    team = next(p for p in pages if p.page_type == PageType.team)
    assert "Claire Fontaine" in team.content_text and team.head_html is None
    assert team.links.get("people_profiles")

    company = await _get_company(cid)
    assert company.website_status == WebsiteStatus.ok
    assert company.last_crawled_at is not None
    assert company.normalized_domain == SITE and company.domain == SITE
    assert company.website_url == "https://agence-lumiere.fr/"

    runs = await _runs(cid)
    assert len(runs) == 1
    run = runs[0]
    assert run.status == "ok" and run.finished_at is not None
    assert run.pages_fetched == len(pages) and run.pages_failed >= 1 and run.bytes > 0
    assert all(p.crawl_run_id == run.id for p in pages)

    requests_before = site.count()
    again = await ensure_crawled(ws, cid)
    assert site.count() == requests_before  # fresh cache → no network at all
    assert [p.id for p in again] == [p.id for p in pages]
    assert len(await _runs(cid)) == 1
    assert [p.id for p in await get_cached_pages(ws, cid)] == [p.id for p in pages]


async def test_forced_recrawl_keeps_hashes_and_counts_unchanged(site, workspace):
    ws, _ = workspace
    cid = await _company(ws, website="http://agence-lumiere.fr/")
    first = {p.canonical_url: p for p in await ensure_crawled(ws, cid)}
    second = {p.canonical_url: p for p in await ensure_crawled(ws, cid, force=True)}
    assert set(first) == set(second)
    for key, page in second.items():
        assert page.id == first[key].id  # upserted, not duplicated
        assert page.content_hash == first[key].content_hash
        assert page.content_text == first[key].content_text  # 304 keeps cached content
        assert page.fetched_at >= first[key].fetched_at
    runs = await _runs(cid)
    assert len(runs) == 2
    assert runs[1].pages_unchanged == len(second)  # 304s + identical home
    conditional = [h for _host, path, h in site.requests if path == "/equipe" and h.get("if-none-match")]
    assert conditional, "second crawl must revalidate with If-None-Match"


async def test_stale_cache_is_recrawled(site, workspace):
    ws, _ = workspace
    cid = await _company(ws, website="http://agence-lumiere.fr/")
    await ensure_crawled(ws, cid)
    async with session_scope() as s:
        await s.execute(
            sa.update(WebsitePage)
            .where(WebsitePage.company_id == cid)
            .values(fetched_at=datetime.now(UTC) - timedelta(days=45))
        )
    before = site.count()
    await ensure_crawled(ws, cid, max_age_days=30)
    assert site.count() > before
    assert len(await _runs(cid)) == 2


async def test_company_without_website(site, workspace):
    ws, _ = workspace
    cid = await _company(ws, website=None)
    assert await ensure_crawled(ws, cid) == []
    assert (await _get_company(cid)).website_status == WebsiteStatus.none
    assert site.requests == []


async def test_unreachable_and_parked_are_recorded_not_raised(site, workspace, monkeypatch):
    ws, _ = workspace
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    dead_port = sock.getsockname()[1]
    sock.close()
    site.add("domaine-parque.fr", "/", "<html><body><p>This domain may be for sale!</p></body></html>")
    configure_overrides(
        monkeypatch,
        {
            SITE: site.target(),
            "domaine-parque.fr": site.target(),
            "agence-fermee.fr": f"127.0.0.1:{dead_port}",
            "www.agence-fermee.fr": f"127.0.0.1:{dead_port}",
        },
    )
    dead = await _company(ws, website="agence-fermee.fr", name="Agence Fermée")
    assert await ensure_crawled(ws, dead) == []
    assert (await _get_company(dead)).website_status == WebsiteStatus.unreachable
    run = (await _runs(dead))[0]
    assert run.status == "unreachable" and run.error and run.error_category is not None

    before = site.count()
    assert await ensure_crawled(ws, dead) == []  # negative cache: no immediate retry
    assert site.count() == before and len(await _runs(dead)) == 1

    parked = await _company(ws, website="https://domaine-parque.fr", name="Domaine Parqué")
    assert await ensure_crawled(ws, parked) == []
    company = await _get_company(parked)
    assert company.website_status == WebsiteStatus.parked
    assert company.normalized_domain is None


async def test_domain_owned_by_other_company_is_not_taken(site, workspace):
    ws, _ = workspace
    await _company(ws, website=None, name="Lumière Holding", domain=SITE)
    cid = await _company(ws, website="http://agence-lumiere.fr/")
    pages = await ensure_crawled(ws, cid)
    assert pages
    company = await _get_company(cid)
    assert company.website_status == WebsiteStatus.ok
    assert company.normalized_domain is None


async def test_unknown_company_raises(site, workspace):
    from scout.errors import PermanentError

    ws, _ = workspace
    with pytest.raises(PermanentError):
        await ensure_crawled(ws, uuid.uuid4())
