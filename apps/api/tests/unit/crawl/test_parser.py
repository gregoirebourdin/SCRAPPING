"""HTML parser: line-structured text, emails/phones, social links, JSON-LD, legal pages, head_html."""

from __future__ import annotations

from pathlib import Path

from scout.crawl.parser import MAX_TEXT_BYTES, clean_email, emails_from_text, normalize_phone, parse_html

FIX = Path(__file__).resolve().parents[2] / "fixtures" / "html"


def _read(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def test_team_page_text_is_line_structured():
    page = parse_html(_read("agence_lumiere/equipe.html"), "http://agence-lumiere.fr/equipe")
    lines = page.content_text.split("\n")
    i = lines.index("Claire Fontaine")
    assert lines[i + 1] == "Co-fondatrice & Directrice générale"
    j = lines.index("Thomas Petit")
    assert lines[j + 1] == "Community manager"
    assert page.title == "Notre équipe – Agence Lumière"
    assert "Notre équipe" in page.headings
    assert page.language == "fr"
    assert page.word_count > 20
    # scripts / styles never leak into the text
    assert "function(" not in page.content_text


def test_repeated_navigation_lines_are_deduplicated():
    html = """<html><body><header><nav><a href='/a'>Services</a><a href='/b'>Contact</a></nav></header>
    <main><h1>Titre</h1><p>Chef de projet</p><p>Jean Dupont</p><p>Chef de projet</p><p>Marie Martin</p></main>
    <footer><nav><a href='/a'>Services</a><a href='/b'>Contact</a></nav></footer></body></html>"""
    page = parse_html(html, "https://x.fr/")
    lines = page.content_text.split("\n")
    assert lines.count("Services") == 1 and lines.count("Contact") == 1
    assert lines.count("Chef de projet") == 2  # short main-content repeats (team cards) are kept


def test_emails_plain_obfuscated_and_filtered():
    page = parse_html(_read("agence_lumiere/contact.html"), "http://agence-lumiere.fr/contact")
    assert page.emails[:3] == [
        "contact@agence-lumiere.fr",
        "claire.fontaine@agence-lumiere.fr",
        "julien.moreau@agence-lumiere.fr",
    ]
    text = "Écrire à jean [at] studio-nova [dot] fr ou marie(at)studio-nova.fr ou bob at nova dot io. Logo: logo@2x.png"
    found = emails_from_text(text)
    assert "jean@studio-nova.fr" in found and "marie@studio-nova.fr" in found and "bob@nova.io" in found
    assert not any(e.endswith(".png") for e in found)
    assert clean_email("abc123def456abc789@sentry.wixpress.com") is None
    assert clean_email("prenom.nom@domaine.fr") is None
    assert clean_email("MAILTO:Hello@Agence.FR?subject=hi") == "hello@agence.fr"


def test_cloudflare_protected_email_is_decoded():
    # "a@b.fr" XOR-encoded with key 0x42
    key = 0x42
    encoded = f"{key:02x}" + "".join(f"{ord(c) ^ key:02x}" for c in "jean@agence.fr")
    html = f'<html><body><a href="/cdn-cgi/l/email-protection#{encoded}">[email&#160;protected]</a></body></html>'
    assert parse_html(html, "https://agence.fr/").emails == ["jean@agence.fr"]


def test_phones_are_normalized():
    page = parse_html(_read("agence_lumiere/home.html"), "http://agence-lumiere.fr/", is_home=True)
    assert page.phones == ["+33123456789"]
    assert normalize_phone("+33 (0)6 12 34 56 78") == "+33612345678"
    assert normalize_phone("0033 6 12 34 56 78") == "+33612345678"
    assert normalize_phone("06.12.34.56.78") == "+33612345678"
    assert normalize_phone("+44 20 7946 0958") == "+442079460958"


def test_social_links_ignore_share_and_intent_links():
    page = parse_html(_read("agence_lumiere/home.html"), "http://agence-lumiere.fr/", is_home=True)
    social = page.links["social"]
    assert social == {
        "instagram": "https://www.instagram.com/agencelumiere/",
        "linkedin": "https://www.linkedin.com/company/agence-lumiere/",
        "facebook": "https://www.facebook.com/agencelumiere",
    }
    assert "x" not in social
    assert not any("sharer" in e["url"] or "intent" in e["url"] for e in page.links["external"])
    internal = [link["url"] for link in page.links["internal"]]
    assert "http://agence-lumiere.fr/equipe" in internal and "http://agence-lumiere.fr/mentions-legales" in internal
    assert len(internal) == len(set(internal))
    team = parse_html(_read("agence_lumiere/equipe.html"), "http://agence-lumiere.fr/equipe")
    assert team.links["people_profiles"][0]["url"] == "https://www.linkedin.com/in/claire-fontaine-12345/"


def test_jsonld_graph_is_flattened():
    page = parse_html(_read("agence_lumiere/home.html"), "http://agence-lumiere.fr/", is_home=True)
    types = [o.get("@type") for o in page.structured_data]
    assert "Organization" in types and "WebSite" in types
    org = next(o for o in page.structured_data if o.get("@type") == "Organization")
    assert org["founder"][0]["name"] == "Claire Fontaine"


def test_legal_notice_page_keeps_one_fact_per_line():
    page = parse_html(_read("agence_lumiere/mentions-legales.html"), "http://agence-lumiere.fr/mentions-legales")
    lines = page.content_text.split("\n")
    assert "RCS Paris B 812 345 676" in lines
    assert "Directeur de la publication : M. Julien Moreau" in lines
    assert "Siège social : 12 rue des Lilas, 75011 Paris" in lines


def test_head_html_only_for_home_and_contains_fingerprints():
    html = _read("agence_lumiere/home.html")
    home = parse_html(html, "http://agence-lumiere.fr/", is_home=True)
    assert home.head_html is not None and len(home.head_html.encode()) <= 48_000
    assert 'content="WordPress 6.6.2"' in home.head_html
    assert "GTM-ABC1234" in home.head_html
    assert "js.hs-scripts.com" in home.head_html  # body <script src> kept
    assert "elementor-default" in home.head_html  # <body class> kept
    assert "youtube.com/embed" in home.head_html  # body iframes kept
    assert home.generator and "WordPress" in home.generator and "Elementor" in home.generator
    assert parse_html(html, "http://agence-lumiere.fr/").head_html is None


def test_language_fallbacks():
    assert parse_html("<html><head><meta property='og:locale' content='de_DE'></head><body>x</body></html>", "https://a.de/").language == "de"
    en = "<html><body><p>We are a creative agency and we help our clients with their brand and their website for the long term.</p></body></html>"
    assert parse_html(en, "https://a.com/").language == "en"
    fr = "<html><body><p>Nous sommes une agence créative et nous accompagnons nos clients pour leur marque et leur site dans la durée.</p></body></html>"
    assert parse_html(fr, "https://a.fr/").language == "fr"


def test_broken_html_and_hidden_elements():
    html = "<html><body><div><p>Visible<div style='display:none'>Hidden text</div><p hidden>Also hidden</p><td>cell</table></span><p>Tail"
    page = parse_html(html, "https://x.fr/")
    assert "Visible" in page.content_text and "Tail" in page.content_text
    assert "Hidden text" not in page.content_text and "Also hidden" not in page.content_text
    assert parse_html("", "https://x.fr/").content_text == ""


def test_text_is_capped():
    html = "<html><body>" + "".join(f"<p>Paragraphe numéro {i} avec du texte unique {i * 7}</p>" for i in range(5000)) + "</body></html>"
    page = parse_html(html, "https://x.fr/")
    assert len(page.content_text.encode("utf-8")) <= MAX_TEXT_BYTES
