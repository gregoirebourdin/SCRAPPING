"""Mail provider detection from MX hosts and provider notes (pure)."""

from __future__ import annotations

import pytest

from scout.db.enums import MailProvider as P
from scout.email.intel.providers import detect_provider, provider_notes, smtp_uninformative_hosts


@pytest.mark.parametrize(
    ("hosts", "expected"),
    [
        # Google Workspace
        (["aspmx.l.google.com", "alt1.aspmx.l.google.com"], P.google_workspace),
        (["ASPMX.L.GOOGLE.COM."], P.google_workspace),
        (["aspmx2.googlemail.com"], P.google_workspace),
        (["smtp.google.com"], P.google_workspace),
        (["gmail-smtp-in.l.google.com"], P.google_workspace),
        # Microsoft 365
        (["acme-fr.mail.protection.outlook.com"], P.microsoft_365),
        (["eur.olc.protection.outlook.com"], P.microsoft_365),
        (["acme-com.l-v1.mx.microsoft"], P.microsoft_365),
        # Zoho
        (["mx.zoho.eu", "mx2.zoho.eu"], P.zoho),
        (["mx.zoho.com"], P.zoho),
        (["mx3.zoho.com.au"], P.zoho),
        (["mx.zohomail.com"], P.zoho),
        # OVH
        (["mx1.mail.ovh.net", "mx2.mail.ovh.net"], P.ovh),
        (["mx0.ovh.net"], P.ovh),
        # IONOS
        (["mx00.ionos.fr", "mx01.ionos.fr"], P.ionos),
        (["mx00.ionos.de"], P.ionos),
        (["mx00.kundenserver.de"], P.ionos),
        (["mx00.1and1.co.uk"], P.ionos),
        # Gandi
        (["spool.mail.gandi.net", "fb.mail.gandi.net"], P.gandi),
        # Infomaniak
        (["mta-gw.infomaniak.ch"], P.infomaniak),
        (["mta-gw2.infomaniak.ch"], P.infomaniak),
        (["mxpool.infomaniak.com"], P.infomaniak),
        # o2switch
        (["acme.o2switch.net"], P.o2switch),
        # Proton
        (["mail.protonmail.ch", "mailsec.protonmail.ch"], P.proton),
        # Yandex
        (["mx.yandex.net"], P.yandex),
        (["mx.yandex.ru"], P.yandex),
        # Fastmail
        (["in1-smtp.messagingengine.com", "in2-smtp.messagingengine.com"], P.fastmail),
        # iCloud
        (["mx01.mail.icloud.com", "mx02.mail.icloud.com"], P.icloud),
        # Amazon SES inbound
        (["inbound-smtp.eu-west-1.amazonaws.com"], P.amazon_ses),
        # Mailgun
        (["mxa.mailgun.org", "mxb.mailgun.org"], P.mailgun),
        (["mxa.eu.mailgun.org"], P.mailgun),
        # Secure gateways
        (["eu-smtp-inbound-1.mimecast.com"], P.secure_gateway),
        (["mx1-eu1.ppe-hosted.com"], P.secure_gateway),
        (["mx0a-001b2d01.pphosted.com"], P.secure_gateway),
        (["d123.ess.barracudanetworks.com"], P.secure_gateway),
        (["cluster1.eu.messagelabs.com"], P.secure_gateway),
        (["in.hes.trendmicro.eu"], P.secure_gateway),
        (["in.hes.trendmicro.com"], P.secure_gateway),
        (["mx-01-eu-central-1.prod.hydra.sophos.com"], P.secure_gateway),
        (["mx1.hc1234-56.iphmx.com"], P.secure_gateway),
        # A gateway in front of Microsoft 365 owns the RCPT behaviour.
        (["eu-smtp-inbound-1.mimecast.com", "acme.mail.protection.outlook.com"], P.secure_gateway),
        # Nothing to deliver to
        ([], P.none),
        ([""], P.none),
    ],
)
def test_detect_provider(hosts, expected):
    assert detect_provider(hosts) == expected


def test_first_known_host_wins_by_preference():
    assert detect_provider(["mx.unknown-filter.example", "aspmx.l.google.com"]) == P.google_workspace
    assert detect_provider(["aspmx.l.google.com", "mx1.mail.ovh.net"]) == P.google_workspace


def test_self_hosted_and_unknown():
    assert detect_provider(["mail.acme.fr"], domain="acme.fr") == P.self_hosted
    assert detect_provider(["acme.fr"], domain="Acme.FR.") == P.self_hosted
    assert detect_provider(["mx.some-isp.example"], domain="acme.fr") == P.unknown
    assert detect_provider(["mail.acme.fr"]) == P.unknown  # domain unknown → cannot tell
    # Look-alikes do not match.
    assert detect_provider(["google.com.evil.example"]) == P.unknown
    assert detect_provider(["notmimecast.com"]) == P.unknown


def test_provider_notes_cover_every_provider():
    for provider in P:
        notes = provider_notes(provider)
        assert notes["provider"] == provider.value and notes["label"]
        assert 0.0 <= notes["catch_all_prior"] <= 1.0
        assert isinstance(notes["smtp_uninformative"], bool)


def test_provider_notes_semantics():
    google = provider_notes(P.google_workspace)
    assert google["rcpt_reliable"] is True and google["catch_all_prior"] < 0.1
    assert "5.1.1" in google["notes"]
    gateway = provider_notes(P.secure_gateway)
    assert gateway["smtp_uninformative"] and gateway["rcpt_reliable"] is False
    assert gateway["catch_all_prior"] >= 0.5
    m365 = provider_notes(P.microsoft_365)
    assert google["catch_all_prior"] < m365["catch_all_prior"] < gateway["catch_all_prior"]
    assert provider_notes(P.none)["catch_all_prior"] == 0.0


def test_consumer_mx_is_smtp_uninformative():
    hosts = ["mta5.am0.yahoodns.net", "mta6.am0.yahoodns.net"]
    assert smtp_uninformative_hosts(hosts)
    assert detect_provider(hosts) == P.unknown
    notes = provider_notes(P.unknown, hosts)
    assert notes["smtp_uninformative"] and notes["rcpt_reliable"] is False
    assert provider_notes(P.unknown, ["mx-aol.mail.gm0.yahoodns.net"])["smtp_uninformative"]
    assert not smtp_uninformative_hosts(["aspmx.l.google.com"])
