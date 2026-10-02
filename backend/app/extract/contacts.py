"""Emails (incl. de-obfuscation), social profiles, phones and booking links."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import unquote, urlparse

from ..fetch.crawler import CrawledSite
from ..util.urls import absolutize, registrable_domain, social_network

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,24}")
# Only *explicit* obfuscation markers count: "name [at] domain [dot] com", "name (at) domain.com", "name at domain dot com".
# A bare "word at word.word" is ordinary prose ("terrible at this. And...") and is never an email.
_AT = r"(?:\[\s*at\s*\]|\(\s*at\s*\)|\{\s*at\s*\}|<\s*at\s*>|\[@\]|\(@\)|&#64;)"
_DOT = r"(?:\[\s*dot\s*\]|\(\s*dot\s*\)|\{\s*dot\s*\}|<\s*dot\s*>)"
OBFUSCATED_RE = re.compile(
    rf"([A-Za-z0-9._%+\-]+)\s*(?:{_AT}|\s+at\s+)\s*([A-Za-z0-9\-]+(?:\s*(?:{_DOT}|\s+dot\s+|\.)\s*[A-Za-z0-9\-]+)+)",
    re.I,
)
_OBF_MARKER = re.compile(rf"{_AT}|{_DOT}|\s+dot\s+", re.I)
KNOWN_TLDS = {
    "com", "net", "org", "io", "co", "ai", "me", "app", "dev", "agency", "marketing", "media", "digital", "studio", "info", "biz", "us", "uk",
    "ca", "au", "nz", "ie", "de", "fr", "es", "it", "nl", "be", "ch", "at", "se", "no", "dk", "fi", "pl", "pt", "br", "mx", "ar", "cl", "co.uk",
    "com.au", "co.nz", "co.za", "za", "in", "sg", "hk", "jp", "kr", "ae", "il", "tv", "cc", "xyz", "online", "site", "club", "live", "pro",
    "group", "team", "global", "world", "email", "consulting", "coach", "academy", "education", "expert", "guru", "ltd", "llc", "inc", "eu",
    "ph", "my", "id", "th", "vn", "pk", "ng", "ke", "gh", "eg", "ma", "tr", "ru", "ua", "cz", "ro", "hu", "gr", "sk", "si", "hr", "bg", "lt",
    "lv", "ee", "is", "lu", "mt", "cy", "cloud", "systems", "solutions", "services", "network", "partners", "ventures", "capital", "fund",
}
CF_EMAIL_RE = re.compile(r'(?:data-cfemail="|/cdn-cgi/l/email-protection#)([0-9a-fA-F]{6,})')
MAILTO_RE = re.compile(r'href="mailto:([^"?]+)', re.I)
PHONE_RE = re.compile(r"(?:\+\d{1,3}[\s.-]?)?\(?\d{2,4}\)?[\s.-]?\d{3,4}[\s.-]?\d{3,4}(?:[\s.-]?\d{2,4})?")

BAD_EMAIL_PARTS = (
    "example.", "sentry.", "wixpress", "yourdomain", "domain.com", "email.com", "@email", "noreply", "no-reply", "donotreply",
    "@2x", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", "@site.", "@company.", "user@", "name@", "john@doe", "test@", "@test.",
    "@godaddy", "@wordpress", "@wix.com", "@squarespace", "@shopify", "@hubspot", "@mailchimp", "@google.com", "@facebook.com",
    "@apple.com", "@microsoft", "@amazon", "privacy@", "abuse@", "dmca@", "legal@", "@latofonts", "@w3.org", "@schema.org",
    "you@", "your@", "email@", "someone@", "me@", "@mail.com", "@placeholder", "jane@", "@acme.", "@xyz.", "@abc.",
)
ROLE_PRIORITY = ["hello", "hi", "hey", "info", "contact", "team", "support", "sales", "partnerships", "inquiries", "enquiries", "admin", "office", "marketing", "growth", "media", "press", "careers", "jobs", "billing", "accounts", "help"]
LOW_VALUE_LOCAL = {"careers", "jobs", "billing", "accounts", "help", "support", "press", "privacy", "unsubscribe", "newsletter", "security", "webmaster", "postmaster", "hostmaster"}


def decode_cf_email(hexstr: str) -> str | None:
    try:
        data = bytes.fromhex(hexstr)
    except ValueError:
        return None
    if not data:
        return None
    key = data[0]
    out = bytes(b ^ key for b in data[1:]).decode("utf-8", "ignore")
    return out if EMAIL_RE.fullmatch(out) else None


def _norm(email: str) -> str:
    e = unquote(email).strip().strip(".,;:()<>[]\"'").lower()
    e = re.sub(r"^(mailto:|email:)", "", e)
    return e


def _valid(email: str) -> bool:
    if not EMAIL_RE.fullmatch(email) or len(email) > 80:
        return False
    low = email.lower()
    if any(b in low for b in BAD_EMAIL_PARTS):
        return False
    local, _, dom = low.partition("@")
    if local.startswith(("u00", "x", "%")) and len(local) < 3:
        return False
    if re.search(r"\d{6,}", local) or dom.count(".") > 4:
        return False
    tld = dom.rsplit(".", 1)[-1]
    if not (tld.isalpha() and 2 <= len(tld) <= 24) or tld in {"png", "jpg", "jpeg", "gif", "svg", "webp", "css", "js", "html", "pdf", "min"}:
        return False
    return True


def _valid_obfuscated(email: str) -> bool:
    """Stricter: de-obfuscated addresses must end with a well-known TLD."""
    if not _valid(email):
        return False
    dom = email.rsplit("@", 1)[1]
    parts = dom.split(".")
    return parts[-1] in KNOWN_TLDS or ".".join(parts[-2:]) in KNOWN_TLDS


@dataclass
class EmailHit:
    email: str
    source: str
    page_url: str
    same_domain: bool
    confidence: float
    rank: int = 0


@dataclass
class Contacts:
    emails: list[EmailHit] = field(default_factory=list)
    socials: dict[str, str] = field(default_factory=dict)
    phones: list[str] = field(default_factory=list)
    booking_url: str | None = None


def extract_emails_from_html(html: str, text: str, page_url: str) -> list[tuple[str, str]]:
    """Return (email, source) pairs found in one page."""
    found: list[tuple[str, str]] = []
    for m in MAILTO_RE.finditer(html):
        for e in m.group(1).split(","):
            found.append((_norm(e), "mailto"))
    for m in CF_EMAIL_RE.finditer(html):
        e = decode_cf_email(m.group(1))
        if e:
            found.append((e.lower(), "cloudflare"))
    for m in EMAIL_RE.finditer(text):
        found.append((_norm(m.group(0)), "text"))
    for m in OBFUSCATED_RE.finditer(text):
        if not _OBF_MARKER.search(m.group(0)):
            continue  # "name at domain.com" with no explicit marker is prose, not an address
        local, dom = m.group(1), m.group(2)
        dom = re.sub(r"\s*(?:\[\s*dot\s*\]|\(\s*dot\s*\)|\{\s*dot\s*\}|<\s*dot\s*>|\s+dot\s+)\s*", ".", dom, flags=re.I)
        dom = re.sub(r"\s+", "", dom)
        e = f"{local}@{dom}".lower()
        if _valid_obfuscated(e):
            found.append((e, "obfuscated"))
    # emails hidden in attributes / JSON blobs (e.g. "email":"x@y.com")
    for m in re.finditer(r'"email"\s*:\s*"([^"]+@[^"]+)"', html):
        found.append((_norm(m.group(1)), "jsonld"))
    return [(e, s) for e, s in found if _valid(e)]


def _social_ok(url: str, network: str) -> bool:
    low = url.lower()
    if re.search(r"(sharer|share\?|/share/|intent/|/dialog/|plugins/|/embed|/widgets|/hashtag/|/search\?|/policies|/legal|/help|/privacy|/login|/signup|/explore)", low):
        return False
    path = urlparse(low).path.strip("/")
    if not path:
        return False
    if network == "linkedin" and not re.match(r"(company|in|school|showcase)/", path):
        return False
    if network == "youtube" and re.match(r"(watch|embed|playlist|shorts|results)", path):
        return False
    if network == "facebook" and re.match(r"(groups|events|photo|video|story|login|share|dialog|tr\b)", path):
        return False
    return True


def extract_contacts(site: CrawledSite) -> Contacts:
    out = Contacts()
    seen: dict[str, EmailHit] = {}
    dom = site.domain
    bare = registrable_domain(site.final_url or site.home_url) or dom
    page_kind_bonus = {"contact": 0.25, "home": 0.15, "about": 0.1}
    for p in site.pages:
        for email, source in extract_emails_from_html(p.parsed.html, p.text, p.url):
            edom = registrable_domain(email.split("@", 1)[1])
            same = edom in (dom, bare) or dom.endswith(edom) or edom.endswith(dom)
            local = email.split("@", 1)[0]
            conf = 0.45 + page_kind_bonus.get(p.kind, 0.0)
            if same:
                conf += 0.25
            if source in ("mailto", "cloudflare"):
                conf += 0.1
            if local in LOW_VALUE_LOCAL:
                conf -= 0.2
            if not same and edom not in {"gmail.com", "outlook.com", "hotmail.com", "yahoo.com", "icloud.com", "protonmail.com", "me.com"}:
                conf -= 0.25  # some other company's address (a featured client, a partner...)
            conf = max(0.05, min(0.98, conf))
            cur = seen.get(email)
            if cur is None or conf > cur.confidence:
                seen[email] = EmailHit(email=email, source=source, page_url=p.url, same_domain=same, confidence=conf)

    def _rank(h: EmailHit) -> tuple:
        local = h.email.split("@", 1)[0]
        role_idx = ROLE_PRIORITY.index(local) if local in ROLE_PRIORITY else 50
        personal = 0 if role_idx == 50 and "." in local or local.isalpha() and role_idx == 50 else 1
        return (0 if h.same_domain else 1, -round(h.confidence, 2), min(role_idx, 20), personal)

    hits = sorted(seen.values(), key=_rank)
    for i, h in enumerate(hits):
        h.rank = i
    out.emails = hits[:12]

    # socials / booking / phones
    booking_scores: dict[str, int] = {}
    for p in site.pages:
        for href, anchor in p.parsed.links:
            url = absolutize(p.url, href)
            if not url:
                continue
            net = social_network(url)
            if net and net not in out.socials and _social_ok(url, net):
                out.socials[net] = url.split("?", 1)[0]
                continue
            low = url.lower()
            a = (anchor or "").lower()
            score = 0
            if re.search(r"(calendly\.com|acuityscheduling\.com|hubspot\.com/meetings|meetings\.hubspot|savvycal|tidycal|oncehub|zcal\.co|cal\.com/)", low):
                score = 5
            elif registrable_domain(url) == dom and re.search(r"/(book|apply|schedule|call|consult|strategy|discovery|get-started|start|contact|work-with)", low):
                score = 3
            if re.search(r"(book|apply|schedule|call|strategy session|consult|get started|talk)", a):
                score += 1
            if score and score > booking_scores.get(url, 0):
                booking_scores[url] = score
        if p.kind in ("home", "contact", "about"):
            for m in PHONE_RE.finditer(p.text):
                s = m.group(0).strip()
                digits = re.sub(r"\D", "", s)
                if 9 <= len(digits) <= 15 and not re.fullmatch(r"(\d)\1+", digits) and not re.search(r"(19|20)\d{2}[-/.](0?\d|1[0-2])", s):
                    if s not in out.phones:
                        out.phones.append(s)
    if booking_scores:
        out.booking_url = max(booking_scores.items(), key=lambda kv: kv[1])[0]
    out.phones = out.phones[:3]
    return out
