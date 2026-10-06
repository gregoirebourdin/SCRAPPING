"""Legal notice parsing: FR "mentions légales", DE "Impressum", UK company information.

Identifiers are validated (SIREN/SIRET Luhn, FR VAT key); people are only returned when their
name passes the person-name checks. Every field carries the verbatim snippet it came from.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from scout.extract.names import is_plausible_person_name
from scout.util.text import collapse_ws

_D = r"\d[\s.  ]?"
SIREN_RE = re.compile(rf"(?<!\d)((?:{_D}){{8}}\d)(?![\d])")


@dataclass
class LegalPerson:
    name: str
    role: str  # verbatim role label, e.g. "Directeur de la publication", "Gérant", "Geschäftsführer"
    evidence: str


@dataclass
class LegalInfo:
    siren: str | None = None
    siret: str | None = None
    rcs: str | None = None
    vat_number: str | None = None
    share_capital: str | None = None
    legal_name: str | None = None
    legal_form: str | None = None
    publication_director: str | None = None
    legal_representative: str | None = None
    address: str | None = None
    legal_representative_role: str | None = None
    register_number: str | None = None  # HRB 12345 (Amtsgericht …) / UK company number
    country: str | None = None  # ISO-2 hint from the identifiers found
    people: list[LegalPerson] = field(default_factory=list)
    evidence: dict[str, str] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not any(
            (self.siren, self.siret, self.rcs, self.vat_number, self.legal_name, self.register_number, self.people)
        )


# ---- identifier validation ------------------------------------------------------------------


def luhn_valid(digits: str) -> bool:
    if not digits.isdigit():
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def valid_siren(siren: str) -> bool:
    s = re.sub(r"\D", "", siren or "")
    return len(s) == 9 and s != "000000000" and luhn_valid(s)


def valid_siret(siret: str) -> bool:
    s = re.sub(r"\D", "", siret or "")
    if len(s) != 14:
        return False
    if s.startswith("356000000"):  # La Poste establishments: digit sum multiple of 5
        return sum(int(c) for c in s) % 5 == 0
    return luhn_valid(s)


def fr_vat_from_siren(siren: str) -> str:
    key = (12 + 3 * (int(siren) % 97)) % 97
    return f"FR{key:02d}{siren}"


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s)


# ---- regexes ------------------------------------------------------------------------------------

_SIREN_LABEL_RE = re.compile(rf"\bSIREN\b\s*(?:n°|no\.?|nº|num[ée]ro|number)?\s*[:#]?\s*((?:{_D}){{8}}\d)(?!\d)", re.IGNORECASE)
_SIRET_LABEL_RE = re.compile(rf"\bSIRET\b\s*(?:n°|no\.?|nº|num[ée]ro|number)?\s*[:#]?\s*((?:{_D}){{13}}\d)(?!\d)", re.IGNORECASE)
_RCS_RE = re.compile(
    rf"\bR\.?\s?C\.?\s?S\.?\s+(?:de\s+|d['’]\s*)?([A-ZÀ-Ý][A-Za-zÀ-ÿ'’\-]+(?:[\s\-][A-ZÀ-Ý][A-Za-zÀ-ÿ'’\-]+){{0,3}})"
    rf"\s*(?:sous\s+le\s+(?:n°|num[ée]ro)\s*)?(?:[:,]\s*)?(?:[AB]\s*)?((?:{_D}){{8}}\d)(?!\d)",
)
_RCS_REVERSE_RE = re.compile(
    rf"(?<!\d)((?:{_D}){{8}}\d)(?!\d)\s*(?:R\.?\s?C\.?\s?S\.?)\s+(?:de\s+)?([A-ZÀ-Ý][A-Za-zÀ-ÿ'’\-]+(?:[\s\-][A-ZÀ-Ý][A-Za-zÀ-ÿ'’\-]+){{0,2}})"
)
_IMMAT_RE = re.compile(
    rf"immatricul[ée]e?s?\s+(?:au|sous)[^.\n]{{0,60}}?(?:n°|num[ée]ro|number)\s*:?\s*((?:{_D}){{8}}\d)(?!\d)", re.IGNORECASE
)
_FR_VAT_RE = re.compile(r"\bFR\s?([0-9A-Z]{2})\s?(\d{3})\s?(\d{3})\s?(\d{3})\b")
_DE_VAT_RE = re.compile(r"\b(DE)\s?(\d{3})\s?(\d{3})\s?(\d{3})\b")
_GENERIC_VAT_RE = re.compile(
    r"\b(?:TVA|VAT|USt[.\-\s]*Id[.\-\s]*Nr\.?|Umsatzsteuer[- ]Identifikationsnummer|IVA|BTW|P\.?\s?IVA|CIF|NIF)"
    r"[^:\n]{0,40}[:\s]\s*([A-Z]{2}\s?[0-9A-Z][0-9A-Z\s.]{6,16}[0-9A-Z])",
    re.IGNORECASE,
)
_CAPITAL_RE = re.compile(
    r"\b(?:au\s+)?capital(?:\s+social)?(?:\s+(?:variable|minimum))?\s*(?:de|:)?\s*"
    r"((?:€|EUR|£|\$)?\s?\d[\d\s.  ,]*\d?\s*(?:€|euros?|EUR|£|GBP|CHF)?)",
    re.IGNORECASE,
)
_DE_CAPITAL_RE = re.compile(r"\b(?:Stammkapital|Grundkapital)\s*:?\s*(\d[\d.\s,]*\s*(?:€|EUR|Euro))", re.IGNORECASE)
_UK_CAPITAL_RE = re.compile(r"\bshare capital\s*(?:of|:)?\s*((?:£|€|\$)\s?\d[\d,.\s]*)", re.IGNORECASE)
_HR_RE = re.compile(r"\b(HR[AB])[ \t]*(?:Nr\.?[ \t]*)?(\d{1,7}(?:[ \t]?[A-Z]{1,2})?)\b")
_AMTSGERICHT_RE = re.compile(r"\b(?:Amtsgericht|Registergericht[ \t]*:?[ \t]*Amtsgericht)[ \t]+([A-ZÄÖÜ][\wäöüß\-]+(?:[ \t][A-ZÄÖÜ][\wäöüß\-]+)?)")
_UK_COMPANY_RE = re.compile(
    r"\b(?:company|registration|registered)\s*(?:number|no\.?|n°|nr)\s*:?\s*([A-Z]{0,2}\d{6,8})\b"
    r"|\bregistered\s+in\s+(?:England|Scotland|Wales|England\s+and\s+Wales|Northern\s+Ireland)[^.\n]{0,40}?(?:number|no\.?)\s*:?\s*([A-Z]{0,2}\d{6,8})\b",
    re.IGNORECASE,
)
_LEGAL_FORMS_LONG: tuple[tuple[str, str], ...] = (
    (r"soci[ée]t[ée] par actions simplifi[ée]e unipersonnelle", "SASU"),
    (r"soci[ée]t[ée] par actions simplifi[ée]e", "SAS"),
    (r"entreprise unipersonnelle [àa] responsabilit[ée] limit[ée]e", "EURL"),
    (r"soci[ée]t[ée] [àa] responsabilit[ée] limit[ée]e", "SARL"),
    (r"soci[ée]t[ée] anonyme", "SA"),
    (r"soci[ée]t[ée] en nom collectif", "SNC"),
    (r"soci[ée]t[ée] civile immobili[èe]re", "SCI"),
    (r"entreprise individuelle [àa] responsabilit[ée] limit[ée]e", "EIRL"),
    (r"entreprise individuelle", "EI"),
    (r"micro[- ]entreprise|auto[- ]entrepreneur|micro[- ]entrepreneur", "EI"),
    (r"gesellschaft mit beschr[äa]nkter haftung", "GmbH"),
    (r"unternehmergesellschaft", "UG"),
    (r"private limited company", "Ltd"),
    (r"limited liability partnership", "LLP"),
)
_FORM_ABBR = r"SASU|SAS|SARL|EURL|SA|SNC|SCI|SCOP|SELARL|SELAS|SCP|EIRL|GmbH|UG|AG|GbR|KG|OHG|e\.K\.|Ltd\.?|Limited|LLP|PLC|LLC|Inc\.?|S\.?L\.?U?|S\.?R\.?L\.?|S\.?p\.?A\.?|B\.?V\.?"
_FORM_NEAR_CAPITAL_RE = re.compile(rf"\b({_FORM_ABBR})\b(?=[^\n.]{{0,25}}\bcapital\b)")
_FORM_AFTER_NAME_RE = re.compile(rf"\b({_FORM_ABBR})\b")

_NAME_LABEL_RE = re.compile(
    r"(?:raison\s+sociale|d[ée]nomination(?:\s+sociale)?|nom\s+de\s+(?:la\s+)?soci[ée]t[ée]|soci[ée]t[ée]|"
    r"[ée]diteur(?:\s+du\s+site)?|propri[ée]taire(?:\s+du\s+site)?|company\s+name|firmenname|firma|"
    r"anbieter|diensteanbieter|site\s+[ée]dit[ée]\s+par)\s*:\s*(.+)",
    re.IGNORECASE,
)
_EDITED_BY_RE = re.compile(
    r"(?:[ée]dit[ée]|exploit[ée]|publi[ée]|propri[ée]t[ée])\s+(?:est\s+)?(?:par|de)\s+(?:la\s+soci[ée]t[ée]\s+|l['’]entreprise\s+|la\s+SAS\s+|la\s+SARL\s+)?"
    rf"([A-Z0-9][\w&'’\-. ]{{1,60}}?)(?=\s*(?:,|\(|\s(?:{_FORM_ABBR})\b|\s+(?:au\s+capital|immatricul|dont\s+le\s+si[èe]ge|situ[ée]e?)))",
)
_PUBLICATION_RE = re.compile(
    r"\b((?:directeur|directrice|responsable|direction)\s+(?:de\s+)?(?:la\s+)?publication(?:\s+du\s+site)?|"
    r"publication\s+director|director\s+of\s+(?:the\s+)?publication|responsible\s+for\s+(?:the\s+)?content|"
    r"verantwortlich(?:er)?\s+f[üu]r\s+den\s+inhalt(?:\s+nach\s+§\s*\d+[^:\n]{0,30})?|"
    r"inhaltlich\s+verantwortlich(?:er)?(?:\s+gem[äa][ßs][^:\n]{0,30})?)\s*(?:est\s+)?[:\-–]?\s*(.*)",
    re.IGNORECASE,
)
_REP_RE = re.compile(
    r"\b(repr[ée]sentant(?:e)?\s+l[ée]gal(?:e)?|repr[ée]sent[ée]e?\s+par|g[ée]rant(?:e)?|co[- ]?g[ée]rant(?:e)?|"
    r"pr[ée]sident(?:e)?|directeur\s+g[ée]n[ée]ral|directrice\s+g[ée]n[ée]rale|PDG|"
    r"gesch[äa]ftsf[üu]hrer(?:in)?|gesch[äa]ftsf[üu]hrung|vertretungsberechtigte[rn]?(?:\s+gesch[äa]ftsf[üu]hrer(?:in)?)?|"
    r"vertreten\s+durch|inhaber(?:in)?|managing\s+director|directors?|legal\s+representative)\s*(?:\([^)]*\))?\s*[:\-–]\s*(.*)",
    re.IGNORECASE,
)
_REPRESENTED_BY_RE = re.compile(
    r"repr[ée]sent[ée]e?\s+par\s+(?:son|sa)?\s*(g[ée]rant(?:e)?|pr[ée]sident(?:e)?|directeur\s+g[ée]n[ée]ral)?\s*,?\s*"
    r"([^,\n]{4,60}?)(?:\s*,|\s+en\s+(?:sa\s+)?qualit[ée]\s+de\s+([a-zà-ÿ\s]{3,40})|\s*$|\.)",
    re.IGNORECASE,
)
_ADDRESS_RE = re.compile(
    r"(?:si[èe]ge(?:\s+social)?|adresse(?:\s+du\s+si[èe]ge)?|adresse\s+postale|registered\s+office|head\s+office|"
    r"anschrift|sitz\s+der\s+gesellschaft|business\s+address)\s*(?:est\s+)?(?:situ[ée]\s+)?(?:au|à|:|-|–)?\s*:?\s*(.+)",
    re.IGNORECASE,
)
_SIEGE_INLINE_RE = re.compile(
    r"si[èe]ge\s+social\s+(?:est\s+)?(?:situ[ée]\s+)?(?:au|à|:)\s*(.{8,140}?)(?=,?\s+(?:immatricul|RCS|SIRE|N°|TVA|au capital)|\.\s|$)",
    re.IGNORECASE,
)
_POSTAL_RE = re.compile(r"\b(?:\d{5}|\d{4}|[A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2})\b")
_HONORIFIC_RE = re.compile(
    r"^(?:(?:M\.|Mr\.?|Mrs\.?|Ms\.?|Mme\.?|Mlle\.?|Monsieur|Madame|Mademoiselle|Herr|Frau|Dr\.?|Prof\.?|Me\.?|Ma[îi]tre)\s+)+",
    re.IGNORECASE,
)
_NAME_CUT_RE = re.compile(
    r"\s*(?:,|;|\(|\)|–|—|\s-\s|\||/|\ben\s+(?:sa\s+)?qualit[ée]\b|\bjoignable\b|\bcontact\b|\bt[ée]l\b|"
    r"\bt[ée]l[ée]phone\b|\bemail\b|\be-mail\b|\bmail\b|@|\bdirect(?:eur|rice)\b|\bg[ée]rant|\bpr[ée]sident|"
    r"\bfondat|\bfounder\b|\bceo\b|\bpdg\b|\bgesch[äa]ftsf|\bh[ée]bergeur\b|\bh[ée]bergement\b|\bsi[èe]ge\b|"
    r"\badresse\b|\bsoci[ée]t[ée]\b|\bimmatricul|\bRCS\b|\bSIRE[NT]\b|\bdepuis\b|\.\s|\.$)",
    re.IGNORECASE,
)


def _evidence(text: str, start: int, end: int, *, pad: int = 60) -> str:
    a = max(0, text.rfind("\n", 0, start) + 1)
    b = text.find("\n", end)
    b = len(text) if b < 0 else b
    if end - start < 200 and b - a > 300:
        a, b = max(a, start - pad), min(b, end + pad)
    return collapse_ws(text[a:b])[:300]


def _clean_person(raw: str) -> list[str]:
    """Extract 1–2 person names from the text following a role label."""
    raw = collapse_ws(raw or "")
    if not raw:
        return []
    out: list[str] = []
    raw = _HONORIFIC_RE.sub("", raw.strip(" .:-–"))
    raw = _NAME_CUT_RE.split(raw, maxsplit=1)[0]
    for part in re.split(r"\s+(?:et|and|und|&|y|e)\s+", raw):
        name = collapse_ws(_HONORIFIC_RE.sub("", part.strip(" .:-–")))
        if is_plausible_person_name(name):
            out.append(name)
    return out[:3]


def _next_nonempty(lines: list[str], idx: int) -> str:
    for j in range(idx + 1, min(idx + 3, len(lines))):
        if lines[j].strip():
            return lines[j]
    return ""


def _clean_legal_name(raw: str) -> str | None:
    name = collapse_ws(raw).strip(" .,:;-–")
    name = re.split(r"\s*(?:,|\(|\s-\s|–|—|\bau capital\b|\bdont\b|\bimmatricul|\bsi[èe]ge\b)", name, maxsplit=1)[0]
    name = name.strip(" .,:;")
    if not name or len(name) > 80 or not (name[0].isupper() or name[0].isdigit()):
        return None
    if re.match(r"(?i)^(le|la|les|ce|cette|this|the|der|die|das)\s+(site|soci[ée]t[ée]|website)\b", name):
        return None
    return name


def _legal_form(text: str) -> tuple[str | None, str | None]:
    for rx, form in _LEGAL_FORMS_LONG:
        m = re.search(rx, text, re.IGNORECASE)
        if m:
            return form, m.group(0)
    m = _FORM_NEAR_CAPITAL_RE.search(text)
    if m:
        return m.group(1).replace(".", "").replace("Limited", "Ltd"), m.group(0)
    return None, None


def parse_legal_notice(text: str) -> LegalInfo:
    """Parse a legal-notice page (line-structured ``content_text``) into :class:`LegalInfo`."""
    info = LegalInfo()
    if not text:
        return info
    m: re.Match[str] | None
    text = text.replace(" ", " ").replace(" ", " ")
    lines = text.split("\n")
    ev = info.evidence

    # --- SIREN / SIRET / RCS ------------------------------------------------------------------
    for m in _SIRET_LABEL_RE.finditer(text):
        siret = _digits(m.group(1))
        if valid_siret(siret) or valid_siren(siret[:9]):
            info.siret = siret
            ev["siret"] = _evidence(text, m.start(), m.end())
            break
    for rx, group in ((_SIREN_LABEL_RE, 1), (_RCS_RE, 2), (_RCS_REVERSE_RE, 1), (_IMMAT_RE, 1)):
        if info.siren:
            break
        for m in rx.finditer(text):
            siren = _digits(m.group(group))
            if valid_siren(siren):
                info.siren = siren
                ev["siren"] = _evidence(text, m.start(), m.end())
                break
    if not info.siren and info.siret and valid_siren(info.siret[:9]):
        info.siren = info.siret[:9]
        ev["siren"] = ev.get("siret", "")
    for rx, city_g, num_g in ((_RCS_RE, 1, 2), (_RCS_REVERSE_RE, 2, 1)):
        m = rx.search(text)
        if m and valid_siren(_digits(m.group(num_g))):
            city = collapse_ws(m.group(city_g))
            info.rcs = collapse_ws(m.group(0))
            ev["rcs"] = _evidence(text, m.start(), m.end())
            ev.setdefault("rcs_city", city)
            break

    # --- VAT ------------------------------------------------------------------------------------
    for m in _FR_VAT_RE.finditer(text):
        key, siren = m.group(1), m.group(2) + m.group(3) + m.group(4)
        vat = f"FR{key}{siren}"
        if valid_siren(siren) and (not key.isdigit() or fr_vat_from_siren(siren) == vat):
            info.vat_number = vat
            ev["vat_number"] = _evidence(text, m.start(), m.end())
            if not info.siren:
                info.siren = siren
                ev["siren"] = ev["vat_number"]
            break
    if not info.vat_number:
        m = _DE_VAT_RE.search(text)
        if m:
            info.vat_number = "DE" + m.group(2) + m.group(3) + m.group(4)
            ev["vat_number"] = _evidence(text, m.start(), m.end())
    if not info.vat_number:
        m = _GENERIC_VAT_RE.search(text)
        if m:
            vat = re.sub(r"[\s.]", "", m.group(1)).upper()
            if 8 <= len(vat) <= 16 and re.search(r"\d{6}", vat):
                info.vat_number = vat
                ev["vat_number"] = _evidence(text, m.start(), m.end())

    # --- capital, form, registry (DE/UK) ---------------------------------------------------------
    for rx in (_CAPITAL_RE, _DE_CAPITAL_RE, _UK_CAPITAL_RE):
        m = rx.search(text)
        if m and re.search(r"\d", m.group(1)) and re.search(r"€|eur|£|gbp|chf|\$", m.group(1), re.IGNORECASE):
            info.share_capital = collapse_ws(m.group(1)).strip(" ,.")
            ev["share_capital"] = _evidence(text, m.start(), m.end())
            break
    form, form_ev = _legal_form(text)
    if form:
        info.legal_form = form
        ev["legal_form"] = collapse_ws(form_ev or form)
    m = _HR_RE.search(text)
    if m:
        court = _AMTSGERICHT_RE.search(text)
        info.register_number = f"{m.group(1)} {m.group(2)}" + (f", Amtsgericht {court.group(1)}" if court else "")
        ev["register_number"] = _evidence(text, m.start(), m.end())
    else:
        m = _UK_COMPANY_RE.search(text)
        if m:
            info.register_number = (m.group(1) or m.group(2) or "").upper()
            ev["register_number"] = _evidence(text, m.start(), m.end())

    # --- legal name ------------------------------------------------------------------------------
    for i, line in enumerate(lines):
        m = _NAME_LABEL_RE.search(line)
        if m:
            raw = m.group(1).strip() or _next_nonempty(lines, i)
            name = _clean_legal_name(raw)
            if name and not is_plausible_person_name(name):
                info.legal_name = name
                ev["legal_name"] = collapse_ws(line)[:300]
                break
    if not info.legal_name:
        m = _EDITED_BY_RE.search(text)
        if m:
            name = _clean_legal_name(m.group(1))
            if name and not is_plausible_person_name(name):
                info.legal_name = name
                ev["legal_name"] = _evidence(text, m.start(), m.end())
    if not info.legal_name:
        for line in lines[:60]:
            m = re.match(rf"^\s*([A-Z0-9][\w&'’\-. ]{{1,60}}?)\s*,?\s+({_FORM_ABBR})(?=$|[\s,.;(])", line)
            if not m or re.match(r"(?i)^(le|la|les|ce|cette|this|the|der|die|das)\s", m.group(1)):
                continue
            name = _clean_legal_name(m.group(1))
            if name and not is_plausible_person_name(name):
                info.legal_name = name
                ev["legal_name"] = collapse_ws(line)[:300]
                if not info.legal_form:
                    info.legal_form = m.group(2).replace(".", "").replace("Limited", "Ltd")
                    ev["legal_form"] = collapse_ws(line)[:300]
                break

    # --- people ----------------------------------------------------------------------------------
    seen: set[tuple[str, str]] = set()

    def add_person(name: str, role: str, evidence: str) -> None:
        role = collapse_ws(role).strip(" :-–")
        role = role[:1].upper() + role[1:]
        key = (name.lower(), role.lower())
        if key in seen:
            return
        seen.add(key)
        info.people.append(LegalPerson(name=name, role=role, evidence=evidence[:300]))

    for i, line in enumerate(lines):
        m = _PUBLICATION_RE.search(line)
        if m:
            role_label = m.group(1)
            rest = m.group(2).strip()
            used = line
            if not rest:
                rest = _next_nonempty(lines, i)
                used = f"{line}\n{rest}"
            for name in _clean_person(rest):
                if not info.publication_director:
                    info.publication_director = name
                    ev["publication_director"] = collapse_ws(used)[:300]
                add_person(name, role_label, used)
        m = _REP_RE.search(line)
        if m:
            role_label = m.group(1)
            rest = m.group(2).strip()
            used = line
            if not rest:
                rest = _next_nonempty(lines, i)
                used = f"{line}\n{rest}"
            names = _clean_person(rest)
            for name in names:
                if not info.legal_representative:
                    info.legal_representative = name
                    info.legal_representative_role = collapse_ws(role_label)
                    ev["legal_representative"] = collapse_ws(used)[:300]
                add_person(name, role_label if not re.match(r"(?i)repr[ée]sent[ée]e?\s+par", role_label) else "Représentant légal", used)
        m = _REPRESENTED_BY_RE.search(line)
        if m:
            role_label = m.group(3) or m.group(1) or "Représentant légal"
            for name in _clean_person(m.group(2)):
                if not info.legal_representative:
                    info.legal_representative = name
                    info.legal_representative_role = collapse_ws(role_label)
                    ev["legal_representative"] = collapse_ws(line)[:300]
                add_person(name, role_label, line)

    # --- address ---------------------------------------------------------------------------------
    m = _SIEGE_INLINE_RE.search(text)
    if m and _POSTAL_RE.search(m.group(1)):
        info.address = collapse_ws(m.group(1)).strip(" ,.")
        ev["address"] = _evidence(text, m.start(), m.end())
    else:
        for i, line in enumerate(lines):
            m = _ADDRESS_RE.search(line)
            if not m:
                continue
            rest = m.group(1).strip()
            if not rest or (not _POSTAL_RE.search(rest) and i + 1 < len(lines) and _POSTAL_RE.search(lines[i + 1])):
                rest = (rest + " " + lines[i + 1]).strip()
            rest = re.split(r"\s*(?:\bimmatricul|\bRCS\b|\bSIRE[NT]\b|\bTVA\b|\bt[ée]l\b|\bphone\b|\bemail\b|\bcapital\b)", rest, maxsplit=1, flags=re.IGNORECASE)[0]
            rest = collapse_ws(rest).strip(" ,.:;-")
            if 8 <= len(rest) <= 200 and re.search(r"\d", rest):
                info.address = rest
                ev["address"] = collapse_ws(line)[:300]
                break

    # --- country hint ----------------------------------------------------------------------------
    if info.siren or info.rcs or (info.vat_number or "").startswith("FR"):
        info.country = "FR"
    elif (info.register_number or "").startswith("HR") or (info.vat_number or "").startswith("DE"):
        info.country = "DE"
    elif info.register_number and re.search(r"(?i)england|wales|scotland|companies house|registered office", text):
        info.country = "GB"
    return info
