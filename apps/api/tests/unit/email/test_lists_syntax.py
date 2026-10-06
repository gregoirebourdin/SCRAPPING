"""Static lists (disposable / free / role) and address syntax helpers."""

from __future__ import annotations

import pytest

from scout.email.lists import (
    DISPOSABLE_DOMAINS,
    FREE_PROVIDERS,
    ROLE_LOCAL_PARTS,
    is_disposable_domain,
    is_free_provider,
    is_role_local_part,
)
from scout.email.syntax import is_valid_syntax, normalize_address, normalize_domain


def test_list_sizes_and_disjointness():
    assert len(DISPOSABLE_DOMAINS) >= 300
    assert len(FREE_PROVIDERS) >= 120
    assert not DISPOSABLE_DOMAINS & FREE_PROVIDERS
    assert all(d == d.lower() and "." in d for d in DISPOSABLE_DOMAINS | FREE_PROVIDERS)


@pytest.mark.parametrize(
    "domain",
    [
        "gmail.com",
        "googlemail.com",
        "yahoo.fr",
        "outlook.com",
        "hotmail.fr",
        "live.fr",
        "msn.com",
        "icloud.com",
        "me.com",
        "aol.com",
        "proton.me",
        "protonmail.com",
        "gmx.de",
        "web.de",
        "orange.fr",
        "wanadoo.fr",
        "free.fr",
        "sfr.fr",
        "laposte.net",
        "bbox.fr",
        "numericable.fr",
        "neuf.fr",
        "club-internet.fr",
        "libero.it",
        "virgilio.it",
        "t-online.de",
        "yandex.ru",
        "mail.ru",
        "zoho.com",
        "yahoo.com.mx",
        "gmx.es",
    ],
)
def test_free_providers(domain):
    assert is_free_provider(domain)


@pytest.mark.parametrize(
    "domain", ["agence-x.fr", "orange-business.com", "google.com", "yahoo-group.example.fr"]
)
def test_not_free_providers(domain):
    assert not is_free_provider(domain)


def test_disposable_including_subdomains():
    assert is_disposable_domain("yopmail.com")
    assert is_disposable_domain("Mailinator.com")
    assert is_disposable_domain("inbox.mailinator.com")
    assert not is_disposable_domain("gmail.com")
    assert not is_disposable_domain("agence-x.fr")


@pytest.mark.parametrize(
    "local",
    [
        "contact",
        "info",
        "hello",
        "bonjour",
        "salut",
        "support",
        "sales",
        "admin",
        "office",
        "team",
        "jobs",
        "careers",
        "recrutement",
        "rh",
        "hr",
        "compta",
        "comptabilite",
        "billing",
        "facturation",
        "noreply",
        "no-reply",
        "marketing",
        "presse",
        "press",
        "communication",
        "commercial",
        "devis",
        "webmaster",
        "postmaster",
        "abuse",
        "service-client",
        "service.client",
        "serviceclient",
        "sav",
        "accueil",
        "direction",
        "agence",
        "studio",
        "booking",
        "reservation",
        "info.paris",
        "contact-lyon",
        "Contact+site",
    ],
)
def test_role_local_parts(local):
    assert is_role_local_part(local)


@pytest.mark.parametrize("local", ["marie.dupont", "jdupont", "marie", "contactless", "infographiste", ""])
def test_person_local_parts(local):
    assert not is_role_local_part(local)


def test_role_vocabulary_is_lowercase():
    assert all(r == r.lower() for r in ROLE_LOCAL_PARTS)


def test_normalize_address():
    assert normalize_address("  Marie.Dupont@Agence-X.FR ") == "marie.dupont@agence-x.fr"
    assert normalize_address("mailto:Marie@Agence-X.fr?subject=Hi") == "marie@agence-x.fr"
    assert normalize_address("<jean@agence-x.fr>") == "jean@agence-x.fr"
    assert normalize_address("contact@société.fr") == "contact@xn--socit-esab.fr"
    for bad in (None, "", "marie", "a@b@c.fr", "@agence.fr", "marie@", "marie@localhost"):
        assert normalize_address(bad) is None


def test_normalize_domain():
    assert normalize_domain(" Agence-X.FR. ") == "agence-x.fr"
    assert normalize_domain("société.fr") == "xn--socit-esab.fr"
    assert normalize_domain("localhost") is None
    assert normalize_domain("a b.fr") is None


@pytest.mark.parametrize(
    ("addr", "ok"),
    [
        ("marie@agence-x.fr", True),
        ("marie.dupont+news@agence-x.fr", True),
        ("marie..x@agence-x.fr", False),
        ("@agence-x.fr", False),
        ("marie@agence", False),
        ("marie dupont@agence-x.fr", False),
        (None, False),
    ],
)
def test_is_valid_syntax(addr, ok):
    assert is_valid_syntax(addr) is ok
