"""Frozen copy of the email extraction rules as they were before the extraction rework (git HEAD of
``scout/crawl/parser.py``), kept only as the "before" baseline of the benchmark. Visible text and JSON-LD
come from the current parser (unchanged code paths); only the email rules are the old ones."""

from __future__ import annotations

import re
from urllib.parse import unquote

from selectolax.lexbor import LexborHTMLParser

from scout.crawl.parser import parse_html

EMAIL_RE = re.compile(
    r"(?<![\w.+-])[a-z0-9][a-z0-9._%+\-]{0,63}@[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?)*\.[a-z]{2,24}(?![\w-])",
    re.IGNORECASE,
)
_OBFUSCATED_EMAIL_RE = re.compile(
    r"([a-z0-9][a-z0-9._%+\-]{0,63})\s*[\[\(\{<]\s*(?:at|arobase|@)\s*[\]\)\}>]\s*"
    r"([a-z0-9\-]+(?:\s*(?:[\[\(\{<]\s*(?:dot|point|\.)\s*[\]\)\}>]|\.)\s*[a-z0-9\-]+)+)",
    re.IGNORECASE,
)
_SPACED_EMAIL_RE = re.compile(
    r"\b([a-z0-9][a-z0-9._\-]{0,63})\s+(?:at|arobase)\s+([a-z0-9\-]+(?:\s+(?:dot|point)\s+[a-z0-9\-]+)+)\b",
    re.IGNORECASE,
)
_DOT_TOKEN_RE = re.compile(
    r"\s*(?:[\[\(\{<]\s*(?:dot|point|\.)\s*[\]\)\}>]|\s(?:dot|point)\s|\.)\s*", re.IGNORECASE
)
_FILE_EXTS = frozenset(
    "png jpg jpeg gif svg webp avif ico bmp tif tiff css js mjs json map woff woff2 ttf eot otf mp4 webm mov "
    "mp3 wav pdf zip php html htm".split()
)
_NOISE_EMAIL_DOMAINS = (
    "sentry.io",
    "wixpress.com",
    "sentry-next.wixpress.com",
    "example.com",
    "example.org",
    "example.net",
    "example.fr",
    "domain.com",
    "domaine.com",
    "domaine.fr",
    "yourdomain.com",
    "votredomaine.fr",
    "votredomaine.com",
    "mysite.com",
    "monsite.fr",
    "test.com",
    "email.com",
    "sentry.com",
    "ingest.sentry.io",
)
_PLACEHOLDER_LOCALS = frozenset(
    "prenom.nom nom.prenom firstname.lastname first.last john.doe jane.doe johndoe you your.name yourname "
    "votre.nom votrenom name username user exemple example email votremail votre-email votreemail "
    "votre.email your-email youremail mail nom prenom".split()
)


def clean_email(raw: str) -> str | None:
    e = unquote(raw or "").strip().strip(".,;:<>()[]{}\"'").lower()
    if e.startswith("mailto:"):
        e = e[7:]
    e = e.split("?", 1)[0].strip()
    if not EMAIL_RE.fullmatch(e):
        return None
    local, _, domain = e.rpartition("@")
    if domain.rsplit(".", 1)[-1] in _FILE_EXTS:
        return None
    if re.search(r"@\d+x\.", e) or re.fullmatch(r"[0-9a-f]{16,}", local):
        return None
    if any(domain == d or domain.endswith("." + d) for d in _NOISE_EMAIL_DOMAINS):
        return None
    if local in _PLACEHOLDER_LOCALS:
        return None
    return e


def _decode_cfemail(hexstr: str) -> str | None:
    try:
        data = bytes.fromhex(hexstr)
    except ValueError:
        return None
    if len(data) < 2:
        return None
    try:
        return bytes(b ^ data[0] for b in data[1:]).decode("utf-8")
    except UnicodeDecodeError:
        return None


def emails_from_text(text: str) -> list[str]:
    found = [m.group(0) for m in EMAIL_RE.finditer(text)]
    for rx in (_OBFUSCATED_EMAIL_RE, _SPACED_EMAIL_RE):
        for m in rx.finditer(text):
            dom = re.sub(r"\s+", "", _DOT_TOKEN_RE.sub(".", m.group(2)))
            found.append(f"{m.group(1)}@{dom}")
    out: list[str] = []
    for raw in found:
        e = clean_email(raw)
        if e and e not in out:
            out.append(e)
    return out


def legacy_extract(html: str, url: str) -> list[str]:
    tree = LexborHTMLParser(html)
    raw: list[str] = []
    for a in tree.css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        low = href.lower()
        if low.startswith("mailto:"):
            raw.extend(p for p in re.split(r"[,;]", href[7:].split("?", 1)[0]) if p)
        elif "/cdn-cgi/l/email-protection" in low and "#" in href:
            decoded = _decode_cfemail(href.rsplit("#", 1)[1])
            if decoded:
                raw.append(decoded)
    for node in tree.css("[data-cfemail]"):
        decoded = _decode_cfemail(node.attributes.get("data-cfemail") or "")
        if decoded:
            raw.append(decoded)
    page = parse_html(html, url)
    emails: list[str] = []
    for r in raw:
        e = clean_email(r)
        if e and e not in emails:
            emails.append(e)
    for e in emails_from_text(page.content_text):
        if e not in emails:
            emails.append(e)
    for obj in page.structured_data:
        val = obj.get("email") if isinstance(obj, dict) else None
        if isinstance(val, str):
            e = clean_email(val)
            if e and e not in emails:
                emails.append(e)
    return emails[:50]
