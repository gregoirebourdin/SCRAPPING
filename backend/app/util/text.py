"""HTML → structured page (text with block boundaries, meta, links, JSON-LD) and light text helpers."""

from __future__ import annotations

import html as htmllib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from selectolax.parser import HTMLParser, Node

_WS = re.compile(r"[ \t\r\f\v]+")
_NL = re.compile(r"\n{3,}")
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"“(])")

NOISE_TAGS = {"script", "style", "noscript", "svg", "iframe", "canvas", "template", "head", "video", "audio", "picture", "source"}
BLOCK_TAGS = {
    "p", "div", "li", "ul", "ol", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "td", "th", "table",
    "section", "article", "header", "footer", "blockquote", "figcaption", "nav", "aside", "main", "form",
    "pre", "dd", "dt", "dl", "address", "summary", "details", "option", "label", "button",
}


def clean_ws(s: str) -> str:
    s = htmllib.unescape(s or "")
    s = s.replace("\xa0", " ").replace("​", "").replace("’", "'")
    s = _WS.sub(" ", s)
    s = re.sub(r" *\n *", "\n", s)
    return _NL.sub("\n\n", s).strip()


@dataclass
class ParsedPage:
    url: str
    title: str = ""
    meta_description: str = ""
    og_site_name: str = ""
    og_title: str = ""
    lang_attr: str = ""
    canonical: str = ""
    text: str = ""
    headings: list[str] = field(default_factory=list)
    links: list[tuple[str, str]] = field(default_factory=list)  # (href, anchor text)
    jsonld: list[dict[str, Any]] = field(default_factory=list)
    dates: list[str] = field(default_factory=list)
    html: str = ""
    blocks: list[str] = field(default_factory=list)  # text split by block boundaries (paragraph-ish)

    @property
    def text_len(self) -> int:
        return len(self.text)


def _jsonld_blocks(tree: HTMLParser) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for node in tree.css('script[type="application/ld+json"]'):
        raw = node.text(strip=True)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            try:
                data = json.loads(re.sub(r",\s*([}\]])", r"\1", raw))
            except json.JSONDecodeError:
                continue
        if isinstance(data, list):
            out.extend(d for d in data if isinstance(d, dict))
        elif isinstance(data, dict):
            graph = data.get("@graph")
            if isinstance(graph, list):
                out.extend(d for d in graph if isinstance(d, dict))
            out.append(data)
    return out


def _walk_text(node: Node, out: list[str]) -> None:
    for child in node.iter(include_text=True):
        tag = child.tag
        if tag == "-text":
            out.append(child.text_content or "")
            continue
        if tag in NOISE_TAGS:
            continue
        if tag in BLOCK_TAGS:
            out.append("\n")
        _walk_text(child, out)
        if tag in BLOCK_TAGS:
            out.append("\n")


_DATE_META = {"article:modified_time", "article:published_time", "og:updated_time", "datemodified", "datepublished", "last-modified"}


def parse_html(html: str, url: str = "") -> ParsedPage:
    """Extract everything the extractors need in a single pass."""
    page = ParsedPage(url=url, html=html)
    if not html:
        return page
    tree = HTMLParser(html)
    root = tree.root
    if root is None:
        return page

    html_node = tree.css_first("html")
    page.lang_attr = (html_node.attributes.get("lang") or "").lower()[:10] if html_node else ""
    t = tree.css_first("title")
    page.title = clean_ws(t.text()) if t else ""
    for m in tree.css("meta"):
        a = m.attributes
        name = (a.get("name") or a.get("property") or a.get("itemprop") or "").lower()
        content = a.get("content") or ""
        if name == "description" and not page.meta_description:
            page.meta_description = clean_ws(content)[:600]
        elif name == "og:site_name":
            page.og_site_name = clean_ws(content)[:200]
        elif name == "og:title":
            page.og_title = clean_ws(content)[:300]
        elif name in _DATE_META or name.endswith("modified_time") or name.endswith("published_time"):
            if content:
                page.dates.append(content[:40])
        elif name == "og:locale" and not page.lang_attr and content:
            page.lang_attr = content.lower()[:10]
    c = tree.css_first('link[rel="canonical"]')
    if c:
        page.canonical = c.attributes.get("href") or ""

    page.jsonld = _jsonld_blocks(tree)
    for d in page.jsonld:
        for k in ("dateModified", "datePublished"):
            v = d.get(k)
            if isinstance(v, str):
                page.dates.append(v[:40])

    for a in tree.css("a[href]"):
        href = a.attributes.get("href") or ""
        page.links.append((href, clean_ws(a.text())[:200]))

    for h in tree.css("h1, h2, h3"):
        txt = clean_ws(h.text())
        if txt and len(txt) < 300:
            page.headings.append(txt)

    body = tree.body or root
    parts: list[str] = []
    _walk_text(body, parts)
    text = clean_ws("".join(parts))
    page.text = text[:400_000]
    page.blocks = [b.strip() for b in text.split("\n") if len(b.strip()) > 1]
    return page


def sentences(text: str) -> list[str]:
    out: list[str] = []
    for para in text.split("\n"):
        para = para.strip()
        if not para:
            continue
        out.extend(s.strip() for s in _SENT_SPLIT.split(para) if s.strip())
    return out


def snippet_around(text: str, needle: str, width: int = 160) -> str:
    i = text.lower().find(needle.lower())
    if i < 0:
        return ""
    a = max(0, i - width)
    b = min(len(text), i + len(needle) + width)
    return clean_ws(text[a:b]).replace("\n", " ")


_YEAR = re.compile(r"\b(199\d|20[0-4]\d)\b")


def years_in(text: str) -> list[int]:
    return [int(y) for y in _YEAR.findall(text)]


def titlecase_ratio(s: str) -> float:
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z'’.-]*", s) if len(w) > 1]
    if not words:
        return 0.0
    return sum(1 for w in words if w[0].isupper()) / len(words)
