"""Debrief strategies come from the evidence of the run, each with an instruction the assistant can run."""

from __future__ import annotations

from collections import Counter

from scout.chat.debrief import bucket, strategies
from scout.schemas.campaign import CampaignDefinition


def _defn(**kw) -> CampaignDefinition:
    base = {"company_filters": {"industries": ["electronics company"], "countries": ["FR"], "cities": ["Lyon"]}}
    base.update(kw)
    return CampaignDefinition.model_validate(base)


def test_reasons_are_bucketed() -> None:
    assert bucket("Website not found", "resolve_website") == "no_website"
    assert bucket("Altium: not found on website", "website_conditions") == "website_condition"
    assert bucket("Not a web agency: Développe une app de running", "company_qualification") == "business_type"
    assert bucket("Email status UNKNOWN not accepted", "people") == "email"
    assert bucket("Location not confirmed in Annecy", "company_qualification") == "location"


def test_a_signal_nobody_writes_on_websites_leads_to_targeting_likely_users() -> None:
    d = _defn(website_conditions=[{"type": "keyword_any", "terms": ["Altium"]}])
    out = strategies(d, Counter({"website_condition": 89, "no_website": 10}), qualified=0, target=20)
    assert out[0]["action"] == "plan" and "Altium" in out[0]["instruction"]
    assert "89" in out[0]["why"]
    assert any(s["action"] == "amend" and "column" in s["instruction"] for s in out)


def test_email_and_location_blockers_get_their_own_moves() -> None:
    out = strategies(_defn(), Counter({"email": 30, "location": 25, "other": 45}), qualified=3, target=20)
    instructions = " | ".join(s["instruction"] for s in out)
    assert "RISKY" in instructions and "surroundings of Lyon" in instructions
    assert len(out) <= 4
