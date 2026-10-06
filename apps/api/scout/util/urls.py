"""URL & domain canonicalization (company identity's strongest signal)."""

from __future__ import annotations

import re
from functools import lru_cache
from urllib.parse import urljoin, urlsplit, urlunsplit

import idna
import tldextract

# Offline PSL snapshot bundled with tldextract (no network fetch at runtime).
_extract = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)

# Hosts that are never a company's own website (social, marketplaces, directories, link hubs).
NON_COMPANY_DOMAINS = {
    "facebook.com",
    "instagram.com",
    "linkedin.com",
    "twitter.com",
    "x.com",
    "tiktok.com",
    "youtube.com",
    "youtu.be",
    "pinterest.com",
    "threads.net",
    "wa.me",
    "whatsapp.com",
    "t.me",
    "snapchat.com",
    "google.com",
    "goo.gl",
    "g.page",
    "maps.app.goo.gl",
    "business.site",
    "sites.google.com",
    "linktr.ee",
    "linktree.com",
    "beacons.ai",
    "carrd.co",
    "bio.link",
    "msha.ke",
    "wix.com",
    "wixsite.com",
    "squarespace.com",
    "webflow.io",
    "wordpress.com",
    "blogspot.com",
    "medium.com",
    "pagesjaunes.fr",
    "yelp.com",
    "yelp.fr",
    "tripadvisor.com",
    "tripadvisor.fr",
    "societe.com",
    "pappers.fr",
    "infogreffe.fr",
    "verif.com",
    "manageo.fr",
    "kompass.com",
    "europages.fr",
    "europages.com",
    "clutch.co",
    "sortlist.com",
    "sortlist.fr",
    "goodfirms.co",
    "designrush.com",
    "malt.fr",
    "malt.com",
    "upwork.com",
    "fiverr.com",
    "indeed.com",
    "indeed.fr",
    "welcometothejungle.com",
    "glassdoor.com",
    "glassdoor.fr",
    "crunchbase.com",
    "wikipedia.org",
    "amazon.com",
    "amazon.fr",
    "etsy.com",
    "apple.com",
    "calendly.com",
    "hubspot.com",
    "mailchimp.com",
    "gmail.com",
    "outlook.com",
    "hotmail.com",
    "yahoo.com",
    "trustpilot.com",
    "avis-verifies.com",
    "doctolib.fr",
    "github.io",
    "netlify.app",
    "vercel.app",
    "herokuapp.com",
    "duckduckgo.com",
    "bing.com",
    "yahoo.fr",
    "orange.fr",
    "free.fr",
    "sfr.fr",
}

_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")


@lru_cache(maxsize=65536)
def registrable_domain(host_or_url: str | None) -> str | None:
    """Return the registrable domain (eTLD+1), lowercase, IDNA-encoded; None if not a public domain."""
    if not host_or_url:
        return None
    raw = host_or_url.strip()
    if not raw:
        return None
    if "@" in raw and "://" not in raw:
        raw = raw.split("@", 1)[1]
    if not _SCHEME_RE.match(raw):
        raw = "http://" + raw
    try:
        host = urlsplit(raw).hostname
    except ValueError:
        return None
    if not host:
        return None
    host = host.strip(".").lower()
    try:
        host = idna.encode(host, uts46=True).decode("ascii")
    except idna.IDNAError:
        return None
    ext = _extract(host)
    if not ext.domain or not ext.suffix:
        return None
    return f"{ext.domain}.{ext.suffix}"


def is_company_domain(domain: str | None) -> bool:
    if not domain:
        return False
    return domain not in NON_COMPANY_DOMAINS


def normalize_website(url: str | None) -> str | None:
    """Canonical home URL: https://<host>/ (keeps subdomain other than www)."""
    if not url:
        return None
    raw = url.strip()
    if not raw:
        return None
    if not _SCHEME_RE.match(raw):
        raw = "https://" + raw
    try:
        parts = urlsplit(raw)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    host = parts.hostname.lower().strip(".")
    if host.startswith("www."):
        host = host[4:]
    try:
        host = idna.encode(host, uts46=True).decode("ascii")
    except idna.IDNAError:
        return None
    return f"https://{host}/"


def canonical_url(url: str) -> str:
    """Canonical page URL for cache keys: lowercase host, no fragment, no tracking params, no trailing slash."""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    query = "&".join(
        q
        for q in (parts.query or "").split("&")
        if q and not q.lower().startswith(("utm_", "gclid", "fbclid", "ref="))
    )
    port = f":{parts.port}" if parts.port and parts.port not in (80, 443) else ""
    return urlunsplit(((parts.scheme or "https").lower(), host + port, path, query, ""))


def absolutize(base: str, href: str) -> str | None:
    href = (href or "").strip()
    if not href or href.startswith(("javascript:", "data:", "#")):
        return None
    try:
        return urljoin(base, href)
    except ValueError:
        return None


def same_site(url: str, domain: str) -> bool:
    return registrable_domain(url) == domain


def host_of(url: str) -> str | None:
    try:
        return (urlsplit(url).hostname or "").lower() or None
    except ValueError:
        return None
