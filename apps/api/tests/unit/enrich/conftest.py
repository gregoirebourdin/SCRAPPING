"""Enrichment unit-test fixtures."""

from __future__ import annotations

import pytest

from scout.ai.factory import FakeProvider, LocalProvider, set_ai


@pytest.fixture
def fake_ai():
    """A scripted FakeProvider installed as the AI provider for the test."""
    fake = FakeProvider()
    set_ai(fake)
    yield fake
    set_ai(None)


@pytest.fixture
def local_ai():
    """The deterministic LocalProvider (no AI available)."""
    set_ai(LocalProvider())
    yield
    set_ai(None)
