"""Pluggable benchmark suites (synthetic, labelled scenarios) shown in /benchmark and the CLI.

A suite module registers itself at import time::

    from scout.benchmark.metrics import rate, measure
    from scout.benchmark.registry import SuiteResult, register_suite

    async def run(config: dict) -> SuiteResult:
        # config: {"strategies": [...] | None, "seed": int, "size": int, …} (whatever the launcher sent)
        return SuiteResult(
            metrics={"email_precision": rate(k, n), "p95_email_resolution_ms": measure(812.0, n)},
            strategies={"legacy": {...same shape...}, "fast_deep": {...}},
            items=[{"label": "bench-0001.example · Marie Dupont", "strategy": "fast_deep",
                    "expected": {...}, "actual": {...}, "verdict": "correct", "fp": 0, "fn": 0,
                    "latency_ms": 12, "cost_usd": 0.0}],
        )

    register_suite("email_engine", "Email engine (synthetic)", "…", run,
                   strategies=("legacy", "fast_only", "fast_deep"))

Metric values may be plain numbers (``samples={key: n}`` gives their sample size) or ``rate(k, n)`` /
``measure(value, n)`` dicts; rates get a Wilson 90 % interval. Give ``labels`` / ``definitions`` for your
metric keys: the harness never assumes its dataset definitions apply to a suite (e.g. a suite's
``email_precision`` may count SAFE + LIKELY_SAFE only). Non-numeric values are ignored. Modules listed in
``SUITE_MODULES`` are imported lazily by ``load_suites()`` (a missing module is skipped).
"""

from __future__ import annotations

import importlib
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import structlog

log = structlog.get_logger("benchmark.registry")

MAX_SUITE_ITEMS = 5_000

SUITE_MODULES = ["scout.benchmark.email_bench"]
DEMO_MODULE = "scout.benchmark.demo_suite"


@dataclass
class SuiteResult:
    metrics: dict[str, Any]
    strategies: dict[str, dict[str, Any]] | None = None
    items: list[dict[str, Any]] = field(default_factory=list)
    samples: dict[str, int] | None = None
    notes: list[str] = field(default_factory=list)
    labels: dict[str, str] | None = None  # metric key → display label (defaults to the harness's names)
    definitions: dict[str, str] | None = None  # metric key → definition shown in the UI


SuiteRun = Callable[[dict[str, Any]], Awaitable[SuiteResult]]


@dataclass(frozen=True)
class SuiteSpec:
    key: str
    title: str
    description: str
    run: SuiteRun
    strategies: tuple[str, ...] = ()
    default_config: Mapping[str, Any] = field(default_factory=dict)
    demo: bool = False

    def public(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "description": self.description,
            "strategies": list(self.strategies),
            "default_config": dict(self.default_config),
            "demo": self.demo,
        }


_suites: dict[str, SuiteSpec] = {}
_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,62}$")


def register_suite(
    key: str,
    title: str,
    description: str,
    run: SuiteRun,
    *,
    strategies: Sequence[str] = (),
    default_config: Mapping[str, Any] | None = None,
    demo: bool = False,
) -> SuiteSpec:
    """Register (or replace) a suite. ``run(config)`` must return a SuiteResult."""
    if not _KEY_RE.match(key):
        raise ValueError(f"Invalid suite key {key!r} (lowercase letters, digits, _ . -)")
    spec = SuiteSpec(
        key=key,
        title=title,
        description=description,
        run=run,
        strategies=tuple(strategies),
        default_config=dict(default_config or {}),
        demo=demo,
    )
    _suites[key] = spec
    return spec


def unregister_suite(key: str) -> None:
    _suites.pop(key, None)


def load_suites(*, include_demo: bool | None = None) -> list[str]:
    """Import suite modules (side-effect registration). The demo suite loads outside production."""
    if include_demo is None:
        from scout.config import get_settings

        include_demo = not get_settings().is_production
    loaded: list[str] = []
    for mod in [*SUITE_MODULES, *([DEMO_MODULE] if include_demo else [])]:
        try:
            importlib.import_module(mod)
            loaded.append(mod)
        except ModuleNotFoundError as exc:
            if exc.name != mod:
                log.warning("benchmark.suite_import_failed", module=mod, error=str(exc))
        except Exception as exc:  # a broken suite must not break the API
            log.warning("benchmark.suite_import_failed", module=mod, error=str(exc))
    return loaded


def get_suite(key: str) -> SuiteSpec | None:
    return _suites.get(key)


def list_suites() -> list[SuiteSpec]:
    return sorted(_suites.values(), key=lambda s: (s.demo, s.title.lower()))


async def run_suite(key: str, config: Mapping[str, Any] | None = None) -> SuiteResult:
    """Run a registered suite with ``default_config`` overlaid by ``config``; validates the result."""
    spec = get_suite(key)
    if spec is None:
        raise KeyError(f"Unknown benchmark suite {key!r}")
    cfg = {**spec.default_config, **dict(config or {})}
    wanted = cfg.get("strategies")
    if wanted is not None and spec.strategies:
        unknown = [s for s in wanted if s not in spec.strategies]
        if unknown:
            raise ValueError(f"Unknown strategies for {key}: {unknown}")
    result = await spec.run(cfg)
    if not isinstance(result, SuiteResult):
        raise TypeError(f"Suite {key!r} returned {type(result).__name__}, expected SuiteResult")
    if len(result.items) > MAX_SUITE_ITEMS:
        result.notes.append(f"Only the first {MAX_SUITE_ITEMS} of {len(result.items)} items are stored.")
        result.items = result.items[:MAX_SUITE_ITEMS]
    return result
