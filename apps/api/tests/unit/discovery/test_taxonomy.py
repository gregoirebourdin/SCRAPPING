"""Industry taxonomy: coverage, NAF consistency and deterministic multilingual matching."""

from __future__ import annotations

import pytest

from scout.discovery.naf import NAF_LABELS, naf_category
from scout.discovery.taxonomy import LANGS, PROFILES, get_profile, looks_french, match_industries


def keys(text: str) -> list[str]:
    return [p.key for p in match_industries(text)]


def test_coverage_and_shape() -> None:
    assert len(PROFILES) >= 45
    assert len({p.key for p in PROFILES}) == len(PROFILES)
    for p in PROFILES:
        assert set(LANGS) <= set(p.labels), p.key
        assert all(p.labels[lang] for lang in LANGS), p.key
        assert p.naf_codes, p.key
        for code in p.all_naf(adjacent=True):
            assert code in NAF_LABELS
        assert p.maps("fr") and p.keywords("de")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("agences marketing", "marketing_agency"),
        ("marketing agencies", "marketing_agency"),
        ("Agence de communication", "communication_agency"),
        ("SaaS startups", "software_saas"),
        ("dentistes à Lyon", "dentist"),
        ("Dentists in London", "dentist"),
        ("cabinets d'avocats", "law_firm"),
        ("experts-comptables", "accounting_firm"),
        ("agences immobilières", "real_estate_agency"),
        ("social media marketing agencies", "social_media_agency"),
        ("Webagenturen", "web_agency"),
        ("agencias de marketing digital", "marketing_agency"),
        ("agenzie di comunicazione", "communication_agency"),
        ("plombiers", "plumber"),
        ("kinés", "physiotherapist"),
        ("salons de coiffure", "hair_salon"),
        ("AI startups", "ai_company"),
        ("coffee shops", "cafe_bar"),
        ("e-commerce brands", "ecommerce_brand"),
        ("wedding planners", "wedding_planner"),
    ],
)
def test_match_best_first(text: str, expected: str) -> None:
    assert keys(text)[0] == expected


def test_specific_beats_generic() -> None:
    ks = keys("social media marketing agency")
    assert ks.index("social_media_agency") < ks.index("marketing_agency")
    assert keys("car repair shop")[0] == "car_repair"


def test_short_synonyms_need_exact_tokens() -> None:
    assert "spa" not in keys("software in Spain")
    assert "cafe_bar" not in keys("barcelona startups")
    assert keys("bars in Barcelona")[0] == "cafe_bar"


def test_no_match_returns_empty() -> None:
    assert match_industries("") == []
    assert match_industries("quantum blorpification") == []


def test_profile_lookup_and_flags() -> None:
    dentist = get_profile("dentist")
    assert dentist is not None and dentist.local_business and not dentist.digital
    assert "86.23Z" in dentist.naf_codes and ("amenity", "dentist") in dentist.osm_tags
    saas = get_profile("software_saas")
    assert saas is not None and saas.digital and "SaaS" in saas.yc_industries
    assert get_profile("marketing_agency").yc_industries == []  # agencies are not YC startups


def test_naf_category_and_language_hint() -> None:
    assert naf_category("73.11Z") == "73.11Z Activités des agences de publicité"
    assert naf_category("99.99Z") == "99.99Z"
    assert looks_french("agences marketing à Lyon")
    assert not looks_french("marketing agencies in Boston")


def test_sports_coaches_are_personal_trainers_not_business_coaching() -> None:
    assert keys("coachs sportifs a annecy")[0] == "personal_trainer"
    assert "coaching" not in keys("coach sportif à domicile")
    assert keys("coach professionnel")[0] == "coaching"
