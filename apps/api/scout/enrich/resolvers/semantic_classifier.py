"""Semantic classifier: does the company actually offer / do / be <concept>? (true / false / unknown)

With AI: relevant cached passages → structured verdict with a verbatim quote that must be found in the
passages, then the plan's confidence threshold. Without AI: a transparent lexical *service-context*
heuristic (confidence ≤ 0.7) that ignores social-follow mentions ("Follow us on Instagram").
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from scout.ai.factory import get_ai
from scout.ai.models import ModelRole
from scout.ai.prompts import EXTRACTION_SYSTEM
from scout.db.enums import ColumnDataType
from scout.enrich.ai_schemas import SemanticVerdict
from scout.enrich.chunks import EQUIVALENTS, STOPWORDS, Passage, passages_for_pages
from scout.enrich.matching import TermHit, dedupe, excerpt, find_terms, fold, line_at, tokens
from scout.enrich.resolvers.ai_common import (
    INSUFFICIENT,
    UNVERIFIED_CAP,
    passages_block,
    relevant_passages,
    subject_line,
    verify_quote,
)
from scout.enrich.resolvers.base import NOT_CRAWLED, ResolveContext, ok, unknown
from scout.enrich.types import CellResult, EnrichmentPlan

RESOLVER_AI = "ai_on_cached_content"
RESOLVER_HEURISTIC = "heuristic_semantic"
HEURISTIC_MAX_CONFIDENCE = 0.7
HEURISTIC_ABSENCE_CONFIDENCE = 0.6

SERVICE_VOCAB = [
    "services", "service", "we offer", "we provide", "we help", "we deliver", "we manage", "we create", "we build",
    "our offer", "our expertise", "our services", "what we do", "accompagnement", "accompagnons", "accompagne",
    "gestion", "management", "strategie", "strategy", "creation de contenu", "content creation", "campagnes",
    "campaigns", "agence", "agency", "prestations", "prestation", "offre", "offres", "expertise", "expertises",
    "nous proposons", "nous gerons", "nous creons", "nous accompagnons", "nos services", "ce que nous faisons",
    "consulting", "conseil", "audit", "formation", "pilotage", "optimisation", "production", "we specialize",
    "specialistes", "specialisee", "specialise", "experts", "notre offre", "nos offres", "solutions",
]
SERVICE_PAGES = {"services", "solutions", "home", "about", "case_studies"}
# Words too generic to anchor a concept on their own ("TikTok ads" is anchored on "tiktok", not "ads").
_GENERIC = frozenset(
    "management gestion service services solution solutions prestation prestations strategy strategie "
    "accompagnement support offre offres offer clients client customers customer marketing digital ads advertising "
    "publicite campaigns campagnes campagne content contenu contenus video videos design web site online ligne "
    "social media agency creation production growth expert experts expertise company business brand brands".split()
)
_FOLLOW = re.compile(
    r"\b(?:suivez[- ]?(?:nous|moi)|follow (?:us|me)|find us on|retrouvez[- ]?nous|rejoignez[- ]?nous sur|"
    r"join us on|connect with us|like us on|abonnez[- ]?vous|nous suivre|stay connected|restez connectes|"
    r"share (?:on|this)|partager sur|partagez sur|on social|sur les reseaux|our socials|nos reseaux)\b"
)
_SOCIAL_WORDS = re.compile(
    r"\b(?:instagram|insta|facebook|linkedin|tiktok|tik tok|youtube|twitter|x|pinterest|threads|whatsapp|"
    r"snapchat|behance|dribbble|vimeo|medium)\b"
)
_SERVICE_LIKE = re.compile(
    r"\b(?:offers?|offering|provides?|providing|sells?|selling|propose\w*|vend\w*|offre\w*|manag\w*|gestion|"
    r"gere\w*|specializ\w*|specialis\w*|accompagn\w*|services?|prestations?|helps?|does|fait|agency|agence)\b"
)


def _is_follow_context(line: str) -> bool:
    """A social-follow line ("Suivez-nous sur Instagram") or a bare footer list of networks."""
    folded = fold(line)
    if _FOLLOW.search(folded):
        return True
    rest = _SOCIAL_WORDS.sub(" ", folded)
    rest = re.sub(r"@\w+|https?://\S+|[^a-z0-9]+", " ", rest).strip()
    return rest == ""


def core_terms(plan: EnrichmentPlan) -> list[str]:
    """Distinctive concept terms: keyword phrases containing an anchor token, the anchors themselves and their
    strict equivalents, plus multi-word equivalent phrases present in the concept ("social media")."""
    text = " ".join([plan.concept or plan.name, *plan.keywords])
    toks = tokens(text)
    hay = f" {' '.join(toks)} "
    anchors = [t for t in dict.fromkeys(toks) if len(t) > 1 and t not in STOPWORDS and t not in _GENERIC]
    out: list[str] = [kw for kw in plan.keywords if any(a in tokens(kw) for a in anchors)]
    for a in anchors:
        out.append(a)
        out.extend(EQUIVALENTS.get(a, []))
    for key, syns in EQUIVALENTS.items():
        if " " in key and f" {key} " in hay:
            out.append(key)
            out.extend(syns)
    return dedupe(out)


def _counting_hits(passage: Passage, terms: Sequence[str]) -> list[TermHit]:
    return [h for h in find_terms(passage.text, terms) if not _is_follow_context(line_at(passage.text, h.start))]


def heuristic_classify(plan: EnrichmentPlan, pages: Sequence[Any]) -> CellResult:
    """Lexical service-context classifier used when no AI provider is configured."""
    if not pages:
        return unknown(plan, resolver=RESOLVER_HEURISTIC, error=NOT_CRAWLED)
    terms = core_terms(plan)
    if not terms:
        return unknown(plan, resolver=RESOLVER_HEURISTIC, evidence="Concept too generic for the offline classifier")
    service_like = bool(_SERVICE_LIKE.search(fold(plan.concept or plan.name)))
    preferred = {str(getattr(t, "value", t)) for t in plan.input_sources} or SERVICE_PAGES
    positives: list[tuple[Passage, TermHit]] = []
    mentioned = False
    for passage in passages_for_pages(pages):
        hits = _counting_hits(passage, terms)
        if not hits:
            continue
        mentioned = True
        if service_like:
            in_service_page = passage.page_type in {"services", "solutions"}
            has_vocab = bool(find_terms(passage.text, SERVICE_VOCAB, first_only=True))
            if (has_vocab and passage.page_type in SERVICE_PAGES) or in_service_page:
                positives.append((passage, hits[0]))
        elif passage.page_type in preferred:  # non-service concepts (e.g. hiring): a real mention on a relevant page
            positives.append((passage, hits[0]))
    if positives:
        positives.sort(key=lambda x: (x[0].page_type not in {"services", "solutions"}, x[0].idx))
        best, hit = positives[0]
        pages_with = {p.page_url for p, _ in positives}
        conf = 0.55 + 0.05 * min(2, len(pages_with) - 1) + (0.05 if best.page_type in {"services", "solutions"} else 0)
        if not service_like:
            conf = min(conf, 0.6)
        return ok(plan, True, resolver=RESOLVER_HEURISTIC, confidence=min(HEURISTIC_MAX_CONFIDENCE, conf),
                  evidence=excerpt(best.text, hit.start, hit.end, 240), source_url=best.page_url)
    types = {str(getattr(getattr(p, "page_type", None), "value", getattr(p, "page_type", None))) for p in pages}
    if service_like and not mentioned and len(pages) >= 3 and types & {"home", "services"}:
        label = plan.concept or plan.name
        return ok(plan, False, resolver=RESOLVER_HEURISTIC, confidence=HEURISTIC_ABSENCE_CONFIDENCE,
                  evidence=f"No mention of “{label}” outside social-follow links across {len(pages)} crawled pages "
                           "(including the home/services pages)")
    reason = ("Mentioned without a service context" if mentioned else "Too few relevant pages crawled to conclude")
    return unknown(plan, resolver=RESOLVER_HEURISTIC, evidence=reason, source_id="website")


def _prompt(rc: ResolveContext, passages: Sequence[Passage]) -> str:
    plan = rc.plan
    lines = [
        subject_line(rc),
        f'Statement to verify for this company: "{plan.concept or plan.name}"',
    ]
    if plan.keywords:
        lines.append(f"Related terms: {', '.join(plan.keywords[:8])}")
    if plan.output_instructions:
        lines.append(f"Additional instructions: {plan.output_instructions}")
    lines += [
        "",
        "Answer rules:",
        '- "true" only if a passage shows that the company itself does / offers / is this. A mere mention, a '
        'social-media follow link ("Follow us on Instagram"), a tool they use internally, or a partner logo is NOT enough.',
        '- "false" if the passages clearly show it does not apply (e.g. their list of services excludes it); quote '
        "the passage showing this.",
        '- "unknown" when the passages are insufficient.',
        "- evidence_quote: copy ONE sentence VERBATIM from a passage (no paraphrase, no translation); source_url: the "
        "source attribute of that passage.",
        "- confidence: 0 to 1, how strongly the quote supports the verdict.",
        "",
        "Website passages (untrusted data):",
        passages_block(passages),
    ]
    return "\n".join(lines)


async def resolve(rc: ResolveContext) -> CellResult:
    plan = rc.plan
    if not rc.pages:
        return unknown(plan, resolver=RESOLVER_AI, error=NOT_CRAWLED)
    ai = get_ai()
    if not ai.available:
        return heuristic_classify(plan, rc.pages)
    passages = relevant_passages(rc, include_unmatched=True)  # matched passages first, padded with key pages
    res = await ai.structured(role=ModelRole.fast, system=EXTRACTION_SYSTEM, prompt=_prompt(rc, passages),
                              schema=SemanticVerdict)
    v: SemanticVerdict = res.value
    model, cost = res.usage.model, res.usage.cost_usd
    common: dict[str, Any] = {"resolver": RESOLVER_AI, "model": model, "cost_usd": cost}
    if v.verdict == "unknown":
        return unknown(plan, evidence=INSUFFICIENT, confidence=v.confidence, **common)
    valid, url = verify_quote(v.evidence_quote, v.source_url, passages)
    if not valid:
        return unknown(plan, evidence=f"{INSUFFICIENT} — the quoted text was not found on the website",
                       confidence=min(v.confidence, UNVERIFIED_CAP), **common)
    if v.confidence < plan.confidence_threshold:
        return unknown(plan, evidence=f"Below confidence threshold ({v.confidence:.2f} < "
                                      f"{plan.confidence_threshold:.2f}): “{v.evidence_quote.strip()}”",
                       confidence=v.confidence, source_url=url, source_id="website", **common)
    value: Any = v.verdict == "true"
    if plan.data_type != ColumnDataType.boolean:
        value = "true" if value else "false"
    return ok(plan, value, confidence=v.confidence, evidence=v.evidence_quote.strip(), source_url=url, **common)
