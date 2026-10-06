"""Deterministic decision-maker extraction from cached website pages.

Sources (confidence): JSON-LD Person / Organization.founder|employee (0.9), team cards with a
recognized title (0.88), legal-notice roles (0.9), contact-page signatures (0.8), text patterns
such as "fondée par X" / "X, fondateur de …" (0.75), name-only lines on team pages (0.6).
Testimonial authors, client quotes and blog authors are never emitted as staff.
Every candidate carries the verbatim lines it came from.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from typing import Any

from rapidfuzz import fuzz

from scout.crawl.types import PageLike
from scout.db.enums import PageType, SourceType
from scout.extract.emails import classify_email, emails_on_domain, match_email_to_person
from scout.extract.legal import parse_legal_notice
from scout.extract.names import display_name, is_plausible_person_name, split_name
from scout.extract.titles import is_job_title, normalize_title
from scout.extract.types import PersonCandidate
from scout.util.text import (
    ascii_fold,
    collapse_ws,
    normalize_company_name,
    normalize_key,
    normalize_person_name,
)

CONF_JSONLD = 0.9
CONF_TEAM_CARD = 0.88
CONF_LEGAL = 0.9
CONF_SIGNATURE = 0.8
CONF_TEXT = 0.75
CONF_NAME_ONLY = 0.6
CORROBORATION_BONUS = 0.05
MAX_CONFIDENCE = 0.97

_TESTIMONIAL_RE = re.compile(
    r"t[ée]moignages?|\bavis\b|testimonials?|\breviews?\b|ils nous font confiance|ils parlent de nous|"
    r"ce que (?:disent|pensent) nos clients|what (?:our )?(?:clients|customers) say|nos clients|our clients|"
    r"kundenstimmen|rese[ñn]as|opiniones|recensioni|dicono di noi|clients? satisfaits|retours? clients?|"
    r"they trust us|trusted by|ils m['’]ont fait confiance|ils ont choisi|references clients|r[ée]f[ée]rences clients|"
    r"satisfaction client|case stud|cas clients?",
    re.IGNORECASE,
)
_QUOTE_LINE_RE = re.compile(r"^\s*[«“\"„‘']|[»”\"’]\s*$|«|»|“|”")
_ORG_TYPES = (
    "organization",
    "corporation",
    "localbusiness",
    "professionalservice",
    "store",
    "company",
    "ngo",
    "educationalorganization",
    "medicalorganization",
    "sportsorganization",
    "governmentorganization",
    "onlinebusiness",
    "onlinestore",
    "restaurant",
    "legalservice",
    "accountingservice",
    "financialservice",
)
_SELF_ORG_RE = re.compile(
    r"^(?:l['’]\s*|la\s+|le\s+|notre\s+|our\s+|this\s+|cette\s+)?(?:agence|entreprise|soci[ée]t[ée]|studio|cabinet|"
    r"company|agency|firm|maison|marque|brand|startup|start-up|structure|team|[ée]quipe)\b",
    re.IGNORECASE,
)

_NAME_PAT = (
    r"[A-ZÀ-ÖØ-Þ][\wÀ-ÖØ-öø-ÿ'’\-]+(?:\s+(?:de|du|des|la|le|van|von|der|den|di|da|d['’])?\s*"
    r"[A-ZÀ-ÖØ-Þ][\wÀ-ÖØ-öø-ÿ'’\-]+){1,3}"
)
_FOUNDER_TITLES = (
    r"co-?fondat(?:eur|rice)s?|fondat(?:eur|rice)s?|co-?founders?|founders?|ceo|pdg|g[ée]rante?|"
    r"pr[ée]sidente?|directeur\s+g[ée]n[ée]ral|directrice\s+g[ée]n[ée]rale|managing\s+director|dirigeante?|"
    r"owner|propri[ée]taire|associ[ée]e?|gesch[äa]ftsf[üu]hrer(?:in)?|inhaber(?:in)?|gr[üu]nder(?:in)?"
)
_FOUNDED_BY_RE = re.compile(
    r"(?i:(?:co-?)?(?:fond[ée]e?s?|cr[ée]{1,2}e?s?|lanc[ée]e?s?|founded|started|created|launched|gegr[üu]ndet|"
    r"fundada|fundado|fondata|fondato)(?:\s+(?:en|in|im|el|nel)\s+\d{4})?\s+(?:par|by|von|por|da))\s+"
    rf"(?P<names>{_NAME_PAT}(?:\s*(?:,|\bet\b|\band\b|\bund\b|\by\b|&)\s*{_NAME_PAT}){{0,2}})"
)
_NAME_COMMA_TITLE_RE = re.compile(
    rf"(?P<name>{_NAME_PAT})\s*,\s*(?:(?i:notre|our|le|la|l['’])\s+)?(?P<title>(?i:{_FOUNDER_TITLES}))"
    r"(?:\s+(?i:(?:et|and|&)\s+[\wÀ-ÿ\-]+))?(?:\s+(?i:de|d['’]|of|at|chez|du|bei|von)\s*(?P<org>[^,.;:!?\n]{2,60}))?"
)
_OUR_TITLE_NAME_RE = re.compile(
    rf"(?i:\b(?:notre|nos|our|mon|ma|unser(?:e|er)?)\s+)(?P<title>(?i:{_FOUNDER_TITLES}))\s*[,:]?\s+(?P<name>{_NAME_PAT})"
)
_INLINE_SEP_RE = re.compile(r"^(?P<a>[^–—|:()]{4,70}?)\s*(?:–|—|\||\s-\s|,|:)\s*(?P<b>[^–—|]{2,90})$")
_PAREN_RE = re.compile(r"^(?P<name>[^()]{4,60}?)\s*\((?P<title>[^)]{2,60})\)\s*$")


# ---- helpers -------------------------------------------------------------------------------------


def _ptype(page: PageLike) -> str:
    pt = getattr(page, "page_type", None)
    return str(pt.value if isinstance(pt, PageType) else pt or PageType.other.value)


def _lines(page: PageLike) -> list[str]:
    return [collapse_ws(line) for line in (page.content_text or "").split("\n")]


def _testimonial_zones(lines: list[str], headings: Iterable[str]) -> list[bool]:
    """Mark lines that belong to testimonial / client-review sections."""
    heads = {collapse_ws(h).lower() for h in headings if isinstance(h, str)}
    zone = False
    zone_len = 0
    out: list[bool] = []
    for line in lines:
        low = line.lower()
        title_like = len(line) <= 60 and _TESTIMONIAL_RE.fullmatch(low.strip(" :.!?")) is not None
        if low in heads or title_like:
            zone = title_like or (bool(_TESTIMONIAL_RE.search(low)) and len(line) <= 80)
            zone_len = 0
        elif zone:
            zone_len += 1
            if zone_len > 40:  # sections are short; never let a stray nav label swallow a page
                zone = False
        out.append(zone)
    return out


def _near_quote(lines: list[str], i: int) -> bool:
    for j in range(max(0, i - 2), min(len(lines), i + 3)):
        if j != i and lines[j] and _QUOTE_LINE_RE.search(lines[j]) and len(lines[j]) > 15:
            return True
    return False


def _org_matches(org: str | None, company_name: str) -> bool:
    """True when ``org`` (from "CEO de <org>") is the company itself or a self-reference."""
    if not org:
        return True
    org = collapse_ws(org)
    if _SELF_ORG_RE.match(org):
        return True
    a, b = normalize_company_name(org), normalize_company_name(company_name or "")
    if not a or not b:
        return False
    return a == b or a in b or b in a or fuzz.token_set_ratio(a, b) >= 85


def _title_mentions_other_org(title_line: str, company_name: str) -> bool:
    """'CEO, Boulangerie Martin' / 'Fondatrice chez X' → the person works elsewhere (a client)."""
    m = re.search(
        r"(?:,|\bchez\b|\bat\b|\b@\s*|\bde la soci[ée]t[ée]\b|\bfor\b|\bbei\b)\s*(?P<org>[^,]{2,60})$",
        title_line,
        re.IGNORECASE,
    )
    if not m:
        return False
    org = m.group("org").strip()
    if is_job_title(org) and not re.search(r"[A-Z]", org[1:]):
        return False  # "Fondatrice, directrice artistique"
    return not _org_matches(org, company_name)


def _best_name(s: str) -> str | None:
    """Longest plausible person-name prefix (≤ 4 tokens) of a captured capitalized sequence."""
    toks = collapse_ws(s).split(" ")
    for n in range(min(len(toks), 5), 1, -1):
        cand = " ".join(toks[:n]).strip(" ,.;:")
        if is_plausible_person_name(cand):
            return cand
    return None


def _is_company_name(name: str, company_name: str) -> bool:
    a, b = normalize_key(name), normalize_company_name(company_name or "")
    return bool(b) and (a == b or a == normalize_key(company_name or ""))


def _candidate(
    name: str,
    title: str | None,
    page: PageLike,
    *,
    method: str,
    evidence: str,
    confidence: float,
    source_type: str = SourceType.website.value,
) -> PersonCandidate:
    full = display_name(name)
    first, last = split_name(full)
    return PersonCandidate(
        full_name=full,
        first_name=first,
        last_name=last,
        title=collapse_ws(title) if title else None,
        source_url=page.url,
        source_type=source_type,
        method=method,
        evidence=evidence[:500],
        confidence=confidence,
        page_type=_ptype(page),
    )


# ---- sources -------------------------------------------------------------------------------------


def _types(obj: dict[str, Any]) -> list[str]:
    t = obj.get("@type")
    if isinstance(t, str):
        return [t.lower()]
    if isinstance(t, list):
        return [x.lower() for x in t if isinstance(x, str)]
    return []


def _jsonld_name(obj: Any) -> str | None:
    if isinstance(obj, str):
        return collapse_ws(obj)
    if not isinstance(obj, dict):
        return None
    name = obj.get("name")
    if isinstance(name, str) and name.strip():
        return collapse_ws(name)
    given, family = obj.get("givenName"), obj.get("familyName")
    if isinstance(given, str) and isinstance(family, str):
        return collapse_ws(f"{given} {family}")
    return None


def _jsonld_evidence(obj: Any, extra: dict[str, Any] | None = None) -> str:
    if isinstance(obj, dict):
        keep = {
            k: obj[k] for k in ("@type", "name", "givenName", "familyName", "jobTitle", "email") if k in obj
        }
        works = obj.get("worksFor")
        if isinstance(works, dict) and works.get("name"):
            keep["worksFor"] = works.get("name")
    else:
        keep = {"name": obj}
    if extra:
        keep = {**extra, **keep}
    return json.dumps(keep, ensure_ascii=False)[:400]


def _from_jsonld(page: PageLike, company_name: str) -> list[PersonCandidate]:
    pt = _ptype(page)
    staff_page = pt in (PageType.home, PageType.about, PageType.team, PageType.contact, PageType.legal)
    out: list[PersonCandidate] = []
    for obj in page.structured_data or []:
        if not isinstance(obj, dict):
            continue
        types = _types(obj)
        if any(t in _ORG_TYPES or t.endswith(("organization", "business")) for t in types):
            org_name = _jsonld_name(obj)
            if org_name and company_name and not _org_matches(org_name, company_name) and pt != PageType.home:
                continue
            for key, default_title in (
                ("founder", "Founder"),
                ("founders", "Founder"),
                ("employee", None),
                ("employees", None),
                ("member", None),
                ("members", None),
            ):
                vals = obj.get(key)
                for person in vals if isinstance(vals, list) else [vals] if vals else []:
                    name = _jsonld_name(person)
                    if not name or not is_plausible_person_name(name):
                        continue
                    title = person.get("jobTitle") if isinstance(person, dict) else None
                    title = title if isinstance(title, str) and title.strip() else default_title
                    ev = _jsonld_evidence(person, {"@type": types[0] if types else "Organization", key: name})
                    out.append(
                        _candidate(name, title, page, method="jsonld", evidence=ev, confidence=CONF_JSONLD)
                    )
        elif "person" in types:
            name = _jsonld_name(obj)
            if not name or not is_plausible_person_name(name):
                continue
            title = obj.get("jobTitle") if isinstance(obj.get("jobTitle"), str) else None
            works = obj.get("worksFor")
            works_name = _jsonld_name(works) if works else None
            if works_name and company_name and not _org_matches(works_name, company_name):
                continue
            if not staff_page and not (title and is_job_title(title)):
                continue  # blog/article authors are not staff unless their title says so
            out.append(
                _candidate(
                    name, title, page, method="jsonld", evidence=_jsonld_evidence(obj), confidence=CONF_JSONLD
                )
            )
            email = obj.get("email")
            if isinstance(email, str) and "@" in email:
                out[-1].email = email.replace("mailto:", "").strip().lower()
            same = obj.get("sameAs")
            for url in same if isinstance(same, list) else [same] if isinstance(same, str) else []:
                if isinstance(url, str) and "linkedin.com/in/" in url:
                    out[-1].profile_url = url
    return out


def _inline_name_title(line: str, company_name: str) -> tuple[str, str] | None:
    """'Jean Dupont – Fondateur', 'Jean Dupont, CEO', 'Fondatrice : Marie Martin', 'Jean Dupont (CEO)'."""
    if len(line) > 140:
        return None
    m = _PAREN_RE.match(line)
    if m:
        name, title = collapse_ws(m.group("name")), collapse_ws(m.group("title"))
        if is_plausible_person_name(name) and is_job_title(title):
            return name, title
    m = _INLINE_SEP_RE.match(line)
    if not m:
        return None
    a, b = collapse_ws(m.group("a")), collapse_ws(m.group("b"))
    if is_plausible_person_name(a) and is_job_title(b) and not _title_mentions_other_org(b, company_name):
        return a, b
    if is_job_title(a) and is_plausible_person_name(b):
        return b, a
    return None


def _from_cards(
    page: PageLike,
    company_name: str,
    *,
    method: str,
    confidence: float,
    allow_name_only: bool,
    strict_quotes: bool,
) -> list[PersonCandidate]:
    """Name line + adjacent (±2) title line. ``strict_quotes`` also rejects cards next to quotes
    (home pages, where client testimonials live); team pages often show a quote per member."""
    lines = _lines(page)
    zones = _testimonial_zones(lines, page.headings or [])
    n = len(lines)
    is_name = [False] * n
    is_title = [False] * n
    inline: list[tuple[str, str] | None] = [
        _inline_name_title(line, company_name) if line else None for line in lines
    ]
    for i, line in enumerate(lines):
        if not line or inline[i] is not None:
            continue  # "Name – Title" lines are complete cards, never a neighbour's title
        if len(line) <= 60 and is_plausible_person_name(line) and not _is_company_name(line, company_name):
            is_name[i] = True
        elif len(line) <= 90 and is_job_title(line):
            is_title[i] = True

    # Dominant card layout on this page: "Name\nTitle" (after) or "Title\nName" (before).
    after = sum(1 for i in range(n - 1) if is_name[i] and is_title[i + 1])
    before = sum(1 for i in range(1, n) if is_name[i] and is_title[i - 1])
    dominant = 1 if after >= before else -1
    offsets = (dominant, -dominant, 2 * dominant, -2 * dominant)

    out: list[PersonCandidate] = []
    for i, line in enumerate(lines):
        if zones[i] or not line:
            continue
        quoted = _near_quote(lines, i)
        if inline[i] is not None:
            if not (strict_quotes and quoted):
                name, title = inline[i]  # type: ignore[misc]
                out.append(_candidate(name, title, page, method=method, evidence=line, confidence=confidence))
            continue
        if not is_name[i] or (strict_quotes and quoted):
            continue
        title_idx = None
        for off in offsets:
            j = i + off
            if 0 <= j < n and is_title[j] and not zones[j]:
                # A title belongs to the name next to it in the page's dominant direction; never
                # steal another card's title.
                owner = j - dominant
                if 0 <= owner < n and is_name[owner] and owner != i:
                    continue
                if off in (2, -2) and is_name[i + off // 2]:
                    continue
                title_idx = j
                break
        if title_idx is not None:
            title = lines[title_idx]
            if _title_mentions_other_org(title, company_name):
                continue
            a, b = sorted((i, title_idx))
            evidence = "\n".join(lines[a : b + 1])
            out.append(_candidate(line, title, page, method=method, evidence=evidence, confidence=confidence))
        elif allow_name_only and not quoted and is_plausible_person_name(line, require_known_first_name=True):
            out.append(
                _candidate(line, None, page, method="name_only", evidence=line, confidence=CONF_NAME_ONLY)
            )
    return out


def _from_legal(page: PageLike) -> list[PersonCandidate]:
    info = parse_legal_notice(page.content_text or "")
    return [
        _candidate(lp.name, lp.role, page, method="legal_notice", evidence=lp.evidence, confidence=CONF_LEGAL)
        for lp in info.people
    ]


def _from_text(page: PageLike, company_name: str) -> list[PersonCandidate]:
    lines = _lines(page)
    zones = _testimonial_zones(lines, page.headings or [])
    out: list[PersonCandidate] = []
    for i, line in enumerate(lines):
        if not line or zones[i] or _QUOTE_LINE_RE.search(line[:1] or " ") or len(line) > 1200:
            continue
        for m in _FOUNDED_BY_RE.finditer(line):
            for raw in re.split(r"\s*(?:,|\bet\b|\band\b|\bund\b|\by\b|&)\s*", m.group("names")):
                name = _best_name(raw)
                if name and not _is_company_name(name, company_name):
                    out.append(
                        _candidate(
                            name,
                            "Founder",
                            page,
                            method="text_pattern",
                            evidence=_snippet(line, m.start(), m.end()),
                            confidence=CONF_TEXT,
                        )
                    )
        for m in _NAME_COMMA_TITLE_RE.finditer(line):
            name = _best_name(m.group("name"))
            if not name or not _org_matches(m.group("org"), company_name):
                continue
            out.append(
                _candidate(
                    name,
                    m.group("title"),
                    page,
                    method="text_pattern",
                    evidence=_snippet(line, m.start(), m.end()),
                    confidence=CONF_TEXT,
                )
            )
        for m in _OUR_TITLE_NAME_RE.finditer(line):
            name = _best_name(m.group("name"))
            if name:
                out.append(
                    _candidate(
                        name,
                        m.group("title"),
                        page,
                        method="text_pattern",
                        evidence=_snippet(line, m.start(), m.end()),
                        confidence=CONF_TEXT,
                    )
                )
    return out


def _snippet(line: str, start: int, end: int, pad: int = 80) -> str:
    a = max(0, start - pad)
    b = min(len(line), end + pad)
    return ("…" if a > 0 else "") + line[a:b].strip() + ("…" if b < len(line) else "")


# ---- merge & enrichment ------------------------------------------------------------------------


def _key(name: str) -> str:
    return " ".join(sorted(normalize_person_name(name).split()))


def _pick_display(cands: list[PersonCandidate]) -> str:
    # Prefer mixed-case spellings ("Jean Dupont") over registry ALL CAPS.
    for c in sorted(cands, key=lambda c: -c.confidence):
        if not c.full_name.isupper():
            return c.full_name
    return cands[0].full_name


def _merge(cands: list[PersonCandidate]) -> list[PersonCandidate]:
    groups: dict[str, list[PersonCandidate]] = {}
    for c in cands:
        groups.setdefault(_key(c.full_name), []).append(c)
    merged: list[PersonCandidate] = []
    for group in groups.values():
        group.sort(key=lambda c: -c.confidence)
        best = group[0]
        titled = [c for c in group if c.title]
        title_choice = None
        if titled:
            title_choice = max(
                titled,
                key=lambda c: (
                    normalize_title(c.title or "").decision_power,
                    c.confidence,
                    c.method != "legal_notice",
                ),
            )
        evidences: list[str] = []
        for c in group:
            if c.evidence and c.evidence not in evidences:
                evidences.append(c.evidence)
        distinct = {(c.method, c.source_url) for c in group}
        conf = best.confidence + (CORROBORATION_BONUS if len(distinct) >= 2 else 0.0)
        name = _pick_display(group)
        first, last = split_name(name)
        out = PersonCandidate(
            full_name=name,
            first_name=first,
            last_name=last,
            title=title_choice.title if title_choice else None,
            source_url=(title_choice or best).source_url,
            source_type=best.source_type,
            method=(title_choice or best).method,
            evidence="\n…\n".join(evidences)[:1000],
            confidence=round(min(MAX_CONFIDENCE, conf), 3),
            page_type=(title_choice or best).page_type,
            email=next((c.email for c in group if c.email), None),
            profile_url=next((c.profile_url for c in group if c.profile_url), None),
        )
        other_titles = sorted({c.title for c in titled if c.title and c.title != out.title})
        if other_titles:
            out.extra["other_titles"] = " | ".join(other_titles)
        methods = sorted({c.method for c in group})
        out.extra["methods"] = ",".join(methods)
        out.extra["sources"] = " ".join(sorted({c.source_url for c in group if c.source_url}))
        merged.append(out)
    return merged


def _attach_emails(people: list[PersonCandidate], pages: Sequence[PageLike], domain: str | None) -> None:
    published: dict[str, str] = {}
    for page in pages:
        for e in emails_on_domain(page.emails or [], domain) if domain else []:
            if classify_email(e) != "role":
                published.setdefault(e, page.url)
    if not published:
        return
    scores: dict[str, list[tuple[float, PersonCandidate]]] = {}
    for person in people:
        if person.email:
            continue
        for e in published:
            s = match_email_to_person(e, person.first_name, person.last_name)
            if s >= 0.8:
                scores.setdefault(e, []).append((s, person))
    for e, matches in scores.items():
        matches.sort(key=lambda t: -t[0])
        if len(matches) > 1 and matches[0][0] == matches[1][0]:
            continue  # ambiguous (two people share the pattern, e.g. same first name)
        person = matches[0][1]
        if not person.email:
            person.email = e
            person.extra["email_source_url"] = published[e]
            person.extra["email_match"] = f"{matches[0][0]:.2f}"


def _attach_profiles(people: list[PersonCandidate], pages: Sequence[PageLike]) -> None:
    profiles: list[dict[str, str]] = []
    for page in pages:
        links = page.links if isinstance(page.links, dict) else {}
        for p in links.get("people_profiles", []) or []:
            if isinstance(p, dict) and p.get("url"):
                profiles.append(p)
    for person in people:
        if person.profile_url or not person.first_name or not person.last_name:
            continue
        f = ascii_fold(person.first_name).lower().replace(" ", "-")
        la = ascii_fold(person.last_name).lower().replace(" ", "-")
        for p in profiles:
            slug = ascii_fold(p["url"]).lower()
            text = normalize_key(p.get("text") or "")
            if (f in slug and la in slug) or (text and text == normalize_key(person.full_name)):
                person.profile_url = p["url"]
                break


# ---- public API ----------------------------------------------------------------------------------


def extract_people(
    pages: Sequence[PageLike], *, company_name: str, domain: str | None
) -> list[PersonCandidate]:
    """Decision-maker candidates from cached pages, deduplicated by normalized name, best first."""
    raw: list[PersonCandidate] = []
    for page in pages:
        pt = _ptype(page)
        raw.extend(_from_jsonld(page, company_name))
        if pt == PageType.team:
            raw.extend(
                _from_cards(
                    page,
                    company_name,
                    method="team_card",
                    confidence=CONF_TEAM_CARD,
                    allow_name_only=True,
                    strict_quotes=False,
                )
            )
        elif pt == PageType.about:
            raw.extend(
                _from_cards(
                    page,
                    company_name,
                    method="team_card",
                    confidence=CONF_TEAM_CARD,
                    allow_name_only=False,
                    strict_quotes=False,
                )
            )
        elif pt == PageType.home:
            raw.extend(
                _from_cards(
                    page,
                    company_name,
                    method="team_card",
                    confidence=CONF_TEAM_CARD,
                    allow_name_only=False,
                    strict_quotes=True,
                )
            )
        elif pt == PageType.contact:
            raw.extend(
                _from_cards(
                    page,
                    company_name,
                    method="signature",
                    confidence=CONF_SIGNATURE,
                    allow_name_only=False,
                    strict_quotes=True,
                )
            )
        if pt == PageType.legal:
            raw.extend(_from_legal(page))
        if pt in (PageType.home, PageType.about, PageType.team, PageType.contact, PageType.services):
            raw.extend(_from_text(page, company_name))
    people = _merge(raw)
    _attach_emails(people, pages, domain)
    _attach_profiles(people, pages)
    people.sort(key=lambda p: (-p.confidence, -normalize_title(p.title or "").decision_power, p.full_name))
    return people
