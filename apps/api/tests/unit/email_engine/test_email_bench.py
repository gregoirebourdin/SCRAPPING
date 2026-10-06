"""Email benchmark harness: determinism and behavioural invariants of each strategy (not precision claims)."""

from __future__ import annotations

import pytest

from scout.benchmark.email_bench import STRATEGIES, format_table, run_email_benchmark, run_strategy
from scout.benchmark.email_scenarios import generate


def test_scenarios_are_deterministic() -> None:
    a, b = generate(30, seed=11), generate(30, seed=11)
    assert [(d.domain, d.true_pattern, sorted(d.mailboxes)) for d in a.domains] == [
        (d.domain, d.true_pattern, sorted(d.mailboxes)) for d in b.domains
    ]
    assert generate(30, seed=12).domains != a.domains


@pytest.fixture(scope="module")
def report() -> dict:
    import asyncio

    return asyncio.run(run_email_benchmark(domains=60, seed=5, waves=2))


def test_every_strategy_runs_and_reports_the_requested_metrics(report: dict) -> None:
    assert set(report["strategies"]) == set(STRATEGIES)
    keys = {
        "email_discovery_recall",
        "email_precision",
        "safe_precision",
        "invalid_false_positive_rate",
        "catch_all_accuracy",
        "domain_pattern_accuracy",
        "avg_resolution_ms",
        "p50_resolution_ms",
        "p95_resolution_ms",
        "smtp_fallback_rate",
        "cache_hit_rate",
        "cost_per_email_usd",
        "emails_resolved_per_minute",
    }
    for r in report["strategies"].values():
        assert keys <= set(r["metrics"])
    table = format_table(report)
    assert all(name in table for name in STRATEGIES)
    assert "not a precision claim" in report["disclaimer"]


def test_engine_never_concludes_invalid_for_an_existing_mailbox(report: dict) -> None:
    for name in ("fast_only", "fast_deep", "fast_deep_blocked"):
        m = report["strategies"][name]["metrics"]
        assert m["invalid_false_positives"] == 0, name
        assert m["safe_precision"] in (None, 1.0), name


def test_smtp_only_settles_ambiguity_and_is_batched_per_domain(report: dict) -> None:
    fast, deep = report["strategies"]["fast_only"]["metrics"], report["strategies"]["fast_deep"]["metrics"]
    legacy = report["strategies"]["legacy"]["metrics"]
    assert fast["smtp_rcpts"] == 0 and fast["smtp_fallback_rate"] == 0
    assert deep["email_discovery_recall"] >= fast["email_discovery_recall"]
    assert deep["smtp_fallback_rate"] < legacy["smtp_fallback_rate"]
    assert deep["smtp_sessions"] < legacy["smtp_sessions"]  # one session per domain, not per address


def test_blocked_port_25_stops_probing_quickly_without_negative_verdicts(report: dict) -> None:
    blocked = report["strategies"]["fast_deep_blocked"]
    m = blocked["metrics"]
    assert blocked["health"] == "BLOCKED"
    assert m["smtp_rcpts"] == 0
    assert m["smtp_fallback_rate"] < 0.05  # the health monitor closed the gate after a few failed sessions
    no_mx = blocked["by_kind"].get("no_mx", {}).get("persons", 0)
    assert m["statuses"].get("INVALID", 0) == no_mx  # only domains without MX are INVALID


async def test_unknown_strategy_is_refused() -> None:
    with pytest.raises(ValueError):
        await run_strategy(generate(2), "nope")


async def test_registered_as_a_benchmark_harness_suite() -> None:
    from scout.benchmark.registry import get_suite, load_suites

    load_suites()
    spec = get_suite("email_engine")
    assert spec is not None and set(spec.strategies) == set(STRATEGIES)
    result = await spec.run({"domains": 20, "strategies": ["fast_only", "fast_deep"]})
    assert set(result.strategies) == {"fast_only", "fast_deep"}
    recall = result.metrics["email_discovery_recall"]
    assert {"value", "k", "n", "ci90"} <= set(recall) and recall["n"] > 0
    assert result.definitions and "email_precision" in result.definitions
    assert any("not a precision claim" in n for n in result.notes)
