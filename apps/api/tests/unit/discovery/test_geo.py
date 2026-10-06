"""Geography helpers: departments, regions, cities, countries."""

from __future__ import annotations

import pytest

from scout.discovery import geo


def test_reference_data_sizes() -> None:
    assert len(geo.FR_DEPARTMENTS) == 101
    assert len(geo.DEPARTMENTS_BY_ECONOMIC_SIZE) == 101
    assert geo.DEPARTMENTS_BY_ECONOMIC_SIZE[:4] == ("75", "69", "13", "92")
    assert len(geo.FR_REGIONS) == 18
    assert len(geo.country_cities("FR")) >= 150
    for cc in ("GB", "US", "DE", "ES", "IT", "BE", "CH", "NL", "CA", "PT", "IE", "LU", "AT", "SE", "DK", "NO", "FI",
               "PL", "AU", "MA"):
        assert 25 <= len(geo.country_cities(cc)) <= 40, cc


def test_departments_for_regions_and_cities() -> None:
    ara = geo.departments_for(["Auvergne-Rhône-Alpes"], [])
    assert ara[0] == "69" and {"38", "74", "63"} <= set(ara) and len(ara) == 12
    assert geo.departments_for(["IDF"], [])[:2] == ["75", "92"]
    assert geo.departments_for(["R84"], []) == geo.departments_for(["auvergne rhone alpes"], []) == ara
    assert geo.departments_for(["region 84"], []) == ara
    assert geo.departments_for(["84"], []) == ["84"]  # bare code = department (Vaucluse)
    assert geo.departments_for([], ["Lyon"]) == ["69"]
    assert geo.departments_for([], ["Marseille", "Paris"]) == ["75", "13"]  # economic order
    assert geo.departments_for([], ["69003"]) == ["69"]
    assert geo.departments_for([], ["20090"]) == ["2A"]
    assert geo.departments_for(["Alsace"], []) == ["67", "68"]
    assert geo.departments_for(["Gironde"], ["Saint-Étienne"]) == ["33", "42"]
    assert geo.departments_for([], ["Atlantis"]) == []


def test_cities_for_respects_area_and_expansion() -> None:
    lyon = geo.cities_for("FR", cities=["Lyon"])
    assert [c.name for c in lyon] == ["Lyon"]
    wider = [c.name for c in geo.cities_for("FR", cities=["Lyon"], expansion=1)]
    assert wider[0] == "Lyon" and "Villeurbanne" in wider
    assert all(c.department == "69" for c in geo.cities_for("FR", cities=["Lyon"], expansion=3))
    top = geo.cities_for("DE")
    assert top[0].name == "Berlin" and len(top) == 10
    assert len(geo.cities_for("DE", expansion=1)) == 25
    bzh = geo.cities_for("FR", regions=["Bretagne"])
    assert bzh[0].name == "Rennes" and all(c.region == "Bretagne" for c in bzh)
    unknown = geo.cities_for("FR", cities=["Trifouilly-les-Oies"])
    assert unknown[0].name == "Trifouilly-les-Oies" and unknown[0].country == "FR"


def test_find_city_aliases_and_homonyms() -> None:
    assert geo.find_city("Munich").name == "München"
    assert geo.find_city("st etienne").name == "Saint-Étienne"
    assert geo.find_city("Saint-Denis").department == "93"  # mainland first
    assert geo.find_city("London").country == "GB"
    assert geo.find_city("London", "CA").region == "Ontario"
    assert geo.infer_countries(["Lyon"]) == ["FR"]
    assert geo.infer_countries([], ["Bretagne"]) == ["FR"]


@pytest.mark.parametrize(
    ("name", "code"),
    [("France", "FR"), ("Belgique", "BE"), ("Suisse", "CH"), ("Allemagne", "DE"), ("Royaume-Uni", "GB"),
     ("UK", "GB"), ("États-Unis", "US"), ("USA", "US"), ("Pays-Bas", "NL"), ("Espagne", "ES"), ("Maroc", "MA"),
     ("fr", "FR"), ("deutschland", "DE"), ("Atlantis", None)],
)
def test_country_code(name: str, code: str | None) -> None:
    assert geo.country_code(name) == code


def test_country_helpers() -> None:
    assert geo.country_name("DE") == "Germany" and geo.country_name("DE", "fr") == "Allemagne"
    assert geo.country_language("BE") == "fr" and geo.country_language("XX") == "en"
    assert geo.ddg_region("FR") == "fr-fr" and geo.ddg_region(None) == "wt-wt"
