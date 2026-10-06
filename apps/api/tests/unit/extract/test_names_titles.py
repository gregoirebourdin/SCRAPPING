"""Person-name plausibility / splitting and multilingual title normalization."""

from __future__ import annotations

import pytest

from scout.db.enums import RoleFamily, Seniority
from scout.extract.names import FIRST_NAMES, is_known_first_name, is_plausible_person_name, split_name
from scout.extract.titles import is_job_title, normalize_title, title_match_score


def test_gazetteer_size_and_accents():
    assert len(FIRST_NAMES) >= 600
    for name in (
        "Zoé",
        "Inès",
        "Hélène",
        "Jean-Pierre",
        "Jürgen",
        "José",
        "Fatima",
        "Giulia",
        "João",
        "Mohammed",
    ):
        assert is_known_first_name(name), name


@pytest.mark.parametrize(
    "name",
    [
        "Jean Dupont",
        "DUPONT Jean",
        "Marie-Claire Martin",
        "Jean de La Fontaine",
        "Zoé Lefèvre-Durand",
        "Thomas Petit",
        "Thomas Müller",
        "John F. Kennedy",
        "Ana María García",
        "Kofi Annan",
        "O'Brien Sean",
        "Jean-Marc O'Neil",
    ],
)
def test_plausible_names(name):
    assert is_plausible_person_name(name)


@pytest.mark.parametrize(
    "text",
    [
        "Directeur Général",
        "Chef de projet",
        "Agence Lumière",
        "En savoir plus",
        "Nos Services",
        "Office Manager",
        "Mentions Légales",
        "Notre Équipe",
        "Bonjour Paris",
        "jean dupont",
        "Jean",
        "Jean D.",
        "SEO SEA",
        "Studio Pixel",
        "Politique de confidentialité",
        "Contact 01 23 45 67 89",
        "Jean Dupont Marie Martin Paul Durand",
        "Boulangerie Martin",
        "Cookies Settings",
        "Lire la suite",
    ],
)
def test_implausible_names(text):
    assert not is_plausible_person_name(text)


def test_require_known_first_name():
    assert is_plausible_person_name("Kofi Annan")
    assert not is_plausible_person_name("Kofi Annan", require_known_first_name=True)
    assert is_plausible_person_name("Claire Fontaine", require_known_first_name=True)


@pytest.mark.parametrize(
    ("full", "expected"),
    [
        ("Jean Dupont", ("Jean", "Dupont")),
        ("DUPONT Jean", ("Jean", "Dupont")),
        ("DUPONT Jean Pierre Marie", ("Jean", "Dupont")),
        ("Dupont, Jean", ("Jean", "Dupont")),
        ("Jean-Pierre de La Fontaine", ("Jean-Pierre", "de La Fontaine")),
        ("Ludwig van Beethoven", ("Ludwig", "van Beethoven")),
        ("Anne Marie Durand", ("Anne", "Durand")),
        ("John F. Kennedy", ("John", "Kennedy")),
        ("JEAN-PIERRE MARTIN", ("Jean-Pierre", "Martin")),
        ("Madonna", ("Madonna", None)),
    ],
)
def test_split_name(full, expected):
    assert split_name(full) == expected


@pytest.mark.parametrize(
    ("title", "canonical", "family", "seniority", "min_power"),
    [
        ("Gérant", "Managing Director", RoleFamily.executive, Seniority.owner, 90),
        ("Gérante", "Managing Director", RoleFamily.executive, Seniority.owner, 90),
        ("Président", "President", RoleFamily.executive, Seniority.c_level, 90),
        ("Présidente", "President", RoleFamily.executive, Seniority.c_level, 90),
        ("PDG", "Chief Executive Officer", RoleFamily.executive, Seniority.c_level, 90),
        ("CEO", "Chief Executive Officer", RoleFamily.executive, Seniority.c_level, 90),
        ("Fondateur", "Founder", RoleFamily.founder, Seniority.owner, 90),
        ("Co-fondatrice", "Co-Founder", RoleFamily.founder, Seniority.owner, 90),
        ("Cofounder & CTO", "Co-Founder", RoleFamily.founder, Seniority.owner, 90),
        ("Geschäftsführer", "Managing Director", RoleFamily.executive, Seniority.c_level, 90),
        ("Inhaberin", "Owner", RoleFamily.founder, Seniority.owner, 90),
        ("Propriétaire", "Owner", RoleFamily.founder, Seniority.owner, 90),
        ("Directeur Général", "Managing Director", RoleFamily.executive, Seniority.c_level, 90),
        ("Managing Partner", "Managing Partner", RoleFamily.executive, Seniority.owner, 90),
        ("Associé", "Partner", RoleFamily.executive, Seniority.owner, 80),
        ("CMO", "Chief Marketing Officer", RoleFamily.marketing, Seniority.c_level, 85),
        ("Head of Growth", "Head of Growth", RoleFamily.marketing, Seniority.head, 60),
        ("Head of Marketing", "Head of Marketing", RoleFamily.marketing, Seniority.head, 60),
        ("Responsable marketing", "Head of Marketing", RoleFamily.marketing, Seniority.head, 60),
        ("Directrice marketing", "Marketing Director", RoleFamily.marketing, Seniority.director, 70),
        ("Directeur commercial", "Sales Director", RoleFamily.sales, Seniority.director, 70),
        ("Directrice artistique", "Art Director", RoleFamily.creative, Seniority.director, 70),
        ("Creative Director", "Creative Director", RoleFamily.creative, Seniority.director, 70),
        ("Chef de projet", "Project Manager", RoleFamily.operations, Seniority.manager, 40),
        ("Account manager", "Account Manager", RoleFamily.sales, Seniority.manager, 40),
        ("Community manager", "Community Manager", RoleFamily.marketing, Seniority.manager, 20),
        ("Social media manager", "Social Media Manager", RoleFamily.marketing, Seniority.manager, 20),
        ("Office manager", "Office Manager", RoleFamily.operations, Seniority.manager, 20),
        ("DRH", "HR Director", RoleFamily.hr, Seniority.director, 70),
        ("RH", "Human Resources", RoleFamily.hr, Seniority.unknown, 10),
        ("Stagiaire marketing", "Intern", RoleFamily.marketing, Seniority.entry, 0),
    ],
)
def test_normalize_title(title, canonical, family, seniority, min_power):
    info = normalize_title(title)
    assert info.original == title
    assert info.normalized_title == canonical
    assert info.role_family == family
    assert info.seniority == seniority
    assert info.decision_power >= min_power


def test_decision_power_ordering_and_modifiers():
    assert normalize_title("CEO").decision_power > normalize_title("CMO").decision_power
    assert normalize_title("CMO").decision_power > normalize_title("Marketing Director").decision_power
    assert (
        normalize_title("Marketing Director").decision_power
        > normalize_title("Head of Marketing").decision_power
    )
    assert (
        normalize_title("Head of Marketing").decision_power
        > normalize_title("Marketing manager").decision_power
    )
    assert normalize_title("Marketing manager").decision_power > normalize_title("Graphiste").decision_power
    deputy = normalize_title("Directeur général adjoint")
    assert deputy.normalized_title.startswith("Deputy") and deputy.decision_power < 90
    unknown = normalize_title("Chief Happiness Wizard of Unicorns")
    assert unknown.original == "Chief Happiness Wizard of Unicorns"


def test_title_match_founder_ceo_owner():
    req = {"titles": ["Founder/CEO/Owner"], "role_families": [], "seniorities": []}
    for accepted in (
        "Gérant",
        "Président",
        "PDG",
        "Fondateur",
        "Managing Director",
        "Co-founder",
        "Co-fondatrice",
        "Geschäftsführer",
        "Inhaber",
        "Propriétaire",
        "Directeur Général",
    ):
        assert title_match_score(normalize_title(accepted), **req) >= 0.9, accepted
    for rejected in (
        "Community manager",
        "Chef de projet",
        "Développeur web",
        "Office manager",
        "Stagiaire",
        "Directrice artistique",
        "Head of Growth",
    ):
        assert title_match_score(normalize_title(rejected), **req) < 0.5, rejected


def test_title_match_families_and_similar_roles():
    info = normalize_title("Head of Growth")
    assert (
        title_match_score(info, titles=[], role_families=["marketing"], seniorities=["head", "director"])
        >= 0.85
    )
    assert title_match_score(info, titles=["Head of Growth"], role_families=[], seniorities=[]) == 1.0
    assert title_match_score(info, titles=["Marketing Director"], role_families=[], seniorities=[]) >= 0.6
    assert (
        title_match_score(
            normalize_title("Community manager"),
            titles=["Marketing Director"],
            role_families=[],
            seniorities=[],
        )
        < 0.5
    )
    assert title_match_score(info, titles=[], role_families=[], seniorities=[]) == 1.0


def test_is_job_title():
    assert is_job_title("Co-fondatrice & Directrice générale")
    assert is_job_title("Community manager")
    assert not is_job_title("Claire Fontaine")
    assert not is_job_title(
        "Nous accompagnons les marques depuis 2012 avec passion et exigence au quotidien."
    )
