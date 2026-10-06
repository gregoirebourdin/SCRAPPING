"""Empirical Source Scoring unit tests: every test starts and ends with an empty in-process stats cache."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from scout.learning import stats


@pytest.fixture(autouse=True)
def _clean_stats_cache() -> Iterator[None]:
    stats.reset_cache()
    yield
    stats.reset_cache()
