"""Fixtures for crawler tests: a local fixture HTTP server wired through crawler_host_overrides."""

from __future__ import annotations

import pytest

from tests.unit.crawl.fixture_server import FixtureServer, reset_crawl_state


@pytest.fixture
async def fixture_server(monkeypatch):
    server = FixtureServer()
    await server.start()
    try:
        yield server
    finally:
        await server.stop()
        await reset_crawl_state()
