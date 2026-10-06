"""Suite registry: registration, validation, demo suite, lazy loading."""

from __future__ import annotations

from typing import Any

import pytest

from scout.benchmark import registry
from scout.benchmark.metrics import rate
from scout.benchmark.registry import SuiteResult, get_suite, list_suites, register_suite, run_suite


@pytest.fixture
def tmp_suite():
    calls: list[dict[str, Any]] = []

    async def run(config: dict[str, Any]) -> SuiteResult:
        calls.append(config)
        return SuiteResult(
            metrics={"email_precision": rate(9, 10)},
            strategies={
                s: {"email_precision": rate(i, 10)}
                for i, s in enumerate(config.get("strategies") or ["a", "b"])
            },
            items=[{"label": f"#{i}"} for i in range(3)],
        )

    register_suite(
        "unit-test", "Unit test suite", "…", run, strategies=("a", "b"), default_config={"seed": 1}
    )
    yield calls
    registry.unregister_suite("unit-test")


async def test_register_and_run_with_default_config_overlay(tmp_suite) -> None:
    spec = get_suite("unit-test")
    assert spec is not None and spec.public()["strategies"] == ["a", "b"]
    assert "unit-test" in [s.key for s in list_suites()]
    res = await run_suite("unit-test", {"strategies": ["b"]})
    assert tmp_suite == [{"seed": 1, "strategies": ["b"]}]
    assert list(res.strategies or {}) == ["b"]


async def test_unknown_suite_strategy_and_bad_result(tmp_suite) -> None:
    with pytest.raises(KeyError):
        await run_suite("nope")
    with pytest.raises(ValueError, match="Unknown strategies"):
        await run_suite("unit-test", {"strategies": ["zzz"]})

    async def bad(config: dict[str, Any]) -> Any:
        return {"metrics": {}}

    register_suite("bad-suite", "Bad", "", bad)
    try:
        with pytest.raises(TypeError):
            await run_suite("bad-suite")
    finally:
        registry.unregister_suite("bad-suite")


def test_invalid_keys_are_rejected() -> None:
    async def run(config: dict[str, Any]) -> SuiteResult:
        return SuiteResult(metrics={})

    for key in ("", "Has Space", "UPPER", "a" * 80):
        with pytest.raises(ValueError):
            register_suite(key, "t", "d", run)


async def test_items_are_capped(monkeypatch: pytest.MonkeyPatch, tmp_suite) -> None:
    monkeypatch.setattr(registry, "MAX_SUITE_ITEMS", 2)
    res = await run_suite("unit-test")
    assert len(res.items) == 2 and "Only the first 2" in res.notes[0]


async def test_demo_suite_compares_strategies_without_io() -> None:
    registry.load_suites(include_demo=True)
    spec = get_suite("demo")
    assert spec is not None and spec.demo and spec.strategies == ("exact", "normalized")
    res = await run_suite("demo")
    assert set(res.strategies or {}) == {"exact", "normalized"}
    exact, norm = res.strategies["exact"], res.strategies["normalized"]  # type: ignore[index]
    assert norm["person_recall"]["value"] == 1.0 and exact["person_recall"]["value"] < 0.5
    assert len(res.items) == 20 and {i["strategy"] for i in res.items} == {"exact", "normalized"}
    assert res.definitions and "person_precision" in res.definitions


def test_load_suites_skips_missing_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry, "SUITE_MODULES", ["scout.benchmark.does_not_exist"])
    assert registry.load_suites(include_demo=False) == []
