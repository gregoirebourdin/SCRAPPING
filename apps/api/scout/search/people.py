"""Decision-maker discovery from free web search results — the step before any Gemini grounding.

At most two free lookups per company (``lookup_chain``, SearXNG by default):

1. public professional profiles — ``"Pixel Studio" (fondateur OR gérant OR CEO) site:linkedin.com``; profile
   result titles read "Jean Dupont - Fondateur - Pixel Studio | LinkedIn";
2. otherwise the open web — ``"Pixel Studio" (fondateur OR gérant OR CEO)``; press / directory snippets such
   as "Jean Dupont, fondateur de Pixel Studio" or "Pixel Studio, fondée par Jean Dupont".

A person is kept only when the result names the company (or its domain) and the name passes
``is_plausible_person_name``. Nothing is invented: name, title and evidence are verbatim search result text.
Telemetry: ``people.source`` attempts under ``linkedin_snippet`` (profile query) and ``searxng_result`` (open
web query), the keys ``scout.learning.people`` derives for these candidates at feedback time.
Provenance: ``source_type="search_snippet"`` (quality 0.6), ``method="search_result"``; LinkedIn profile URLs
are kept as ``profile_url`` (search result data only — LinkedIn itself is never fetched).
"""

from __future__ import annotations

import re
import time
from collections.abc import Sequence
from urllib.parse import urlsplit

from scout.extract.names import (
    is_known_first_name,
    is_plausible_person_name,
    looks_like_company_name,
    split_name,
)
from scout.extract.titles import is_job_title
from scout.extract.types import PersonCandidate
from scout.search.assess import assess_subject, mentions_subject
from scout.search.chain import SearchChain, gemini_fallback_only, lookup_chain
from scout.search.telemetry import record_people_source
from scout.search.types import SearchResult
from scout.util.text import collapse_ws, normalize_person_name
from scout.util.urls import registrable_domain

CONF_PROFILE = 0.65  # profile title "Name - Title - Company"
CONF_PROFILE_SNIPPET = 0.55  # company only in the profile snippet
CONF_OPEN_WEB = 0.55  # "Name, title de Company" in a press / directory snippet
MIN_QUERY_S = 2.0  # time left below which another lookup query is not worth starting

DEFAULT_ROLES: dict[str, list[str]] = {
    "fr": ["fondateur", "gérant", "CEO", "dirigeant"],
    "en": ["founder", "CEO", "owner", "managing director"],
    "de": ["Gründer", "Geschäftsführer", "CEO", "Inhaber"],
    "es": ["fundador", "CEO", "director general", "gerente"],
    "it": ["fondatore", "CEO", "titolare", "amministratore delegato"],
    "nl": ["oprichter", "CEO", "eigenaar", "directeur"],
    "pt": ["fundador", "CEO", "diretor geral", "sócio"],
}
_LINKEDIN_SUFFIX = re.compile(r"\s*(?:[|\-–—]\s*)?LinkedIn(?:\s+[A-Z][a-zé]+)?\s*$", re.I)
_SEP = re.compile(r"\s+[|\-–—·•]\s+")
_AT = re.compile(r"\s+(?:chez|at|@|bei|en|à|em|presso|bij)\s+", re.I)
_FORMER = re.compile(
    r"\b(ancien(ne)?|anciennement|pr[ée]c[ée]demment|former(ly)?|previously|past|ex-|ehemalige[rn]?|antiguo|ex)\b",
    re.I,
)
_NAME = (
    r"[A-ZÀ-ÖØ-Ý][a-zà-öø-ÿ'’\-]+(?:\s+(?:de\s+|du\s+|van\s+|von\s+|da\s+|di\s+|del\s+|le\s+|la\s+)?"
    r"[A-ZÀ-ÖØ-Ý][A-Za-zà-öø-ÿ'’\-]+){1,3}"
)
_NAME_TITLE_RE = re.compile(
    rf"(?P<name>{_NAME}),\s+(?P<title>[^,.;:()|]{{2,60}}?)\s+(?:de|d['’]|du|des|of|at|chez|bei|von|@)\s*"
    r"(?P<org>[^,.;:()|]{2,60})"
)
_FOUNDED_BY_RE = re.compile(
    rf"(?P<verb>fond[ée]+e?s?|co-?fond[ée]+e?s?|cr[ée]+e?s?|dirig[ée]+e?s?|founded|co-?founded|led|run)"
    rf"\s+(?:en\s+\d{{4}}\s+|in\s+\d{{4}}\s+)?(?:par|by)\s+(?P<name>{_NAME})"
)
_VERB_TITLE = {
    "fond": "Fondateur",
    "cr": "Fondateur",
    "dirig": "Dirigeant",
    "found": "Founder",
    "co": "Co-founder",
}


def _roles(titles: Sequence[str], lang: str | None) -> list[str]:
    wanted = [t.strip() for raw in titles for t in re.split(r"\s*[/,|;]\s*", raw or "") if t.strip()]
    return list(dict.fromkeys(wanted))[:4] or DEFAULT_ROLES.get(lang or "en", DEFAULT_ROLES["en"])


PROFILE_KEY = "linkedin_snippet"  # people.source keys, as derived by scout.learning.people.people_source_key
OPEN_WEB_KEY = "searxng_result"


def people_queries(company_name: str, roles: Sequence[str]) -> list[tuple[str, str | None, str]]:
    """(query, site, people.source key) triples, public profiles first."""
    role_expr = " OR ".join(f'"{r}"' if " " in r else r for r in roles)
    base = f'"{collapse_ws(company_name)}" ({role_expr})'
    return [(base, "linkedin.com", PROFILE_KEY), (base, None, OPEN_WEB_KEY)]


def _title_from(segment: str, company_name: str, domain: str | None) -> str | None:
    """Job title in a profile segment: "CEO", "Fondateur chez Pixel Studio", "CEO @ Pixel Studio"."""
    seg = collapse_ws(segment)
    if _FORMER.search(seg):
        return None
    head = _AT.split(seg, maxsplit=1)[0].strip(" ,")
    for cand in (head, seg):
        if (
            cand
            and is_job_title(cand)
            and not mentions_subject(
                SearchResult(url="", title=cand, snippet=""), names=[company_name], domain=None
            )
        ):
            return cand
    return None


def _clean_name(raw: str) -> str:
    name = re.sub(r"\s*[,(].*$", "", raw).strip()  # "Jean Dupont, MBA" / "Jean Dupont (il/lui)"
    return re.sub(r"\s+(?:MBA|PhD|Dr\.?)$", "", name).strip()


def _best_name(raw: str) -> str | None:
    """Person name inside a capitalized run ("Selon Jean Dupont" → "Jean Dupont"), else None."""
    toks = _clean_name(raw).split()
    for i in range(len(toks) - 1):  # longest run starting with a known first name
        cand = " ".join(toks[i:])
        if is_known_first_name(toks[i]) and is_plausible_person_name(cand):
            return cand
    full = " ".join(toks)
    return full if len(toks) <= 3 and is_plausible_person_name(full) else None


def _candidate(
    name: str,
    title: str | None,
    r: SearchResult,
    *,
    confidence: float,
    profile: bool,
) -> PersonCandidate:
    first, last = split_name(name)
    evidence = collapse_ws(f"{r.title} — {r.snippet}")[:400]
    return PersonCandidate(
        full_name=name,
        first_name=first,
        last_name=last,
        title=title,
        source_url=r.url,
        source_type="search_snippet",
        method="search_result",
        evidence=evidence,
        confidence=confidence,
        profile_url=r.url if profile else None,
        extra={"engines": ",".join(r.engines)} if r.engines else {},
    )


def from_profile_result(r: SearchResult, *, company_name: str, domain: str | None) -> PersonCandidate | None:
    """A public LinkedIn profile result naming the company → candidate (else None)."""
    if registrable_domain(r.url) != "linkedin.com" or not urlsplit(r.url).path.startswith(("/in/", "/pub/")):
        return None
    segs = [s.strip() for s in _SEP.split(_LINKEDIN_SUFFIX.sub("", r.title).strip()) if s.strip()]
    if not segs:
        return None
    name = _clean_name(segs[0])
    if not is_plausible_person_name(name) or looks_like_company_name(name, company_name):
        return None
    rest = segs[1:]
    if any(_FORMER.search(s) for s in rest):
        return None  # "Ancien CEO - Pixel Studio": not a current decision maker
    in_title = any(
        mentions_subject(SearchResult(url="", title=s, snippet=""), names=[company_name], domain=domain)
        and not _FORMER.search(s)
        for s in rest
    )
    in_snippet = mentions_subject(
        SearchResult(url="", title="", snippet=r.snippet), names=[company_name], domain=domain
    )
    if not in_title and (not in_snippet or len(rest) >= 2 or _FORMER.search(r.snippet)):
        return None  # "Name - Headline - Other Corp": the current employer named in the title is not ours
    title = next((t for t in (_title_from(s, company_name, domain) for s in rest) if t), None)
    if title is None:
        for sentence in re.split(r"[.·|\n]", r.snippet):
            if mentions_subject(
                SearchResult(url="", title=sentence, snippet=""), names=[company_name], domain=domain
            ):
                title = _title_from(sentence, company_name, domain)
                if title:
                    break
    return _candidate(
        name, title, r, confidence=CONF_PROFILE if in_title else CONF_PROFILE_SNIPPET, profile=True
    )


def from_open_web_result(r: SearchResult, *, company_name: str, domain: str | None) -> list[PersonCandidate]:
    """'Jean Dupont, fondateur de Pixel Studio' / 'Pixel Studio, fondée par Jean Dupont' in a title/snippet."""
    out: list[PersonCandidate] = []
    for sentence in re.split(r"(?<=[.!?])\s+|\s+[|·]\s+", f"{r.title}. {r.snippet}"):
        if not mentions_subject(
            SearchResult(url="", title=sentence, snippet=""), names=[company_name], domain=domain
        ):
            continue
        for m in _NAME_TITLE_RE.finditer(sentence):
            name, title, org = _best_name(m["name"]), collapse_ws(m["title"]), m["org"]
            if not name or _FORMER.search(title) or not is_job_title(title):
                continue
            if not mentions_subject(
                SearchResult(url="", title=org, snippet=""), names=[company_name], domain=domain
            ):
                continue
            if not looks_like_company_name(name, company_name):
                out.append(_candidate(name, title, r, confidence=CONF_OPEN_WEB, profile=False))
        for m in _FOUNDED_BY_RE.finditer(sentence):
            name = _best_name(m["name"])
            verb = m["verb"].lower()
            title = next((v for k, v in _VERB_TITLE.items() if verb.startswith(k)), "Dirigeant")
            if verb.startswith(("led", "run")):
                title = "CEO"
            if name and not looks_like_company_name(name, company_name):
                out.append(_candidate(name, title, r, confidence=CONF_OPEN_WEB, profile=False))
    return out


def people_from_results(
    results: Sequence[SearchResult], *, company_name: str, domain: str | None, max_people: int = 5
) -> list[PersonCandidate]:
    """Deduplicated candidates (best confidence first, then a titled one over an untitled one)."""
    best: dict[str, PersonCandidate] = {}
    for r in results:
        found: list[PersonCandidate] = []
        prof = from_profile_result(r, company_name=company_name, domain=domain)
        if prof is not None:
            found.append(prof)
        elif registrable_domain(r.url) != "linkedin.com":
            found.extend(from_open_web_result(r, company_name=company_name, domain=domain))
        for c in found:
            key = normalize_person_name(c.full_name)
            prev = best.get(key)
            if prev is None or (c.confidence, c.title is not None) > (
                prev.confidence,
                prev.title is not None,
            ):
                best[key] = c
    ranked = sorted(best.values(), key=lambda c: (-c.confidence, c.title is None))
    return ranked[:max_people]


async def serp_people(
    company_name: str,
    *,
    domain: str | None = None,
    country: str | None = None,
    titles: Sequence[str] = (),
    chain: SearchChain | None = None,
    max_people: int = 5,
    deadline: float | None = None,
) -> list[PersonCandidate]:
    """Decision makers found by free web search; [] when unresolved (callers then try Gemini grounding).

    No-op (returns []) when ``GEMINI_SEARCH_FALLBACK_ONLY`` is off (legacy: Gemini directly) or when no lookup
    provider is configured / healthy — the pipeline then behaves exactly as before. A query is not started
    once the lookup chain is cooling down or less than ``MIN_QUERY_S`` remain before ``deadline``
    (``time.monotonic()`` clock).
    """
    if not company_name or not gemini_fallback_only():
        return []
    chain = chain or lookup_chain("people")
    if not chain.available():
        return []
    from scout.discovery import geo

    region = (country or "").upper() or None
    lang = geo.country_language(region) if region else None
    found: list[PersonCandidate] = []
    for query, site, key in people_queries(company_name, _roles(titles, lang)):
        started = time.monotonic()
        if (deadline is not None and deadline - started < MIN_QUERY_S) or not chain.available():
            break
        res = await chain.search(
            query,
            num=10,
            lang=lang,
            region=region,
            site=site,
            assess=lambda rs: assess_subject(rs, names=[company_name], domain=domain),
        )
        if res.provider is None:
            continue  # nobody answered: not an attempt of this people source
        if res.sufficient:
            found = people_from_results(
                res.usable, company_name=company_name, domain=domain, max_people=max_people
            )
        await record_people_source(
            key, produced=bool(found), latency_ms=int((time.monotonic() - started) * 1000)
        )
        if found:
            break
    return found
