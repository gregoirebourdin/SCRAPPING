"""RDAP contacts: redaction / privacy-proxy filtering, registrant organisation, fetch (respx, no network)."""

from __future__ import annotations

import httpx
import pytest
import respx

from scout.db.enums import EmailEvidenceSource
from scout.discovery.common import Throttle
from scout.email.intel import rdap


@pytest.fixture(autouse=True)
def _no_throttle(monkeypatch):
    monkeypatch.setattr(rdap, "_throttle", Throttle(0))


def vcard(*props):
    return ["vcard", [["version", {}, "text", "4.0"], *props]]


def entity(roles, *props, entities=None):
    out = {"objectClassName": "entity", "roles": roles, "vcardArray": vcard(*props)}
    if entities:
        out["entities"] = entities
    return out


REDACTED_GTLD = {
    "objectClassName": "domain",
    "ldhName": "acme.com",
    "links": [{"rel": "self", "href": "https://rdap.verisign.com/com/v1/domain/ACME.COM"}],
    "entities": [
        entity(
            ["registrar"],
            ["fn", {}, "text", "Example Registrar, Inc."],
            entities=[
                entity(
                    ["abuse"], ["fn", {}, "text", "Abuse"], ["email", {}, "text", "abuse@registrar.example"]
                )
            ],
        ),
        entity(
            ["registrant"],
            ["fn", {}, "text", "REDACTED FOR PRIVACY"],
            ["org", {}, "text", "Privacy service provided by Withheld for Privacy ehf"],
            ["email", {}, "text", "6f1c2a@withheldforprivacy.com"],
        ),
        entity(
            ["administrative", "technical"],
            ["fn", {}, "text", "Redacted for Privacy"],
            ["email", {}, "text", "redacted@acme.com"],  # on-domain but redacted
            ["email", {}, "text", "Please query the RDDS service of the Registrar of Record"],
        ),
    ],
    "remarks": [{"title": "REDACTED FOR PRIVACY", "description": ["..."]}],
}

AFNIC_PUBLISHED = {
    "objectClassName": "domain",
    "ldhName": "acme.fr",
    "entities": [
        entity(["registrar"], ["fn", {}, "text", "OVH"], ["email", {}, "text", "support@ovh.net"]),
        entity(
            ["registrant"],
            ["fn", {}, "text", "ACME SAS"],
            ["kind", {}, "text", "org"],
            ["org", {}, "text", "ACME SAS"],
            ["email", {}, "text", "domains@acme.fr"],
        ),
        entity(
            ["administrative"],
            ["fn", {}, "text", "Jean Dupont"],
            ["kind", {}, "text", "individual"],
            ["email", {}, "text", "Jean.Dupont@acme.fr"],
        ),
        entity(
            ["technical"],
            ["fn", {}, "text", "Hostmaster"],
            ["email", {}, "text", "hostmaster@acme.fr"],
            ["email", {}, "text", "noc@acme.fr"],
        ),
        entity(["billing"], ["fn", {}, "text", "Marie Curie"], ["email", {}, "text", "billing.team@acme.fr"]),
    ],
}

PROXY = {
    "entities": [
        entity(
            ["registrant"],
            ["org", {}, "text", "Domains By Proxy, LLC"],
            ["email", {}, "text", "acme.fr@domainsbyproxy.com"],
        ),
        entity(
            ["technical"], ["fn", {}, "text", "Data Protected"], ["email", {}, "text", "privacy-tech@acme.fr"]
        ),
    ]
}


def test_redacted_gtld_yields_nothing_personal():
    res = rdap.parse_rdap("acme.com", REDACTED_GTLD, source_url="https://rdap.org/domain/acme.com")
    assert res.emails == [] and res.registrant_org is None
    assert res.registrar == "Example Registrar, Inc."
    assert res.source_url == "https://rdap.verisign.com/com/v1/domain/ACME.COM"  # self link wins


def test_published_contacts_keep_on_domain_addresses_and_registrant_org():
    res = rdap.parse_rdap("acme.fr", AFNIC_PUBLISHED, source_url="https://rdap.nic.fr/domain/acme.fr")
    assert res.registrant_org == "ACME SAS" and res.registrar == "OVH"
    found = {o.address: o for o in res.emails}
    assert set(found) == {
        "domains@acme.fr",
        "jean.dupont@acme.fr",
        "hostmaster@acme.fr",
        "noc@acme.fr",
        "billing.team@acme.fr",
    }
    assert all(o.source == EmailEvidenceSource.rdap for o in found.values())
    assert all(o.source_url == "https://rdap.nic.fr/domain/acme.fr" for o in found.values())
    jean = found["jean.dupont@acme.fr"]
    assert (jean.first_name, jean.last_name, jean.pattern, jean.is_role) == (
        "Jean",
        "Dupont",
        "{first}.{last}",
        False,
    )
    for role in ("domains@acme.fr", "hostmaster@acme.fr", "noc@acme.fr"):
        assert found[role].is_role and found[role].first_name is None
    # A published name that does not render the address is not attached to it.
    billing = found["billing.team@acme.fr"]
    assert billing.first_name is None and billing.pattern is None


def test_privacy_proxy_organisation_and_addresses_are_ignored():
    res = rdap.parse_rdap("acme.fr", PROXY)
    assert res.registrant_org is None and res.emails == []


def test_malformed_payloads():
    assert rdap.parse_rdap("acme.fr", {}).emails == []
    weird = {"entities": [{"roles": ["registrant"], "vcardArray": ["vcard"]}, "x", {"entities": "nope"}]}
    assert rdap.parse_rdap("acme.fr", weird).emails == []


@respx.mock
async def test_fetch_follows_bootstrap_redirect():
    respx.get("https://rdap.org/domain/acme.fr").mock(
        return_value=httpx.Response(302, headers={"location": "https://rdap.nic.fr/domain/acme.fr"})
    )
    registry = respx.get("https://rdap.nic.fr/domain/acme.fr").mock(
        return_value=httpx.Response(200, json=AFNIC_PUBLISHED)
    )
    res = await rdap.fetch_rdap("ACME.fr")
    assert res.status == "ok" and res.completed and registry.call_count == 1
    assert res.source_url == "https://rdap.nic.fr/domain/acme.fr" and res.registrant_org == "ACME SAS"
    assert "application/rdap+json" in registry.calls.last.request.headers["accept"]


@respx.mock
async def test_fetch_statuses():
    respx.get("https://rdap.org/domain/unknown.fr").mock(return_value=httpx.Response(404))
    respx.get("https://rdap.org/domain/busy.fr").mock(return_value=httpx.Response(429))
    respx.get("https://rdap.org/domain/broken.fr").mock(return_value=httpx.Response(503))
    respx.get("https://rdap.org/domain/html.fr").mock(return_value=httpx.Response(200, text="<html>"))
    nf = await rdap.fetch_rdap("unknown.fr")
    assert nf.status == "not_found" and nf.completed
    busy = await rdap.fetch_rdap("busy.fr")
    assert busy.status == "rate_limited" and not busy.completed
    broken = await rdap.fetch_rdap("broken.fr")
    assert broken.status == "error" and broken.error
    assert (await rdap.fetch_rdap("html.fr")).status == "error"
    assert (await rdap.fetch_rdap("not a domain")).status == "error"
