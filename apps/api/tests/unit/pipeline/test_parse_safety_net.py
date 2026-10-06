"""parse_prompt: the deterministic parser backs up a model that drops the location or the business type."""

from __future__ import annotations

from typing import Any

from scout.ai.factory import set_ai
from scout.ai.provider import AIResult, AIUsage
from scout.discovery.router import select_sources
from scout.pipeline.icp import AIParsedCampaign, parse_prompt


class TitlesOnlyModel:
    """What Gemini returned for "10 coachs sportifs a annecy": titles, no city, no industry."""

    name = "gemini"
    available = True

    async def structured(self, *, schema: Any, **_: Any) -> AIResult[Any]:
        value = AIParsedCampaign(
            name="SPORT", target_count=10, titles=["Coach sportif", "Personal Trainer"], countries=[]
        )
        return AIResult(value=value, usage=AIUsage(model="fake"))


async def test_location_and_trade_come_back_from_the_heuristic_parser() -> None:
    set_ai(TitlesOnlyModel())  # type: ignore[arg-type]
    try:
        defn, parser = await parse_prompt(
            "Fais moi une liste de 10 coachs sportifs a annecy et crée une liste qui s'appelle SPORT"
        )
    finally:
        set_ai(None)
    assert parser == "gemini"
    cf = defn.company_filters
    assert cf.cities == ["Annecy"] and cf.countries == ["FR"] and cf.industries == ["personal trainer"]
    keys = [s.key for s, _ in select_sources(defn)]
    assert "fr_registry" in keys  # the campaign has somewhere to search


def test_a_specialty_restated_as_a_website_condition_is_not_a_hard_filter() -> None:
    from scout.pipeline.icp import _restates_target

    assert _restates_target("offers growth marketing services", ["marketing agency", "growth marketing"])
    assert not _restates_target("uses Shopify Plus for e-commerce", ["marketing agency"])
