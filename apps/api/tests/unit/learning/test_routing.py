"""Priors, min-evidence estimates, ranking with seeded exploration, confidence blending (pure)."""

from __future__ import annotations

import random

import pytest

from scout.learning import priors as P
from scout.learning import routing
from scout.learning.stats import StatRow, coverage, precision, wilson_interval


def row(attempts: int = 0, successes: int = 0, correct: int = 0, wrong: int = 0, **kw) -> StatRow:
    return StatRow(
        attempts=attempts,
        correct=correct,
        wrong=wrong,
        inconclusive=kw.get("inconclusive", 0),
        latency_ms_total=kw.get("latency", 0),
        successes=successes,
        cost_usd_total=kw.get("cost", 0.0),
    )


# ---- priors ------------------------------------------------------------------------------------
def test_priors_cover_every_dimension_and_requested_people_sources() -> None:
    people = P.priors_for("people.source")
    for key in (
        "official_team_page",
        "legal_notice",
        "registry",
        "about_page",
        "searxng_result",
        "gemini_grounded_result",
        "directory",
        "linkedin_snippet",
    ):
        assert key in people and 0 < people[key].precision <= 1
    assert set(P.priors_for("enrich.resolver")) >= {"keyword", "semantic_classifier", "web_research", "user"}
    assert {"fr_registry", "google_maps", "web_search", "gemini_search"} <= set(
        P.priors_for("discovery.source")
    )
    assert P.priors_for("search.engine") and P.priors_for("crawl.tier")
    # grounded research costs money, website extraction does not
    assert people["gemini_grounded_result"].cost_usd > 0 == people["official_team_page"].cost_usd
    assert P.prior("nope", "x") == P.DEFAULT_PRIOR and P.label("people.source", "registry")


def test_email_resolver_priors_are_mirrored_not_changed() -> None:
    from scout.email.confidence import RESOLVER_PRIORS

    mirrored = P.priors_for("resolver")
    assert {k: v.precision for k, v in mirrored.items()} == RESOLVER_PRIORS


# ---- estimates / min evidence -----------------------------------------------------------------------
def test_estimate_is_prior_until_min_evidence() -> None:
    pr = P.prior("enrich.resolver", "keyword")
    few = {
        ("enrich.resolver", "keyword"): row(
            attempts=5, successes=0, correct=0, wrong=routing.MIN_OUTCOMES - 1
        )
    }
    e = routing.estimate("enrich.resolver", "keyword", few)
    assert (e.precision, e.coverage) == (pr.precision, pr.coverage)
    assert not e.learned and e.evidence_level == "learning"
    assert routing.estimate("enrich.resolver", "keyword", None).evidence_level == "prior only"

    many = {
        ("enrich.resolver", "keyword"): row(
            attempts=routing.MIN_ATTEMPTS, successes=0, correct=0, wrong=routing.MIN_OUTCOMES, cost=0.3
        )
    }
    e = routing.estimate("enrich.resolver", "keyword", many)
    assert e.precision_learned and e.coverage_learned and e.evidence_level == "learned"
    assert e.precision == pytest.approx(precision("enrich.resolver", "keyword", pr.precision, many))
    assert e.coverage == pytest.approx(coverage("enrich.resolver", "keyword", pr.coverage, many))
    assert e.precision < pr.precision and e.coverage < pr.coverage
    assert e.cost_usd == pytest.approx(0.3 / routing.MIN_ATTEMPTS)


def test_explicit_float_prior_keeps_known_coverage() -> None:
    e = routing.estimate("people.source", "official_team_page", {}, prior=0.5)
    assert e.precision == 0.5 and e.coverage == P.prior("people.source", "official_team_page").coverage


def test_wilson_interval() -> None:
    assert wilson_interval(0, 0) is None
    lo, hi = wilson_interval(45, 50)
    assert 0.8 < lo < 0.9 < hi < 0.97
    lo, hi = wilson_interval(0, 5)
    assert lo == 0.0 and hi < 0.4


# ---- ranking ------------------------------------------------------------------------------------
KEYS = ["keyword", "semantic_classifier", "web_research"]


def test_rank_keeps_input_order_without_evidence() -> None:
    assert [r.key for r in routing.rank("enrich.resolver", KEYS[::-1], {})] == KEYS[::-1]
    # on priors only, yield per dollar puts the free resolver first
    by_prior = routing.rank("enrich.resolver", KEYS[::-1], {}, keep_order_without_evidence=False)
    assert by_prior[0].key == "keyword" and by_prior[-1].key == "web_research"


def test_learned_evidence_reorders_by_objective() -> None:
    snap = {
        ("enrich.resolver", "keyword"): row(attempts=200, successes=20, correct=5, wrong=40),
        ("enrich.resolver", "semantic_classifier"): row(attempts=200, successes=180, correct=95, wrong=5),
    }
    ranked = routing.rank(
        "enrich.resolver", ["keyword", "semantic_classifier"], snap, objective="yield", explore=0
    )
    assert [r.key for r in ranked] == ["semantic_classifier", "keyword"]
    assert not any(r.explored for r in ranked)
    prec = routing.rank(
        "enrich.resolver", ["keyword", "semantic_classifier"], snap, objective="precision", explore=0
    )
    assert prec[0].key == "semantic_classifier" and prec[0].score > prec[1].score


def test_exploration_is_seeded_and_bounded() -> None:
    snap = {
        ("discovery.source", "fr_registry"): row(attempts=400, successes=380, correct=300, wrong=60),
        ("discovery.source", "osm"): row(attempts=40, successes=30, correct=3, wrong=9),
    }
    keys = ["fr_registry", "osm", "web_search"]

    def run(seed: int) -> list[tuple[str, bool]]:
        return [
            (r.key, r.explored)
            for r in routing.rank("discovery.source", keys, snap, objective="yield", seed=seed)
        ]

    assert run(7) == run(7)  # deterministic per seed
    runs = [routing.rank("discovery.source", keys, snap, objective="yield", seed=s) for s in range(400)]
    share = sum(r[0].explored for r in runs) / len(runs)
    assert 0.04 < share < 0.18  # ≈ EXPLORATION_SHARE
    # exploitation (no exploration) always puts the proven source first
    assert all(r[0].key == "fr_registry" for r in runs if not r[0].explored)
    # forced exploration on a close contest: Thompson draws let an unproven source lead some of the time
    close = {("discovery.source", "fr_registry"): row(attempts=40, successes=40, correct=12, wrong=8)}
    always = [
        routing.rank("discovery.source", keys, close, objective="precision", explore=1.0, seed=s)
        for s in range(300)
    ]
    assert all(r[0].explored for r in always)
    leaders = [r[0].key for r in always]
    assert 0 < leaders.count("web_search") < leaders.count("fr_registry")


def test_rank_never_raises_on_garbage() -> None:
    out = routing.rank("enrich.resolver", ["keyword", "", "keyword"], {"bad": "snapshot"})  # type: ignore[dict-item]
    assert [r.key for r in out] == ["keyword"]
    assert routing.objective_value(0.5, 0.5, 0.0, 0.0, "nope") == 0.25  # type: ignore[arg-type]


def test_make_rng_string_seed_is_stable() -> None:
    a, b = routing.make_rng("campaign-x"), routing.make_rng("campaign-x")
    assert [a.random() for _ in range(3)] == [b.random() for _ in range(3)]
    assert isinstance(routing.make_rng(None), random.Random)


# ---- confidence blending / relative yield ----------------------------------------------------------------
def test_blend_confidence_is_identity_without_evidence_and_monotonic_with_it() -> None:
    k = ("people.source", "official_team_page")
    assert routing.learned_confidence(*k, 0.88, {}) == 0.88
    assert routing.learned_confidence(*k, 0.88, {k: row(correct=3, wrong=5)}) == 0.88  # below MIN_OUTCOMES

    bad = {k: row(attempts=100, correct=20, wrong=80)}
    good = {k: row(attempts=100, correct=99, wrong=1)}
    lowered = routing.learned_confidence(*k, 0.88, bad)
    raised = routing.learned_confidence(*k, 0.88, good)
    assert lowered < 0.88 < raised <= 0.99
    # formula: logit(c') = logit(c) + logit(p̂) − logit(p₀)
    import math

    e = routing.estimate(*k, bad)
    lg = lambda p: math.log(p / (1 - p))  # noqa: E731
    expected = 1 / (1 + math.exp(-(lg(0.88) + lg(e.precision) - lg(e.prior.precision))))
    assert lowered == pytest.approx(expected, abs=1e-3)
    # a hard 1.0 is lowered by bad evidence but never by good evidence
    assert routing.learned_confidence(*k, 1.0, bad) < 1.0
    assert routing.learned_confidence(*k, 1.0, good) == 1.0


def test_relative_yield() -> None:
    k = ("discovery.source", "osm")
    assert routing.relative_yield(routing.estimate(*k, {})) == 1.0
    great = routing.estimate(*k, {k: row(attempts=500, successes=500, correct=400, wrong=10)})
    poor = routing.estimate(*k, {k: row(attempts=500, successes=50, correct=1, wrong=100)})
    assert routing.relative_yield(great) == routing.FACTOR_RANGE[1]
    assert routing.relative_yield(poor) == routing.FACTOR_RANGE[0]
