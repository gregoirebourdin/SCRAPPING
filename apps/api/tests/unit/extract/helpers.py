"""Build FetchedPage objects from HTML fixtures (no network)."""

from __future__ import annotations

from pathlib import Path

from scout.crawl.page_selector import classify_page_type
from scout.crawl.parser import parse_html
from scout.crawl.types import FetchedPage
from scout.util.text import content_hash
from scout.util.urls import canonical_url

FIX = Path(__file__).resolve().parents[2] / "fixtures" / "html"

AGENCE_FILES = {
    "http://agence-lumiere.fr/": "agence_lumiere/home.html",
    "http://agence-lumiere.fr/agence": "agence_lumiere/agence.html",
    "http://agence-lumiere.fr/equipe": "agence_lumiere/equipe.html",
    "http://agence-lumiere.fr/services": "agence_lumiere/services.html",
    "http://agence-lumiere.fr/contact": "agence_lumiere/contact.html",
    "http://agence-lumiere.fr/mentions-legales": "agence_lumiere/mentions-legales.html",
    "http://agence-lumiere.fr/blog": "agence_lumiere/blog.html",
}


def page_from_html(html: str, url: str, *, page_type=None) -> FetchedPage:
    is_home = classify_page_type(url) == "home"
    parsed = parse_html(html, url, is_home=is_home)
    return FetchedPage(
        url=url,
        final_url=url,
        canonical_url=canonical_url(url),
        status_code=200,
        content_type="text/html",
        page_type=page_type or classify_page_type(url),
        title=parsed.title,
        meta_description=parsed.meta_description,
        content_text=parsed.content_text,
        content_hash=content_hash(parsed.content_text),
        language=parsed.language,
        head_html=parsed.head_html,
        links=parsed.links,
        emails=parsed.emails,
        phones=parsed.phones,
        structured_data=parsed.structured_data,
        headings=parsed.headings,
        word_count=parsed.word_count,
    )


def agence_pages() -> list[FetchedPage]:
    return [page_from_html((FIX / f).read_text(encoding="utf-8"), u) for u, f in AGENCE_FILES.items()]
