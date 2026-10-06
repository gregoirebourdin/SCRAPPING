"""Shared helpers for discovery adapters: ICP interpretation, polite HTTP, status → error mapping."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from typing import Any

import httpx
import structlog

from scout.config import get_settings
from scout.db.enums import ErrorCategory
from scout.discovery import geo
from scout.discovery.taxonomy import IndustryMatch, IndustryProfile, looks_french, match_industries_detailed
from scout.errors import BlockedError, FetchError, PermanentError, RateLimitedError
from scout.schemas.campaign import CampaignDefinition, KeywordCondition, TechnologyCondition
from scout.util.urls import is_company_domain, registrable_domain

log = structlog.get_logger(__name__)

# Hosts that are never a company's own site, beyond scout.util.urls.NON_COMPANY_DOMAINS:
# media, directories, marketplaces, job boards, review sites, generic platforms.
EXTRA_NON_COMPANY_DOMAINS = frozenset(
    {
        # directories / review / rankings
        "pagesjaunes.fr",
        "118712.fr",
        "annuaire-entreprises.data.gouv.fr",
        "data.gouv.fr",
        "entreprises.lefigaro.fr",
        "societeinfo.com",
        "corporama.com",
        "dnb.com",
        "zoominfo.com",
        "apollo.io",
        "rocketreach.co",
        "lusha.com",
        "yellowpages.com",
        "yell.com",
        "gelbeseiten.de",
        "paginasamarillas.es",
        "paginegialle.it",
        "goldenpages.ie",
        "foursquare.com",
        "mapquest.com",
        "waze.com",
        "bing.com",
        "here.com",
        "cylex.fr",
        "hoodspot.fr",
        "justacote.com",
        "starofservice.com",
        "habitatpresto.com",
        "travaux.com",
        "houzz.com",
        "houzz.fr",
        "thumbtack.com",
        "angi.com",
        "bark.com",
        "checkatrade.com",
        "agencies.semrush.com",
        "expertise.com",
        "themanifest.com",
        "agencyspotter.com",
        "g2.com",
        "capterra.com",
        "capterra.fr",
        "getapp.com",
        "producthunt.com",
        "trustradius.com",
        "appvizer.fr",
        "tracxn.com",
        "dealroom.co",
        "pitchbook.com",
        "owler.com",
        "cbinsights.com",
        "f6s.com",
        "wellfound.com",
        "angel.co",
        "ycombinator.com",
        "ziprecruiter.com",
        "monster.com",
        "monster.fr",
        "apec.fr",
        "hellowork.com",
        "jobteaser.com",
        "francetravail.fr",
        "pole-emploi.fr",
        "stepstone.de",
        "xing.com",
        "lefigaro.fr",
        "lemonde.fr",
        "lesechos.fr",
        "bfmtv.com",
        "journaldunet.com",
        "journaldunet.fr",
        "maddyness.com",
        "frenchweb.fr",
        "challenges.fr",
        "forbes.com",
        "forbes.fr",
        "techcrunch.com",
        "nytimes.com",
        "theguardian.com",
        "bbc.co.uk",
        "bbc.com",
        "reuters.com",
        "bloomberg.com",
        "businessinsider.com",
        "inc.com",
        "entrepreneur.com",
        "20minutes.fr",
        "ouest-france.fr",
        "leparisien.fr",
        "lyoncapitale.fr",
        "leprogres.fr",
        "actu.fr",
        "francebleu.fr",
        "huffingtonpost.fr",
        "lexpress.fr",
        "capital.fr",
        "zdnet.fr",
        "zdnet.com",
        "wired.com",
        "spiegel.de",
        "elpais.com",
        "corriere.it",
        "reddit.com",
        "quora.com",
        "youtube.com",
        "vimeo.com",
        "dailymotion.com",
        "wikimedia.org",
        "wiktionary.org",
        "fandom.com",
        "tumblr.com",
        "substack.com",
        "notion.site",
        "notion.so",
        "google.fr",
        "google.de",
        "google.co.uk",
        "maps.google.com",
        "airbnb.com",
        "airbnb.fr",
        "booking.com",
        "expedia.com",
        "hotels.com",
        "thefork.fr",
        "lafourchette.com",
        "ubereats.com",
        "deliveroo.fr",
        "justeat.fr",
        "planity.com",
        "treatwell.fr",
        "resalib.fr",
        "maiia.com",
        "jameda.de",
        "zocdoc.com",
        "leboncoin.fr",
        "ebay.com",
        "ebay.fr",
        "cdiscount.com",
        "aliexpress.com",
        "alibaba.com",
        "shopify.com",
        "greenhouse.io",
        "lever.co",
        "ashbyhq.com",
        "workable.com",
        "smartrecruiters.com",
        "recruitee.com",
        "breezy.hr",
        "applytojob.com",
        "bamboohr.com",
        "teamtailor.com",
        "personio.de",
        "jobs.personio.de",
        "workatastartup.com",
        "factorialhr.com",
        "grnh.se",
        "jobvite.com",
        "icims.com",
        "myworkdayjobs.com",
        "jazzhr.com",
        "homerun.co",
        "pinpointhq.com",
        "withgoogle.com",
        "join.com",
        "jobs.ch",
        "jobup.ch",
        "otta.com",
        "builtin.com",
        "docs.google.com",
        "forms.gle",
        "typeform.com",
        "bit.ly",
        "lnkd.in",
        "t.co",
        "tinyurl.com",
        "archive.org",
        "gouv.fr",
        "service-public.fr",
        "europa.eu",
        "legifrance.gouv.fr",
        "insee.fr",
        "urssaf.fr",
        "impots.gouv.fr",
    }
)

_GOV_EDU_SUFFIXES = (
    ".gouv.fr",
    ".gov",
    ".gov.uk",
    ".gov.au",
    ".gc.ca",
    ".edu",
    ".ac.uk",
    ".edu.au",
    ".int",
    ".mil",
    ".europa.eu",
    ".admin.ch",
    ".bund.de",
    ".gob.es",
    ".gov.it",
    ".overheid.nl",
    ".fgov.be",
)


def is_candidate_domain(domain: str | None) -> bool:
    """True when ``domain`` can plausibly be a company's own website (not social/directory/media/gov/edu)."""
    if not domain or not is_company_domain(domain) or domain in EXTRA_NON_COMPANY_DOMAINS:
        return False
    return not any(domain.endswith(s) or domain == s.lstrip(".") for s in _GOV_EDU_SUFFIXES)


def candidate_domain(url: str | None) -> str | None:
    """Registrable domain of ``url`` when it looks like a company website, else None."""
    dom = registrable_domain(url) if url else None
    return dom if is_candidate_domain(dom) else None


# ---- ICP interpretation ---------------------------------------------------------------------


def industry_texts(defn: CampaignDefinition) -> list[str]:
    cf = defn.company_filters
    return [t for t in [*cf.industries, *cf.keywords] if t and t.strip()]


def industry_matches(defn: CampaignDefinition) -> list[IndustryMatch]:
    """Profiles for the ICP's industries/keywords. Per phrase, keeps the best match and close runners-up."""
    best: dict[str, IndustryMatch] = {}
    for text in industry_texts(defn):
        matches = match_industries_detailed(text)
        if not matches:
            continue
        top = matches[0].score
        for m in matches:
            if m.score >= top - 3 and (m.profile.key not in best or m.score > best[m.profile.key].score):
                best[m.profile.key] = m
    return sorted(best.values(), key=lambda m: -m.score)


def profiles_for(defn: CampaignDefinition) -> list[IndustryProfile]:
    """Taxonomy profiles of the ICP, plus — for any activity the taxonomy does not know — a profile built from
    the definition itself (its words for web search, its NAF codes for the registry)."""
    profiles = [m.profile for m in industry_matches(defn)]
    cf = defn.company_filters
    known_naf = {c for p in profiles for c in p.naf_codes}
    extra_naf = [c for c in cf.naf_codes if c not in known_naf]
    texts = [t for t in [*cf.industries, *cf.keywords] if t and t.strip()]
    if extra_naf or (texts and not profiles):
        profiles.append(_custom_profile(texts, extra_naf, local=bool(cf.cities)))
    return profiles


def _custom_profile(texts: list[str], naf: list[str], *, local: bool) -> IndustryProfile:
    words = texts[:3] or ["company"]
    return IndustryProfile(
        key="custom:" + "|".join(words)[:60],
        labels={lang: words for lang in ("en", "fr", "de", "es", "it")},
        naf_codes=naf,
        maps_queries={lang: words[:2] for lang in ("en", "fr", "de", "es", "it")},
        osm_tags=[],
        search_keywords={lang: words[:2] for lang in ("en", "fr", "de", "es", "it")},
        yc_industries=[],
        local_business=local,
        digital=False,
    )


def target_countries(defn: CampaignDefinition) -> list[str]:
    """Explicit ISO countries, else inferred from cities / regions."""
    cf = defn.company_filters
    if cf.countries:
        out = []
        for c in cf.countries:
            code = geo.country_code(c) or c.upper()
            out.append(code)
        return list(dict.fromkeys(out))
    return geo.infer_countries(cf.cities, cf.regions)


def is_french_context(defn: CampaignDefinition) -> bool:
    """FR explicitly targeted, or no country given but French geography / phrasing."""
    countries = target_countries(defn)
    if countries:
        return "FR" in countries
    return any(looks_french(t) for t in industry_texts(defn))


def employee_bounds(defn: CampaignDefinition) -> tuple[int | None, int | None]:
    er = defn.company_filters.employee_range
    return (er.min, er.max) if er else (None, None)


def website_terms(defn: CampaignDefinition, limit: int = 2) -> list[str]:
    """Deterministic website-condition terms ('instagram', 'manychat') usable as search refinements."""
    terms: list[str] = []
    for cond in defn.website_conditions:
        if isinstance(cond, KeywordCondition):
            terms.extend(t.strip() for t in cond.terms if t.strip())
        elif isinstance(cond, TechnologyCondition):
            # "agences social media (potentiellement) ManyChat": also search agencies that name the tool
            terms.extend(t.strip() for t in cond.technologies if t.strip())
    return list(dict.fromkeys(terms))[:limit]


def is_local_icp(profiles: list[IndustryProfile]) -> bool:
    return bool(profiles) and profiles[0].local_business


def is_digital_icp(profiles: list[IndustryProfile]) -> bool:
    return bool(profiles) and profiles[0].digital


def size_fits(team_size: int | None, bounds: tuple[int | None, int | None]) -> bool:
    lo, hi = bounds
    if team_size is None:
        return True
    if lo is not None and team_size < lo:
        return False
    return not (hi is not None and team_size > hi)


# ---- polite HTTP ----------------------------------------------------------------------------


class Throttle:
    """Minimum spacing between requests to one upstream (loop-safe, process-wide)."""

    def __init__(self, min_interval: float) -> None:
        self.min_interval = min_interval
        self._next = 0.0
        self._lock: asyncio.Lock | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    async def wait(self) -> None:
        if self.min_interval <= 0:
            return
        loop = asyncio.get_running_loop()
        if self._lock is None or self._loop is not loop:
            self._lock, self._loop = asyncio.Lock(), loop
        async with self._lock:
            now = time.monotonic()
            delay = self._next - now
            if delay > 0:
                await asyncio.sleep(delay)
            self._next = max(now, self._next) + self.min_interval


def default_headers(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    headers = {"User-Agent": get_settings().crawler_user_agent, "Accept-Language": "en,fr;q=0.8"}
    if extra:
        headers.update(extra)
    return headers


def check_status(resp: httpx.Response, source: str, *, allow: tuple[int, ...] = ()) -> None:
    """Map upstream HTTP statuses to job errors (429 → rate limited, 403 → blocked, 5xx → retryable)."""
    code = resp.status_code
    if code < 400 or code in allow:
        return
    if code == 429:
        retry_after = resp.headers.get("retry-after")
        raise RateLimitedError(
            f"{source}: rate limited (429{', retry-after ' + retry_after if retry_after else ''})"
        )
    if code == 403:
        raise BlockedError(f"{source}: access blocked (403)")
    if code >= 500:
        raise FetchError(f"{source}: upstream error {code}")
    raise PermanentError(f"{source}: request rejected ({code}): {resp.text[:200]}")


async def http_request(
    method: str,
    url: str,
    *,
    source: str,
    params: Mapping[str, Any] | None = None,
    data: Mapping[str, Any] | str | None = None,
    json: Any = None,
    headers: Mapping[str, str] | None = None,
    timeout_s: float = 20.0,
    allow: tuple[int, ...] = (),
) -> httpx.Response:
    """One request to a fixed public API endpoint (not for arbitrary third-party URLs: use scout.crawl.http)."""
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s, connect=min(10.0, timeout_s)),
            headers=default_headers(headers),
            follow_redirects=True,
        ) as client:
            if isinstance(data, str):
                resp = await client.request(method, url, params=params, content=data, json=json)
            else:
                resp = await client.request(method, url, params=params, data=data, json=json)
    except httpx.TimeoutException as exc:
        raise FetchError(f"{source}: timeout", category=ErrorCategory.timeout) from exc
    except httpx.TransportError as exc:
        raise FetchError(f"{source}: network error: {exc.__class__.__name__}") from exc
    check_status(resp, source, allow=allow)
    return resp
