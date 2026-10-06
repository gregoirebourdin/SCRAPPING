"""Website-field strategy: deterministic extractors on cached pages (no AI).

Presence-type fields (testimonials, pricing page, careers page, blog, newsletter, chat widget) return
false only when the relevant pages were crawled; value-type fields (phone, email, address, CTA,
booking link) return `unknown` when nothing is found — absence of a value is not a value.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Iterable
from typing import Any
from urllib.parse import urlsplit

from scout.db.enums import ColumnDataType, PageType
from scout.enrich.matching import contains_any, excerpt, find_terms, fold
from scout.enrich.resolvers.base import (
    NOT_CRAWLED,
    ResolveContext,
    failed,
    ok,
    ordered_pages,
    page_type_of,
    unknown,
)
from scout.enrich.resolvers.social_profile import host_of, iter_links
from scout.enrich.types import CellResult
from scout.util.urls import registrable_domain

RESOLVER = "website_extractor"

ROLE_LOCALS = (
    "contact",
    "hello",
    "bonjour",
    "info",
    "infos",
    "information",
    "hi",
    "hey",
    "team",
    "equipe",
    "agence",
    "agency",
    "studio",
    "office",
    "admin",
    "sales",
    "commercial",
    "welcome",
    "support",
    "service",
    "rdv",
    "partenariat",
    "partnerships",
    "marketing",
    "press",
    "presse",
    "jobs",
    "recrutement",
    "careers",
    "accueil",
    "direction",
)
_EMAIL_RX = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_EMAIL_NOISE = re.compile(
    r"(?:example\.|sentry|wixpress|\.(?:png|jpe?g|gif|webp|svg)$|@\dx|noreply|no-reply|domain\.com|email\.com|"
    r"yourdomain|votredomaine|exemple\.)",
    re.I,
)
_PHONE_RX = re.compile(
    r"(?<![\d+])(?:(?:\+|00)33[\s.-]?\(?0?\)?[1-9]|0[1-9])(?:[\s.-]?\d{2}){4}(?!\d)"
    r"|(?<![\d+])\+\d{1,3}[\s.-]?\(?\d{1,4}\)?(?:[\s.-]?\d{2,4}){2,4}(?!\d)"
)
_ADDRESS_FR = re.compile(
    r"\d{1,4}(?:\s?(?:bis|ter))?,?\s+(?:rue|avenue|av\.|boulevard|bd|place|chemin|allee|allée|impasse|quai|route|"
    r"cours|square|passage|rond-point|esplanade|parvis)\s+[^\n,]{2,60},?\s*(?:\n\s*)?\d{5}\s+[A-Za-zÀ-ÿ' -]{2,40}",
    re.I,
)
_ADDRESS_EN = re.compile(
    r"\d{1,5}\s+(?:[A-Z][a-z]+\s){1,3}(?:Street|St\.?|Avenue|Ave\.?|Road|Rd\.?|Boulevard|Blvd\.?|Lane|Ln\.?|"
    r"Drive|Dr\.?|Way|Place|Square)\b[^\n]{0,60}"
)

CTA_PHRASES: dict[str, int] = {
    # strength 3: direct conversion actions
    "book a call": 3,
    "book a demo": 3,
    "book a meeting": 3,
    "book a consultation": 3,
    "schedule a call": 3,
    "schedule a demo": 3,
    "request a demo": 3,
    "get a demo": 3,
    "get a quote": 3,
    "request a quote": 3,
    "get a free quote": 3,
    "free consultation": 3,
    "free audit": 3,
    "start free trial": 3,
    "start your free trial": 3,
    "try for free": 3,
    "try it free": 3,
    "free trial": 3,
    "prendre rendez-vous": 3,
    "prendre rdv": 3,
    "reserver un appel": 3,
    "reserver une demo": 3,
    "demander une demo": 3,
    "demander un devis": 3,
    "devis gratuit": 3,
    "obtenir un devis": 3,
    "demander un audit": 3,
    "audit gratuit": 3,
    "essai gratuit": 3,
    "essayer gratuitement": 3,
    "reserver un creneau": 3,
    "planifier un appel": 3,
    "etre rappele": 3,
    # strength 2: project/engagement starts
    "get started": 2,
    "start a project": 2,
    "start your project": 2,
    "let's talk": 2,
    "lets talk": 2,
    "talk to an expert": 2,
    "talk to us": 2,
    "work with us": 2,
    "get in touch": 2,
    "parlons de votre projet": 2,
    "parlons-en": 2,
    "lancer mon projet": 2,
    "demarrer un projet": 2,
    "discutons": 2,
    "echangeons": 2,
    "commencer": 2,
    "demarrer": 2,
    # strength 1: generic contact
    "contact us": 1,
    "contact": 1,
    "nous contacter": 1,
    "contactez-nous": 1,
    "contactez nous": 1,
    "appelez-nous": 1,
    "call us": 1,
}
TESTIMONIAL_TERMS = [
    "testimonials",
    "testimonial",
    "temoignages",
    "temoignage",
    "what our clients say",
    "what our customers say",
    "what clients say",
    "ce que disent nos clients",
    "ils parlent de nous",
    "avis clients",
    "avis de nos clients",
    "nos clients temoignent",
    "client stories",
    "customer stories",
    "success stories",
    "reviews",
    "avis google",
    "client reviews",
    "customer reviews",
    "ils nous recommandent",
]
NEWSLETTER_TERMS = [
    "newsletter",
    "subscribe to our newsletter",
    "sign up for our newsletter",
    "lettre d'information",
    "inscrivez-vous a notre newsletter",
    "abonnez-vous a notre newsletter",
]
CHAT_VENDORS: dict[str, str] = {
    "widget.intercom.io": "Intercom",
    "js.intercomcdn.com": "Intercom",
    "client.crisp.chat": "Crisp",
    "js.driftt.com": "Drift",
    "embed.tawk.to": "Tawk.to",
    "js.usemessages.com": "HubSpot Chat",
    "static.zdassets.com": "Zendesk Chat",
    "cdn.livechatinc.com": "LiveChat",
    "code.tidio.co": "Tidio",
    "wchat.freshchat.com": "Freshchat",
    "static.olark.com": "Olark",
    "widget.manychat.com": "ManyChat",
    "smartsuppchat.com": "Smartsupp",
    "config.gorgias.chat": "Gorgias",
    "xfbml.customerchat": "Messenger Chat",
    "chatra.io": "Chatra",
    "userlike": "Userlike",
    "landbot.io": "Landbot",
    "chatwoot": "Chatwoot",
    "botpress": "Botpress",
    "voiceflow": "Voiceflow",
    "iadvize": "iAdvize",
}
BOOKING_HOSTS = (
    "calendly.com",
    "meetings.hubspot.com",
    "cal.com",
    "zcal.co",
    "savvycal.com",
    "tidycal.com",
    "youcanbook.me",
    "acuityscheduling.com",
    "lemcal.com",
    "koalendar.com",
    "setmore.com",
    "simplybook.me",
    "planity.com",
    "doctolib.fr",
    "resalib.fr",
    "calendar.app.google",
    "book.morgen.so",
    "meet.brevo.com",
    "zeeg.me",
)
_PRICING_PATH = re.compile(
    r"/(?:pricing|tarifs?|prix|plans|packages|forfaits|offres?|nos-offres|our-plans)(?:[/.?#-]|$)", re.I
)
_CAREERS_PATH = re.compile(
    r"/(?:careers?|jobs?|recrutement|carrieres?|join(?:-us)?|nous-rejoindre|rejoignez-nous|emplois?|"
    r"work-with-us|hiring|we-are-hiring)(?:[/.?#-]|$)",
    re.I,
)
CAREERS_HOSTS = (
    "welcometothejungle.com",
    "lever.co",
    "greenhouse.io",
    "workable.com",
    "recruitee.com",
    "teamtailor.com",
    "smartrecruiters.com",
    "ashbyhq.com",
    "jobs.personio.de",
    "join.com",
)
_BLOG_PATH = re.compile(
    r"/(?:blog|actualites?|news|articles?|journal|insights|ressources|resources|magazine|le-mag|mag)(?:[/.?#-]|$)",
    re.I,
)


def _company_domain(rc: ResolveContext) -> str | None:
    c = rc.company
    if c is None:
        return None
    return c.normalized_domain or registrable_domain(c.domain or c.website_url)


# ---------------------------------------------------------------------------------------------
# value-type fields
# ---------------------------------------------------------------------------------------------
async def _email(rc: ResolveContext) -> CellResult:
    plan = rc.plan
    domain = _company_domain(rc)
    seen: dict[str, Any] = {}
    for page in ordered_pages(rc.pages, ["contact", "home", "legal", "about"]) + ordered_pages(rc.pages):
        for addr in [
            *(getattr(page, "emails", None) or []),
            *_EMAIL_RX.findall(getattr(page, "content_text", "") or ""),
        ]:
            if not isinstance(addr, str):
                continue
            a = addr.strip().lower().removeprefix("mailto:")
            if _EMAIL_RX.fullmatch(a) and not _EMAIL_NOISE.search(a):
                seen.setdefault(a, page)
    if not seen:
        return unknown(plan, resolver=RESOLVER, evidence="No email address published on crawled pages")

    def score(a: str) -> tuple[int, int]:
        local, _, dom = a.partition("@")
        on_domain = bool(domain) and registrable_domain(dom) == domain
        role = local.split(".")[0].split("-")[0] in ROLE_LOCALS
        return (0 if on_domain and role else 1 if role else 2 if on_domain else 3, len(a))

    best = sorted(seen, key=score)[0]
    rank = score(best)[0]
    conf = {0: 0.95, 1: 0.8, 2: 0.6, 3: 0.5}[rank]
    page = seen[best]
    return ok(
        plan,
        best,
        resolver=RESOLVER,
        confidence=conf,
        evidence=f"Published on {page.url}",
        source_url=page.url,
    )


async def _phone(rc: ResolveContext) -> CellResult:
    plan = rc.plan
    for page in ordered_pages(rc.pages, ["contact", "home", "legal", "about"]) + ordered_pages(rc.pages):
        phones = [p for p in (getattr(page, "phones", None) or []) if isinstance(p, str) and p.strip()]
        if phones:
            return ok(
                plan,
                phones[0].strip(),
                resolver=RESOLVER,
                confidence=0.9,
                evidence=f"Published on {page.url}",
                source_url=page.url,
            )
        text = getattr(page, "content_text", "") or ""
        m = _PHONE_RX.search(text)
        if m:
            return ok(
                plan,
                m.group(0).strip(),
                resolver=RESOLVER,
                confidence=0.8,
                evidence=excerpt(text, m.start(), m.end(), 120),
                source_url=page.url,
            )
    if rc.company is not None and rc.company.phone:
        return ok(
            plan,
            rc.company.phone,
            resolver="company_record",
            confidence=0.8,
            evidence="Phone from the company record",
            source_id="company_record",
        )
    return unknown(plan, resolver=RESOLVER, evidence="No phone number found on crawled pages")


def _jsonld_address(data: Any, depth: int = 0) -> str | None:
    if depth > 4:
        return None
    if isinstance(data, list):
        for item in data:
            found = _jsonld_address(item, depth + 1)
            if found:
                return found
        return None
    if not isinstance(data, dict):
        return None
    addr = data.get("address")
    if isinstance(addr, str) and len(addr) > 8:
        return addr.strip()
    if isinstance(addr, dict):
        parts = [
            addr.get("streetAddress"),
            " ".join(x for x in (addr.get("postalCode"), addr.get("addressLocality")) if x),
        ]
        country = addr.get("addressCountry")
        if isinstance(country, dict):
            country = country.get("name")
        parts.append(country if isinstance(country, str) else None)
        text = ", ".join(str(p).strip() for p in parts if p and str(p).strip())
        if addr.get("streetAddress") and text:
            return text
    for key in ("@graph", "location", "publisher", "organization", "provider"):
        found = _jsonld_address(data.get(key), depth + 1)
        if found:
            return found
    return None


async def _address(rc: ResolveContext) -> CellResult:
    plan = rc.plan
    pages = ordered_pages(rc.pages, ["contact", "legal", "home", "about"]) + ordered_pages(rc.pages)
    for page in pages:
        found = _jsonld_address(getattr(page, "structured_data", None) or [])
        if found:
            return ok(
                plan,
                found,
                resolver=RESOLVER,
                confidence=0.9,
                evidence="Structured data (JSON-LD) address",
                source_url=page.url,
            )
    for page in pages:
        text = getattr(page, "content_text", "") or ""
        m = _ADDRESS_FR.search(text) or _ADDRESS_EN.search(text)
        if m:
            value = re.sub(r"\s+", " ", m.group(0)).strip(" ,")
            return ok(
                plan,
                value,
                resolver=RESOLVER,
                confidence=0.75,
                evidence=excerpt(text, m.start(), m.end(), 160),
                source_url=page.url,
            )
    c = rc.company
    if c is not None and c.address:
        value = ", ".join(x for x in (c.address, " ".join(y for y in (c.postal_code, c.city) if y)) if x)
        return ok(
            plan,
            value,
            resolver="company_record",
            confidence=0.8,
            evidence="Address from the company record",
            source_id="company_record",
        )
    return unknown(plan, resolver=RESOLVER, evidence="No postal address found on crawled pages")


def _cta_candidates(page: Any) -> Iterable[str]:
    for _url, text in iter_links(page):
        if text:
            yield text
    yield from (getattr(page, "content_text", "") or "").splitlines()


async def _cta(rc: ResolveContext) -> CellResult:
    plan = rc.plan
    homes = ordered_pages(rc.pages, ["home"]) or ordered_pages(rc.pages)[:1]
    scores: dict[str, float] = {}
    shown: dict[str, tuple[str, Any]] = {}
    for page in homes:
        for pos, raw in enumerate(_cta_candidates(page)):
            line = raw.strip(" \t•·>→›»|-–—")
            if not line or len(line) > 45 or len(line.split()) > 7:
                continue
            key = fold(line).strip(" .!")
            strength = max((s for phrase, s in CTA_PHRASES.items() if contains_any(key, [phrase])), default=0)
            if not strength:
                continue
            scores[key] = (
                scores.get(key, 0.0)
                + strength
                + (0.5 if key in scores else 0.0)
                + max(0.0, 0.3 - pos * 0.002)
            )
            shown.setdefault(key, (line, page))
    if not scores:
        if not homes:
            return unknown(plan, resolver=RESOLVER, error=NOT_CRAWLED)
        return unknown(plan, resolver=RESOLVER, evidence="No clear call to action found on the home page")
    best = max(scores, key=lambda k: scores[k])
    text, page = shown[best]
    strength = max(s for phrase, s in CTA_PHRASES.items() if contains_any(best, [phrase]))
    return ok(
        plan,
        text,
        resolver=RESOLVER,
        confidence=0.8 if strength >= 2 else 0.65,
        evidence=f"Button/link text on {page.url}: “{text}”",
        source_url=page.url,
    )


async def _booking_link(rc: ResolveContext) -> CellResult:
    plan = rc.plan
    for page in ordered_pages(rc.pages):
        urls = [u for u, _ in iter_links(page)]
        urls += re.findall(r"https?://[^\s\"'<>)]+", getattr(page, "content_text", "") or "")
        for url in urls:
            host = host_of(url)
            if any(host == h or host.endswith("." + h) for h in BOOKING_HOSTS):
                value: Any = True if plan.data_type == ColumnDataType.boolean else url
                return ok(
                    plan,
                    value,
                    resolver=RESOLVER,
                    confidence=0.9,
                    evidence=f"Booking link on {page.url}: {url}",
                    source_url=page.url,
                )
    evidence = "No booking/scheduling link found on crawled pages"
    if plan.data_type == ColumnDataType.boolean:
        return ok(plan, False, resolver=RESOLVER, confidence=0.75, evidence=evidence)
    return unknown(plan, resolver=RESOLVER, evidence=evidence, source_id="website")


# ---------------------------------------------------------------------------------------------
# presence-type fields
# ---------------------------------------------------------------------------------------------
def _presence(
    rc: ResolveContext, found: tuple[Any, str] | None, *, absent: str, absent_conf: float
) -> CellResult:
    plan = rc.plan
    if found is not None:
        page_or_url, evidence = found
        url = page_or_url if isinstance(page_or_url, str) else getattr(page_or_url, "url", None)
        value: Any = url if plan.data_type == ColumnDataType.url else True
        return ok(plan, value, resolver=RESOLVER, confidence=0.9, evidence=evidence, source_url=url)
    if plan.data_type == ColumnDataType.boolean:
        return ok(plan, False, resolver=RESOLVER, confidence=absent_conf, evidence=absent)
    return unknown(plan, resolver=RESOLVER, evidence=absent, source_id="website")


def _page_or_link(
    rc: ResolveContext, types: set[str], path_rx: re.Pattern[str], hosts: tuple[str, ...] = ()
) -> tuple[Any, str] | None:
    for page in rc.pages:
        if page_type_of(page) in types:
            return page, f"{page_type_of(page).replace('_', ' ').title()} page crawled: {page.url}"
    for page in ordered_pages(rc.pages):
        for url, text in iter_links(page):
            path = urlsplit(url).path if "://" in url else url
            host = host_of(url)
            if path_rx.search(path or "") or (
                hosts and any(host == h or host.endswith("." + h) for h in hosts)
            ):
                return url, f"Linked from {page.url}" + (f" (“{text.strip()[:60]}”)" if text.strip() else "")
    return None


async def _pricing_page(rc: ResolveContext) -> CellResult:
    found = _page_or_link(rc, {PageType.pricing.value}, _PRICING_PATH)
    return _presence(
        rc,
        found,
        absent=f"No pricing page among {len(rc.pages)} crawled pages and their links",
        absent_conf=0.75,
    )


async def _careers_page(rc: ResolveContext) -> CellResult:
    found = _page_or_link(rc, {PageType.careers.value}, _CAREERS_PATH, CAREERS_HOSTS)
    return _presence(
        rc,
        found,
        absent=f"No careers page among {len(rc.pages)} crawled pages and their links",
        absent_conf=0.75,
    )


async def _blog(rc: ResolveContext) -> CellResult:
    found = _page_or_link(rc, {PageType.blog.value, PageType.news.value}, _BLOG_PATH)
    return _presence(
        rc, found, absent=f"No blog among {len(rc.pages)} crawled pages and their links", absent_conf=0.75
    )


def _text_presence(rc: ResolveContext, terms: list[str], pages: list[Any]) -> tuple[Any, str] | None:
    for page in pages:
        text = getattr(page, "content_text", "") or ""
        hits = find_terms(text, terms, first_only=True)
        if hits:
            h = hits[0]
            return page, excerpt(text, h.start, h.end, 160)
    return None


async def _testimonials(rc: ResolveContext) -> CellResult:
    plan = rc.plan
    home_only = [str(getattr(s, "value", s)) for s in plan.input_sources] == ["home"]
    scope = ordered_pages(rc.pages, ["home"]) if home_only else ordered_pages(rc.pages)
    if not scope:
        return unknown(plan, resolver=RESOLVER, error="Home page not crawled" if home_only else NOT_CRAWLED)
    found = _text_presence(rc, TESTIMONIAL_TERMS, scope)
    if found is None:
        for page in scope:
            data = str(getattr(page, "structured_data", None) or "")
            if (
                '"Review"' in data
                or "AggregateRating" in data
                or "★★★★" in (getattr(page, "content_text", "") or "")
            ):
                found = (page, "Review markup / star ratings on the page")
                break
    where = "the home page" if home_only else f"{len(scope)} crawled pages"
    return _presence(rc, found, absent=f"No testimonials section found on {where}", absent_conf=0.8)


async def _newsletter(rc: ResolveContext) -> CellResult:
    found = _text_presence(rc, NEWSLETTER_TERMS, ordered_pages(rc.pages))
    return _presence(
        rc, found, absent=f"No newsletter signup found on {len(rc.pages)} crawled pages", absent_conf=0.7
    )


async def _chat_widget(rc: ResolveContext) -> CellResult:
    plan = rc.plan
    heads: list[tuple[Any, str]] = [
        (p, str(p.head_html)) for p in ordered_pages(rc.pages) if getattr(p, "head_html", None)
    ]
    if not heads:
        return unknown(plan, resolver=RESOLVER, evidence="Page scripts were not captured for this site")
    for page, head in heads:
        low = head.lower()
        for sig, vendor in CHAT_VENDORS.items():
            if sig in low:
                value: Any = vendor if plan.data_type == ColumnDataType.text else True
                return ok(
                    plan,
                    value,
                    resolver=RESOLVER,
                    confidence=0.95,
                    evidence=f"{vendor} chat script on {page.url}",
                    source_url=page.url,
                )
    if plan.data_type == ColumnDataType.boolean:
        return ok(
            plan, False, resolver=RESOLVER, confidence=0.8, evidence="No known chat widget script detected"
        )
    return unknown(
        plan, resolver=RESOLVER, evidence="No known chat widget script detected", source_id="website"
    )


EXTRACTORS: dict[str, Callable[[ResolveContext], Awaitable[CellResult]]] = {
    "email": _email,
    "phone": _phone,
    "address": _address,
    "cta": _cta,
    "booking_link": _booking_link,
    "pricing_page": _pricing_page,
    "careers_page": _careers_page,
    "blog": _blog,
    "testimonials": _testimonials,
    "newsletter": _newsletter,
    "chat_widget": _chat_widget,
}
RECORD_FALLBACK = {"phone", "address"}


async def resolve(rc: ResolveContext) -> CellResult:
    plan = rc.plan
    extractor = EXTRACTORS.get(plan.field or "")
    if extractor is None:
        return failed(plan, resolver=RESOLVER, error=f"Unsupported website field: {plan.field}")
    if not rc.pages and plan.field not in RECORD_FALLBACK:
        return unknown(plan, resolver=RESOLVER, error=NOT_CRAWLED)
    result = await extractor(rc)
    if not rc.pages and result.status != "success":
        return unknown(plan, resolver=RESOLVER, error=NOT_CRAWLED)
    return result
