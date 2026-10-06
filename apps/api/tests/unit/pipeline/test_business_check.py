"""Business-type verification from the company's own pages (fast model), never from the activity code alone."""

from __future__ import annotations

from typing import Any

import pytest

from scout.ai.factory import set_ai
from scout.ai.provider import AIResult, AIUsage
from scout.pipeline.business_check import BusinessVerdict, check_business_type


class Says:
    name = "gemini"
    available = True

    def __init__(self, verdict: str, what: str, confidence: float = 0.9) -> None:
        self.v = (verdict, what, confidence)
        self.prompts: list[str] = []

    async def structured(self, *, schema: Any, prompt: str, **_: Any) -> AIResult[Any]:
        self.prompts.append(prompt)
        verdict, what, conf = self.v
        return AIResult(
            value=schema(verdict=verdict, what_it_does=what, confidence=conf), usage=AIUsage(model="lite")
        )


@pytest.fixture(autouse=True)
def reset_ai():
    yield
    set_ai(None)


async def test_a_developer_with_a_running_app_is_not_a_web_agency() -> None:
    ai = Says("no", "Développe et édite une application de running")
    set_ai(ai)  # type: ignore[arg-type]
    v = await check_business_type(
        ["web agency"],
        name="RUNAPP",
        registry_activity="62.01Z Programmation informatique",
        pages_text="Notre app…",
    )
    assert v is not None and v.is_mismatch and not v.is_match
    assert "web agency" in ai.prompts[0] and "62.01Z" in ai.prompts[0]


async def test_low_confidence_is_neither_match_nor_mismatch_and_round_trips() -> None:
    set_ai(Says("match", "Création de sites web", confidence=0.4))  # type: ignore[arg-type]
    v = await check_business_type(["web agency"], name="X", registry_activity=None, pages_text="…")
    assert v is not None and not v.is_match and not v.is_mismatch
    assert BusinessVerdict.from_dict(v.as_dict()) == BusinessVerdict(v.verdict, v.what_it_does, v.confidence)


async def test_no_text_or_no_industry_means_no_verdict() -> None:
    set_ai(Says("match", "x"))  # type: ignore[arg-type]
    assert await check_business_type(["web agency"], name="X", registry_activity=None, pages_text="") is None
    assert await check_business_type([], name="X", registry_activity=None, pages_text="text") is None


def test_the_most_specific_business_type_wins():
    from scout.pipeline.business_check import most_specific

    assert most_specific(["social media marketing agency", "marketing agency"]) == [
        "social media marketing agency"
    ]
    assert most_specific(["web agency", "digital agency"]) == [
        "web agency",
        "digital agency",
    ]  # siblings both stay
    assert most_specific(["marketing agency"]) == ["marketing agency"]
