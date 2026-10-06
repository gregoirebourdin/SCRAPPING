"""HTML → structured page (crawler tier L2, selectolax/lexbor).

``content_text`` is *line structured*: one block-level element per line, spaces collapsed within a
line, repeated boilerplate lines removed. Downstream extractors (team cards, legal notices,
addresses) rely on those line boundaries, so never flatten them.
"""

from __future__ import annotations

import html as html_lib
import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import structlog
from selectolax.lexbor import LexborHTMLParser, LexborNode

from scout.extract.email_extract import (  # noqa: F401 - EMAIL_RE re-exported for callers
    EMAIL_RE,
    cfemail_from_href,
    clean_email,
    decode_cfemail,
    emails_from_jsonld,
    emails_from_mailto,
    emails_from_text,
    emails_from_tree,
)
from scout.extract.social import LINKEDIN_PERSON, normalize_social_url
from scout.util.text import collapse_ws
from scout.util.urls import absolutize, canonical_url, registrable_domain

log = structlog.get_logger(__name__)

MAX_TEXT_BYTES = 40_000
MAX_HEAD_BYTES = 48_000
MAX_INTERNAL_LINKS = 400
MAX_EXTERNAL_LINKS = 50
MAX_JSONLD_OBJECTS = 60

_SKIP_TAGS = frozenset(
    {
        "script",
        "style",
        "noscript",
        "svg",
        "iframe",
        "template",
        "head",
        "object",
        "embed",
        "canvas",
        "select",
        "datalist",
        "math",
        "video",
        "audio",
        "picture",
        "map",
        "dialog",
    }
)
_BLOCK_TAGS = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "body",
        "dd",
        "details",
        "div",
        "dl",
        "dt",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "summary",
        "table",
        "tr",
        "td",
        "th",
        "caption",
        "ul",
        "option",
        "legend",
        "label",
        "button",
        "center",
        "tbody",
        "thead",
        "tfoot",
        "menu",
        "hgroup",
        "html",
    }
)
_CHROME_TAGS = frozenset({"header", "nav", "footer"})
_SOCIAL_HOST_DOMAINS = frozenset(
    {
        "facebook.com",
        "fb.com",
        "fb.me",
        "instagram.com",
        "linkedin.com",
        "twitter.com",
        "x.com",
        "tiktok.com",
        "youtube.com",
        "youtu.be",
        "pinterest.com",
        "pinterest.fr",
        "pin.it",
        "threads.net",
        "whatsapp.com",
    }
)

# ---- phones ---------------------------------------------------------------------------------

_FR_PHONE_RE = re.compile(
    r"(?<![\d+])(?:(?:\+|00)\s?33[\s.\-]?(?:\(0\)[\s.\-]?)?[1-9]|0[1-9])(?:[\s.\-]?\d{2}){4}(?!\d)"
)
_INTL_PHONE_RE = re.compile(
    r"(?<![\d+\w])\+(?!33)[1-9]\d{0,2}[\s.\-]?(?:\(\d{1,4}\)[\s.\-]?)?\d{1,4}(?:[\s.\-]?\d{2,4}){2,4}(?!\d)"
)

# ---- language ---------------------------------------------------------------------------------

_STOPWORDS: dict[str, frozenset[str]] = {
    "fr": frozenset(
        "les des est une pour dans nous vous avec sur qui sont pas plus par cette notre nos votre vos aux du au et le la".split()
    ),
    "en": frozenset(
        "the and is are for with our your we you to of that this from by at as be have will".split()
    ),
    "de": frozenset(
        "der die das und ist mit für wir sie nicht von zu den dem ein eine auf unsere ihre sind".split()
    ),
    "es": frozenset(
        "el los las y es para con nuestro nuestra nuestros que por una del se su sus somos".split()
    ),
    "it": frozenset(
        "il lo gli e è per con nostro nostra che di un una del della sono siamo nel alla".split()
    ),
}
_AMBIGUOUS_STOPWORDS = frozenset({"la", "de", "en", "le", "in", "e", "a", "se", "che"})


@dataclass
class ParsedPage:
    title: str | None = None
    meta_description: str | None = None
    language: str | None = None
    content_text: str = ""
    headings: list[str] = field(default_factory=list)
    links: dict[str, Any] = field(default_factory=lambda: {"social": {}, "internal": [], "external": []})
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    structured_data: list[dict[str, Any]] = field(default_factory=list)
    word_count: int = 0
    head_html: str | None = None
    generator: str | None = None
    canonical: str | None = None
    meta: dict[str, str] = field(default_factory=dict)
    noscript_text: str = ""
    spa_root: bool = False


# =============================================================================================
# helpers
# =============================================================================================


def _attr(node: LexborNode, name: str) -> str | None:
    try:
        val = node.attributes.get(name)
    except Exception:
        return None
    return val if isinstance(val, str) else None


def _is_hidden(node: LexborNode) -> bool:
    attrs = node.attributes
    if "hidden" in attrs:
        return True
    style = (attrs.get("style") or "").replace(" ", "").lower()
    return "display:none" in style or "visibility:hidden" in style


def _visible_lines(root: LexborNode) -> list[tuple[str, bool]]:
    """Iterative DOM walk → [(line, in_header_nav_footer)] preserving block boundaries."""
    segments: list[tuple[str, bool]] = []
    # stack items: (node, closing marker: "" | "\n" | " ", chrome_depth)
    stack: list[tuple[LexborNode, str, int]] = [(root, "", 0)]
    while stack:
        node, closing, chrome = stack.pop()
        if closing:
            segments.append((closing, chrome > 0))
            continue
        tag = node.tag or ""
        if tag == "-text":
            txt = node.text_content
            if txt:
                segments.append((txt, chrome > 0))
            continue
        if tag.startswith("-") or tag in _SKIP_TAGS:
            continue
        if tag == "br":
            segments.append(("\n", chrome > 0))
            continue
        if tag in ("img", "input", "hr", "wbr", "meta", "link"):
            continue
        if _is_hidden(node):
            continue
        depth = chrome + (1 if tag in _CHROME_TAGS else 0)
        if tag in _BLOCK_TAGS or (tag == "a" and depth > 0):
            # Navigation links are discrete items: one per line so header/footer repeats dedupe.
            segments.append(("\n", depth > 0))
            stack.append((node, "\n", depth))
        elif tag == "a":
            segments.append((" ", False))
            stack.append((node, " ", depth))
        children = list(node.iter(include_text=True))
        for child in reversed(children):
            stack.append((child, "", depth))

    lines: list[tuple[str, bool]] = []
    buf: list[str] = []
    buf_chrome = False
    for text, chrome in segments:
        if text == "\n":
            line = collapse_ws("".join(buf).replace(" ", " ").replace("​", ""))
            if line:
                lines.append((line, buf_chrome))
            buf, buf_chrome = [], False
        else:
            buf.append(text)
            buf_chrome = buf_chrome or chrome
    line = collapse_ws("".join(buf).replace(" ", " "))
    if line:
        lines.append((line, buf_chrome))
    return lines


def _dedupe_lines(lines: list[tuple[str, bool]]) -> list[str]:
    """Drop repeated boilerplate: chrome (header/nav/footer) repeats and long repeated paragraphs.

    Short repeated lines in the main content (e.g. the same job title on two team cards) are kept.
    """
    seen: set[str] = set()
    out: list[str] = []
    for line, chrome in lines:
        key = line.lower()
        if key in seen and (chrome or len(line) >= 30):
            continue
        seen.add(key)
        out.append(line)
    return out


def _cap_text(text: str, limit: int = MAX_TEXT_BYTES) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    cut = raw[:limit].decode("utf-8", errors="ignore")
    nl = cut.rfind("\n")
    return cut[:nl] if nl > limit // 2 else cut


def _detect_language_from_text(text: str) -> str | None:
    words = re.findall(r"[a-zà-ÿäöüßèéêëìíîïòóôõùúûñç']+", text.lower()[:20000])
    if len(words) < 8:
        return None
    scores = dict.fromkeys(_STOPWORDS, 0.0)
    for w in words:
        for lang, sw in _STOPWORDS.items():
            if w in sw:
                scores[lang] += 0.3 if w in _AMBIGUOUS_STOPWORDS else 1.0
    best = max(scores, key=lambda k: scores[k])
    ordered = sorted(scores.values(), reverse=True)
    if ordered[0] >= 3 and ordered[0] >= ordered[1] * 1.3:
        return best
    return None


def _lang_code(value: str | None) -> str | None:
    if not value:
        return None
    m = re.match(r"^\s*([a-zA-Z]{2,3})(?:[-_][a-zA-Z0-9]+)*\s*$", value)
    return m.group(1).lower() if m else None


def normalize_phone(raw: str, *, default_country: str | None = "FR") -> str | None:
    """'+33 (0)1 23 45 67 89' / '01.23.45.67.89' → '+33123456789'; other numbers → '+<digits>'."""
    s = (raw or "").strip()
    if not s:
        return None
    s = re.sub(r"\(0\)", "", s)
    plus = s.startswith("+")
    digits = re.sub(r"\D", "", s)
    if not plus and digits.startswith("00"):
        digits, plus = digits[2:], True
    if plus:
        if digits.startswith("330") and len(digits) == 12:  # +33 0X… written with the trunk zero
            digits = "33" + digits[3:]
        return "+" + digits if 8 <= len(digits) <= 15 else None
    if default_country == "FR" and len(digits) == 10 and digits[0] == "0" and digits[1] != "0":
        return "+33" + digits[1:]
    if 6 <= len(digits) <= 15:
        return digits
    return None


_decode_cfemail = decode_cfemail  # backwards-compatible alias


def phones_from_text(text: str) -> list[str]:
    out: list[str] = []
    for rx in (_FR_PHONE_RE, _INTL_PHONE_RE):
        for m in rx.finditer(text):
            p = normalize_phone(m.group(0))
            if p and p not in out:
                out.append(p)
    return out


def _flatten_jsonld(obj: Any, out: list[dict[str, Any]]) -> None:
    if len(out) >= MAX_JSONLD_OBJECTS:
        return
    if isinstance(obj, list):
        for item in obj:
            _flatten_jsonld(item, out)
    elif isinstance(obj, dict):
        graph = obj.get("@graph")
        if graph is not None:
            rest = {k: v for k, v in obj.items() if k != "@graph"}
            if "@type" in rest:
                out.append(rest)
            _flatten_jsonld(graph, out)
        else:
            out.append(obj)


def _parse_jsonld(raw: str) -> list[dict[str, Any]]:
    txt = (raw or "").strip()
    if not txt:
        return []
    txt = re.sub(r"^\s*<!--|-->\s*$", "", txt)
    txt = re.sub(r"^\s*//\s*<!\[CDATA\[|//\s*\]\]>\s*$", "", txt).strip()
    data: Any = None
    for candidate in (txt, re.sub(r",\s*([}\]])", r"\1", txt)):
        try:
            data = json.loads(candidate, strict=False)
            break
        except (ValueError, RecursionError):
            continue
    if data is None:
        return []
    out: list[dict[str, Any]] = []
    _flatten_jsonld(data, out)
    return out


def _open_tag(node: LexborNode) -> str:
    attrs = []
    for k, v in node.attributes.items():
        if v is None:
            attrs.append(k)
        else:
            attrs.append(f'{k}="{html_lib.escape(v[:500], quote=True)}"')
    return f"<{node.tag}{' ' + ' '.join(attrs) if attrs else ''}>"


def _compact_node_html(node: LexborNode) -> str:
    tag = node.tag or ""
    if tag == "script":
        if _attr(node, "src"):
            return _open_tag(node) + "</script>"
        body = node.text(deep=True) or ""
        return _open_tag(node) + body[:4000] + "</script>"
    if tag == "style":
        return _open_tag(node) + (node.text(deep=True) or "")[:300] + "</style>"
    if tag in ("meta", "link", "base"):
        return _open_tag(node)
    return node.html or ""


def _build_head_html(tree: LexborHTMLParser) -> str | None:
    parts: list[str] = []
    size = 0

    def add(fragment: str) -> bool:
        nonlocal size
        b = len(fragment.encode("utf-8"))
        if size + b > MAX_HEAD_BYTES:
            return False
        parts.append(fragment)
        size += b
        return True

    root = tree.root
    if root is not None and root.tag == "html":
        add(_open_tag(root))
    head = tree.head
    if head is not None:
        add("<head>")
        for child in head.iter():
            if not add(_compact_node_html(child) + "\n"):
                break
        add("</head>")
    body = tree.body
    if body is not None:
        add(_open_tag(body))
        for node in body.css(
            "script[src], link, iframe[src], meta, #__next, #___gatsby, #__nuxt, #root, #app, "
            "[ng-version], [data-reactroot], [data-wf-page], [data-server-rendered]"
        ):
            if not add(_open_tag(node) + "\n"):
                break
        for node in body.css("script:not([src])"):
            body_txt = (node.text(deep=True) or "").strip()
            if body_txt and not add(_open_tag(node) + body_txt[:1500] + "</script>\n"):
                break
    return "".join(parts) or None


def _link_text(node: LexborNode) -> str:
    txt = collapse_ws(node.text(deep=True, separator=" ") or "")
    if not txt:
        txt = collapse_ws(_attr(node, "aria-label") or _attr(node, "title") or "")
    if not txt:
        img = node.css_first("img[alt]")
        if img is not None:
            txt = collapse_ws(_attr(img, "alt") or "")
    return txt[:120]


# =============================================================================================
# main entry point
# =============================================================================================


def parse_html(html: str, url: str, *, is_home: bool = False) -> ParsedPage:
    """Parse raw HTML into a :class:`ParsedPage`. Never raises on malformed markup."""
    page = ParsedPage()
    if not html or not html.strip():
        return page
    try:
        tree = LexborHTMLParser(html)
    except Exception as exc:  # pragma: no cover - lexbor is very tolerant
        log.warning("html_parse_failed", url=url, error=str(exc))
        return page

    # --- head-level metadata -------------------------------------------------------------
    title_node = tree.css_first("title")
    if title_node is not None:
        page.title = collapse_ws(title_node.text(deep=True) or "") or None
    meta: dict[str, str] = {}
    generators: list[str] = []
    for m in tree.css("meta"):
        key = (_attr(m, "name") or _attr(m, "property") or _attr(m, "http-equiv") or "").strip().lower()
        content = _attr(m, "content")
        if not key or content is None:
            continue
        content = collapse_ws(content)
        if key == "generator":
            generators.append(content)
        meta.setdefault(key, content)
    page.meta = meta
    page.generator = "; ".join(dict.fromkeys(g for g in generators if g)) or None
    if not page.title:
        page.title = meta.get("og:title") or meta.get("twitter:title") or None
    page.meta_description = (
        meta.get("description") or meta.get("og:description") or meta.get("twitter:description") or None
    )

    base_url = url
    base = tree.css_first("base[href]")
    if base is not None:
        b = absolutize(url, _attr(base, "href") or "")
        if b and registrable_domain(b) == registrable_domain(url):
            base_url = b
    canon = tree.css_first('link[rel="canonical"]')
    if canon is not None:
        page.canonical = absolutize(base_url, _attr(canon, "href") or "")

    root = tree.root
    lang = _lang_code(_attr(root, "lang") or _attr(root, "xml:lang")) if root is not None else None
    lang = lang or _lang_code(meta.get("og:locale")) or _lang_code(meta.get("content-language"))

    if is_home:
        try:
            page.head_html = _build_head_html(tree)
        except Exception as exc:  # defensive: fingerprints are optional
            log.debug("head_html_failed", url=url, error=str(exc))

    # --- JSON-LD ------------------------------------------------------------------------------
    for script in tree.css(
        'script[type="application/ld+json"], script[type="application/ld+json; charset=utf-8"]'
    ):
        page.structured_data.extend(_parse_jsonld(script.text(deep=True) or ""))
        if len(page.structured_data) >= MAX_JSONLD_OBJECTS:
            break

    # --- SPA hints ---------------------------------------------------------------------------
    page.spa_root = (
        tree.css_first("#root, #__next, #app, #__nuxt, #___gatsby, [ng-app], [ng-version], app-root")
        is not None
    )
    page.noscript_text = collapse_ws(" ".join((n.text(deep=True) or "") for n in tree.css("noscript")))[:500]

    # --- links, mailto, tel, cloudflare emails ---------------------------------------------
    page_domain = registrable_domain(url)
    social: dict[str, str] = {}
    internal: list[dict[str, str]] = []
    external: list[dict[str, str]] = []
    profiles: list[dict[str, str]] = []
    seen_internal: set[str] = set()
    seen_external: set[str] = set()
    raw_emails: list[str] = []
    raw_phones: list[str] = []
    for a in tree.css("a[href]"):
        href = (_attr(a, "href") or "").strip()
        if not href:
            continue
        low = href.lower()
        if low.startswith("mailto:"):
            raw_emails.extend(emails_from_mailto(href))
            continue
        if low.startswith(("tel:", "callto:")):
            raw_phones.append(href.split(":", 1)[1])
            continue
        if "/cdn-cgi/l/email-protection" in low and "#" in href:
            decoded = cfemail_from_href(href)
            if decoded:
                raw_emails.append(decoded)
            continue
        absolute = absolutize(base_url, href)
        if not absolute or not absolute.lower().startswith(("http://", "https://")):
            continue
        absolute = absolute.split("#", 1)[0]
        text = _link_text(a)
        norm = normalize_social_url(absolute)
        if norm is not None:
            network, canonical = norm
            if network == LINKEDIN_PERSON:
                if canonical not in {p["url"] for p in profiles}:
                    profiles.append({"url": canonical, "text": text})
            else:
                social.setdefault(network, canonical)
            continue
        dom = registrable_domain(absolute)
        if dom in _SOCIAL_HOST_DOMAINS:
            continue  # share/intent/post links
        try:
            key = canonical_url(absolute)
        except ValueError:
            continue
        if dom and dom == page_domain:
            if key not in seen_internal and len(internal) < MAX_INTERNAL_LINKS:
                seen_internal.add(key)
                internal.append({"url": absolute, "text": text})
        elif key not in seen_external and len(external) < MAX_EXTERNAL_LINKS:
            seen_external.add(key)
            external.append({"url": absolute, "text": text})
    raw_emails.extend(emails_from_tree(tree))  # data-cfemail, CSS-reversed text, JSON data islands
    links: dict[str, Any] = {"social": social, "internal": internal, "external": external}
    if profiles:
        links["people_profiles"] = profiles[:50]
    page.links = links

    # --- visible text ----------------------------------------------------------------------
    body = tree.body or tree.root
    lines: list[str] = []
    if body is not None:
        lines = _dedupe_lines(_visible_lines(body))
    text = _cap_text("\n".join(lines))
    page.content_text = text
    page.word_count = len(text.split())
    page.headings = [
        h
        for h in dict.fromkeys(
            collapse_ws(n.text(deep=True, separator=" ") or "") for n in tree.css("h1, h2, h3")
        )
        if h
    ][:60]

    # --- emails / phones -------------------------------------------------------------------
    emails: list[str] = []
    for raw in raw_emails:
        e = clean_email(raw)
        if e and e not in emails:
            emails.append(e)
    for e in [*emails_from_text(text, site_domain=page_domain), *emails_from_jsonld(page.structured_data)]:
        if e not in emails:
            emails.append(e)
    page.emails = emails[:50]
    phones: list[str] = []
    for raw in raw_phones:
        p = normalize_phone(raw)
        if p and p not in phones:
            phones.append(p)
    for p in phones_from_text(text):
        if p not in phones:
            phones.append(p)
    page.phones = phones[:20]

    page.language = lang or _detect_language_from_text(text)
    return page


def host_path(url: str) -> tuple[str, str]:
    """Small helper for callers: (lowercase host, path)."""
    parts = urlsplit(url)
    return (parts.hostname or "").lower(), parts.path or "/"
