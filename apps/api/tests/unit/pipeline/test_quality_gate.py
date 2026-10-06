"""ICP score + quality gate (spec §72–§74, PIPELINE.md §3.5–3.6): every threshold is enforced at its boundary,
uncollected components never count as zero, and only fully qualified rows pass."""

from __future__ import annotations

import pytest

from scout.db.enums import CampaignMode, EmailStatus
from scout.pipeline.scoring import ConditionOutcome, ScoringInput, location_fit, score, size_fit
from scout.schemas.campaign import CampaignDefinition


def _defn(**kw) -> CampaignDefinition:
    base = {
        "company_filters": {"industries": ["marketing agency"], "countries": ["FR"]},
        "people_filters": {"titles": ["Founder", "CEO"]},
    }
    base.update(kw)
    return CampaignDefinition.model_validate(base)


def _lead(defn: CampaignDefinition | None = None, **kw) -> ScoringInput:
    values = dict(
        defn=defn or _defn(),
        industry_fit=1.0,
        size_fit=None,
        location_fit=1.0,
        company_confidence=0.9,
        person_identified=True,
        person_name="Claire Fontaine",
        person_title="CEO",
        title_match=1.0,
        decision_power=95,
        person_confidence=0.92,
        email="claire@agence.fr",
        email_status=EmailStatus.SAFE,
        email_confidence=0.95,
        evidence_sources=3,
        evidence_quality=0.9,
    )
    values.update(kw)
    return ScoringInput(**values)


def _gate(result, name: str) -> dict:
    return next(g for g in result.gates if g["gate"] == name)


def test_fully_qualified_lead_passes_every_gate():
    r = score(_lead())
    assert r.qualified and r.first_failure is None
    assert all(g["passed"] for g in r.gates)
    assert r.icp_score >= 75


@pytest.mark.parametrize(("confidence", "ok"), [(0.79, False), (0.80, True), (0.81, True)])
def test_person_confidence_threshold(confidence, ok):
    r = score(_lead(person_confidence=confidence))
    assert _gate(r, "person_confidence")["passed"] is ok
    assert r.qualified is ok


@pytest.mark.parametrize(("confidence", "ok"), [(0.79, False), (0.80, True)])
def test_email_confidence_threshold(confidence, ok):
    r = score(_lead(email_confidence=confidence))
    assert _gate(r, "email_confidence")["passed"] is ok


@pytest.mark.parametrize(
    "status", [EmailStatus.RISKY, EmailStatus.CATCH_ALL, EmailStatus.UNKNOWN, EmailStatus.INVALID]
)
def test_only_accepted_email_statuses_qualify(status):
    r = score(_lead(email_status=status))
    assert not r.qualified
    assert r.first_failure == f"Email status {status.value} not accepted"


def test_risky_qualifies_only_when_explicitly_accepted():
    defn = _defn(accepted_email_statuses=["SAFE", "RISKY"])
    assert score(_lead(defn, email_status=EmailStatus.RISKY, email_confidence=0.85)).qualified
    assert not score(_lead(defn, email_status=EmailStatus.CATCH_ALL, email_confidence=0.85)).qualified


def test_missing_email_fails_when_required_but_not_in_optional_email_campaign():
    r = score(_lead(email=None, email_status=None, email_confidence=None))
    assert not r.qualified and _gate(r, "email_exists")["passed"] is False
    optional = _defn(required_fields=["company", "person"], minimum_icp_score=50)
    r2 = score(_lead(optional, email=None, email_status=None, email_confidence=None))
    assert not any(g["gate"].startswith("email") for g in r2.gates)
    assert r2.qualified


def test_role_must_match_requested_titles():
    r = score(_lead(title_match=0.4, person_title="Community manager"))
    assert not r.qualified
    assert r.first_failure == "Role 'Community manager' does not match"


def test_no_person_fails_in_people_mode():
    r = score(_lead(person_identified=False, person_name=None, title_match=None, person_confidence=None))
    assert not r.qualified and _gate(r, "person_identified")["passed"] is False


@pytest.mark.parametrize(("threshold", "ok"), [(60, True), (101, False)])
def test_company_fit_threshold(threshold, ok):
    defn = _defn(minimum_company_fit=min(threshold, 100))
    r = score(_lead(defn, industry_fit=1.0 if ok else 0.2, location_fit=1.0))
    assert _gate(r, "company_fit")["passed"] is ok


def test_company_fit_gate_passes_when_no_company_criteria_requested():
    """List-seeded campaigns ('a different decision maker at these companies') request no company criteria:
    company fit is not scored, and must not fail as a zero."""
    defn = CampaignDefinition.model_validate({"people_filters": {"titles": ["Head of Marketing"]}})
    r = score(_lead(defn, industry_fit=None, location_fit=None))
    assert r.company_fit is None
    assert _gate(r, "company_fit")["passed"] is True
    assert r.qualified
    assert "No company criteria requested (company fit not scored)" in r.explanation


def test_icp_score_threshold_boundary():
    lead = _lead(
        title_match=0.5, decision_power=10, person_confidence=0.8, email_confidence=0.8, evidence_quality=0.5
    )
    icp = score(lead).icp_score
    assert score(_lead(_defn(minimum_icp_score=icp), **_without_defn(lead))).qualified
    r = score(_lead(_defn(minimum_icp_score=icp + 1), **_without_defn(lead)))
    assert not r.qualified and r.first_failure == f"ICP score {icp} < {icp + 1}"


def _without_defn(inp: ScoringInput) -> dict:
    return {k: v for k, v in inp.__dict__.items() if k != "defn"}


def test_required_condition_must_be_true_unknown_is_not_enough():
    unknown = ConditionOutcome(label="Offers Instagram", passed=None, confidence=0.4)
    r = score(_lead(conditions=[unknown]))
    assert not r.qualified and r.first_failure == "Offers Instagram: insufficient evidence"
    failed = ConditionOutcome(label="Offers Instagram", passed=False, confidence=0.9)
    assert score(_lead(conditions=[failed])).first_failure == "Offers Instagram: not satisfied"
    optional = ConditionOutcome(label="Mentions ManyChat", passed=False, confidence=0.9, required=False)
    assert score(_lead(conditions=[optional])).qualified


def test_exclusion_and_suppression_always_block():
    assert score(_lead(excluded=True)).first_failure == "Previously seen (exclusion active)"
    assert score(_lead(suppressed=True)).first_failure == "Suppressed"


def test_uncollected_components_are_excluded_from_the_denominator():
    """No signal stage ran: intent is excluded (not a zero) and the explanation says so."""
    r = score(_lead())
    assert r.intent is None and "intent" not in r.weights
    assert "No intent signals collected (excluded from score)" in r.explanation
    with_signal = score(_lead(signals=[{"type": "hiring", "confidence": 0.2}]))
    assert "intent" in with_signal.weights and with_signal.icp_score < r.icp_score


def test_company_mode_ignores_person_and_email_gates():
    defn = CampaignDefinition.model_validate(
        {"mode": "companies", "company_filters": {"industries": ["marketing agency"]}}
    )
    assert defn.mode == CampaignMode.companies
    r = score(
        ScoringInput(
            defn=defn,
            industry_fit=0.9,
            size_fit=None,
            location_fit=None,
            company_confidence=0.9,
            phone=True,
            evidence_sources=2,
            evidence_quality=0.9,
        )
    )
    assert r.qualified
    assert {g["gate"] for g in r.gates} == {"company_fit", "not_excluded", "not_suppressed", "icp_score"}


def test_size_and_location_fit():
    defn = _defn(
        company_filters={"employee_range": {"min": 2, "max": 30}, "countries": ["FR"], "cities": ["Lyon"]}
    )
    assert size_fit(defn, 5, 10) == 1.0
    assert size_fit(defn, 31, 31) == 0.3  # near miss
    assert size_fit(defn, 200, 500) == 0.0
    assert size_fit(defn, None, None) is None  # unknown size is not a mismatch
    assert location_fit(defn, "FR", "lyon", None) == 1.0
    # French cities compare at department level: another city is outside, a suburb is inside
    assert location_fit(defn, "FR", "Paris", None) == 0.0
    assert location_fit(defn, "FR", "Marseille", None, "13001") == 0.0
    assert location_fit(defn, "FR", "Villeurbanne", None, "69100") == 0.9
    assert location_fit(defn, "FR", None, None) is None  # unknown location is not a mismatch
    assert location_fit(defn, "BE", "Bruxelles", None) == 0.0
    region = _defn(company_filters={"countries": ["FR"], "regions": ["Bretagne"]})
    assert location_fit(region, "FR", "Rennes", None, "35000") == 0.9
    assert location_fit(region, "FR", "Lyon", None, "69002") == 0.0


def test_accepting_risky_lowers_the_email_confidence_floor_to_the_risky_band() -> None:
    from scout.pipeline.scoring import email_confidence_floor

    strict = _defn()
    assert email_confidence_floor(strict) == strict.minimum_email_confidence == 80
    risky_ok = _defn(accepted_email_statuses=["SAFE", "LIKELY_SAFE", "RISKY"])
    assert email_confidence_floor(risky_ok) == 50
    assert email_confidence_floor(_defn(accepted_email_statuses=["SAFE", "UNKNOWN"])) == 0
    # a stricter user floor still wins over the band
    assert email_confidence_floor(_defn(accepted_email_statuses=["RISKY"], minimum_email_confidence=30)) == 30

    guess = dict(email="claire.fontaine@agence.fr", email_status=EmailStatus.RISKY, email_confidence=0.62)
    assert not score(_lead(strict, **guess)).qualified
    r = score(_lead(risky_ok, **guess))
    assert _gate(r, "email_confidence")["passed"] and _gate(r, "email_status")["passed"]
    low = score(_lead(risky_ok, **{**guess, "email_confidence": 0.41}))
    assert not _gate(low, "email_confidence")["passed"]


def test_headings_and_product_names_never_become_leads() -> None:
    from scout.extract.names import has_known_first_name

    assert has_known_first_name("Jean Dupont") and has_known_first_name("DUPONT Jean")
    assert has_known_first_name("Marie-Claire Roux") and has_known_first_name("Pierre-Eric Beaudraps")
    assert not has_known_first_name("Related Websites")
    assert not has_known_first_name("Prompts Gpt")
