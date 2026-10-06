"""Website resolution for registry/maps candidates without a URL (docs/PIPELINE.md §7).

Domain candidates from the normalized name → DNS A/AAAA check → fetch home → identity proof on
the home page or its legal notice: SIREN/SIRET (0.98) > phone (0.9) > name + city/postal code
(0.82). Name-only matches (0.65) are rejected. Bounded: stops at the first match ≥ 0.9.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import structlog

from scout.config import get_settings
from scout.crawl import http
from scout.crawl.crawler import detect_parked
from scout.crawl.page_selector import classify_page_type
from scout.crawl.parser import ParsedPage, parse_html
from scout.crawl.ssrf import SSRFBlocked
from scout.db.enums import PageType
from scout.errors import JobError
from scout.extract.legal import valid_siren
from scout.util.text import GENERIC_COMPANY_WORDS, LEGAL_FORMS, normalize_company_name, normalize_key
from scout.util.urls import NON_COMPANY_DOMAINS, registrable_domain

log = structlog.get_logger(__name__)

MIN_CONFIDENCE = 0.75
STOP_CONFIDENCE = 0.9
MAX_CANDIDATES = 12
MAX_FETCHED_DOMAINS = 6
TIME_BUDGET_S = 40.0
DNS_TIMEOUT_S = 2.5

_TLDS_BY_COUNTRY: dict[str, tuple[str, ...]] = {
    "FR": ("fr", "com", "eu", "net", "io", "co"),
    "BE": ("be", "com", "eu", "fr", "net", "io"),
    "CH": ("ch", "com", "eu", "net", "io"),
    "LU": ("lu", "com", "eu", "fr", "net"),
    "DE": ("de", "com", "eu", "net", "io"),
    "AT": ("at", "com", "de", "eu", "net"),
    "ES": ("es", "com", "eu", "net", "io"),
    "IT": ("it", "com", "eu", "net", "io"),
    "NL": ("nl", "com", "eu", "net", "io"),
    "PT": ("pt", "com", "eu", "net", "io"),
    "GB": ("co.uk", "com", "uk", "io", "net", "co"),
    "UK": ("co.uk", "com", "uk", "io", "net", "co"),
    "IE": ("ie", "com", "eu", "io", "net"),
    "CA": ("ca", "com", "io", "net", "co"),
    "US": ("com", "io", "co", "net", "us", "org"),
}
_DEFAULT_TLDS = ("com", "io", "co", "net", "org", "eu")
_EXTRA_GENERIC = {
    "cabinet",
    "atelier",
    "maison",
    "societe",
    "ste",
    "ets",
    "etablissements",
    "entreprise",
    "les",
    "le",
    "la",
    "l",
    "de",
    "des",
    "du",
    "d",
    "of",
    "sarl",
    "sas",
    "the",
    "agency",
    "agence",
    "studio",
    "group",
    "groupe",
    "and",
    "et",
}
# A lone generic word would match someone else's domain (marketing.fr, conseil.com …).
_TOO_GENERIC = {
    "contact",
    "services",
    "service",
    "digital",
    "marketing",
    "conseil",
    "consulting",
    "design",
    "web",
    "communication",
    "france",
    "paris",
    "lyon",
    "media",
    "studio",
    "agence",
    "agency",
    "creation",
    "creative",
    "solutions",
    "group",
    "groupe",
    "immobilier",
    "construction",
    "batiment",
    "transport",
    "formation",
    "sante",
    "beaute",
    "restaurant",
    "boulangerie",
    "garage",
    "auto",
    "info",
    "net",
    "online",
    "shop",
    "boutique",
    "store",
    "home",
    "art",
    "photo",
    "video",
    "event",
    "events",
    "travaux",
    "renovation",
    "nettoyage",
    "plomberie",
    "electricite",
    "coiffure",
}


@dataclass
class ResolvedWebsite:
    url: str
    domain: str
    confidence: float
    evidence: str
    method: str
    checked: list[str] = field(default_factory=list)


def _labels(name: str) -> list[str]:
    norm = normalize_company_name(name or "")
    tokens = [t for t in norm.split() if t and t not in LEGAL_FORMS]
    if not tokens:
        return []
    generic = GENERIC_COMPANY_WORDS | _EXTRA_GENERIC
    core = [t for t in tokens if t not in generic]
    variants: list[list[str]] = [tokens]
    if core and core != tokens:
        variants.append(core)
    labels: list[str] = []
    for toks in variants:
        if len(toks) == 1 and (toks[0] in _TOO_GENERIC or len(toks[0]) < 3):
            continue
        joined, hyphen = "".join(toks), "-".join(toks)
        for label in (joined, hyphen):
            if (
                3 <= len(label) <= 63
                and re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label)
                and label not in labels
            ):
                labels.append(label)
    return labels


def domain_candidates(name: str, country: str | None) -> list[str]:
    """Likely domains for a company name, most probable first (max 12)."""
    labels = _labels(name)
    if not labels:
        return []
    tlds = _TLDS_BY_COUNTRY.get((country or "").upper(), _DEFAULT_TLDS)
    out: list[str] = []
    # Interleave: every label on the primary TLD first, then the next TLD, …
    for tld in tlds:
        for label in labels:
            domain = f"{label}.{tld}"
            if domain not in out and domain not in NON_COMPANY_DOMAINS:
                out.append(domain)
            if len(out) >= MAX_CANDIDATES:
                return out
    return out


async def dns_exists(domain: str) -> bool:
    """True when the domain has an A or AAAA record (short timeouts; monkeypatched in tests)."""
    if domain in get_settings().crawler_host_overrides:
        return True
    import dns.asyncresolver
    import dns.exception
    import dns.resolver

    resolver = dns.asyncresolver.Resolver()
    resolver.lifetime = DNS_TIMEOUT_S
    resolver.timeout = DNS_TIMEOUT_S / 2
    for rtype in ("A", "AAAA"):
        try:
            answer = await resolver.resolve(domain, rtype, raise_on_no_answer=False)
            if answer.rrset is not None and len(answer.rrset) > 0:
                return True
        except (dns.resolver.NXDOMAIN, dns.resolver.NoNameservers):
            return False
        except (dns.exception.Timeout, dns.resolver.NoAnswer, dns.exception.DNSException):
            continue
    return False


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def _snippet_around(text: str, idx: int, pad: int = 80) -> str:
    a, b = max(0, idx - pad), min(len(text), idx + pad)
    return re.sub(r"\s+", " ", text[a:b]).strip()


def _verify(
    parsed: ParsedPage,
    *,
    name: str,
    city: str | None,
    postal_code: str | None,
    registry_id: str | None,
    phone: str | None,
) -> tuple[float, str, str]:
    """(confidence, evidence, method) for one page."""
    text = parsed.content_text or ""
    digits_text = _digits(text)
    reg = _digits(registry_id or "")
    if reg and len(reg) in (9, 14) and valid_siren(reg[:9]):
        siren = reg[:9]
        if siren in digits_text:
            m = re.search(r"\s?".join(siren), text)
            ev = _snippet_around(text, m.start()) if m else f"SIREN {siren}"
            return 0.98, ev, "registry_id"
    ph = _digits(phone or "")[-9:]
    if len(ph) == 9:
        for p in parsed.phones:
            if _digits(p).endswith(ph):
                return 0.9, f"phone {p}", "phone"
        if ph in digits_text:
            return 0.9, f"phone …{ph}", "phone"
    norm_name = normalize_company_name(name)
    hay = normalize_key(" ".join([parsed.title or "", " ".join(parsed.headings), text]))
    if norm_name and len(norm_name) >= 3 and re.search(rf"(?:^| ){re.escape(norm_name)}(?: |$)", hay):
        loc_hit = None
        if postal_code and _digits(postal_code) and _digits(postal_code) in digits_text:
            loc_hit = postal_code
        elif city and re.search(rf"(?:^| ){re.escape(normalize_key(city))}(?: |$)", normalize_key(text)):
            loc_hit = city
        if loc_hit:
            return 0.82, f"name '{name}' and location '{loc_hit}' on page", "name_location"
        return 0.65, f"name '{name}' on page", "name_only"
    return 0.0, "", "none"


def _legal_link(parsed: ParsedPage) -> str | None:
    for link in parsed.links.get("internal", []):
        url = link.get("url") if isinstance(link, dict) else None
        if url and classify_page_type(url, None, link.get("text")) == PageType.legal:
            return url
    return None


async def _fetch_home(domain: str) -> http.HttpResponse | None:
    for url in (f"https://{domain}/", f"http://{domain}/"):
        try:
            resp = await http.fetch(url)
        except (JobError, SSRFBlocked):
            continue
        if resp.ok and resp.text.strip():
            return resp
    return None


async def resolve_website(
    name: str,
    *,
    city: str | None = None,
    postal_code: str | None = None,
    country: str | None = None,
    registry_id: str | None = None,
    phone: str | None = None,
) -> ResolvedWebsite | None:
    """Find and verify the company's website, or None (never a guess below 0.75)."""
    candidates = domain_candidates(name, country)
    if not candidates:
        return None
    deadline = time.monotonic() + TIME_BUDGET_S
    best: ResolvedWebsite | None = None
    checked: list[str] = []
    fetched = 0

    exists = await asyncio.gather(*(dns_exists(d) for d in candidates), return_exceptions=True)
    live = [d for d, ok in zip(candidates, exists, strict=True) if ok is True]

    async def attempt(domain: str, origin: str) -> None:
        nonlocal best, fetched
        fetched += 1
        checked.append(domain)
        resp = await _fetch_home(domain)
        if resp is None:
            return
        final_domain = registrable_domain(resp.final_url) or domain
        if final_domain in NON_COMPANY_DOMAINS:
            return
        parsed = parse_html(resp.text, resp.final_url, is_home=False)
        if detect_parked(resp.text, parsed, resp.final_url):
            return
        conf, evidence, method = _verify(
            parsed, name=name, city=city, postal_code=postal_code, registry_id=registry_id, phone=phone
        )
        source = resp.final_url
        if conf < STOP_CONFIDENCE:
            legal = _legal_link(parsed)
            if legal and time.monotonic() < deadline:
                try:
                    lresp = await http.fetch(legal)
                except (JobError, SSRFBlocked):
                    lresp = None
                if lresp is not None and lresp.ok:
                    lparsed = parse_html(lresp.text, lresp.final_url)
                    lconf, lev, lmethod = _verify(
                        lparsed,
                        name=name,
                        city=city,
                        postal_code=postal_code,
                        registry_id=registry_id,
                        phone=phone,
                    )
                    if lconf > conf:
                        conf, evidence, method, source = lconf, lev, lmethod, lresp.final_url
        log.info(
            "website_resolution_candidate", domain=final_domain, confidence=conf, method=method, origin=origin
        )
        if conf >= MIN_CONFIDENCE and (best is None or conf > best.confidence):
            home = urlsplit(resp.final_url)
            best = ResolvedWebsite(
                url=f"{home.scheme}://{home.netloc}/",
                domain=final_domain,
                confidence=conf,
                evidence=f"{evidence} ({source})",
                method=f"{origin}+{method}",
            )

    def done() -> bool:
        return (
            (best is not None and best.confidence >= STOP_CONFIDENCE)
            or fetched >= MAX_FETCHED_DOMAINS
            or time.monotonic() > deadline
        )

    for domain in live:
        if done():
            break
        await attempt(domain, "domain_guess")
    if not done():
        # free web search (SearXNG) proposes more candidates; identity is proven on the site exactly as above
        try:
            from scout.search.website import search_domain_candidates

            found = await search_domain_candidates(name, city=city, country=country)
        except Exception as exc:  # search is optional: never break resolution
            log.info("website_resolution_search_failed", error=str(exc))
            found = []
        for domain in found:
            if done():
                break
            if domain not in checked:
                await attempt(domain, "web_search")
    if best is not None:
        best.checked = checked
    return best
