"""Relevant-chunk retrieval for AI resolvers (PIPELINE §6.2): cached pages → ~900-char passages →
BM25-style scoring against concept + keyword expansions + page-type priors → top passages."""

from __future__ import annotations

import itertools
import math
import re
import uuid
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from scout.enrich.matching import contains_any, dedupe, fold, tokens

DEFAULT_PAGE_PRIORS: dict[str, float] = {
    "services": 1.4,
    "solutions": 1.35,
    "home": 1.2,
    "about": 1.1,
    "case_studies": 1.1,
    "pricing": 1.0,
    "team": 0.9,
    "other": 0.9,
    "contact": 0.8,
    "careers": 0.8,
    "blog": 0.75,
    "news": 0.75,
    "legal": 0.4,
}

STOPWORDS: frozenset[str] = frozenset(
    """
    a an the and or of to in on for with by at from as into about over than then so not no yes
    their they them its it is are be been was were do does did has have had having can could will would
    whether if that this these those which who whom what when where how why any all some each every
    our your we you us me my i he she his her
    add column columns showing show shows find get list give tell want please new value field
    actually really truly indeed currently also only just still mainly mostly
    offer offers offering offered provide provides providing provided sell sells selling sold
    specialize specializes specialized specialise specialises specialised specializing specialising
    agency agencies company companies business businesses firm site website websites webpage page pages homepage
    mention mentions mentioned mentioning mentionne
    le la les l un une des du de d et ou en au aux pour par sur avec dans leur leurs est sont si que qui quoi
    ce cet cette ces son sa ses il ils elle elles nous vous ne pas plus tres vraiment reellement aussi
    propose proposent vend vendent offre offrent agence agences entreprise entreprises societe site
    ajoute ajouter colonne trouve trouver indique indiquant affiche est-ce
    """.split()
)

# Strict equivalents (same concept, other wording / language). Used for ranking AND the no-AI heuristic.
EQUIVALENTS: dict[str, list[str]] = {
    "instagram": ["insta"],
    "insta": ["instagram"],
    "tiktok": ["tik tok"],
    "facebook": ["fb"],
    "social media": ["reseaux sociaux", "social media management", "gestion des reseaux sociaux"],
    "reseaux sociaux": ["social media", "gestion des reseaux sociaux"],
    "community management": ["community manager", "gestion de communaute", "animation de communaute"],
    "ecommerce": ["e-commerce", "boutique en ligne", "online store", "vente en ligne"],
    "e commerce": ["ecommerce", "boutique en ligne", "online store", "vente en ligne"],
    "seo": ["referencement naturel", "search engine optimization", "search engine optimisation"],
    "referencement": ["seo", "search engine optimization"],
    "sea": ["referencement payant", "paid search", "google ads"],
    "google ads": ["adwords", "sea"],
    "meta ads": ["facebook ads", "instagram ads"],
    "ads": ["advertising", "publicite"],
    "advertising": ["publicite", "ads"],
    "publicite": ["advertising", "ads"],
    "chatbot": ["chat bot", "agent conversationnel", "bot conversationnel"],
    "manychat": ["many chat"],
    "automation": ["automatisation"],
    "automatisation": ["automation"],
    "email marketing": ["emailing", "e-mailing", "marketing par email"],
    "emailing": ["email marketing", "e-mailing"],
    "pricing": ["tarifs", "tarif", "prix", "price", "prices"],
    "price": ["pricing", "tarifs", "prix"],
    "tarifs": ["pricing", "prix", "tarif"],
    "prix": ["pricing", "price", "tarifs"],
    "hiring": ["recrutement", "recrute", "we are hiring", "nous recrutons"],
    "recrutement": ["hiring", "recrute", "nous recrutons"],
    "careers": ["carrieres", "jobs", "recrutement"],
    "web design": ["webdesign", "conception de sites", "creation de site", "creation de sites", "website design"],
    "branding": ["identite visuelle", "brand identity", "image de marque"],
    "influencer": ["influenceurs", "influence", "influencer marketing", "marketing d influence"],
    "influence": ["influencer marketing", "marketing d influence", "influenceurs"],
    "content": ["contenu"],
    "contenu": ["content"],
    "content creation": ["creation de contenu", "creation de contenus"],
    "video": ["videos", "production video"],
    "consulting": ["conseil"],
    "conseil": ["consulting"],
    "training": ["formation", "formations"],
    "formation": ["training"],
    "ai": ["artificial intelligence", "intelligence artificielle", "ia"],
    "ia": ["ai", "intelligence artificielle", "artificial intelligence"],
    "lead generation": ["generation de leads", "generation de prospects"],
    "testimonials": ["temoignages", "avis clients", "customer reviews"],
    "temoignages": ["testimonials", "avis clients"],
    "podcast": ["podcasts"],
    "app": ["application", "mobile app", "application mobile"],
}

# Broader related vocabulary (recall only): used for ranking passages sent to the AI, never as evidence.
RELATED: dict[str, list[str]] = {
    "instagram": ["reels", "stories", "reseaux sociaux", "social media", "community management", "instagram ads"],
    "tiktok": ["short videos", "videos courtes", "reseaux sociaux", "social media", "ugc"],
    "social media": ["community management", "social ads", "instagram", "facebook", "linkedin", "tiktok"],
    "reseaux sociaux": ["community management", "instagram", "facebook", "linkedin", "tiktok"],
    "linkedin": ["linkedin ads", "social selling", "reseaux sociaux"],
    "facebook": ["meta", "facebook ads", "meta ads"],
    "ecommerce": ["shopify", "woocommerce", "prestashop", "magento", "marketplace"],
    "e commerce": ["shopify", "woocommerce", "prestashop", "magento", "marketplace"],
    "seo": ["referencement", "netlinking", "google", "mots cles", "keywords"],
    "sea": ["google ads", "campagnes", "ppc"],
    "ads": ["sea", "google ads", "meta ads", "facebook ads", "paid media", "campagnes", "campaigns"],
    "chatbot": ["manychat", "automation", "dm", "messenger", "conversational", "whatsapp"],
    "manychat": ["chatbot", "dm automation", "instagram automation", "messenger", "automation", "dm"],
    "automation": ["workflow", "zapier", "make", "crm"],
    "email marketing": ["newsletter", "klaviyo", "mailchimp", "brevo", "sendinblue"],
    "pricing": ["plans", "packages", "forfaits", "offres", "devis", "per month", "par mois", "eur", "starting at"],
    "tarifs": ["forfaits", "devis", "par mois", "a partir de", "plans"],
    "hiring": ["careers", "jobs", "join us", "join the team", "nous rejoindre", "offres d emploi", "job openings"],
    "recrutement": ["careers", "jobs", "nous rejoindre", "offres d emploi", "candidature"],
    "web design": ["ux", "ui", "wordpress", "webflow", "site internet", "site vitrine"],
    "branding": ["logo", "charte graphique", "brand"],
    "influencer": ["creators", "createurs", "ugc", "collaborations"],
    "content": ["copywriting", "redaction", "articles", "blog"],
    "crm": ["hubspot", "salesforce", "pipedrive", "gestion de la relation client"],
    "saas": ["software", "logiciel", "platform", "plateforme"],
    "b2b": ["business to business", "entreprises", "professionnels"],
    "b2c": ["consumers", "particuliers", "grand public"],
    "target": ["clients", "customers", "we help", "we work with", "nous accompagnons", "travaillons avec", "secteurs",
               "industries", "marques", "brands", "pme", "startups"],
    "customer": ["clients", "customers", "we help", "we work with", "nous accompagnons", "travaillons avec",
                 "secteurs", "industries", "pour qui", "marques", "brands", "pme", "startups"],
    "cible": ["clients", "nous accompagnons", "travaillons avec", "secteurs", "marques", "pme", "startups"],
    "client": ["clients", "nous accompagnons", "travaillons avec", "secteurs", "marques", "pme", "startups"],
    "niche": ["specialistes", "specialists", "secteurs", "industries", "dedicated to", "dedie"],
    "podcast": ["episode", "interview", "spotify", "apple podcasts"],
}

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?;])\s+")


@dataclass
class Passage:
    page_id: uuid.UUID | None
    page_url: str
    page_type: str
    text: str
    idx: int


def _page_type(page: Any) -> str:
    pt = getattr(page, "page_type", None) or "other"
    return str(getattr(pt, "value", pt))


def _split_long(line: str, max_chars: int) -> list[str]:
    """Split an over-long line on sentence boundaries, then hard-wrap at whitespace."""
    out: list[str] = []
    buf = ""
    for sent in _SENTENCE_SPLIT.split(line):
        while len(sent) > max_chars:
            cut = sent.rfind(" ", 0, max_chars)
            cut = cut if cut > max_chars // 2 else max_chars
            if buf:
                out.append(buf)
                buf = ""
            out.append(sent[:cut].strip())
            sent = sent[cut:].strip()
        if buf and len(buf) + 1 + len(sent) > max_chars:
            out.append(buf)
            buf = sent
        else:
            buf = f"{buf} {sent}".strip()
    if buf:
        out.append(buf)
    return [s for s in out if s]


def split_passages(page: Any, *, max_chars: int = 900, min_chars: int = 240) -> list[Passage]:
    """Split a cached page into passages on paragraph/line boundaries, merging short lines.

    A header passage (title + meta description) comes first when present, so page metadata can be cited.
    """
    url = getattr(page, "url", "") or ""
    ptype = _page_type(page)
    pid = getattr(page, "id", None)
    out: list[Passage] = []

    def emit(text: str) -> None:
        text = text.strip()
        if text:
            out.append(Passage(page_id=pid, page_url=url, page_type=ptype, text=text, idx=len(out)))

    title = (getattr(page, "title", None) or "").strip()
    meta = (getattr(page, "meta_description", None) or "").strip()
    if meta:
        emit("\n".join(x for x in (title, meta) if x)[:max_chars])

    paragraphs: list[list[str]] = [[]]
    for raw in (getattr(page, "content_text", None) or "").splitlines():
        line = raw.strip()
        if not line:
            if paragraphs[-1]:
                paragraphs.append([])
            continue
        paragraphs[-1].extend(_split_long(line, max_chars) if len(line) > max_chars else [line])

    buf: list[str] = []
    size = 0
    for para in paragraphs:
        for unit in para:
            if buf and size + len(unit) + 1 > max_chars:
                emit("\n".join(buf))
                buf, size = [], 0
            buf.append(unit)
            size += len(unit) + 1
        if size >= min_chars:
            emit("\n".join(buf))
            buf, size = [], 0
    if buf:
        emit("\n".join(buf))
    return out


def _key_present(key: str, haystack_tokens: list[str], haystack: str) -> bool:
    if " " in key:
        return f" {key} " in f" {haystack} "
    return key in haystack_tokens


def expand_terms(concept: str | None, keywords: Sequence[str], *, strict: bool = False) -> list[str]:
    """Search terms for a concept: keyword phrases, concept tokens (stopwords dropped), multi-word
    concept phrases and EN/FR synonyms. `strict=True` limits synonyms to exact equivalents."""
    concept = concept or ""
    base = " ".join([concept, *keywords])
    folded_tokens = tokens(base)
    haystack = " ".join(folded_tokens)
    out: list[str] = [k for k in keywords if k and k.strip()]
    content = [t for t in tokens(concept) if t not in STOPWORDS and len(t) > 1]
    out.extend(content)
    # Adjacent content-word pairs from the concept ("instagram management", "google ads").
    ctoks = tokens(concept)
    for a, b in itertools.pairwise(ctoks):
        if a not in STOPWORDS and b not in STOPWORDS:
            out.append(f"{a} {b}")
    tables = [EQUIVALENTS] if strict else [EQUIVALENTS, RELATED]
    for table in tables:
        for key, syns in table.items():
            if _key_present(key, folded_tokens, haystack):
                out.extend(syns)
    return dedupe(out)[:48]


def _stem(tok: str) -> str:
    """Very light EN/FR stemmer (consistent on both query and document side)."""
    if len(tok) <= 3 or tok.isdigit():
        return tok
    for suf, rep in (
        ("eaux", "eau"), ("aux", "al"), ("ies", "y"), ("ements", "e"), ("ement", "e"), ("ments", ""),
        ("ment", ""), ("ations", "at"), ("ation", "at"), ("ings", ""), ("ing", ""), ("euses", "eu"),
        ("euse", "eu"), ("eurs", "eu"), ("eur", "eu"), ("es", ""), ("s", ""),
    ):
        if tok.endswith(suf) and len(tok) - len(suf) >= 3:
            if suf == "s" and tok.endswith(("ss", "us", "is")):
                break
            tok = tok[: -len(suf)] + rep
            break
    if len(tok) > 4 and tok.endswith("e"):
        tok = tok[:-1]
    return tok


def _stems(text: str) -> list[str]:
    return [_stem(t) for t in tokens(text)]


def page_priors_for(concept: str | None, input_sources: Iterable[str] = ()) -> dict[str, float]:
    """Default priors, boosted for the plan's preferred page types and for pricing questions."""
    priors = dict(DEFAULT_PAGE_PRIORS)
    for src in input_sources:
        key = str(getattr(src, "value", src))
        priors[key] = priors.get(key, 1.0) * 1.25
    if concept and contains_any(concept, ["pricing", "price", "prices", "tarif", "tarifs", "prix", "cost", "forfait",
                                          "plans", "cheapest", "per month", "par mois", "subscription", "abonnement",
                                          "euros", "how much", "combien"]):
        priors["pricing"] = 1.9
    return priors


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def rank_passages(
    passages: Sequence[Passage],
    terms: Sequence[str],
    *,
    page_priors: dict[str, float] | None = None,
    k: int = 6,
    max_chars: int = 6000,
    max_per_page: int = 3,
    include_unmatched: bool = False,
) -> list[Passage]:
    """BM25-style ranking with accent/case folding, light stemming, phrase bonus and page-type priors.

    Returns up to `k` diverse passages (near-duplicates such as repeated footers are dropped, at most
    `max_per_page` per page) totalling ≤ `max_chars`. With `include_unmatched`, remaining slots are
    filled with unmatched passages from the highest-prior pages (useful for summaries).
    """
    if not passages:
        return []
    priors = page_priors or DEFAULT_PAGE_PRIORS
    doc_stems = [_stems(p.text) for p in passages]
    n = len(passages)
    avgdl = sum(len(d) for d in doc_stems) / n or 1.0

    q_terms: list[str] = []
    phrases: list[str] = []
    for term in terms:
        st = [s for s in _stems(term) if s not in STOPWORDS]
        q_terms.extend(st)
        if len(tokens(term)) > 1:
            phrases.append(term)
    q_set = list(dict.fromkeys(q_terms))
    df: Counter[str] = Counter()
    doc_sets = [set(d) for d in doc_stems]
    for ds in doc_sets:
        for t in q_set:
            if t in ds:
                df[t] += 1
    idf = {t: math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5)) for t in q_set}
    k1, b = 1.2, 0.75

    scored: list[tuple[float, int]] = []
    for i, (p, d) in enumerate(zip(passages, doc_stems, strict=True)):
        tf = Counter(d)
        dl = len(d) or 1
        s = 0.0
        for t in q_set:
            f = tf.get(t, 0)
            if f:
                s += idf[t] * f * (k1 + 1) / (f + k1 * (1 - b + b * dl / avgdl))
        if s > 0 and phrases:
            folded = fold(p.text)
            for ph in phrases:
                if contains_any(folded, [ph]):
                    s += 1.5
        scored.append((s * priors.get(p.page_type, 1.0), i))

    scored.sort(key=lambda x: (-x[0], x[1]))
    selected: list[Passage] = []
    sigs: list[set[str]] = []
    per_page: Counter[str] = Counter()
    total = 0

    def try_add(p: Passage, sig: set[str]) -> bool:
        nonlocal total
        if per_page[p.page_url] >= max_per_page or total + len(p.text) > max_chars:
            return False
        if any(_jaccard(sig, s) >= 0.8 for s in sigs):
            return False
        selected.append(p)
        sigs.append(sig)
        per_page[p.page_url] += 1
        total += len(p.text)
        return True

    for score, i in scored:
        if len(selected) >= k:
            break
        if score <= 0:
            break
        try_add(passages[i], doc_sets[i])
    if include_unmatched and len(selected) < k:
        chosen = {id(p) for p in selected}
        rest = sorted(
            (i for i in range(n) if id(passages[i]) not in chosen),
            key=lambda i: (-priors.get(passages[i].page_type, 1.0), passages[i].idx, i),
        )
        for i in rest:
            if len(selected) >= k:
                break
            try_add(passages[i], doc_sets[i])
    return selected


def passages_for_pages(pages: Iterable[Any], *, max_chars: int = 900) -> list[Passage]:
    out: list[Passage] = []
    for page in pages:
        out.extend(split_passages(page, max_chars=max_chars))
    return out
