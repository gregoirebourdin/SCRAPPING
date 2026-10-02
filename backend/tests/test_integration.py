"""End-to-end tests against a local fixture website (no internet, no browser, no SMTP)."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal, init_db
from app.export import export_csv
from app.extract.contacts import decode_cf_email
from app.fetch.crawler import SiteCrawler
from app.fetch.http import Fetcher
from app.models import Agency, AgencyEmail, Candidate, Client, Run
from app.pipeline.orchestrator import Pipeline, RunConfig
from app.pipeline.processor import process_site


@pytest.fixture(scope="session")
async def processed(agency_site_url: str):
    await init_db()
    async with Fetcher() as fetcher:
        return await process_site(agency_site_url, SiteCrawler(fetcher))


async def test_crawl_covers_key_pages(processed) -> None:
    kinds = {p.kind for p in processed.site.pages}
    assert {"home", "about", "case_studies", "contact", "services"} <= kinds
    assert processed.liveness.alive and processed.liveness.reason == "ok"
    assert processed.language[0] == "en"


async def test_company_and_contacts(processed) -> None:
    c = processed.company
    assert c is not None and c.name == "Northstar Growth"
    assert c.country == "United States"
    assert c.founded_year == 2018
    emails = {e.email for e in processed.contacts.emails}
    assert "hello@northstargrowth.example" in emails
    assert "contact@northstargrowth.example" in emails  # Cloudflare email-protection decoded
    assert "team@northstargrowth.com" in emails  # [at] [dot] de-obfuscation
    assert processed.contacts.emails[0].email == "hello@northstargrowth.example"  # same-domain, mailto, homepage first
    assert processed.contacts.socials.get("linkedin", "").startswith("https://www.linkedin.com/company/")
    assert processed.contacts.socials.get("instagram")
    assert "calendly.com" in (processed.contacts.booking_url or "")
    assert "meta pixel" in processed.tech


async def test_founder_from_jsonld_and_text(processed) -> None:
    f = processed.founder
    assert f.name == "Alex Rivera"
    assert f.linkedin == "https://www.linkedin.com/in/alexrivera-growth/"
    assert f.confidence >= 0.85


async def test_clients_are_coaches_not_team(processed) -> None:
    names = {c.name: c for c in processed.clients}
    assert "Sarah Mitchell" in names and "Daniel Okafor" in names
    assert names["Sarah Mitchell"].role_title and "coach" in names["Sarah Mitchell"].role_title.lower()
    assert names["Sarah Mitchell"].website == "https://sarahmitchell.example/"
    assert names["Sarah Mitchell"].confidence >= 0.6
    # the agency's own people never show up as clients
    assert "Alex Rivera" not in names and "Maria Chen" not in names and "Jordan Lee" not in names
    # brands from the "Trusted by coaches" logo wall
    assert "Launch Lab" in names and names["Launch Lab"].kind == "brand"


async def test_score_qualifies(processed) -> None:
    sc = processed.score
    assert sc is not None
    assert sc.status == "qualified" and sc.score >= 70
    assert "meta ads" in sc.services and "youtube ads" in sc.services and "funnels" in sc.services
    assert "coaches" in sc.icp_signals and "course creators" in sc.icp_signals
    assert sc.tier == "A"


def test_cloudflare_fixture_decodes() -> None:
    assert decode_cf_email("2d4e4243594c4e596d43425f59455e594c5f4a5f425a59450348554c405d4148") == "contact@northstargrowth.example"


async def test_pipeline_crawl_persists_and_exports(agency_site_url: str, tmp_path: Path) -> None:
    await init_db()
    async with SessionLocal() as s:
        run = Run(name="test", status="pending")
        s.add(run)
        s.add(Candidate(domain="127.0.0.1", url=agency_site_url, source="seed"))
        await s.commit()
        run_id = run.id
    # no "emails" stage: the fixture domain has no MX record, and SMTP/DNS are not part of an offline test
    cfg = RunConfig(target_leads=5, engines=[], stages=["crawl", "finalize"], countries=["us"])
    pipeline = Pipeline(run_id, cfg)
    await pipeline.run()
    async with SessionLocal() as s:
        run = await s.get(Run, run_id)
        assert run is not None and run.status == "completed"
        agency = (await s.execute(select(Agency).where(Agency.domain == "127.0.0.1"))).scalar_one()
        assert agency.status == "qualified" and agency.name == "Northstar Growth"
        assert agency.founder_linkedin
        emails = list((await s.execute(select(AgencyEmail).where(AgencyEmail.agency_id == agency.id))).scalars())
        assert any(e.is_primary for e in emails)
        clients = list((await s.execute(select(Client).where(Client.agency_id == agency.id))).scalars())
        assert any(c.name == "Sarah Mitchell" for c in clients)
    out = tmp_path / "leads.csv"
    n = await export_csv(out)
    assert n >= 1
    rows = list(csv.DictReader(out.open(encoding="utf-8")))
    row = next(r for r in rows if r["domain"] == "127.0.0.1")
    assert row["email"] == "hello@northstargrowth.example"
    assert row["founder_name"] == "Alex Rivera"
    assert row["client_name"] == "Sarah Mitchell"


async def test_api_endpoints(agency_site_url: str) -> None:
    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        assert (await c.get("/api/health")).json()["ok"] is True
        stats = (await c.get("/api/stats")).json()
        assert stats["agencies"] >= 1
        page = (await c.get("/api/leads", params={"has_email": "true"})).json()
        assert page["total"] >= 1 and page["items"][0]["primary_email"]
        lead_id = page["items"][0]["id"]
        detail = (await c.get(f"/api/leads/{lead_id}")).json()
        assert detail["emails"] and detail["clients"]
        patched = (await c.patch(f"/api/leads/{lead_id}", json={"user_status": "contacted"})).json()
        assert patched["user_status"] == "contacted"
        csv_res = await c.get("/api/export.csv")
        assert csv_res.status_code == 200 and csv_res.text.startswith("tier,score")
        assert (await c.get("/api/leads/999999")).status_code == 404
        assert (await c.post("/api/runs", json={"stages": ["nope"]})).status_code == 422
    assert settings.data_dir.exists()
