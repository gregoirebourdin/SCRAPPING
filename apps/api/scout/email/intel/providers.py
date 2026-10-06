"""Mail provider detection from MX host names, and what each provider means for SMTP probing.

Pure functions, no I/O. The provider is a *prior* for the SMTP layer (catch-all likelihood, whether a
RCPT verdict is informative), never a verdict on a mailbox.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from scout.db.enums import MailProvider

# (provider, host regex) — first match wins; hosts are lowercase, without the trailing dot.
_RULES: tuple[tuple[MailProvider, re.Pattern[str]], ...] = tuple(
    (provider, re.compile(rx))
    for provider, rx in (
        # Secure email gateways first: they front another provider and own the RCPT behaviour.
        (MailProvider.secure_gateway, r"(^|\.)mimecast(-offshore)?\.(com|co\.za)$"),
        (MailProvider.secure_gateway, r"(^|\.)(pphosted|ppe-hosted)\.com$"),
        (MailProvider.secure_gateway, r"(^|\.)barracudanetworks\.com$"),
        (MailProvider.secure_gateway, r"(^|\.)messagelabs\.com$"),
        (MailProvider.secure_gateway, r"(^|\.)trendmicro\.[a-z]{2,3}(\.[a-z]{2})?$"),
        (MailProvider.secure_gateway, r"(^|\.)sophos\.com$"),
        (MailProvider.secure_gateway, r"(^|\.)iphmx\.com$"),
        (MailProvider.secure_gateway, r"(^|\.)(mailcontrol|hornetsecurity|fireeyecloud|vadesecure)\.com$"),
        (MailProvider.secure_gateway, r"(^|\.)(antispamcloud\.com|spamexperts\.(com|eu|net))$"),
        (MailProvider.secure_gateway, r"(^|\.)mailinblack\.com$"),
        # Hosted mailbox providers.
        (MailProvider.google_workspace, r"(^|\.)aspmx\.l\.google\.com$"),
        (MailProvider.google_workspace, r"(^|\.)googlemail\.com$"),
        (MailProvider.google_workspace, r"(^|\.)smtp\.google\.com$"),
        (MailProvider.google_workspace, r"\.l\.google\.com$"),  # gmail-smtp-in.l.google.com
        (MailProvider.microsoft_365, r"\.(mail|olc)\.protection\.outlook\.com$"),
        (MailProvider.microsoft_365, r"\.mail\.protection\.partner\.outlook\.cn$"),
        (MailProvider.microsoft_365, r"\.mx\.microsoft$"),
        (MailProvider.zoho, r"^mx\d*\.zoho(mail)?\.[a-z]{2,3}(\.[a-z]{2})?$"),
        (MailProvider.ovh, r"(^|\.)ovh\.(net|ca)$"),
        (MailProvider.ionos, r"^mx\d*\.ionos\.[a-z]{2,3}(\.[a-z]{2})?$"),
        (MailProvider.ionos, r"(^|\.)kundenserver\.de$"),
        (MailProvider.ionos, r"(^|\.)1and1\.[a-z]{2,3}(\.[a-z]{2})?$"),
        (MailProvider.gandi, r"(^|\.)gandi\.net$"),
        (MailProvider.infomaniak, r"(^|\.)infomaniak\.(ch|com)$"),
        (MailProvider.o2switch, r"(^|\.)o2switch\.net$"),
        (MailProvider.proton, r"(^|\.)protonmail\.ch$"),
        (MailProvider.yandex, r"^mx\.yandex\.[a-z]{2,3}$"),
        (MailProvider.fastmail, r"(^|\.)messagingengine\.com$"),
        (MailProvider.icloud, r"\.mail\.icloud\.com$"),
        (MailProvider.amazon_ses, r"^inbound-smtp\.[a-z0-9-]+\.amazonaws\.com$"),
        (MailProvider.mailgun, r"(^|\.)mailgun\.org$"),
    )
)

# Consumer webmail MX (Yahoo / AOL / Verizon): no MailProvider member, but RCPT is accept-then-bounce.
_UNINFORMATIVE_HOSTS = re.compile(r"(^|\.)(yahoodns\.net|yahoo\.com|aol\.com|verizon\.net)$")

_LABELS: dict[MailProvider, str] = {
    MailProvider.google_workspace: "Google Workspace",
    MailProvider.microsoft_365: "Microsoft 365",
    MailProvider.zoho: "Zoho Mail",
    MailProvider.ovh: "OVHcloud",
    MailProvider.ionos: "IONOS",
    MailProvider.gandi: "Gandi",
    MailProvider.infomaniak: "Infomaniak",
    MailProvider.o2switch: "o2switch",
    MailProvider.proton: "Proton Mail",
    MailProvider.yandex: "Yandex 360",
    MailProvider.fastmail: "Fastmail",
    MailProvider.icloud: "iCloud Mail",
    MailProvider.amazon_ses: "Amazon SES (inbound)",
    MailProvider.mailgun: "Mailgun (routes)",
    MailProvider.secure_gateway: "Secure email gateway",
    MailProvider.self_hosted: "Self-hosted mail server",
    MailProvider.none: "No mail server",
    MailProvider.unknown: "Unknown provider",
}

# provider → (catch_all_prior, rcpt_reliable, smtp_uninformative, greylisting_prior, note)
_NOTES: dict[MailProvider, tuple[float, bool | None, bool, float, str]] = {
    MailProvider.google_workspace: (
        0.03,
        True,
        False,
        0.01,
        "Rejects unknown recipients at RCPT (550 5.1.1); throttles unknown senders with 421 4.7.x.",
    ),
    MailProvider.microsoft_365: (
        0.25,
        True,
        False,
        0.02,
        "Usually rejects unknown recipients (550 5.4.1, directory-based edge blocking); tenants with "
        "edge blocking off or hybrid routing accept every RCPT.",
    ),
    MailProvider.zoho: (
        0.1,
        True,
        False,
        0.02,
        "Rejects unknown recipients unless a catch-all is configured.",
    ),
    MailProvider.ovh: (0.15, True, False, 0.05, "MX Plan / Exchange: rejects unknown recipients by default."),
    MailProvider.ionos: (0.2, True, False, 0.05, "Rejects unknown recipients; catch-all is a common option."),
    MailProvider.gandi: (0.15, True, False, 0.05, "Rejects unknown recipients; forwards/catch-all optional."),
    MailProvider.infomaniak: (0.15, True, False, 0.05, "Rejects unknown recipients by default."),
    MailProvider.o2switch: (
        0.3,
        None,
        False,
        0.15,
        "cPanel shared hosting: default address may swallow unknown recipients; greylisting possible.",
    ),
    MailProvider.proton: (0.1, True, False, 0.02, "Rejects unknown recipients; catch-all is opt-in."),
    MailProvider.yandex: (0.1, True, False, 0.02, "Rejects unknown recipients."),
    MailProvider.fastmail: (0.1, True, False, 0.02, "Rejects unknown recipients; catch-all is opt-in."),
    MailProvider.icloud: (0.05, True, False, 0.02, "Custom-domain iCloud Mail rejects unknown recipients."),
    MailProvider.amazon_ses: (
        0.6,
        False,
        True,
        0.0,
        "SES receipt rules usually accept every recipient: RCPT verdicts are uninformative.",
    ),
    MailProvider.mailgun: (
        0.6,
        False,
        True,
        0.0,
        "Mailgun routes accept every recipient: RCPT verdicts are uninformative.",
    ),
    MailProvider.secure_gateway: (
        0.6,
        False,
        True,
        0.05,
        "Gateways (Mimecast, Proofpoint, Barracuda…) often accept every RCPT and bounce later.",
    ),
    MailProvider.self_hosted: (
        0.3,
        None,
        False,
        0.2,
        "Behaviour depends on the server; greylisting is common.",
    ),
    MailProvider.none: (0.0, None, False, 0.0, "The domain does not receive mail."),
    MailProvider.unknown: (0.25, None, False, 0.1, "Unrecognised MX: no provider-specific prior."),
}


def _clean(host: str) -> str:
    return host.strip().rstrip(".").lower()


def _host_provider(host: str) -> MailProvider | None:
    for provider, rx in _RULES:
        if rx.search(host):
            return provider
    return None


def _on_domain(host: str, domain: str | None) -> bool:
    if not domain:
        return False
    d = _clean(domain)
    return host == d or host.endswith("." + d)


def detect_provider(mx_hosts: Sequence[str], *, domain: str | None = None) -> MailProvider:
    """Provider behind a domain's MX hosts (given best preference first).

    The first host that maps to a known provider decides (a gateway in front of Microsoft 365 is a
    gateway). No usable host (no MX, null MX) → ``none``; MX on the domain itself → ``self_hosted``;
    anything else → ``unknown``.
    """
    hosts = [h for h in (_clean(h) for h in mx_hosts) if h]
    if not hosts:
        return MailProvider.none
    for host in hosts:
        provider = _host_provider(host)
        if provider is not None:
            return provider
    if any(_on_domain(h, domain) for h in hosts):
        return MailProvider.self_hosted
    return MailProvider.unknown


def smtp_uninformative_hosts(mx_hosts: Sequence[str]) -> bool:
    """MX hosts whose RCPT answers say nothing (Yahoo / AOL / Verizon consumer mail: accept-then-bounce)."""
    return any(_UNINFORMATIVE_HOSTS.search(_clean(h)) for h in mx_hosts if h)


def provider_notes(provider: MailProvider, mx_hosts: Sequence[str] = ()) -> dict[str, Any]:
    """Priors for the SMTP layer.

    ``catch_all_prior`` — probability that RCPT accepts any recipient; ``rcpt_reliable`` — unknown
    recipients are rejected at RCPT (None: depends on the server); ``smtp_uninformative`` — accepted
    RCPTs prove nothing (accept-then-bounce); ``greylisting_prior`` — first contact answered 4xx.
    """
    catch_all, reliable, uninformative, greylisting, note = _NOTES[provider]
    if mx_hosts and smtp_uninformative_hosts(mx_hosts):
        catch_all, reliable, uninformative = max(catch_all, 0.6), False, True
        note = "Consumer webmail MX (Yahoo/AOL/Verizon): accepts at RCPT and bounces later."
    return {
        "provider": provider.value,
        "label": _LABELS[provider],
        "catch_all_prior": catch_all,
        "rcpt_reliable": reliable,
        "smtp_uninformative": uninformative,
        "greylisting_prior": greylisting,
        "notes": note,
    }
