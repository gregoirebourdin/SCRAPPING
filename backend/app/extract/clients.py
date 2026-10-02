"""Find the coaches / course creators / infopreneurs an agency names as clients, with evidence.

Signals combined (each adds confidence):
  * structured testimonials (JSON-LD ``Review``), testimonial/case-study DOM blocks (``— Name, Business Coach``)
  * headings on case-study pages ("How we helped Jane Doe add $40k/month")
  * sentences where a PERSON entity (spaCy) co-occurs with client context and/or a coach-type role
  * client logo walls (``<img alt="...">`` inside a "trusted by" section) → brand clients
  * outbound links whose domain/anchor matches the person (gives us their website for free)
The agency's own team (about/team pages, "founder of <agency>") is excluded.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from selectolax.parser import HTMLParser

from ..fetch.crawler import CrawledSite
from ..lexicon import CASE_STUDY_PATH_RE, CLIENT_CONTEXT_RE, COACH_ROLE_RE, MONEY_RESULT_RE, NAME_STOPWORDS
from ..util.text import clean_ws, sentences, titlecase_ratio
from ..util.urls import absolutize, is_blocked_domain, registrable_domain, social_network

log = logging.getLogger(__name__)

NAME_RE = re.compile(r"\b([A-Z][a-z]+(?:[-'’][A-Z][a-z]+)?(?:\s+(?:[A-Z]\.?\s+)?[A-Z][a-z]+(?:[-'’][A-Z][a-z]+)?){1,2})\b")
ATTRIB_RE = re.compile(r"(?:^|[\n\"”’'])\s*[—–\-~]\s*([A-Z][A-Za-z'’.-]+(?:\s+[A-Z][A-Za-z'’.-]+){1,3})\s*(?:[,|–—\-]\s*(.{3,80}))?$", re.M)
PIPE_ROLE_RE = re.compile(r"^([A-Z][A-Za-z'’.-]+(?:\s+[A-Z][A-Za-z'’.-]+){1,3})\s*[,|–—\-:]\s*(.{3,90})$")
HEADING_CLIENT_RE = re.compile(r"(?:how (?:we )?helped|case study[:\s-]+|client spotlight[:\s-]+|success story[:\s-]+|working with|results for|meet)\s+([A-Z][A-Za-z'’.-]+(?:\s+[A-Z][A-Za-z'’.-]+){0,3})", re.I)
HEADING_RESULT_RE = re.compile(r"^([A-Z][A-Za-z'’.-]+(?:\s+[A-Z][A-Za-z'’.-]+){1,3})\s*[:|–—\-]\s*.*(\$|€|£|%|x|roas|leads|calls|clients|students|launch)", re.I)
TEAM_CONTEXT_RE = re.compile(r"\b(our (founder|ceo|team|co-founder|head of|director)|meet (the|our) team|i'?m the founder|my name is|i am the founder|founder & ceo|co-founder|account manager|media buyer|strategist|head of growth|team member|joined (the )?team|our story)\b", re.I)
BRAND_WORDS_RE = re.compile(r"\b(academy|institute|coaching|co\.?|llc|inc\.?|ltd|group|collective|method|system|university|school|lab|labs|media|agency|studio|club|society|mastermind|program|blueprint|formula|society|network|hq|company|fitness|wellness|yoga|nutrition)\b", re.I)
TESTIMONIAL_CLASS_RE = re.compile(r"(testimonial|review|quote|case-?stud|client|success|result|story|feedback|wall-of-love|proof|kudos|praise)", re.I)
LOGO_SECTION_RE = re.compile(r"(client|logo|trusted|featured|partner|brand|worked|as-seen|seen-on|portfolio)", re.I)
AS_SEEN_RE = re.compile(r"(as seen (on|in)|featured (on|in)|press)", re.I)

_nlp = None


def _spacy():
    global _nlp
    if _nlp is None:
        try:
            import spacy

            _nlp = spacy.load("en_core_web_sm", disable=["parser", "lemmatizer", "textcat"])
        except Exception as e:  # pragma: no cover - model missing
            log.warning("spaCy unavailable (%s); falling back to regex names", e)
            _nlp = False
    return _nlp or None


@dataclass
class ClientHit:
    name: str
    kind: str = "person"
    role_title: str | None = None
    niche: str | None = None
    evidence: str = ""
    evidence_url: str = ""
    website: str | None = None
    website_source: str | None = None
    confidence: float = 0.3
    signals: set[str] = field(default_factory=set)


def _clean_name(raw: str) -> str | None:
    n = clean_ws(raw).strip(" .,:;|-–—\"'“”‘’")
    n = re.sub(r"\s+", " ", n)
    n = re.sub(r"^(by|with|from|and|for|the|our|client|meet|featuring)\s+", "", n, flags=re.I)
    if not n or len(n) < 4 or len(n) > 48:
        return None
    toks = n.split()
    if not 1 <= len(toks) <= 4:
        return None
    if n.lower() in NAME_STOPWORDS or toks[0].lower() in {"the", "a", "an", "our", "your", "my", "this", "that", "how", "why", "what", "when"}:
        return None
    if titlecase_ratio(n) < 0.6:
        return None
    if re.search(r"\d", n) and not BRAND_WORDS_RE.search(n):
        return None
    if re.fullmatch(r"(?i)(facebook|instagram|google|youtube|tiktok|meta|kajabi|clickfunnels)\b.*", n):
        return None
    return n


def _role_near(text: str, name: str, window: int = 140) -> str | None:
    i = text.find(name)
    if i < 0:
        return None
    ctx = text[max(0, i - window): i + len(name) + window]
    m = COACH_ROLE_RE.search(ctx)
    if not m:
        return None
    role = m.group(1)
    # expand to "Founder of X" style phrase
    tail = re.search(re.escape(role) + r"(?:\s+(?:of|at|for)\s+[A-Z][\w'’&. -]{2,40})?", ctx)
    return clean_ws(tail.group(0) if tail else role)[:120]


def _niche(role: str | None, ctx: str) -> str | None:
    blob = f"{role or ''} {ctx}".lower()
    for key, label in (
        ("business coach", "business coaching"), ("life coach", "life coaching"), ("fitness", "fitness"), ("health", "health & wellness"),
        ("wellness", "health & wellness"), ("nutrition", "nutrition"), ("mindset", "mindset"), ("relationship", "relationships"), ("dating", "dating"),
        ("real estate", "real estate"), ("trading", "trading & investing"), ("forex", "trading & investing"), ("crypto", "trading & investing"),
        ("ecommerce", "ecommerce education"), ("amazon", "ecommerce education"), ("financial", "finance"), ("money", "finance"), ("career", "career"),
        ("leadership", "leadership"), ("executive", "executive coaching"), ("spiritual", "spirituality"), ("yoga", "yoga"), ("parenting", "parenting"),
        ("marketing", "marketing education"), ("sales", "sales training"), ("author", "author / publishing"), ("speaker", "speaking"), ("course", "online courses"),
        ("coach", "coaching"), ("consult", "consulting"),
    ):
        if key in blob:
            return label
    return None


class ClientExtractor:
    def __init__(self, site: CrawledSite, agency_name: str) -> None:
        self.site = site
        self.agency_name = agency_name
        self.agency_tokens = {t for t in re.findall(r"[a-z0-9]+", agency_name.lower()) if len(t) > 2}
        self.agency_tokens |= {t for t in re.findall(r"[a-z0-9]+", site.domain.split(".")[0].lower()) if len(t) > 2}
        self.team: set[str] = set()
        self.hits: dict[str, ClientHit] = {}

    # --- helpers ---------------------------------------------------------------------------------------------
    def _is_team(self, name: str, ctx: str) -> bool:
        if name.lower() in self.team:
            return True
        low = ctx.lower()
        if TEAM_CONTEXT_RE.search(ctx):
            return True
        m = re.search(r"\b(founder|ceo|owner|director|president|head)\b[^.\n]{0,40}\b(of|at|@)\s+([A-Za-z0-9'’&. -]{2,50})", ctx, re.I)
        if m:
            org_tokens = {t for t in re.findall(r"[a-z0-9]+", m.group(3).lower()) if len(t) > 2}
            if org_tokens & self.agency_tokens:
                return True
        for tok in self.agency_tokens:
            if len(tok) > 4 and re.search(rf"\b(i'?m|i am|hi,? i'?m|hey,? i'?m)\b[^.\n]{{0,30}}\b{re.escape(name.split()[0].lower())}\b", low):
                return True
        return False

    def _add(self, name: str, *, kind: str, ctx: str, url: str, signal: str, base_conf: float) -> None:
        cleaned = _clean_name(name)
        if not cleaned:
            return
        if self.agency_tokens and {t for t in re.findall(r"[a-z0-9]+", cleaned.lower())} <= self.agency_tokens:
            return
        if self._is_team(cleaned, ctx):
            return
        ctx_clean = clean_ws(ctx).replace("\n", " ")
        role = _role_near(ctx_clean, cleaned)
        if kind == "person" and BRAND_WORDS_RE.search(cleaned) and len(cleaned.split()) >= 2 and not re.search(r"\b(of|at)\b", cleaned):
            kind = "brand"
        hit = self.hits.get(cleaned.lower())
        if hit is None:
            hit = ClientHit(name=cleaned, kind=kind, evidence=ctx_clean[:400], evidence_url=url, confidence=base_conf)
            self.hits[cleaned.lower()] = hit
        else:
            hit.confidence = min(0.98, hit.confidence + base_conf * 0.4)
            if len(ctx_clean) > len(hit.evidence) and signal in ("heading", "testimonial", "sentence"):
                hit.evidence = ctx_clean[:400]
                hit.evidence_url = url
        hit.signals.add(signal)
        if role and not hit.role_title:
            hit.role_title = role
            hit.confidence = min(0.98, hit.confidence + 0.2)
        if MONEY_RESULT_RE.search(ctx_clean):
            hit.signals.add("result")
            hit.confidence = min(0.98, hit.confidence + 0.1)
        if CLIENT_CONTEXT_RE.search(ctx_clean):
            hit.signals.add("client_context")
            hit.confidence = min(0.98, hit.confidence + 0.05)
        if not hit.niche:
            hit.niche = _niche(hit.role_title, ctx_clean)

    # --- passes ----------------------------------------------------------------------------------------------
    def _collect_team(self) -> None:
        for p in self.site.pages_of("about", "home", "services"):
            for m in re.finditer(r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})\s*[,|–—\-]?\s*(?:is (?:the|our)\s+)?(founder|co-founder|ceo|owner|director|head of|managing|chief|partner|lead|strategist|media buyer|account manager|team)", p.text):
                ctx = p.text[max(0, m.start() - 80): m.end() + 80]
                if not re.search(r"\b(client|testimonial|case study|review)\b", ctx, re.I):
                    self.team.add(m.group(1).lower())
            for m in re.finditer(r"\b(?:i'?m|i am|my name is|hi,? i'?m)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)", p.text):
                self.team.add(m.group(1).lower())

    def _jsonld_reviews(self) -> None:
        for p in self.site.pages:
            for d in p.parsed.jsonld:
                items = []
                if str(d.get("@type", "")).lower() in ("review",):
                    items.append(d)
                for r in d.get("review", []) if isinstance(d.get("review"), list) else []:
                    if isinstance(r, dict):
                        items.append(r)
                for r in items:
                    author = r.get("author")
                    name = author.get("name") if isinstance(author, dict) else author if isinstance(author, str) else None
                    body = r.get("reviewBody") or r.get("description") or ""
                    if name:
                        self._add(str(name), kind="person", ctx=f"{name}: {body}", url=p.url, signal="testimonial", base_conf=0.5)

    def _dom_blocks(self) -> None:
        for p in self.site.pages:
            if not p.parsed.html:
                continue
            tree = HTMLParser(p.parsed.html)
            if tree.body is None:
                continue
            # testimonial / case-study blocks
            seen_blocks = 0
            for node in tree.css("blockquote, figure, [class*=testimonial], [class*=review], [class*=quote], [class*=case-stud], [class*=casestud], [class*=client], [class*=success], [class*=result], [id*=testimonial], [id*=review], [class*=story], [class*=feedback]"):
                cls = f"{node.attributes.get('class','')} {node.attributes.get('id','')}"
                if not TESTIMONIAL_CLASS_RE.search(cls) and node.tag not in ("blockquote", "figure"):
                    continue
                text = clean_ws(node.text(separator="\n"))
                if len(text) < 25 or len(text) > 2500:
                    continue
                seen_blocks += 1
                if seen_blocks > 120:
                    break
                cite = node.css_first("cite, figcaption, [class*=author], [class*=name], [class*=title], h3, h4, h5, strong, b")
                cand_names: list[str] = []
                if cite is not None:
                    ct = clean_ws(cite.text()).replace("\n", " ")
                    m = PIPE_ROLE_RE.match(ct) or re.match(r"^([A-Z][A-Za-z'’.-]+(?:\s+[A-Z][A-Za-z'’.-]+){1,3})$", ct)
                    if m:
                        cand_names.append(m.group(1))
                for m in ATTRIB_RE.finditer(text):
                    cand_names.append(m.group(1))
                lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
                for ln in lines[-4:] + lines[:2]:
                    m = PIPE_ROLE_RE.match(ln)
                    if m:
                        cand_names.append(m.group(1))
                    elif 1 < len(ln.split()) <= 4 and titlecase_ratio(ln) >= 0.9 and len(ln) < 40 and not COACH_ROLE_RE.fullmatch(ln):
                        cand_names.append(ln)
                for n in cand_names[:4]:
                    self._add(n, kind="person", ctx=text, url=p.url, signal="testimonial", base_conf=0.45)

            # client logo walls → brands
            for node in tree.css("section, div, ul"):
                cls = f"{node.attributes.get('class','')} {node.attributes.get('id','')}"
                if not LOGO_SECTION_RE.search(cls):
                    continue
                heading = clean_ws(node.text(separator=" "))[:200]
                if AS_SEEN_RE.search(heading) or AS_SEEN_RE.search(cls):
                    continue  # press logos (Forbes, Inc...) are not clients
                imgs = node.css("img[alt]")
                if not 2 <= len(imgs) <= 40:
                    continue
                for img in imgs:
                    alt = clean_ws(img.attributes.get("alt") or "")
                    alt = re.sub(r"(?i)\b(logo|client|brand|image|icon|png|svg|jpg)\b", "", alt).strip(" -_|")
                    if 3 <= len(alt) <= 40 and not re.search(r"\d{3,}", alt):
                        self._add(alt.title() if alt.islower() else alt, kind="brand", ctx=f"Client logo wall on {p.kind} page: {alt}", url=p.url, signal="logo_wall", base_conf=0.3)

    def _headings(self) -> None:
        for p in self.site.pages:
            cs = bool(CASE_STUDY_PATH_RE.search(p.url)) or p.kind == "case_studies"
            for h in p.parsed.headings + [p.parsed.title]:
                if not h:
                    continue
                m = HEADING_CLIENT_RE.search(h)
                if m:
                    self._add(m.group(1), kind="person", ctx=h, url=p.url, signal="heading", base_conf=0.5 if cs else 0.4)
                    continue
                m = HEADING_RESULT_RE.match(h)
                if m and cs:
                    self._add(m.group(1), kind="person", ctx=h, url=p.url, signal="heading", base_conf=0.45)

    def _sentences(self) -> None:
        nlp = _spacy()
        budget = 70_000
        for p in sorted(self.site.pages, key=lambda x: 0 if x.kind in ("case_studies", "clients", "home") else 1):
            cs = p.kind in ("case_studies", "clients") or bool(CASE_STUDY_PATH_RE.search(p.url))
            for s in sentences(p.text):
                if len(s) < 25 or len(s) > 600:
                    continue
                if not (CLIENT_CONTEXT_RE.search(s) or COACH_ROLE_RE.search(s)):
                    continue
                budget -= len(s)
                if budget < 0:
                    return
                names: list[str] = []
                if nlp is not None:
                    doc = nlp(s)
                    names = [e.text for e in doc.ents if e.label_ == "PERSON"]
                    orgs = [e.text for e in doc.ents if e.label_ == "ORG" and BRAND_WORDS_RE.search(e.text)]
                    for o in orgs[:2]:
                        if COACH_ROLE_RE.search(s):
                            self._add(o, kind="brand", ctx=s, url=p.url, signal="sentence", base_conf=0.3)
                else:
                    names = [m.group(1) for m in NAME_RE.finditer(s)]
                for n in names[:3]:
                    base = 0.35 if cs else 0.28
                    if COACH_ROLE_RE.search(s):
                        base += 0.1
                    self._add(n, kind="person", ctx=s, url=p.url, signal="sentence", base_conf=base)

    def _outbound_links(self) -> None:
        """Match outbound links to extracted client names → client website."""
        if not self.hits:
            return
        links: list[tuple[str, str, str]] = []
        for p in self.site.pages:
            for href, anchor in p.parsed.links:
                url = absolutize(p.url, href)
                if not url:
                    continue
                dom = registrable_domain(url)
                if not dom or dom == self.site.domain or is_blocked_domain(dom) or social_network(url):
                    continue
                links.append((url, dom, (anchor or "").lower()))
        for hit in self.hits.values():
            toks = [t for t in re.findall(r"[a-z0-9]+", hit.name.lower()) if len(t) > 2]
            if not toks:
                continue
            joined = "".join(toks)
            last = toks[-1]
            for url, dom, anchor in links:
                dom_core = dom.split(".")[0].replace("-", "")
                if joined == dom_core or (len(joined) > 7 and joined in dom_core) or (len(last) > 4 and dom_core.startswith(last) and toks[0][:3] in dom_core):
                    hit.website = url.split("?", 1)[0]
                    hit.website_source = "outbound_link"
                    hit.confidence = min(0.98, hit.confidence + 0.12)
                    hit.signals.add("outbound_link")
                    break
                if anchor and hit.name.lower() == anchor.strip():
                    hit.website = url.split("?", 1)[0]
                    hit.website_source = "outbound_link"
                    hit.confidence = min(0.98, hit.confidence + 0.08)
                    hit.signals.add("outbound_link")
                    break

    def run(self) -> list[ClientHit]:
        self._collect_team()
        self._jsonld_reviews()
        self._dom_blocks()
        self._headings()
        self._sentences()
        self._outbound_links()
        out: list[ClientHit] = []
        for hit in self.hits.values():
            # A coach/infopreneur client needs role evidence OR strong structure + ICP context in the evidence
            icp_ctx = bool(COACH_ROLE_RE.search(hit.evidence)) or bool(re.search(r"\b(coach|course|program|students|launch|webinar|mastermind|membership)\b", hit.evidence, re.I))
            if hit.kind == "person" and not hit.role_title and not icp_ctx and "logo_wall" not in hit.signals:
                hit.confidence -= 0.15
            if not icp_ctx and not hit.role_title:
                hit.confidence -= 0.1
            if hit.confidence >= 0.3:
                out.append(hit)
        out.sort(key=lambda h: (-h.confidence, h.kind != "person"))
        return out[:25]


def extract_clients(site: CrawledSite, agency_name: str) -> list[ClientHit]:
    return ClientExtractor(site, agency_name).run()
