"""Candidate ranking: known → inferred → priors, caps and de-duplication."""

from __future__ import annotations

from scout.db.enums import EmailDiscoveryMethod as M
from scout.email.permutations import rank_candidates

DOMAIN = "agence-x.fr"


def _addresses(cands):
    return [c.address for c in cands]


def test_priors_only_large_company():
    out = rank_candidates("Marie", "Dupont", DOMAIN, company_size_max=5000, country="FR")
    assert out[0].address == "marie.dupont@agence-x.fr"
    assert all(c.method == M.permutation for c in out)
    assert len(out) == 6 and len(set(_addresses(out))) == 6
    assert out[0].pattern == "{first}.{last}"
    assert out[0].pattern_confidence > out[-1].pattern_confidence


def test_priors_micro_french_agency_prefers_first_name():
    out = rank_candidates("Marie", "Dupont", DOMAIN, company_size_max=5, country="FR", max_candidates=3)
    assert _addresses(out)[:2] == ["marie@agence-x.fr", "marie.dupont@agence-x.fr"]
    assert len(out) == 3


def test_known_pattern_first_with_stored_confidence():
    out = rank_candidates(
        "Marie",
        "Dupont",
        DOMAIN,
        known_patterns=[("{f}{last}", 0.92, 3)],
        company_size_max=5000,
        country="FR",
    )
    assert out[0].address == "mdupont@agence-x.fr"
    assert out[0].method == M.known_pattern
    assert out[0].pattern_confidence == 0.92
    assert out[0].supporting_samples == 3
    # Priors still provide fallbacks, without duplicating the known address.
    assert "marie.dupont@agence-x.fr" in _addresses(out)
    assert _addresses(out).count("mdupont@agence-x.fr") == 1


def test_weak_known_pattern_competes_with_priors_and_unknown_patterns_ignored():
    out = rank_candidates(
        "Marie",
        "Dupont",
        DOMAIN,
        known_patterns=[("{last}{f}", 0.2, 1), ("{bogus}", 0.99, 9)],
        company_size_max=5000,
        country="FR",
    )
    assert out[0].address == "marie.dupont@agence-x.fr"
    assert not any("bogus" in a for a in _addresses(out))


def test_inferred_from_named_samples_beats_priors():
    out = rank_candidates(
        "Marie",
        "Dupont",
        DOMAIN,
        observed_samples=[("Jean", "Martin", "martin.jean"), ("Paul", "Durand", "durand.paul")],
        company_size_max=5000,
        country="FR",
    )
    assert out[0].address == "dupont.marie@agence-x.fr"
    assert out[0].method == M.inferred_pattern
    assert out[0].pattern_confidence == 0.85 and out[0].supporting_samples == 2


def test_inferred_from_nameless_local_parts():
    out = rank_candidates(
        "Marie",
        "Dupont",
        DOMAIN,
        observed_local_parts=["j.martin", "p.durand", "contact"],
        company_size_max=5000,
    )
    assert out[0].address == "m.dupont@agence-x.fr"
    assert out[0].method == M.inferred_pattern
    assert out[0].supporting_samples == 0


def test_tier_order_known_then_inferred_then_priors():
    out = rank_candidates(
        "Marie",
        "Dupont",
        DOMAIN,
        known_patterns=[("{first}", 0.7, 1)],
        observed_samples=[("Jean", "Martin", "jmartin")],
        company_size_max=5000,
    )
    assert [c.method for c in out[:3]] == [M.known_pattern, M.inferred_pattern, M.permutation]
    assert _addresses(out)[:3] == ["marie@agence-x.fr", "mdupont@agence-x.fr", "marie.dupont@agence-x.fr"]


def test_compound_names_respect_cap_and_interleave():
    out = rank_candidates("Jean-Pierre", "de la Fontaine", DOMAIN, company_size_max=5000, max_candidates=6)
    addrs = _addresses(out)
    assert len(addrs) == 6 and len(set(addrs)) == 6
    assert addrs[0] == "jean-pierre.delafontaine@agence-x.fr"
    # Secondary spellings never crowd out the next most likely pattern entirely.
    assert any(c.pattern != "{first}.{last}" for c in out[:4])


def test_max_candidates_and_degenerate_inputs():
    assert len(rank_candidates("Marie", "Dupont", DOMAIN, max_candidates=2)) == 2
    assert rank_candidates("Marie", "Dupont", DOMAIN, max_candidates=0) == []
    assert rank_candidates("Marie", "Dupont", "not a domain") == []
    assert rank_candidates(None, None, DOMAIN) == []
    # First name only → only patterns that need no last name.
    assert _addresses(rank_candidates("Marie", None, DOMAIN)) == ["marie@agence-x.fr"]


def test_domain_is_normalized():
    out = rank_candidates("Marie", "Dupont", "Agence-X.FR.", max_candidates=1)
    assert out[0].address.endswith("@agence-x.fr")
