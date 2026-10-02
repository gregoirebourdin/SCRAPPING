"""URL / domain helpers: canonicalization, registrable-domain extraction, blocklists."""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import tldextract

# Offline suffix list: never hit the network to resolve TLDs.
_extract = tldextract.TLDExtract(suffix_list_urls=(), fallback_to_snapshot=True)

# Hosts where every subdomain is a different tenant/site, so the subdomain *is* the identity.
MULTI_TENANT_HOSTS = {
    "mykajabi.com", "kajabi.com", "clickfunnels.com", "myclickfunnels.com", "systeme.io", "wixsite.com",
    "squarespace.com", "webflow.io", "carrd.co", "github.io", "netlify.app", "vercel.app", "godaddysites.com",
    "weebly.com", "wordpress.com", "blogspot.com", "teachable.com", "thinkific.com", "podia.com",
    "leadpages.co", "leadpages.net", "lpages.co", "kartra.com", "samcart.com", "thrivecart.com",
    "gumroad.com", "stanstore.com", "stan.store", "linktr.ee", "beacons.ai", "hubspotpagebuilder.com",
    "mailchimpsites.com", "unbounce.com", "instapage.com", "typeform.com", "convertkit.com", "kit.com",
    "gohighlevel.com", "msgsndr.com", "funnelish.com", "groovepages.com", "mystrikingly.com",
    "framer.app", "framer.website", "notion.site", "super.site", "launchpass.com", "simplero.com",
}

# Domains that can never be an agency lead (platforms, social, aggregators, news, tools...).
BLOCKED_DOMAINS = {
    # social / video
    "facebook.com", "instagram.com", "linkedin.com", "twitter.com", "x.com", "youtube.com", "tiktok.com",
    "pinterest.com", "reddit.com", "threads.net", "snapchat.com", "vimeo.com", "quora.com", "medium.com",
    "substack.com", "tumblr.com", "discord.com", "t.me", "telegram.org", "whatsapp.com",
    # marketplaces / directories / review sites
    "clutch.co", "upwork.com", "fiverr.com", "freelancer.com", "toptal.com", "g2.com", "capterra.com",
    "trustpilot.com", "yelp.com", "goodfirms.co", "designrush.com", "sortlist.com", "sortlist.co.uk",
    "agencyvista.com", "upcity.com", "themanifest.com", "expertise.com", "crunchbase.com", "glassdoor.com",
    "indeed.com", "zoominfo.com", "apollo.io", "rocketreach.co", "owler.com", "dnb.com", "bark.com",
    "thumbtack.com", "angi.com", "manta.com", "yellowpages.com", "bbb.org", "trustradius.com", "producthunt.com",
    "semrush.com", "ahrefs.com", "similarweb.com", "builtwith.com", "wappalyzer.com", "hubspot.com",
    "ecosystem.hubspot.com", "mailchimp.com", "contra.com", "dribbble.com", "behance.net", "99designs.com",
    "awwwards.com", "sortlist.fr", "topagency.com", "agencyspotter.com", "credo.com", "getcredo.com",
    # knowledge / news / blogs
    "wikipedia.org", "forbes.com", "entrepreneur.com", "inc.com", "businessinsider.com", "techcrunch.com",
    "hbr.org", "nytimes.com", "theguardian.com", "bbc.com", "cnn.com", "huffpost.com", "yahoo.com",
    "msn.com", "prnewswire.com", "businesswire.com", "globenewswire.com", "prweb.com", "einpresswire.com",
    "issuu.com", "slideshare.net", "scribd.com", "wordpress.org", "wix.com", "squarespace.com",
    # platforms that are not agencies
    "google.com", "bing.com", "duckduckgo.com", "apple.com", "microsoft.com", "amazon.com", "shopify.com",
    "kajabi.com", "clickfunnels.com", "gohighlevel.com", "teachable.com", "thinkific.com", "podia.com",
    "kartra.com", "systeme.io", "leadpages.com", "unbounce.com", "instapage.com", "webflow.com",
    "zapier.com", "canva.com", "notion.so", "calendly.com", "typeform.com", "convertkit.com", "kit.com",
    "activecampaign.com", "klaviyo.com", "gumroad.com", "udemy.com", "coursera.org", "skillshare.com",
    "eventbrite.com", "meetup.com", "spotify.com", "apple.co", "podcasts.apple.com", "anchor.fm",
    "linktr.ee", "beacons.ai", "stan.store", "github.com", "gitlab.com", "stackoverflow.com",
    "neilpatel.com", "hootsuite.com", "sproutsocial.com", "buffer.com", "later.com",
    "wordstream.com", "adespresso.com", "bigcommerce.com", "woocommerce.com",
    "mailerlite.com", "getresponse.com", "aweber.com", "constantcontact.com", "sendinblue.com", "brevo.com",
    "hubspot.fr", "salesforce.com", "pipedrive.com", "zoho.com", "monday.com", "asana.com", "trello.com",
    "fiverr.co", "peopleperhour.com", "guru.com", "designhill.com", "agencyanalytics.com", "databox.com",
    "whop.com", "skool.com", "circle.so", "mighty.co", "mightynetworks.com", "patreon.com", "kickstarter.com",
    "archive.org", "web.archive.org", "glassdoor.co.uk", "ziprecruiter.com", "lever.co", "greenhouse.io",
    "workable.com", "wellfound.com", "angel.co", "ycombinator.com", "news.ycombinator.com", "coursehero.com",
    "chegg.com", "studocu.com", "quizlet.com", "jotform.com", "surveymonkey.com", "docs.google.com",
    "sites.google.com", "drive.google.com", "play.google.com", "apps.apple.com", "chrome.google.com",
    "amazon.co.uk", "ebay.com", "etsy.com", "alibaba.com", "aliexpress.com", "walmart.com",
}

SOCIAL_HOSTS = {
    "facebook.com": "facebook", "fb.com": "facebook", "instagram.com": "instagram", "linkedin.com": "linkedin",
    "twitter.com": "twitter", "x.com": "twitter", "youtube.com": "youtube", "youtu.be": "youtube",
    "tiktok.com": "tiktok", "pinterest.com": "pinterest", "threads.net": "threads", "vimeo.com": "vimeo",
}

TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "fbclid", "gclid", "msclkid",
    "ref", "referrer", "mc_cid", "mc_eid", "_ga", "yclid", "igshid", "si", "source",
}

_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*://", re.I)


def ensure_scheme(url: str) -> str:
    url = url.strip()
    if not url:
        return url
    if not _SCHEME_RE.match(url):
        url = "https://" + url.lstrip("/")
    return url


def registrable_domain(url_or_host: str) -> str:
    """``https://www.foo.co.uk/x`` → ``foo.co.uk``; multi-tenant hosts keep their subdomain."""
    s = url_or_host.strip().lower()
    if "://" in s:
        host = urlparse(s).hostname or ""
    else:
        host = s.split("/")[0].split(":")[0]
    host = host.strip(".")
    if not host:
        return ""
    ext = _extract(host)
    if not ext.suffix:
        return host
    reg = f"{ext.domain}.{ext.suffix}"
    if reg in MULTI_TENANT_HOSTS and ext.subdomain and ext.subdomain != "www":
        sub = ext.subdomain.split(".")[-1]
        return f"{sub}.{reg}"
    return reg


def host_of(url: str) -> str:
    return (urlparse(ensure_scheme(url)).hostname or "").lower()


def canonicalize(url: str, *, drop_fragment: bool = True, drop_tracking: bool = True) -> str:
    """Stable form of a URL for caching and de-duplication."""
    url = ensure_scheme(url)
    p = urlparse(url)
    host = (p.hostname or "").lower()
    port = f":{p.port}" if p.port and p.port not in (80, 443) else ""
    path = re.sub(r"/{2,}", "/", p.path or "/")
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    query = ""
    if p.query:
        pairs = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=False) if not (drop_tracking and k.lower() in TRACKING_PARAMS)]
        pairs.sort()
        query = urlencode(pairs)
    fragment = "" if drop_fragment else p.fragment
    return urlunparse((p.scheme.lower() or "https", host + port, path, "", query, fragment))


def absolutize(base: str, href: str) -> str | None:
    href = (href or "").strip()
    if not href or href.startswith(("#", "javascript:", "mailto:", "tel:", "data:", "sms:")):
        return None
    try:
        out = urljoin(base, href)
    except ValueError:
        return None
    if not out.startswith(("http://", "https://")):
        return None
    return out


def is_blocked_domain(domain: str) -> bool:
    d = domain.lower()
    if d in BLOCKED_DOMAINS:
        return True
    # subdomains of blocked platforms (e.g. business.linkedin.com)
    parts = d.split(".")
    for i in range(1, len(parts) - 1):
        if ".".join(parts[i:]) in BLOCKED_DOMAINS:
            return True
    return False


def social_network(url: str) -> str | None:
    host = host_of(url)
    for h, name in SOCIAL_HOSTS.items():
        if host == h or host.endswith("." + h):
            return name
    return None


def same_site(url_a: str, url_b: str) -> bool:
    return registrable_domain(url_a) == registrable_domain(url_b)


_ASSET_EXT = re.compile(
    r"\.(?:jpe?g|png|gif|webp|svg|ico|css|js|mjs|json|xml|pdf|zip|rar|7z|gz|mp4|mp3|wav|mov|avi|woff2?|ttf|eot|otf|csv|xlsx?|docx?|pptx?)(?:[?#].*)?$",
    re.I,
)


def looks_like_asset(url: str) -> bool:
    return bool(_ASSET_EXT.search(url))
