"""RDAP domain contacts (``https://rdap.org/domain/<domain>``, follows the IANA bootstrap redirect).

Since GDPR and ICANN's Registration Data Policy (2025-08-21), gTLD contacts are redacted or replaced
by anonymised relay addresses: expect near-zero personal emails. We keep it cheap (one request per
domain every 90 days) and keep only:

* on-domain, non-relay addresses → ``ObservedEmail(source=rdap)`` (mostly role mailboxes:
  hostmaster@, admin@…; a registrant's personal name is attached only when published with it);
* the registrant ORGANISATION, when published → profile evidence (domain ownership), never an email.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import structlog

from scout.db.enums import EmailEvidenceSource, UsageCategory
from scout.discovery.common import Throttle, http_request
from scout.email.contracts import ObservedEmail
from scout.email.lists import is_role_local_part
from scout.email.patterns import infer_pattern
from scout.email.syntax import is_valid_syntax, normalize_address, normalize_domain, split_address
from scout.errors import FetchError, PermanentError, RateLimitedError
from scout.extract.names import is_plausible_person_name, split_name
from scout.services.usage import record_usage
from scout.util.pools import pool

log = structlog.get_logger(__name__)

RDAP_BASE_URL = "https://rdap.org/domain/"
RDAP_FRESHNESS = timedelta(days=90)
RDAP_ERROR_BACKOFF = timedelta(days=1)
RDAP_SAMPLE_CONFIDENCE = 0.7
_throttle = Throttle(0.25)  # rdap.org is a free community service: ≤ 4 lookups/s process-wide

# Markers of redacted / privacy-proxy / relay contacts (addresses and organisation names).
_PRIVACY = re.compile(
    r"redact|privacy|whoisguard|whois-?protect|anonymi[sz]|proxy|protect|withheld|masked|gdpr|"
    r"not ?disclosed|non-?public|obscur|please ?query|statutory|contactprivacy|domainsbyproxy|"
    r"identity ?shield|data ?protected",
    re.IGNORECASE,
)
# Registration contacts that are functions rather than people.
_RDAP_ROLES = frozenset(
    {"domain", "domains", "dns", "noc", "network", "netadmin", "registration", "registrar"}
)


@dataclass
class RdapResult:
    domain: str
    status: str  # ok | not_found | rate_limited | error
    emails: list[ObservedEmail] = field(default_factory=list)
    registrant_org: str | None = None
    registrar: str | None = None
    source_url: str | None = None
    error: str | None = None

    @property
    def completed(self) -> bool:
        return self.status in ("ok", "not_found")


def _vcard_props(entity: Mapping[str, Any]) -> list[tuple[str, Any]]:
    card = entity.get("vcardArray")
    if not isinstance(card, list) or len(card) < 2 or not isinstance(card[1], list):
        return []
    out = []
    for prop in card[1]:
        if isinstance(prop, list) and len(prop) >= 4 and isinstance(prop[0], str):
            out.append((prop[0].lower(), prop[3]))
    return out


def _text(value: Any) -> str | None:
    if isinstance(value, list):
        value = next((v for v in value if isinstance(v, str) and v.strip()), None)
    if not isinstance(value, str):
        return None
    v = " ".join(value.split())
    return v or None


def _is_private(value: str | None) -> bool:
    return bool(value) and bool(_PRIVACY.search(value or ""))


def _walk(entities: Any) -> Iterable[tuple[list[str], dict[str, list[Any]]]]:
    for ent in entities if isinstance(entities, list) else []:
        if not isinstance(ent, Mapping):
            continue
        roles = [str(r).lower() for r in ent.get("roles") or []]
        props: dict[str, list[Any]] = {}
        for name, value in _vcard_props(ent):
            props.setdefault(name, []).append(value)
        yield roles, props
        yield from _walk(ent.get("entities"))


def _self_link(payload: Mapping[str, Any]) -> str | None:
    for link in payload.get("links") or []:
        if isinstance(link, Mapping) and link.get("rel") == "self" and isinstance(link.get("href"), str):
            return str(link["href"])
    return None


def parse_rdap(domain: str, payload: Mapping[str, Any], *, source_url: str | None = None) -> RdapResult:
    """Keep on-domain, non-redacted contact addresses and the published registrant organisation."""
    d = normalize_domain(domain) or domain
    result = RdapResult(domain=d, status="ok", source_url=_self_link(payload) or source_url)
    found: dict[str, ObservedEmail] = {}
    for roles, props in _walk(payload.get("entities")):
        fn = _text((props.get("fn") or [None])[0])
        org = _text((props.get("org") or [None])[0])
        kind = (_text((props.get("kind") or [None])[0]) or "").lower()
        if "registrar" in roles:
            result.registrar = result.registrar or org or fn
            continue
        if "registrant" in roles and result.registrant_org is None:
            name = org or (fn if kind == "org" else None)
            if name and not _is_private(name):
                result.registrant_org = name
        for raw in props.get("email") or []:
            addr = normalize_address(_text(raw))
            if addr is None or not is_valid_syntax(addr) or _is_private(addr):
                continue
            local, adom = split_address(addr)
            if not (adom == d or adom.endswith("." + d)) or addr in found:
                continue
            first = last = None
            if fn and kind != "org" and not _is_private(fn) and is_plausible_person_name(fn):
                first, last = split_name(fn)
            pattern = infer_pattern(first, last, local) if first and last else None
            if pattern is None:
                first = last = None  # a name that does not render the address is not tied to it
            found[addr] = ObservedEmail(
                address=addr,
                local_part=local,
                source=EmailEvidenceSource.rdap,
                first_name=first,
                last_name=last,
                pattern=pattern,
                is_role=pattern is None and (is_role_local_part(local) or local in _RDAP_ROLES),
                source_url=result.source_url,
                evidence=f"RDAP {'/'.join(roles) or 'contact'} contact",
                confidence=RDAP_SAMPLE_CONFIDENCE,
            )
    result.emails = list(found.values())
    return result


async def fetch_rdap(domain: str) -> RdapResult:
    """One RDAP lookup (rdap.org bootstrap → registry server). Never raises."""
    d = normalize_domain(domain)
    if d is None:
        return RdapResult(domain=domain, status="error", error="invalid_domain")
    url = RDAP_BASE_URL + d
    try:
        async with pool("public_api"):
            await _throttle.wait()
            resp = await http_request(
                "GET",
                url,
                source="rdap",
                headers={"Accept": "application/rdap+json, application/json;q=0.9"},
                timeout_s=15.0,
                allow=(400, 403, 404, 410, 422, 429),
            )
        await record_usage(UsageCategory.registry_request, source_key="rdap", resolver="domain_intel")
    except (FetchError, PermanentError, RateLimitedError) as exc:
        return RdapResult(domain=d, status="error", source_url=url, error=str(exc)[:200])
    final_url = str(resp.url)
    if resp.status_code in (404, 410):
        return RdapResult(domain=d, status="not_found", source_url=final_url)
    if resp.status_code in (403, 429):
        return RdapResult(
            domain=d, status="rate_limited", source_url=final_url, error=f"http_{resp.status_code}"
        )
    if resp.status_code != 200:
        return RdapResult(domain=d, status="error", source_url=final_url, error=f"http_{resp.status_code}")
    try:
        payload = resp.json()
    except ValueError:
        return RdapResult(domain=d, status="error", source_url=final_url, error="invalid_json")
    if not isinstance(payload, Mapping):
        return RdapResult(domain=d, status="error", source_url=final_url, error="invalid_payload")
    result = parse_rdap(d, payload, source_url=final_url)
    log.info(
        "email.intel.rdap", domain=d, emails=len(result.emails), registrant_org=bool(result.registrant_org)
    )
    return result
