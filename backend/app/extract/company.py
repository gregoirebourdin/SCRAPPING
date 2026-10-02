"""Agency identity: name, tagline, description, location, founding year, team size."""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..fetch.crawler import CrawledSite
from ..util.text import clean_ws, years_in

_TITLE_SPLIT = re.compile(r"\s+[|\-–—•·:»]\s+|\s*\|\s*")
_GENERIC_TITLE = re.compile(r"^(home|homepage|welcome|index|untitled|official site|official website|website|site)$", re.I)

COUNTRY_TLD = {
    ".co.uk": "United Kingdom", ".uk": "United Kingdom", ".com.au": "Australia", ".au": "Australia", ".ca": "Canada", ".ie": "Ireland",
    ".nz": "New Zealand", ".co.nz": "New Zealand", ".sg": "Singapore", ".co.za": "South Africa", ".in": "India", ".ae": "United Arab Emirates",
    ".us": "United States", ".de": "Germany", ".fr": "France", ".es": "Spain", ".nl": "Netherlands", ".ph": "Philippines", ".pk": "Pakistan",
}
COUNTRY_WORDS = {
    "united states": "United States", "usa": "United States", "u.s.": "United States", "america": "United States",
    "united kingdom": "United Kingdom", "uk": "United Kingdom", "england": "United Kingdom", "scotland": "United Kingdom", "wales": "United Kingdom",
    "australia": "Australia", "canada": "Canada", "ireland": "Ireland", "new zealand": "New Zealand", "singapore": "Singapore",
    "south africa": "South Africa", "dubai": "United Arab Emirates", "uae": "United Arab Emirates", "india": "India", "philippines": "Philippines",
    "germany": "Germany", "netherlands": "Netherlands", "spain": "Spain", "portugal": "Portugal", "france": "France", "israel": "Israel",
    "pakistan": "Pakistan", "nigeria": "Nigeria", "malaysia": "Malaysia", "mexico": "Mexico", "brazil": "Brazil", "bali": "Indonesia", "thailand": "Thailand",
}
CITY_COUNTRY = {
    "new york": "United States", "nyc": "United States", "los angeles": "United States", "miami": "United States", "austin": "United States",
    "san diego": "United States", "san francisco": "United States", "chicago": "United States", "dallas": "United States", "houston": "United States",
    "denver": "United States", "phoenix": "United States", "scottsdale": "United States", "atlanta": "United States", "nashville": "United States",
    "boston": "United States", "seattle": "United States", "las vegas": "United States", "orlando": "United States", "tampa": "United States",
    "charlotte": "United States", "salt lake city": "United States", "boise": "United States", "portland": "United States", "philadelphia": "United States",
    "london": "United Kingdom", "manchester": "United Kingdom", "birmingham": "United Kingdom", "bristol": "United Kingdom", "leeds": "United Kingdom", "edinburgh": "United Kingdom", "glasgow": "United Kingdom",
    "sydney": "Australia", "melbourne": "Australia", "brisbane": "Australia", "perth": "Australia", "gold coast": "Australia", "adelaide": "Australia",
    "toronto": "Canada", "vancouver": "Canada", "montreal": "Canada", "calgary": "Canada", "ottawa": "Canada",
    "dublin": "Ireland", "auckland": "New Zealand", "wellington": "New Zealand", "cape town": "South Africa", "johannesburg": "South Africa",
    "lisbon": "Portugal", "barcelona": "Spain", "berlin": "Germany", "amsterdam": "Netherlands", "tel aviv": "Israel", "bali": "Indonesia",
}
US_STATE_RE = re.compile(r"\b([A-Z][a-zA-Z .]{2,30}),\s*(AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY)\b\.?\s*\d{5}?")
UK_POSTCODE_RE = re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b")
AU_STATE_RE = re.compile(r"\b(NSW|VIC|QLD|WA|SA|TAS|ACT|NT)\s+\d{4}\b")
CA_POSTCODE_RE = re.compile(r"\b[A-Z]\d[A-Z]\s?\d[A-Z]\d\b")
BASED_IN_RE = re.compile(r"\b(?:based|located|headquartered|hq|offices?|we are|we're|team)\s+(?:in|out of|from)\s+([A-Z][a-zA-Z]+(?:\s[A-Z][a-zA-Z]+){0,2})", re.I)
FOUNDED_RE = re.compile(r"\b(?:founded|established|since|started|launched)\s+(?:in\s+)?(19[89]\d|20[0-4]\d)\b", re.I)
TEAM_RE = re.compile(r"\b(?:team of|over|more than|with)\s+(\d{1,4})\+?\s+(?:people|team members|employees|specialists|experts|marketers|media buyers|staff)\b", re.I)
SIZE_WORDS_RE = re.compile(r"\b(boutique|small team|solo|one[- ]person|freelance|freelancer|we are a (small|lean|tight-knit) team|just me)\b", re.I)


@dataclass
class CompanyInfo:
    name: str
    tagline: str | None = None
    description: str | None = None
    country: str | None = None
    city: str | None = None
    founded_year: int | None = None
    team_size_hint: str | None = None
    copyright_year: int | None = None


def _org_from_jsonld(site: CrawledSite) -> dict:
    for p in site.pages:
        for d in p.parsed.jsonld:
            t = d.get("@type")
            types = t if isinstance(t, list) else [t]
            if any(str(x).lower() in ("organization", "localbusiness", "corporation", "marketingagency", "professionalservice", "advertisingagency") for x in types):
                return d
    return {}


def guess_name(site: CrawledSite) -> str:
    org = _org_from_jsonld(site)
    if org.get("name") and isinstance(org["name"], str) and 2 < len(org["name"]) < 80:
        return clean_ws(org["name"])
    home = site.home
    if home is None:
        return site.domain
    if home.parsed.og_site_name and len(home.parsed.og_site_name) < 80:
        return home.parsed.og_site_name
    title = home.parsed.title or home.parsed.og_title
    if title:
        parts = [p.strip() for p in _TITLE_SPLIT.split(title) if p.strip()]
        parts = [p for p in parts if not _GENERIC_TITLE.match(p)]
        if parts:
            dom_token = site.domain.split(".")[0].replace("-", "")
            # prefer the part that echoes the domain, otherwise the shortest
            for p in parts:
                if dom_token and dom_token in p.lower().replace(" ", "").replace("-", ""):
                    return p[:80]
            parts.sort(key=len)
            return parts[0][:80]
    return site.domain.split(".")[0].replace("-", " ").title()


def _location(site: CrawledSite) -> tuple[str | None, str | None]:
    org = _org_from_jsonld(site)
    addr = org.get("address")
    if isinstance(addr, dict):
        country = addr.get("addressCountry")
        if isinstance(country, dict):
            country = country.get("name")
        city = addr.get("addressLocality")
        if country or city:
            c = COUNTRY_WORDS.get(str(country).lower(), str(country)) if country else None
            if c in ("US", "USA"):
                c = "United States"
            return (c, city if isinstance(city, str) else None)

    text = "\n".join(p.text for p in site.pages if p.kind in ("home", "about", "contact"))[:60_000]
    low = text.lower()
    # explicit address formats
    if US_STATE_RE.search(text):
        m = US_STATE_RE.search(text)
        return ("United States", m.group(1).strip()[:40])
    if AU_STATE_RE.search(text):
        return ("Australia", None)
    if UK_POSTCODE_RE.search(text) and ("united kingdom" in low or " uk" in low or "london" in low):
        return ("United Kingdom", "London" if "london" in low else None)
    if CA_POSTCODE_RE.search(text) and "canada" in low:
        return ("Canada", None)
    m = BASED_IN_RE.search(text)
    if m:
        place = m.group(1).strip()
        pl = place.lower()
        if pl in CITY_COUNTRY:
            return (CITY_COUNTRY[pl], place)
        if pl in COUNTRY_WORDS:
            return (COUNTRY_WORDS[pl], None)
    for city, country in CITY_COUNTRY.items():
        if re.search(rf"\b{re.escape(city)}\b", low):
            return (country, city.title())
    for word, country in COUNTRY_WORDS.items():
        if len(word) > 3 and re.search(rf"\b{re.escape(word)}\b", low):
            return (country, None)
    for tld, country in COUNTRY_TLD.items():
        if site.domain.endswith(tld):
            return (country, None)
    return (None, None)


def extract_company(site: CrawledSite) -> CompanyInfo:
    name = guess_name(site)
    home = site.home
    tagline = None
    description = None
    if home is not None:
        h1 = next((h for h in home.parsed.headings if 8 < len(h) < 160), None)
        tagline = h1 or (home.parsed.og_title if home.parsed.og_title and home.parsed.og_title != name else None)
        description = home.parsed.meta_description or None
        if not description:
            for b in home.parsed.blocks:
                if 60 < len(b) < 400 and b.count(" ") > 8:
                    description = b
                    break
    country, city = _location(site)
    text = site.all_text[:200_000]
    founded = None
    org = _org_from_jsonld(site)
    fd = org.get("foundingDate")
    if isinstance(fd, str) and fd[:4].isdigit():
        founded = int(fd[:4])
    else:
        m = FOUNDED_RE.search(text)
        if m:
            founded = int(m.group(1))
    team = None
    m = TEAM_RE.search(text)
    if m:
        team = f"{m.group(1)}+ people"
    else:
        m = SIZE_WORDS_RE.search(text)
        if m:
            team = m.group(1).lower()
    copyright_year = None
    cm = re.findall(r"(?:©|&copy;|copyright)\s*(?:\d{4}\s*[-–]\s*)?(20[0-4]\d)", text, re.I)
    if cm:
        copyright_year = max(int(y) for y in cm)
    else:
        ys = years_in(text[-3000:])
        if ys:
            copyright_year = max(ys)
    return CompanyInfo(name=name, tagline=tagline, description=description, country=country, city=city, founded_year=founded, team_size_hint=team, copyright_year=copyright_year)
