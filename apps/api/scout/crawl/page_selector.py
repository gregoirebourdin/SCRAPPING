"""Page classification (multilingual URL/anchor patterns), sitemap parsing and page selection.

Selection is deliberately small (budget 5–12 pages): the pages that carry company facts and
decision makers (team, about, legal notice, contact, services…), never a whole-domain crawl.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

from lxml import etree

from scout.config import get_settings
from scout.db.enums import PageType
from scout.util.text import normalize_key
from scout.util.urls import canonical_url, registrable_domain

MAX_PAGES_CAP = 12

# Exact (normalized, hyphenated) path segments → page type.
_SEGMENTS: dict[PageType, tuple[str, ...]] = {
    PageType.legal: (
        "mentions-legales", "mention-legales", "mentions-legale", "mentions", "legal", "legal-notice", "legal-notices",
        "legal-mentions", "legals", "notice-legale", "informations-legales", "imprint", "impressum", "aviso-legal",
        "avisolegal", "note-legali", "colofon", "disclaimer", "cgv", "cgu", "cgvu", "conditions-generales",
        "conditions-generales-de-vente", "conditions-generales-d-utilisation", "terms", "terms-of-service",
        "terms-and-conditions", "terms-conditions", "agb",
    ),
    PageType.team: (
        "team", "our-team", "the-team", "meet-the-team", "meet-our-team", "equipe", "l-equipe", "notre-equipe",
        "nos-equipes", "equipes", "lequipe", "leadership", "people", "our-people", "management", "founders",
        "fondateurs", "fondatrices", "team-members", "unser-team", "das-team", "equipo", "nuestro-equipo",
        "il-team", "squadra", "ons-team", "het-team", "medewerkers", "staff", "who-we-are-team", "dirigeants",
        "direction", "associes", "nos-associes", "partners-team", "collaborateurs", "nos-collaborateurs",
    ),
    PageType.about: (
        "about", "about-us", "aboutus", "qui-sommes-nous", "quisommesnous", "qui-somme-nous", "a-propos", "apropos",
        "a-propos-de-nous", "notre-histoire", "histoire", "notre-agence", "l-agence", "agence", "lagence",
        "ueber-uns", "uber-uns", "ueberuns", "unternehmen", "quienes-somos", "sobre-nosotros", "nosotros",
        "chi-siamo", "chisiamo", "over-ons", "overons", "company", "our-story", "who-we-are", "la-societe",
        "notre-societe", "societe", "presentation", "le-studio", "notre-studio", "le-cabinet", "notre-cabinet",
        "cabinet", "nous", "manifesto", "nos-valeurs", "valeurs", "mission", "our-company", "story",
    ),
    PageType.contact: (
        "contact", "contact-us", "contactus", "contactez-nous", "contactez-moi", "nous-contacter", "contacts",
        "kontakt", "contacto", "contactanos", "contatti", "contattaci", "get-in-touch", "devis", "demande-de-devis",
        "nous-trouver", "acces", "plan-d-acces", "contact-et-acces",
    ),
    PageType.pricing: (
        "pricing", "prices", "tarifs", "tarif", "prix", "plans", "preise", "precios", "prezzi", "prijzen",
        "nos-tarifs", "pricing-plans", "abonnements", "offres-et-tarifs", "tarification",
    ),
    PageType.careers: (
        "careers", "career", "jobs", "job", "recrutement", "recrute", "on-recrute", "nous-recrutons", "carrieres",
        "carriere", "rejoignez-nous", "rejoindre", "nous-rejoindre", "join-us", "join", "join-the-team",
        "karriere", "stellenangebote", "stellen", "empleo", "trabaja-con-nosotros", "lavora-con-noi",
        "werken-bij", "vacatures", "offres-d-emploi", "emploi", "emplois", "hiring",
    ),
    PageType.services: (
        "services", "service", "nos-services", "our-services", "prestations", "nos-prestations", "expertises",
        "expertise", "nos-expertises", "offres", "offre", "nos-offres", "what-we-do", "savoir-faire", "metiers",
        "nos-metiers", "leistungen", "servicios", "servizi", "diensten", "accompagnement", "competences",
    ),
    PageType.solutions: (
        "solutions", "solution", "nos-solutions", "our-solutions", "loesungen", "losungen", "soluciones",
        "soluzioni", "oplossingen", "produits", "products", "produkte", "productos", "prodotti",
    ),
    PageType.case_studies: (
        "case-studies", "case-study", "casestudies", "cas-clients", "cas-client", "etudes-de-cas", "references",
        "nos-references", "realisations", "nos-realisations", "realisation", "portfolio", "projets", "nos-projets",
        "projects", "our-work", "work", "travaux", "nos-travaux", "clients", "nos-clients", "referenzen",
        "projekte", "casos-de-exito", "progetti", "cases", "showcase", "success-stories",
    ),
    PageType.blog: ("blog", "blogs", "articles", "journal", "magazine", "le-blog", "notre-blog", "insights", "conseils"),
    PageType.news: (
        "actualites", "actualite", "actus", "actu", "news", "presse", "press", "newsroom", "communiques",
        "communiques-de-presse", "aktuelles", "neuigkeiten", "noticias", "notizie", "nieuws", "media", "medias",
    ),
}
# Keywords that classify a segment even when embedded ("notre-equipe-creative", "contact-paris").
_TOKENS: tuple[tuple[PageType, tuple[str, ...]], ...] = (
    (PageType.legal, ("mentions-legales", "impressum", "imprint", "legal-notice", "aviso-legal")),
    (PageType.team, ("equipe", "team", "fondateurs", "leadership")),
    (PageType.careers, ("recrutement", "careers", "carriere", "rejoignez", "karriere", "jobs")),
    (PageType.contact, ("contact", "kontakt", "contatti")),
    (PageType.about, ("qui-sommes", "a-propos", "about", "ueber-uns", "chi-siamo", "quienes-somos", "over-ons")),
    (PageType.pricing, ("tarifs", "pricing")),
    (PageType.case_studies, ("realisations", "case-stud", "references", "portfolio")),
    (PageType.services, ("prestations", "services", "expertises")),
)
_SEGMENT_LOOKUP: dict[str, PageType] = {seg: pt for pt, segs in _SEGMENTS.items() for seg in segs}
# "privacy" pages are explicitly not legal notices.
_OTHER_SEGMENTS = frozenset(
    {"privacy", "privacy-policy", "politique-de-confidentialite", "confidentialite", "cookies",
     "politique-cookies", "datenschutz", "gdpr", "rgpd", "donnees-personnelles", "sitemap", "plan-du-site",
     "plan-du-site-web", "search", "recherche"}
)

_LANG_PREFIX_RE = re.compile(r"^/(fr|en|de|es|it|nl|pt)(?:[-_][a-z]{2})?(?=/|$)", re.IGNORECASE)
_ASSET_EXT_RE = re.compile(
    r"\.(?:pdf|jpe?g|png|gif|svg|webp|avif|ico|bmp|tiff?|zip|rar|7z|gz|tar|mp4|webm|mov|avi|mp3|wav|ogg|docx?|xlsx?|"
    r"pptx?|odt|ods|csv|css|js|mjs|json|xml|rss|atom|txt|woff2?|ttf|eot|otf|exe|dmg|apk)$",
    re.IGNORECASE,
)
_SKIP_PATH_RE = re.compile(
    r"(?:^|/)(?:feed|rss|atom|wp-json|wp-admin|wp-login\.php|wp-content|wp-includes|xmlrpc\.php|cdn-cgi|tag|tags|"
    r"category|categorie|categories|author|auteur|page/\d+|amp|cart|panier|checkout|commande|mon-compte|"
    r"my-account|account|login|signin|sign-in|register|inscription|connexion|logout|search|recherche|"
    r"comments|trackback|embed|print|attachment|wishlist|compare)(?:/|$)",
    re.IGNORECASE,
)
_PAGINATION_QS_RE = re.compile(r"(?:^|&)(?:page|p|paged|pg|start|offset|s|q|replytocom|share|lang|add-to-cart)=", re.IGNORECASE)

# Selection priorities: (page type, how many to take).
_PRIORITY: tuple[tuple[PageType, int], ...] = (
    (PageType.team, 2),
    (PageType.about, 2),
    (PageType.legal, 1),
    (PageType.contact, 1),
    (PageType.services, 1),
    (PageType.solutions, 1),
    (PageType.case_studies, 1),
    (PageType.careers, 1),
    (PageType.pricing, 1),
    (PageType.news, 1),
    (PageType.blog, 1),
)


def _slug(s: str) -> str:
    return normalize_key(s).replace(" ", "-")


def _segments(url: str) -> list[str]:
    try:
        path = urlsplit(url).path or "/"
    except ValueError:
        return []
    path = _LANG_PREFIX_RE.sub("", path)
    segs = []
    for raw in path.split("/"):
        if not raw:
            continue
        raw = re.sub(r"\.(?:html?|php|aspx?|jsp|cfm)$", "", raw, flags=re.IGNORECASE)
        s = _slug(raw.replace("_", "-"))
        if s:
            segs.append(s)
    return segs


def _classify_slug(slug: str, *, allow_tokens: bool) -> PageType | None:
    if not slug:
        return None
    if slug in _OTHER_SEGMENTS:
        return PageType.other
    pt = _SEGMENT_LOOKUP.get(slug)
    if pt is not None:
        return pt
    if slug.startswith(("index", "accueil", "home")) and slug in ("index", "accueil", "home", "homepage"):
        return PageType.home
    if allow_tokens:
        for ptype, toks in _TOKENS:
            if any(t in slug for t in toks):
                return ptype
    return None


def classify_page_type(url: str, title: str | None = None, anchor: str | None = None) -> PageType:
    """Classify a page from its URL path, then anchor text, then title (EN/FR/DE/ES/IT/NL)."""
    segs = _segments(url)
    if not segs or segs in (["index"], ["accueil"], ["home"]):
        try:
            has_query = bool(urlsplit(url).query)
        except ValueError:
            has_query = False
        if has_query:  # "/?page_id=12": let the anchor text / title decide
            for text in (anchor, title):
                if text and len(text) <= 80:
                    pt = _classify_slug(_slug(text), allow_tokens=True)
                    if pt is not None and pt != PageType.home:
                        return pt
        return PageType.home
    # Deepest exact segment wins ("/agence/equipe" → team), then the first segment
    # ("/blog/notre-equipe-s-agrandit" → blog), then embedded keywords.
    last = _classify_slug(segs[-1], allow_tokens=False)
    if last is not None:
        return last
    for seg in reversed(segs[:-1]):
        pt = _classify_slug(seg, allow_tokens=False)
        if pt is not None and pt in (PageType.blog, PageType.news, PageType.careers, PageType.case_studies):
            return pt
    pt = _classify_slug(segs[-1], allow_tokens=True)
    if pt is not None:
        return pt
    for text in (anchor, title):
        if text and len(text) <= 80:
            pt = _classify_slug(_slug(text), allow_tokens=True)
            if pt is not None and pt != PageType.home:
                return pt
    for seg in segs[:-1]:
        pt = _classify_slug(seg, allow_tokens=False)
        if pt is not None:
            return pt
    return PageType.other


# ---- sitemaps ---------------------------------------------------------------------------------


@dataclass
class SitemapEntries:
    urls: list[str] = field(default_factory=list)  # <urlset><url><loc>
    sitemaps: list[str] = field(default_factory=list)  # <sitemapindex><sitemap><loc>

    @property
    def is_index(self) -> bool:
        return bool(self.sitemaps) and not self.urls


def parse_sitemap_entries(xml: str, *, limit: int = 5000) -> SitemapEntries:
    """Parse a sitemap (urlset or sitemapindex). XXE-safe; regex fallback for broken XML."""
    out = SitemapEntries()
    if not xml or not xml.strip():
        return out
    data = xml.strip().encode("utf-8", "ignore")
    try:
        parser = etree.XMLParser(recover=True, resolve_entities=False, no_network=True, huge_tree=False, load_dtd=False)
        root = etree.fromstring(data, parser=parser)
    except (etree.XMLSyntaxError, ValueError):
        root = None
    if root is not None:
        for el in root.iter():
            if not isinstance(el.tag, str) or etree.QName(el).localname != "loc" or not el.text:
                continue
            parent = el.getparent()
            pname = etree.QName(parent).localname if parent is not None and isinstance(parent.tag, str) else ""
            loc = el.text.strip()
            if not loc:
                continue
            (out.sitemaps if pname == "sitemap" else out.urls).append(loc)
            if len(out.urls) + len(out.sitemaps) >= limit:
                break
    if not out.urls and not out.sitemaps:
        is_index = "<sitemapindex" in xml[:2000].lower()
        for m in re.finditer(r"<loc>\s*(?:<!\[CDATA\[)?\s*([^<\]\s]+)", xml, re.IGNORECASE):
            (out.sitemaps if is_index else out.urls).append(m.group(1).strip())
            if len(out.urls) + len(out.sitemaps) >= limit:
                break
    return out


def parse_sitemap(xml: str) -> list[str]:
    """All ``<loc>`` URLs of a sitemap (page URLs for a urlset, child sitemaps for an index)."""
    entries = parse_sitemap_entries(xml)
    return entries.urls + entries.sitemaps


def pick_child_sitemaps(sitemaps: list[str], n: int = 1) -> list[str]:
    """Prefer page sitemaps over post/product/image ones (WordPress/Yoast/Shopify conventions)."""

    def score(u: str) -> tuple[int, int]:
        low = u.lower()
        if any(k in low for k in ("page", "pages", "static", "main", "general")):
            return (0, len(u))
        if any(k in low for k in ("post", "product", "image", "video", "blog", "news", "tag", "categor", "author")):
            return (2, len(u))
        return (1, len(u))

    return sorted(dict.fromkeys(sitemaps), key=score)[:n]


# ---- selection ---------------------------------------------------------------------------------


def _skip_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return True
    if parts.scheme not in ("http", "https"):
        return True
    path = parts.path or "/"
    if _ASSET_EXT_RE.search(path) or _SKIP_PATH_RE.search(path):
        return True
    if parts.query and _PAGINATION_QS_RE.search(parts.query):
        return True
    if parts.query:
        return True  # filters/sorting/session variants: never needed for company pages
    return len(path) > 160


@dataclass
class _Candidate:
    url: str
    page_type: PageType
    depth: int
    length: int
    from_link: bool
    lang_ok: bool
    legal_rank: int


def _legal_rank(url: str) -> int:
    segs = _segments(url)
    last = segs[-1] if segs else ""
    if any(k in last for k in ("mentions", "legal-notice", "imprint", "impressum", "aviso-legal", "informations-legales", "legal")):
        return 0
    return 1  # cgv/cgu/terms


def select_pages(
    home_url: str,
    internal_links: list[dict] | list[dict[str, str]],
    sitemap_urls: list[str],
    max_pages: int | None = None,
) -> list[tuple[str, PageType]]:
    """Prioritized crawl plan, home first: ``[(url, page_type), …]`` with ``len ≤ max_pages`` (≤ 12)."""
    budget = max_pages if max_pages is not None else get_settings().crawler_max_pages
    budget = max(1, min(int(budget), MAX_PAGES_CAP))
    home_key = canonical_url(home_url)
    domain = registrable_domain(home_url)
    home_parts = urlsplit(home_url)
    home_host = (home_parts.hostname or "").lower().removeprefix("www.")
    home_lang = _LANG_PREFIX_RE.match(urlsplit(home_url).path or "/")
    home_prefix = home_lang.group(1).lower() if home_lang else None

    raw: list[tuple[str, str | None, bool]] = []
    for link in internal_links or []:
        if isinstance(link, dict) and link.get("url"):
            raw.append((str(link["url"]), link.get("text"), True))
    for u in sitemap_urls or []:
        raw.append((u, None, False))

    best: dict[str, _Candidate] = {}
    for url, anchor, from_link in raw:
        url = url.split("#", 1)[0].strip()
        if not url or _skip_url(url) or registrable_domain(url) != domain:
            continue
        parts = urlsplit(url)
        host = (parts.hostname or "").lower().removeprefix("www.")
        if host != home_host:
            continue  # stay on the home host (no blog./shop. subdomains)
        # Fetch on the origin that served the home page (scheme + www/non-www), avoiding
        # redirect hops and TLS failures on http-only sites listed with https in sitemaps.
        url = urlunsplit((home_parts.scheme, home_parts.netloc, parts.path or "/", parts.query, ""))
        key = canonical_url(url)
        if key == home_key:
            continue
        pt = classify_page_type(url, None, anchor)
        if pt in (PageType.other, PageType.home):
            continue
        path = urlsplit(url).path or "/"
        m = _LANG_PREFIX_RE.match(path)
        prefix = m.group(1).lower() if m else None
        cand = _Candidate(
            url=url,
            page_type=pt,
            depth=len(_segments(url)),
            length=len(path),
            from_link=from_link,
            lang_ok=prefix == home_prefix,
            legal_rank=_legal_rank(url) if pt == PageType.legal else 0,
        )
        if cand.depth > 3 or (pt in (PageType.blog, PageType.news) and cand.depth > 1):
            continue  # listing pages only for blog/news; never deep article pages
        prev = best.get(key)
        if prev is None or (cand.from_link and not prev.from_link):
            best[key] = cand

    by_type: dict[PageType, list[_Candidate]] = {}
    for cand in best.values():
        by_type.setdefault(cand.page_type, []).append(cand)
    for cands in by_type.values():
        cands.sort(key=lambda c: (not c.lang_ok, c.legal_rank, c.depth, not c.from_link, c.length, c.url))

    plan: list[tuple[str, PageType]] = [(home_url, PageType.home)]
    picked: set[str] = {home_key}
    # Round 1: the best page of every type in priority order; round 2: second picks.
    for round_no in (0, 1):
        for pt, quota in _PRIORITY:
            if len(plan) >= budget:
                return plan
            cands = by_type.get(pt, [])
            if round_no >= quota or round_no >= len(cands):
                continue
            cand = cands[round_no]
            key = canonical_url(cand.url)
            if key in picked:
                continue
            picked.add(key)
            plan.append((cand.url, pt))
    return plan[:budget]
