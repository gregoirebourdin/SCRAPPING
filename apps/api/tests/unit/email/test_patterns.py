"""Name normalization, rendering, inference and priors."""

from __future__ import annotations

import pytest

from scout.email.patterns import (
    GLOBAL_PRIORS,
    PATTERNS,
    infer_from_local_parts,
    infer_pattern,
    infer_patterns,
    name_parts,
    pattern_confidence,
    prior_for,
    ranked_priors,
    render,
)


def test_vocabulary():
    assert PATTERNS[0] == "{first}.{last}"
    assert set(GLOBAL_PRIORS) == set(PATTERNS)
    assert sum(GLOBAL_PRIORS.values()) == pytest.approx(1.0, abs=0.001)


def test_name_parts_accents_and_case():
    p = name_parts("Éloïse", "LEFÈVRE")
    assert p.first == ("eloise",) and p.last == ("lefevre",) and p.f == ("e",) and p.l == ("l",)


def test_name_parts_compound_first_name():
    p = name_parts("Jean-Pierre", "Dupont")
    assert p.first == ("jean-pierre", "jeanpierre")
    assert p.f == ("j", "jp")


def test_name_parts_spaced_and_registry_first_names():
    assert name_parts("Jean Pierre", "Dupont").first == ("jean", "jean-pierre", "jeanpierre")
    assert name_parts("Jean Pierre Marie", "Dupont").first == ("jean",)


def test_name_parts_particles():
    assert name_parts("Marie", "de la Fontaine").last == ("delafontaine", "fontaine")
    assert name_parts("Marie", "Le Gall").last == ("legall", "gall")
    assert name_parts("Ana", "van der Berg").last == ("vanderberg", "berg")
    # A last name that *is* a particle stays as is.
    assert name_parts("Marco", "Da").last == ("da",)


def test_name_parts_apostrophes():
    assert name_parts("Marie", "d'Alembert").last == ("dalembert", "alembert")
    assert name_parts("Seán", "O’Brien").last == ("obrien",)
    assert name_parts("D'Arcy", "Smith").first == ("darcy",)


def test_name_parts_compound_last_name():
    assert name_parts("Marie", "Martin-Durand").last == ("martin-durand", "martindurand", "martin")
    assert name_parts("Ana", "Garcia Lopez").last == ("garcialopez", "garcia-lopez", "garcia")


def test_render_basic_patterns():
    expected = {
        "{first}.{last}": "marie.dupont",
        "{first}": "marie",
        "{f}{last}": "mdupont",
        "{first}{last}": "mariedupont",
        "{f}.{last}": "m.dupont",
        "{last}.{first}": "dupont.marie",
        "{first}_{last}": "marie_dupont",
        "{first}-{last}": "marie-dupont",
        "{last}": "dupont",
        "{first}{l}": "maried",
        "{last}{f}": "dupontm",
        "{f}{l}": "md",
        "{first}.{l}": "marie.d",
    }
    for pattern, local in expected.items():
        assert render(pattern, "Marie", "Dupont") == [local]


def test_render_variants_for_compounds_primary_first():
    out = render("{first}.{last}", "Jean-Pierre", "de la Fontaine")
    assert out[0] == "jean-pierre.delafontaine"
    assert set(out) == {
        "jean-pierre.delafontaine",
        "jean-pierre.fontaine",
        "jeanpierre.delafontaine",
        "jeanpierre.fontaine",
    }
    assert render("{f}{last}", "Jean-Pierre", "Dupont") == ["jdupont", "jpdupont"]


def test_render_missing_parts():
    assert render("{first}.{last}", "Zoé", None) == []
    assert render("{first}", "Zoé", None) == ["zoe"]
    assert render("{last}", None, "Dupont") == ["dupont"]
    assert render("{nonsense}", "Marie", "Dupont") == []


@pytest.mark.parametrize(
    ("first", "last", "local", "pattern"),
    [
        ("Marie", "Dupont", "marie.dupont", "{first}.{last}"),
        ("Marie", "Dupont", "Marie.Dupont+news", "{first}.{last}"),
        ("Marie", "Dupont", "marie", "{first}"),
        ("Marie", "Dupont", "mdupont", "{f}{last}"),
        ("Marie", "Dupont", "dupontm", "{last}{f}"),
        ("Marie", "Dupont", "marie.d", "{first}.{l}"),
        ("Jean-Pierre", "Dupont", "jp.dupont", "{f}.{last}"),
        ("Jean-Pierre", "Dupont", "jeanpierre.dupont", "{first}.{last}"),
        ("Jean-Pierre", "Dupont", "jean-pierre", "{first}"),
        ("Marie", "de la Fontaine", "marie.fontaine", "{first}.{last}"),
        ("Marie", "de la Fontaine", "mdelafontaine", "{f}{last}"),
        ("Éloïse", "Lefèvre", "eloise.lefevre", "{first}.{last}"),
        ("Marie", "Dupont", "contact", None),
        ("Marie", "Dupont", "jean.martin", None),
    ],
)
def test_infer_pattern(first, last, local, pattern):
    assert infer_pattern(first, last, local) == pattern


def test_infer_patterns_counts():
    samples = [
        ("Marie", "Dupont", "marie.dupont"),
        ("Jean", "Martin", "jean.martin"),
        ("Paul", "Durand", "pdurand"),
        ("Zoé", "Roux", "contact"),
    ]
    assert infer_patterns(samples) == {"{first}.{last}": 2, "{f}{last}": 1}


def test_pattern_confidence_curve():
    assert pattern_confidence(0) == 0.0
    assert pattern_confidence(1) == 0.7
    assert pattern_confidence(2) == 0.85
    assert pattern_confidence(50) == 0.97
    assert pattern_confidence(1, successes=1) == 0.85
    assert pattern_confidence(2, failures=1) < pattern_confidence(2)
    assert pattern_confidence(0, failures=3) == 0.0


def test_infer_from_local_parts_shapes():
    out = infer_from_local_parts(["marie.dupont", "jean.martin", "contact", "info", "paul.durand"])
    pattern, conf, count = out[0]
    assert pattern == "{first}.{last}" and count == 3
    assert conf <= 0.75  # shapes cannot distinguish first/last order
    assert infer_from_local_parts(["contact", "info", "user123"]) == []
    assert infer_from_local_parts(["j.dupont"])[0][0] == "{f}.{last}"


def test_priors_reflect_company_size_and_country():
    micro_fr = ranked_priors(company_size_max=5, country="FR")
    assert micro_fr[0][0] == "{first}"
    large = ranked_priors(company_size_max=5000, country="FR")
    assert large[0][0] == "{first}.{last}"
    assert prior_for("{first}", company_size_max=5000, country="FR") < 0.1
    us = dict(ranked_priors(company_size_max=500, country="us"))
    assert us["{f}{last}"] > dict(ranked_priors(company_size_max=500, country="FR"))["{f}{last}"]
    for ctx in (micro_fr, large, ranked_priors()):
        assert sum(v for _, v in ctx) == pytest.approx(1.0, abs=0.01)
    assert prior_for("{unknown}") == 0.0


def test_titles_and_suffixes_are_not_part_of_names() -> None:
    # email-enrich comparison: "Dr Jean" must not yield "dr-jean@", "Smith Jr." must not yield "smithjr@"
    assert name_parts("Dr Jean", "Dupont").first == ("jean",)
    assert name_parts("Mme Marie", "Curie PhD").last == ("curie",)
    assert name_parts("John", "Smith Jr.").last == ("smith",)
    assert name_parts("M.", "Dupont").first == ("m",)  # a lone initial is kept


def test_last_dot_initial_pattern() -> None:
    assert render("{last}.{f}", "John", "Smith") == ["smith.j"]
    assert infer_pattern("John", "Smith", "smith.j") == "{last}.{f}"
    shapes = dict((p, c) for p, c, _ in infer_from_local_parts(["smith.j", "doe.a", "martin.p"]))
    assert set(shapes) == {"{first}.{l}", "{last}.{f}"}  # order unknown without names
