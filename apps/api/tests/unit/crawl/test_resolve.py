"""Website resolution: domain candidates, mocked DNS, identity verification on a local fixture site."""

from __future__ import annotations

from scout.crawl import resolve
from scout.crawl.resolve import domain_candidates, resolve_website
from tests.unit.crawl.fixture_server import AGENCE_LUMIERE_PAGES, configure_overrides


def test_domain_candidates_fr():
    cands = domain_candidates("Agence Lumière SAS", "FR")
    assert cands[:3] == ["agencelumiere.fr", "agence-lumiere.fr", "lumiere.fr"]
    assert "agencelumiere.com" in cands and "agence-lumiere.com" in cands
    assert len(cands) <= 12
    assert cands.index("agencelumiere.com") > cands.index("lumiere.fr")


def test_domain_candidates_other_countries_and_generic_names():
    de = domain_candidates("Muster Digital GmbH", "DE")
    assert de[0] == "musterdigital.de" and "muster-digital.de" in de
    gb = domain_candidates("Acme Studio Ltd", "GB")
    assert gb[0] == "acmestudio.co.uk" and "acme.co.uk" in gb
    us = domain_candidates("Northwind Traders Inc", None)
    assert us[0] == "northwindtraders.com"
    assert domain_candidates("Marketing", "FR") == []
    assert domain_candidates("SARL", "FR") == []
    assert all(c != "agence.fr" for c in domain_candidates("Agence Digital", "FR"))


async def _setup(fixture_server, monkeypatch, live: set[str]):
    fixture_server.add_site("agence-lumiere.fr", AGENCE_LUMIERE_PAGES)
    configure_overrides(monkeypatch, {"agence-lumiere.fr": fixture_server.target()})
    checked: list[str] = []

    async def fake_dns(domain: str) -> bool:
        checked.append(domain)
        return domain in live

    monkeypatch.setattr(resolve, "dns_exists", fake_dns)
    return checked


async def test_resolve_by_siren_on_legal_page(fixture_server, monkeypatch):
    checked = await _setup(fixture_server, monkeypatch, {"agence-lumiere.fr"})
    found = await resolve_website("Agence Lumière SAS", city="Paris", country="FR", registry_id="812345676")
    assert found is not None
    assert found.domain == "agence-lumiere.fr"
    assert found.url == "http://agence-lumiere.fr/"
    assert found.confidence == 0.98
    assert found.method.endswith("registry_id")
    assert "mentions-legales" in found.evidence
    assert "agencelumiere.fr" in checked  # DNS checked first, only live domains fetched
    assert {h for h, _p, _ in fixture_server.requests} == {"agence-lumiere.fr"}


async def test_resolve_by_phone_and_by_name_city(fixture_server, monkeypatch):
    await _setup(fixture_server, monkeypatch, {"agence-lumiere.fr"})
    by_phone = await resolve_website("Agence Lumière", country="FR", phone="+33 1 23 45 67 89")
    assert by_phone is not None and by_phone.confidence == 0.9
    by_city = await resolve_website("Agence Lumière", city="Paris", country="FR")
    assert by_city is not None and by_city.confidence == 0.82


async def test_resolve_rejects_name_only_and_wrong_siren(fixture_server, monkeypatch):
    await _setup(fixture_server, monkeypatch, {"agence-lumiere.fr"})
    assert await resolve_website("Agence Lumière", city="Lyon", postal_code="69002", country="FR") is None
    assert (
        await resolve_website("Agence Lumière", city="Bordeaux", country="FR", registry_id="853456788")
        is None
    )


async def test_resolve_without_live_domains(fixture_server, monkeypatch):
    await _setup(fixture_server, monkeypatch, set())
    assert await resolve_website("Agence Lumière", city="Paris", country="FR") is None
    assert fixture_server.requests == []


async def test_resolve_uses_free_search_candidates_and_still_proves_identity(fixture_server, monkeypatch):
    # no DNS-guessed candidate is live: the domain only comes from web search, identity is still verified on the site
    await _setup(fixture_server, monkeypatch, set())
    asked: list[str] = []

    async def fake_search(name: str, *, city: str | None = None, country: str | None = None, **_: object):
        asked.append(name)
        return ["annuaire-pro.example", "agence-lumiere.fr"]

    import scout.search.website as sw

    monkeypatch.setattr(sw, "search_domain_candidates", fake_search)
    found = await resolve_website("Agence Lumière SAS", city="Paris", country="FR", registry_id="812345676")
    assert asked == ["Agence Lumière SAS"]
    assert found is not None and found.domain == "agence-lumiere.fr"
    assert found.method.startswith("web_search+")
    assert found.checked == ["annuaire-pro.example", "agence-lumiere.fr"]
