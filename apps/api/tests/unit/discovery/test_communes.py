"""City-precise location: communes from the geo API, the location gate and registry queries by commune."""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
import respx

from scout.discovery import communes
from scout.discovery.fr_registry import FrRegistrySource
from scout.pipeline.scoring import location_fit
from scout.schemas.campaign import CampaignDefinition

ANNECY = communes.Commune(
    code="74010",
    name="Annecy",
    department="74",
    postal_codes=("74000", "74370", "74600", "74940", "74960"),
    population=132117,
)


@pytest.fixture(autouse=True)
def clean() -> Iterator[None]:
    communes.clear_cache()
    yield
    communes.clear_cache()


def _defn(**cf) -> CampaignDefinition:
    return CampaignDefinition.model_validate(
        {"company_filters": {"industries": ["web agency"], "countries": ["FR"], **cf}}
    )


def test_pick_is_exact_and_most_populous() -> None:
    rows = [
        {
            "nom": "Saint-Denis",
            "code": "93066",
            "codeDepartement": "93",
            "codesPostaux": ["93200"],
            "population": 113000,
        },
        {
            "nom": "Saint-Denis",
            "code": "97411",
            "codeDepartement": "974",
            "codesPostaux": ["97400"],
            "population": 153000,
        },
        {"nom": "Saint-Denis-lès-Bourg", "code": "01344", "codeDepartement": "01", "codesPostaux": ["01000"]},
    ]
    assert communes.pick("saint denis", rows).code == "97411"  # type: ignore[union-attr]
    assert communes.pick("Lyon", rows) is None  # never a guess


def test_a_named_city_means_the_commune_not_the_department() -> None:
    d = _defn(cities=["Annecy"])
    # unresolved: department-level fallback (Thonon is in 74)
    assert location_fit(d, "FR", "Thonon-les-Bains", None, "74200") == 0.9
    communes.remember(ANNECY)
    assert location_fit(d, "FR", "Thonon-les-Bains", None, "74200") == 0.0
    assert location_fit(d, "FR", "Seynod", None, "74600") == 1.0  # merged former commune of Annecy
    assert location_fit(d, "FR", "ANNECY", None, None) == 1.0
    assert location_fit(d, "FR", "Cran-Gevrier", None, None) == 0.0  # a known city outside, no postal code
    assert (
        location_fit(d, "FR", None, None, None) is None
    )  # unknown → rejected by the pipeline when a city is named
    # a region keeps department-level matching
    assert (
        location_fit(_defn(regions=["Auvergne-Rhône-Alpes"]), "FR", "Thonon-les-Bains", None, "74200") == 0.9
    )


def test_registry_queries_use_the_commune_once_resolved() -> None:
    src = FrRegistrySource()
    d = _defn(cities=["Annecy"])
    assert all("dep:74" in q.key for q in src.plan(d))
    communes.remember(ANNECY)
    qs = src.plan(d)
    assert qs and all(q.params["code_commune"] == "74010" for q in qs)


@respx.mock
async def test_resolve_calls_the_geo_api_once() -> None:
    route = respx.get("https://geo.api.gouv.fr/communes").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "nom": "Annecy",
                    "code": "74010",
                    "codeDepartement": "74",
                    "codesPostaux": ["74000"],
                    "population": 1,
                }
            ],
        )
    )
    assert (await communes.resolve("annecy")).code == "74010"  # type: ignore[union-attr]
    assert (await communes.resolve("Annecy")).code == "74010"  # type: ignore[union-attr]
    assert route.call_count == 1
