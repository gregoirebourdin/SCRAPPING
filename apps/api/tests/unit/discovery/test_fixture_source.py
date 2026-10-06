"""Fixture source (test / E2E determinism)."""

from __future__ import annotations

from scout.discovery.fixture import FixtureSource

from .conftest import FIXTURES, defn

MANIFEST = str(FIXTURES / "fixture_manifest.json")


def test_only_configured_outside_production(settings_env) -> None:
    src = FixtureSource()
    assert not src.is_configured() and src.suitability(defn()) == 0
    settings_env(DISCOVERY_FIXTURE_MANIFEST=MANIFEST, APP_ENV="test")
    assert src.is_configured() and src.suitability(defn()) >= 10
    settings_env(APP_ENV="production", INTERNAL_API_SECRET="prod-secret-0123456789abcdef0123456789")
    assert not src.is_configured()


async def test_pages_filtered_by_country_and_industry(settings_env) -> None:
    settings_env(DISCOVERY_FIXTURE_MANIFEST=MANIFEST)
    src = FixtureSource()
    [everything_fr] = src.plan(defn(countries=["FR"]))
    p1 = await src.discover(everything_fr, None)
    assert len(p1.candidates) == 10 and p1.next_cursor == {"offset": 10}
    p2 = await src.discover(everything_fr, p1.next_cursor)
    assert len(p2.candidates) == 2 and p2.next_cursor is None  # 13 companies, 1 Belgian

    [q] = src.plan(defn(industries=["agences marketing"], countries=["FR"]))
    page = await src.discover(q, None)
    names = [c.name for c in page.candidates]
    assert page.next_cursor is None and "Agence Lumière" in names and "Socialize" in names
    assert "Bruxelles Média" not in names  # BE
    assert "Cabinet Dentaire Bellecour" not in names  # other industry
    assert "No Category Co" in names  # unknown category passes the loose filter

    first = p1.candidates[0]
    assert first.source == "fixture" and first.registry_id == "812345678" and first.registry_source == "fr_sirene"
    assert first.domain == "agence-lumiere.fr" and first.website == "https://agence-lumiere.fr/"
    assert (first.employee_min, first.employee_max) == (2, 10)
    assert first.people[0]["full_name"] == "Claire Dubois" and first.people[0]["source_url"] == first.website
    by_name = {c.name: c for c in p1.candidates + p2.candidates}
    assert (by_name["Kréa Com"].employee_min, by_name["Kréa Com"].employee_max) == (3, 5)
    assert by_name["Kréa Com"].location["country"] == "FR"  # "France" normalised
