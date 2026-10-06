"""Listicle expansion: outbound company links from list-like structures only (fetch is mocked)."""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field

import pytest

from scout.discovery.listicle import expand_listicle, extract_listicle_links

from .conftest import fixture_text

PAGE = "https://www.lafabrique-du-web.fr/blog/top-10-agences-marketing-lyon"


def test_extract_company_links() -> None:
    cands = extract_listicle_links(fixture_text("listicle_agences_lyon.html"), PAGE)
    assert [(c.domain, c.name) for c in cands] == [
        ("agence-boreal.fr", "Boréal"),            # "Voir le site" → preceding heading, ordinal stripped
        ("pixel-studio-lyon.fr", "Pixel Studio"),
        ("kreacom.fr", "Kréa Com"),                # facebook link skipped, "leur site" → heading
        ("atelier-nord.studio", "Atelier Nord"),
        ("mediapilote-lyon.fr", "Médiapilote Lyon"),
        ("okidoki-agence.fr", "Okidoki"),
    ]
    c = cands[0]
    assert c.source == "web_search" and c.website == "https://agence-boreal.fr/" and c.source_url == PAGE
    assert c.raw_data["listicle_url"] == PAGE
    # nav / footer / directory / same-site links never become candidates
    assert not {"partner-network.io", "footer-sponsor.fr", "sortlist.fr", "lafabrique-du-web.fr"} & {x.domain for x in cands}


@dataclass
class FakeResponse:
    final_url: str
    status_code: int
    text: str
    headers: dict[str, str] = field(default_factory=dict)


async def test_expand_uses_ssrf_safe_fetcher(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, int | None]] = []

    async def fake_fetch(url: str, *, etag=None, last_modified=None, max_bytes=None, accept=None):
        calls.append((url, max_bytes))
        return FakeResponse(final_url=url, status_code=200, text=fixture_text("listicle_agences_lyon.html"))

    module = types.ModuleType("scout.crawl.http")
    module.fetch = fake_fetch  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "scout.crawl.http", module)
    cands = await expand_listicle(PAGE)
    assert calls and calls[0][0] == PAGE and calls[0][1] is not None
    assert len(cands) == 6

    async def failing_fetch(url: str, **_: object) -> FakeResponse:
        return FakeResponse(final_url=url, status_code=404, text="not found")

    module.fetch = failing_fetch  # type: ignore[attr-defined]
    assert await expand_listicle(PAGE) == []
