"""Clarification protocol (≤ 3 questions, one round), answers → definition, and "resume with changes" parsing —
all deterministic (no AI provider needed)."""

from __future__ import annotations

import pytest

import scout.chat.tools  # noqa: F401  (registers the tools)
from scout.chat import local_router
from scout.chat.amend import apply_amendment, parse_instruction
from scout.chat.clarify import ClarifyAnswer, apply_answers, build_questions, wants_go
from scout.chat.i18n import detect_lang
from scout.db.enums import CampaignMode, EmailStatus, ExclusionMode
from scout.errors import ValidationFailed
from scout.pipeline.icp import heuristic_parse, to_definition
from scout.pipeline.icp import ParseContext
from scout.schemas.campaign import CampaignDefinition, CompanyFilters, PeopleFilters


def _defn(text: str) -> CampaignDefinition:
    return to_definition(heuristic_parse(text), text, ParseContext())


@pytest.mark.parametrize(
    ("request_text", "expected"),
    [
        ("Trouve des agences marketing", ["geography", "roles", "volume"]),
        ("Find leads", ["industry", "geography", "roles"]),
        ("Find dentists in Lyon", ["roles", "volume", "size"]),
    ],
)
def test_vague_requests_get_at_most_three_ordered_questions(request_text, expected):
    questions, _lang, _missing = build_questions(request_text)
    assert [q.id for q in questions] == expected
    assert len(questions) <= 3
    for q in questions:
        assert 1 <= len(q.options) <= 4
        assert q.default is not None and q.default in {o.value for o in q.options}


@pytest.mark.parametrize(
    "request_text",
    [
        "Find 200 marketing agencies in Lyon with 2-30 employees, founders with a safe email, only new leads",
        "Trouve 50 agences marketing à Lyon, fondateurs",
        "Find 300 marketing agencies in France with 2–30 employees, founders only, no leads I've seen before",
    ],
)
def test_precise_requests_need_no_questions(request_text):
    questions, _lang, _missing = build_questions(request_text)
    assert questions == []


def test_questions_follow_the_users_language():
    fr, lang_fr, _ = build_questions("Trouve des agences marketing")
    en, lang_en, _ = build_questions("Find marketing agencies")
    assert lang_fr == "fr" and fr[0].text == "Où dois-je chercher ?"
    assert lang_en == "en" and en[0].text == "Where should I look?"
    assert detect_lang("reprends en ajoutant Marseille") == "fr"
    assert detect_lang("resume and add Marseille") == "en"


@pytest.mark.parametrize("text", ["vas-y", "Go", "lance", "launch it", "c'est parti", "sans questions"])
def test_go_words(text):
    assert wants_go(text)


def test_go_words_do_not_match_ordinary_words():
    assert not wants_go("Find agencies in Gortyna")
    assert not wants_go("Trouve des agences marketing")


def test_answers_are_applied_as_structured_overrides():
    d = _defn("Trouve des agences marketing")
    out, leftovers = apply_answers(
        d,
        [
            ClarifyAnswer(id="geography", value="Lyon"),
            ClarifyAnswer(id="roles", value="founders"),
            ClarifyAnswer(id="volume", value="50"),
            ClarifyAnswer(id="size", value="2-30"),
            ClarifyAnswer(id="email", value="safe_only"),
            ClarifyAnswer(id="exclusions", value="new_only"),
            ClarifyAnswer(id="tiktok", value="yes", question="Must they run TikTok ads?"),
        ],
    )
    cf = out.company_filters
    assert cf.cities == ["Lyon"] and cf.countries == ["FR"]
    assert out.target_qualified_count == 50
    assert cf.employee_range is not None and (cf.employee_range.min, cf.employee_range.max) == (2, 30)
    assert "Founder" in out.people_filters.titles
    assert out.accepted_email_statuses == [EmailStatus.SAFE]
    assert out.exclusion.mode == ExclusionMode.EXCLUDE_PREVIOUS_PEOPLE
    assert leftovers == ["Must they run TikTok ads?: yes"]


def test_companies_only_answer_switches_mode_and_country_codes():
    out, _ = apply_answers(
        _defn("Find marketing agencies"),
        [ClarifyAnswer(id="roles", value="companies"), ClarifyAnswer(id="geography", value="DE")],
    )
    assert out.mode == CampaignMode.companies and not out.requires_email
    assert out.company_filters.countries == ["DE"] and out.company_filters.cities == []


# ---- amendments --------------------------------------------------------------------------------------

BASE = CampaignDefinition(
    target_qualified_count=50,
    company_filters=CompanyFilters(industries=["marketing agency"], countries=["FR"], cities=["Lyon"]),
    people_filters=PeopleFilters(titles=["Founder", "CEO", "Owner"]),
)


def _diff(text: str) -> dict[str, tuple[str, str]]:
    d, changes, _w = apply_amendment(BASE, parse_instruction(text, BASE), qualified=10)
    assert CampaignDefinition.model_validate(d.model_dump())  # still a valid definition
    return {c.field: (c.before, c.after) for c in changes}


def test_amend_add_city():
    assert _diff("ajoute aussi Marseille") == {"location": ("Lyon, France", "Lyon, Marseille, France")}


def test_amend_replace_and_remove_city():
    assert _diff("seulement Paris")["location"][1] == "Paris, France"
    assert _diff("retire Lyon")["location"][1] == "France"


def test_amend_only_founders_replaces_titles():
    assert _diff("seulement les fondateurs") == {"titles": ("Founder / CEO / Owner", "Founder / Co-Founder")}


def test_amend_add_titles_keeps_existing():
    after = _diff("add the CMOs too")["titles"][1]
    assert after.startswith("Founder / CEO / Owner") and "CMO" in after


def test_amend_relative_and_absolute_target():
    assert _diff("+200 leads") == {"target": ("50", "250")}
    assert _diff("100 leads de plus") == {"target": ("50", "150")}
    assert _diff("monte à 300 leads") == {"target": ("50", "300")}


def test_amend_budget_is_not_a_lead_count():
    assert _diff("budget 10 $") == {"budget": ("No limit", "$10.00")}


def test_amend_combined_change():
    diff = _diff("100 leads de plus et ajoute Marseille")
    assert set(diff) == {"target", "location"}


def test_amend_safe_fields_are_marked_safe():
    _d, changes, _w = apply_amendment(BASE, parse_instruction("+20 leads, budget 5 $", BASE))
    assert {c.field for c in changes} == {"target", "budget"} and all(c.safe for c in changes)
    _d, changes, warnings = apply_amendment(BASE, parse_instruction("ajoute Marseille", BASE))
    assert not changes[0].safe and any("already found" in w for w in warnings)


def test_amend_unmapped_instruction_is_refused_with_examples():
    with pytest.raises(ValidationFailed) as exc:
        parse_instruction("blabla", BASE)
    assert exc.value.code == "amend_not_understood" and "+100 leads" in (exc.value.hint or "")


# ---- deterministic router ------------------------------------------------------------------------------

CTX = 'workspace: Test\ncampaign "Agences · Lyon": paused 3/50 qualified'


@pytest.mark.parametrize(
    ("text", "tool", "args"),
    [
        ("Trouve des agences marketing", "ask_clarifications", {"request": "Trouve des agences marketing"}),
        ("Find 200 marketing agencies in Lyon, 2-30 employees, founders, safe email", "plan_campaign", None),
        ("Trouve 50 agences marketing à Lyon, vas-y", "create_campaign", None),
        ("pause", "pause_campaign", {}),
        ("arrête la recherche", "pause_campaign", {}),
        ("stop", "pause_campaign", {}),
        ("annule la campagne", "cancel_campaign", {}),
        ("reprends", "resume_campaign", {}),
        ("reprends en ajoutant Marseille", "amend_campaign", {"instruction": "ajoutant Marseille", "resume": True}),
        ("ajoute aussi Marseille", "amend_campaign", {"instruction": "ajoute aussi Marseille", "resume": True}),
        ("+200 leads", "amend_campaign", {"instruction": "+200 leads", "resume": True}),
        ("Only keep SAFE emails", "filter_table", None),
    ],
)
def test_router(text, tool, args):
    calls, _reply = local_router.route(text, CTX)
    assert calls and calls[0].name == tool
    if args is not None:
        assert calls[0].args == args


def test_router_intro_text_matches_language():
    _calls, reply = local_router.route("Trouve des agences marketing", CTX)
    assert reply == "3 précisions rapides avant de lancer la recherche :"
    _calls, reply = local_router.route("Find marketing agencies", CTX)
    assert reply == "3 quick questions before I start the search:"
