"""Discovery unit-test helpers: settings overrides, fixture files, no network (respx mocks only)."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from scout.config import Settings, get_settings
from scout.schemas.campaign import CampaignDefinition

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "discovery"


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def fixture_json(name: str) -> Any:
    return json.loads(fixture_text(name))


def defn(**company_filters: Any) -> CampaignDefinition:
    """CampaignDefinition with the given company_filters (employee_range as a (min, max) tuple)."""
    er = company_filters.pop("employee_range", None)
    if er is not None:
        company_filters["employee_range"] = {"min": er[0], "max": er[1]}
    extra = company_filters.pop("_extra", {})
    return CampaignDefinition.model_validate({"company_filters": company_filters, **extra})


@pytest.fixture
def settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., Settings]]:
    """Apply env overrides (None = unset) and return fresh settings; cache is reset afterwards."""

    def apply(**env: str | None) -> Settings:
        for k, v in env.items():
            if v is None:
                monkeypatch.delenv(k, raising=False)
            else:
                monkeypatch.setenv(k, v)
        get_settings.cache_clear()
        return get_settings()

    get_settings.cache_clear()
    yield apply
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _no_maps_or_fixture_by_default(settings_env: Callable[..., Settings]) -> None:
    """Optional sources start unconfigured; tests opt in explicitly."""
    settings_env(GMAPS_SCRAPER_URL=None, DISCOVERY_FIXTURE_MANIFEST=None, GITHUB_TOKEN=None)
