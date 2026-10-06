"""AI people extraction: verbatim post-validation rejects hallucinations; no AI → no output."""

from __future__ import annotations

import pytest

from scout.ai.factory import FakeProvider, LocalProvider, set_ai
from scout.extract.ai_people import AIPeopleExtraction, ai_extract_people, appears_verbatim
from tests.unit.extract.helpers import agence_pages


@pytest.fixture
def fake_ai():
    provider = FakeProvider()
    set_ai(provider)
    yield provider
    set_ai(None)


def _answer(prompt: str) -> dict:
    assert "<untrusted_website_content" in prompt  # scraped text is wrapped as untrusted data
    if "agence-lumiere.fr/equipe" not in prompt:
        return {"people": []}
    return {
        "people": [
            {"full_name": "Sarah Benali", "title": "Cheffe de projet",
             "evidence_quote": "Sarah Benali\nCheffe de projet", "source_url": "http://agence-lumiere.fr/equipe"},
            # hallucinated: not on the page
            {"full_name": "Pierre Lambert", "title": "CEO",
             "evidence_quote": "Pierre Lambert, CEO", "source_url": "http://agence-lumiere.fr/equipe"},
            # real name but invented evidence → rejected
            {"full_name": "Thomas Petit", "title": "Directeur général",
             "evidence_quote": "Thomas Petit, directeur général depuis 2010", "source_url": "http://agence-lumiere.fr/equipe"},
            # verbatim text but not a person's name
            {"full_name": "Ils nous font confiance", "title": None,
             "evidence_quote": "Ils nous font confiance", "source_url": "http://agence-lumiere.fr/equipe"},
            # real person, invented title → title dropped, person kept with lower confidence
            {"full_name": "INÈS GARNIER", "title": "VP Engineering",
             "evidence_quote": "Inès Garnier  Développeuse web", "source_url": "http://agence-lumiere.fr/equipe"},
        ]
    }


async def test_ai_people_rejects_hallucinations(fake_ai):
    fake_ai.on(AIPeopleExtraction.__name__, _answer)
    people = await ai_extract_people(agence_pages(), company_name="Agence Lumière", max_pages=3)
    names = {p.full_name: p for p in people}
    assert set(names) == {"Sarah Benali", "Inès Garnier"}  # ALL CAPS normalized for display
    sarah = names["Sarah Benali"]
    assert sarah.method == "ai" and sarah.source_type == "ai_extraction"
    assert sarah.confidence <= 0.8 and sarah.title == "Cheffe de projet"
    assert sarah.source_url == "http://agence-lumiere.fr/equipe"
    ines = names["Inès Garnier"]
    assert ines.title is None and ines.confidence < sarah.confidence
    # only team/about/home pages are sent, at most max_pages calls
    assert len(fake_ai.calls) == 3


async def test_ai_unavailable_returns_nothing():
    set_ai(LocalProvider())
    try:
        assert await ai_extract_people(agence_pages(), company_name="Agence Lumière") == []
    finally:
        set_ai(None)


async def test_ai_errors_are_swallowed(fake_ai):
    # No handler registered → FakeProvider raises AIUnavailable for every call.
    assert await ai_extract_people(agence_pages(), company_name="Agence Lumière") == []


def test_appears_verbatim_is_accent_case_whitespace_insensitive():
    hay = "Claire Fontaine\nCo-fondatrice & Directrice générale"
    assert appears_verbatim("CLAIRE  FONTAINE", hay)
    assert appears_verbatim("claire fontaine co-fondatrice", hay)
    assert appears_verbatim("Claire Fontaine … Directrice générale", hay)
    assert not appears_verbatim("Claire Fontaine, CEO", hay)
