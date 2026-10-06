"""Web-search email evidence: on-domain addresses published elsewhere, named from the company's people."""

from __future__ import annotations

from scout.db.enums import EmailEvidenceSource
from scout.email.intel.web_search import addresses_in, fetch_search_evidence
from scout.search import ProviderHealth, SearchCache, SearchChain
from scout.search.types import SearchProviderError, SearchResult
from tests.unit.search.test_chain import Scripted


def _chain(*outcomes) -> SearchChain:
    return SearchChain(
        [Scripted("searxng", *outcomes)], health=ProviderHealth(), cache=SearchCache(), cache_ttl_s=60
    )


def test_only_on_domain_addresses_are_extracted():
    text = "Contact : Marie Durand marie.durand@agence-x.fr — presse@agence-x.fr, voir aussi bob@gmail.com, x@other.fr."
    assert addresses_in(text, "agence-x.fr") == ["marie.durand@agence-x.fr", "presse@agence-x.fr"]
    assert addresses_in("jean@mail.agence-x.fr", "agence-x.fr") == ["jean@mail.agence-x.fr"]


async def test_published_addresses_become_named_search_samples():
    results = [
        SearchResult(
            url="https://annuaire.example/agence-x",
            title="Agence X — équipe",
            snippet="Paul Roux, fondateur : paul.roux@agence-x.fr · contact@agence-x.fr",
            position=1,
        )
    ]
    ev = await fetch_search_evidence(
        "agence-x.fr", people=[("Paul", "Roux"), ("Marie", "Durand")], chain=_chain(results)
    )
    assert ev.completed and ev.queries == 1
    by = {e.address: e for e in ev.emails}
    assert (
        by["paul.roux@agence-x.fr"].first_name == "Paul" and by["paul.roux@agence-x.fr"].last_name == "Roux"
    )
    assert by["paul.roux@agence-x.fr"].source == EmailEvidenceSource.search
    assert by["contact@agence-x.fr"].is_role and by["contact@agence-x.fr"].first_name is None


async def test_no_answer_is_an_error_not_an_empty_completed_check():
    ev = await fetch_search_evidence(
        "agence-x.fr", people=[("Paul", "Roux")], chain=_chain(SearchProviderError("searxng: captcha"))
    )
    assert not ev.completed and ev.error and not ev.emails
