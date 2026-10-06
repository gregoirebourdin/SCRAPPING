"""Page classification (multilingual), sitemap parsing, page selection and parked detection."""

from __future__ import annotations

from pathlib import Path

import pytest

from scout.crawl.crawler import detect_parked
from scout.crawl.page_selector import classify_page_type, parse_sitemap, parse_sitemap_entries, select_pages
from scout.crawl.parser import parse_html
from scout.db.enums import PageType

FIX = Path(__file__).resolve().parents[2] / "fixtures" / "html"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://x.fr/", PageType.home),
        ("https://x.fr/fr/", PageType.home),
        ("https://x.fr/qui-sommes-nous", PageType.about),
        ("https://x.fr/a-propos/", PageType.about),
        ("https://x.fr/agence", PageType.about),
        ("https://x.fr/notre-histoire", PageType.about),
        ("https://x.de/ueber-uns", PageType.about),
        ("https://x.es/quienes-somos", PageType.about),
        ("https://x.it/chi-siamo", PageType.about),
        ("https://x.nl/over-ons", PageType.about),
        ("https://x.com/about-us", PageType.about),
        ("https://x.fr/equipe", PageType.team),
        ("https://x.fr/l-equipe", PageType.team),
        ("https://x.fr/agence/notre-equipe", PageType.team),
        ("https://x.com/our-team", PageType.team),
        ("https://x.com/leadership", PageType.team),
        ("https://x.fr/fondateurs", PageType.team),
        ("https://x.fr/nos-services", PageType.services),
        ("https://x.fr/prestations", PageType.services),
        ("https://x.fr/expertises/seo", PageType.services),
        ("https://x.fr/solutions", PageType.solutions),
        ("https://x.fr/contact", PageType.contact),
        ("https://x.fr/contactez-nous", PageType.contact),
        ("https://x.de/kontakt", PageType.contact),
        ("https://x.fr/tarifs", PageType.pricing),
        ("https://x.de/preise", PageType.pricing),
        ("https://x.com/pricing", PageType.pricing),
        ("https://x.fr/recrutement", PageType.careers),
        ("https://x.fr/rejoignez-nous", PageType.careers),
        ("https://x.de/karriere", PageType.careers),
        ("https://x.com/careers", PageType.careers),
        ("https://x.fr/blog", PageType.blog),
        ("https://x.fr/blog/mon-article-sur-l-equipe", PageType.blog),
        ("https://x.fr/actualites", PageType.news),
        ("https://x.fr/presse", PageType.news),
        ("https://x.fr/mentions-legales", PageType.legal),
        ("https://x.fr/mentions-legales.html", PageType.legal),
        ("https://x.de/impressum", PageType.legal),
        ("https://x.com/legal-notice", PageType.legal),
        ("https://x.es/aviso-legal", PageType.legal),
        ("https://x.fr/cgv", PageType.legal),
        ("https://x.fr/politique-de-confidentialite", PageType.other),
        ("https://x.com/privacy-policy", PageType.other),
        ("https://x.fr/realisations", PageType.case_studies),
        ("https://x.fr/references", PageType.case_studies),
        ("https://x.com/case-studies", PageType.case_studies),
        ("https://x.fr/portfolio/projet-x", PageType.case_studies),
        ("https://x.fr/produit/chaise-bleue", PageType.other),
    ],
)
def test_classify_page_type(url, expected):
    assert classify_page_type(url) == expected


def test_classify_uses_anchor_text_when_path_is_opaque():
    assert classify_page_type("https://x.fr/page-12", anchor="Notre équipe") == PageType.team
    assert classify_page_type("https://x.fr/?p=4", anchor="Mentions légales") == PageType.legal
    assert classify_page_type("https://x.com/p/8812", title="Contact us") == PageType.contact


def test_parse_sitemap_urlset_and_index():
    urls = parse_sitemap((FIX / "agence_lumiere/sitemap.xml").read_text())
    assert "http://agence-lumiere.fr/equipe" in urls and len(urls) == 10
    index = """<?xml version="1.0" encoding="UTF-8"?>
    <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <sitemap><loc>https://x.fr/post-sitemap.xml</loc></sitemap>
      <sitemap><loc>https://x.fr/page-sitemap.xml</loc></sitemap>
    </sitemapindex>"""
    entries = parse_sitemap_entries(index)
    assert entries.is_index and entries.sitemaps == ["https://x.fr/post-sitemap.xml", "https://x.fr/page-sitemap.xml"]
    assert parse_sitemap(index) == entries.sitemaps
    # broken XML still yields locs; entities are never resolved (XXE-safe)
    assert parse_sitemap("<urlset><url><loc>https://x.fr/a</loc></url><url><loc>https://x.fr/b</loc>") == [
        "https://x.fr/a", "https://x.fr/b",
    ]
    xxe = '<?xml version="1.0"?><!DOCTYPE r [<!ENTITY e SYSTEM "file:///etc/passwd">]><urlset><url><loc>&e;</loc></url></urlset>'
    assert all("root:" not in u for u in parse_sitemap(xxe))


def test_select_pages_fr_site():
    home_url = "http://agence-lumiere.fr/"
    home = parse_html((FIX / "agence_lumiere/home.html").read_text(), home_url, is_home=True)
    sitemap = parse_sitemap((FIX / "agence_lumiere/sitemap.xml").read_text())
    plan = select_pages(home_url, home.links["internal"], sitemap, max_pages=10)
    assert plan[0] == (home_url, PageType.home)
    types = [pt for _u, pt in plan]
    urls = [u for u, _pt in plan]
    for expected in (PageType.team, PageType.about, PageType.legal, PageType.contact, PageType.services):
        assert expected in types
    assert len(plan) <= 10
    assert not any(u.endswith(".pdf") or "/tag/" in u or "/blog/" in u or "confidentialite" in u for u in urls)
    assert len(urls) == len(set(urls))
    assert types.index(PageType.team) < types.index(PageType.services)


def test_select_pages_budget_and_language_preference():
    home_url = "https://studio.example/"
    links = [
        {"url": "https://studio.example/en/about", "text": "About"},
        {"url": "https://studio.example/a-propos", "text": "À propos"},
        {"url": "https://studio.example/equipe", "text": "Équipe"},
        {"url": "https://studio.example/team/jane-doe/bio/extra/deep", "text": "Jane"},
        {"url": "https://studio.example/contact", "text": "Contact"},
        {"url": "https://studio.example/blog/page/2", "text": "Page 2"},
        {"url": "https://studio.example/services?sort=asc", "text": "Services"},
        {"url": "https://blog.studio.example/equipe", "text": "Équipe blog"},
        {"url": "https://other.example/team", "text": "Other"},
        {"url": "https://studio.example/wp-content/uploads/brochure.pdf", "text": "PDF"},
    ]
    plan = select_pages(home_url, links, [], max_pages=3)
    assert len(plan) == 3
    assert plan[1] == ("https://studio.example/equipe", PageType.team)
    assert plan[2] == ("https://studio.example/a-propos", PageType.about)  # root-language version preferred
    full = select_pages(home_url, links, [], max_pages=50)
    assert len(full) <= 12
    urls = [u for u, _ in full]
    assert "https://other.example/team" not in urls and "https://blog.studio.example/equipe" not in urls
    assert not any("page/2" in u or "?" in u or u.endswith(".pdf") for u in urls)


def test_select_pages_en_site():
    home_url = "https://acme.io/"
    links = [
        {"url": "https://acme.io/about", "text": "About"},
        {"url": "https://acme.io/company/leadership", "text": "Leadership"},
        {"url": "https://acme.io/pricing", "text": "Pricing"},
        {"url": "https://acme.io/careers", "text": "Careers"},
        {"url": "https://acme.io/contact-us", "text": "Contact us"},
        {"url": "https://acme.io/legal/terms", "text": "Terms"},
        {"url": "https://acme.io/legal/imprint", "text": "Imprint"},
    ]
    plan = dict((pt, u) for u, pt in select_pages(home_url, links, [], max_pages=12))
    assert plan[PageType.team] == "https://acme.io/company/leadership"
    assert plan[PageType.pricing] == "https://acme.io/pricing"
    assert plan[PageType.careers] == "https://acme.io/careers"
    assert plan[PageType.legal] == "https://acme.io/legal/imprint"  # legal notice preferred over terms


def test_parked_detection():
    html = (FIX / "parked.html").read_text()
    assert detect_parked(html, parse_html(html, "http://boulangerie-dupont.fr/"), "http://boulangerie-dupont.fr/")
    assert detect_parked("<html><body>x</body></html>", parse_html("<html><body>x</body></html>", "https://a.fr/"), "https://sedo.com/x")
    construction = "<html><body><h1>Site en construction</h1><p>Boulangerie Dupont – ouverture prochaine !</p></body></html>"
    assert detect_parked(construction, parse_html(construction, "https://a.fr/"), "https://a.fr/") is None
    real = (FIX / "agence_lumiere/home.html").read_text()
    assert detect_parked(real, parse_html(real, "http://agence-lumiere.fr/"), "http://agence-lumiere.fr/") is None


def test_select_pages_stays_on_home_origin():
    plan = select_pages(
        "http://www.atelier-bois.fr/",
        [],
        ["https://atelier-bois.fr/equipe", "https://www.atelier-bois.fr/contact/"],
        max_pages=5,
    )
    assert plan[1:] == [
        ("http://www.atelier-bois.fr/equipe", PageType.team),
        ("http://www.atelier-bois.fr/contact/", PageType.contact),
    ]
