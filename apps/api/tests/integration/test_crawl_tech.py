"""Technology detection (DB): cached home page → builtin detector → technologies + observations."""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa

from scout.db.engine import session_scope
from scout.db.enums import SourceType
from scout.db.models import Company, CompanyFieldObservation, Technology
from scout.tech import detector as tech_detector
from scout.tech.detector import detect_technologies
from scout.util.pools import reset_pools
from tests.unit.crawl.fixture_server import (
    AGENCE_LUMIERE_PAGES,
    FixtureServer,
    configure_overrides,
    reset_crawl_state,
)

pytestmark = pytest.mark.integration


@pytest.fixture
async def site(db, monkeypatch):
    server = FixtureServer()
    await server.start()
    server.add_site("agence-lumiere.fr", AGENCE_LUMIERE_PAGES)
    configure_overrides(monkeypatch, {"agence-lumiere.fr": server.target()})
    reset_pools()
    try:
        yield server
    finally:
        await server.stop()
        await reset_crawl_state()


async def _company(ws: uuid.UUID) -> uuid.UUID:
    async with session_scope() as s:
        c = Company(
            workspace_id=ws,
            name="Agence Lumière",
            normalized_name="agence lumiere",
            website_url="http://agence-lumiere.fr/",
        )
        s.add(c)
        await s.flush()
        return c.id


async def test_detect_technologies_crawls_when_needed_then_caches(site, workspace):
    ws, _ = workspace
    cid = await _company(ws)
    techs = await detect_technologies(ws, cid)
    names = {t.name for t in techs}
    assert {"WordPress", "Elementor", "Google Tag Manager", "Meta Pixel", "HubSpot"} <= names
    wp = next(t for t in techs if t.name == "WordPress")
    assert wp.detector == "builtin" and wp.version == "6.6.2" and wp.source_url == "http://agence-lumiere.fr/"
    async with session_scope() as s:
        obs = (
            await s.scalars(
                sa.select(CompanyFieldObservation).where(
                    CompanyFieldObservation.company_id == cid,
                    CompanyFieldObservation.field_name == "technology",
                )
            )
        ).all()
    assert len(obs) == len(techs)
    assert all(
        o.source_type == SourceType.tech_scan and o.is_current and o.page_id and o.evidence for o in obs
    )

    requests = site.count()
    cached = await detect_technologies(ws, cid)
    assert site.count() == requests
    assert [t.id for t in cached] == [t.id for t in techs]


async def test_detect_technologies_prefers_service_and_replaces_rows(site, workspace, monkeypatch):
    ws, _ = workspace
    cid = await _company(ws)
    await detect_technologies(ws, cid)

    from scout.tech.types import DetectedTech

    async def fake_service(url, headers, html):
        assert html and "wp-content" in html
        return [
            DetectedTech(
                name="WordPress", category="CMS", version="6.6.2", confidence=0.9, evidence="wappalyzergo"
            )
        ]

    monkeypatch.setattr(tech_detector, "detect_via_service", fake_service)
    techs = await detect_technologies(ws, cid, force=True)
    assert [(t.name, t.detector) for t in techs] == [("WordPress", "wappalyzergo")]
    async with session_scope() as s:
        current = (
            await s.scalars(
                sa.select(CompanyFieldObservation).where(
                    CompanyFieldObservation.company_id == cid, CompanyFieldObservation.is_current.is_(True)
                )
            )
        ).all()
        total = await s.scalar(
            sa.select(sa.func.count()).select_from(Technology).where(Technology.company_id == cid)
        )
    assert len(current) == 1 and total == 1


async def test_zero_tech_site_is_not_rescanned_within_freshness_window(site, workspace, monkeypatch):
    ws, _ = workspace
    cid = await _company(ws)
    calls = {"n": 0}

    def nothing_detected(headers, head_html, text, links):
        calls["n"] += 1
        return []

    monkeypatch.setattr(tech_detector.builtin, "detect", nothing_detected)
    assert await detect_technologies(ws, cid) == []
    async with session_scope() as s:
        scanned_at = await s.scalar(sa.select(Company.last_tech_scan_at).where(Company.id == cid))
    assert scanned_at is not None and calls["n"] == 1

    # within max_age_days: no re-scan although no technology row exists
    assert await detect_technologies(ws, cid) == []
    assert calls["n"] == 1
    # outside the window (or forced): scanned again
    async with session_scope() as s:
        await s.execute(
            sa.update(Company)
            .where(Company.id == cid)
            .values(last_tech_scan_at=sa.func.now() - sa.text("interval '40 days'"))
        )
    await detect_technologies(ws, cid)
    assert calls["n"] == 2
    await detect_technologies(ws, cid, force=True)
    assert calls["n"] == 3
