"""Pure helpers of the profile builder: website items, name attachment, samples prep, state."""

from __future__ import annotations

from datetime import UTC, datetime

from scout.db.enums import EmailEvidenceSource as Src
from scout.email.contracts import DomainIntel, ObservedEmail
from scout.email.intel import state
from scout.email.intel.profile import match_person, person_names, website_items
from scout.email.intel.samples import prepare_samples, unique_by_address

NOW = datetime(2026, 10, 1, tzinfo=UTC)
PEOPLE = [("Marie", "Dupont"), ("Marie", "Curie"), ("Jean", "Martin"), ("Paul", None)]


def test_person_names_mirrors_the_finder_rule():
    assert person_names("Marie", "Dupont", "x") == ("Marie", "Dupont")
    assert person_names(None, None, "Jean de La Fontaine") == ("Jean", "de La Fontaine")
    assert person_names("Paul", None, "Paul") == ("Paul", None)


def test_match_person_unique_and_unambiguous():
    assert match_person("marie.dupont", PEOPLE) == ("Marie", "Dupont")
    assert match_person("jmartin", PEOPLE) == ("Jean", "Martin")
    assert match_person("jean", PEOPLE) == ("Jean", "Martin")
    assert match_person("marie", PEOPLE) is None  # two Maries: ambiguous
    assert match_person("mc", PEOPLE) is None  # {f}{l} is too weak
    assert match_person("contact", PEOPLE) is None
    assert match_person("paul", PEOPLE) is None  # no last name on record → never invented


def test_website_items_on_domain_only_and_named_from_people():
    pages = [
        (
            "https://acme.fr/equipe",
            [
                "Marie.Dupont@acme.fr",
                {"address": "contact@acme.fr"},
                {"email": "jm@acme.fr"},
                "someone@gmail.com",
                "info@partner.com",
                {"value": "jean.martin@paris.acme.fr"},
                42,
            ],
            NOW,
        ),
        ("https://acme.fr/contact", ["marie.dupont@acme.fr", "not-an-email"], NOW),
    ]
    items = {o.address: o for o in website_items("acme.fr", pages, PEOPLE)}
    assert set(items) == {
        "marie.dupont@acme.fr",
        "contact@acme.fr",
        "jm@acme.fr",
        "jean.martin@paris.acme.fr",
    }
    marie = items["marie.dupont@acme.fr"]
    assert (marie.first_name, marie.last_name, marie.source, marie.source_url) == (
        "Marie",
        "Dupont",
        Src.website,
        "https://acme.fr/equipe",
    )
    assert items["contact@acme.fr"].is_role and items["contact@acme.fr"].first_name is None
    assert items["jm@acme.fr"].first_name is None  # {f}{l} never names an address
    assert items["jean.martin@paris.acme.fr"].last_name == "Martin"
    assert marie.observed_at == NOW


def test_prepare_samples_filters_and_infers():
    items = [
        ObservedEmail("Marie.Dupont@acme.fr", "marie.dupont", Src.website, "Marie", "Dupont"),
        ObservedEmail("marie.dupont@acme.fr", "marie.dupont", Src.website, confidence=0.95),  # same key
        ObservedEmail("contact@acme.fr", "contact", Src.website, "Marie", "Dupont"),
        ObservedEmail("paul@gmail.com", "paul", Src.website, "Paul", "Durand"),
        ObservedEmail("x@other.fr", "x", Src.website),
        ObservedEmail("bad", "bad", Src.website),
        ObservedEmail("jm@acme.fr", "jm", Src.github, "Jean", "Martin"),
    ]
    rows = {
        (r["address"], r["source"]): r for r in prepare_samples("acme.fr", items, workspace_id=None, now=NOW)
    }
    assert set(rows) == {
        ("marie.dupont@acme.fr", Src.website),
        ("contact@acme.fr", Src.website),
        ("jm@acme.fr", Src.github),
    }
    marie = rows[("marie.dupont@acme.fr", Src.website)]
    assert (
        marie["pattern"] == "{first}.{last}"
        and marie["confidence"] == 0.95
        and marie["first_name"] == "Marie"
    )
    contact = rows[("contact@acme.fr", Src.website)]
    assert contact["is_role"] and contact["pattern"] is None
    assert rows[("jm@acme.fr", Src.github)]["pattern"] == "{f}{l}"  # stored, but learning ignores it


def test_unique_by_address_prefers_reusable_sources():
    gh = ObservedEmail("a@acme.fr", "a", Src.github, "Anne", "A")
    web = ObservedEmail("a@acme.fr", "a", Src.website)
    rd = ObservedEmail("b@acme.fr", "b", Src.rdap)
    out = unique_by_address([gh, rd, web])
    assert [(o.address, o.source) for o in out] == [("a@acme.fr", Src.website), ("b@acme.fr", Src.rdap)]


def test_state_cache_flags_and_network_switch():
    state.reset()
    intel = DomainIntel(domain="acme.fr", mx_hosts=["mx.acme.fr"])
    state.cache_put(("acme.fr", None), intel)
    got = state.cache_get(("acme.fr", None))
    assert got is not None and got is not intel and got.mx_hosts == ["mx.acme.fr"]
    got.mx_hosts.append("mutated")
    assert state.cache_get(("acme.fr", None)).mx_hosts == ["mx.acme.fr"]  # cached copy is isolated
    state.invalidate("acme.fr")
    assert state.cache_get(("acme.fr", None)) is None
    state.cache_put(("acme.fr", None), intel, ttl=-1)
    assert state.cache_get(("acme.fr", None)) is None  # expired

    stats = state.add_flag({"builds": 1}, state.MX_CHANGED, {"from": "a", "to": "b"}, NOW)
    assert stats["builds"] == 1 and stats["invalidated_at"] == NOW.isoformat()
    assert stats["flags"][0]["flag"] == "MX_CHANGED"
    ev = state.flag_evidence(stats)
    assert ev[0]["signal"] == "MX_CHANGED" and ev[0]["value"] == {"from": "a", "to": "b"}
    for _ in range(30):
        stats = state.add_flag(stats, state.PATTERN_CHANGED, {}, NOW)
    assert len(stats["flags"]) == state.MAX_FLAGS

    state.count_hit("acme.fr")
    state.count_hit("acme.fr")
    assert state.take_hits("acme.fr") == 2 and state.take_hits("acme.fr") == 0

    assert state.network_evidence_enabled() is False  # APP_ENV=test: no internet by default
    state.set_network_evidence(True)
    assert state.network_evidence_enabled() is True
    state.set_network_evidence(None)
    state.reset()


def test_facts_from_probe():
    from scout.db.enums import SmtpHealthState, SmtpResult
    from scout.email.contracts import DomainProbeResult, RcptVerdict, SessionOutcome
    from scout.email.intel.profile import facts_from_probe

    ok = DomainProbeResult(
        domain="acme.fr", session=SessionOutcome.ok, catch_all=False, catch_all_confidence=0.9
    )
    assert facts_from_probe(ok) == {
        "smtp_last_result": "ok",
        "smtp_reachable": True,
        "catch_all": False,
        "catch_all_confidence": 0.9,
        "catch_all_method": "smtp_random_probes:builtin",
    }
    grey = DomainProbeResult(
        domain="acme.fr",
        session=SessionOutcome.temporary,
        verdicts={
            "a@acme.fr": RcptVerdict("a@acme.fr", SmtpResult.temporary, 451, "4.7.1 Greylisted, try later")
        },
    )
    assert facts_from_probe(grey) == {
        "smtp_last_result": "temporary",
        "smtp_reachable": True,
        "greylisting_seen": True,
    }
    down = DomainProbeResult(domain="acme.fr", session=SessionOutcome.infra_failure)
    assert facts_from_probe(down, SmtpHealthState.HEALTHY)["smtp_reachable"] is False
    assert "smtp_reachable" not in facts_from_probe(down, SmtpHealthState.DEGRADED)  # maybe our side
    assert "smtp_reachable" not in facts_from_probe(DomainProbeResult("acme.fr", SessionOutcome.policy_block))
    assert facts_from_probe(DomainProbeResult("acme.fr", SessionOutcome.not_attempted)) == {}
