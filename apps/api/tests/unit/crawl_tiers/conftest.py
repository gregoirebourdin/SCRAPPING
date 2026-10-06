"""Fixtures for tier tests: local fixture HTTP server, clean Scrapling probe state, telemetry capture."""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass

import pytest

from scout.crawl import scrapling_tiers
from tests.unit.crawl.fixture_server import FixtureServer, reset_crawl_state


@pytest.fixture
async def fixture_server():
    server = FixtureServer()
    await server.start()
    try:
        yield server
    finally:
        await server.stop()
        await reset_crawl_state()


@pytest.fixture(autouse=True)
def _fresh_scrapling_state():
    scrapling_tiers.reset_state()
    yield
    scrapling_tiers.reset_state()


@dataclass(frozen=True)
class FakeStatEvent:
    dimension: str
    key: str
    correct: bool | None = None
    outcome: bool = False
    produced: bool | None = None
    latency_ms: int = 0
    cost_usd: float = 0.0


@pytest.fixture
def stats_events(monkeypatch):
    """Install a fake ``scout.learning.stats`` and return the list of recorded events."""
    recorded: list[FakeStatEvent] = []
    mod = types.ModuleType("scout.learning.stats")

    async def record(events):
        recorded.extend(events)

    mod.StatEvent = FakeStatEvent  # type: ignore[attr-defined]
    mod.record = record  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "scout.learning.stats", mod)
    return recorded
