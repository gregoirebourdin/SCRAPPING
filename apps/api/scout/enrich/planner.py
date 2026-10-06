"""Column planner (PIPELINE §6.1): natural-language column → `EnrichmentPlan`.

Deterministic EN/FR rules run first and pick the cheapest sufficiently reliable resolver. Keyword,
technology and social rules always win. The AI planner is consulted only when no rule matched with
confidence, and it can never choose grounded search for a keyword question.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any

import structlog

from scout.db.enums import ColumnDataType, ColumnKind, CostClass, EntityType, PageType, ResolverType
from scout.enrich.chunks import STOPWORDS
from scout.enrich.matching import dedupe, fold_with_map, tokens
from scout.enrich.types import EnrichmentPlan, Strategy

log = structlog.get_logger("enrich.planner")

STRATEGY_RESOLVER: dict[str, ResolverType] = {
    "keyword": ResolverType.CACHED_WEBSITE,
    "regex": ResolverType.CACHED_WEBSITE,
    "social_profile": ResolverType.CACHED_WEBSITE,
    "website_field": ResolverType.CACHED_WEBSITE,
    "deterministic_field": ResolverType.DETERMINISTIC,
    "tech_detection": ResolverType.TECH_DETECTION,
    "semantic_classifier": ResolverType.AI_ON_CACHED_CONTENT,
    "ai_extraction": ResolverType.AI_ON_CACHED_CONTENT,
    "web_research": ResolverType.AI_WEB_RESEARCH,
    "generated_text": ResolverType.AI_ON_CACHED_CONTENT,
    "composite": ResolverType.COMPOSITE,
}
STRATEGY_COST: dict[str, CostClass] = {
    "keyword": CostClass.FREE,
    "regex": CostClass.FREE,
    "social_profile": CostClass.FREE,
    "website_field": CostClass.FREE,
    "deterministic_field": CostClass.FREE,
    "tech_detection": CostClass.CHEAP,
    "semantic_classifier": CostClass.AI,
    "ai_extraction": CostClass.AI,
    "web_research": CostClass.WEB_SEARCH,
    "generated_text": CostClass.AI,
    "composite": CostClass.FREE,
}
WEBSITE_STRATEGIES = frozenset(
    {
        "keyword",
        "regex",
        "social_profile",
        "website_field",
        "semantic_classifier",
        "ai_extraction",
        "generated_text",
    }
)

NETWORK_LABELS = {
    "instagram": "Instagram",
    "linkedin": "LinkedIn",
    "facebook": "Facebook",
    "tiktok": "TikTok",
    "youtube": "YouTube",
    "twitter": "X (Twitter)",
    "pinterest": "Pinterest",
}
WEBSITE_FIELD_LABELS = {
    "phone": "phone number",
    "email": "contact email",
    "address": "postal address",
    "cta": "main call to action",
    "testimonials": "testimonials",
    "pricing_page": "pricing page",
    "careers_page": "careers page",
    "blog": "blog",
    "newsletter": "newsletter signup",
    "chat_widget": "chat widget",
    "booking_link": "booking link",
}
DETERMINISTIC_PERSON_FIELDS = frozenset(
    {"email_status", "job_title", "seniority", "person_email", "full_name"}
)

# Platforms recognised from a bare column name ("Shopify"). Explicit "uses X" also consults the
# technology registry (scout.tech.builtin.KNOWN_TECHNOLOGIES).
_BARE_TECH: dict[str, str] = {
    "shopify": "Shopify",
    "shopify plus": "Shopify Plus",
    "wordpress": "WordPress",
    "woocommerce": "WooCommerce",
    "wix": "Wix",
    "webflow": "Webflow",
    "squarespace": "Squarespace",
    "magento": "Magento",
    "prestashop": "PrestaShop",
    "bigcommerce": "BigCommerce",
    "drupal": "Drupal",
    "joomla": "Joomla",
    "hubspot": "HubSpot",
    "salesforce": "Salesforce",
    "klaviyo": "Klaviyo",
    "google analytics": "Google Analytics",
    "google tag manager": "Google Tag Manager",
    "hotjar": "Hotjar",
    "framer": "Framer",
    "ghost": "Ghost",
    "elementor": "Elementor",
    "divi": "Divi",
    "next js": "Next.js",
    "nextjs": "Next.js",
}
BUILTIN_TECHNOLOGIES: dict[str, str] = {
    **_BARE_TECH,
    "mailchimp": "Mailchimp",
    "brevo": "Brevo",
    "sendinblue": "Brevo",
    "intercom": "Intercom",
    "drift": "Drift",
    "crisp": "Crisp",
    "zendesk": "Zendesk",
    "tawk": "Tawk.to",
    "tawk to": "Tawk.to",
    "livechat": "LiveChat",
    "tidio": "Tidio",
    "calendly": "Calendly",
    "stripe": "Stripe",
    "ga4": "Google Analytics",
    "gtm": "Google Tag Manager",
    "matomo": "Matomo",
    "segment": "Segment",
    "mixpanel": "Mixpanel",
    "meta pixel": "Meta Pixel",
    "facebook pixel": "Meta Pixel",
    "tiktok pixel": "TikTok Pixel",
    "linkedin insight tag": "LinkedIn Insight Tag",
    "react": "React",
    "vue": "Vue.js",
    "vue js": "Vue.js",
    "nuxt": "Nuxt.js",
    "angular": "Angular",
    "gatsby": "Gatsby",
    "cloudflare": "Cloudflare",
    "vercel": "Vercel",
    "netlify": "Netlify",
    "jquery": "jQuery",
    "bootstrap": "Bootstrap",
    "tailwind": "Tailwind CSS",
    "typeform": "Typeform",
    "pipedrive": "Pipedrive",
    "zoho": "Zoho",
    "axeptio": "Axeptio",
    "didomi": "Didomi",
    "cookiebot": "Cookiebot",
    "onetrust": "OneTrust",
    "optimizely": "Optimizely",
    "manychat": "ManyChat",
    "marketo": "Marketo",
    "pardot": "Pardot",
    "bubble": "Bubble",
}

# ---------------------------------------------------------------------------------------------
# Patterns (all applied to accent-folded, lowercase text)
# ---------------------------------------------------------------------------------------------
_PREFIX = re.compile(
    r"^(?:please\s+|merci de\s+|can you\s+|peux-tu\s+|i want(?: to)?\s+|je veux\s+)?"
    r"(?:(?:add|create|make|insert|ajoute[rz]?|cree[rz]?|creer)\s+(?:a\s+|an\s+|une?\s+|la\s+|le\s+)?"
    r"(?:new\s+|nouvelle?\s+)?(?:column|col|field|colonne|champ)?\s*"
    r"(?:showing|that shows|to show|indicating|that indicates|which indicates|for|with|about|called|named|"
    r"indiquant|qui indique|montrant|pour|avec|sur|nommee?|:|-)?\s*"
    r"|(?:find|get|show|fetch|extract|look ?up|identify|detect|check|tell me|give me|enrich(?: with)?|"
    r"trouve[rz]?|recupere[rz]?|affiche[rz]?|indique[rz]?|donne[rz]?(?:-moi)?|verifie[rz]?|detecte[rz]?|"
    r"identifie[rz]?|extrai[st]|extraire)\s+)"
)
_STRIP = " \t\n.?!:;,\"'`“”«»()[]"
_TERM_LEAD = re.compile(
    r"^(?:the (?:words?|terms?|keywords?|phrases?|names?|brand)|le mot|les mots|le terme|les termes|"
    r"l'expression|any of|one of|either|a|an|the|le|la|les|un|une|des|du|de)\s+|^(?:l|d)'"
)
_TERM_TRAIL = re.compile(
    r"\s+(?:anywhere|somewhere|at all|or not|ou non|ou pas|on it|"
    r"on (?:their|the|its|his|her) (?:web ?site|site|home ?page|landing page|pages?|blog)|"
    r"in (?:their|the|its) (?:content|text|pages?|copy|web ?site|site)|"
    r"sur (?:leur|son|sa|le|la|les) (?:site(?: web| internet)?|page(?: d'accueil)?|pages|blog)|"
    r"dans (?:leur|son|le|les) (?:site|contenu|pages?))\b.*$"
)
_ALT_SEP = re.compile(r"\s*(?:,|/|\|)\s*|\s+(?:or|ou|and|et|&)\s+")
_ALL_SEP = re.compile(r"\s(?:and|et|&)\s")
_QUOTED = re.compile(r"[\"“«]\s*([^\"”»]{1,80}?)\s*[\"”»]")

_MENTION = re.compile(
    r"\b(?:mentions?|mentioning|mentioned|contains?|containing|includes?|including|talks? about|talking about|"
    r"refers? to|references?|uses? the (?:word|term|keyword|phrase)|has the (?:word|term|keyword)|"
    r"mentionne(?:nt)?|contien(?:t|nent)|parle(?:nt)? (?:de |d'|du |des )|font mention (?:de |d')|"
    r"fait mention (?:de |d')|cite(?:nt)?|evoque(?:nt)?|inclu(?:t|ent)|comporte(?:nt)?)"
    r"(?:(?<=[ '])|(?![a-z]))\s*(?P<term>.+)$"
)
_USES = re.compile(
    r"\b(?:uses?|using|utilise(?:nt)?|built (?:with|on)|runs? on|running on|(?:is|are) (?:on|built on|hosted on)|"
    r"powered by|hosted (?:on|by|with)|tourne(?:nt)? sur|(?:est|sont) (?:sur|sous|heberge(?:e|s)? (?:sur|chez))|"
    r"construit (?:avec|sur)|developpe (?:avec|sous|sur)|propulse par|have installed|installed)\s+(?P<term>.+)$"
)
_STACK = re.compile(
    r"\b(?:tech(?:nology)? stack|technolog(?:y|ies)|tech used|stack technique|technos?|website stack|"
    r"(?:which|what) (?:cms|tools|software)|cms|martech|outils? (?:web|utilises|marketing))\b"
)
_SOCIAL = re.compile(r"\b(instagram|insta|linkedin|facebook|tiktok|tik tok|youtube|twitter|pinterest)\b")
_SOCIAL_X = re.compile(r"\b(?:x|twitter) (?:account|profile|handle|url|link|page)\b|\bx\.com\b")
_SOCIAL_NOUN = re.compile(
    r"\b(?:accounts?|profiles?|handles?|pages?|urls?|links?|channels?|usernames?|compte|profil|lien|chaine|presence)\b"
)
_SERVICE_VERB = re.compile(
    r"\b(?:offers?|offering|provides?|providing|sells?|selling|manag(?:e|es|ing|ement)|gestion|gere(?:nt)?|"
    r"propose(?:nt)?|vend(?:ent)?|specializ\w*|specialis\w*|accompagne\w*|helps?|aide(?:nt)?|runs ads)\b"
)
_SERVICE_NOUN = re.compile(
    r"\b(?:services?|prestations?|management|gestion|agency|agence|for (?:their |its )?clients|"
    r"pour (?:leurs|ses|des) clients|creation|design|setup|set up|installation|integration|mise en place|writing|"
    r"redaction|strateg(?:y|ie))\b"
)
_HOME_SCOPE = re.compile(r"\b(?:home ?page|landing page|page d'accueil|accueil)\b")
_BOOL_LEAD = re.compile(
    r"^(?:whether|if|does|do|did|is|are|was|has|have|can|si|s'ils?|est-ce|est ce|sont|ont|a-t-(?:il|elle))\b"
)
_SEMANTIC = re.compile(
    r"\b(?:actually|really|truly|genuinely|vraiment|reellement|offers?|offering|provides?|providing|sells?|"
    r"selling|specializ\w*|specialis\w*|focus(?:es|ed)? on|works? with|serves?|targets?|helps?|"
    r"propose(?:nt)?|vend(?:ent)?|offre(?:nt)?|travaille(?:nt)? avec|accompagne(?:nt)?|cible(?:nt)?|aide(?:nt)?|"
    r"hiring|recrute(?:nt)?|is an? [a-z -]{1,40} (?:agency|company|firm|studio|brand|business)|b2b|b2c)\b"
)
_SEM_SUBJECT = re.compile(
    r"^(?:(?:whether|if|si|est-ce que|est ce que|est-ce qu'|does|do|is|are|has|have)\s+)?"
    r"(?:(?:the|this|that) (?:agency|company|business|firm|brand|studio|team|lead|prospect)|they|it|their (?:company|agency|business)|"
    r"l'agence|l'entreprise|la societe|la marque|l'equipe|ils|elles?|leur (?:entreprise|agence|societe))?\s*"
    r"(?:(?:actually|really|truly|genuinely|vraiment|reellement|currently|still|already)\s+)*"
)
_SEM_VERB_LEAD = re.compile(
    r"^(?:offers?|offering|provides?|providing|sells?|selling|propose(?:nt)?|vend(?:ent)?|offre(?:nt)?|"
    r"specializ\w* in|specialis\w* (?:in|en|dans)|est specialisee? (?:en|dans)|focus(?:es)? on|works? with|"
    r"does|do|has|have|is|are|a|ont)\s+"
)
_GENERIC_CONCEPT_WORDS = frozenset(
    "management gestion service services solution solutions prestation prestations strategy strategie "
    "accompagnement support clients client customers customer".split()
)

_WEBSITE_FIELDS: list[tuple[str, re.Pattern[str], ColumnDataType]] = [
    (
        "testimonials",
        re.compile(
            r"\b(?:testimonials?|temoignages?|avis (?:clients?|google)|customer reviews?|client reviews?|"
            r"reviews? from (?:clients|customers)|what (?:our )?(?:clients|customers) say|social proof|preuve sociale)\b"
        ),
        ColumnDataType.boolean,
    ),
    (
        "cta",
        re.compile(
            r"\b(?:ctas?|call[- ]to[- ]actions?|calls[- ]to[- ]action|main button|primary button|appel a l'action|"
            r"bouton principal)\b"
        ),
        ColumnDataType.text,
    ),
    (
        "pricing_page",
        re.compile(
            r"\b(?:pricing|tarifs?|prix|price) page\b|\bpage (?:de |des )?(?:tarifs?|prix|pricing)\b|"
            r"\b(?:public|published|transparent|displayed|listed) (?:pricing|prices)\b|"
            r"\b(?:prices?|pricing|tarifs?) (?:are )?(?:public|published|displayed|listed|affiches?)\b"
        ),
        ColumnDataType.boolean,
    ),
    (
        "careers_page",
        re.compile(
            r"\b(?:careers?|jobs?|recrutement|carrieres?|join us|emplois?) (?:page|section)\b|"
            r"\bpage (?:carrieres?|recrutement|emplois?|jobs?|careers?)\b"
        ),
        ColumnDataType.boolean,
    ),
    (
        "booking_link",
        re.compile(
            r"\b(?:booking|scheduling|meeting|appointment|calendar|reservation|rendez-vous|rdv) (?:link|page|url|lien)\b|"
            r"\blien (?:de )?(?:prise de )?(?:rendez-vous|rdv|reservation)\b|\bcalendly(?: link)?\b|"
            r"\bhubspot meetings?\b|\bonline booking\b|\bbook (?:a|an) (?:call|meeting|demo|appointment)\b"
        ),
        ColumnDataType.url,
    ),
    (
        "chat_widget",
        re.compile(
            r"\b(?:chat widget|live ?chat|chat en (?:direct|ligne)|chatbox|chat box|website chat|widget (?:de )?chat|"
            r"tchat|chat bubble|messenger widget|support chat)\b"
        ),
        ColumnDataType.boolean,
    ),
    ("newsletter", re.compile(r"\bnewsletters?\b"), ColumnDataType.boolean),
    ("blog", re.compile(r"\bblog\b(?!\s*(?:posts?|articles?))"), ColumnDataType.boolean),
    (
        "email",
        re.compile(
            r"\b(?:e-?mail(?: address)?|adresse (?:e-?mail|mail)|mail de contact|contact (?:e-?mail|address)|"
            r"generic email|courriel)\b"
        ),
        ColumnDataType.email,
    ),
    (
        "phone",
        re.compile(
            r"\b(?:phone(?: number)?|telephone|tel|numero de (?:telephone|tel)|landline|standard telephonique)\b"
        ),
        ColumnDataType.text,
    ),
    (
        "address",
        re.compile(
            r"\b(?:address|adresse(?: postale)?|postal address|street address|head ?office|headquarters|hq|"
            r"siege(?: social)?|locaux)\b"
        ),
        ColumnDataType.text,
    ),
]

_DETERMINISTIC_FIELDS: list[tuple[str, re.Pattern[str], ColumnDataType]] = [
    (
        "email_status",
        re.compile(
            r"\b(?:email status|e-?mail verification|verification status|statut (?:de l'|d')?e-?mail|email validity|"
            r"deliverability|delivrabilite)\b"
        ),
        ColumnDataType.text,
    ),
    (
        "person_email",
        re.compile(
            r"\b(?:work email|professional email|business email|email pro(?:fessionnel)?|person(?:al)? email)\b"
        ),
        ColumnDataType.email,
    ),
    (
        "icp_score",
        re.compile(r"\b(?:icp score|lead score|score icp|fit score|score)\b"),
        ColumnDataType.number,
    ),
    ("seniority", re.compile(r"\b(?:seniority|niveau hierarchique|seniorite)\b"), ColumnDataType.text),
    (
        "job_title",
        re.compile(r"\b(?:job title|title|poste|fonction|intitule(?: de poste)?|role)\b"),
        ColumnDataType.text,
    ),
    ("city", re.compile(r"\b(?:city|ville|town|commune|localite)\b"), ColumnDataType.text),
    ("country", re.compile(r"\b(?:country|pays)\b"), ColumnDataType.text),
    ("region", re.compile(r"\b(?:region|departement|county|province)\b"), ColumnDataType.text),
    (
        "postal_code",
        re.compile(r"\b(?:postal code|post ?code|zip(?: code)?|code postal)\b"),
        ColumnDataType.text,
    ),
    (
        "employee_range",
        re.compile(
            r"\b(?:company size|size|headcount|employees?|employee count|team size|staff|effectifs?|"
            r"taille(?: de l'entreprise)?|salaries|nombre de salaries)\b"
        ),
        ColumnDataType.text,
    ),
    (
        "industry",
        re.compile(
            r"\b(?:industry|sector|secteur(?: d'activite)?|vertical|code naf|naf|code ape|activity code)\b"
        ),
        ColumnDataType.text,
    ),
    (
        "founded_year",
        re.compile(
            r"\b(?:founded|founding year|year founded|creation year|date de creation|annee de creation|"
            r"fondee? en|creee? en|year of creation)\b"
        ),
        ColumnDataType.number,
    ),
    (
        "company_name",
        re.compile(
            r"^(?:(?:their|the|leur) )?(?:(?:company|legal) )?(?:name|nom(?: de l'entreprise| commercial)?|raison sociale)$"
        ),
        ColumnDataType.text,
    ),
    (
        "domain",
        re.compile(
            r"^(?:(?:their|the|leur) )?(?:domain|domaine|website|site web|site internet|website url|url)$"
        ),
        ColumnDataType.url,
    ),
]
_DET_BLOCKERS = re.compile(
    r"\b(?:instagram|tiktok|linkedin|facebook|youtube|audience|followers|abonnes|traffic|trafic)\b"
)

_ROLE_WORDS = (
    r"ceo|founder|co-?founder|owner|president|managing director|cto|cmo|cfo|coo|head of [a-z]+|"
    r"(?:marketing|sales|hr|commercial|communication) (?:manager|director|lead|head)|"
    r"directeur [a-z]+|directrice [a-z]+|directeur|directrice|dirigeant|gerant|gerante|fondateur|fondatrice|pdg|dg|"
    r"responsable [a-z]+"
)
_ROLE_EMAIL = re.compile(
    rf"\b(?P<r1>{_ROLE_WORDS})(?:'s|s')?\s+(?:e-?mail|email address|adresse e-?mail|mail)\b|"
    rf"\b(?:e-?mail|email address|adresse e-?mail|mail)\s+(?:of|for|du|de la|de l'|de)\s+(?:the\s+|leur\s+)?"
    rf"(?P<r2>{_ROLE_WORDS})\b"
)

_GEN_OPENER = re.compile(
    r"\b(?:opener|opening line|ice ?-?breaker|accroche|first line|premiere (?:phrase|ligne)|"
    r"intro(?:duction)? line|personali[sz]ed (?:opener|first line|intro(?:duction)?|line|message|email intro))\b"
)
_GEN_ANGLE = re.compile(
    r"\b(?:outreach angle|(?:sales |prospecting |pitch )?angle|personali[sz]ed (?:reason|angle|hook|pitch|note)|"
    r"reason to (?:reach out|contact)|why (?:we should )?reach out|angle d'approche|angle de (?:prospection|vente)|"
    r"raison de (?:les )?contacter|hook)\b"
)
_GEN_SUMMARY = re.compile(
    r"\b(?:summary|summari[sz]e|resume|one[- ]sentence|one[- ]liner|en une phrase|synthese|elevator pitch|"
    r"description of what (?:they|the company) do(?:es)?|what (?:they|the company) do(?:es)?|"
    r"describe (?:what )?(?:they|the company)|ce qu'ils font|ce que fait|description(?: courte)?|"
    r"short description|tagline)\b"
)
_RESEARCH = re.compile(
    r"\b(?:job (?:postings?|posts?|offers?|openings?|listings?|ads?)|open (?:positions|roles|jobs)|"
    r"offres? d'emploi|annonces? (?:d'emploi|de recrutement)|postes? (?:ouverts?|a pourvoir)|podcasts?|interviews?|"
    r"funding|fund ?raising|raised|levees? de fonds|leve(?:e)? des fonds|investors?|investisseurs?|series [a-d]|"
    r"seed round|press (?:mentions?|coverage|articles?|releases?)|presse|in the news|recent news|latest news|news|"
    r"actualites?|media coverage|retombees (?:presse|mediatiques)|awards?|prix remportes|recompenses?|"
    r"conferences?|speaking engagements?|keynotes?|webinars?|acquisitions?|acquired|rachat|rachete|revenue|"
    r"chiffre d'affaires|turnover|valuation|valorisation|glassdoor|trustpilot|google reviews?|"
    r"(?:review|google) (?:score|rating)|note google)\b"
)
_EXTRACT_MAIN = re.compile(
    r"\b(?:main|primary|core|principal(?:e|es|aux)?|key|top|biggest|flagship|phare)\s+(?:[a-z']+\s+){0,2}"
    r"(?:target|customers?|clients?|niche|offers?|offering|services?|products?|markets?|audiences?|cible|"
    r"offres?|prestations?|produits?|marches?|clienteles?|segments?|industr(?:y|ies)|secteurs?|verticals?|"
    r"competitors?|concurrents?|locations?)\b"
)
_EXTRACT = re.compile(
    r"\b(?:target (?:customers?|audiences?|markets?|clients?|segments?)|ideal (?:customer|client)|icp|niche|cible|"
    r"clientele|persona|pricing|prices?|tarifs?|prix|costs?|how much|combien|starting (?:price|at)|packages?|"
    r"forfaits?|plans?|number of|nombre de|how many|combien de|list of|liste des?|which|what|who|quels?|quelles?|"
    r"lequel|laquelle|clients? (?:list|logos)|notable clients|references clients|case studies|use cases?|"
    r"languages?|langues?|founder(?:'s)? name|ceo(?:'s)? name|nom du (?:fondateur|dirigeant))\b"
)
_COUNT = re.compile(r"\b(?:number of|nombre de|how many|combien de|count of)\b")
_REGEX_EXPLICIT = re.compile(
    r"\b(?:regex|regexp|regular expression|pattern|expression reguliere)\s*[:=]?\s*(?P<p>.+)$"
)
_SLASHED = re.compile(r"/(?P<p>.{2,200})/")

IDENTIFIER_PATTERNS: dict[str, str] = {
    "siren": r"(?:siren|rcs)[^\d\n]{0,30}(\d{3}[\s.]?\d{3}[\s.]?\d{3})(?!\d)",
    "siret": r"siret[^\d\n]{0,20}(\d{3}[\s.]?\d{3}[\s.]?\d{3}[\s.]?\d{5})(?!\d)",
    "vat": r"(?:tva|vat)[^A-Za-z0-9\n]{0,40}(?:intracommunautaire|number|n°|no\.?|:|\s)*([A-Z]{2}\s?[0-9A-Z]{2}\s?\d{3}\s?\d{3}\s?\d{3,5})",
}
_IDENTIFIER = re.compile(
    r"\b(?P<id>siren|siret|vat|tva(?: intracommunautaire)?|vat number|numero de tva|n° tva)\b"
)


# ---------------------------------------------------------------------------------------------
@dataclass
class _Ask:
    """A phrasing of the column request: folded text with a map back to the original casing."""

    original: str
    folded: str
    idx: list[int] | None
    start: int = 0

    @classmethod
    def of(cls, text: str) -> _Ask:
        text = re.sub(r"\s+", " ", text).strip()
        folded, idx = fold_with_map(text)
        ask = cls(original=text, folded=folded, idx=idx)
        for _ in range(3):
            m = _PREFIX.match(ask.core)
            if not m or m.end() == 0:
                break
            ask.start += m.end()
        return ask

    @property
    def core(self) -> str:
        return self.folded[self.start :]

    def orig(self, s: int, e: int) -> str:
        """Original-case text for a core-relative folded span."""
        s, e = s + self.start, e + self.start
        if s >= e:
            return ""
        if self.idx is None:
            return self.original[s:e]
        return self.original[self.idx[s] : self.idx[e - 1] + 1]


@dataclass
class _Rule:
    strategy: Strategy
    strong: bool = True
    data_type: ColumnDataType = ColumnDataType.text
    kind: ColumnKind = ColumnKind.factual
    entity_type: EntityType = EntityType.company
    concept: str | None = None
    keywords: list[str] = dc_field(default_factory=list)
    pattern: str | None = None
    field: str | None = None
    technologies: list[str] = dc_field(default_factory=list)
    input_sources: list[PageType] = dc_field(default_factory=list)
    confidence_threshold: float = 0.8
    refresh_days: int = 30
    explanation: str = ""


def _clean_span(f: str, s: int, e: int) -> tuple[int, int]:
    m = _TERM_TRAIL.search(f[s:e])
    if m:
        e = s + m.start()
    for _ in range(4):
        while s < e and f[s] in _STRIP:
            s += 1
        while e > s and f[e - 1] in _STRIP:
            e -= 1
        m = _TERM_LEAD.match(f[s:e])
        if not m:
            break
        s += m.end()
    return s, e


def _term_spans(f: str, s: int, e: int) -> list[tuple[int, int]]:
    s, e = _clean_span(f, s, e)
    spans: list[tuple[int, int]] = []
    pos = s
    for m in _ALT_SEP.finditer(f, s, e):
        spans.append(_clean_span(f, pos, m.start()))
        pos = m.end()
    spans.append(_clean_span(f, pos, e))
    return [(a, b) for a, b in spans if b > a]


def _variants(term: str) -> list[str]:
    out = [term]
    camel = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", term)
    if camel != term:
        out.append(camel)
    return out


_TECH_ALIAS_CACHE: dict[str, str] | None = None


def known_technology_aliases() -> dict[str, str]:
    """Compact alias ("nextjs") → canonical name: the tech registry (scout.tech.builtin) over the builtin list."""
    global _TECH_ALIAS_CACHE
    if _TECH_ALIAS_CACHE is not None:
        return _TECH_ALIAS_CACHE
    out = {"".join(tokens(k)): v for k, v in BUILTIN_TECHNOLOGIES.items()}
    try:
        from scout.tech.builtin import KNOWN_TECHNOLOGIES
    except ImportError:  # registry not available yet: builtin list only (not cached, retried next call)
        log.debug("enrich.tech_registry_unavailable")
        return out
    out.update({"".join(tokens(str(k))): str(v) for k, v in KNOWN_TECHNOLOGIES.items()})
    _TECH_ALIAS_CACHE = out
    return out


def _lookup_tech(term: str) -> str | None:
    key = "".join(tokens(term))
    return known_technology_aliases().get(key) if key else None


def _is_stop(word: str) -> bool:
    key = " ".join(tokens(word))
    return (
        not key
        or key in STOPWORDS
        or key in {"main", "latest", "derniere", "dernier", "principal", "principale"}
    )


def _content_keywords(concept: str) -> list[str]:
    """Keyword hints from a concept: the trimmed phrase plus its distinctive words."""
    toks = [t for t in re.findall(r"[\w'’+.-]+", concept) if t]
    keep = [t for t in toks if not _is_stop(t) and " ".join(tokens(t)) not in _GENERIC_CONCEPT_WORDS]
    words = concept.strip(_STRIP).split()
    while words and _is_stop(words[0]):
        words.pop(0)
    while words and _is_stop(words[-1]):
        words.pop()
    phrase = " ".join(words).strip(_STRIP)
    out = [phrase] if 1 < len(tokens(phrase)) <= 5 else []
    out.extend(keep)
    return dedupe(out)[:6]


# ---------------------------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------------------------
def _rule_regex(ask: _Ask) -> _Rule | None:
    core = ask.core
    m = _REGEX_EXPLICIT.search(core) or _SLASHED.search(core)
    if m:
        raw = ask.orig(m.start("p"), m.end("p")).strip().strip("`")
        if raw.startswith("/") and raw.endswith("/") and len(raw) > 2:
            raw = raw[1:-1]
        return _Rule(
            "regex",
            data_type=ColumnDataType.text,
            pattern=raw,
            concept=f"Text matching /{raw}/",
            explanation="Pattern match on cached website text — no AI needed",
        )
    m = _IDENTIFIER.search(core)
    if m:
        ident = m.group("id")
        key = "siret" if "siret" in ident else "siren" if "siren" in ident else "vat"
        return _Rule(
            "regex",
            data_type=ColumnDataType.text,
            pattern=IDENTIFIER_PATTERNS[key],
            field=key,
            concept=f"{key.upper()} number",
            input_sources=[PageType.legal, PageType.contact],
            explanation=f"{key.upper()} pattern on cached legal/contact pages — no AI needed",
        )
    return None


def _website_field_rule(name: str, dtype: ColumnDataType, ask: _Ask) -> _Rule:
    sources: list[PageType] = []
    if name == "cta" or (name == "testimonials" and _HOME_SCOPE.search(ask.core)):
        sources = [PageType.home]
    if _BOOL_LEAD.search(ask.core) and dtype != ColumnDataType.boolean and name in {"booking_link"}:
        dtype = ColumnDataType.boolean
    if name in {"pricing_page", "careers_page", "blog"} and re.search(r"\b(?:url|link|lien)\b", ask.core):
        dtype = ColumnDataType.url
    label = WEBSITE_FIELD_LABELS.get(name, name)
    return _Rule(
        "website_field",
        data_type=dtype,
        field=name,
        concept=label,
        input_sources=sources,
        explanation=f"Deterministic detection of the {label} on cached pages — no AI needed",
    )


def _rule_keyword(ask: _Ask) -> _Rule | None:
    core = ask.core
    m = _MENTION.search(core)
    if not m:
        return None
    quoted = [q.strip() for q in _QUOTED.findall(ask.original)]
    s, e = m.start("term"), m.end("term")
    match_all = bool(_ALL_SEP.search(core[s:e]))
    if quoted:
        terms = quoted
    else:
        terms = [ask.orig(a, b) for a, b in _term_spans(core, s, e)]
    terms = [t for t in terms if t]
    if not terms or any(len(tokens(t)) > 5 for t in terms):
        return None
    joined = " ".join(terms)
    for fname, rx, dtype in _WEBSITE_FIELDS[:8]:  # presence-type fields ("contains testimonials")
        if rx.search(" ".join(tokens(joined))):
            return _website_field_rule(fname, dtype, ask)
    keywords = dedupe([v for t in terms for v in _variants(t)])
    label = " and ".join(terms) if match_all else " or ".join(terms)
    return _Rule(
        "keyword",
        data_type=ColumnDataType.boolean,
        keywords=keywords,
        concept=f"Website mentions {label}",
        field="all" if match_all and len(terms) > 1 else None,
        explanation="Website keyword detection on existing crawl — no AI needed",
    )


def _rule_composite(ask: _Ask) -> _Rule | None:
    m = _ROLE_EMAIL.search(ask.core)
    if not m:
        return None
    grp = "r1" if m.group("r1") else "r2"
    role = ask.orig(m.start(grp), m.end(grp))
    return _Rule(
        "composite",
        data_type=ColumnDataType.email,
        field="email_of_role",
        concept=role,
        explanation=f"Finds the {role} among known people at the company and returns their primary email "
        "with its verification status — no AI needed",
    )


def _rule_social(ask: _Ask) -> _Rule | None:
    core = ask.core
    m = _SOCIAL.search(core)
    network = None
    if m:
        network = {"insta": "instagram", "tik tok": "tiktok"}.get(m.group(1), m.group(1))
    elif _SOCIAL_X.search(core):
        network = "twitter"
    if not network:
        return None
    if _SERVICE_VERB.search(core) or _SERVICE_NOUN.search(core):
        return None
    rest = [
        t
        for t in tokens(core)
        if t not in STOPWORDS and t not in {"their", "leur", "leurs", "url", "link", "lien"}
    ]
    bare = len(rest) <= 2
    if not (_SOCIAL_NOUN.search(core) or bare):
        return None
    dtype = ColumnDataType.boolean if _BOOL_LEAD.search(core) else ColumnDataType.url
    label = NETWORK_LABELS.get(network, network.title())
    return _Rule(
        "social_profile",
        data_type=dtype,
        field=network,
        concept=f"{label} profile",
        explanation=f"Reads the {label} link from the cached website — no AI needed",
    )


def _rule_tech(ask: _Ask) -> _Rule | None:
    core = ask.core
    m = _USES.search(core)
    if m:
        spans = _term_spans(core, m.start("term"), m.end("term"))
        names: list[str] = []
        for a, b in spans:
            term = ask.orig(a, b)
            term = re.sub(r"\s+(?:for|as|on|to|pour|comme|sur|in)\b.*$", "", term, flags=re.I).strip(_STRIP)
            if not term or _SOCIAL.search(" ".join(tokens(term))):
                return None
            known = _lookup_tech(term)
            if known:
                names.append(known)
            elif len(tokens(term)) <= 3 and re.search(r"[A-Z]", term):
                names.append(term)  # looks like a product name the detector may know
            else:
                return None
        if names:
            return _Rule(
                "tech_detection",
                data_type=ColumnDataType.boolean,
                technologies=dedupe(names),
                concept=f"Uses {' or '.join(names)}",
                explanation="Technology fingerprinting of the website (scripts, headers, HTML) — no AI needed",
            )
    m2 = _STACK.search(core)
    if m2 and not _SERVICE_VERB.search(core):
        category = "cms" if re.search(r"\bcms\b", core) else None
        return _Rule(
            "tech_detection",
            data_type=ColumnDataType.text,
            field=category,
            concept="Technology stack",
            explanation="Technology fingerprinting of the website (scripts, headers, HTML) — no AI needed",
        )
    return None


def _rule_website_field(ask: _Ask) -> _Rule | None:
    core = ask.core
    if any(rx.search(core) for f, rx, _ in _DETERMINISTIC_FIELDS if f in {"email_status", "person_email"}):
        return None
    service_like = bool(_SERVICE_VERB.search(core) and _SERVICE_NOUN.search(core))
    for fname, rx, dtype in _WEBSITE_FIELDS:
        if rx.search(core):
            if service_like:
                return None
            return _website_field_rule(fname, dtype, ask)
    return None


def _rule_deterministic(ask: _Ask) -> _Rule | None:
    core = ask.core
    if _BOOL_LEAD.search(core) or _SEMANTIC.search(core) or _DET_BLOCKERS.search(core):
        return None
    content = [t for t in tokens(core) if t not in STOPWORDS]
    if len(content) > 5:
        return None
    for fname, rx, dtype in _DETERMINISTIC_FIELDS:
        if rx.search(core):
            entity = EntityType.person if fname in DETERMINISTIC_PERSON_FIELDS else EntityType.company
            return _Rule(
                "deterministic_field",
                data_type=dtype,
                field=fname,
                entity_type=entity,
                concept=fname.replace("_", " "),
                explanation=f"Copied from the lead record ({fname.replace('_', ' ')}) — no AI needed",
            )
    return None


def _rule_generated(ask: _Ask, *, summary: bool) -> _Rule | None:
    core = ask.core
    checks = (
        [("summary", _GEN_SUMMARY)] if summary else [("opener", _GEN_OPENER), ("outreach_angle", _GEN_ANGLE)]
    )
    for fname, rx in checks:
        if rx.search(core):
            concept = {
                "summary": "One-sentence summary of what the company does and for whom",
                "outreach_angle": "Personalized outreach angle grounded in the company's website",
                "opener": "Personalized cold-email opening line",
            }[fname]
            return _Rule(
                "generated_text",
                data_type=ColumnDataType.text,
                kind=ColumnKind.generated,
                field=fname,
                concept=concept,
                confidence_threshold=0.0,
                input_sources=[PageType.home, PageType.about, PageType.services],
                explanation="AI-written text from cached website content and lead facts "
                "(labeled generated, never used as evidence)",
            )
    return None


def _rule_research(ask: _Ask) -> _Rule | None:
    if not _RESEARCH.search(ask.core):
        return None
    concept = ask.orig(0, len(ask.core)).strip(_STRIP)
    return _Rule(
        "web_research",
        data_type=ColumnDataType.text,
        concept=concept,
        confidence_threshold=0.6,
        refresh_days=14,
        keywords=_content_keywords(concept),
        explanation="Grounded web search with cited sources (results cached 14 days)",
    )


def _extraction_rule(ask: _Ask, *, strong: bool) -> _Rule:
    core = ask.core
    concept = ask.orig(0, len(core)).strip(_STRIP)
    dtype = ColumnDataType.number if _COUNT.search(core) else ColumnDataType.text
    sources = [PageType.home, PageType.about, PageType.services, PageType.solutions]
    if re.search(
        r"\b(?:pricing|prices?|tarifs?|prix|costs?|forfaits?|packages?|plans?|combien|how much)\b", core
    ):
        sources = [PageType.pricing, PageType.services, PageType.home]
    elif re.search(r"\b(?:target|customers?|clients?|cible|clientele|niche|audiences?|persona|icp)\b", core):
        sources = [PageType.home, PageType.about, PageType.services, PageType.case_studies]
    return _Rule(
        "ai_extraction",
        strong=strong,
        data_type=dtype,
        concept=concept,
        confidence_threshold=0.7,
        keywords=_content_keywords(concept),
        input_sources=sources,
        explanation="AI extraction from the most relevant cached website passages, with a verbatim quote",
    )


def _rule_extraction(ask: _Ask) -> _Rule | None:
    if _BOOL_LEAD.search(ask.core):
        return None
    if _EXTRACT_MAIN.search(ask.core) or _EXTRACT.search(ask.core):
        return _extraction_rule(ask, strong=True)
    return None


def _semantic_rule(ask: _Ask, *, strong: bool) -> _Rule:
    core = ask.core
    m = _SEM_SUBJECT.match(core)
    s = m.end() if m else 0
    concept = ask.orig(s, len(core)).strip(_STRIP) or ask.orig(0, len(core)).strip(_STRIP)
    kw_src = concept
    lead = _SEM_VERB_LEAD.match(" ".join(tokens(concept)))
    if lead:  # drop the leading verb from the keyword phrase, keep original casing for the rest
        words = concept.split()
        kw_src = " ".join(words[len(lead.group(0).split()) :]) or concept
    sources = [PageType.services, PageType.home, PageType.about, PageType.case_studies, PageType.solutions]
    if re.search(r"\b(?:hiring|recrut\w*)\b", core):
        sources = [PageType.careers, PageType.home, PageType.about]
    return _Rule(
        "semantic_classifier",
        strong=strong,
        data_type=ColumnDataType.boolean,
        concept=concept,
        keywords=_content_keywords(kw_src),
        input_sources=sources,
        explanation="AI classification on the most relevant cached website passages, "
        "with a verbatim evidence quote",
    )


def _rule_semantic(ask: _Ask) -> _Rule | None:
    if _BOOL_LEAD.search(ask.core) or _SEMANTIC.search(ask.core):
        return _semantic_rule(ask, strong=True)
    return None


def _rule_bare(ask: _Ask) -> _Rule | None:
    """A bare product/brand/term as the whole request ("ManyChat", "Shopify")."""
    core = ask.core.strip(_STRIP)
    toks = tokens(core)
    if not toks or len(toks) > 3 or any(t in STOPWORDS for t in toks):
        return None
    key = " ".join(toks)
    if key in _BARE_TECH:
        tech = _BARE_TECH[key]
        return _Rule(
            "tech_detection",
            data_type=ColumnDataType.boolean,
            technologies=[tech],
            concept=f"Uses {tech}",
            explanation="Technology fingerprinting of the website (scripts, headers, HTML) — no AI needed",
        )
    term = ask.orig(0, len(ask.core)).strip(_STRIP)
    # Brand-like names (CamelCase, digits/dots, acronyms, known tech) are confident; plain words stay weak so
    # the AI planner may reinterpret them ("Offices"), with a keyword mention as the offline fallback.
    brandlike = bool(
        re.search(r"[a-z][A-Z]", term)
        or re.search(r"[\d.]", term)
        or re.fullmatch(r"[A-Z]{2,6}", term)
        or known_technology_aliases().get("".join(toks))
    )
    return _Rule(
        "keyword",
        strong=brandlike,
        data_type=ColumnDataType.boolean,
        keywords=dedupe(_variants(term)),
        concept=f"Website mentions {term}",
        explanation="Website keyword detection on existing crawl — no AI needed",
    )


_ORDER = (
    _rule_regex,
    _rule_keyword,
    _rule_composite,
    _rule_social,
    _rule_tech,
    _rule_website_field,
    _rule_deterministic,
    lambda a: _rule_generated(a, summary=False),
    _rule_research,
    lambda a: _rule_generated(a, summary=True),
    _rule_extraction,
    _rule_semantic,
)


def _deterministic(text: str, *, allow_bare: bool, data_type: ColumnDataType | None) -> _Rule:
    ask = _Ask.of(text)
    for rule in _ORDER:
        r = rule(ask)
        if r is not None:
            return r
    if allow_bare:
        r = _rule_bare(ask)
        if r is not None:
            return r
    if data_type == ColumnDataType.boolean or _BOOL_LEAD.search(ask.core):
        return _semantic_rule(ask, strong=False)
    return _extraction_rule(ask, strong=False)


def _to_plan(name: str, r: _Rule, data_type: ColumnDataType | None) -> EnrichmentPlan:
    dtype = data_type or r.data_type
    if r.strategy == "generated_text":
        dtype = ColumnDataType.text
    return EnrichmentPlan(
        name=name.strip() or "Column",
        data_type=dtype,
        kind=r.kind,
        entity_type=r.entity_type,
        resolver=STRATEGY_RESOLVER[r.strategy],
        strategy=r.strategy,
        concept=r.concept,
        keywords=r.keywords,
        pattern=r.pattern,
        field=r.field,
        technologies=r.technologies,
        input_sources=r.input_sources,
        confidence_threshold=r.confidence_threshold,
        refresh_days=r.refresh_days,
        cost_class=STRATEGY_COST[r.strategy],
        explanation=r.explanation,
    )


async def _ai_plan(name: str, instruction: str | None, fallback: EnrichmentPlan) -> EnrichmentPlan | None:
    from scout.ai.factory import get_ai
    from scout.ai.models import ModelRole
    from scout.ai.prompts import UNTRUSTED_CONTENT_RULES
    from scout.enrich.ai_schemas import ColumnPlanDraft

    ai = get_ai()
    system = (
        "You plan enrichment columns for a B2B lead database. Choose the cheapest reliable strategy:\n"
        "- semantic_classifier: true/false question about what the company does, answered from its website.\n"
        "- ai_extraction: a short value stated on the company website (target customer, offer, price, count).\n"
        "- website_field: one of phone/email/address/cta/testimonials/pricing_page/careers_page/blog/newsletter/"
        "chat_widget/booking_link (put it in `field`).\n"
        "- deterministic_field: a canonical record field (city, country, employee_range, industry, founded_year).\n"
        "- web_research: ONLY when the answer requires external sources (news, funding, job postings, podcasts).\n"
        "- generated_text: written copy (summaries, outreach angles, openers).\n"
        "Never choose web_research for something visible on the company's own website.\n\n"
        + UNTRUSTED_CONTENT_RULES
    )
    prompt = (
        f"Column name: {name}\nInstruction: {instruction or '(none)'}\n"
        f"Default plan if unsure: {fallback.strategy} ({fallback.data_type.value}).\n"
        "Return the plan."
    )
    res = await ai.structured(role=ModelRole.reasoning, system=system, prompt=prompt, schema=ColumnPlanDraft)
    d = res.value
    strategy: Strategy = d.strategy
    if strategy == "website_field" and d.field not in WEBSITE_FIELD_LABELS:
        return None
    if strategy == "deterministic_field" and d.field not in {f for f, _, _ in _DETERMINISTIC_FIELDS}:
        return None
    kind = ColumnKind.generated if strategy == "generated_text" else ColumnKind.factual
    dtype = ColumnDataType.text if strategy == "generated_text" else ColumnDataType(d.data_type)
    entity = EntityType.person if (d.field or "") in DETERMINISTIC_PERSON_FIELDS else EntityType.company
    return EnrichmentPlan(
        name=name.strip() or "Column",
        data_type=dtype,
        kind=kind,
        entity_type=entity,
        resolver=STRATEGY_RESOLVER[strategy],
        strategy=strategy,
        concept=d.concept or fallback.concept,
        keywords=dedupe(d.keywords)[:10],
        field=d.field,
        enum_values=d.enum_values if dtype == ColumnDataType.enum else [],
        input_sources=[PageType(s) for s in d.input_sources] or fallback.input_sources,
        confidence_threshold={"web_research": 0.6, "ai_extraction": 0.7, "generated_text": 0.0}.get(
            strategy, 0.8
        ),
        refresh_days=14 if strategy == "web_research" else 30,
        cost_class=STRATEGY_COST[strategy],
        explanation=(d.explanation or fallback.explanation)[:300],
    )


async def plan_column(
    name: str,
    instruction: str | None = None,
    *,
    data_type: ColumnDataType | None = None,
    use_ai: bool = True,
) -> EnrichmentPlan:
    """Plan a column from its name and natural-language instruction (EN/FR)."""
    instruction = (instruction or "").strip() or None
    rule: _Rule | None = None
    if instruction:
        rule = _deterministic(instruction, allow_bare=False, data_type=data_type)
        if not rule.strong:
            alt = _deterministic(name, allow_bare=True, data_type=data_type)
            if alt.strong:  # e.g. name "ManyChat" + vague instruction
                rule = alt
    else:
        rule = _deterministic(name, allow_bare=True, data_type=data_type)
    plan = _to_plan(name, rule, data_type)
    if not rule.strong and use_ai:
        from scout.ai.factory import get_ai

        if get_ai().available:
            try:
                ai_plan = await _ai_plan(name, instruction, plan)
            except Exception as exc:  # planner failure never blocks column creation
                log.warning("enrich.ai_planner_failed", error=str(exc))
                ai_plan = None
            if ai_plan is not None:
                if data_type is not None and ai_plan.strategy != "generated_text":
                    ai_plan.data_type = data_type
                return ai_plan
    return plan


# ---------------------------------------------------------------------------------------------
_RESOLVER_LABELS = {
    "keyword": "Website keyword match",
    "regex": "Pattern match on website",
    "social_profile": "Social link on website",
    "website_field": "Website extractor",
    "deterministic_field": "Lead record",
    "tech_detection": "Technology detection",
    "semantic_classifier": "AI classifier on website",
    "ai_extraction": "AI extraction from website",
    "web_research": "Web research (grounded)",
    "generated_text": "AI writer",
    "composite": "Combined lookup",
}
_COST_LABELS = {
    CostClass.FREE: "Free · no AI",
    CostClass.CHEAP: "Low cost · no AI",
    CostClass.AI: "AI · ≈ $0.001 / row",
    CostClass.WEB_SEARCH: "Web search · ≈ $0.02 / row",
    CostClass.EXPENSIVE: "Expensive · browser + research",
}


def describe_plan(plan: EnrichmentPlan) -> dict[str, Any]:
    """Short labels for the UI "column created" card."""
    strategy = plan.strategy
    if strategy in WEBSITE_STRATEGIES:
        if plan.input_sources:
            pages = ", ".join(str(getattr(s, "value", s)).replace("_", " ") for s in plan.input_sources[:4])
            sources = f"Cached website ({pages})"
        else:
            sources = "Cached website (all crawled pages)"
        if strategy == "generated_text":
            sources += " + lead facts"
    elif strategy == "tech_detection":
        sources = "Website fingerprints (scripts, headers, HTML)"
    elif strategy == "web_research":
        sources = "Web search with cited sources"
    elif strategy == "composite":
        sources = "People & emails already in Research"
    else:
        sources = "Lead record"
    return {
        "resolver_label": _RESOLVER_LABELS.get(strategy, strategy),
        "sources_label": sources,
        "cost_label": _COST_LABELS.get(plan.cost_class, str(plan.cost_class)),
        "kind": plan.kind.value,
        "explanation": plan.explanation,
    }
