"""Passage splitting, term expansion, ranking and evidence matching."""

from __future__ import annotations

from scout.enrich.chunks import (
    Passage,
    expand_terms,
    page_priors_for,
    passages_for_pages,
    rank_passages,
    split_passages,
)
from scout.enrich.matching import excerpt, find_terms, quote_in_text
from tests.unit.enrich.helpers import load_fixture, make_company, make_pages


def _pages(name: str):
    data = load_fixture(name)
    return make_pages(data, make_company(data))


def test_split_passages_respects_size_and_line_boundaries():
    page = _pages("agency_instagram")[0]
    passages = split_passages(page, max_chars=200)
    assert passages[0].text.startswith("La Ruche Sociale — Agence Instagram")  # header: title + meta
    assert all(len(p.text) <= 200 for p in passages)
    assert [p.idx for p in passages] == list(range(len(passages)))
    body = "\n".join(p.text for p in passages[1:])
    for line in page.content_text.splitlines():
        if line.strip():
            assert line.strip() in body  # lines are never cut in the middle


def test_split_passages_splits_long_lines_on_sentences():
    class P:
        url = "https://x.test/"
        page_type = "home"
        title = None
        meta_description = None
        content_text = " ".join(f"Sentence number {i} talks about something." for i in range(60))

    passages = split_passages(P(), max_chars=300)
    assert len(passages) > 5 and all(len(p.text) <= 300 for p in passages)


def test_expand_terms_synonyms_and_stopwords():
    terms = expand_terms("offers Instagram management", ["Instagram management"])
    lowered = [t.lower() for t in terms]
    assert "instagram management" in lowered and "instagram" in lowered
    assert "insta" in lowered and "reseaux sociaux" in lowered and "community management" in lowered
    assert "offers" not in lowered
    strict = [t.lower() for t in expand_terms("offers Instagram management", ["Instagram management"], strict=True)]
    assert "insta" in strict and "reels" not in strict


def test_expand_terms_french_and_ecommerce():
    terms = [t.lower() for t in expand_terms("propose du référencement e-commerce", [])]
    assert "seo" in terms
    assert "boutique en ligne" in terms


def test_rank_passages_prefers_service_passages_and_dedupes_footers():
    pages = _pages("agency_instagram")
    passages = passages_for_pages(pages, max_chars=300)
    terms = expand_terms("offers Instagram management", ["Instagram management", "Instagram"])
    ranked = rank_passages(passages, terms, page_priors=page_priors_for("Instagram management", ["services"]), k=4)
    assert ranked and ranked[0].page_type in {"services", "home"}
    assert any(p.page_type == "services" and "gérons vos comptes Instagram" in p.text for p in ranked[:3])
    assert all(p.page_type != "contact" for p in ranked)
    assert sum(len(p.text) for p in ranked) <= 6000
    texts = [p.text for p in ranked]
    assert len(texts) == len(set(texts))


def test_rank_passages_max_chars_and_unmatched_fill():
    passages = [Passage(None, f"https://x.test/{i}", "other", "lorem ipsum " * 40, i) for i in range(10)]
    assert rank_passages(passages, ["instagram"]) == []
    filled = rank_passages(passages, ["instagram"], include_unmatched=True, k=10, max_chars=1000, max_per_page=1)
    assert filled and sum(len(p.text) for p in filled) <= 1000


def test_pricing_priors():
    assert page_priors_for("their pricing")["pricing"] > page_priors_for("their niche")["pricing"]


def test_find_terms_accent_case_and_word_boundaries():
    text = "Notre agence fait du Référencement et de l'E-Commerce. Instagrammable n'est pas Instagram."
    assert find_terms(text, ["referencement"])
    assert find_terms(text, ["e-commerce"])  # multi-token terms tolerate separators…
    assert find_terms("Experts ecommerce", ["e-commerce"])  # …including none
    hits = find_terms(text, ["instagram"])
    assert len(hits) == 1 and text[hits[0].start : hits[0].end] == "Instagram"
    assert excerpt(text, hits[0].start, hits[0].end, 40).endswith("Instagram.")


def test_quote_validation_is_accent_case_whitespace_insensitive():
    passage = "Nous gérons vos comptes Instagram de A à Z :\nstratégie éditoriale, création de contenu."
    assert quote_in_text("nous GERONS vos comptes instagram de a a z", passage)
    assert quote_in_text("Nous gérons vos comptes … création de contenu", passage)
    assert not quote_in_text("Nous gérons vos comptes TikTok", passage)
    assert not quote_in_text("Nous", passage)  # too short to be evidence
