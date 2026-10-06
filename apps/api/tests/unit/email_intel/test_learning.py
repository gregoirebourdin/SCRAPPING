"""Pattern learning v2 math (pure): weights, recency, ambiguity, exclusions, SMTP counters, posterior."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from scout.db.enums import EmailEvidenceSource as Src
from scout.email.contracts import ObservedEmail
from scout.email.intel import learning
from scout.email.intel.learning import (
    HALF_LIFE_DAYS,
    learn,
    posterior,
    recency_weight,
    sample_pattern,
    sample_weight,
    wilson_lower_bound,
)
from scout.email.patterns import PATTERNS, infer_pattern, ranked_priors

NOW = datetime(2026, 10, 1, tzinfo=UTC)
DOMAIN = "acme.fr"
PEOPLE = [
    ("Marie", "Dupont"),
    ("Jean", "Martin"),
    ("Paul", "Durand"),
    ("Léa", "Petit"),
    ("Hugo", "Bernard"),
    ("Chloé", "Thomas"),
    ("Louis", "Robert"),
    ("Emma", "Richard"),
    ("Nathan", "Moreau"),
    ("Inès", "Laurent"),
]


def obs(
    first: str | None,
    last: str | None,
    local: str,
    *,
    source: Src = Src.website,
    at: datetime | None = NOW,
    conf: float = 0.9,
    role: bool = False,
    domain: str = DOMAIN,
) -> ObservedEmail:
    return ObservedEmail(
        address=f"{local}@{domain}",
        local_part=local,
        source=source,
        first_name=first,
        last_name=last,
        is_role=role,
        confidence=conf,
        observed_at=at,
    )


def render_first_last(first: str, last: str) -> str:
    from scout.email.patterns import render

    return render("{first}.{last}", first, last)[0]


def by_pattern(stats):
    return {s.pattern: s for s in stats}


def test_eight_first_last_plus_one_flast_is_dominant():
    samples = [obs(f, last, render_first_last(f, last)) for f, last in PEOPLE[:8]]
    samples.append(obs("Nathan", "Moreau", "nmoreau"))
    stats = learn(DOMAIN, samples, now=NOW)
    top = stats[0]
    assert top.pattern == "{first}.{last}" and top.samples == 8
    assert top.confidence >= 0.88 and top.confidence < 0.97  # ≈ 0.9, never 100 %
    assert top.share == pytest.approx(8 / 9, abs=0.001)
    flast = by_pattern(stats)["{f}{last}"]
    assert flast.samples == 1 and flast.share == pytest.approx(1 / 9, abs=0.001) and flast.confidence < 0.1
    assert top.last_confirmed_at == NOW


def test_single_sample_is_never_certain_and_more_samples_help():
    one = learn(DOMAIN, [obs("Marie", "Dupont", "marie.dupont")], now=NOW)[0]
    two = learn(
        DOMAIN, [obs("Marie", "Dupont", "marie.dupont"), obs("Jean", "Martin", "jean.martin")], now=NOW
    )[0]
    eight = learn(DOMAIN, [obs(f, last, render_first_last(f, last)) for f, last in PEOPLE[:8]], now=NOW)[0]
    assert 0.75 < one.confidence < 0.9
    assert one.confidence < two.confidence < eight.confidence < 0.97
    assert two.confidence >= 0.85  # strong enough for the status rules (≥ 0.85 from ≥ 2 samples)


def test_recency_weight_half_life():
    assert recency_weight(NOW, now=NOW) == 1.0
    assert recency_weight(None, now=NOW) == 1.0
    assert recency_weight(NOW + timedelta(days=3), now=NOW) == 1.0  # clock skew
    assert recency_weight(NOW - timedelta(days=HALF_LIFE_DAYS), now=NOW) == pytest.approx(0.5)
    assert recency_weight(NOW - timedelta(days=2 * HALF_LIFE_DAYS), now=NOW) == pytest.approx(0.25)
    assert recency_weight(NOW - timedelta(days=365 * 20), now=NOW) == learning.MIN_RECENCY_WEIGHT


def test_recent_convention_beats_older_one():
    four_years = NOW - timedelta(days=4 * 365)
    old = [obs(f, last, f[0].lower() + last.lower(), at=four_years) for f, last in PEOPLE[:3]]
    new = [obs(f, last, render_first_last(f, last)) for f, last in PEOPLE[3:5]]
    stats = learn(DOMAIN, old + new, now=NOW)
    assert stats[0].pattern == "{first}.{last}"
    # Same samples, all recent: the more frequent {f}{last} wins.
    fresh = [obs(f, last, f[0].lower() + last.lower()) for f, last in PEOPLE[:3]]
    assert learn(DOMAIN, fresh + new, now=NOW)[0].pattern == "{f}{last}"


def test_source_weights():
    base = obs("Marie", "Dupont", "marie.dupont")
    assert sample_weight(base, now=NOW) == pytest.approx(1.0)
    for source, expected in [
        (Src.user, 1.0),
        (Src.smtp_verified, 1.0),
        (Src.import_, 0.9),
        (Src.github, 0.7),
        (Src.search, 0.6),
        (Src.rdap, 0.5),
    ]:
        assert sample_weight(obs("Marie", "Dupont", "marie.dupont", source=source), now=NOW) == pytest.approx(
            expected
        )
    # Sample confidence scales below the default 0.9, never above 1.
    assert sample_weight(obs("Marie", "Dupont", "marie.dupont", conf=0.45), now=NOW) == pytest.approx(0.5)
    assert sample_weight(obs("Marie", "Dupont", "marie.dupont", conf=1.0), now=NOW) == pytest.approx(1.0)

    website = learn(DOMAIN, [base], now=NOW)[0]
    github = learn(DOMAIN, [obs("Marie", "Dupont", "marie.dupont", source=Src.github)], now=NOW)[0]
    rdap = learn(DOMAIN, [obs("Marie", "Dupont", "marie.dupont", source=Src.rdap)], now=NOW)[0]
    assert website.confidence > github.confidence > rdap.confidence
    # Two website samples outweigh two GitHub commits of another convention.
    mixed = [
        obs("Marie", "Dupont", "marie.dupont"),
        obs("Jean", "Martin", "jean.martin"),
        obs("Paul", "Durand", "pdurand", source=Src.github),
        obs("Léa", "Petit", "lpetit", source=Src.github),
    ]
    assert learn(DOMAIN, mixed, now=NOW)[0].pattern == "{first}.{last}"


def test_address_seen_from_several_sources_counts_once():
    samples = [
        obs("Marie", "Dupont", "marie.dupont"),
        obs("Marie", "Dupont", "marie.dupont", source=Src.github),
        obs("Marie", "Dupont", "marie.dupont", source=Src.rdap),
    ]
    top = learn(DOMAIN, samples, now=NOW)[0]
    assert top.samples == 1
    assert top.confidence == learn(DOMAIN, samples[:1], now=NOW)[0].confidence


def test_ambiguity_follows_infer_pattern():
    cases = [
        ("Paul", "Martin", "paul"),
        ("Paul", "Martin", "martin"),
        ("Paul", "Martin", "pmartin"),
        ("Jean-Pierre", "Dupont", "jp.dupont"),
        ("Jean Pierre", "Dupont", "jean.dupont"),
        ("Marie", "de la Fontaine", "marie.delafontaine"),
        ("Marie", "de la Fontaine", "mfontaine"),
        ("Anne", "Anne", "anne"),  # first == last: first vocabulary match ({first}) wins
    ]
    for first, last, local in cases:
        expected = infer_pattern(first, last, local)
        assert expected is not None
        assert sample_pattern(obs(first, last, local)) == expected
    stats = learn(DOMAIN, [obs("Anne", "Anne", "anne")], now=NOW)
    assert [(s.pattern, s.samples) for s in stats] == [("{first}", 1)]


def test_role_nameless_weak_and_off_domain_samples_teach_nothing():
    ignored = [
        obs("Marie", "Dupont", "contact"),  # role local part
        obs("Marie", "Dupont", "marie.dupont", role=True),  # flagged role
        obs(None, None, "jean.martin"),  # nameless
        obs("Marie", "Dupont", "md"),  # {f}{l}: too weak
        obs("Marie", "Dupont", "dupont.marie.paris"),  # names do not render it
        obs("Paul", "Durand", "paul.durand", domain="other.fr"),  # off-domain
    ]
    for o in ignored[:5]:
        assert sample_pattern(o) is None
    assert learn(DOMAIN, ignored, now=NOW) == []
    # Subdomain addresses count for the parent domain.
    sub = learn(DOMAIN, [obs("Paul", "Durand", "paul.durand", domain="paris.acme.fr")], now=NOW)
    assert sub[0].pattern == "{first}.{last}"


def test_smtp_successes_and_failures():
    one = [obs("Marie", "Dupont", "mdupont")]
    plain = by_pattern(learn(DOMAIN, one, now=NOW))["{f}{last}"]
    confirmed = by_pattern(learn(DOMAIN, one, successes={"{f}{last}": 2}, now=NOW))["{f}{last}"]
    failed = by_pattern(learn(DOMAIN, one, failures={"{f}{last}": 1}, now=NOW))["{f}{last}"]
    assert confirmed.confidence > plain.confidence > failed.confidence
    assert confirmed.successes == 2 and failed.failures == 1
    assert failed.confidence < 0.5  # a rejected guess demotes a single-sample pattern below "known"
    twice = by_pattern(learn(DOMAIN, one, failures={"{f}{last}": 2}, now=NOW))["{f}{last}"]
    assert twice.confidence < failed.confidence

    # SMTP-only evidence still yields a pattern; failure-only patterns are hidden unless asked.
    only_success = learn(DOMAIN, [], successes={"{first}": 1}, now=NOW)
    assert [(s.pattern, s.samples, s.successes) for s in only_success] == [("{first}", 0, 1)]
    assert learn(DOMAIN, [], failures={"{last}": 3}, now=NOW) == []
    hidden = learn(DOMAIN, [], failures={"{last}": 3}, now=NOW, include_unsupported=True)
    assert hidden[0].pattern == "{last}" and hidden[0].share == 0 and hidden[0].failures == 3


def test_smtp_verified_samples_are_not_double_counted_with_success_counters():
    verified = [obs("Marie", "Dupont", "marie.dupont", source=Src.smtp_verified, conf=0.95)]
    both = learn(DOMAIN, verified, successes={"{first}.{last}": 1}, now=NOW)[0]
    sample_only = learn(DOMAIN, verified, now=NOW)[0]
    counter_only = learn(DOMAIN, [], successes={"{first}.{last}": 1}, now=NOW)[0]
    assert both.confidence == sample_only.confidence == counter_only.confidence
    more = learn(DOMAIN, verified, successes={"{first}.{last}": 3}, now=NOW)[0]
    assert more.confidence > both.confidence  # counters beyond the recorded samples still count


def test_legacy_counters_are_kept_without_double_counting():
    legacy = learn(DOMAIN, [], legacy={"{first}.{last}": 2}, now=NOW)[0]
    assert legacy.pattern == "{first}.{last}" and legacy.samples == 2
    real = [obs("Marie", "Dupont", "marie.dupont"), obs("Jean", "Martin", "jean.martin")]
    merged = learn(DOMAIN, real, legacy={"{first}.{last}": 2}, now=NOW)[0]
    assert merged.samples == 2 and merged.confidence == learn(DOMAIN, real, now=NOW)[0].confidence


def test_priors_follow_company_size_and_country():
    one_first = [obs("Marie", "Dupont", "marie")]
    small = learn(DOMAIN, one_first, company_size_max=5, country="FR", now=NOW)[0]
    large = learn(DOMAIN, one_first, company_size_max=5000, country="FR", now=NOW)[0]
    assert small.pattern == large.pattern == "{first}"
    assert small.confidence > large.confidence


def test_posterior_is_a_distribution():
    priors = dict(ranked_priors())
    for evidence, failures in [({}, {}), ({"{first}.{last}": 3.0, "{f}{last}": 1.0}, {"{first}": 2.0})]:
        conf = posterior(evidence, failures, priors)
        assert set(conf) == set(PATTERNS)
        assert sum(conf.values()) == pytest.approx(1.0, abs=1e-6)
        assert all(0 < v < 1 for v in conf.values())
    # Without evidence the posterior is (close to) the prior ranking.
    conf = posterior({}, {}, priors)
    assert max(conf, key=lambda p: conf[p]) == ranked_priors()[0][0]


def test_rank_and_lower_bound():
    stats = learn(
        DOMAIN,
        [obs(f, last, render_first_last(f, last)) for f, last in PEOPLE[:7]]
        + [obs("Hugo", "Bernard", "hugo")],
        now=NOW,
    )
    assert [s.pattern for s in stats][:2] == ["{first}.{last}", "{first}"]
    assert learning.dominant_lower_bound(stats) < stats[0].confidence
    assert learning.dominant_lower_bound([]) is None
    assert wilson_lower_bound(1.0, 1) < wilson_lower_bound(7 / 8, 8)  # 1/1 never outranks 7/8
    assert wilson_lower_bound(0.5, 0) == 0.0
    assert 0 < wilson_lower_bound(0.9, 10) < 0.9
