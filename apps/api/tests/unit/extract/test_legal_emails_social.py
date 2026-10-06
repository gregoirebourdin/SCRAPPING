"""Legal notice parsing (FR/DE/UK), email classification/matching and social URL normalization."""

from __future__ import annotations

import pytest

from scout.extract.emails import classify_email, emails_on_domain, match_email_to_person
from scout.extract.legal import fr_vat_from_siren, luhn_valid, parse_legal_notice, valid_siren, valid_siret
from scout.extract.social import extract_social_profiles, normalize_social_url
from tests.unit.extract.helpers import agence_pages

FR_NOTICE = """Mentions légales
Éditeur du site
Agence Lumière SAS au capital de 10 000 €
Siège social : 12 rue des Lilas, 75011 Paris
RCS Paris B 812 345 676
SIRET : 812 345 676 00017
N° TVA intracommunautaire : FR19812345676
Présidente : Claire Fontaine
Directeur de la publication : M. Julien Moreau
Hébergeur : OVH SAS, 2 rue Kellermann, 59100 Roubaix
Photographies : Studio Pixel"""


def test_siren_luhn_and_vat():
    assert luhn_valid("812345676") and valid_siren("812 345 676")
    assert not valid_siren("812345674") and not valid_siren("12345678") and not valid_siren("000000000")
    assert valid_siret("81234567600017")
    assert fr_vat_from_siren("812345676") == "FR19812345676"


def test_parse_fr_legal_notice():
    info = parse_legal_notice(FR_NOTICE)
    assert info.siren == "812345676"
    assert info.siret == "81234567600017"
    assert info.rcs == "RCS Paris B 812 345 676"
    assert info.vat_number == "FR19812345676"
    assert info.share_capital == "10 000 €"
    assert info.legal_name == "Agence Lumière" and info.legal_form == "SAS"
    assert info.publication_director == "Julien Moreau"
    assert info.legal_representative == "Claire Fontaine" and info.legal_representative_role == "Présidente"
    assert info.address == "12 rue des Lilas, 75011 Paris"
    assert info.country == "FR"
    assert {(p.name, p.role) for p in info.people} == {
        ("Claire Fontaine", "Présidente"),
        ("Julien Moreau", "Directeur de la publication"),
    }
    assert info.evidence["siren"] == "RCS Paris B 812 345 676"
    assert "Julien Moreau" in info.evidence["publication_director"]


def test_invalid_siren_is_rejected():
    info = parse_legal_notice("RCS Paris B 812 345 674\nSIREN : 123 456 789")
    assert info.siren is None


def test_inline_fr_notice_and_vat_derivation():
    text = (
        "Le site est édité par la société Studio Nova, SARL au capital de 5 000 euros, immatriculée au RCS de Lyon "
        "sous le numéro 853 456 788, représentée par Mme Claire Fontaine en qualité de gérante. "
        "Responsable de la publication : Paul Durand"
    )
    info = parse_legal_notice(text)
    assert info.siren == "853456788"
    assert info.legal_name == "Studio Nova" and info.legal_form == "SARL"
    assert info.legal_representative == "Claire Fontaine"
    assert info.publication_director == "Paul Durand"
    only_vat = parse_legal_notice("TVA : FR86 853456788")
    assert only_vat.siren == "853456788" and only_vat.vat_number == "FR86853456788"


def test_parse_german_impressum():
    text = """Impressum
Angaben gemäß § 5 TMG
Muster Digital GmbH
Musterstraße 1
10115 Berlin
Vertreten durch:
Geschäftsführer: Hans Müller
Registergericht: Amtsgericht Berlin-Charlottenburg
Registernummer: HRB 123456 B
Umsatzsteuer-Identifikationsnummer gemäß § 27 a Umsatzsteuergesetz: DE123456789"""
    info = parse_legal_notice(text)
    assert info.legal_name == "Muster Digital" and info.legal_form == "GmbH"
    assert info.legal_representative == "Hans Müller" and info.legal_representative_role == "Geschäftsführer"
    assert info.register_number == "HRB 123456 B, Amtsgericht Berlin-Charlottenburg"
    assert info.vat_number == "DE123456789"
    assert info.country == "DE"


def test_parse_uk_company_info():
    text = "Acme Studio Ltd is registered in England and Wales. Company number: 09876543\nRegistered office: 1 High Street, London EC1A 1BB\nManaging Director: Sarah Connor"
    info = parse_legal_notice(text)
    assert info.register_number == "09876543" and info.country == "GB"
    assert info.address == "1 High Street, London EC1A 1BB"
    assert info.legal_representative == "Sarah Connor"


def test_legal_notice_never_invents_people():
    info = parse_legal_notice("Directeur de la publication : la société Agence Lumière\nGérant : voir ci-dessous")
    assert info.people == [] and info.publication_director is None


@pytest.mark.parametrize(
    ("address", "kind"),
    [
        ("contact@agence-lumiere.fr", "role"),
        ("info.paris@x.fr", "role"),
        ("jobs2024@x.fr", "role"),
        ("recrutement@x.fr", "role"),
        ("noreply@x.fr", "role"),
        ("hello@x.io", "role"),
        ("jean.dupont@agence-lumiere.fr", "person"),
        ("j.dupont@x.fr", "person"),
        ("marie@agence-lumiere.fr", "person"),
        ("lumiere@agence-lumiere.fr", "generic"),
        ("jdupont@x.fr", "generic"),
    ],
)
def test_classify_email(address, kind):
    assert classify_email(address) == kind


def test_match_email_to_person():
    assert match_email_to_person("jean.dupont@x.fr", "Jean", "Dupont") == 1.0
    assert match_email_to_person("Jean.Dupont@X.fr", "Jean", "Dupont") == 1.0
    assert match_email_to_person("dupont.jean@x.fr", "Jean", "Dupont") == 0.95
    assert match_email_to_person("jdupont@x.fr", "Jean", "Dupont") == 0.9
    assert match_email_to_person("jean@x.fr", "Jean", "Dupont") == 0.8
    assert match_email_to_person("helene.lefevre@x.fr", "Hélène", "Lefèvre") == 1.0
    assert match_email_to_person("jeanpierre.martin@x.fr", "Jean-Pierre", "Martin") == 1.0
    assert match_email_to_person("contact@x.fr", "Jean", "Dupont") == 0.0
    assert match_email_to_person("marie@x.fr", "Jean", "Dupont") == 0.0


def test_emails_on_domain():
    emails = ["A@Agence-Lumiere.fr", "b@gmail.com", "c@mail.agence-lumiere.fr", "a@agence-lumiere.fr", "bad"]
    assert emails_on_domain(emails, "agence-lumiere.fr") == ["a@agence-lumiere.fr", "c@mail.agence-lumiere.fr"]
    assert emails_on_domain(emails, None) == []


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.instagram.com/agencelumiere/", ("instagram", "https://www.instagram.com/agencelumiere/")),
        ("https://instagram.com/AgenceLumiere?hl=fr", ("instagram", "https://www.instagram.com/agencelumiere/")),
        ("https://www.instagram.com/p/C1234abcd/", None),
        ("https://fr-fr.facebook.com/agencelumiere/", ("facebook", "https://www.facebook.com/agencelumiere")),
        ("https://www.facebook.com/sharer/sharer.php?u=x", None),
        ("https://www.facebook.com/profile.php?id=100064", ("facebook", "https://www.facebook.com/profile.php?id=100064")),
        ("https://fr.linkedin.com/company/agence-lumiere", ("linkedin", "https://www.linkedin.com/company/agence-lumiere/")),
        ("https://www.linkedin.com/in/claire-fontaine-12345", ("linkedin_profile", "https://www.linkedin.com/in/claire-fontaine-12345/")),
        ("https://www.linkedin.com/shareArticle?mini=true&url=x", None),
        ("https://twitter.com/agencelumiere", ("x", "https://x.com/agencelumiere")),
        ("https://twitter.com/intent/tweet?text=hi", None),
        ("https://x.com/agencelumiere/status/123456", None),
        ("https://www.tiktok.com/@agencelumiere", ("tiktok", "https://www.tiktok.com/@agencelumiere")),
        ("https://www.tiktok.com/@agencelumiere/video/123", None),
        ("https://www.youtube.com/@AgenceLumiere", ("youtube", "https://www.youtube.com/@AgenceLumiere")),
        ("https://www.youtube.com/channel/UC123abc", ("youtube", "https://www.youtube.com/channel/UC123abc")),
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", None),
        ("https://www.pinterest.fr/agencelumiere/", ("pinterest", "https://www.pinterest.com/agencelumiere/")),
        ("https://www.pinterest.com/pin/create/button/?url=x", None),
        ("https://www.threads.net/@agencelumiere", ("threads", "https://www.threads.net/@agencelumiere")),
        ("https://agence-lumiere.fr/contact", None),
        ("mailto:contact@agence-lumiere.fr", None),
    ],
)
def test_normalize_social_url(url, expected):
    assert normalize_social_url(url) == expected


def test_extract_social_profiles_prefers_home():
    profiles = extract_social_profiles(agence_pages())
    assert profiles["instagram"] == ("https://www.instagram.com/agencelumiere/", "http://agence-lumiere.fr/")
    assert profiles["linkedin"][0] == "https://www.linkedin.com/company/agence-lumiere/"
    assert "linkedin_profile" not in profiles
