"""ICP parser (spec §1, §27–28, §104–105): natural language → typed CampaignDefinition.

Gemini (structured output) does the language understanding when available; a deterministic EN/FR parser
is the zero-cost fallback and also enforces safety defaults (exclusion triggers, list resolution).
Exact vs semantic website conditions are distinguished explicitly.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field
from unidecode import unidecode

from scout.ai.factory import get_ai
from scout.ai.models import ModelRole
from scout.db.enums import CampaignMode, EmailStatus, ExclusionMode, RoleFamily
from scout.errors import AIUnavailable, RetryableError
from scout.schemas.campaign import (
    CampaignDefinition,
    CompanyFilters,
    EmployeeRange,
    EnrichmentRequest,
    ExclusionSpec,
    KeywordCondition,
    PeopleFilters,
    Seed,
    SemanticCondition,
    TechnologyCondition,
)
from scout.services.exclusion import default_exclusion_for_prompt
from scout.util.text import normalize_key

log = structlog.get_logger("icp")


@dataclass
class ParseContext:
    """What the parser may reference: lists, imports, the current list and selection."""

    lists: dict[str, uuid.UUID] = field(default_factory=dict)  # lowercase name → id
    latest_import_id: uuid.UUID | None = None
    current_list_id: uuid.UUID | None = None
    current_list_name: str | None = None
    selected_person_ids: list[uuid.UUID] = field(default_factory=list)
    selected_company_ids: list[uuid.UUID] = field(default_factory=list)
    like_profile: dict[str, Any] | None = None  # "find more like these" profile


# ---- AI-facing schema (simple types; mapped deterministically to CampaignDefinition) -------------


class AIWebsiteCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["keyword_any", "keyword_all", "semantic", "technology"] = Field(
        description="keyword_* = the site merely MENTIONS words (deterministic). semantic = the company ACTUALLY "
        "offers/does/specializes in something (needs evidence). technology = uses a technology like Shopify."
    )
    terms: list[str] = Field(default_factory=list, description="Words for keyword conditions / technologies")
    concept: str | None = Field(
        default=None,
        description="For semantic: what must be true, e.g. 'offers Instagram marketing services'",
    )


class AIExclusion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: ExclusionMode = ExclusionMode.NONE
    exclude_list_names: list[str] = Field(default_factory=list)
    exclude_latest_import: bool = False
    cooldown_days: int | None = None
    exclude_existing_companies: bool = False
    allow_new_people_at_existing_companies: bool = True


class AIParsedCampaign(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(description="Short campaign name, e.g. 'FR marketing agencies · Instagram'")
    mode: CampaignMode = CampaignMode.people
    target_count: int = Field(ge=1, le=100000)
    industries: list[str] = Field(
        default_factory=list, description="English industry names, singular, e.g. 'marketing agency'"
    )
    keywords: list[str] = Field(default_factory=list)
    naf_codes: list[str] = Field(
        default_factory=list,
        description="For French targets: up to 4 NAF rév.2 codes (format 12.34A) of the requested activity, "
        "only codes you are sure of (e.g. electronics design office → 71.12B, 26.12Z)",
    )
    countries: list[str] = Field(default_factory=list, description="ISO alpha-2")
    regions: list[str] = Field(default_factory=list)
    cities: list[str] = Field(default_factory=list)
    employee_min: int | None = None
    employee_max: int | None = None
    website_conditions: list[AIWebsiteCondition] = Field(default_factory=list)
    titles: list[str] = Field(default_factory=list)
    role_families: list[RoleFamily] = Field(default_factory=list)
    max_people_per_company: int = 1
    require_email: bool = True
    accepted_email_statuses: list[EmailStatus] = Field(
        default_factory=lambda: [EmailStatus.SAFE, EmailStatus.LIKELY_SAFE]
    )
    minimum_icp_score: int | None = None
    exclusion: AIExclusion = Field(default_factory=AIExclusion)
    enrichments: list[EnrichmentRequest] = Field(default_factory=list)
    seed_from_current_list: bool = Field(
        default=False, description="True when the request targets companies already in the current list"
    )
    seed_from_selection: bool = False


ICP_SYSTEM = """You convert a user's request for B2B leads into a structured campaign definition for a lead
intelligence app. Rules:
- target_count is the number of QUALIFIED leads requested (default 100 if unspecified).
- mode=companies only if the user wants companies without contacts.
- Distinguish website conditions: "mentions / contains / talks about X" → keyword_any (OR) or keyword_all (AND);
  "actually offers / sells / provides / specializes in X" → semantic with a precise concept; "uses Shopify" → technology.
- Titles: list what the user asked (e.g. Founder, Co-Founder, CEO, Owner). Decision makers without detail → Founder, CEO, Owner, Managing Director.
- Exclusion: "new / fresh / not already scraped / never seen / don't repeat" → EXCLUDE_PREVIOUS_PEOPLE.
  "companies I already have / only new companies" → EXCLUDE_PREVIOUS_PEOPLE_AND_COMPANIES (or EXCLUDE_PREVIOUS_COMPANIES for company searches).
  "exported before" → EXCLUDE_EXPORTED. "contacted" → EXCLUDE_CONTACTED. "anyone from my <name> list" → EXCLUDE_SPECIFIC_LISTS
  with exclude_list_names. "from my uploaded file / import" → exclude_latest_import=true. "it's okay if the company is
  already in my database" → allow_new_people_at_existing_companies=true. "a different person at my existing companies"
  → seed_from_current_list=true, EXCLUDE_PREVIOUS_PEOPLE.
- Professional/verified email required → require_email=true, accepted_email_statuses=[SAFE, LIKELY_SAFE]; [SAFE] only when the user says strictly SAFE/SMTP-verified; add RISKY when the user accepts risky.
- Location is mandatory whenever the user names one: cities with proper capitalization ("annecy" → "Annecy"),
  regions, and countries as ISO alpha-2 (a French city → "FR").
- Local professions and trades (coachs sportifs, kinés, ostéopathes, plombiers, avocats, photographes…) are the
  businesses themselves: put the trade in industries (English, e.g. "personal trainer", "physiotherapist",
  "plumber") and use owner titles (Owner, Founder, Manager) plus the trade title if the user named one.
- naf_codes: for a French target whose activity is specific or unusual (bureau d'études électronique, tatoueur,
  sophrologue…), give up to 4 official NAF rév.2 codes you are sure of; leave empty when unsure.
- Never invent constraints the user did not express.
"""


# ---- deterministic parser ---------------------------------------------------------------------------

_NUM = r"(\d{1,3}(?:[ ,.  ]\d{3})+|\d+(?:\.\d+)?\s*[kK]?)"

COUNTRY_WORDS: dict[str, str] = {
    "france": "FR",
    "french": "FR",
    "francais": "FR",
    "francaises": "FR",
    "francaise": "FR",
    "français": "FR",
    "française": "FR",
    "françaises": "FR",
    "belgium": "BE",
    "belgian": "BE",
    "belgique": "BE",
    "belges": "BE",
    "switzerland": "CH",
    "swiss": "CH",
    "suisse": "CH",
    "suisses": "CH",
    "germany": "DE",
    "german": "DE",
    "allemagne": "DE",
    "spain": "ES",
    "spanish": "ES",
    "espagne": "ES",
    "italy": "IT",
    "italian": "IT",
    "italie": "IT",
    "uk": "GB",
    "united kingdom": "GB",
    "british": "GB",
    "england": "GB",
    "royaume uni": "GB",
    "us": "US",
    "usa": "US",
    "united states": "US",
    "american": "US",
    "etats unis": "US",
    "canada": "CA",
    "canadian": "CA",
    "quebec": "CA",
    "netherlands": "NL",
    "dutch": "NL",
    "pays bas": "NL",
    "portugal": "PT",
    "ireland": "IE",
    "luxembourg": "LU",
    "morocco": "MA",
    "maroc": "MA",
    "australia": "AU",
}

FR_CITIES = [
    "paris",
    "lyon",
    "marseille",
    "toulouse",
    "nice",
    "nantes",
    "montpellier",
    "strasbourg",
    "bordeaux",
    "lille",
    "rennes",
    "reims",
    "toulon",
    "grenoble",
    "dijon",
    "angers",
    "nimes",
    "clermont ferrand",
    "aix en provence",
    "brest",
    "tours",
    "amiens",
    "limoges",
    "annecy",
    "perpignan",
    "metz",
    "besancon",
    "orleans",
    "rouen",
    "caen",
    "nancy",
    "avignon",
    "la rochelle",
    "pau",
    "biarritz",
    "bayonne",
    "cannes",
    "antibes",
    "le mans",
    "saint etienne",
]

TITLE_WORDS: list[tuple[str, list[str], list[RoleFamily]]] = [
    (r"co[- ]?founders?|co[- ]?fondat\w*", ["Co-Founder"], [RoleFamily.founder]),
    (r"founders?|fondat\w*", ["Founder"], [RoleFamily.founder]),
    (r"\bceos?\b|chief executive|pdg|directeur g[ée]n[ée]ral|\bdg\b", ["CEO"], [RoleFamily.executive]),
    (
        r"owners?|propri[ée]taires?|g[ée]rant\w*|dirigeant\w*",
        ["Owner"],
        [RoleFamily.founder, RoleFamily.executive],
    ),
    (r"managing directors?", ["Managing Director"], [RoleFamily.executive]),
    (r"pr[ée]sident\w*", ["President"], [RoleFamily.executive]),
    (
        r"head of marketing|marketing directors?|directeur marketing|directrice marketing|\bcmo\b|responsable marketing|marketing decision makers?",
        ["Head of Marketing", "Marketing Director", "CMO"],
        [RoleFamily.marketing],
    ),
    (r"head of growth", ["Head of Growth"], [RoleFamily.marketing]),
    (
        r"head of sales|sales directors?|directeur commercial|\bcso\b",
        ["Head of Sales", "Sales Director"],
        [RoleFamily.sales],
    ),
    (r"\bcto\b|head of engineering|directeur technique", ["CTO"], [RoleFamily.technology]),
    (
        r"decision[- ]makers?|d[ée]cideurs?",
        ["Founder", "CEO", "Owner", "Managing Director"],
        [RoleFamily.founder, RoleFamily.executive],
    ),
]

_STOP_TERMS = {
    "the",
    "and",
    "for",
    "with",
    "services",
    "service",
    "offer",
    "offers",
    "gestion",
    "management",
    "de",
    "la",
    "le",
    "les",
    "des",
    "du",
    "en",
    "et",
    "pour",
    "their",
    "they",
    "real",
    "really",
}

TECH_NAMES = [
    "shopify",
    "wordpress",
    "woocommerce",
    "webflow",
    "wix",
    "squarespace",
    "hubspot",
    "klaviyo",
    "mailchimp",
    "salesforce",
    "prestashop",
    "magento",
    "framer",
    "bubble",
    "intercom",
    "calendly",
    "stripe",
    "next.js",
    "react",
]


def _to_int(s: str) -> int:
    s = s.strip().lower()
    mult = 1000 if s.endswith("k") else 1
    s = s.rstrip("k").strip()
    s = re.sub(r"[ ,  ]", "", s)
    if s.count(".") == 1 and len(s.split(".")[1]) == 3:
        s = s.replace(".", "")
    return int(float(s) * mult)


def _industries_from_text(text: str) -> list[str]:
    try:
        from scout.discovery.taxonomy import match_industries

        found = match_industries(text)
        if found:
            return [p.key.replace("_", " ") for p in found[:2]]
    except Exception as exc:  # taxonomy optional at parse time
        log.debug("icp.taxonomy_unavailable", error=str(exc))
    t = normalize_key(text)
    table = [
        (
            r"(marketing|communication|digital|web|social media|seo|advertising|ad|pub\w*|growth) (agenc\w+|agence\w*)|agences? (de )?(marketing|communication|web|digitale?s?|social media|seo|pub\w*)",
            None,
        ),
        (r"saas|software compan\w+|startups?", "software company"),
        (r"dentist\w*|dentistes?", "dentist"),
        (r"restaurants?", "restaurant"),
        (r"hotels?|hotels?", "hotel"),
        (r"real estate|agences? immobili\w+", "real estate agency"),
        (r"law firms?|cabinets? d avocats?|avocats?", "law firm"),
        (r"accounting firms?|experts? comptables?|cabinets? comptables?", "accounting firm"),
        (r"e ?commerce|ecommerce brands?|online stores?|boutiques? en ligne", "e-commerce brand"),
        (r"recruit\w+ agenc\w+|cabinets? de recrutement", "recruitment agency"),
        (r"consult\w+", "consulting firm"),
    ]
    out: list[str] = []
    for pat, label in table:
        m = re.search(pat, t)
        if not m:
            continue
        if label is None:
            words = m.group(0)
            kind = next(
                (
                    k
                    for k in (
                        "social media",
                        "seo",
                        "web",
                        "digital",
                        "communication",
                        "advertising",
                        "marketing",
                        "growth",
                    )
                    if k in words
                ),
                "marketing",
            )
            if "pub" in words:
                kind = "advertising"
            label = f"{kind} agency"
        out.append(label)
        break
    return out


def heuristic_parse(prompt: str, ctx: ParseContext | None = None) -> AIParsedCampaign:
    ctx = ctx or ParseContext()
    raw = prompt.strip()
    t = normalize_key(raw)
    low = raw.lower()
    # target count
    count = 100
    m = re.search(
        rf"\b(?:find|get|give|trouve\w*|donne\w*|cherche\w*|want|veux|besoin de|another|more|encore)?\D{{0,20}}?{_NUM}\s*(?:more |new |nouveaux |nouvelles |autres )?(?:[a-zA-Zéèàç]+\s){{0,4}}?(?:leads?|contacts?|agenc|compan|companies|entreprises?|soci|people|personnes|prospects?|startups?|founders?|fondat|dentist|restaurants?|businesses|firms?|cabinets?|brands?|marques?|ceos?|owners?)",
        raw,
        re.I,
    )
    if m:
        try:
            count = _to_int(m.group(1))
        except ValueError:
            count = 100
    else:
        m2 = re.search(rf"\b(?:another|more|encore|autres?|find|trouve\w*|get)\s+{_NUM}\b", raw, re.I)
        if m2:
            try:
                count = _to_int(m2.group(1))
            except ValueError:
                count = 100
    count = max(1, min(count, 100_000))
    mode = CampaignMode.people
    if re.search(
        r"\b(companies only|only companies|company list|no contacts?|without (contacts?|people)|juste les entreprises|seulement les entreprises|uniquement les entreprises|sans contact)\b",
        low,
    ):
        mode = CampaignMode.companies
    industries = _industries_from_text(raw)
    countries = []
    for word, code in COUNTRY_WORDS.items():
        if re.search(rf"\b{re.escape(word)}\b", t) and code not in countries:
            countries.append(code)
    cities = [c.title() for c in FR_CITIES if re.search(rf"\b{re.escape(c)}\b", t)]
    if cities and not countries:
        countries = ["FR"]
    # employee range
    emin = emax = None
    folded = unidecode(raw).lower()
    rm = re.search(
        r"(\d+)\s*(?:-|to|a|and|et)\s*(\d+)\s*(?:employees|employ\w*|salari\w+|people|personnes|staff|collaborat\w+|emp)",
        folded,
    )
    if rm:
        emin, emax = int(rm.group(1)), int(rm.group(2))
    else:
        rm = re.search(
            r"(?:under|less than|fewer than|moins de|max(?:imum)?)\s*(\d+)\s*(?:employees|salari\w+|people|personnes|staff)",
            t,
        )
        if rm:
            emax = int(rm.group(1))
        rm2 = re.search(
            r"(?:over|more than|plus de|at least|au moins|min(?:imum)?)\s*(\d+)\s*(?:employees|salari\w+|people|personnes|staff)",
            t,
        )
        if rm2:
            emin = int(rm2.group(1))
        rm3 = re.search(r"(\d+)\s*\+\s*(?:employees|salari\w+|people)", t)
        if rm3:
            emin = int(rm3.group(1))
    # titles
    titles: list[str] = []
    families: list[RoleFamily] = []
    for pat, ts, fams in TITLE_WORDS:
        if pat.startswith("decision") and titles:
            continue
        if re.search(pat, low):
            titles += [x for x in ts if x not in titles]
            families += [f for f in fams if f not in families]
    if mode == CampaignMode.people and not titles:
        titles = ["Founder", "CEO", "Owner", "Managing Director"]
        families = [RoleFamily.founder, RoleFamily.executive]
    # website conditions
    conds: list[AIWebsiteCondition] = []
    sm = re.search(
        r"(?:actually|really|vraiment|r[ée]ellement)?\s*(?:offer|offers|offering|sell|sells|provide|provides|specializ\w+ in|propos\w+|vend\w*|sp[ée]cialis\w+ (?:en|dans))\s+([^.;,]+?)(?:\s+services?|\s+management)?(?:[.;,]|$)",
        low,
    )
    if sm and re.search(r"actually|really|vraiment|r[ée]ellement|must|doivent|offer|propos", low):
        concept = re.sub(r"^(?:vraiment|r[ée]ellement|really|actually)\s+", "", sm.group(1).strip())
        concept = re.sub(r"^(?:de la|de l'|du|des|de|d')\s*", "", concept).strip()
        if concept and len(concept) < 80 and not re.search(r"\bemail|\bfounder|\bceo\b", concept):
            full = sm.group(0).strip(" .,;")
            key_terms = [
                w for w in re.findall(r"[a-zà-ÿ0-9]+", concept) if w not in _STOP_TERMS and len(w) > 2
            ]
            conds.append(
                AIWebsiteCondition(
                    kind="semantic",
                    concept=f"offers {concept}"
                    + (" services" if "service" in full and "service" not in concept else ""),
                    terms=key_terms[:6] or [concept],
                )
            )
    km = re.search(
        r"(?:mention|mentions|mentioning|contain|contains|talks? about|mentionn\w+|parl\w+ de|contien\w+)\s+([^.;]+)",
        low,
    )
    if km:
        part = km.group(1)
        joiner_all = " and " in part or " et " in part
        terms = [
            x.strip(" '\"") for x in re.split(r",|\bor\b|\bou\b|\band\b|\bet\b", part) if x.strip(" '\"")
        ]
        terms = [x for x in terms if 0 < len(x) <= 40][:6]
        if terms:
            conds.append(AIWebsiteCondition(kind="keyword_all" if joiner_all else "keyword_any", terms=terms))
    for tech in TECH_NAMES:
        if re.search(rf"\b(use|uses|using|utilis\w+|built with|on)\s+{re.escape(tech)}\b", low):
            conds.append(AIWebsiteCondition(kind="technology", terms=[tech.title()]))
    # email
    require_email = mode == CampaignMode.people and not re.search(
        r"\b(no email|email optional|sans email|pas besoin d.?email)\b", low
    )
    # Professional email: SAFE (confirmed) or LIKELY_SAFE (confirmed domain convention + MX + strong name
    # affinity). "strictly verified / SAFE only" keeps SAFE alone; "risky ok" widens.
    statuses = [EmailStatus.SAFE, EmailStatus.LIKELY_SAFE]
    if re.search(
        r"\b(safe only|only safe|strictly verified|smtp[- ]verified|v[ée]rifi[ée]s? smtp|uniquement safe)\b",
        low,
    ):
        statuses = [EmailStatus.SAFE]
    if re.search(r"\brisky\b.*\b(ok|fine|accept)|accept\w* risky|catch[- ]all ok", low):
        statuses = [EmailStatus.SAFE, EmailStatus.LIKELY_SAFE, EmailStatus.RISKY]
    # exclusion
    ex = AIExclusion()
    default_mode = default_exclusion_for_prompt(raw)
    if default_mode:
        ex.mode = default_mode
    if re.search(
        r"(compan\w+|entreprises?|soci[ée]t[ée]s?) (i|que j)\w* (already|d[ée]j[àa])|only new compan|nouvelles entreprises|new companies|companies i already have|don.?t include companies",
        low,
    ):
        ex.mode = (
            ExclusionMode.EXCLUDE_PREVIOUS_PEOPLE_AND_COMPANIES
            if mode == CampaignMode.people
            else ExclusionMode.EXCLUDE_PREVIOUS_COMPANIES
        )
        ex.exclude_existing_companies = True
        ex.allow_new_people_at_existing_companies = False
    if re.search(r"exported|export[ée]s? (before|avant|d[ée]j[àa])|d[ée]j[àa] export", low):
        ex.mode = ExclusionMode.EXCLUDE_EXPORTED
    if re.search(r"\bcontacted\b|d[ée]j[àa] contact", low):
        ex.mode = ExclusionMode.EXCLUDE_CONTACTED
    cm = re.search(r"(?:last|past|derniers?)\s+(\d+)\s*(?:days|jours)", low)
    if cm and re.search(r"seen|vu|scrap|exclude|exclu", low):
        ex.mode = ExclusionMode.EXCLUDE_WITHIN_COOLDOWN
        ex.cooldown_days = int(cm.group(1))
    if re.search(
        r"(uploaded|imported) (file|csv|list)|my (upload|import)|fichier (import|upload)\w*|mon (import|fichier)",
        low,
    ):
        ex.exclude_latest_import = True
        if ex.mode == ExclusionMode.NONE:
            ex.mode = ExclusionMode.EXCLUDE_PREVIOUS_PEOPLE
    for name in sorted(ctx.lists, key=len, reverse=True):
        if (
            len(name) >= 3
            and re.search(rf"\b{re.escape(name)}\b", low)
            and re.search(r"exclude|not|no one|anyone|sans|pas|aucun|except|hors|repeat", low)
        ):
            ex.exclude_list_names.append(name)
    if ex.exclude_list_names and ex.mode in (ExclusionMode.NONE, ExclusionMode.EXCLUDE_PREVIOUS_PEOPLE):
        ex.mode = ExclusionMode.EXCLUDE_SPECIFIC_LISTS if ex.mode == ExclusionMode.NONE else ex.mode
    if re.search(
        r"(ok|okay|fine|acceptable|d.?accord) if the company|company (was|is) already|entreprise (est )?d[ée]j[àa]",
        low,
    ):
        ex.allow_new_people_at_existing_companies = True
        ex.exclude_existing_companies = False
    seed_list = bool(
        re.search(
            r"(this|current|ce|cette) list|(companies|entreprises) (in|de|of) (this|my|cette|ma) list|my existing companies|mes entreprises existantes|existing companies",
            low,
        )
    )
    if re.search(r"different|another|other|autre|nouveau|nouvelle", low) and seed_list:
        ex.mode = ExclusionMode.EXCLUDE_PREVIOUS_PEOPLE
        ex.allow_new_people_at_existing_companies = True
    seed_selection = bool(re.search(r"\b(these|selected|those|ces|celles?-ci|s[ée]lection)\b", low)) and bool(
        re.search(r"for (these|selected|those)|pour (ces|la s[ée]lection)", low)
    )
    min_score = None
    sm2 = re.search(r"score (?:over|above|>|greater than|sup[ée]rieur [àa]|de plus de)\s*(\d+)", low)
    if sm2:
        min_score = int(sm2.group(1))
    max_people = 1
    pm = re.search(
        r"(\d+)\s*(?:people|contacts|personnes|decision makers?)\s*(?:per|par)\s*(?:company|entreprise)", low
    )
    if pm:
        max_people = max(1, min(10, int(pm.group(1))))
    name_bits = [*(i.title() for i in industries[:1]), *(cities[:1] or countries[:1])]
    name = " · ".join(name_bits) or raw[:48]
    return AIParsedCampaign(
        name=name,
        mode=mode,
        target_count=count,
        industries=industries,
        countries=countries,
        cities=cities,
        employee_min=emin,
        employee_max=emax,
        website_conditions=conds,
        titles=titles if mode == CampaignMode.people else [],
        role_families=families if mode == CampaignMode.people else [],
        require_email=require_email,
        accepted_email_statuses=statuses,
        exclusion=ex,
        minimum_icp_score=min_score,
        max_people_per_company=max_people,
        seed_from_current_list=seed_list,
        seed_from_selection=seed_selection,
    )


def to_definition(parsed: AIParsedCampaign, prompt: str, ctx: ParseContext) -> CampaignDefinition:
    """Deterministic mapping + safety defaults (exclusion triggers, list/import resolution)."""
    conds: list[Any] = []
    for c in parsed.website_conditions:
        if c.kind in ("keyword_any", "keyword_all") and c.terms:
            conds.append(KeywordCondition(type=c.kind, terms=[x.strip() for x in c.terms if x.strip()][:10]))
        elif c.kind == "semantic" and (c.concept or c.terms):
            concept = c.concept or " ".join(c.terms)
            conds.append(
                SemanticCondition(
                    type="semantic_service", concept=concept, keywords=[x for x in c.terms if x][:8]
                )
            )
        elif c.kind == "technology" and c.terms:
            conds.append(TechnologyCondition(technologies=c.terms[:5]))
    ex_in = parsed.exclusion
    mode = ex_in.mode
    trigger = default_exclusion_for_prompt(prompt)
    if mode == ExclusionMode.NONE and trigger:
        mode = trigger
    list_ids = [ctx.lists[n.lower()] for n in ex_in.exclude_list_names if n.lower() in ctx.lists]
    if ex_in.exclude_list_names and list_ids and mode in (ExclusionMode.NONE,):
        mode = ExclusionMode.EXCLUDE_SPECIFIC_LISTS
    import_ids = [ctx.latest_import_id] if ex_in.exclude_latest_import and ctx.latest_import_id else []
    previous_companies = ex_in.exclude_existing_companies or mode in (
        ExclusionMode.EXCLUDE_PREVIOUS_COMPANIES,
        ExclusionMode.EXCLUDE_PREVIOUS_PEOPLE_AND_COMPANIES,
    )
    exclusion = ExclusionSpec(
        mode=mode,
        previous_people=mode
        in (ExclusionMode.EXCLUDE_PREVIOUS_PEOPLE, ExclusionMode.EXCLUDE_PREVIOUS_PEOPLE_AND_COMPANIES),
        previous_companies=previous_companies,
        allow_new_people_at_existing_companies=not previous_companies
        and ex_in.allow_new_people_at_existing_companies,
        list_ids=list_ids,
        list_scope="both" if previous_companies else "people",
        import_ids=import_ids,
        cooldown_days=ex_in.cooldown_days,
        include_company_scope=previous_companies,
    )
    seed = Seed()
    if parsed.seed_from_selection and (ctx.selected_company_ids or ctx.selected_person_ids):
        seed = Seed(
            type="selection", company_ids=ctx.selected_company_ids, person_ids=ctx.selected_person_ids
        )
    elif parsed.seed_from_current_list and ctx.current_list_id:
        seed = Seed(type="list", list_id=ctx.current_list_id)
    required: list[Any] = ["company"]
    if parsed.mode == CampaignMode.people:
        required.append("person")
        if parsed.require_email:
            required.append("professional_email")
    defn = CampaignDefinition(
        name=parsed.name[:80] if parsed.name else None,
        mode=parsed.mode,
        target_qualified_count=parsed.target_count,
        company_filters=CompanyFilters(
            industries=parsed.industries,
            keywords=parsed.keywords,
            naf_codes=parsed.naf_codes,
            countries=parsed.countries,
            regions=parsed.regions,
            cities=parsed.cities,
            employee_range=EmployeeRange(min=parsed.employee_min, max=parsed.employee_max)
            if parsed.employee_min is not None or parsed.employee_max is not None
            else None,
        ),
        website_conditions=conds,
        people_filters=PeopleFilters(
            titles=parsed.titles,
            role_families=parsed.role_families,
            max_people_per_company=parsed.max_people_per_company,
        ),
        required_fields=required,
        accepted_email_statuses=parsed.accepted_email_statuses or [EmailStatus.SAFE, EmailStatus.LIKELY_SAFE],
        exclusion=exclusion,
        enrichments=parsed.enrichments,
        seed=seed,
    )
    if parsed.minimum_icp_score is not None:
        defn.minimum_icp_score = parsed.minimum_icp_score
    if ctx.like_profile:
        _apply_like_profile(defn, ctx.like_profile)
    return defn


def _apply_like_profile(defn: CampaignDefinition, profile: dict[str, Any]) -> None:
    """'Find more like these': fill unspecified criteria from the reference leads' profile."""
    cf = defn.company_filters
    if not cf.industries and profile.get("industries"):
        cf.industries = profile["industries"][:2]
    if not cf.countries and profile.get("countries"):
        cf.countries = profile["countries"][:3]
    if not cf.cities and profile.get("cities") and len(profile["cities"]) <= 5:
        cf.cities = profile["cities"]
    if cf.employee_range is None and (
        profile.get("employee_min") is not None or profile.get("employee_max") is not None
    ):
        cf.employee_range = EmployeeRange(min=profile.get("employee_min"), max=profile.get("employee_max"))
    if not defn.people_filters.titles and profile.get("titles"):
        defn.people_filters.titles = profile["titles"][:6]
    if not defn.website_conditions and profile.get("website_conditions"):
        defn.website_conditions = profile["website_conditions"]
    if defn.exclusion.mode == ExclusionMode.NONE:
        defn.exclusion.mode = ExclusionMode.EXCLUDE_PREVIOUS_PEOPLE
        defn.exclusion.previous_people = True


async def parse_prompt(prompt: str, ctx: ParseContext | None = None) -> tuple[CampaignDefinition, str]:
    """Return (definition, parser) where parser ∈ {"gemini", "heuristic"}."""
    ctx = ctx or ParseContext()
    ai = get_ai()
    parsed: AIParsedCampaign | None = None
    parser = "heuristic"
    if ai.available:
        lists_hint = ", ".join(sorted(ctx.lists)[:50]) or "(none)"
        user = (
            f"User request:\n{prompt}\n\nExisting list names (for exclusions): {lists_hint}\n"
            f"Current list: {ctx.current_list_name or '(none)'}; selected rows: "
            f"{len(ctx.selected_person_ids) + len(ctx.selected_company_ids)}"
        )
        try:
            res = await ai.structured(
                role=ModelRole.reasoning, system=ICP_SYSTEM, prompt=user, schema=AIParsedCampaign
            )
            parsed = res.value
            parser = "gemini"
        except (AIUnavailable, RetryableError):
            parsed = None
    if parsed is None:
        parsed = heuristic_parse(prompt, ctx)
    else:
        # deterministic safety net for things the heuristic parser sees reliably
        h = heuristic_parse(prompt, ctx)
        if not parsed.exclusion.exclude_list_names and h.exclusion.exclude_list_names:
            parsed.exclusion.exclude_list_names = h.exclusion.exclude_list_names
        if h.exclusion.exclude_latest_import:
            parsed.exclusion.exclude_latest_import = True
        # A model that drops the location or the business type leaves discovery nowhere to search ("10 coachs
        # sportifs a annecy" came back as titles only): the deterministic parser reads both reliably.
        if not (parsed.cities or parsed.regions or parsed.countries) and (
            h.cities or h.regions or h.countries
        ):
            parsed.cities, parsed.regions, parsed.countries = h.cities, h.regions, h.countries
        elif (parsed.cities or parsed.regions) and not parsed.countries and h.countries:
            parsed.countries = h.countries
        if not (parsed.industries or parsed.keywords) and h.industries:
            parsed.industries = h.industries
    return to_definition(parsed, prompt, ctx), parser
