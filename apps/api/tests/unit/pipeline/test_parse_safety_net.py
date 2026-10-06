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


class ManyChatModel:
    """What Gemini returns for "agences social media qui utilisent potentiellement ManyChat": a required tech."""

    name = "gemini"
    available = True

    async def structured(self, *, schema: Any, **_: Any) -> AIResult[Any]:
        from scout.pipeline.icp import AIWebsiteCondition

        value = AIParsedCampaign(
            name="ManyChat agencies",
            target_count=30,
            industries=["social media marketing agency"],
            countries=["FR"],
            titles=["Founder"],
            website_conditions=[AIWebsiteCondition(kind="technology", terms=["Manychat"])],
        )
        return AIResult(value=value, usage=AIUsage(model="fake"))


async def _parse(prompt: str):
    set_ai(ManyChatModel())  # type: ignore[arg-type]
    try:
        defn, _ = await parse_prompt(prompt)
    finally:
        set_ai(None)
    return defn


async def test_a_hedged_criterion_is_a_bonus_column_not_a_filter() -> None:
    defn = await _parse("Trouve 30 agences social media en France qui utilisent potentiellement ManyChat")
    (cond,) = defn.website_conditions
    assert cond.type == "technology" and cond.required is False
    assert [(e.name, e.instruction) for e in defn.enrichments] == [("ManyChat", "Uses ManyChat?")]
    strict = await _parse("Trouve 30 agences social media en France qui utilisent ManyChat")
    assert strict.website_conditions[0].required is True and strict.enrichments == []


async def test_catch_all_emails_are_accepted_unless_verified_only() -> None:
    from scout.db.enums import EmailStatus

    defn = await _parse("Trouve 30 agences social media en France")
    assert EmailStatus.CATCH_ALL in defn.accepted_email_statuses
    strict = await _parse("Trouve 30 agences social media en France, emails vérifiés uniquement")
    assert EmailStatus.CATCH_ALL not in strict.accepted_email_statuses
    assert EmailStatus.SAFE in strict.accepted_email_statuses


class ForgetfulModel(ManyChatModel):
    """What Gemini returned in production: the hedged ManyChat criterion silently dropped."""

    async def structured(self, *, schema: Any, **kw: Any) -> AIResult[Any]:
        res = await super().structured(schema=schema, **kw)
        res.value.website_conditions = []
        return res


async def test_a_named_tool_the_model_dropped_comes_back_as_a_bonus() -> None:
    set_ai(ForgetfulModel())  # type: ignore[arg-type]
    try:
        defn, _ = await parse_prompt(
            "Trouve 15 agences marketing spécialisées réseaux sociaux en France qui utilisent potentiellement "
            "ManyChat, fondateur ou gérant, avec email"
        )
    finally:
        set_ai(None)
    (cond,) = defn.website_conditions
    assert cond.type == "technology" and cond.technologies == ["ManyChat"] and cond.required is False
    assert [e.name for e in defn.enrichments] == ["ManyChat"]


def test_large_targets_widen_further_and_keep_enough_candidates() -> None:
    from scout.pipeline.icp import AIParsedCampaign, ParseContext, to_definition
    from scout.pipeline.jobs import max_expansion

    big = to_definition(
        AIParsedCampaign(
            name="Insta", target_count=3000, industries=["social media marketing agency"], countries=["FR"]
        ),
        "3000 agences social media en France",
        ParseContext(),
    )
    assert big.limits.max_raw_candidates == 120_000 and max_expansion(big) == 11
    small = to_definition(
        AIParsedCampaign(
            name="Web", target_count=50, industries=["web agency"], cities=["Annecy"], countries=["FR"]
        ),
        "50 agences web à Annecy",
        ParseContext(),
    )
    assert small.limits.max_raw_candidates == 60_000 and max_expansion(small) == 2
