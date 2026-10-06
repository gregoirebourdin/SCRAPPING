"""Job-title normalization (EN/FR/DE/ES/IT/NL) → canonical English title, role family, seniority,
department and decision power; plus matching against requested roles.

The original title is never altered (``TitleInfo.original``).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache

from rapidfuzz import fuzz

from scout.db.enums import RoleFamily, Seniority
from scout.extract.types import TitleInfo
from scout.util.text import collapse_ws, normalize_key

F, S = RoleFamily, Seniority

_SENIORITY_RANK = {
    S.owner: 7, S.c_level: 6, S.vp: 5, S.director: 4, S.head: 3, S.manager: 2, S.senior: 1, S.entry: 0, S.unknown: 0,
}
TOP_TITLES = frozenset(
    {"Founder", "Co-Founder", "Chief Executive Officer", "Owner", "Managing Director", "President",
     "Managing Partner", "Chairman"}
)

# ---- departments --------------------------------------------------------------------------------

# (regex on normalized text, english label, role family)
_DEPARTMENTS: tuple[tuple[str, str, RoleFamily], ...] = (
    (r"growth", "Growth", F.marketing),
    (r"marketing|acquisition|brand(?!ing design)|seo|sea|contenu|content|digital marketing", "Marketing", F.marketing),
    (r"communication|communications|relations presse|public relations|\bpr\b|presse", "Communications", F.marketing),
    (r"reseaux sociaux|social media|community", "Social Media", F.marketing),
    (r"commercial|commerciale|sales|ventes|vente|vertrieb|business development|developpement commercial|bizdev|"
     r"revenue|partenariats|partnerships|grands comptes|key account|comercial|commerciale", "Sales", F.sales),
    (r"clientele|client|customer|success|support|service client|relation client|kundenservice", "Customer Success", F.customer),
    (r"artistique|creation|creative|creatif|creatrice|design|graphi|studio|art\b|arte|artistico|motion|video|"
     r"photo|ux|ui\b|brand design|identite", "Creative", F.creative),
    (r"technique|technical|technology|technologie|tech\b|informatique|\bit\b|\bsi\b|systemes d information|"
     r"engineering|ingenierie|developpement logiciel|software|data|devops|infrastructure|r d|recherche", "Engineering", F.technology),
    (r"produit|product|produkt", "Product", F.product),
    (r"financ|comptab|accounting|controlling|tresorerie|treasury|daf|administratif et financier|finanzen", "Finance", F.finance),
    (r"ressources humaines|human resources|\brh\b|\bhr\b|people|talent|recrutement|recruitment|recruiting|personal\b|"
     r"personnel", "Human Resources", F.hr),
    (r"juridique|legal|compliance|conformite|droit|recht", "Legal", F.legal),
    (r"operations|operationnel|ops\b|logistique|logistics|supply|production|achats|purchasing|qualite|quality|"
     r"administration|office|projet|project|programme|program", "Operations", F.operations),
)


def _department(text: str) -> tuple[str | None, RoleFamily | None]:
    for rx, label, family in _DEPARTMENTS:
        if re.search(rx, text):
            return label, family
    return None, None


# ---- rules -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Rule:
    pattern: re.Pattern[str]
    title: str | Callable[[str], str]
    family: RoleFamily | None  # None → from department (fallback: other)
    seniority: Seniority
    power: int
    department: str | None = "Executive"


def _r(rx: str, title: str | Callable[[str], str], family: RoleFamily | None, seniority: Seniority, power: int,
       department: str | None = "Executive") -> _Rule:
    return _Rule(re.compile(rx), title, family, seniority, power, department)


def _dept_title(suffix: str, fallback: str) -> Callable[[str], str]:
    def build(text: str) -> str:
        label, _ = _department(text)
        return f"{label} {suffix}" if label else fallback

    return build


def _head_title(text: str) -> str:
    m = re.search(r"\bhead of ([a-z ]{2,40})", text)
    if m:
        rest = m.group(1).strip()
        rest = re.split(r"\b(?:and|et|chez|at|de la|du|des|pour|for)\b", rest)[0].strip() or rest
        return "Head of " + " ".join(w.upper() if w in ("hr", "seo", "ux", "ui", "it", "pr") else w.capitalize() for w in rest.split())
    label, _ = _department(text)
    return f"Head of {label}" if label else "Head"


def _vp_title(text: str) -> str:
    label, _ = _department(text)
    return f"VP {label}" if label else "Vice President"


def _chief_title(text: str) -> str:
    m = re.search(r"chief [a-z]+ officer", text)
    return m.group(0).title() if m else "Chief Officer"


_FOUNDER_WORDS = r"(?:founder|fondateur|fondatrice|grunder|grunderin|gruender|gruenderin|fundador|fundadora|fondatore|oprichter|oprichtster)"

_RULES: tuple[_Rule, ...] = (
    _r(rf"\bco ?{_FOUNDER_WORDS}\b|\bcofounder\b|\bmitgrunder(in)?\b|\bmitgruender(in)?\b|\bcofundador(a)?\b|\bmede ?oprichter\b",
       "Co-Founder", F.founder, S.owner, 95),
    _r(rf"\b{_FOUNDER_WORDS}\b|\b(createur|creatrice) (de|d)\b", "Founder", F.founder, S.owner, 95),
    _r(r"\bmanaging partner\b|\bassociee? gerante?\b|\bsenior partner\b", "Managing Partner", F.executive, S.owner, 95),
    _r(r"\b(pdg|p d g|ceo|c e o|chief executive( officer)?|amministratore delegato|consejer[oa] delegad[oa]|"
       r"president directeur general|presidente directrice generale|president directrice generale)\b",
       "Chief Executive Officer", F.executive, S.c_level, 95),
    _r(r"\b(directeur general|directrice generale|dg|dga|managing director|geschaftsfuhrer(in)?|"
       r"geschaeftsfuehrer(in)?|geschaftsfuhrende(r)? gesellschafter(in)?|director general|directora general|"
       r"direttore generale|direttrice generale|algemeen directeur|director ejecutivo|directora ejecutiva|"
       r"general manager|dirigeant|dirigeante|dirigente)\b",
       "Managing Director", F.executive, S.c_level, 95),
    _r(r"\b(gerant|gerante|co gerant|co gerante|cogerant|cogerante)\b", "Managing Director", F.executive, S.owner, 95),
    _r(r"\b(owner|co owner|business owner|proprietaire|inhaber|inhaberin|dueno|duena|propietari[oa]|titolare|"
       r"eigenaar|eigenaresse|chef d entreprise|cheffe d entreprise|chef d entreprises)\b",
       "Owner", F.founder, S.owner, 95),
    _r(r"\b(vice president|vice presidente|vice presidenta|vizeprasident(in)?|vp|svp|evp|vice chair)\b",
       _vp_title, None, S.vp, 70, None),
    _r(r"\b(chairman|chairwoman|chairperson|chair of the board|president du conseil|presidente du conseil)\b",
       "Chairman", F.executive, S.c_level, 90),
    _r(r"\b(president|presidente|presidenta|prasident|prasidentin|praesident|praesidentin|voorzitter)\b",
       "President", F.executive, S.c_level, 95),
    _r(r"\b(associe|associee|partner|socio|socia|gesellschafter|gesellschafterin|teilhaber|teilhaberin|vennoot)\b"
       r"(?! (manager|success|marketing|relations|program|programme|development|developpement))",
       "Partner", F.executive, S.owner, 85),
    _r(r"\b(coo|chief operating officer)\b", "Chief Operating Officer", F.operations, S.c_level, 85, "Operations"),
    _r(r"\b(cmo|chief marketing officer)\b", "Chief Marketing Officer", F.marketing, S.c_level, 85, "Marketing"),
    _r(r"\b(cto|chief technology officer|chief technical officer)\b", "Chief Technology Officer", F.technology, S.c_level, 85, "Engineering"),
    _r(r"\b(cfo|chief financial officer|daf|directeur administratif et financier|directrice administrative et financiere)\b",
       "Chief Financial Officer", F.finance, S.c_level, 85, "Finance"),
    _r(r"\b(cpo|chief product officer)\b", "Chief Product Officer", F.product, S.c_level, 85, "Product"),
    _r(r"\b(cro|chief revenue officer|chief commercial officer)\b", "Chief Revenue Officer", F.sales, S.c_level, 85, "Sales"),
    _r(r"\b(cco|chief creative officer)\b", "Chief Creative Officer", F.creative, S.c_level, 85, "Creative"),
    _r(r"\b(chro|chief people officer|chief human resources officer)\b", "Chief People Officer", F.hr, S.c_level, 85, "Human Resources"),
    _r(r"\b(cdo|chief digital officer|chief data officer)\b", "Chief Digital Officer", F.technology, S.c_level, 85, "Engineering"),
    _r(r"\bchief [a-z]+ officer\b", _chief_title, None, S.c_level, 85, None),
    _r(r"\bchief of staff\b", "Chief of Staff", F.operations, S.director, 70, "Operations"),
    _r(r"\b(directeur|directrice|responsable) de (la )?publication\b|\bpublication director\b",
       "Publication Director", F.executive, S.director, 60),
    _r(r"\b(directeur|directrice|director|directora|direttore|direttrice) (artistique|artistico|artistica|de arte)\b|\bart director\b|\bdirection artistique\b",
       "Art Director", F.creative, S.director, 70, "Creative"),
    _r(r"\b(directeur|directrice) (de (la )?creation|creati(f|ve))\b|\bcreative director\b|\bkreativdirektor(in)?\b",
       "Creative Director", F.creative, S.director, 70, "Creative"),
    _r(r"\b(directeur|directrice|director|directora) (d agence|de l agence|de agencia)\b|\bagency director\b|\bbranch director\b",
       "Agency Director", F.executive, S.director, 80),
    _r(r"\bdrh\b|\b(directeur|directrice) (des )?(ressources humaines|rh)\b|\bhr director\b",
       "HR Director", F.hr, S.director, 70, "Human Resources"),
    _r(r"\b(directeur|directrice|director|directora|direttore|direttrice|direktor|direktorin|leiter|leiterin)\b",
       _dept_title("Director", "Director"), None, S.director, 70, None),
    _r(r"\bhead of\b|\bresponsable\b|\bjef[ea] de\b|\bresponsabile\b|\bteamleiter(in)?\b|\babteilungsleiter(in)?\b",
       _head_title, None, S.head, 60, None),
    _r(r"\b(chef|cheffe) de (projet|projets)\b|\bproject manager\b|\bprojektleiter(in)?\b|\bprojektmanager(in)?\b|"
       r"\bjef[ea] de proyecto\b|\bproject lead\b|\bproduction manager\b",
       "Project Manager", F.operations, S.manager, 40, "Operations"),
    _r(r"\bsocial media manager\b|\bsocial media\b", "Social Media Manager", F.marketing, S.manager, 30, "Marketing"),
    _r(r"\bcommunity manager\b|\bcm\b", "Community Manager", F.marketing, S.manager, 30, "Marketing"),
    _r(r"\b(key )?account manager\b|\bkam\b|\bcharge(e)? d affaires\b|\bchef de publicite\b|\baccount executive\b",
       "Account Manager", F.sales, S.manager, 40, "Sales"),
    _r(r"\boffice manager\b", "Office Manager", F.operations, S.manager, 30, "Operations"),
    _r(r"\b(chef|cheffe) de produit\b|\bproduct manager\b|\bproduct owner\b", "Product Manager", F.product, S.manager, 40, "Product"),
    _r(r"\bmanager\b|\bmanagerin\b|\bgestionnaire\b", _dept_title("Manager", "Manager"), None, S.manager, 40, None),
    _r(r"\b(team lead|tech lead|lead developer|lead designer|lead dev|lead)\b", _dept_title("Lead", "Lead"), None, S.senior, 35, None),
    _r(r"\b(stagiaire|intern|internship|alternant|alternante|apprenti|apprentie|werkstudent(in)?|praktikant(in)?|"
       r"becari[oa]|stagista)\b", "Intern", None, S.entry, 10, None),
    _r(r"\b(assistant|assistante|secretaire|secretary|receptionist|receptionniste|office assistant)\b",
       _dept_title("Assistant", "Assistant"), F.operations, S.entry, 15, None),
    _r(r"\b(commercial|commerciale|business developer|bizdev|sales representative|sales rep|sales executive|"
       r"ingenieur commercial|ingenieure commerciale|vendeur|vendeuse|attache commercial|attachee commerciale|"
       r"conseiller commercial|conseillere commerciale|account executive)\b",
       "Sales Representative", F.sales, S.entry, 20, "Sales"),
    _r(r"\b(charge|chargee) (de|d) (communication|marketing)\b|\bmarketing (assistant|specialist|executive|officer)\b|"
       r"\bgrowth hacker\b|\btraffic manager\b|\bseo\b|\bsea\b|\bcontent manager\b|\bredacteur web\b",
       _dept_title("Specialist", "Marketing Specialist"), F.marketing, S.entry, 20, "Marketing"),
    _r(r"\b(charge|chargee) de recrutement\b|\brecruteur\b|\brecruteuse\b|\brecruiter\b|\btalent acquisition\b|"
       r"\b(charge|chargee) rh\b|\bhr (officer|specialist|generalist)\b|\bgestionnaire rh\b",
       "HR Specialist", F.hr, S.entry, 20, "Human Resources"),
    _r(r"\b(developpeur|developpeuse|developer|engineer|ingenieur|ingenieure|entwickler(in)?|programmer|"
       r"programmeur|devops|data scientist|data analyst|data engineer|integrateur|integratrice|webmaster|"
       r"desarrollador(a)?|sviluppatore)\b",
       "Engineer", F.technology, S.entry, 20, "Engineering"),
    _r(r"\b(designer|graphiste|webdesigner|web designer|illustrat(eur|rice|or)|motion designer|monteur|monteuse|"
       r"videaste|photographe|photographer|redacteur|redactrice|copywriter|concepteur redacteur|"
       r"conceptrice redactrice|ux|ui|infographiste|directeur photo)\b",
       "Designer", F.creative, S.entry, 20, "Creative"),
    _r(r"\b(comptable|accountant|controleur de gestion|controleuse de gestion|financial controller|buchhalter(in)?)\b",
       "Accountant", F.finance, S.entry, 20, "Finance"),
    _r(r"\b(juriste|avocat|avocate|lawyer|legal counsel|paralegal|rechtsanwalt|rechtsanwaltin|abogad[oa])\b",
       "Legal Counsel", F.legal, S.entry, 20, "Legal"),
    _r(r"\b(customer success|support|service client|charge de clientele|chargee de clientele|customer care|"
       r"conseiller client|conseillere client)\b", "Customer Success", F.customer, S.entry, 20, "Customer Success"),
    _r(r"\b(rh|hr|ressources humaines|human resources|personalwesen)\b", "Human Resources", F.hr, S.unknown, 20,
       "Human Resources"),
    _r(r"\b(consultant|consultante|conseiller|conseillere|advisor|adviser|berater(in)?|consulente)\b",
       _dept_title("Consultant", "Consultant"), None, S.entry, 20, None),
    _r(r"\b(associate|analyst|analyste|coordinator|coordinateur|coordinatrice|officer|specialist|specialiste|"
       r"technicien|technicienne|technician|operator|operateur|employe|employee|collaborateur|collaboratrice)\b",
       _dept_title("Specialist", "Staff"), None, S.entry, 20, None),
)


_DEPUTY_RE = re.compile(r"\b(adjoint|adjointe|deputy|stellvertretende?r?|vice|delegue|deleguee|assistant to|assistante de)\b")
_JUNIOR_RE = re.compile(r"\b(junior|jr|debutant|trainee)\b")
_SENIOR_RE = re.compile(r"\b(senior|sr|confirme|confirmee|experimente|principal)\b")


@lru_cache(maxsize=8192)
def _match(norm: str) -> tuple[int, _Rule] | None:
    for i, rule in enumerate(_RULES):
        if rule.pattern.search(norm):
            return i, rule
    return None


def normalize_title(title: str) -> TitleInfo:
    """Canonical English title + role family + seniority + department + decision power (0–100)."""
    original = title if title is not None else ""
    norm = normalize_key(original)
    hit = _match(norm) if norm else None
    if hit is None:
        return TitleInfo(
            original=original,
            normalized_title=collapse_ws(original),
            role_family=F.other,
            seniority=S.unknown,
            department=None,
            decision_power=20 if norm else 0,
        )
    _i, rule = hit
    canonical = rule.title(norm) if callable(rule.title) else rule.title
    dept_label, dept_family = _department(norm)
    family = rule.family or dept_family
    if family is None:
        family = F.executive if rule.seniority in (S.c_level, S.vp, S.director, S.owner) else F.other
    department = rule.department if rule.department is not None else dept_label
    if rule.department == "Executive" and rule.family in (F.founder, F.executive):
        department = "Executive"
    seniority = rule.seniority
    power = rule.power
    if _DEPUTY_RE.search(norm) and seniority in (S.c_level, S.owner, S.director, S.head):
        if re.search(r"\b(delegue|deleguee)\b", norm):
            power = min(power, 85)
            canonical = f"Deputy {canonical}" if not canonical.startswith("Deputy") else canonical
        else:
            power = max(0, power - 20)
            canonical = f"Deputy {canonical}" if not canonical.startswith(("Deputy", "Vice")) else canonical
            if seniority in (S.c_level, S.owner):
                seniority = S.vp
    if seniority in (S.entry, S.unknown, S.senior) and _SENIOR_RE.search(norm):
        seniority, power = S.senior, power + 5
        canonical = canonical if canonical.startswith("Senior") else f"Senior {canonical}"
    elif seniority in (S.entry, S.senior, S.manager) and _JUNIOR_RE.search(norm):
        seniority, power = S.entry, max(5, power - 10)
    return TitleInfo(
        original=original,
        normalized_title=canonical,
        role_family=family,
        seniority=seniority,
        department=department,
        decision_power=max(0, min(100, power)),
    )


def is_job_title(text: str) -> bool:
    """True when ``text`` is a short line recognized as a job title (used by team-card parsing)."""
    t = collapse_ws(text or "")
    if not t or len(t) > 90 or len(t.split()) > 12:
        return False
    if t.endswith((".", "!", "?")) and len(t.split()) > 6:
        return False  # a sentence, not a title
    norm = normalize_key(t)
    return bool(norm) and _match(norm) is not None


def is_top_decision_maker(info: TitleInfo) -> bool:
    return (
        info.normalized_title in TOP_TITLES
        or info.seniority == S.owner
        or (info.seniority == S.c_level and info.decision_power >= 90)
    )


_SPLIT_REQ_RE = re.compile(r"\s*(?:/|,|\||;|&|\+|\bor\b|\bou\b|\boder\b|\bo\b)\s*", re.IGNORECASE)


def title_match_score(info: TitleInfo, *, titles: list[str], role_families: list[str], seniorities: list[str]) -> float:
    """0–1 fit between a person's title and the requested titles / role families / seniorities.

    "Founder/CEO/Owner" accepts Gérant, Président, PDG, Fondateur, Managing Director, Co-founder…
    and rejects e.g. "Community manager". Returns 1.0 when nothing is requested.
    """
    if not titles and not role_families and not seniorities:
        return 1.0
    best = 0.0
    cand_rank = _SENIORITY_RANK.get(info.seniority, 0)
    cand_top = is_top_decision_maker(info)
    for raw in titles or []:
        for part in _SPLIT_REQ_RE.split(raw or ""):
            part = part.strip()
            if not part:
                continue
            req = normalize_title(part)
            if req.normalized_title.lower() == info.normalized_title.lower() and req.role_family != F.other:
                best = max(best, 1.0)
                continue
            if is_top_decision_maker(req):
                if cand_top:
                    best = max(best, 0.95)
                continue  # a top-role request is never satisfied by a non-top title
            if req.role_family != F.other and req.role_family == info.role_family:
                diff = _SENIORITY_RANK.get(req.seniority, 0) - cand_rank
                best = max(best, 0.85 if diff <= 0 else 0.65 if diff == 1 else 0.3)
            ratio = fuzz.token_set_ratio(normalize_key(part), normalize_key(info.original or info.normalized_title))
            if ratio >= 90:
                best = max(best, 0.8)
    fam_ok = bool(role_families) and info.role_family.value in {f.lower() for f in role_families}
    sen_ok = bool(seniorities) and info.seniority.value in {s.lower() for s in seniorities}
    if fam_ok and sen_ok:
        best = max(best, 0.9)
    elif fam_ok and not seniorities:
        best = max(best, 0.75)
    elif sen_ok and not role_families:
        best = max(best, 0.75)
    elif fam_ok or sen_ok:
        best = max(best, 0.5)
    return round(best, 3)
