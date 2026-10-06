"""Deterministic company facts from cached pages, each with source URL, verbatim evidence and
confidence: description, phone, email, employee-count hints, founded year, registry id (SIREN),
VAT, legal name/form, address / city / postal code and social profiles."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from scout.crawl.types import PageLike
from scout.db.enums import PageType
from scout.extract.emails import classify_email
from scout.extract.legal import parse_legal_notice
from scout.extract.social import extract_social_profiles
from scout.util.text import collapse_ws


@dataclass
class Fact:
    field_name: str
    value: Any
    source_url: str | None
    evidence: str
    confidence: float
    source_type: str = "website"


_STAFF_NOUNS = (
    r"personnes|collaborateurs|collaboratrices|collaborateur\(?s?\)?|salari[ée]s|employ[ée]s|experts|expertes|"
    r"consultants|consultantes|passionn[ée]s|talents|associ[ée]s|ing[ée]nieurs|d[ée]veloppeurs|designers|"
    r"cr[ée]atifs|sp[ée]cialistes|professionnels|membres|people|employees|staff|experts|specialists|"
    r"professionals|team members|consultants|engineers|mitarbeiter(?:innen)?|mitarbeitende|empleados|dipendenti"
)
_NUM = r"(\d{1,3}(?:[ ., ]\d{3})*|\d+)"
_EMP_PATTERNS: tuple[tuple[re.Pattern[str], str, float], ...] = (
    # ranges: "entre 10 et 20 collaborateurs", "10 à 20 salariés", "10-20 employees"
    (re.compile(rf"(?:entre|between)\s+{_NUM}\s+(?:et|and|à)\s+{_NUM}\s+(?:{_STAFF_NOUNS})", re.I), "range", 0.6),
    (re.compile(rf"{_NUM}\s*(?:à|-|–|to)\s*{_NUM}\s+(?:{_STAFF_NOUNS})", re.I), "range", 0.6),
    # lower bounds: "plus de 50 collaborateurs", "+50 experts", "over 100 employees"
    (re.compile(rf"(?:plus\s+de|more\s+than|over|au-del[àa]\s+de|mehr\s+als|m[áa]s\s+de|oltre)\s+{_NUM}\s+(?:{_STAFF_NOUNS})", re.I), "min", 0.55),
    (re.compile(rf"(?<![\w])\+\s?{_NUM}\s+(?:{_STAFF_NOUNS})", re.I), "min", 0.55),
    # exact: "une équipe de 12 personnes", "team of 25", "15 collaborateurs"
    (re.compile(rf"(?:[ée]quipe|team|teams)\s+(?:de|of|von|di)\s+{_NUM}(?:\s+(?:{_STAFF_NOUNS}))?", re.I), "exact", 0.65),
    (re.compile(rf"(?<![\d+.,])\b{_NUM}\s+(?:{_STAFF_NOUNS})\b", re.I), "exact", 0.6),
)
_FOUNDED_PATTERNS: tuple[tuple[re.Pattern[str], float], ...] = (
    (re.compile(r"(?:fond[ée]e?|cr[ée]{1,2}e?|cr[ée]ation|lanc[ée]e?|n[ée]e?|founded|established|incorporated|"
                r"gegr[üu]ndet|fundada|fundado|fondata|fondato)\s+(?:en|in|im|el|nel|le\s+\d{1,2}\s+\w+)?\s*((?:18|19|20)\d{2})\b", re.I), 0.8),
    (re.compile(r"\b(?:est\.|estd\.?|since|depuis|seit|desde|dal)\s+((?:18|19|20)\d{2})\b", re.I), 0.55),
    (re.compile(r"\b(?:en|in)\s+((?:19|20)\d{2})\b[^.\n]{0,40}\b(?:fond[ée]|cr[ée]{1,2}|founded|started|launched|lanc[ée])", re.I), 0.7),
)
_FR_POSTAL_LINE_RE = re.compile(
    r"(?<![\d€.,])(?P<cp>(?:0[1-9]|[1-8]\d|9[0-5]|97|98)\d{3})\s+(?P<city>[A-ZÉÈÀÂÎÔÛÇ][A-Za-zÀ-ÿ'’\-]+(?:[ \-](?:[Ss]ur|[Ll]es|[Ll]e|[Ll]a|de|du|en|[A-ZÉÈÀ][A-Za-zÀ-ÿ'’\-]+)){0,4})(?:\s+(?:Cedex|CEDEX)(?:\s+\d+)?)?"
)
_STREET_RE = re.compile(
    r"\b\d{1,4}(?:\s?(?:bis|ter|b|t))?,?\s+(?:rue|avenue|av\.?|boulevard|bd|place|pl\.?|chemin|all[ée]e|impasse|quai|"
    r"route|cours|square|passage|parvis|esplanade|faubourg|rond-point|voie|zone|za|zi|zac|parc|r[ée]sidence)\b[^\n]{2,80}",
    re.IGNORECASE,
)
_COOKIE_RE = re.compile(r"cookie|javascript|navigateur|browser|consent|rgpd|gdpr|copyright|©|tous droits", re.IGNORECASE)


def _ptype(page: PageLike) -> str:
    pt = getattr(page, "page_type", None)
    return str(pt.value if isinstance(pt, PageType) else pt or "other")


def _lines(page: PageLike) -> list[str]:
    return [collapse_ws(x) for x in (page.content_text or "").split("\n") if x.strip()]


def _int(s: str) -> int | None:
    digits = re.sub(r"[^\d]", "", s)
    return int(digits) if digits and len(digits) <= 6 else None


def _employee_facts(page: PageLike) -> list[Fact]:
    out: list[Fact] = []
    for line in _lines(page):
        if len(line) > 600:
            line = line[:600]
        for rx, kind, conf in _EMP_PATTERNS:
            m = rx.search(line)
            if not m:
                continue
            nums: list[int] = [n for n in (_int(g) for g in m.groups() if g) if n is not None]
            if not nums or not (1 <= nums[0] <= 100_000):
                continue
            value: dict[str, int | None]
            if kind == "range" and len(nums) >= 2 and nums[0] <= nums[1]:
                value = {"min": nums[0], "max": nums[1]}
            elif kind == "min":
                value = {"min": nums[0], "max": None}
            else:
                value = {"min": nums[0], "max": nums[0]}
            out.append(Fact("employee_count", value, page.url, line[:300], conf))
            break
    return out


def _founded_facts(page: PageLike) -> list[Fact]:
    out: list[Fact] = []
    this_year = datetime.now(UTC).year
    for line in _lines(page):
        for rx, conf in _FOUNDED_PATTERNS:
            m = rx.search(line)
            if m:
                year = int(m.group(1))
                if 1800 <= year <= this_year:
                    out.append(Fact("founded_year", year, page.url, line[:300], conf))
                    break
    return out


def _address_facts(page: PageLike, conf: float) -> list[Fact]:
    lines = _lines(page)
    out: list[Fact] = []
    for i, line in enumerate(lines):
        if len(line) > 300 or re.search(r"capital|€|euros?|SIRE[NT]|RCS|TVA", line, re.IGNORECASE):
            continue
        m = _FR_POSTAL_LINE_RE.search(line)
        if not m:
            continue
        cp, city = m.group("cp"), collapse_ws(m.group("city"))
        street = _STREET_RE.search(line)
        evidence = line
        if street is None and i > 0:
            street = _STREET_RE.search(lines[i - 1])
            if street is not None:
                evidence = f"{lines[i - 1]}\n{line}"
        out.append(Fact("postal_code", cp, page.url, evidence[:300], conf))
        out.append(Fact("city", city, page.url, evidence[:300], conf))
        if street is not None:
            street_txt = collapse_ws(street.group(0))
            street_txt = street_txt.split(cp)[0].strip(" ,–-") if cp in street_txt else street_txt
            out.append(Fact("address", f"{street_txt}, {cp} {city}", page.url, evidence[:300], conf))
        break
    return out


def _best(facts: list[Fact]) -> Fact | None:
    return max(facts, key=lambda f: f.confidence) if facts else None


def extract_company_facts(pages: list[PageLike]) -> list[Fact]:
    """Facts with provenance; at most one value per field (highest confidence, home/legal first)."""
    by_type: dict[str, list[PageLike]] = {}
    for p in pages:
        by_type.setdefault(_ptype(p), []).append(p)
    ordered = sorted(pages, key=lambda p: {"home": 0, "about": 1, "contact": 2, "legal": 3, "team": 4}.get(_ptype(p), 9))
    facts: list[Fact] = []

    # description
    desc: Fact | None = None
    for p in by_type.get("home", []) + by_type.get("about", []):
        md = collapse_ws(getattr(p, "meta_description", None) or "")
        if 40 <= len(md) <= 400 and not _COOKIE_RE.search(md):
            desc = Fact("description", md, p.url, md, 0.7)
            break
    if desc is None:
        for p in by_type.get("about", []) + by_type.get("home", []):
            for line in _lines(p):
                if 120 <= len(line) <= 800 and not _COOKIE_RE.search(line) and line.count(" ") >= 15:
                    desc = Fact("description", line, p.url, line[:300], 0.6)
                    break
            if desc:
                break
    if desc:
        facts.append(desc)

    # phone: contact page first, then home, then any page
    for p in sorted(pages, key=lambda p: {"contact": 0, "home": 1, "legal": 2}.get(_ptype(p), 5)):
        phones = getattr(p, "phones", None) or []
        if phones:
            phone = phones[0]
            digits = re.sub(r"\D", "", phone)[-9:]
            evidence = next((ln for ln in _lines(p) if digits and digits in re.sub(r"\D", "", ln)), phone)
            facts.append(Fact("phone", phone, p.url, evidence[:300], 0.8))
            break

    # public role email on the site (contact@…)
    for p in ordered:
        role = [e for e in (p.emails or []) if isinstance(e, str) and classify_email(e) == "role"]
        if role:
            facts.append(Fact("email", role[0], p.url, role[0], 0.8))
            break

    # employee count / founded year (about first)
    emp: list[Fact] = []
    founded: list[Fact] = []
    for p in by_type.get("about", []) + by_type.get("home", []) + by_type.get("team", []) + by_type.get("careers", []):
        emp.extend(_employee_facts(p))
        founded.extend(_founded_facts(p))
    if (b := _best(emp)) is not None:
        facts.append(b)
    if (b := _best(founded)) is not None:
        facts.append(b)

    # legal notice: registry id, VAT, legal name/form, address
    legal_address: list[Fact] = []
    for p in by_type.get("legal", []) + [p for p in pages if _ptype(p) != "legal"]:
        info = parse_legal_notice(p.content_text or "")
        if info.is_empty:
            continue
        conf = 0.95 if _ptype(p) == "legal" else 0.85
        ev = info.evidence
        if info.siren:
            facts.append(Fact("registry_id", info.siren, p.url, ev.get("siren", info.siren), conf))
        if info.siret:
            facts.append(Fact("siret", info.siret, p.url, ev.get("siret", info.siret), conf))
        if info.vat_number:
            facts.append(Fact("vat_number", info.vat_number, p.url, ev.get("vat_number", info.vat_number), conf))
        if info.register_number:
            facts.append(Fact("register_number", info.register_number, p.url, ev.get("register_number", ""), conf - 0.05))
        if info.legal_name:
            facts.append(Fact("legal_name", info.legal_name, p.url, ev.get("legal_name", info.legal_name), conf - 0.1))
        if info.legal_form:
            facts.append(Fact("legal_form", info.legal_form, p.url, ev.get("legal_form", info.legal_form), conf - 0.1))
        if info.share_capital:
            facts.append(Fact("share_capital", info.share_capital, p.url, ev.get("share_capital", ""), conf - 0.1))
        if info.address:
            legal_address.append(Fact("address", info.address, p.url, ev.get("address", info.address), 0.85))
            m = _FR_POSTAL_LINE_RE.search(info.address)
            if m:
                legal_address.append(Fact("postal_code", m.group("cp"), p.url, ev.get("address", ""), 0.85))
                legal_address.append(Fact("city", collapse_ws(m.group("city")), p.url, ev.get("address", ""), 0.85))
        break

    # address / city / postal code: legal siège first, then contact page, then home footer
    addr = legal_address
    if not addr:
        for p in by_type.get("contact", []) + by_type.get("home", []) + by_type.get("about", []):
            addr = _address_facts(p, 0.75 if _ptype(p) == "contact" else 0.65)
            if addr:
                break
    facts.extend(addr)

    # social profiles
    for network, (url, source_url) in extract_social_profiles(pages).items():
        facts.append(Fact(f"social_{network}", url, source_url, url, 0.9))
    return facts
