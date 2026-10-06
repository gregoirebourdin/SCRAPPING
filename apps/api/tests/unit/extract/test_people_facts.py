"""People extraction (team cards, JSON-LD, legal notice, text patterns) and company facts."""

from __future__ import annotations

from scout.db.enums import PageType
from scout.extract.company_info import extract_company_facts
from scout.extract.people import extract_people
from tests.unit.extract.helpers import agence_pages, page_from_html


def _people():
    return {p.full_name: p for p in extract_people(agence_pages(), company_name="Agence Lumière", domain="agence-lumiere.fr")}


def test_team_page_and_legal_notice_people():
    people = _people()
    assert set(people) == {"Claire Fontaine", "Julien Moreau", "Sarah Benali", "Thomas Petit", "Inès Garnier"}
    claire = people["Claire Fontaine"]
    assert claire.title == "Co-fondatrice & Directrice générale"
    assert claire.first_name == "Claire" and claire.last_name == "Fontaine"
    assert claire.confidence >= 0.9  # corroborated by JSON-LD + team card + legal notice
    assert "Présidente" in claire.extra.get("other_titles", "")
    assert claire.email == "claire.fontaine@agence-lumiere.fr"  # published, obfuscated on the contact page
    assert claire.profile_url == "https://www.linkedin.com/in/claire-fontaine-12345/"
    julien = people["Julien Moreau"]
    assert julien.title == "Co-fondateur & Directeur artistique"
    assert "legal_notice" in julien.extra["methods"] and "team_card" in julien.extra["methods"]
    assert julien.email == "julien.moreau@agence-lumiere.fr"
    sarah = people["Sarah Benali"]
    assert sarah.title == "Cheffe de projet" and sarah.method == "team_card" and sarah.confidence == 0.88
    assert sarah.evidence == "Sarah Benali\nCheffe de projet"
    assert sarah.source_url == "http://agence-lumiere.fr/equipe"
    assert people["Thomas Petit"].title == "Community manager"
    for p in people.values():
        assert p.evidence and p.source_url and 0 < p.confidence <= 0.97


def test_no_testimonial_authors_clients_or_blog_authors():
    people = _people()
    for outsider in ("Sophie Bernard", "Marc Lefebvre", "Lucie Martin", "Boulangerie Martin", "Studio Pixel"):
        assert outsider not in people


def test_legal_notice_alone():
    pages = [p for p in agence_pages() if p.page_type == PageType.legal]
    people = extract_people(pages, company_name="Agence Lumière", domain="agence-lumiere.fr")
    roles = {p.full_name: (p.title, p.method, p.confidence) for p in people}
    assert roles == {
        "Claire Fontaine": ("Présidente", "legal_notice", 0.9),
        "Julien Moreau": ("Directeur de la publication", "legal_notice", 0.9),
    }


def test_inline_patterns_and_title_above_layout():
    html = """<html><body><main><h1>Notre équipe</h1>
    <p>Directrice générale</p><p>Camille Roux</p>
    <p>Responsable marketing</p><p>Hugo Lambert</p>
    <p>Nicolas Fabre – Directeur commercial</p>
    <p>Fondatrice : Léa Moreau</p>
    <p>Paul Girard (CTO)</p>
    <p>Élodie Martin</p>
    </main></body></html>"""
    page = page_from_html(html, "https://studio-nova.fr/equipe")
    people = {p.full_name: p for p in extract_people([page], company_name="Studio Nova", domain="studio-nova.fr")}
    assert people["Camille Roux"].title == "Directrice générale"
    assert people["Hugo Lambert"].title == "Responsable marketing"
    assert people["Nicolas Fabre"].title == "Directeur commercial"
    assert people["Léa Moreau"].title == "Fondatrice"
    assert people["Paul Girard"].title == "CTO"
    assert people["Élodie Martin"].title is None and people["Élodie Martin"].confidence == 0.6


def test_text_patterns_and_other_company_guard():
    html = """<html><body><main>
    <p>Studio Nova a été fondé en 2018 par Antoine Lefort et Julie Bernard à Nantes.</p>
    <p>Notre fondatrice, Julie Bernard, anime aussi un podcast.</p>
    <p>Marc Dubois, CEO de Boulangerie Dubois, nous a confié sa refonte.</p>
    </main></body></html>"""
    page = page_from_html(html, "https://studio-nova.fr/a-propos")
    people = {p.full_name: p for p in extract_people([page], company_name="Studio Nova", domain="studio-nova.fr")}
    assert set(people) == {"Antoine Lefort", "Julie Bernard"}
    assert people["Antoine Lefort"].method == "text_pattern" and people["Antoine Lefort"].confidence == 0.75
    assert people["Julie Bernard"].confidence == 0.75  # same page & method: no corroboration bonus


def test_jsonld_people_and_blog_author_rule():
    about = page_from_html(
        """<html><head><script type="application/ld+json">{"@type":"Person","name":"Nadia Haddad","jobTitle":"CEO",
        "worksFor":{"@type":"Organization","name":"Studio Nova"},"email":"mailto:nadia@studio-nova.fr"}</script></head>
        <body><p>À propos</p></body></html>""",
        "https://studio-nova.fr/a-propos",
    )
    blog = page_from_html(
        """<html><head><script type="application/ld+json">{"@type":"BlogPosting","author":{"@type":"Person","name":"Kevin Durand"}}</script>
        <script type="application/ld+json">{"@type":"Person","name":"Sami Kaci","jobTitle":"Head of Growth"}</script></head>
        <body><p>Article</p></body></html>""",
        "https://studio-nova.fr/blog",
    )
    people = {p.full_name: p for p in extract_people([about, blog], company_name="Studio Nova", domain="studio-nova.fr")}
    assert people["Nadia Haddad"].method == "jsonld" and people["Nadia Haddad"].email == "nadia@studio-nova.fr"
    assert "Sami Kaci" in people  # blog page person kept only because the title says so
    assert "Kevin Durand" not in people


def test_company_facts():
    facts = {f.field_name: f for f in extract_company_facts(agence_pages())}
    assert facts["registry_id"].value == "812345676" and facts["registry_id"].confidence >= 0.9
    assert facts["registry_id"].source_url == "http://agence-lumiere.fr/mentions-legales"
    assert facts["vat_number"].value == "FR19812345676"
    assert facts["legal_name"].value == "Agence Lumière"
    assert facts["employee_count"].value == {"min": 12, "max": 12}
    assert "12 personnes" in facts["employee_count"].evidence
    assert facts["founded_year"].value == 2015
    assert facts["phone"].value == "+33123456789"
    assert facts["email"].value == "contact@agence-lumiere.fr"
    assert facts["postal_code"].value == "75011" and facts["city"].value == "Paris"
    assert facts["address"].value == "12 rue des Lilas, 75011 Paris"
    assert facts["description"].value.startswith("Agence Lumière est une agence de communication")
    assert facts["social_instagram"].value == "https://www.instagram.com/agencelumiere/"
    assert facts["social_linkedin"].value == "https://www.linkedin.com/company/agence-lumiere/"
    for f in facts.values():
        assert f.evidence and f.source_url and f.source_type == "website"


def test_employee_count_variants():
    html = """<html><body><main>
    <p>Plus de 50 collaborateurs passionnés.</p>
    <p>Entre 10 et 20 consultants interviennent sur vos projets.</p>
    <p>We are a team of 25 people.</p>
    <p>Nous avons réalisé 300 projets pour 120 clients.</p>
    </main></body></html>"""
    page = page_from_html(html, "https://x.fr/a-propos")
    from scout.extract.company_info import _employee_facts

    values = [f.value for f in _employee_facts(page)]
    assert {"min": 50, "max": None} in values
    assert {"min": 10, "max": 20} in values
    assert {"min": 25, "max": 25} in values
    assert len(values) == 3  # "300 projets" / "120 clients" are not headcounts


def test_name_without_title_never_steals_neighbour_title():
    html = """<html><body><main><h1>Équipe</h1>
    <p>Jean Dupont</p><p>CEO</p>
    <p>Marie Martin</p>
    <p>Paul Durand</p><p>Directeur commercial</p>
    </main></body></html>"""
    page = page_from_html(html, "https://studio-nova.fr/equipe")
    people = {p.full_name: p for p in extract_people([page], company_name="Studio Nova", domain="studio-nova.fr")}
    assert people["Jean Dupont"].title == "CEO"
    assert people["Paul Durand"].title == "Directeur commercial"
    assert people["Marie Martin"].title is None and people["Marie Martin"].confidence == 0.6
