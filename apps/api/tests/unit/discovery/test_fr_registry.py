"""French registry adapter: mapping of captured API responses, plan, pagination and error handling.

Fixtures: ``fr_registry_page.json`` is a live response (73.11Z+70.21Z, dep 69, tranches 01–12, page 1/24,
captured 2026-10-05); ``fr_registry_last_page.json`` holds real records from another live response with the
pagination metadata set to a single page.
"""

from __future__ import annotations

import copy

import httpx
import pytest
import respx

from scout.discovery.base import DiscoveryQuery
from scout.discovery.common import Throttle
from scout.discovery.fr_registry import FrRegistrySource, map_result
from scout.errors import FetchError, PermanentError, RateLimitedError

from .conftest import defn, fixture_json

API = "https://recherche-entreprises.api.gouv.fr/search"


def by_siren(body: dict, siren: str) -> dict:
    return next(r for r in body["results"] if r["siren"] == siren)


@pytest.fixture
def src() -> FrRegistrySource:
    return FrRegistrySource(throttle=Throttle(0))


def test_mapping_core_fields() -> None:
    item = by_siren(fixture_json("fr_registry_page.json"), "839985603")  # DENTSUX FRANCE
    c = map_result(item)
    assert c is not None
    assert c.source == "fr_registry" and c.source_entity_id == "839985603"
    assert c.registry_source == "fr_sirene" and c.registry_id == "839985603"
    assert c.name == "DENTSUX FRANCE"
    assert c.website is None
    assert c.category == "73.11Z Activités des agences de publicité"
    assert (c.employee_min, c.employee_max) == (20, 49)  # tranche 12 (legal unit)
    assert c.status == "active"
    assert c.source_url == "https://annuaire-entreprises.data.gouv.fr/entreprise/839985603"
    loc = c.location
    assert loc["country"] == "FR" and loc["city"] == "PARIS" and loc["postal_code"] == "75017"
    assert loc["department"] == "75" and loc["region"] == "Île-de-France"
    assert isinstance(loc["lat"], float) and isinstance(loc["lng"], float)
    assert c.raw_data["matching_etablissements"][0]["libelle_commune"] == "LYON"


def test_people_from_physical_directors_only() -> None:
    c = map_result(by_siren(fixture_json("fr_registry_page.json"), "839985603"))
    assert c is not None
    assert [p["full_name"] for p in c.people] == ["Pierre Calmard"]  # legal-entity auditors dropped
    p = c.people[0]
    assert p["title"] == "Président de SAS" and p["first_name"] == "Pierre" and p["last_name"] == "Calmard"
    assert p["raw"]["prenoms"] == "PIERRE MARCEL PARVIZ"
    assert p["source_url"].endswith("/839985603")


def test_statutory_auditors_and_birth_names_are_handled() -> None:
    globe = map_result(by_siren(fixture_json("fr_registry_last_page.json"), "441514361"))
    assert globe is not None and globe.people == []  # only "Commissaire aux comptes" natural persons
    ebra = by_siren(fixture_json("fr_registry_page.json"), "392987582")
    names = [p["full_name"] for p in map_result(ebra).people]
    assert names == ["Soizic Bouju", "Pierre Fanneau"]
    tweaked = copy.deepcopy(ebra)
    tweaked["dirigeants"] = [{"nom": "GUILLOT (DUPESSEY)", "prenoms": "CAROLE FRANCOISE", "qualite": "Gérant",
                              "type_dirigeant": "personne physique"}]
    assert map_result(tweaked).people[0]["full_name"] == "Carole Guillot"


def test_display_name_rules() -> None:
    page = fixture_json("fr_registry_page.json")
    assert map_result(by_siren(page, "530437698")).name == "TRIB-U"  # ambiguous trade name → first enseigne
    assert map_result(by_siren(page, "451292445")).name == "LOYALTY COMPANY"  # 'A; B; C' list → legal name
    item = copy.deepcopy(by_siren(page, "444360069"))
    item.update({"nom_complet": "ACME CONSEIL (ACME)", "sigle": "ACME"})
    item["siege"].update({"nom_commercial": None, "liste_enseignes": None})
    assert map_result(item).name == "ACME CONSEIL"  # sigle suffix stripped


def test_query_area_flag() -> None:
    c = map_result(by_siren(fixture_json("fr_registry_page.json"), "839985603"), departments=["69"])
    assert c is not None and c.raw_data["siege_in_query_area"] is False


def test_suitability() -> None:
    src = FrRegistrySource()
    fr = src.suitability(defn(industries=["marketing agency"], countries=["FR"], employee_range=(2, 30)))
    assert fr == 1.0
    assert src.suitability(defn(industries=["agences marketing"])) > 0  # French phrasing, no country
    assert src.suitability(defn(industries=["dentistes"], cities=["Lyon"])) > 0
    assert src.suitability(defn(industries=["marketing agency"], countries=["US"])) == 0
    assert src.suitability(defn(industries=["quantum blorp"], countries=["FR"])) == 0
    local = src.suitability(defn(industries=["dentist"], countries=["FR"]))
    assert 0 < local < fr


def test_plan_without_geo_restriction() -> None:
    d = defn(industries=["marketing agency"], countries=["FR"], employee_range=(2, 30))
    plan = FrRegistrySource().plan(d)
    assert len(plan) == 20
    assert [q.params["departement"] for q in plan[:3]] == ["75", "69", "13"]
    q = plan[0]
    assert q.params["naf"] == ["73.11Z", "70.21Z"]
    assert q.params["tranches"] == ["01", "02", "03", "11", "12"]
    assert q.key == "naf:73.11Z,70.21Z|dep:75|t:01,02,03,11,12"
    wider = FrRegistrySource().plan(d, expansion=1)
    assert {x.key for x in plan} <= {x.key for x in wider}  # cumulative, stable keys
    assert len({x.params["departement"] for x in wider}) == 101
    assert any("73.12Z" in x.params["naf"] for x in wider)  # adjacent NAF codes


def test_plan_restricted_to_requested_area() -> None:
    d = defn(industries=["dentistes"], cities=["Lyon"])
    plan = FrRegistrySource().plan(d)
    assert [q.params["departement"] for q in plan] == ["69"]
    assert plan[0].params["tranches"] == [] and plan[0].params["naf"] == ["86.23Z"]
    assert {q.params["departement"] for q in FrRegistrySource().plan(d, expansion=2)} == {"69"}
    regions = FrRegistrySource().plan(defn(industries=["agence web"], regions=["Bretagne"]))
    assert [q.params["departement"] for q in regions] == ["35", "29", "56", "22"]


@respx.mock
async def test_discover_pagination_and_params(src: FrRegistrySource) -> None:
    route = respx.get(API).mock(return_value=httpx.Response(200, json=fixture_json("fr_registry_page.json")))
    q = DiscoveryQuery(key="k", params={"naf": ["73.11Z", "70.21Z"], "departement": "69",
                                        "tranches": ["01", "02", "03", "11", "12"], "restricted": True})
    page = await src.discover(q, None)
    assert len(page.candidates) == 25 and page.next_cursor == {"page": 2}
    sent = route.calls.last.request.url.params
    assert sent["activite_principale"] == "73.11Z,70.21Z" and sent["departement"] == "69"
    assert sent["tranche_effectif_salarie"] == "01,02,03,11,12" and sent["etat_administratif"] == "A"
    assert sent["per_page"] == "25" and sent["page"] == "1" and sent["minimal"] == "true"
    assert "dirigeants" in sent["include"]

    route.mock(return_value=httpx.Response(200, json=fixture_json("fr_registry_last_page.json")))
    last = await src.discover(q, {"page": 2})
    assert route.calls.last.request.url.params["page"] == "2"
    assert len(last.candidates) == 5 and last.next_cursor is None


@respx.mock
async def test_empty_results_exhaust(src: FrRegistrySource) -> None:
    respx.get(API).mock(return_value=httpx.Response(200, json={"results": [], "total_results": 0, "page": 1,
                                                                "per_page": 25, "total_pages": 0}))
    page = await src.discover(DiscoveryQuery(key="k", params={"naf": ["86.23Z"], "departement": "48"}), None)
    assert page.candidates == [] and page.next_cursor is None


@respx.mock
@pytest.mark.parametrize(("status", "exc"), [(429, RateLimitedError), (503, FetchError), (400, PermanentError)])
async def test_http_errors(src: FrRegistrySource, status: int, exc: type[Exception]) -> None:
    respx.get(API).mock(return_value=httpx.Response(status, json={"erreur": "x"}))
    with pytest.raises(exc):
        await src.discover(DiscoveryQuery(key="k", params={"naf": ["73.11Z"], "departement": "75"}), None)


@respx.mock
async def test_network_error_is_retryable(src: FrRegistrySource) -> None:
    respx.get(API).mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(FetchError):
        await src.discover(DiscoveryQuery(key="k", params={"naf": ["73.11Z"], "departement": "75"}), None)
