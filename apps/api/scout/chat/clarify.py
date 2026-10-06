"""Clarifying questions before a search starts (≤ 3, one round).

Deterministic slot filling on top of the ICP parser: the request is parsed, the slots whose answer would change
the search (what, where, who, how many, size, email strictness, exclusions) are checked, and at most three
targeted questions are produced — ordered by impact. Answers come back as `{id, value}` pairs and are applied
as structured overrides on the parsed definition, so the outcome never depends on re-parsing concatenated text.

Works without any AI provider; the LLM operator may also pass its own questions (unknown ids are folded back
into the request text and re-parsed).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from scout.chat.i18n import Lang, detect_lang
from scout.db.enums import CampaignMode, EmailStatus, ExclusionMode, RoleFamily
from scout.schemas.campaign import CampaignDefinition, EmployeeRange

MAX_QUESTIONS = 3

GO_RE = re.compile(
    r"(^|\b)(go|go go|vas[- ]?y|lance[sz]?(-la|-le)?|launch( it)?|start( it| now)?|d[ée]marre|fonce|just do it|"
    r"directement|sans (me poser de )?questions?|no questions( asked)?|pas de questions?|c.?est parti|ok go)(\b|$)",
    re.I,
)
_NEW_SEARCH_RE = re.compile(
    r"^(find|get|search|discover|trouve|cherche|donne|liste|je (veux|voudrais|cherche)|i (want|need))\w*\b",
    re.I,
)


def wants_go(text: str) -> bool:
    return bool(GO_RE.search(text or ""))


def is_new_search(text: str) -> bool:
    return bool(_NEW_SEARCH_RE.search((text or "").strip()))


# ---- question model ---------------------------------------------------------------------------------


class ClarifyOption(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str = Field(min_length=1, max_length=120)
    label: str = Field(min_length=1, max_length=80)


class ClarifyQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(
        min_length=1,
        max_length=40,
        description="Slot id: industry, geography, roles, volume, size, email, exclusions (or a short custom id)",
    )
    text: str = Field(min_length=3, max_length=200)
    options: list[ClarifyOption] = Field(default_factory=list, max_length=4)
    allow_free_text: bool = True
    default: str | None = Field(default=None, description="Value used by 'Use defaults'")


class ClarifyAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=40)
    value: str = Field(max_length=300)
    label: str | None = Field(default=None, max_length=300)
    question: str | None = Field(default=None, max_length=200)


# ---- slot detection -------------------------------------------------------------------------------------

_COUNT_RE = re.compile(
    r"\b\d[\d  ,.]*\s*[kK]?\s*(?:more |new |nouveaux |nouvelles |autres )?(?:[a-zA-Zéèàç]+\s){0,4}?"
    r"(?:leads?|contacts?|agenc|compan|entreprises?|soci|people|personnes|prospects?|startups?|founders?|fondat|"
    r"dentist|restaurants?|businesses|firms?|cabinets?|brands?|marques?|ceos?|owners?|stores?|boutiques?|shops?)",
    re.I,
)
_ROLE_RE = re.compile(
    r"founders?|fondat\w*|\bceos?\b|chief|pdg|directeur|directrice|\bdg\b|owners?|propri[ée]taire|g[ée]rant|"
    r"dirigeant|managing director|pr[ée]sident|head of|\bcmo\b|\bcto\b|\bcso\b|marketing (lead|manager|director)|"
    r"responsable|decision[- ]makers?|d[ée]cideurs?|companies only|only companies|sans contact|no contacts?|"
    r"seulement les entreprises|juste les entreprises",
    re.I,
)
_EMAIL_RE = re.compile(
    r"\bsafe\b|v[ée]rifi|verified|risky|catch[- ]?all|no email|sans email|email optional|pas besoin d.?email|"
    r"smtp|any email|n.?importe quel email",
    re.I,
)
_SIZE_RE = re.compile(
    r"\d+\s*(?:-|–|to|à|a)\s*\d+\s*(?:employ|salari|people|personnes|staff|collab)|"
    r"(?:under|less than|moins de|plus de|more than|over|at least|au moins)\s*\d+\s*(?:employ|salari|people|personnes)|"
    r"\b(tpe|pme|eti|startups?|solo|freelances?|ind[ée]pendants?|small|petites?|large|grandes?|any size|toute taille)\b",
    re.I,
)
_EXCL_RE = re.compile(
    r"\bnew\b|nouveaux|nouvelles|never seen|jamais vu|d[ée]j[àa]|already|exclu|exclude|don.?t repeat|"
    r"sans doublon|fresh|not (already )?scraped|pas d[ée]j[àa]|again|à nouveau",
    re.I,
)


@dataclass
class SlotReport:
    lang: Lang
    missing: list[str]
    parsed: Any  # scout.pipeline.icp.AIParsedCampaign


SLOT_ORDER = ["industry", "geography", "roles", "volume", "size", "email", "exclusions"]
# Questions are only asked when one of these is open; the others are asked alongside, never alone.
CORE_SLOTS = ("industry", "geography", "roles", "volume")


def analyse(request: str) -> SlotReport:
    from scout.pipeline.icp import heuristic_parse

    parsed = heuristic_parse(request)
    lang = detect_lang(request)
    text = request or ""
    missing: list[str] = []
    has_what = bool(parsed.industries or parsed.website_conditions or parsed.keywords)
    if not has_what and not re.search(
        r"agenc|startup|saas|shop|store|boutique|restaurant|dentist|cabinet|firm|brand|marque|h[oô]tel|"
        r"avocat|lawyer|comptable|account|consult|immobili|real estate|e-?commerce|studio|clinic|clinique|"
        r"architect|plombier|plumber|[ée]cole|school|coach|recrut|recruit|logiciel|software|fintech|"
        r"industrie|manufactur|transport|logisti|construction|btp|retail|magasin",
        text,
        re.I,
    ):
        missing.append("industry")
    if not (parsed.countries or parsed.cities or parsed.regions):
        missing.append("geography")
    if parsed.mode == CampaignMode.people and not _ROLE_RE.search(text):
        missing.append("roles")
    if not _COUNT_RE.search(text):
        missing.append("volume")
    if parsed.employee_min is None and parsed.employee_max is None and not _SIZE_RE.search(text):
        missing.append("size")
    if parsed.mode == CampaignMode.people and not _EMAIL_RE.search(text):
        missing.append("email")
    if parsed.exclusion.mode == ExclusionMode.NONE and not _EXCL_RE.search(text):
        missing.append("exclusions")
    return SlotReport(lang=lang, missing=missing, parsed=parsed)


def needs_clarification(request: str) -> bool:
    """Ask only when something that changes the search is genuinely open."""
    rep = analyse(request)
    return any(s in CORE_SLOTS for s in rep.missing)


# ---- question catalogue -----------------------------------------------------------------------------------


def _q(
    id_: str, text: str, options: list[tuple[str, str]], default: str, free: bool = True
) -> ClarifyQuestion:
    return ClarifyQuestion(
        id=id_,
        text=text,
        options=[ClarifyOption(value=v, label=lbl) for v, lbl in options[:4]],
        allow_free_text=free,
        default=default,
    )


def question_for(slot: str, lang: Lang, parsed: Any) -> ClarifyQuestion:
    fr = lang == "fr"
    if slot == "industry":
        return _q(
            "industry",
            "Quel type d'entreprises ?" if fr else "What kind of companies?",
            [
                ("marketing agency", "Agences marketing" if fr else "Marketing agencies"),
                ("software company", "Startups SaaS / logiciel" if fr else "SaaS / software"),
                ("e-commerce brand", "E-commerce"),
                ("consulting firm", "Cabinets de conseil" if fr else "Consulting firms"),
            ],
            "marketing agency",
        )
    if slot == "geography":
        if fr or "FR" in (parsed.countries or []):
            opts = [
                ("FR", "Toute la France" if fr else "All of France"),
                ("Paris", "Paris"),
                ("Lyon", "Lyon"),
                ("Marseille", "Marseille"),
            ]
            default = "FR"
        else:
            opts = [("FR", "France"), ("GB", "United Kingdom"), ("DE", "Germany"), ("US", "United States")]
            default = "FR"
        return _q("geography", "Où dois-je chercher ?" if fr else "Where should I look?", opts, default)
    if slot == "roles":
        return _q(
            "roles",
            "Qui veux-tu contacter ?" if fr else "Who should I reach?",
            [
                ("founders", "Fondateurs / dirigeants" if fr else "Founders / CEOs"),
                ("marketing", "Responsables marketing" if fr else "Marketing leaders"),
                ("sales", "Responsables commerciaux" if fr else "Sales leaders"),
                ("companies", "Entreprises seulement" if fr else "Companies only"),
            ],
            "founders",
        )
    if slot == "volume":
        return _q(
            "volume",
            "Combien de leads qualifiés ?" if fr else "How many qualified leads?",
            [("25", "25"), ("50", "50"), ("100", "100"), ("300", "300")],
            "50",
        )
    if slot == "size":
        return _q(
            "size",
            "Quelle taille d'entreprise ?" if fr else "Company size?",
            [
                ("1-10", "1–10 salariés" if fr else "1–10 employees"),
                ("2-30", "2–30 salariés" if fr else "2–30 employees"),
                ("10-200", "10–200 salariés" if fr else "10–200 employees"),
                ("any", "Peu importe" if fr else "Any size"),
            ],
            "any",
            free=False,
        )
    if slot == "email":
        return _q(
            "email",
            "Quel niveau d'exigence sur l'email ?" if fr else "How strict on the email?",
            [
                ("safe_likely", "Vérifié ou très probable" if fr else "Verified or very likely"),
                ("safe_only", "Vérifié SMTP uniquement" if fr else "SMTP-verified only"),
                ("risky_ok", "Accepter les risqués" if fr else "Accept risky too"),
                ("optional", "Pas besoin d'email" if fr else "No email needed"),
            ],
            "safe_likely",
            free=False,
        )
    return _q(
        "exclusions",
        "Exclure les leads que tu as déjà ?" if fr else "Skip leads you already have?",
        [
            ("new_only", "Oui, uniquement des nouveaux" if fr else "Yes, only new leads"),
            ("allow", "Non, tout m'intéresse" if fr else "No, include them"),
        ],
        "new_only",
        free=False,
    )


def build_questions(request: str) -> tuple[list[ClarifyQuestion], Lang, list[str]]:
    """→ (≤ 3 questions ordered by impact, language, every missing slot)."""
    rep = analyse(request)
    if not any(s in CORE_SLOTS for s in rep.missing):
        return [], rep.lang, rep.missing
    slots = [s for s in SLOT_ORDER if s in rep.missing][:MAX_QUESTIONS]
    return [question_for(s, rep.lang, rep.parsed) for s in slots], rep.lang, rep.missing


def intro(lang: Lang, n: int) -> str:
    if lang == "fr":
        return (
            "Une précision avant de lancer :"
            if n == 1
            else f"{n} précisions rapides avant de lancer la recherche :"
        )
    return (
        "One quick question before I start:" if n == 1 else f"{n} quick questions before I start the search:"
    )


# ---- answers → definition overrides ---------------------------------------------------------------------

_ROLE_PRESETS: dict[str, tuple[list[str], list[RoleFamily]]] = {
    "founders": (
        ["Founder", "Co-Founder", "CEO", "Owner", "Managing Director"],
        [RoleFamily.founder, RoleFamily.executive],
    ),
    "marketing": (["Head of Marketing", "Marketing Director", "CMO"], [RoleFamily.marketing]),
    "sales": (["Head of Sales", "Sales Director"], [RoleFamily.sales]),
}

_EMAIL_PRESETS: dict[str, list[EmailStatus]] = {
    "safe_likely": [EmailStatus.SAFE, EmailStatus.LIKELY_SAFE],
    "safe_only": [EmailStatus.SAFE],
    "risky_ok": [EmailStatus.SAFE, EmailStatus.LIKELY_SAFE, EmailStatus.RISKY],
}


def _int(v: str) -> int | None:
    m = re.search(r"\d[\d  ,.]*", v or "")
    if not m:
        return None
    digits = re.sub(r"[^\d]", "", m.group(0))
    try:
        n = int(digits)
    except ValueError:
        return None
    if re.search(r"\d\s*k\b", v, re.I):
        n *= 1000
    return n


def apply_answers(
    defn: CampaignDefinition, answers: list[ClarifyAnswer]
) -> tuple[CampaignDefinition, list[str]]:
    """Structured overrides for known slots; returns (definition, free-text fragments for unknown slots)."""
    from scout.pipeline.icp import _industries_from_text, heuristic_parse

    d = defn.model_copy(deep=True)
    leftovers: list[str] = []
    cf = d.company_filters
    for a in answers:
        v = (a.value or "").strip()
        if not v:
            continue
        sid = a.id.lower()
        if sid == "volume":
            n = _int(v)
            if n:
                d.target_qualified_count = max(1, min(100_000, n))
        elif sid == "geography":
            code = v.upper()
            if len(code) == 2 and code.isalpha():
                cf.countries = [code]
                cf.cities = []
            else:
                p = heuristic_parse(v)
                if p.cities or p.countries:
                    cf.cities = p.cities
                    cf.countries = p.countries or (["FR"] if p.cities else cf.countries)
                else:
                    cf.cities = [v.strip().title()[:60]]
        elif sid == "roles":
            key = v.lower()
            if key == "companies":
                d.mode = CampaignMode.companies
                d.required_fields = [
                    f for f in d.required_fields if f not in ("person", "professional_email")
                ]
                d.people_filters.titles = []
                d.people_filters.role_families = []
            elif key in _ROLE_PRESETS:
                titles, fams = _ROLE_PRESETS[key]
                d.people_filters.titles = list(titles)
                d.people_filters.role_families = list(fams)
            else:
                p = heuristic_parse(f"find {v}")
                d.people_filters.titles = p.titles or [v[:60]]
                d.people_filters.role_families = p.role_families
        elif sid == "size":
            key = v.lower()
            if key in ("any", "peu importe", "any size"):
                cf.employee_range = None
            else:
                nums = [int(x) for x in re.findall(r"\d+", v)]
                if len(nums) >= 2:
                    cf.employee_range = EmployeeRange(min=nums[0], max=nums[1])
                elif len(nums) == 1:
                    cf.employee_range = EmployeeRange(min=None, max=nums[0])
        elif sid == "email":
            key = v.lower()
            if key == "optional":
                d.required_fields = [f for f in d.required_fields if f != "professional_email"]
            elif key in _EMAIL_PRESETS:
                d.accepted_email_statuses = list(_EMAIL_PRESETS[key])
                if d.mode == CampaignMode.people and "professional_email" not in d.required_fields:
                    d.required_fields = [*d.required_fields, "professional_email"]
        elif sid == "exclusions":
            if v.lower() == "new_only":
                d.exclusion.mode = ExclusionMode.EXCLUDE_PREVIOUS_PEOPLE
                d.exclusion.previous_people = True
            elif v.lower() == "allow":
                d.exclusion.mode = ExclusionMode.NONE
                d.exclusion.previous_people = False
                d.exclusion.previous_companies = False
        elif sid == "industry":
            found = _industries_from_text(v)
            cf.industries = found or [v.strip().lower()[:80]]
        else:
            leftovers.append(f"{a.question or a.id}: {a.label or v}".strip())
    if not d.name or d.name == defn.name:
        bits = [*(i.title() for i in cf.industries[:1]), *(cf.cities[:1] or cf.countries[:1])]
        if bits:
            d.name = " · ".join(bits)
    return CampaignDefinition.model_validate(d.model_dump()), leftovers


def answers_text(answers: list[ClarifyAnswer]) -> str:
    """Readable one-liner of the answers, used as the user's chat message and appended to the prompt."""
    return " · ".join((a.label or a.value).strip() for a in answers if (a.label or a.value or "").strip())
