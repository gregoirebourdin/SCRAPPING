"""Published email extraction from web pages (precision first; used by ``scout.crawl.parser``).

Sources, strongest first:

* ``mailto:`` links — percent-encoding, several recipients, ``?to=`` / ``cc`` / ``bcc`` query fields;
* Cloudflare email protection — ``data-cfemail`` attributes and ``/cdn-cgi/l/email-protection#<hex>``
  links (hex string, first byte is the XOR key of the following bytes);
* structured data — JSON-LD ``email`` keys at any depth, JSON data islands (``__NEXT_DATA__`` and other
  ``<script type="application/json">``), every string scanned after JSON decoding;
* visible text — plain addresses; addresses glued to the next inline element ("…@acme.frTél");
  bracketed obfuscations (``jean [at] acme [dot] fr``, ``marie(at)acme.fr``, ``{arobase}``,
  ``[@]``) which are unambiguous; and weak spaced forms (``jean at acme dot fr``, ``jean at acme.fr``,
  ``hello @ acme.fr``) accepted only with corroborating context (see :func:`_weak_ok`);
* text made readable by CSS reversal (``unicode-bidi: bidi-override`` + ``direction: rtl``, ``<bdo
  dir=rtl>``) — only when the reversed text is exactly one address.

Text is scanned after removing invisible characters (zero-width, word joiner, soft hyphen), decoding
leftover HTML entities and NFKC normalization (full-width ``＠``). Every candidate goes through
:func:`clean_email`, which also requires a real public suffix. Nothing is ever guessed: an address is
returned only when its characters are on the page once de-obfuscated.
"""

from __future__ import annotations

import html as html_lib
import json
import re
import unicodedata
from collections.abc import Iterable, Iterator
from typing import Any
from urllib.parse import unquote

from selectolax.lexbor import LexborHTMLParser

from scout.util.text import ascii_fold
from scout.util.urls import registrable_domain

MAX_EMAILS = 50
MAX_JSON_BYTES = 1_000_000
MAX_JSON_STRINGS = 20_000
WEAK_CUE_WINDOW = 160

EMAIL_RE = re.compile(
    r"(?<![\w.+-])[a-z0-9][a-z0-9._%+\-]{0,63}@[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?)*\.[a-z]{2,24}(?![\w-])",
    re.IGNORECASE,
)
_LOCAL = r"[a-z0-9][a-z0-9._%+\-]{0,63}"
_LABEL = r"[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?"
# Address glued to the next inline element: lowercase TLD immediately followed by an upper-case letter
# or a digit ("contact@acme.frTél", "jean@acme.fr0123…"). Case-sensitive on purpose.
_GLUED_RE = re.compile(
    r"(?<![\w.+-])([A-Za-z0-9][A-Za-z0-9._%+\-]{0,63}@(?:[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?\.)+[a-z]{2,24})(?=[A-Z0-9])"
)
_AT_BRACKET = r"\s*[\[\(\{<]\s*(?:at|@|arobase|chez)\s*[\]\)\}>]\s*"
_DOT_BRACKET = r"\s*[\[\(\{<]\s*(?:dot|point|punkt|\.)\s*[\]\)\}>]\s*"
_BRACKET_RE = re.compile(
    rf"(?<![\w.+-])({_LOCAL}){_AT_BRACKET}({_LABEL}(?:(?:{_DOT_BRACKET}|\.){_LABEL})+)(?![\w-])",
    re.IGNORECASE,
)
_WORD_RE = re.compile(
    rf"(?<![\w.+-])({_LOCAL})\s+(at|arobase)\s+({_LABEL}(?:(?:\s+(?:dot|point)\s+|\.){_LABEL})+)(?![\w-])",
    re.IGNORECASE,
)
_SPACED_SYMBOL_RE = re.compile(
    rf"(?<![\w.+-])({_LOCAL})\s+@\s+({_LABEL}(?:\.{_LABEL})+)(?![\w-])",
    re.IGNORECASE,
)
_DOT_SEP_RE = re.compile(rf"{_DOT_BRACKET}|\s+(?:dot|point)\s+|\s*\.\s*", re.IGNORECASE)
_UPPER_WORDS_RE = re.compile(r"\b(?:AT|DOT)\b")
_CUE_RE = re.compile(
    r"\b(?:e-?mail|mail|mailto|courriel|mel|contact\w*|ecri\w*|write|reach|joindre|adresse|address"
    r"|kontakt\w*|schreib\w*|envoy\w*|send|correo|contatt\w*|scriv\w*)\b",
    re.IGNORECASE,
)
_INVISIBLE = dict.fromkeys(map(ord, "​‌‍⁠﻿­᠎⁡⁢⁣"))
_BIDI_STYLE_RE = re.compile(r"unicode-bidi\s*:\s*bidi-override", re.IGNORECASE)
_RTL_STYLE_RE = re.compile(r"direction\s*:\s*rtl", re.IGNORECASE)

# Words that precede "at" in prose ("contact us at", "born at the …") or follow it ("at the …"):
# never a local part / first domain label of a spaced obfuscation.
_STOPWORDS = frozenset(
    """
a an the and or but nor of to in on for with by from as at up out off over into onto via per
i me my we us our you your he him his she her it its they them their one all both each this that these those
is are was were be been am being do does did done have has had will would can could may might must shall
here there now then today tonight tomorrow soon later anytime always also just simply only directly
please thanks touch line work works working worked born made held based located available open live
lives living stay stays meet met see seen find found visit call reach look looking join joined arrive
arrived release released launch launched publish published host hosted event venue booth stand noon
night home scale least best first last once time times risk school dot point
nous vous moi toi lui eux elle elles ici la le les un une des du de et ou chez notre votre nos vos
uns mich dich ihn sie wir ihr der die das und oder bei
""".split()
)

_FILE_EXTS = frozenset(
    """
png jpg jpeg gif svg webp avif ico bmp tif tiff css js mjs json map woff woff2 ttf eot otf mp4 webm mov
mp3 wav pdf zip php html htm
""".split()
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
    """
prenom.nom nom.prenom firstname.lastname first.last john.doe jane.doe johndoe you your.name yourname
votre.nom votrenom name username user exemple example email votremail votre-email votreemail votre.email
your-email youremail mail nom prenom
""".split()
)


# ---- validation ---------------------------------------------------------------------------------


def clean_email(raw: str) -> str | None:
    """Lowercase + validate + drop asset filenames / tracker noise / placeholders / unknown TLDs."""
    e = unquote(raw or "").translate(_INVISIBLE).strip().strip(".,;:<>()[]{}\"'").lower()
    if e.startswith("mailto:"):
        e = e[7:]
    e = e.split("?", 1)[0].strip()
    if not EMAIL_RE.fullmatch(e):
        return None
    local, _, domain = e.rpartition("@")
    tld = domain.rsplit(".", 1)[-1]
    if tld in _FILE_EXTS:
        return None
    if re.search(r"@\d+x\.", e) or re.fullmatch(r"[0-9a-f]{16,}", local):
        return None
    if any(domain == d or domain.endswith("." + d) for d in _NOISE_EMAIL_DOMAINS):
        return None
    if local in _PLACEHOLDER_LOCALS:
        return None
    if registrable_domain(domain) is None:  # "acme.frtel", reversed "…@x.naej": not a public suffix
        return None
    return e


def decode_cfemail(hexstr: str) -> str | None:
    """Cloudflare email protection: first byte is the XOR key of the remaining bytes."""
    try:
        data = bytes.fromhex((hexstr or "").strip())
    except ValueError:
        return None
    if len(data) < 2:
        return None
    key = data[0]
    try:
        return bytes(b ^ key for b in data[1:]).decode("utf-8")
    except UnicodeDecodeError:
        return None


def cfemail_from_href(href: str) -> str | None:
    if "/cdn-cgi/l/email-protection" not in href.lower() or "#" not in href:
        return None
    return decode_cfemail(href.rsplit("#", 1)[1])


def emails_from_mailto(href: str) -> list[str]:
    """Recipients of a ``mailto:`` URL: path (comma/semicolon separated) + ``to`` / ``cc`` / ``bcc``."""
    body = href.split(":", 1)[1] if ":" in href else ""
    path, _, query = body.partition("?")
    parts = re.split(r"[,;]", unquote(path))
    for pair in query.split("&"):
        key, _, value = pair.partition("=")
        if unquote(key).strip().lower() in ("to", "cc", "bcc"):
            parts.extend(re.split(r"[,;]", unquote(value)))
    return _unique(clean_email(p) for p in parts if p.strip())


def _unique(items: Iterable[str | None]) -> list[str]:
    out: list[str] = []
    for e in items:
        if e and e not in out:
            out.append(e)
    return out


# ---- text -------------------------------------------------------------------------------------


def scan_form(text: str) -> str:
    """Text as scanned for addresses: invisible characters removed, entities decoded, NFKC."""
    t = (text or "").translate(_INVISIBLE)
    if "&" in t:
        t = html_lib.unescape(t)
    return unicodedata.normalize("NFKC", t)


def _domain_from(raw: str) -> str:
    return re.sub(r"\s+", "", _DOT_SEP_RE.sub(".", raw))


def _weak_ok(
    line: str,
    start: int,
    local: str,
    domain: str,
    matched: str,
    site_domain: str | None,
    strong_on_line: bool,
) -> bool:
    """Spaced forms ("jean at acme dot fr") are accepted only when the parts are not prose words and
    something corroborates them: AT/DOT written in capitals, the site's own domain, an email cue word
    shortly before on the same line, or a non-ambiguous address on the same line."""
    loc = ascii_fold(local).lower()
    first_label = ascii_fold(domain.split(".", 1)[0]).lower()
    if len(loc) < 2 or loc.isdigit() or loc in _STOPWORDS or loc.split(".")[-1] in _STOPWORDS:
        return False
    if first_label in _STOPWORDS:
        return False
    if len(_UPPER_WORDS_RE.findall(matched)) >= 2:
        return True
    if site_domain and registrable_domain(domain) == site_domain:
        return True
    if strong_on_line:
        return True
    before = ascii_fold(line[max(0, start - WEAK_CUE_WINDOW) : start])
    return bool(_CUE_RE.search(before))


def emails_from_text(text: str, *, site_domain: str | None = None) -> list[str]:
    """Plain, glued, bracketed and (corroborated) spaced addresses in visible text."""
    found: list[str | None] = []
    for line in scan_form(text).split("\n"):
        if "@" not in line and not re.search(r"\b(?:at|arobase|chez)\b", line, re.IGNORECASE):
            continue
        strong: list[str | None] = [clean_email(m.group(0)) for m in EMAIL_RE.finditer(line)]
        strong += [clean_email(m.group(1)) for m in _GLUED_RE.finditer(line)]
        for m in _BRACKET_RE.finditer(line):
            strong.append(clean_email(f"{m.group(1)}@{_domain_from(m.group(2))}"))
        found.extend(strong)
        strong_on_line = any(strong)
        for m in _WORD_RE.finditer(line):
            local, domain = m.group(1), _domain_from(m.group(3))
            if _weak_ok(line, m.start(), local, domain, m.group(0), site_domain, strong_on_line):
                found.append(clean_email(f"{local}@{domain}"))
        for m in _SPACED_SYMBOL_RE.finditer(line):
            local, domain = m.group(1), m.group(2)
            if _weak_ok(line, m.start(), local, domain, m.group(0), site_domain, strong_on_line):
                found.append(clean_email(f"{local}@{domain}"))
    return _unique(found)


# ---- structured data ----------------------------------------------------------------------------


def _walk(obj: Any, depth: int = 0) -> Iterator[tuple[str | None, Any]]:
    if depth > 12:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield (k if isinstance(k, str) else None), v
            yield from _walk(v, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            yield None, v
            yield from _walk(v, depth + 1)


def emails_from_jsonld(objects: Iterable[Any]) -> list[str]:
    """``email`` values at any depth (contactPoint, employees, founders…), strings or lists."""
    found: list[str | None] = []
    for obj in objects:
        for key, value in _walk(obj):
            if key is None or key.lower() != "email":
                continue
            for v in value if isinstance(value, list) else [value]:
                if isinstance(v, str):
                    found.append(clean_email(v))
    return _unique(found)


def emails_from_json_island(raw: str) -> list[str]:
    """Every address inside the decoded strings of a JSON data island (``__NEXT_DATA__`` …)."""
    if not raw or len(raw) > MAX_JSON_BYTES or "@" not in raw:
        return []
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        return []
    found: list[str | None] = []
    seen = 0
    for _key, value in _walk(data):
        if not isinstance(value, str) or "@" not in value:
            continue
        seen += 1
        if seen > MAX_JSON_STRINGS:
            break
        found.extend(clean_email(m.group(0)) for m in EMAIL_RE.finditer(scan_form(value)))
    return _unique(found)


# ---- DOM-level sources ------------------------------------------------------------------------


def _reversed_text_email(text: str) -> str | None:
    candidate = scan_form(text).strip()[::-1].strip()
    return clean_email(candidate) if EMAIL_RE.fullmatch(candidate) else None


def emails_from_tree(tree: LexborHTMLParser) -> list[str]:
    """DOM-only sources not covered by links / visible text: ``data-cfemail`` attributes, CSS-reversed
    addresses and JSON data islands."""
    found: list[str | None] = []
    for node in tree.css("[data-cfemail]"):
        decoded = decode_cfemail(node.attributes.get("data-cfemail") or "")
        found.append(clean_email(decoded) if decoded else None)
    for node in tree.css("bdo[dir], [style]"):
        attrs = node.attributes
        style = attrs.get("style") or ""
        rtl = (node.tag == "bdo" and (attrs.get("dir") or "").lower() == "rtl") or (
            bool(_BIDI_STYLE_RE.search(style))
            and (bool(_RTL_STYLE_RE.search(style)) or (attrs.get("dir") or "").lower() == "rtl")
        )
        if rtl:
            found.append(_reversed_text_email(node.text(deep=True) or ""))
    for script in tree.css('script[type="application/json"], script#__NEXT_DATA__'):
        found.extend(emails_from_json_island(script.text(deep=True) or ""))
    return _unique(found)
